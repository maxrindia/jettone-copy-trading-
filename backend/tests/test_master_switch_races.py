"""Order-event races around a Master switch.

The gap these pin down: a new Master's monitor used to baseline EVERYTHING
its first order-book poll saw, so an order the new Master placed after the
switch's preflight check but before that first poll was silently never
copied. Now the preflight's own order-book snapshot (taken under the switch
lock, while the new Master has no working order) is the history boundary:

- snapshot orders are history: persisted, never copied;
- orders absent from the snapshot were placed after it: copied once, through
  the normal eligibility rules (Jettone-originated orders still excluded);
- a stale snapshot (first poll after the replay window) is not trusted:
  everything is baselined as before and the missed orders are reported;
- no snapshot (engine startup): unchanged first-poll baseline.

Plus the switch gate: events for the NEW Master that arrive while the switch
holds the gate wait and are then processed (never dropped); events for the
OLD Master are recorded, not copied, and reported.

Broker access is always a double; no real broker call is possible.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.models.accounts import AccountType
from app.models.audit import SystemEvent
from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services import strategy_membership as membership
from app.services import strategy_switch
from app.services.alice_blue.client import order_book_row_id
from app.services.alice_blue.types import OrderRecord
from app.services.copy_engine.detector import MasterTradeDetector
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.reliability.master_order_poller import MasterOrderPoller
from app.services.runtime.engine_loop import EngineLoop
from strategy_helpers import link_strategy
from test_strategy_master_switching import (
    _Broker,
    _FakePoller,
    _account,
    _drain,
    _engine_patches,
    _provider,
    _ws_event,
)


def rec(boid, status="OPEN", order_type="LIMIT", filled=0, qty=1):
    return OrderRecord(
        broker_order_id=boid, client_order_id=None, symbol="ITC", exchange="NSE", side=OrderSide.BUY,
        quantity=qty, filled_quantity=filled, average_price=Decimal("400") if filled else Decimal("0"),
        status=status, order_timestamp=datetime.now(timezone.utc), order_type=order_type, price=Decimal("400"),
    )


def _poller(account_id, book, dispatched, mock_redis, **activation):
    client = MagicMock(get_order_book=AsyncMock(side_effect=lambda: list(book)))
    return MasterOrderPoller(
        master_account_id=str(account_id), client_provider=lambda _a: client,
        on_order_update=dispatched.append, pipeline=CopyEnginePipeline(validator=EventValidator(redis=mock_redis)),
        **activation,
    )


# ─── first-poll boundary ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_without_snapshot_first_poll_still_baselines_everything(db_session, mock_redis):
    """Unchanged startup behavior - and the gap: NEW_1 is never copied."""
    acc = uuid.uuid4()
    dispatched = []
    poller = _poller(acc, [rec("HIST", "COMPLETE"), rec("NEW_1")], dispatched, mock_redis)
    await poller.poll_once()
    assert dispatched == [] and poller.is_seeded


@pytest.mark.asyncio
async def test_fresh_snapshot_copies_only_orders_placed_after_it(db_session, mock_redis):
    acc = uuid.uuid4()
    book = [rec("HIST_A", "COMPLETE", "MARKET", filled=1), rec("HIST_B", "CANCELLED"), rec("NEW_1")]
    dispatched = []
    poller = _poller(acc, book, dispatched, mock_redis,
                     activation_baseline={"HIST_A", "HIST_B"},
                     activation_deadline=datetime.now(timezone.utc) + timedelta(seconds=60))
    await poller.poll_once()
    assert [e.broker_order_id for e in dispatched] == ["NEW_1"]
    assert poller.is_seeded

    # History stays history even when it changes later; nothing is re-sent.
    book[1] = rec("HIST_B", "REJECTED")
    await poller.poll_once()
    assert [e.broker_order_id for e in dispatched] == ["NEW_1"]


@pytest.mark.asyncio
async def test_fresh_snapshot_never_replays_jettone_originated_orders(db_session, mock_redis):
    acc = uuid.uuid4()
    db_session.add(Order(
        id=uuid.uuid4(), internal_order_id="JT_MANUAL_1", broker_order_id="JT_1", account_id=acc,
        source=OrderSource.INTERNAL_UI, exchange="NSE", symbol="ITC", side=OrderSide.BUY, order_type="LIMIT",
        product_type="MIS", quantity=1, filled_quantity=0, pending_quantity=1, price=Decimal("400"),
        status=OrderStatus.OPEN,
    ))
    await db_session.commit()
    dispatched = []
    poller = _poller(acc, [rec("JT_1"), rec("NEW_2")], dispatched, mock_redis,
                     activation_baseline=set(),
                     activation_deadline=datetime.now(timezone.utc) + timedelta(seconds=60))
    await poller.poll_once()
    assert [e.broker_order_id for e in dispatched] == ["NEW_2"]


@pytest.mark.asyncio
async def test_expired_snapshot_baselines_everything_and_reports_the_missed_orders(db_session, mock_redis):
    acc = uuid.uuid4()
    dispatched = []
    poller = _poller(acc, [rec("HIST"), rec("LATE_1")], dispatched, mock_redis,
                     activation_baseline={"HIST"},
                     activation_deadline=datetime.now(timezone.utc) - timedelta(seconds=1))
    await poller.poll_once()
    assert dispatched == []
    events = (await db_session.execute(
        select(SystemEvent).where(SystemEvent.event_type == "MASTER_ACTIVATION_ORDERS_NOT_COPIED")
    )).scalars().all()
    assert len(events) == 1 and events[0].metadata_payload["broker_order_ids"] == ["LATE_1"]


@pytest.mark.asyncio
async def test_a_replayed_order_is_never_copied_twice(db_session, mock_redis):
    """Replayed through the REAL detector: the first sighting is the
    creation, later sightings of the same order are its lifecycle."""
    acc = str(uuid.uuid4())
    detector = MasterTradeDetector(master_account_id=acc)
    trade_events = []

    def on_update(event):
        trade_event = detector.process_order_update(event)
        if trade_event is not None:
            trade_events.append(trade_event.event_type)

    book = [rec("NEW_1")]
    client = MagicMock(get_order_book=AsyncMock(side_effect=lambda: list(book)))
    poller = MasterOrderPoller(
        master_account_id=acc, client_provider=lambda _a: client, on_order_update=on_update,
        pipeline=CopyEnginePipeline(validator=EventValidator(redis=mock_redis)),
        activation_baseline=set(), activation_deadline=datetime.now(timezone.utc) + timedelta(seconds=60),
    )
    await poller.poll_once()
    await poller.poll_once()  # unchanged: not re-dispatched
    book[0] = rec("NEW_1", "COMPLETE", filled=1)
    await poller.poll_once()
    assert trade_events == ["LIMIT_CREATED", "ORDER_FILLED"]


# ─── the snapshot itself ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_preflight_snapshot_uses_the_poller_order_id_and_is_not_exposed(db_session):
    old = await _account(db_session, "old", AccountType.MASTER)
    new = await _account(db_session, "new")
    strategy = await link_strategy(db_session, old, [])
    new_rows = [{"brokerOrderId": "260101000001", "orderStatus": "COMPLETE"},
                {"Nstordno": "260101000002", "Status": "CANCELED"}]
    brokers = {str(old.id): _Broker(), str(new.id): _Broker(book=new_rows)}
    before = datetime.now(timezone.utc)
    pre = await strategy_switch.run_preflight(db_session, strategy.id, new.id, "REMOVE", _provider(brokers))
    ids, deadline = pre.activations()[str(new.id)]
    assert ids == {order_book_row_id(r) for r in new_rows} == {"260101000001", "260101000002"}
    assert before + strategy_switch.ACTIVATION_REPLAY_WINDOW <= deadline
    assert "new_master_order_ids" not in pre.to_dict() and "snapshot_at" not in pre.to_dict()


@pytest.mark.asyncio
async def test_switch_starts_the_new_masters_monitor_with_the_locked_snapshot(db_session, mock_redis):
    legacy = await _account(db_session, "legacy", AccountType.MASTER)
    dhinesh, vikash = await _account(db_session, "dhinesh"), await _account(db_session, "vikash")
    strategy = await link_strategy(db_session, dhinesh, [vikash], name="Intraday")
    ids = {"legacy": str(legacy.id), "dhinesh": str(dhinesh.id), "vikash": str(vikash.id)}
    strategy_id = strategy.id
    brokers = {ids["dhinesh"]: _Broker(), ids["vikash"]: _Broker(book=[{"brokerOrderId": "V_HIST",
                                                                         "orderStatus": "COMPLETE"}])}
    captured = {}

    class _CapturingPoller(_FakePoller):
        def __init__(self, master_account_id, **kwargs):
            super().__init__(master_account_id, **kwargs)
            captured[master_account_id] = kwargs

    with _engine_patches(ids["legacy"], {}, MagicMock(save_or_update_order=AsyncMock())), \
         patch("app.services.runtime.engine_loop.MasterOrderPoller", _CapturingPoller):
        loop = EngineLoop(redis_client=mock_redis)
        await loop._run()
        assert captured[ids["dhinesh"]]["activation_baseline"] is None  # startup: unchanged first-poll seed
        await strategy_switch.execute_switch(
            db_session, strategy_id, uuid.UUID(ids["vikash"]), "CHILD", _provider(brokers),
            after_commit=lambda pf: loop.sync_master_monitors(seed_timeout_seconds=0,
                                                              activations=pf.activations()),
        )
        assert captured[ids["vikash"]]["activation_baseline"] == {"V_HIST"}
        assert captured[ids["vikash"]]["activation_deadline"] > datetime.now(timezone.utc)
        await loop.stop()


# ─── the switch gate ───────────────────────────────────────────────────────

async def _swap_master_in_db(strategy_id, new_master_id, old_master_id):
    from conftest import TestingSessionLocal
    from app.models.strategies import GroupStrategy, StrategyAccountRole, StrategyMember

    async with TestingSessionLocal() as s:
        strat = await s.get(GroupStrategy, strategy_id)
        await s.execute(StrategyAccountRole.__table__.delete())
        await s.execute(StrategyMember.__table__.delete())
        strat.master_account_id = new_master_id
        s.add(StrategyMember(strategy_id=strat.id, account_id=old_master_id))
        await s.commit()


@pytest.mark.asyncio
async def test_new_master_event_during_switch_waits_and_is_processed_not_dropped(db_session, mock_redis, monkeypatch):
    monkeypatch.setattr("app.services.runtime.engine_loop.settings.ENGINE_LIVE_TRADING_ENABLED", True)
    # Live Trading state is read from the database (stored value, else this env default).
    monkeypatch.setattr("app.services.live_trading.AsyncSessionLocal", __import__("conftest").TestingSessionLocal)
    monkeypatch.setattr("app.services.runtime.engine_loop.is_master_trading_enabled", AsyncMock(return_value=True))
    dhinesh, vikash = await _account(db_session, "dhinesh", AccountType.MASTER), await _account(db_session, "vikash")
    strategy = await link_strategy(db_session, dhinesh, [vikash])
    d_id, v_id, strategy_id = str(dhinesh.id), str(vikash.id), strategy.id
    detectors = {}
    trade = MagicMock(event_type="LIMIT_CREATED", master_order_id="V1", symbol="ITC", side="BUY", quantity=1)
    pipeline_instance = MagicMock(save_or_update_order=AsyncMock(),
                                  execute_replication=AsyncMock(return_value={"status": "completed"}))
    with _engine_patches(d_id, detectors, pipeline_instance, {d_id: object(), v_id: object()}):
        loop = EngineLoop(redis_client=mock_redis)
        await loop._run()
        async with membership.accounts_switch_lock([d_id, v_id]):
            await _swap_master_in_db(strategy_id, uuid.UUID(v_id), uuid.UUID(d_id))
            await loop.sync_master_monitors(seed_timeout_seconds=0)
            detectors[v_id].process_order_update.return_value = trade
            # The new Master's poller dispatches its first new order while the
            # switch still holds the gate...
            loop._on_order_update(_ws_event(v_id, "V1"))
            await asyncio.sleep(0.05)
            detectors[v_id].process_order_update.assert_not_called()
        # ...and it is processed, exactly once, the moment the switch ends.
        await _drain()
        detectors[v_id].process_order_update.assert_called_once()
        pipeline_instance.execute_replication.assert_awaited_once()
        await loop.stop()


@pytest.mark.asyncio
async def test_old_master_event_during_switch_is_recorded_reported_and_not_copied(db_session, mock_redis, monkeypatch):
    monkeypatch.setattr("app.services.runtime.engine_loop.settings.ENGINE_LIVE_TRADING_ENABLED", True)
    # Live Trading state is read from the database (stored value, else this env default).
    monkeypatch.setattr("app.services.live_trading.AsyncSessionLocal", __import__("conftest").TestingSessionLocal)
    monkeypatch.setattr("app.services.runtime.engine_loop.is_master_trading_enabled", AsyncMock(return_value=True))
    dhinesh, vikash = await _account(db_session, "dhinesh", AccountType.MASTER), await _account(db_session, "vikash")
    strategy = await link_strategy(db_session, dhinesh, [vikash])
    d_id, v_id, strategy_id = str(dhinesh.id), str(vikash.id), strategy.id
    detectors = {}
    pipeline_instance = MagicMock(save_or_update_order=AsyncMock(),
                                  execute_replication=AsyncMock(return_value={"status": "completed"}))
    with _engine_patches(d_id, detectors, pipeline_instance, {d_id: object(), v_id: object()}):
        loop = EngineLoop(redis_client=mock_redis)
        await loop._run()
        async with membership.accounts_switch_lock([d_id, v_id]):
            loop._ws_client.on_order_update(_ws_event(d_id, "D_LATE"))
            await asyncio.sleep(0.05)
            await _swap_master_in_db(strategy_id, uuid.UUID(v_id), uuid.UUID(d_id))
            await loop.sync_master_monitors(seed_timeout_seconds=0)
        await _drain()
        pipeline_instance.save_or_update_order.assert_awaited()  # recorded
        detectors[d_id].process_order_update.assert_not_called()
        pipeline_instance.execute_replication.assert_not_called()  # not copied
        events = (await db_session.execute(select(SystemEvent).where(
            SystemEvent.event_type == "ORDER_DURING_MASTER_SWITCH_NOT_COPIED"))).scalars().all()
        assert [e.metadata_payload["broker_order_id"] for e in events] == ["D_LATE"]  # reported
        await loop.stop()
