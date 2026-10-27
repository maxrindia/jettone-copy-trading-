"""
Dynamic Master/Child P0: unsupported broker pairs cannot be activated by accident. Fake data and in-memory database only - no broker,
Redis, network or production access. The one policy is the R5 gate; membership, activation and the Master switch consult it, the
pair matrix endpoint reports it, and existing members are preserved and reported, never deleted.
"""
import ast
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.config import Settings, settings
from app.models.accounts import Account, AccountType
from app.models.audit import SystemEvent
from app.models.strategies import GroupStrategy, StrategyAccountRole, StrategyMember
from app.services import strategy_switch
from app.services.brokers import pairs
from conftest import TestingSessionLocal
from strategy_helpers import link_strategy
from test_child_membership_gate import add, set_broker, snapshot, strategy_with_master
from test_strategy_master_switching import AUTH, _account, _Broker, _provider, api_client  # noqa: F401  (api_client is a fixture)
from angel_helpers import SYNTHETIC_CONTRACT

APP = Path(__file__).resolve().parent.parent / "app"
ALICE = "alicebluenew"
CODE = "BROKER_PAIR_NOT_SUPPORTED"


@pytest.fixture
def role_gates_open(monkeypatch):
    """Every flag that opens the Groww and Angel One CHILD role gates, so only the PAIR gate stands between them and a strategy."""
    for name in ("GROWW_ENABLED", "GROWW_CHILD_TRADING_ENABLED", "ANGEL_ONE_ENABLED", "ANGEL_ONE_CHILD_TRADING_ENABLED"):
        monkeypatch.setattr(settings, name, True)
    monkeypatch.setattr("app.services.brokers.registry.ANGEL_CONTRACT", SYNTHETIC_CONTRACT)


async def members_of(strategy_id):
    async with TestingSessionLocal() as s:
        rows = (await s.execute(select(StrategyMember).where(StrategyMember.strategy_id == strategy_id))).scalars().all()
        return {str(r.account_id): (r.trading_enabled, str(r.multiplier)) for r in rows}


async def events(event_type):
    async with TestingSessionLocal() as s:
        return (await s.execute(select(SystemEvent).where(SystemEvent.event_type == event_type))).scalars().all()


# ── membership: add ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_alice_to_alice_is_still_added_and_reported_supported(api_client, db_session):
    sid, _ = await strategy_with_master(api_client, db_session)
    child = await _account(db_session, "alice_child")
    resp = await add(api_client, sid, child)
    assert resp.status_code == 201
    body = resp.json()
    assert body["broker_pair"]["supported"] is True and body["broker_pair"]["code"] is None
    assert {"id", "strategy_id", "account_id", "multiplier", "trading_enabled", "created_at", "account"} <= set(body)   # shape unchanged


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["groww", "angelone"])
@pytest.mark.parametrize("trading", [True, False])
async def test_an_unsupported_pair_is_refused_with_409_and_nothing_is_written(api_client, db_session, role_gates_open, broker, trading):
    sid, _ = await strategy_with_master(api_client, db_session)
    child = await _account(db_session, f"{broker}_child")
    await set_broker(child, broker)
    before = await snapshot()
    resp = await add(api_client, sid, child, trading_enabled=trading)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert isinstance(detail, str) and detail.startswith(CODE)                    # a string, so the frontend proxy keeps showing it
    assert await snapshot() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["zerodha", "dhan", "robinhood"])
async def test_zerodha_and_other_adapterless_brokers_remain_blocked(api_client, db_session, broker):
    sid, _ = await strategy_with_master(api_client, db_session)
    child = await _account(db_session, "no_adapter")
    await set_broker(child, broker)
    before = await snapshot()
    resp = await add(api_client, sid, child)
    assert resp.status_code == 409 and await snapshot() == before                  # refused by the role gate first, and still refused


