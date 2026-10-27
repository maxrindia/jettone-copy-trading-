"""
Regression coverage for the confirmed ORDER_REJECTED replication bug.

Root cause (confirmed via live, empirical reproduction on 2026-09-30, no real
broker calls made): CopyEnginePipeline.execute_replication's if/elif chain
had no branch for event_type == "ORDER_REJECTED". Since none of the existing
branches (ORDER_MODIFIED, LIMIT_CREATED, SL_PENDING_CREATED/MODIFIED,
ORDER_CANCELLED, ORDER_FILLED) matched, a rejected master order fell through
to the unconditional _execute_market_replication fallback at the bottom of
the function and was replicated to followers as if it were a real fill -
client.place_order() was called with the rejected order's price/quantity.

This predates any recent change: git log -S "ORDER_REJECTED" on
pipeline.py/detector.py shows exactly one commit ever touched it
(bc071ec, 2026-09-03), which added ORDER_REJECTED detection to detector.py
and, in the same diff, added explicit ORDER_CANCELLED/ORDER_FILLED branches
to execute_replication but never a matching ORDER_REJECTED one. The gap is
original, not a regression.

Fix: one new elif branch in execute_replication, mirroring the existing
ORDER_CANCELLED branch's pattern - a pure no-op, no follower action of any
kind, no DB writes.

No live trading and no real broker calls are made anywhere in this file -
all broker clients are AsyncMock test doubles and the DB is the in-memory
test SQLite engine (db_session fixture).
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.models.trades import CopyTrade, Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.types import OrderRecord, OrderSide as BrokerOrderSide
from app.services.copy_engine.detector import MasterTradeDetector
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.reliability.master_order_poller import MasterOrderPoller
from strategy_helpers import ensure_account

MASTER_ID = "caf4726b-c48c-4e54-b899-ceb0f7259da8"


def _rejected_follower_client():
    """A follower broker client that must NEVER be called by this branch -
    every method is an AsyncMock so a call is unambiguously observable via
    assert_not_awaited()."""
    client = AsyncMock()
    client.place_order = AsyncMock()
    client.modify_order = AsyncMock()
    client.cancel_order = AsyncMock()
    return client


def _rejected_trade_event(broker_order_id="ORD_REJECTED_1", quantity=10, price=Decimal("265.00")):
    return MasterTradeEvent(
        master_order_id=broker_order_id,
        symbol="ITC",
        exchange="NSE",
        side="BUY",
        quantity=quantity,
        price=price,
        product_type="MIS",
        timestamp=datetime.now(timezone.utc),
        account_id=MASTER_ID,
        order_type="LIMIT",
        event_type="ORDER_REJECTED",
        status="REJECTED",
        rejection_reason="RMS:Margin Exceeds",
    )


# ---------------------------------------------------------------------------
# 1. Direct execute_replication() test.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_replication_rejected_master_order_takes_no_follower_action(mock_redis):
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    client = _rejected_follower_client()
    event = _rejected_trade_event()

    result = await pipeline.execute_replication(
        event=event,
        clients={"some-follower-id": client},
    )

    assert result == {"status": "master_order_rejected", "reason": "RMS:Margin Exceeds"}
    client.place_order.assert_not_awaited()
    client.modify_order.assert_not_awaited()
    client.cancel_order.assert_not_awaited()


# ---------------------------------------------------------------------------
# 2-4. Full realistic regression: MasterOrderPoller -> MasterTradeDetector ->
# execute_replication, for a master order whose FIRST observed broker status
# is already REJECTED (the realistic case - a broker-rejected order never
# passes through OPEN/PENDING first, so there is no prior successful event
# competing for the same Redis dedup key).
# ---------------------------------------------------------------------------

async def _persist_master_row(pipeline, event):
    """Mirrors engine_loop.py's persist_and_process_event STEP 1 exactly -
    MasterOrderPoller itself does not persist a dispatched (replication-
    eligible) order; that is the caller's job in production."""
    status_mapped = {
        "COMPLETE": OrderStatus.COMPLETE, "OPEN": OrderStatus.OPEN, "PARTIAL": OrderStatus.PARTIAL,
        "PENDING": OrderStatus.PENDING, "CANCELLED": OrderStatus.CANCELLED, "REJECTED": OrderStatus.REJECTED,
    }.get(event.status, OrderStatus.OPEN)
    await pipeline.save_or_update_order(
        internal_order_id=f"EXT_{event.broker_order_id}_{MASTER_ID[:8]}",
        broker_order_id=event.broker_order_id, account_id=uuid.UUID(MASTER_ID),
        source=OrderSource.BROKER_EXTERNAL, exchange=event.exchange or "NSE", symbol=event.symbol,
        side=OrderSide.BUY if event.side == "BUY" else OrderSide.SELL,
        order_type=event.order_type or "MARKET", product_type=event.product_type,
        quantity=event.quantity, filled_quantity=event.filled_quantity,
        pending_quantity=max(0, event.quantity - event.filled_quantity),
        price=event.price if event.price > 0 else event.fill_price,
        trigger_price=event.trigger_price, average_price=event.fill_price,
        status=status_mapped, broker_status=event.status, rejection_reason=event.rejection_reason,
    )


