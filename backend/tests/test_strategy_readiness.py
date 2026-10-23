"""
R6: strategy membership, broker-pair and instrument-mapping coverage diagnostics. Fixtures and mocks only - no broker, Redis,
network or production access. The diagnostics are read-only: they reuse the R5 pair gate (no second policy), never report a
candidate ISIN as a verified identity, and say so explicitly when evidence is absent instead of reporting zero gaps.
"""
import ast
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.config import Settings, settings
from app.models.accounts import Account, AccountType
from app.models.audit import AuditLog, SystemEvent
from app.models.strategies import StrategyAccountRole, StrategyMember
from app.models.trades import CopyTrade, Order, OrderSide, OrderSource, OrderStatus
from app.services import strategy_readiness as sr
from app.services import strategy_switch
from app.services.brokers import pairs
from app.services.instruments import crosswalk_evidence as ce
from app.services.instruments import nse_crosswalk as cwm
from conftest import TestingSessionLocal
from strategy_helpers import link_strategy
from test_crosswalk_evidence import make_file
from test_strategy_master_switching import AUTH, _account, _provider, api_client, trio  # noqa: F401  (fixtures)

APP = Path(__file__).resolve().parent.parent / "app"
LINK = "MASTER_TOKEN_LINK_UNVERIFIED"
ALICE = "alicebluenew"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    ce.clear_cache()
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", "")
    yield
    ce.clear_cache()


def ist_today():
    return datetime.now(timezone.utc).astimezone(cwm.IST).date()


async def set_broker(account, broker):
    async with TestingSessionLocal() as s:
        (await s.get(Account, account.id)).broker = broker
        await s.commit()


async def master_order(db, master, symbol, token, exchange="NSE", hours=1.0):
    db.add(Order(id=uuid.uuid4(), internal_order_id=f"EXT_{uuid.uuid4().hex[:10]}", broker_order_id=uuid.uuid4().hex[:8],
                 account_id=master.id, source=OrderSource.BROKER_EXTERNAL, exchange=exchange, symbol=symbol, side=OrderSide.BUY,
                 order_type="LIMIT", product_type="MIS", quantity=1, filled_quantity=0, pending_quantity=1,
                 price=Decimal("1"), status=OrderStatus.OPEN, created_at=datetime.now(timezone.utc) - timedelta(hours=hours),
                 instrument_token=token))
    await db.commit()


async def counts():
    out = []
    async with TestingSessionLocal() as s:
        for model in (Account, StrategyMember, StrategyAccountRole, Order, CopyTrade, AuditLog, SystemEvent):
            out.append((await s.execute(select(func.count()).select_from(model))).scalar_one())
    return tuple(out)


# ── pair readiness: one policy ───────────────────────────────────────────────────────────

def test_alice_to_alice_is_reported_supported():
    p = sr.pair_readiness(ALICE, ALICE)
    assert p["supported"] is True and p["code"] is None and p["requirement"] is None
    assert (p["master_broker"], p["child_broker"]) == (ALICE, ALICE)


@pytest.mark.parametrize("child", ["groww", "angelone"])
def test_groww_and_angel_children_report_broker_pair_not_supported(child):
    p = sr.pair_readiness(ALICE, child)
    assert p["supported"] is False and p["code"] == "BROKER_PAIR_NOT_SUPPORTED" and p["requirement"] == "PAIR_NOT_ALLOWLISTED"
    assert "NOT be copied" in p["message"] and child in p["detail"]


@pytest.mark.parametrize("master,child", [(None, ALICE), (ALICE, "zerodha"), ("nonsense", ALICE), (ALICE, "totally-unknown")])
def test_unknown_brokers_and_a_missing_master_fail_closed(master, child):
    p = sr.pair_readiness(master, child)
    assert p["supported"] is False and p["code"] == "BROKER_PAIR_NOT_SUPPORTED"


def test_a_missing_capability_profile_fails_closed(monkeypatch):
    monkeypatch.setattr(pairs, "build_profile", lambda b: None)
    p = sr.pair_readiness(ALICE, ALICE)
    assert p["supported"] is False and p["requirement"] == "EVIDENCE_BELOW_LIVE" or "NO_PROFILE" in p["detail"]