@pytest.mark.asyncio
async def test_an_unrecognised_master_broker_fails_closed_for_a_new_member(api_client, db_session):
    sid, master = await strategy_with_master(api_client, db_session)
    await set_broker(master, "not-a-broker")
    child = await _account(db_session, "alice_child")
    resp = await add(api_client, sid, child)
    assert resp.status_code == 409 and resp.json()["detail"].startswith(CODE) and "Master" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_with_no_master_the_pair_is_unknown_and_the_switch_decides(api_client, db_session, role_gates_open):
    created = await api_client.post("/api/v1/strategies", json={"name": "headless"}, headers=AUTH)
    sid = created.json()["id"]
    groww = await _account(db_session, "groww_first")
    await set_broker(groww, "groww")
    resp = await add(api_client, sid, groww)
    assert resp.status_code == 201 and resp.json()["broker_pair"]["supported"] is None          # unknown, never "supported"
    new_master = await _account(db_session, "alice_master", AccountType.MASTER)
    api_client.brokers[str(new_master.id)] = _Broker()
    api_client.brokers[str(groww.id)] = _Broker()
    switch = await api_client.post(f"/api/v1/strategies/{sid}/switch-master", json={"new_master_account_id": str(new_master.id)}, headers=AUTH)
    assert switch.status_code == 409 and CODE in switch.json()["detail"]                        # the switch refuses the unsupported trading Child
    async with TestingSessionLocal() as s:
        assert (await s.get(GroupStrategy, __import__("uuid").UUID(sid))).master_account_id is None


# ── membership: update (activation) ──────────────────────────────────────────────────────

async def strategy_with_unsupported_member(db_session, *, enabled):
    master = await _account(db_session, "master", AccountType.MASTER, copy_setting=False)
    alice = await _account(db_session, "alice_child")
    groww = await _account(db_session, "groww_child")
    await set_broker(groww, "groww")
    strategy = await link_strategy(db_session, master, [alice, groww], name="mixed", member_trading_enabled=enabled)
    return master, alice, groww, strategy


@pytest.mark.asyncio
async def test_existing_unsupported_members_are_preserved_reported_and_cannot_be_activated(api_client, db_session):
    master, alice, groww, strategy = await strategy_with_unsupported_member(db_session, enabled=False)
    sid = str(strategy.id)
    before = await members_of(strategy.id)
    listed = (await api_client.get(f"/api/v1/strategies/{sid}/members", headers=AUTH)).json()
    assert {m["account"]["display_name"]: m["broker_pair"]["supported"] for m in listed} == {"alice_child": True, "groww_child": False}
    assert await members_of(strategy.id) == before                                            # reading changed nothing

    refused = await api_client.put(f"/api/v1/strategies/{sid}/members/{groww.id}", json={"trading_enabled": True}, headers=AUTH)
    assert refused.status_code == 409 and refused.json()["detail"].startswith(CODE)
    assert await members_of(strategy.id) == before                                            # the refusal changed nothing

    allowed = await api_client.put(f"/api/v1/strategies/{sid}/members/{groww.id}", json={"multiplier": "2.00"}, headers=AUTH)
    assert allowed.status_code == 200 and allowed.json()["broker_pair"]["supported"] is False  # preserved and still reported
    assert (await members_of(strategy.id))[str(groww.id)] == (False, "2.00")

    ok = await api_client.put(f"/api/v1/strategies/{sid}/members/{alice.id}", json={"trading_enabled": False}, headers=AUTH)
    assert ok.status_code == 200 and (await members_of(strategy.id))[str(alice.id)][0] is False
    back = await api_client.put(f"/api/v1/strategies/{sid}/members/{alice.id}", json={"trading_enabled": True}, headers=AUTH)
    assert back.status_code == 200 and back.json()["broker_pair"]["supported"] is True


@pytest.mark.asyncio
async def test_switching_off_an_already_off_unsupported_member_is_never_refused(api_client, db_session):
    master, alice, groww, strategy = await strategy_with_unsupported_member(db_session, enabled=False)
    resp = await api_client.put(f"/api/v1/strategies/{strategy.id}/members/{groww.id}", json={"trading_enabled": False}, headers=AUTH)
    assert resp.status_code == 200 and (await members_of(strategy.id))[str(groww.id)][0] is False


@pytest.mark.asyncio
async def test_an_already_trading_unsupported_member_can_be_switched_off_and_edited_never_trapped(api_client, db_session):
    master, alice, groww, strategy = await strategy_with_unsupported_member(db_session, enabled=True)
    sid = str(strategy.id)
    same = await api_client.put(f"/api/v1/strategies/{sid}/members/{groww.id}", json={"trading_enabled": True}, headers=AUTH)
    assert same.status_code == 200                                                            # no transition, no activation
    off = await api_client.put(f"/api/v1/strategies/{sid}/members/{groww.id}", json={"trading_enabled": False}, headers=AUTH)
    assert off.status_code == 200 and (await members_of(strategy.id))[str(groww.id)][0] is False
    on = await api_client.put(f"/api/v1/strategies/{sid}/members/{groww.id}", json={"trading_enabled": True}, headers=AUTH)
    assert on.status_code == 409                                                              # off -> on is the activation