def _rejected_record(broker_order_id, quantity=10, price=Decimal("265.00")):
    return OrderRecord(
        broker_order_id=broker_order_id, client_order_id=None, symbol="ITC", exchange="NSE",
        side=BrokerOrderSide.BUY, quantity=quantity, filled_quantity=0, average_price=Decimal("0"),
        status="REJECTED", order_timestamp=datetime.now(timezone.utc), order_type="LIMIT",
        product_type="MIS", price=price, trigger_price=Decimal("0"),
        rejection_reason="RMS:Margin Exceeds",
    )


@pytest.mark.asyncio
async def test_first_observed_rejected_master_order_never_reaches_follower_broker(db_session, mock_redis, sample_accounts):
    await ensure_account(db_session, MASTER_ID)        # the persisted Master row needs a real account (PostgreSQL enforces the FK)
    follower = sample_accounts["follower1"]
    follower_client = _rejected_follower_client()

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    detector = MasterTradeDetector(master_account_id=MASTER_ID)

    dispatched = []

    async def on_order_update(event):
        dispatched.append(event)
        await _persist_master_row(pipeline, event)
        trade_event = detector.process_order_update(event)
        if trade_event is None:
            return
        await pipeline.execute_replication(
            event=trade_event,
            clients={str(follower.id): follower_client},
        )

    poller = MasterOrderPoller(
        master_account_id=MASTER_ID,
        client_provider=lambda _acc_id: MagicMock(get_order_book=AsyncMock(return_value=[])),
        on_order_update=on_order_update,
        pipeline=pipeline,
    )
    # Baseline seed pass (empty book) - matches how the poller starts in
    # production: nothing pre-existing, so the rejected order below is
    # genuinely new, not a baseline artifact.
    await poller.poll_once()
    assert dispatched == []

    poller.client_provider = lambda _acc_id: MagicMock(
        get_order_book=AsyncMock(return_value=[_rejected_record("ORD_REJECTED_FIRST_SIGHT")])
    )
    count = await poller.poll_once()

    # The poller itself still dispatches (REJECTED is a known/mapped status,
    # unchanged behavior - see master_order_poller.py) - the protection is
    # downstream, in execute_replication.
    assert count == 1
    assert len(dispatched) == 1

    # 3. Master Order row is still persisted as REJECTED.
    master_rows = (
        await db_session.execute(
            select(Order).where(Order.broker_order_id == "ORD_REJECTED_FIRST_SIGHT")
        )
    ).scalars().all()
    assert len(master_rows) == 1
    assert master_rows[0].status == OrderStatus.REJECTED
    assert master_rows[0].source == OrderSource.BROKER_EXTERNAL

    # No follower broker call of any kind.
    follower_client.place_order.assert_not_awaited()
    follower_client.modify_order.assert_not_awaited()
    follower_client.cancel_order.assert_not_awaited()

    # 4. No follower CopyTrade execution created.
    ct_rows = (
        await db_session.execute(
            select(CopyTrade).where(CopyTrade.master_order_id == "ORD_REJECTED_FIRST_SIGHT")
        )
    ).scalars().all()
    assert ct_rows == []

    # No follower Order row created either.
    follower_order_rows = (
        await db_session.execute(
            select(Order).where(Order.account_id == follower.id)
        )
    ).scalars().all()
    assert follower_order_rows == []


# ---------------------------------------------------------------------------
# 5. Existing ORDER_CANCELLED behavior remains unchanged by the new branch.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_order_cancelled_still_routes_to_handle_master_cancellation(mock_redis):
    """The new ORDER_REJECTED elif sits directly after the existing
    ORDER_CANCELLED elif in the chain - this proves that insertion did not
    disturb ORDER_CANCELLED's own routing (e.g. via a misplaced return or
    indentation slip)."""
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    pipeline.handle_master_cancellation = AsyncMock(return_value={"status": "no_open_child_orders"})

    event = MasterTradeEvent(
        master_order_id="ORD_CANCEL_1",
        symbol="ITC",
        exchange="NSE",
        side="BUY",
        quantity=1,
        price=Decimal("265.00"),
        product_type="MIS",
        timestamp=datetime.now(timezone.utc),
        account_id=MASTER_ID,
        order_type="LIMIT",
        event_type="ORDER_CANCELLED",
        status="CANCELLED",
    )

    result = await pipeline.execute_replication(event=event, clients={})

    pipeline.handle_master_cancellation.assert_awaited_once_with(
        master_order_id="ORD_CANCEL_1",
        clients={},
    )
    assert result == {"status": "no_open_child_orders"}