def test_the_readiness_helper_delegates_to_the_r5_gate_and_has_no_policy_of_its_own(monkeypatch):
    from app.services.brokers.pairs import PairDecision
    monkeypatch.setattr(sr, "check_pair", lambda m, c: PairDecision(False, "BROKER_PAIR_NOT_SUPPORTED", "PAIR_NOT_ALLOWLISTED:x->y"))
    assert sr.pair_readiness(ALICE, ALICE)["supported"] is False                 # the gate said no, so does the diagnostic
    monkeypatch.setattr(sr, "check_pair", lambda m, c: PairDecision(True, None, "ok"))
    assert sr.pair_readiness(ALICE, "groww")["supported"] is True                # and yes: nothing else is consulted
    src = (APP / "services" / "strategy_readiness.py").read_text(encoding="utf-8")
    assert "SUPPORTED_PAIRS" not in src and "can_be_child" not in src and "can_be_master" not in src


def test_a_strategy_without_a_master_has_unknown_pairs_never_supported():
    p = sr.no_master_pair("groww")
    assert p["supported"] is None and p["requirement"] == "NO_MASTER" and p["child_broker"] == "groww"


def test_members_without_a_master_report_unknown_pairs_and_a_truncated_window_is_a_warning():
    from types import SimpleNamespace
    member = SimpleNamespace(trading_enabled=True, account=SimpleNamespace(display_name="c", broker=ALICE))
    r = sr.strategy_readiness(SimpleNamespace(master_account=None, members=[member]))
    assert r["children"][0]["broker_pair"]["supported"] is None and r["broker_pairs_ready"] is False
    assert r["summary"]["children_pair_unsupported"] == 0 and "NO_MASTER" in {w["code"] for w in r["warnings"]}
    warned = sr.coverage_warnings({"status": "AVAILABLE", "truncated": True, "by_state": {}, "snapshot": None})
    assert [w["code"] for w in warned] == ["MAPPING_COVERAGE_TRUNCATED"]


# ── the strategy and member APIs ─────────────────────────────────────────────────────────

async def two_child_strategy(db_session, *, groww_enabled=True):
    master = await _account(db_session, "master", AccountType.MASTER, copy_setting=False)
    alice_child = await _account(db_session, "achild")
    groww_child = await _account(db_session, "gchild")
    await set_broker(groww_child, "groww")
    strategy = await link_strategy(db_session, master, [alice_child, groww_child], name="mix")
    if not groww_enabled:
        async with TestingSessionLocal() as s:
            row = (await s.execute(select(StrategyMember).where(StrategyMember.account_id == groww_child.id))).scalar_one()
            row.trading_enabled = False
            await s.commit()
    return master, alice_child, groww_child, strategy


@pytest.mark.asyncio
async def test_strategy_response_is_additive_and_shows_unsupported_membership(api_client, db_session):
    master, alice_child, groww_child, strategy = await two_child_strategy(db_session)
    body = (await api_client.get(f"/api/v1/strategies/{strategy.id}", headers=AUTH)).json()

    assert {"id", "name", "master_account_id", "trading_enabled", "member_count", "master_account", "members"} <= set(body)  # unchanged
    by_name = {m["account"]["display_name"]: m for m in body["members"]}
    assert by_name["achild"]["broker_pair"]["supported"] is True
    groww = by_name["gchild"]["broker_pair"]
    assert groww["supported"] is False and groww["code"] == "BROKER_PAIR_NOT_SUPPORTED" and groww["child_broker"] == "groww"
    assert body["member_count"] == 2                                          # membership itself is unchanged

    r = body["readiness"]
    assert r["broker_pairs_ready"] is False and r["summary"]["children_pair_unsupported"] == 1
    assert r["summary"]["children_pair_supported"] == 1 and r["observe_only"] is True
    (warn,) = [w for w in r["warnings"] if w["code"] == "BROKER_PAIR_NOT_SUPPORTED"]
    assert warn["child"] == "gchild" and warn["requirement"] == "PAIR_NOT_ALLOWLISTED"
    assert "not mean orders will be placed" in r["note"]                      # ready is not "will trade"


@pytest.mark.asyncio
async def test_no_account_ids_or_secrets_are_added_by_the_diagnostics(api_client, db_session):
    master, alice_child, groww_child, strategy = await two_child_strategy(db_session)
    body = (await api_client.get(f"/api/v1/strategies/{strategy.id}", headers=AUTH)).json()
    text = json.dumps(body["readiness"]).lower()
    for ident in (master.id, alice_child.id, groww_child.id, master.alice_user_id, alice_child.alice_user_id):
        assert str(ident).lower() not in text
    assert not any(w in text for w in ("secret", "password", "bearer", "api_key", "session_token"))