@pytest.mark.asyncio
async def test_readiness_and_member_count_stay_accurate_after_a_refusal(api_client, db_session, role_gates_open):
    sid, master = await strategy_with_master(api_client, db_session)
    ok = await _account(db_session, "alice_child")
    assert (await add(api_client, sid, ok)).status_code == 201
    bad = await _account(db_session, "angel_child")
    await set_broker(bad, "angelone")
    assert (await add(api_client, sid, bad)).status_code == 409
    body = (await api_client.get(f"/api/v1/strategies/{sid}", headers=AUTH)).json()
    assert body["member_count"] == 1 and body["readiness"]["broker_pairs_ready"] is True
    assert body["readiness"]["summary"]["children_pair_unsupported"] == 0


# ── Master switch ────────────────────────────────────────────────────────────────────────

def provide(api_client, *accounts):
    for a in accounts:
        api_client.brokers[str(a.id)] = _Broker()


async def switch(api_client, sid, new_master, **extra):
    return await api_client.post(f"/api/v1/strategies/{sid}/switch-master",
                                 json={"new_master_account_id": str(new_master.id), **extra}, headers=AUTH)


async def preflight(api_client, sid, new_master, action="REMOVE"):
    return await api_client.get(f"/api/v1/strategies/{sid}/switch-master/preflight",
                                params={"new_master_account_id": str(new_master.id), "old_master_action": action}, headers=AUTH)


@pytest.fixture
def groww_can_be_master(monkeypatch):
    """Pretend the Groww Master ROLE were open, so the switch reaches the pair check (the pair stays unsupported regardless)."""
    monkeypatch.setattr(strategy_switch, "can_be_master", lambda broker: True)


@pytest.mark.asyncio
async def test_a_switch_that_would_newly_leave_a_trading_child_unsupported_is_refused(api_client, db_session, groww_can_be_master):
    master = await _account(db_session, "alice_master", AccountType.MASTER, copy_setting=False)
    child = await _account(db_session, "alice_child")
    strategy = await link_strategy(db_session, master, [child], name="s")
    new_master = await _account(db_session, "other_master", AccountType.MASTER)
    await set_broker(new_master, "groww")
    provide(api_client, master, child, new_master)
    pre = (await preflight(api_client, str(strategy.id), new_master)).json()
    assert pre["allowed"] is False and pre["can_override"] is False
    (blocker,) = [b for b in pre["blockers"] if b["code"] == CODE]
    assert blocker["account_name"] == "alice_child" and blocker["role"] == "CHILD" and "PAIR_NOT_ALLOWLISTED" in blocker["items"][0]
    resp = await switch(api_client, str(strategy.id), new_master)
    assert resp.status_code == 409 and CODE in resp.json()["detail"]
    async with TestingSessionLocal() as s:
        assert (await s.get(GroupStrategy, strategy.id)).master_account_id == master.id          # nothing moved
    assert await events("STRATEGY_MASTER_SWITCHED") == []


@pytest.mark.asyncio
async def test_a_preexisting_unsupported_pair_does_not_trap_the_strategy_and_is_audited(api_client, db_session):
    master, alice, groww, strategy = await strategy_with_unsupported_member(db_session, enabled=True)
    new_master = await _account(db_session, "second_alice", AccountType.MASTER)
    provide(api_client, master, alice, groww, new_master)
    pre = (await preflight(api_client, str(strategy.id), new_master)).json()
    assert pre["allowed"] is True and not [b for b in pre["blockers"] if b["code"] == CODE]
    (warn,) = [w for w in pre["warnings"] if w["code"] == CODE]
    assert warn["account_name"] == "groww_child" and "already unsupported" in warn["message"]
    resp = await switch(api_client, str(strategy.id), new_master)
    assert resp.status_code == 200
    assert str(groww.id) in await members_of(strategy.id)                                     # preserved, not deleted
    (event,) = await events("STRATEGY_MASTER_SWITCHED")
    kept = event.metadata_payload["unsupported_pairs_preserved"]
    assert [(k["child"], k["preexisting"]) for k in kept] == [("groww_child", True)]


