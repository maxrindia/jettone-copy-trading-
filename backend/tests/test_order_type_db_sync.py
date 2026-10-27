"""
Regression coverage for the master-order_type DB-staleness fix.

Root cause: save_or_update_order()'s UPDATE branch never wrote order_type on
an existing row - it was set only at creation. A master order whose
broker-reported type later changed (LIMIT -> MARKET while still open, or a
native SL/SL-M converting to LIMIT/MARKET once triggered) kept showing its
ORIGINAL order_type in the DB forever, even though status/filled_quantity/
price all updated correctly.

Investigation confirmed this is a DB-consistency/display issue (category A),
not a correctness bug in replication or restart recovery:
- Every behavioral branch in MasterTradeDetector reads order_type from the
  LIVE broker event each poll, never from the persisted Order row.
- Restart recovery's own child-identity check (seed_state's _known_order_ids
  membership) has an independent, order_type-free safety net:
  known_child_master_order_ids, computed in engine_loop.py by directly
  querying actual SYSTEM-sourced child Order rows - never consulting the
  master's own order_type field.
- reconciliation.py never reads order_type at all (purely quantity-based).

Fix: save_or_update_order's UPDATE branch now refreshes order_type when the
incoming value is non-empty (same "falsy means not reported" guard already
used for price/quantity/trigger_price). Pure persistence correction - never
dispatches an event, calls a broker API, or changes a replication decision.

No live trading and no real broker calls are made anywhere in this file -
all broker clients are AsyncMock test doubles and the DB is the in-memory
test SQLite engine (db_session fixture).
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.types import OrderResult, OrderResultCategory, OrderUpdateEvent
from app.services.copy_engine.detector import MasterTradeDetector
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from strategy_helpers import SAMPLE_MASTER_ACCOUNT_ID, ensure_account

# The sample_accounts master, so events from MASTER_ID route to sample_strategy.
MASTER_ID = SAMPLE_MASTER_ACCOUNT_ID


# ---------------------------------------------------------------------------
# 1. The core fix: LIMIT -> MARKET updates the persisted order_type.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_limit_to_market_conversion_updates_persisted_order_type(db_session, mock_redis):
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    master_acc_id = (await ensure_account(db_session)).id

    created = await pipeline.save_or_update_order(
        internal_order_id="EXT_ORD_OT_1_M_OT_", broker_order_id="ORD_OT_1",
        account_id=master_acc_id, source=OrderSource.BROKER_EXTERNAL,
        exchange="NSE", symbol="RELIANCE", side=OrderSide.BUY, order_type="LIMIT",
        product_type="MIS", quantity=100, price=Decimal("2950.00"), status=OrderStatus.OPEN,
    )
    assert created.order_type == "LIMIT"

    updated = await pipeline.save_or_update_order(
        internal_order_id="EXT_ORD_OT_1_M_OT_", broker_order_id="ORD_OT_1",
        account_id=master_acc_id, source=OrderSource.BROKER_EXTERNAL,
        exchange="NSE", symbol="RELIANCE", side=OrderSide.BUY, order_type="MARKET",
        product_type="MIS", quantity=100, price=Decimal("0"), status=OrderStatus.OPEN,
    )
    assert updated.order_type == "MARKET"

    row = (
        await db_session.execute(select(Order).where(Order.broker_order_id == "ORD_OT_1"))
    ).scalar_one()
    assert row.order_type == "MARKET"
    # Preserved fields - the fix touches order_type only.
    assert row.broker_order_id == "ORD_OT_1"
    assert row.status == OrderStatus.OPEN


@pytest.mark.asyncio
async def test_native_sl_trigger_updates_persisted_order_type_to_limit(db_session, mock_redis):
    """Mirrors the other confirmed-live pattern: a native SL/SL-M order
    reported as order_type SL while TRIGGER_PENDING, then reported as LIMIT
    once triggered - all other fields (trigger_price, price, status) must be
    preserved exactly as the existing guards already ensure."""
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    master_acc_id = (await ensure_account(db_session)).id

    await pipeline.save_or_update_order(
        internal_order_id="EXT_ORD_OT_SL_M_OT_", broker_order_id="ORD_OT_SL",
        account_id=master_acc_id, source=OrderSource.BROKER_EXTERNAL,
        exchange="NSE", symbol="ITC", side=OrderSide.BUY, order_type="SL",
        product_type="MIS", quantity=1, price=Decimal("265.65"),
        trigger_price=Decimal("265.60"), status=OrderStatus.PENDING,
    )

    updated = await pipeline.save_or_update_order(
        internal_order_id="EXT_ORD_OT_SL_M_OT_", broker_order_id="ORD_OT_SL",
        account_id=master_acc_id, source=OrderSource.BROKER_EXTERNAL,
        exchange="NSE", symbol="ITC", side=OrderSide.BUY, order_type="LIMIT",
        product_type="MIS", quantity=1, filled_quantity=1, price=Decimal("265.65"),
        trigger_price=Decimal("0"),  # broker stops reporting trigger_price post-trigger
        status=OrderStatus.COMPLETE,
    )

    assert updated.order_type == "LIMIT"
    assert updated.trigger_price == Decimal("265.60")  # preserved - the existing "0 means not reported" guard
    assert updated.price == Decimal("265.65")           # preserved
    assert updated.status == OrderStatus.COMPLETE
    assert updated.broker_order_id == "ORD_OT_SL"


# ---------------------------------------------------------------------------
# 2. SL/SL-M trigger still does not create a duplicate child, with the fix.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sl_trigger_still_does_not_create_duplicate_child_with_order_type_now_synced(db_session, mock_redis, sample_accounts, sample_strategy):
    from decimal import Decimal as D
    from app.services.alice_blue.types import FundsData, OrderType

    follower = sample_accounts["follower1"]
    client = AsyncMock()
    client.get_funds.return_value = FundsData(
        account_id="f", available_margin=D("500000"), collateral=D("0"),
        used_margin=D("0"), updated_at=datetime.now(timezone.utc),
    )
    client.get_positions.return_value = []
    client.place_order.return_value = OrderResult(
        category=OrderResultCategory.SUCCESS, client_order_id="c", broker_order_id="BRK_SL_SYNC",
        message="ok", raw_response={"stat": "Ok"}, timestamp=datetime.now(timezone.utc),
    )

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    detector = MasterTradeDetector(master_account_id=MASTER_ID)
    await ensure_account(db_session, uuid.uuid5(uuid.NAMESPACE_DNS, MASTER_ID))   # persist() stores the Master row under this id

    async def persist(event):
        status_mapped = {
            "COMPLETE": OrderStatus.COMPLETE, "OPEN": OrderStatus.OPEN, "PARTIAL": OrderStatus.PARTIAL,
            "PENDING": OrderStatus.PENDING, "CANCELLED": OrderStatus.CANCELLED, "REJECTED": OrderStatus.REJECTED,
        }.get(event.status, OrderStatus.OPEN)
        await pipeline.save_or_update_order(
            internal_order_id=f"EXT_{event.broker_order_id}_{MASTER_ID[:8]}",
            broker_order_id=event.broker_order_id, account_id=uuid.uuid5(uuid.NAMESPACE_DNS, MASTER_ID),
            source=OrderSource.BROKER_EXTERNAL, exchange=event.exchange or "NSE", symbol=event.symbol,
            side=OrderSide.BUY if event.side == "BUY" else OrderSide.SELL,
            order_type=event.order_type or "MARKET", product_type=event.product_type,
            quantity=event.quantity, filled_quantity=event.filled_quantity,
            pending_quantity=max(0, event.quantity - event.filled_quantity),
            price=event.price if event.price > 0 else event.fill_price,
            trigger_price=event.trigger_price, average_price=event.fill_price,
            status=status_mapped, broker_status=event.status, rejection_reason=event.rejection_reason,
        )

    # 1. Pending SL detected -> follower child created.
    pending_event = OrderUpdateEvent(
        broker_order_id="ORD_SL_SYNC", client_order_id=None, symbol="ITC", side="BUY",
        quantity=1, filled_quantity=0, fill_price=D("0"), status="PENDING",
        timestamp=datetime.now(timezone.utc), account_id=MASTER_ID, order_type="SL",
        price=D("265.65"), trigger_price=D("265.60"),
    )
    await persist(pending_event)
    trade_event = detector.process_order_update(pending_event)
    assert trade_event.event_type == "SL_PENDING_CREATED"
    res = await pipeline.execute_replication(event=trade_event, clients={str(follower.id): client})
    assert res["results"][str(follower.id)]["status"] == "SUCCESS"
    detector.mark_sl_child_created("ORD_SL_SYNC")

    # 2. Master triggers: broker now reports order_type=LIMIT, status=COMPLETE.
    # persist() now ALSO updates the master's own Order.order_type (the fix) -
    # this must still not create a second follower child.
    triggered_event = OrderUpdateEvent(
        broker_order_id="ORD_SL_SYNC", client_order_id=None, symbol="ITC", side="BUY",
        quantity=1, filled_quantity=1, fill_price=D("265.65"), status="COMPLETE",
        timestamp=datetime.now(timezone.utc), account_id=MASTER_ID, order_type="LIMIT",
        price=D("265.65"), trigger_price=D("0"),
    )
    await persist(triggered_event)
    fill_event = detector.process_order_update(triggered_event)
    assert fill_event.event_type == "ORDER_FILLED"  # not LIMIT_CREATED/MARKET_ACCEPTED

    await pipeline.execute_replication(event=fill_event, clients={str(follower.id): client})

    # Exactly one child order exists for this master order - no duplicate.
    child_rows = (
        await db_session.execute(select(Order).where(Order.master_broker_order_id == "ORD_SL_SYNC"))
    ).scalars().all()
    assert len(child_rows) == 1
    client.place_order.assert_called_once()  # never called a second time


# ---------------------------------------------------------------------------
# 3. Restart recovery still works after order_type has been synced in DB.
# ---------------------------------------------------------------------------

def test_restart_recovery_unaffected_by_now_current_order_type(db_session, mock_redis):
    """Simulates the theoretical edge case the fix could affect: a master
    order's DB row now correctly shows order_type=MARKET (post-fix) instead
    of the stale LIMIT it used to retain. seed_state's _known_order_ids
    membership must still resolve correctly via the independent
    known_child_master_order_ids safety net, and a subsequent fill must
    still be recognized as the SAME already-known order, not a new one."""
    from types import SimpleNamespace

    detector = MasterTradeDetector(master_account_id=MASTER_ID)

    # Order row AS IT WOULD NOW APPEAR with the fix applied: already
    # converted to MARKET in the DB (previously this would have stayed
    # "LIMIT" forever). order_type == "LIMIT" is no longer true for this
    # row, so the OLD code path relying on that check to grant
    # _known_order_ids membership would fail - the restart-safety net
    # (known_child_master_order_ids) must carry it instead.
    seeded_order = SimpleNamespace(
        broker_order_id="ORD_RESTART_SYNC", filled_quantity=0, order_type="MARKET",
        quantity=100, price=Decimal("0"),
    )
    detector.seed_state([seeded_order], known_child_master_order_ids={"ORD_RESTART_SYNC"})

    assert "ORD_RESTART_SYNC" in detector._known_order_ids

    # A fill arrives post-restart: must be classified as ORDER_FILLED (same
    # already-known order), never MARKET_ACCEPTED (which would replicate a
    # brand-new, duplicate child).
    fill_event = OrderUpdateEvent(
        broker_order_id="ORD_RESTART_SYNC", client_order_id=None, symbol="RELIANCE", side="BUY",
        quantity=100, filled_quantity=100, fill_price=Decimal("3000.00"), status="COMPLETE",
        timestamp=datetime.now(timezone.utc), account_id=MASTER_ID, order_type="MARKET",
        price=Decimal("0"),
    )
    trade_event = detector.process_order_update(fill_event)
    assert trade_event.event_type == "ORDER_FILLED"


# ---------------------------------------------------------------------------
# 4. Existing plain LIMIT behavior is unchanged.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_plain_limit_order_type_unaffected_through_its_lifecycle(db_session, mock_redis):
    """A LIMIT order that is never converted keeps order_type == "LIMIT"
    through OPEN -> PARTIAL -> COMPLETE, exactly as before the fix."""
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    master_acc_id = (await ensure_account(db_session)).id

    for status, filled in [(OrderStatus.OPEN, 0), (OrderStatus.PARTIAL, 40), (OrderStatus.COMPLETE, 100)]:
        row = await pipeline.save_or_update_order(
            internal_order_id="EXT_ORD_OT_PLAIN_M_OT_", broker_order_id="ORD_OT_PLAIN",
            account_id=master_acc_id, source=OrderSource.BROKER_EXTERNAL,
            exchange="NSE", symbol="RELIANCE", side=OrderSide.BUY, order_type="LIMIT",
            product_type="MIS", quantity=100, filled_quantity=filled,
            price=Decimal("2950.00"), status=status,
        )
        assert row.order_type == "LIMIT"
