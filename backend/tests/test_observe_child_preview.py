"""
Live Trading OFF: the engine logs a read-only [ObservePreview] of what each candidate Child would face for a new Master entry - and
still copies, sends, writes and opens nothing.

Offline: the real EngineLoop with the same fakes as test_live_trading_toggle.py, a real Master / strategy / Alice Child and a
non-member Groww account in the in-memory DB, and mocked broker clients. No broker, network or production access.
"""
import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models.accounts import Account
from app.models.audit import AuditLog, SystemEvent
from app.models.trades import CopyTrade, Order
from app.services.alice_blue.types import OrderUpdateEvent
from app.services.copy_engine import child_preview as P
from app.services.copy_engine.events import MasterTradeEvent
from app.services.groww import instruments as groww_instruments
from app.services.runtime.engine_loop import EngineLoop
from conftest import TestingSessionLocal
from groww_helpers import make_store
from strategy_helpers import link_strategy
from test_engine_loop import _FakeMasterClient, _FakeWebSocketClient
from test_strategy_master_switching import _account


@pytest.fixture
def production_flags(monkeypatch):
    monkeypatch.setattr(settings, "GROWW_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_CHILD_TRADING_ENABLED", False)
    monkeypatch.setattr(settings, "SELL_POSITION_SAFETY_BYPASS", True)
    monkeypatch.setattr(settings, "OBSERVE_CHILD_PREVIEW_ENABLED", True)


async def _world(db_session):
    """Alice Master + one Alice Child in a strategy, and a Groww account that is NOT a member (production's layout)."""
    master = await _account(db_session, "zqownermaster")
    alice_child = await _account(db_session, "zqalicechild")
    groww = await _account(db_session, "zqgrowwacct")
    await link_strategy(db_session, master, [alice_child], name="observe")
    async with TestingSessionLocal() as s:
        (await s.get(Account, groww.id)).broker = "groww"
        await s.commit()
    return master, alice_child, groww


def _trade_event(master_id, event_type, *, symbol="SBIN", exchange="NSE", order_type="LIMIT"):
    return MasterTradeEvent(
        master_order_id="AB-ORDER-123456789", symbol=symbol, exchange=exchange, side="BUY", quantity=10, price=Decimal("800"),
        limit_price=Decimal("800"), product_type="INTRADAY", timestamp=datetime.now(timezone.utc), account_id=str(master_id),
        order_type=order_type, event_type=event_type, source="BROKER_EXTERNAL", status="OPEN",
    )


async def _drive(mock_redis, master, event_type, *, live_enabled, clients, preview_patch=None):
    """The real EngineLoop with one Master event; returns the pipeline mock."""
    mid = str(master.id)
    fake_account_manager = MagicMock()
    fake_account_manager.initialize = AsyncMock()
    fake_account_manager.authenticate_all = AsyncMock(return_value={})
    fake_account_manager.get_master_client = MagicMock(return_value=_FakeMasterClient(account_id=mid))
    fake_account_manager.get_follower_clients_map = MagicMock(return_value=clients)
    fake_account_manager.get_clients_map_excluding = MagicMock(return_value=clients)
    fake_account_manager.get_client = MagicMock(return_value=None)
    detector = MagicMock()
    detector.process_order_update = MagicMock(return_value=_trade_event(mid, event_type))
    pipeline = MagicMock()
    pipeline.save_or_update_order = AsyncMock()
    pipeline.execute_replication = AsyncMock(return_value={"status": "completed"})

    def fake(**kw):
        m = MagicMock()
        for name in ("start", "stop"):
            setattr(m, name, AsyncMock())
        return m

    recon = MagicMock()
    recon.run_crash_recovery = AsyncMock(return_value=0)
    patches = [
        patch("app.services.runtime.engine_loop.account_manager", fake_account_manager),
        patch("app.services.runtime.engine_loop.MasterTradeDetector", return_value=detector),
        patch("app.services.runtime.engine_loop.CopyEnginePipeline", return_value=pipeline),
        patch("app.services.runtime.engine_loop.AliceBlueWebSocketClient", _FakeWebSocketClient),
        patch("app.services.runtime.engine_loop.load_and_cache_contract_master", AsyncMock()),
        patch("app.services.runtime.engine_loop.ReconciliationEngine", recon),
        patch("app.services.runtime.engine_loop.StaleOrderSweeper", return_value=fake()),
        patch("app.services.runtime.engine_loop.FollowerFillSyncer", return_value=fake()),
        patch("app.services.runtime.engine_loop.MasterOrderPoller", return_value=fake()),
        patch("app.services.runtime.engine_loop.is_live_trading_enabled", AsyncMock(return_value=live_enabled)),
    ]
    if preview_patch is not None:
        patches.append(patch("app.services.runtime.engine_loop.observe_child_previews", preview_patch))
    for p in patches:
        p.start()
    try:
        loop = EngineLoop(redis_client=mock_redis)
        await loop._run()
        loop._ws_client.on_order_update(OrderUpdateEvent(
            broker_order_id="AB-ORDER-123456789", client_order_id=None, symbol="SBIN", side="BUY", quantity=10, filled_quantity=0,
            fill_price=Decimal("0"), status="OPEN", timestamp=datetime.now(timezone.utc), account_id=mid, product_type="MIS",
            order_type="LIMIT", price=Decimal("800"), trigger_price=Decimal("0"), rejection_reason=None, exchange="NSE",
        ))
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)
        await loop.stop()
    finally:
        for p in reversed(patches):
            p.stop()
    return pipeline