@pytest.mark.asyncio
async def test_a_child_that_is_not_trading_and_a_kept_old_master_are_warnings_only(api_client, db_session, groww_can_be_master):
    master = await _account(db_session, "alice_master", AccountType.MASTER, copy_setting=False)
    strategy = await link_strategy(db_session, master, [], name="solo")
    new_master = await _account(db_session, "other_master", AccountType.MASTER)
    await set_broker(new_master, "groww")
    provide(api_client, master, new_master)
    pre = (await preflight(api_client, str(strategy.id), new_master, action="CHILD")).json()
    assert pre["allowed"] is True and not [b for b in pre["blockers"] if b["code"] == CODE]
    (warn,) = [w for w in pre["warnings"] if w["code"] == CODE]
    assert warn["role"] == "CURRENT_MASTER" and "not trading" in warn["message"]


@pytest.mark.asyncio
async def test_a_non_trading_child_never_blocks_a_switch_even_when_newly_unsupported(api_client, db_session, groww_can_be_master):
    master = await _account(db_session, "alice_master", AccountType.MASTER, copy_setting=False)
    child = await _account(db_session, "idle_child")
    strategy = await link_strategy(db_session, master, [child], name="idle", member_trading_enabled=False)
    new_master = await _account(db_session, "other_master", AccountType.MASTER)
    await set_broker(new_master, "groww")
    provide(api_client, master, child, new_master)
    pre = (await preflight(api_client, str(strategy.id), new_master)).json()
    assert pre["allowed"] is True and not [b for b in pre["blockers"] if b["code"] == CODE]
    (warn,) = [w for w in pre["warnings"] if w["code"] == CODE]
    assert warn["account_name"] == "idle_child" and "not trading" in warn["message"] and warn["role"] == "CHILD"


@pytest.mark.asyncio
async def test_alice_to_alice_switching_is_unchanged(api_client, db_session):
    master = await _account(db_session, "alice_master", AccountType.MASTER, copy_setting=False)
    child = await _account(db_session, "alice_child")
    strategy = await link_strategy(db_session, master, [child], name="aa")
    new_master = await _account(db_session, "alice_two", AccountType.MASTER)
    provide(api_client, master, child, new_master)
    pre = (await preflight(api_client, str(strategy.id), new_master)).json()
    assert pre["allowed"] is True and not [w for w in pre["warnings"] if w["code"] == CODE]
    assert (await switch(api_client, str(strategy.id), new_master)).status_code == 200
    (event,) = await events("STRATEGY_MASTER_SWITCHED")
    assert event.metadata_payload["unsupported_pairs_preserved"] == []


# ── GET /brokers/pairs ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_pair_matrix_has_exactly_one_supported_pair_and_explains_the_rest(api_client, db_session):
    resp = await api_client.get("/api/v1/brokers/pairs", headers=AUTH)
    assert resp.status_code == 200 and resp.json()["success"] is True
    data = resp.json()["data"]
    assert data["supported"] == [{"master": ALICE, "child": ALICE}]
    by = {(p["master"], p["child"]): p for p in data["pairs"]}
    for child in ("groww", "angelone", "zerodha"):
        p = by[(ALICE, child)]
        assert p["supported"] is False and p["code"] == CODE and p["requirement"] == "PAIR_NOT_ALLOWLISTED" and p["message"]
    for master in ("groww", "angelone", "zerodha"):
        assert by[(master, ALICE)]["supported"] is False
    assert by[(ALICE, ALICE)]["supported"] is True and by[(ALICE, ALICE)]["code"] is None
    assert {"alicebluenew", "groww", "angelone", "zerodha"} <= {b["id"] for b in data["brokers"]}
    assert [a["master"] + ">" + a["child"] for a in data["allowlist"]] == [f"{ALICE}>{ALICE}"]
    assert len(data["pairs"]) == len(data["brokers"]) ** 2


@pytest.mark.asyncio
async def test_opening_every_role_gate_does_not_enable_a_pair_in_the_matrix(api_client, db_session, role_gates_open):
    data = (await api_client.get("/api/v1/brokers/pairs", headers=AUTH)).json()["data"]
    assert data["supported"] == [{"master": ALICE, "child": ALICE}]
    child_flags = {b["id"]: b["as_child"] for b in data["brokers"]}
    assert child_flags["groww"] is True                                                       # the role gate really is open ...
    assert not any(p["supported"] for p in data["pairs"] if p["child"] == "groww")            # ... and the pair is still refused


@pytest.mark.asyncio
async def test_the_matrix_follows_the_r5_gate_not_a_list_of_its_own(api_client, db_session, monkeypatch):
    from dataclasses import replace
    from app.services.brokers.capabilities import Feature, Verification
    from app.services.brokers.profiles import build_profile as real
    monkeypatch.setattr(pairs, "build_profile", lambda b: replace(real(b), features={**real(b).features, Feature.FUNDS: Verification.VERIFIED_DOCS}))
    data = (await api_client.get("/api/v1/brokers/pairs", headers=AUTH)).json()["data"]
    assert data["supported"] == []                                                            # lowered evidence ends the only pair