@pytest.mark.asyncio
async def test_the_member_endpoints_carry_the_pair_and_stay_compatible(api_client, db_session):
    master, alice_child, groww_child, strategy = await two_child_strategy(db_session)
    rows = (await api_client.get(f"/api/v1/strategies/{strategy.id}/members", headers=AUTH)).json()
    assert {"id", "strategy_id", "account_id", "multiplier", "trading_enabled", "created_at", "account"} <= set(rows[0])
    assert {r["account"]["display_name"]: r["broker_pair"]["supported"] for r in rows} == {"achild": True, "gchild": False}

    upd = (await api_client.put(f"/api/v1/strategies/{strategy.id}/members/{groww_child.id}",
                                json={"multiplier": "2.00"}, headers=AUTH)).json()
    assert upd["broker_pair"]["supported"] is False and upd["broker_pair"]["code"] == "BROKER_PAIR_NOT_SUPPORTED"

    extra = await _account(db_session, "newchild")
    added = await api_client.post(f"/api/v1/strategies/{strategy.id}/members", json={"account_id": str(extra.id)}, headers=AUTH)
    assert added.status_code == 201 and added.json()["broker_pair"]["supported"] is True


@pytest.mark.asyncio
async def test_a_disabled_child_is_shown_but_does_not_block_readiness(api_client, db_session):
    master, alice_child, groww_child, strategy = await two_child_strategy(db_session, groww_enabled=False)
    r = (await api_client.get(f"/api/v1/strategies/{strategy.id}", headers=AUTH)).json()["readiness"]
    groww = [c for c in r["children"] if c["display_name"] == "gchild"][0]
    assert groww["trading_enabled"] is False and groww["broker_pair"]["supported"] is False     # still visible as unsupported
    assert r["broker_pairs_ready"] is True and r["summary"]["children_pair_unsupported"] == 0


@pytest.mark.asyncio
async def test_a_strategy_without_a_master_is_unknown_not_ready(api_client):
    resp = await api_client.post("/api/v1/strategies", json={"name": "empty"}, headers=AUTH)
    body = (await api_client.get(f"/api/v1/strategies/{resp.json()['id']}/readiness", headers=AUTH)).json()
    assert body["master"] is None and body["broker_pairs_ready"] is False
    assert {w["code"] for w in body["warnings"]} >= {"NO_MASTER", "MAPPING_COVERAGE_UNKNOWN"}
    assert body["mapping_coverage"]["status"] == "NO_MASTER" and body["mapping_coverage"]["verified_identities"] == 0


@pytest.mark.asyncio
async def test_a_master_with_an_unrecognised_broker_fails_closed(api_client, db_session):
    master, alice_child, groww_child, strategy = await two_child_strategy(db_session)
    await set_broker(master, "not-a-broker")
    r = (await api_client.get(f"/api/v1/strategies/{strategy.id}", headers=AUTH)).json()["readiness"]
    assert r["broker_pairs_ready"] is False
    assert {c["broker_pair"]["requirement"] for c in r["children"]} == {"MASTER_BROKER_UNKNOWN"}


@pytest.mark.asyncio
async def test_a_null_master_broker_is_alice_as_at_order_time(api_client, db_session):
    master, alice_child, groww_child, strategy = await two_child_strategy(db_session)
    await set_broker(master, None)
    r = (await api_client.get(f"/api/v1/strategies/{strategy.id}", headers=AUTH)).json()["readiness"]
    assert r["master"]["broker"] == ALICE
    assert [c["broker_pair"]["supported"] for c in r["children"] if c["display_name"] == "achild"] == [True]


# ── Master switch ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_switch_preflight_warns_about_children_the_new_master_would_leave_unsupported(db_session, trio):
    await set_broker(trio.vikash, "groww")
    await db_session.refresh(trio.vikash)                                      # the broker was changed through another session
    pre = await strategy_switch.run_preflight(db_session, trio.strategy.id, trio.arun.id, "CHILD", _provider(trio.brokers))
    pair = [w for w in pre.warnings if w.code == "BROKER_PAIR_NOT_SUPPORTED"]
    assert {(w.account_name, w.role) for w in pair} == {("vikash", "CHILD")}
    assert pair[0].account_id is None and "PAIR_NOT_ALLOWLISTED" in pair[0].items[0]
    assert pre.allowed is True and pre.blockers == []                          # informational: the switch semantics are unchanged