def _broker_clients(*accounts):
    clients = {}
    for a in accounts:
        c = AsyncMock()
        clients[str(a.id)] = c
    return clients


def _assert_no_broker_write(clients):
    for c in clients.values():
        for method in ("place_order", "modify_order", "cancel_order", "login", "get_session"):
            assert getattr(c, method).await_count == 0, method


def _previews(caplog):
    return [json.loads(r.getMessage()[len("[ObservePreview] "):]) for r in caplog.records
            if r.name == "engine_loop" and r.getMessage().startswith("[ObservePreview] {")]


async def _row_counts():
    async with TestingSessionLocal() as s:
        return tuple([(await s.execute(select(func.count()).select_from(m))).scalar_one() for m in (Order, CopyTrade, AuditLog, SystemEvent)])


# ── engine integration ────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["LIMIT_CREATED", "MARKET_ACCEPTED", "SL_PENDING_CREATED"])
async def test_live_off_logs_a_preview_per_candidate_and_sends_nothing(db_session, mock_redis, production_flags, caplog, event_type):
    caplog.set_level(logging.INFO, logger="engine_loop")
    master, alice_child, groww = await _world(db_session)
    clients = _broker_clients(alice_child, groww)
    before = await _row_counts()
    pipeline = await _drive(mock_redis, master, event_type, live_enabled=False, clients=clients)

    pipeline.execute_replication.assert_not_called()                         # still observe-only
    _assert_no_broker_write(clients)
    assert await _row_counts() == before                                      # no DB write at all
    by_broker = {r["child"]["broker"]: r for r in _previews(caplog)}
    assert set(by_broker) == {"alicebluenew", "groww"}
    assert all(r["would_send"] is False for r in by_broker.values())

    alice = by_broker["alicebluenew"]
    assert alice["child"]["membership"] == "MEMBER" and alice["blockers"] == []

    g = by_broker["groww"]
    codes = {b["code"] for b in g["blockers"]}
    assert g["child"]["membership"] == "NOT_A_STRATEGY_MEMBER"
    assert {"NOT_A_STRATEGY_MEMBER", "CHILD_BROKER_NOT_READY", "BROKER_PAIR_NOT_SUPPORTED"} <= codes


@pytest.mark.asyncio
async def test_with_groww_instruments_loaded_the_preview_shows_the_would_be_groww_order(
        db_session, mock_redis, production_flags, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="engine_loop")
    monkeypatch.setattr(groww_instruments, "store", make_store())
    master, alice_child, groww = await _world(db_session)
    await _drive(mock_redis, master, "LIMIT_CREATED", live_enabled=False, clients=_broker_clients(alice_child, groww))
    g = next(r for r in _previews(caplog) if r["child"]["broker"] == "groww")
    assert g["order_ok_if_enabled"] is True                                   # only the closed gates and membership stand in the way
    assert g["child_order"] == {"trading_symbol": "SBIN", "exchange": "NSE", "segment": "CASH", "transaction_type": "BUY",
                                "quantity": 10, "product": "MIS", "order_type": "LIMIT", "price": 800.0, "validity": "DAY"}
    # the Master's own product and prices, so a product-mapping or price blocker can be traced from the line alone
    assert g["master"]["product_type"] == "INTRADAY"
    assert (g["master"]["price"], g["master"]["limit_price"], g["master"]["trigger_price"]) == ("800", "800", "0")
    assert g["master"]["order"] == "...6789"                                  # still masked