@pytest.mark.asyncio
async def test_the_matrix_is_read_only_authenticated_and_free_of_secrets(api_client, db_session, mock_redis):
    async def counts():
        out = []
        async with TestingSessionLocal() as s:
            for m in (Account, StrategyMember, StrategyAccountRole, SystemEvent):
                out.append((await s.execute(select(func.count()).select_from(m))).scalar_one())
        return out
    before = await counts()
    text = (await api_client.get("/api/v1/brokers/pairs", headers=AUTH)).text.lower()
    assert await counts() == before
    assert not any(w in text for w in ("secret", "password", "bearer", "api_key", "session_token"))
    assert (await api_client.get("/api/v1/brokers/pairs")).status_code in (401, 503)          # the internal token is required
    assert (await api_client.post("/api/v1/brokers/pairs", headers=AUTH)).status_code == 405


# ── isolation and defaults ───────────────────────────────────────────────────────────────

def test_the_new_router_and_rules_reach_no_broker_client_or_the_trading_path():
    tree = ast.parse((APP / "api" / "brokers.py").read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 0}
    assert not [m for m in imported if any(w in m for w in ("alice_blue", "groww.", "angel_one", "copy_engine", "runtime", "redis"))]
    for rel in ("services/copy_engine/pipeline.py", "services/brokers/pairs.py"):
        text = (APP / rel).read_text(encoding="utf-8")
        assert "assert_pair_supported" not in text and "switch_pair_findings" not in text and "api.brokers" not in text, rel
    assert set(pairs.SUPPORTED_PAIRS) == {(ALICE, ALICE)}                                      # no pair was added by this change


def test_no_default_was_changed():
    s = Settings(_env_file=None)
    assert s.ENGINE_LIVE_TRADING_ENABLED is False and s.SELL_POSITION_SAFETY_BYPASS is False
    assert s.CANONICAL_MAPPING_ENFORCED is False and s.INSTRUMENT_SHADOW_ENABLED is False


# ── GET /brokers/matrix (the 4 x 4 view) ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_four_by_four_matrix_supports_only_alice_to_alice(api_client, db_session):
    data = (await api_client.get("/api/v1/brokers/matrix", headers=AUTH)).json()["data"]
    assert [b["id"] for b in data["brokers"]] == ["alicebluenew", "groww", "angelone", "zerodha"]
    assert len(data["matrix"]) == 16
    assert [(c["master"], c["child"]) for c in data["supported"]] == [(ALICE, ALICE)]
    by = {b["id"]: b for b in data["brokers"]}
    assert by[ALICE]["master_detection"]["external_orders_verified"] is True
    for b in ("groww", "angelone", "zerodha"):
        d = by[b]["master_detection"]
        assert d["external_orders_verified"] is False and d["blocker"]
        assert by[b]["master"]["gate_open"] is False and by[b]["child"]["gate_open"] is False       # default flags
    assert (await api_client.get("/api/v1/brokers/matrix")).status_code in (401, 503)


@pytest.mark.asyncio
async def test_opening_every_zerodha_gate_still_leaves_the_matrix_unchanged(api_client, db_session, monkeypatch):
    from app.services.brokers import registry
    from app.services.zerodha.contract import ZerodhaContract
    for name in ("ZERODHA_ENABLED", "ZERODHA_CHILD_TRADING_ENABLED", "ZERODHA_MASTER_ENABLED"):
        monkeypatch.setattr(settings, name, True)
    monkeypatch.setattr(registry, "ZERODHA_CONTRACT", ZerodhaContract(master_verified=True, live_verified=True))
    data = (await api_client.get("/api/v1/brokers/matrix", headers=AUTH)).json()["data"]
    z = {b["id"]: b for b in data["brokers"]}["zerodha"]
    assert z["child"]["gate_open"] is True and z["master"]["gate_open"] is True                 # the role gates really are open ...
    assert [(c["master"], c["child"]) for c in data["supported"]] == [(ALICE, ALICE)]            # ... and no Zerodha pair is supported
    cell = {(c["master"], c["child"]): c for c in data["matrix"]}[("zerodha", "zerodha")]
    assert cell["supported"] is False and cell["requirement"] == "PAIR_NOT_ALLOWLISTED"
