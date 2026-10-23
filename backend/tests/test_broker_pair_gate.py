"""
R5: the explicit Master -> Child broker-pair support gate. Fixtures and mocks only - no broker, Redis, network or production
access. A pair is supported only when it is on the allowlist AND the live gates and evidence still back it; a catalog or a
declared capability can never make it so; an unsupported pair is skipped for that Child alone, before any broker call.
"""
import ast
import dataclasses
import socket
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import Settings, settings
from app.models.accounts import Account
from app.models.audit import AuditLog
from app.models.trades import CopyTrade, CopyTradeStatus, Order
from app.services.brokers import pairs
from app.services.brokers.capabilities import Feature, Verification
from app.services.brokers.pairs import BROKER_PAIR_NOT_SUPPORTED, check_pair
from app.services.brokers.profiles import build_profile
from app.services.copy_engine import pipeline as pipeline_module
from app.services.copy_engine.child_outcomes import RecoveryScope
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from conftest import TestingSessionLocal
from strategy_helpers import link_strategy
from test_order_handling_matrix import _event
from test_strategy_master_switching import _account, _child_client

APP = Path(__file__).resolve().parent.parent / "app"
ALICE = "alicebluenew"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    local = {"127.0.0.1", "::1", "localhost", None, ""}
    real_connect, real_getaddrinfo = socket.socket.connect, socket.getaddrinfo

    def connect(self, address, *a, **k):
        if (address[0] if isinstance(address, tuple) else address) not in local:
            raise AssertionError("network access attempted")
        return real_connect(self, address, *a, **k)

    def getaddrinfo(host, *a, **k):
        if host not in local:
            raise AssertionError("network access attempted")
        return real_getaddrinfo(host, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture
def groww_and_angel_open(monkeypatch):
    """Every flag that opens the Groww and Angel One Child ROLE gates. The pair gate must still refuse them."""
    for name in ("GROWW_ENABLED", "GROWW_CHILD_TRADING_ENABLED", "ANGEL_ONE_ENABLED", "ANGEL_ONE_CHILD_TRADING_ENABLED"):
        monkeypatch.setattr(settings, name, True)
    monkeypatch.setattr(settings, "SELL_POSITION_SAFETY_BYPASS", False)


# ── the policy ───────────────────────────────────────────────────────────────────────────

def test_only_alice_to_alice_is_allowlisted():
    assert set(pairs.SUPPORTED_PAIRS) == {(ALICE, ALICE)}
    assert "VERIFIED_LIVE" in pairs.SUPPORTED_PAIRS[(ALICE, ALICE)]


def test_alice_to_alice_is_supported_including_legacy_values():
    for master, child in ((ALICE, ALICE), ("aliceblue", ALICE), (ALICE, None), (ALICE, ""), ("Alice Blue", "alice_blue")):
        d = check_pair(master, child)
        assert d.supported is True and d.code is None, (master, child, d)
    assert check_pair(ALICE, ALICE).detail == f"{ALICE}->{ALICE}"


@pytest.mark.parametrize("master,child", [
    (None, ALICE), (ALICE, "zerodha"), ("zerodha", ALICE), (ALICE, "totally-unknown"), ("totally-unknown", ALICE),
    ("groww", ALICE), ("angelone", ALICE), ("groww", "groww"), ("angelone", "angelone"), ("groww", "angelone"),
    (ALICE, "groww"), (ALICE, "angelone"), ("dhan", "fyers"), ("", "zerodha"),
])
def test_unknown_unlisted_and_unsupported_pairs_fail_closed(master, child):
    d = check_pair(master, child)
    assert d.supported is False and d.code == BROKER_PAIR_NOT_SUPPORTED and 0 < len(d.detail) <= 200


def test_flags_and_contracts_that_open_the_roles_do_not_make_a_pair(groww_and_angel_open):
    from app.services.brokers.registry import can_be_child
    assert can_be_child("groww")                                              # the Groww ROLE gate really is open ...
    for child in ("groww", "angelone"):
        d = check_pair(ALICE, child)                                          # ... and the pair is still refused
        assert d.supported is False and d.detail == f"PAIR_NOT_ALLOWLISTED:{ALICE}->{child}"


def test_a_loaded_catalog_cannot_make_a_pair_supported(groww_and_angel_open):
    from app.services.instruments.catalogs import GrowwCatalog
    from app.services.instruments.model import CanonicalInstrument
    from test_instrument_catalogs import groww_row, groww_store
    view = GrowwCatalog(groww_store(groww_row())).view_for(CanonicalInstrument("NSE", "RELIANCE", None))
    assert view.candidates                                                    # the Groww catalog really has the instrument
    assert check_pair(ALICE, "groww").supported is False                      # catalog availability is not execution support


def test_a_declared_capability_profile_alone_cannot_make_a_pair(monkeypatch, groww_and_angel_open):
    """Even a profile that claims every Groww feature VERIFIED_LIVE changes nothing without an allowlist entry."""
    live = {f: Verification.VERIFIED_LIVE for f in Feature}
    real = build_profile
    monkeypatch.setattr(pairs, "build_profile", lambda b: dataclasses.replace(real(b), features=live))
    assert check_pair(ALICE, "groww").supported is False
    assert check_pair(ALICE, ALICE).supported is True


def test_a_listed_pair_stops_being_supported_when_its_evidence_drops(monkeypatch):
    real = build_profile

    def lowered(feature):
        def build(broker):
            p = real(broker)
            return dataclasses.replace(p, features={**p.features, feature: Verification.VERIFIED_DOCS})
        return build

    for feature in (*pairs.REQUIRED_MASTER_FEATURES, *pairs.REQUIRED_CHILD_FEATURES):
        monkeypatch.setattr(pairs, "build_profile", lowered(feature))
        d = check_pair(ALICE, ALICE)
        assert d.supported is False and d.detail.startswith("EVIDENCE_BELOW_LIVE:") and feature.value in d.detail, feature


def test_the_required_evidence_is_pinned():
    """Shrinking these tuples would quietly weaken every pair, so they are pinned here."""
    assert pairs.REQUIRED_MASTER_FEATURES == (Feature.MASTER_DETECTION, Feature.ORDER_STATUS)
    assert pairs.REQUIRED_CHILD_FEATURES == (Feature.CHILD_PLACEMENT, Feature.MODIFY, Feature.CANCEL, Feature.ORDER_STATUS,
                                             Feature.POSITIONS, Feature.FUNDS)


def test_a_missing_feature_or_profile_is_unsupported(monkeypatch):
    real = build_profile
    monkeypatch.setattr(pairs, "build_profile", lambda b: dataclasses.replace(real(b), features={}))
    assert check_pair(ALICE, ALICE).supported is False
    monkeypatch.setattr(pairs, "build_profile", lambda b: None)
    d = check_pair(ALICE, ALICE)
    assert d.supported is False and "NO_PROFILE" in d.detail


def test_a_closed_role_gate_ends_support(monkeypatch):
    monkeypatch.setattr(pairs, "can_be_child", lambda b: False)
    assert check_pair(ALICE, ALICE).detail == f"CHILD_ROLE_NOT_OPEN:{ALICE}"
    monkeypatch.setattr(pairs, "can_be_child", lambda b: True)
    monkeypatch.setattr(pairs, "can_be_master", lambda b: False)
    assert check_pair(ALICE, ALICE).detail == f"MASTER_ROLE_NOT_OPEN:{ALICE}"


def test_an_error_while_checking_is_never_support(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("registry exploded")
    monkeypatch.setattr(pairs, "normalize_broker_id", boom)
    d = check_pair(ALICE, ALICE)
    assert d.supported is False and d.code == BROKER_PAIR_NOT_SUPPORTED and d.detail == "PAIR_CHECK_ERROR:RuntimeError"


def test_the_detail_is_short_and_carries_broker_ids_only():
    d = check_pair("x" * 500, ALICE)
    assert len(d.detail) <= 200 and "x" * 100 not in d.detail


def test_the_gate_module_is_isolated():
    tree = ast.parse((APP / "services" / "brokers" / "pairs.py").read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 0}
    assert imported <= {"dataclasses", "typing", "app.services.brokers.capabilities", "app.services.brokers.profiles",
                        "app.services.brokers.registry"}
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not called & {"place_order", "modify_order", "cancel_order", "get_funds", "request", "urlopen"}


def test_no_defaults_turn_anything_on():
    s = Settings(_env_file=None)
    assert s.CANONICAL_MAPPING_ENFORCED is False and s.INSTRUMENT_SHADOW_ENABLED is False
    assert s.ENGINE_LIVE_TRADING_ENABLED is False
    assert (s.GROWW_ENABLED, s.GROWW_CHILD_TRADING_ENABLED, s.GROWW_MASTER_ENABLED) == (False, False, False)
    assert (s.ANGEL_ONE_ENABLED, s.ANGEL_ONE_CHILD_TRADING_ENABLED, s.ANGEL_ONE_MASTER_ENABLED) == (False, False, False)
    assert s.INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH == ""


# ── the pipeline ─────────────────────────────────────────────────────────────────────────

ORDERS = [("LIMIT", "0"), ("MARKET", "0"), ("SL", "790")]


async def two_children(db_session):
    """Alice Master with an Alice Child (supported) and a Groww Child whose role gate is open (flags on)."""
    master = await _account(db_session, "master")
    alice_child = await _account(db_session, "achild")
    groww_child = await _account(db_session, "gchild")
    async with TestingSessionLocal() as s:
        (await s.get(Account, groww_child.id)).broker = "groww"
        await s.commit()
    await link_strategy(db_session, master, [alice_child, groww_child], name="pair-gate")
    return master, alice_child, groww_child


def order_event(master, oid, order_type, trigger):
    return _event(master, oid, side="BUY", order_type=order_type, product="INTRADAY", exchange="NSE", symbol="SBIN",
                  qty=10, price="800", trigger=trigger, token="12345")


async def rows_for(account):
    async with TestingSessionLocal() as s:
        copies = (await s.execute(select(CopyTrade).where(CopyTrade.follower_account_id == account.id))).scalars().all()
        orders = (await s.execute(select(Order).where(Order.account_id == account.id))).scalars().all()
        audits = (await s.execute(select(AuditLog).where(AuditLog.account_id == account.id))).scalars().all()
    return copies, orders, audits


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type,trigger", ORDERS)
async def test_an_unsupported_pair_is_skipped_before_any_broker_call_and_siblings_continue(
        db_session, mock_redis, groww_and_angel_open, order_type, trigger):
    master, alice_child, groww_child = await two_children(db_session)
    clients = {str(alice_child.id): _child_client("BRK_A"), str(groww_child.id): _child_client("BRK_G")}
    pipe = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipe.execute_replication(event=order_event(master, f"PG-{order_type}", order_type, trigger), clients=clients)
    results = res["results"]

    skipped = results[str(groww_child.id)]
    assert skipped == {"status": "SKIPPED", "reason": BROKER_PAIR_NOT_SUPPORTED, "detail": f"PAIR_NOT_ALLOWLISTED:{ALICE}->groww"}
    assert clients[str(groww_child.id)].mock_calls == []                      # no funds, positions, place, modify or cancel
    copies, orders, _ = await rows_for(groww_child)
    assert [(c.status, (c.failure_reason or "").split(":")[0]) for c in copies] == [(CopyTradeStatus.SKIPPED, BROKER_PAIR_NOT_SUPPORTED)]
    assert orders == []                                                       # no claim, no pre-write, nothing sent
    assert results[str(alice_child.id)]["status"] == "SUCCESS"                # the supported sibling is unaffected
    assert clients[str(alice_child.id)].place_order.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type,trigger", ORDERS)
async def test_the_skip_is_audited_with_non_sensitive_detail(db_session, mock_redis, groww_and_angel_open, order_type, trigger):
    """One Child only: the advisory audit rows share a test connection, so a sibling's concurrent transaction could roll one back
    here (not in PostgreSQL). The per-Child outcome row itself is asserted above with siblings present."""
    master = await _account(db_session, "master")
    groww_child = await _account(db_session, "gchild")
    async with TestingSessionLocal() as s:
        (await s.get(Account, groww_child.id)).broker = "groww"
        await s.commit()
    await link_strategy(db_session, master, [groww_child], name="pair-audit")
    client = _child_client("BRK_G")
    pipe = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await pipe.execute_replication(event=order_event(master, f"PGA-{order_type}", order_type, trigger),
                                   clients={str(groww_child.id): client})
    _, _, audits = await rows_for(groww_child)
    skip_audit = [a for a in audits if a.event_type == "COPY_SKIPPED"]
    assert len(skip_audit) == 1 and skip_audit[0].reason_code == BROKER_PAIR_NOT_SUPPORTED
    meta = skip_audit[0].metadata_payload
    assert meta["child_broker"] == "groww" and meta["master_broker"] == ALICE and meta["detail"].startswith("PAIR_NOT_ALLOWLISTED")
    assert not any(k in str(meta).lower() for k in ("token", "secret", "password"))
    assert client.mock_calls == []


@pytest.mark.asyncio
async def test_a_runtime_paused_child_still_reports_trading_disabled_first(db_session, mock_redis, groww_and_angel_open):
    """Guard order: TRADING_DISABLED precedes the pair gate, which writes nothing for that Child."""
    master, alice_child, groww_child = await two_children(db_session)

    async def stop_provider():
        return False, {str(groww_child.id)}

    clients = {str(alice_child.id): _child_client("BRK_A"), str(groww_child.id): _child_client("BRK_G")}
    pipe = CopyEnginePipeline(validator=EventValidator(redis=mock_redis), stop_provider=stop_provider)
    res = await pipe.execute_replication(event=order_event(master, "PG-PAUSED", "LIMIT", "0"), clients=clients)
    assert res["results"][str(groww_child.id)] == {"status": "SKIPPED", "reason": "TRADING_DISABLED"}
    _, _, audits = await rows_for(groww_child)
    assert not [a for a in audits if a.reason_code == BROKER_PAIR_NOT_SUPPORTED]


@pytest.mark.asyncio
async def test_the_gate_runs_before_the_planner_even_when_enforcement_is_on(db_session, mock_redis, groww_and_angel_open, monkeypatch):
    monkeypatch.setattr(settings, "CANONICAL_MAPPING_ENFORCED", True)
    seen = []
    real = pipeline_module.check_child_order
    monkeypatch.setattr(pipeline_module, "check_child_order", lambda **kw: seen.append(kw["child_broker"]) or real(**kw))
    master, alice_child, groww_child = await two_children(db_session)
    clients = {str(alice_child.id): _child_client("BRK_A"), str(groww_child.id): _child_client("BRK_G")}
    pipe = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipe.execute_replication(event=order_event(master, "PG-PLAN", "LIMIT", "0"), clients=clients)
    assert res["results"][str(groww_child.id)]["reason"] == BROKER_PAIR_NOT_SUPPORTED
    assert "groww" not in seen                                               # the planner never saw the refused Child
    assert res["results"][str(alice_child.id)]["status"] == "SUCCESS"        # Alice -> Alice still maps and copies


@pytest.mark.asyncio
async def test_an_unknown_master_broker_fails_closed_for_every_child(db_session, mock_redis, monkeypatch):
    master, alice_child, groww_child = await two_children(db_session)

    async def unknown(self, account_id):
        return None

    monkeypatch.setattr(CopyEnginePipeline, "_master_broker_id", unknown)
    client = _child_client("BRK_A")
    pipe = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipe.execute_replication(event=order_event(master, "PG-UNK", "LIMIT", "0"), clients={str(alice_child.id): client})
    assert res["results"][str(alice_child.id)] == {"status": "SKIPPED", "reason": BROKER_PAIR_NOT_SUPPORTED,
                                                   "detail": "MASTER_BROKER_UNKNOWN"}
    assert client.mock_calls == []


@pytest.mark.asyncio
async def test_a_recovery_fanout_is_gated_too(db_session, mock_redis, groww_and_angel_open):
    master, alice_child, groww_child = await two_children(db_session)
    client = _child_client("BRK_G")
    pipe = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipe.replicate_limit_order(
        order_event(master, "PG-REC", "LIMIT", "0"), {str(groww_child.id): client},
        recovery=RecoveryScope(only_children=frozenset({str(groww_child.id)})),
    )
    assert res["results"][str(groww_child.id)]["reason"] == BROKER_PAIR_NOT_SUPPORTED and client.mock_calls == []


@pytest.mark.asyncio
async def test_alice_to_alice_is_unchanged(db_session, mock_redis):
    master = await _account(db_session, "master")
    child = await _account(db_session, "child")
    await link_strategy(db_session, master, [child], name="alice-only")
    client = _child_client("BRK_A")
    pipe = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipe.execute_replication(event=order_event(master, "PG-AA", "LIMIT", "0"), clients={str(child.id): client})
    assert res["results"][str(child.id)]["status"] == "SUCCESS" and client.place_order.await_count == 1


@pytest.mark.asyncio
async def test_duplicate_events_are_still_refused_before_the_gate(db_session, mock_redis):
    master = await _account(db_session, "master")
    child = await _account(db_session, "child")
    await link_strategy(db_session, master, [child], name="alice-only")
    client = _child_client("BRK_A")
    pipe = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    event = order_event(master, "PG-DUP", "LIMIT", "0")
    await pipe.execute_replication(event=event, clients={str(child.id): client})
    again = await pipe.execute_replication(event=event, clients={str(child.id): client})
    assert again.get("status") == "rejected" and client.place_order.await_count == 1


# ── what the gate does NOT touch ─────────────────────────────────────────────────────────

def test_only_the_three_new_order_paths_call_the_gate():
    tree = ast.parse((APP / "services" / "copy_engine" / "pipeline.py").read_text(encoding="utf-8"))
    callers = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
            for n in ast.walk(fn):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "_pair_gate_skip":
                    callers.add(fn.name)
    assert callers == {"_process_single_limit_follower", "_process_single_sl_follower", "_process_single_market_follower"}


def test_modify_cancel_trigger_and_square_off_paths_are_not_gated():
    """They act on orders that already exist (or are an operator's risk-reducing exit); blocking them would strand a live order."""
    for rel in ("services/copy_engine/pipeline.py", "api/strategies.py"):
        tree = ast.parse((APP / rel).read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)) and fn.name in (
                    "handle_master_modification", "handle_master_cancellation", "handle_sl_trigger_execution",
                    "handle_pending_sl_modification", "_execute_triggered_sl_child", "square_off_child_position"):
                names = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
                assert not names & {"_pair_gate_skip", "check_pair"}, fn.name


def test_the_crosswalk_and_shadow_features_stay_out_of_the_pair_gate_and_the_trading_path():
    for rel in ("services/brokers/pairs.py", "services/copy_engine/pipeline.py"):
        text = (APP / rel).read_text(encoding="utf-8").lower()
        assert "crosswalk" not in text and "shadow" not in text, rel