@pytest.mark.asyncio
async def test_the_preview_log_carries_no_identifiers_or_secrets(db_session, mock_redis, production_flags, caplog):
    caplog.set_level(logging.INFO, logger="engine_loop")
    master, alice_child, groww = await _world(db_session)
    await _drive(mock_redis, master, "LIMIT_CREATED", live_enabled=False, clients=_broker_clients(alice_child, groww))
    text = "\n".join(r.getMessage() for r in caplog.records if "[ObservePreview]" in r.getMessage())
    assert text
    async with TestingSessionLocal() as s:
        accounts = (await s.execute(select(Account))).scalars().all()
    for a in accounts:
        for secret in (str(a.id), a.alice_user_id, a.display_name):
            assert secret not in text, secret
    assert "AB-ORDER-123456789" not in text and "...6789" in text             # the Master order id is masked
    assert "secret" not in text.lower() and "token" not in text.lower()


@pytest.mark.asyncio
async def test_the_kill_switch_silences_the_preview(db_session, mock_redis, production_flags, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="engine_loop")
    monkeypatch.setattr(settings, "OBSERVE_CHILD_PREVIEW_ENABLED", False)
    master, alice_child, groww = await _world(db_session)
    pipeline = await _drive(mock_redis, master, "LIMIT_CREATED", live_enabled=False, clients=_broker_clients(alice_child, groww))
    assert _previews(caplog) == [] and pipeline.execute_replication.await_count == 0


@pytest.mark.asyncio
async def test_a_non_entry_event_is_not_previewed(db_session, mock_redis, production_flags, caplog):
    caplog.set_level(logging.INFO, logger="engine_loop")
    master, alice_child, groww = await _world(db_session)
    pipeline = await _drive(mock_redis, master, "ORDER_MODIFIED", live_enabled=False, clients=_broker_clients(alice_child, groww))
    assert _previews(caplog) == [] and pipeline.execute_replication.await_count == 0