@pytest.mark.asyncio
async def test_switch_preflight_checks_the_old_master_when_it_stays_as_a_child(db_session, trio):
    await set_broker(trio.dhinesh, "groww")
    await db_session.refresh(trio.dhinesh)
    clean_pre = await strategy_switch.run_preflight(db_session, trio.strategy.id, trio.arun.id, "REMOVE", _provider(trio.brokers))
    assert not [w for w in clean_pre.warnings if w.code == "BROKER_PAIR_NOT_SUPPORTED"]
    kept = await strategy_switch.run_preflight(db_session, trio.strategy.id, trio.arun.id, "CHILD", _provider(trio.brokers))
    (w,) = [w for w in kept.warnings if w.code == "BROKER_PAIR_NOT_SUPPORTED"]
    assert (w.account_name, w.role) == ("dhinesh", "CURRENT_MASTER")


@pytest.mark.asyncio
async def test_a_fully_supported_switch_has_no_pair_warning_and_the_api_dict_is_unchanged_in_shape(db_session, trio):
    pre = await strategy_switch.run_preflight(db_session, trio.strategy.id, trio.arun.id, "CHILD", _provider(trio.brokers))
    assert not [w for w in pre.warnings if w.code == "BROKER_PAIR_NOT_SUPPORTED"]
    assert {"allowed", "can_override", "blockers", "warnings", "limitations", "checked_accounts",
            "requires_cnc_acknowledgement"} <= set(pre.to_dict())


@pytest.mark.asyncio
async def test_the_switch_preflight_endpoint_still_blocks_a_non_master_broker_exactly_as_before(api_client, db_session, trio):
    await set_broker(trio.arun, "groww")
    resp = await api_client.get(f"/api/v1/strategies/{trio.strategy.id}/switch-master/preflight",
                                params={"new_master_account_id": str(trio.arun.id)}, headers=AUTH)
    assert resp.status_code == 409                                             # NEW_MASTER_BROKER_UNSUPPORTED is unchanged


# ── mapping coverage ─────────────────────────────────────────────────────────────────────

async def mixed_master(db_session):
    master = await _account(db_session, "master", AccountType.MASTER, copy_setting=False)
    child = await _account(db_session, "child")
    strategy = await link_strategy(db_session, master, [child], name="cov")
    await master_order(db_session, master, "SYNALPHA", "9000003")                  # a fresh-snapshot candidate
    await master_order(db_session, master, "SYNALPHA", "9000003", hours=2.0)       # the same instrument again: counted once
    await master_order(db_session, master, "SYNBETA", "9999999")                   # not in the snapshot
    await master_order(db_session, master, "SYNALPHA", None)                       # no token
    await master_order(db_session, master, "TCS", "11536", exchange="BSE")
    await master_order(db_session, master, "NIFTY", "74056", exchange="NFO")
    return master, strategy