@pytest.mark.asyncio
async def test_a_failing_preview_never_changes_the_observe_only_outcome(db_session, mock_redis, production_flags, caplog):
    caplog.set_level(logging.INFO, logger="engine_loop")
    master, alice_child, groww = await _world(db_session)
    clients = _broker_clients(alice_child, groww)
    pipeline = await _drive(mock_redis, master, "LIMIT_CREATED", live_enabled=False, clients=clients,
                            preview_patch=AsyncMock(side_effect=RuntimeError("boom")))
    assert pipeline.execute_replication.await_count == 0
    _assert_no_broker_write(clients)
    assert any("[ObservePreview] skipped (RuntimeError)" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_preview_past_the_timeout_is_abandoned_and_observe_only_continues(
        db_session, mock_redis, production_flags, caplog, monkeypatch):
    """Deterministic: the preview sleeps far past a shortened bound; no database or network delay is involved."""
    caplog.set_level(logging.INFO, logger="engine_loop")
    monkeypatch.setattr(EngineLoop, "OBSERVE_PREVIEW_TIMEOUT_SECONDS", 0.05)
    master, alice_child, groww = await _world(db_session)
    clients = _broker_clients(alice_child, groww)
    state = {"started": False, "finished": False, "cancelled": False}

    async def slow(*args, **kwargs):
        state["started"] = True
        try:
            await asyncio.sleep(30)
            state["finished"] = True
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise
        return []

    pipeline = await _drive(mock_redis, master, "LIMIT_CREATED", live_enabled=False, clients=clients, preview_patch=slow)
    assert state == {"started": True, "finished": False, "cancelled": True}   # abandoned at the bound, not awaited to the end
    assert pipeline.execute_replication.await_count == 0
    _assert_no_broker_write(clients)
    skipped = [r.getMessage() for r in caplog.records if "[ObservePreview] skipped" in r.getMessage()]
    assert skipped == ["[ObservePreview] skipped (TimeoutError); observe-only behaviour unchanged"]   # no identifiers in it
    assert _previews(caplog) == []


@pytest.mark.asyncio
async def test_the_preview_runs_after_the_masters_order_gate_is_released(db_session, mock_redis, production_flags):
    """A Master switch must never wait on a preview: while the preview runs, the gate has no holder and the exclusive switch lock
    can be taken at once."""
    from app.services import strategy_membership as membership

    master, alice_child, groww = await _world(db_session)
    seen = {}

    async def probe(event, source_id):
        seen["readers"] = membership._gate_for(source_id)._readers
        async def take_switch_lock():
            async with membership.accounts_switch_lock([source_id]):
                return True
        seen["switch_lock"] = await asyncio.wait_for(take_switch_lock(), timeout=1.0)
        return []

    pipeline = await _drive(mock_redis, master, "LIMIT_CREATED", live_enabled=False, clients=_broker_clients(alice_child, groww),
                            preview_patch=probe)
    assert seen == {"readers": 0, "switch_lock": True}
    assert pipeline.execute_replication.await_count == 0


@pytest.mark.asyncio
async def test_a_body_that_raises_inside_the_gate_previews_nothing_and_still_releases_it(db_session, mock_redis, production_flags):
    """_order_gate_then_preview: an exception in the gated body propagates as before, no preview runs, and the gate is released."""
    from app.services import strategy_membership as membership

    loop = EngineLoop(redis_client=mock_redis)
    called = AsyncMock()
    loop._log_observe_preview = called
    pending = [object()]
    with pytest.raises(RuntimeError):
        async with loop._order_gate_then_preview("acct-x", pending):
            raise RuntimeError("inside the gate")
    assert called.await_count == 0 and membership._gate_for("acct-x")._readers == 0


@pytest.mark.asyncio
async def test_live_on_is_unchanged_and_not_previewed(db_session, mock_redis, production_flags, caplog):
    caplog.set_level(logging.INFO, logger="engine_loop")
    master, alice_child, groww = await _world(db_session)
    pipeline = await _drive(mock_redis, master, "LIMIT_CREATED", live_enabled=True, clients=_broker_clients(alice_child, groww))
    pipeline.execute_replication.assert_awaited_once()
    assert _previews(caplog) == []


@pytest.mark.asyncio
async def test_a_cancellation_while_off_still_propagates_and_is_not_previewed(db_session, mock_redis, production_flags, caplog):
    caplog.set_level(logging.INFO, logger="engine_loop")
    master, alice_child, groww = await _world(db_session)
    pipeline = await _drive(mock_redis, master, "ORDER_CANCELLED", live_enabled=False, clients=_broker_clients(alice_child, groww))
    pipeline.execute_replication.assert_awaited_once()
    assert _previews(caplog) == []


# ── the read-only builder ─────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_master_without_a_strategy_reports_no_strategy_for_every_candidate(db_session, production_flags):
    master = await _account(db_session, "lonemaster")
    other = await _account(db_session, "other")
    records = await P.observe_child_previews(_trade_event(master.id, "LIMIT_CREATED"), str(master.id))
    rec = next(r for r in records if r["child"]["ref"] == P.account_ref(other.id))
    assert rec["child"]["membership"] == "NO_STRATEGY" and rec["blockers"][0]["code"] == "NO_STRATEGY"


@pytest.mark.asyncio
async def test_a_member_with_trading_disabled_is_reported(db_session, production_flags):
    master = await _account(db_session, "m2")
    child = await _account(db_session, "c2")
    await link_strategy(db_session, master, [child], name="disabled-member")
    from app.models.strategies import StrategyMember
    async with TestingSessionLocal() as s:
        m = (await s.execute(select(StrategyMember).where(StrategyMember.account_id == child.id))).scalar_one()
        m.trading_enabled = False
        await s.commit()
    records = await P.observe_child_previews(_trade_event(master.id, "LIMIT_CREATED"), str(master.id))
    rec = next(r for r in records if r["child"]["ref"] == P.account_ref(child.id))
    assert rec["child"]["membership"] == "MEMBER_TRADING_DISABLED"
    assert "MEMBER_TRADING_DISABLED" in {b["code"] for b in rec["blockers"]}


@pytest.mark.asyncio
async def test_the_builder_returns_nothing_for_a_non_entry_and_never_writes(db_session, production_flags):
    master, alice_child, groww = await _world(db_session)
    before = await _row_counts()
    assert await P.observe_child_previews(_trade_event(master.id, "ORDER_FILLED"), str(master.id)) == []
    records = await P.observe_child_previews(_trade_event(master.id, "LIMIT_CREATED"), str(master.id))
    assert len(records) == 2 and await _row_counts() == before