async def coverage(api_client, strategy, hours=24):
    resp = await api_client.get(f"/api/v1/strategies/{strategy.id}/readiness", params={"hours": hours}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_with_no_source_nothing_is_called_zero_gaps_and_nothing_is_verified(api_client, db_session):
    master, strategy = await mixed_master(db_session)
    body = await coverage(api_client, strategy)
    cov = body["mapping_coverage"]
    assert cov["status"] == "AVAILABLE" and cov["source"] == "NOT_CONFIGURED" and cov["snapshot"] is None
    assert cov["total_instruments"] == 5 and cov["verified_identities"] == 0 and cov["candidates"] == 0
    assert cov["by_state"] == {"CROSSWALK_UNAVAILABLE": 2, "EXCHANGE_NOT_COVERED": 2, "NO_MASTER_TOKEN": 1}
    assert cov["groups"]["source_unavailable"] == 2 and cov["groups"]["no_mapping_found"] == 0   # absent source != not found
    assert {"MAPPING_SOURCE_UNAVAILABLE", "MAPPING_EXCHANGE_NOT_COVERED", "MAPPING_NO_MASTER_TOKEN"} <= {w["code"] for w in body["warnings"]}


@pytest.mark.asyncio
async def test_a_fresh_snapshot_gives_candidates_that_are_never_verified(api_client, db_session, tmp_path, monkeypatch):
    master, strategy = await mixed_master(db_session)
    path = make_file(tmp_path, snapshot=ist_today())
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", str(path))
    body = await coverage(api_client, strategy)
    cov = body["mapping_coverage"]
    assert cov["source"] == "LOADED" and cov["snapshot"]["stale"] is False and cov["snapshot"]["snapshot_date"] == ist_today().isoformat()
    assert cov["candidates"] == 1 and cov["verified_identities"] == 0 and cov["link_status"] == LINK
    assert cov["groups"]["identity_unverified"] == 1 and cov["groups"]["no_mapping_found"] == 1
    assert cov["by_state"] == {"CANDIDATE": 1, "EXCHANGE_NOT_COVERED": 2, "NO_MASTER_TOKEN": 1, "UNMAPPED": 1}
    codes = {w["code"] for w in body["warnings"]}
    assert {"MAPPING_IDENTITY_UNVERIFIED", "MAPPING_NOT_FOUND"} <= codes and "MAPPING_SOURCE_UNAVAILABLE" not in codes
    text = json.dumps(body)
    assert "ZZSYN001A014" not in text                                          # the diagnostic never prints an ISIN


@pytest.mark.asyncio
async def test_a_stale_snapshot_is_distinct_from_absent_and_unmapped(api_client, db_session, tmp_path, monkeypatch):
    master, strategy = await mixed_master(db_session)
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", str(make_file(tmp_path, snapshot=ist_today() - timedelta(days=6))))
    body = await coverage(api_client, strategy)
    cov = body["mapping_coverage"]
    assert cov["source"] == "LOADED" and cov["snapshot"]["stale"] is True
    assert cov["by_state"].get("STALE_SNAPSHOT") == 2 and "UNMAPPED" not in cov["by_state"] and "CANDIDATE" not in cov["by_state"]
    assert cov["groups"]["source_stale"] == 2 and cov["candidates"] == 0
    assert "MAPPING_SNAPSHOT_STALE" in {w["code"] for w in body["warnings"]}


@pytest.mark.asyncio
async def test_conflicts_are_reported_as_conflicts(api_client, db_session, tmp_path, monkeypatch):
    master, strategy = await mixed_master(db_session)

    def dup(rows):
        twin = dict(next(r for r in rows if r["FinInstrmId"] == "9000003"))
        twin["ISIN"] = "ZZSYN002A012"
        return rows + [twin]

    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", str(make_file(tmp_path, snapshot=ist_today(), rows=dup)))
    cov = (await coverage(api_client, strategy))["mapping_coverage"]
    assert cov["by_state"].get("CONFLICT") == 1 and cov["candidates"] == 0 and cov["groups"]["needs_review"] == 1


@pytest.mark.asyncio
async def test_an_unusable_file_is_reported_with_a_code_only(api_client, db_session, tmp_path, monkeypatch):
    master, strategy = await mixed_master(db_session)
    bad = tmp_path / f"NSE_CM_security_{ist_today().strftime('%d%m%Y')}.csv"
    bad.write_text("FinInstrmId,TckrSymb\n1,A\n", encoding="utf-8")
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", str(bad))
    body = await coverage(api_client, strategy)
    cov = body["mapping_coverage"]
    assert cov["source"] == "FILE_UNUSABLE" and cov["source_error"] == "MISSING_REQUIRED_COLUMNS" and cov["snapshot"] is None
    assert cov["by_state"].get("CROSSWALK_UNAVAILABLE") == 2 and str(tmp_path) not in json.dumps(body)


@pytest.mark.asyncio
async def test_bse_and_nfo_stay_unmapped_even_with_a_matching_token(api_client, db_session, tmp_path, monkeypatch):
    master = await _account(db_session, "master", AccountType.MASTER, copy_setting=False)
    strategy = await link_strategy(db_session, master, [], name="bse")
    await master_order(db_session, master, "SYNALPHA", "9000003", exchange="BSE")     # the NSE token, on BSE
    await master_order(db_session, master, "SYNALPHA", "9000003", exchange="NFO")
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", str(make_file(tmp_path, snapshot=ist_today())))
    cov = (await coverage(api_client, strategy))["mapping_coverage"]
    assert cov["by_state"] == {"EXCHANGE_NOT_COVERED": 2} and cov["candidates"] == 0 and cov["verified_identities"] == 0


@pytest.mark.asyncio
async def test_an_empty_window_is_unknown_not_fully_covered(api_client, db_session):
    master = await _account(db_session, "master", AccountType.MASTER, copy_setting=False)
    strategy = await link_strategy(db_session, master, [], name="quiet")
    await master_order(db_session, master, "SYNALPHA", "9000003", hours=72)           # outside the 24 h window
    body = await coverage(api_client, strategy)
    cov = body["mapping_coverage"]
    assert cov["status"] == "NO_RECENT_ORDERS" and cov["total_instruments"] == 0 and cov["verified_identities"] == 0
    assert "MAPPING_COVERAGE_UNKNOWN" in {w["code"] for w in body["warnings"]}
    wide = (await coverage(api_client, strategy, hours=100000))["mapping_coverage"]      # bounded, not rejected
    assert wide["window_hours"] == 168 and wide["total_instruments"] == 1


@pytest.mark.asyncio
async def test_child_orders_and_other_accounts_are_not_master_instruments(api_client, db_session):
    master = await _account(db_session, "master", AccountType.MASTER, copy_setting=False)
    child = await _account(db_session, "child")
    strategy = await link_strategy(db_session, master, [child], name="own")
    await master_order(db_session, child, "SYNALPHA", "9000003")
    other = await _account(db_session, "other")
    await master_order(db_session, other, "SYNBETA", "9000004")
    cov = (await coverage(api_client, strategy))["mapping_coverage"]
    assert cov["status"] == "NO_RECENT_ORDERS" and cov["total_instruments"] == 0


@pytest.mark.asyncio
async def test_copy_engine_orders_on_the_master_account_are_not_master_instruments(api_client, db_session):
    master = await _account(db_session, "master", AccountType.MASTER, copy_setting=False)
    strategy = await link_strategy(db_session, master, [], name="sys")
    db_session.add(Order(id=uuid.uuid4(), internal_order_id="SYS_1", broker_order_id="b1", account_id=master.id,
                         source=OrderSource.SYSTEM, exchange="NSE", symbol="SYNALPHA", side=OrderSide.BUY, order_type="LIMIT",
                         product_type="MIS", quantity=1, filled_quantity=0, pending_quantity=1, price=Decimal("1"),
                         status=OrderStatus.OPEN, created_at=datetime.now(timezone.utc), instrument_token="9000003"))
    await db_session.commit()
    assert (await coverage(api_client, strategy))["mapping_coverage"]["total_instruments"] == 0


@pytest.mark.asyncio
async def test_unknown_strategy_is_a_404(api_client):
    resp = await api_client.get(f"/api/v1/strategies/{uuid.uuid4()}/readiness", headers=AUTH)
    assert resp.status_code == 404


# ── read-only, isolation, defaults ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_diagnostics_write_nothing_and_change_no_role(api_client, db_session, tmp_path, monkeypatch):
    master, strategy = await mixed_master(db_session)
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", str(make_file(tmp_path, snapshot=ist_today())))
    before = await counts()
    for path in (f"/api/v1/strategies/{strategy.id}", f"/api/v1/strategies/{strategy.id}/members",
                 f"/api/v1/strategies/{strategy.id}/readiness", "/api/v1/strategies"):
        assert (await api_client.get(path, headers=AUTH)).status_code == 200
    assert await counts() == before


def test_the_module_makes_no_broker_redis_or_network_call():
    tree = ast.parse((APP / "services" / "strategy_readiness.py").read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 0}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    banned = ("redis", "httpx", "requests", "urllib", "socket", "websockets")
    assert not [m for m in imported if m and m.split(".")[0] in banned]
    assert not [m for m in imported if m and any(w in m for w in ("alice_blue", "groww", "angel_one", "copy_engine", "runtime"))]
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not called & {"place_order", "modify_order", "cancel_order", "get_funds", "add", "commit", "delete", "request"}


def test_defaults_still_disable_enforcement_and_shadow_observation():
    s = Settings(_env_file=None)
    assert s.CANONICAL_MAPPING_ENFORCED is False and s.INSTRUMENT_SHADOW_ENABLED is False and s.ENGINE_LIVE_TRADING_ENABLED is False


def test_the_order_time_gate_is_not_touched_by_the_diagnostics():
    pipeline = (APP / "services" / "copy_engine" / "pipeline.py").read_text(encoding="utf-8")
    assert "strategy_readiness" not in pipeline
    assert set(pairs.SUPPORTED_PAIRS) == {(ALICE, ALICE)}
