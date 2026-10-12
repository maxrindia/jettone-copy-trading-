"""
Immediate native SL/SL-M replication.

Approved behavior change (supersedes the earlier "wait for trigger" design):
a master's native SL/SL-M order is copied to every eligible follower AS ITS
OWN INDEPENDENT SL/SL-M order immediately, while the master stays
TRIGGER_PENDING - never waiting for the master to actually trigger. Master
and follower SL orders execute independently from that point on; Jettone
never forces one to track the other's eventual trigger.

Covers, end to end:
- detection of a first-time pending SL/SL-M (SL_PENDING_CREATED)
- exactly one follower child created, at the master's EXACT price/
  trigger_price - never compute_marketable_limit_price, never
  MARKET_ORDER_BUFFER_PERCENT
- a genuine price/trigger modification while still pending
  (SL_PENDING_MODIFIED) modifying the SAME child, never creating a second
- idempotency across repeated identical polls
- the master's eventual trigger does NOT create a duplicate child, and
  specifically cannot be misrouted into the missed-LIMIT-reprice logic or
  the LIMIT->MARKET reconciliation branch (both of which pre-date this
  feature and reprice/reconcile LIMIT children, never SL/SL-M ones)
- cancellation of a still-pending master SL cancels the real follower child
- restart recovery does not duplicate the child
- multiple followers each get their own independent child

No live trading and no real broker calls are made anywhere in this file -
all broker clients are AsyncMock test doubles and the DB is the in-memory
test SQLite engine (db_session fixture).
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.core.config import settings as app_settings
from app.models.accounts import Account, AccountType
from app.models.trades import CopyTrade, Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.types import (
    FundsData, OrderRecord, OrderResult, OrderResultCategory,
    OrderSide as BrokerOrderSide, OrderType, Position,
)
from app.services.copy_engine.detector import MasterTradeDetector
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.reliability.master_order_poller import MasterOrderPoller
from strategy_helpers import SAMPLE_MASTER_ACCOUNT_ID, add_child

# The sample_accounts master, so events from MASTER_ID route to sample_strategy.
MASTER_ID = SAMPLE_MASTER_ACCOUNT_ID


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

def _record(broker_order_id, order_type, status, price=Decimal("0"), trigger_price=Decimal("0"),
            quantity=1, filled_quantity=0, side=BrokerOrderSide.SELL):
    return OrderRecord(
        broker_order_id=broker_order_id, client_order_id=None, symbol="ITC", exchange="NSE",
        side=side, quantity=quantity, filled_quantity=filled_quantity, average_price=price,
        status=status, order_timestamp=datetime.now(timezone.utc), order_type=order_type,
        product_type="INTRADAY", price=price, trigger_price=trigger_price,
    )


def _pending(broker_order_id, order_type, price=Decimal("0"), trigger_price=Decimal("321.90"),
             quantity=1, side=BrokerOrderSide.SELL):
    return _record(broker_order_id, order_type, "TRIGGER_PENDING", price=price,
                    trigger_price=trigger_price, quantity=quantity, side=side)


def _sl_follower_client(broker_order_id="BRK_SL_CHILD", positions=None, place_result=None, modify_result=None):
    client = AsyncMock()
    client.get_funds.return_value = FundsData(
        account_id="f", available_margin=Decimal("500000"), collateral=Decimal("0"),
        used_margin=Decimal("0"), updated_at=datetime.now(timezone.utc),
    )
    client.get_positions.return_value = positions if positions is not None else []
    client.get_positions_or_none.return_value = positions if positions is not None else []
    client.place_order.return_value = place_result or OrderResult(
        category=OrderResultCategory.SUCCESS, client_order_id="c", broker_order_id=broker_order_id,
        message="ok", raw_response={"stat": "Ok"}, timestamp=datetime.now(timezone.utc),
    )
    client.modify_order.return_value = modify_result or OrderResult(
        category=OrderResultCategory.SUCCESS, client_order_id=None, broker_order_id=broker_order_id,
        message="Modified successfully", raw_response={"stat": "Ok"}, timestamp=datetime.now(timezone.utc),
    )
    client.cancel_order.return_value = OrderResult(
        category=OrderResultCategory.SUCCESS, client_order_id=None, broker_order_id=broker_order_id,
        message="Cancelled", raw_response={"stat": "Ok"}, timestamp=datetime.now(timezone.utc),
    )
    # handle_master_cancellation re-fetches the order book post-cancel to
    # verify the true filled_quantity before finalizing the row - default
    # to "found, cancelled, unfilled" so a plain cancel test doesn't need
    # to configure this separately.
    client.get_order_book.return_value = [
        _record(broker_order_id, "SL", "CANCELLED", price=Decimal("0"), filled_quantity=0)
    ]
    return client


def _covering_long(qty=1):
    return [Position(symbol="ITC-EQ", quantity=qty, side="BUY", average_price=Decimal("264.00"),
                      account_id="a", exchange="NSE", product_type="MIS")]


async def _persist_master_row(pipeline, event):
    """Mirrors engine_loop.py's persist_and_process_event STEP 1 exactly -
    the master's own Order row is kept current independently of whatever
    the detector decides to do with the same event."""
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


def _wire_engine(pipeline, detector, clients, results):
    """Mirrors engine_loop.py's persist_and_process_event end to end:
    persist master row -> detector -> execute_replication -> conditional
    mark_sl_child_created - the exact call ordering the deployed code
    uses, reproduced here since these tests dispatch directly to the
    poller/detector without a real EngineLoop."""
    async def on_order_update(event):
        await _persist_master_row(pipeline, event)
        trade_event = detector.process_order_update(event)
        if trade_event is None:
            return
        # stagger_ms: the suite's single shared in-memory SQLite connection cannot
        # commit two followers' writes concurrently ("cannot commit transaction");
        # Postgres has no such limit. Same approach as test_scale_load.
        result = await pipeline.execute_replication(event=trade_event, clients=clients, stagger_ms=150)
        results.append((trade_event.event_type, result))
        if trade_event.event_type == "SL_PENDING_CREATED":
            child_results = result.get("results") if isinstance(result, dict) else None
            if isinstance(child_results, dict) and any(
                isinstance(r, dict) and r.get("status") == "SUCCESS" for r in child_results.values()
            ):
                detector.mark_sl_child_created(trade_event.master_order_id)
    return on_order_update


async def _order_rows(session, master_broker_order_id=None, source=None):
    stmt = select(Order)
    if master_broker_order_id:
        stmt = stmt.where(Order.master_broker_order_id == master_broker_order_id)
    if source:
        stmt = stmt.where(Order.source == source)
    return (await session.execute(stmt)).scalars().all()


# ---------------------------------------------------------------------------
# 1 / 2. Detector-level: first-time pending SL / SL-M detection
# ---------------------------------------------------------------------------

def _pending_event(broker_order_id="ORD_SL_1", order_type="SL", price=Decimal("391.50"),
                    trigger_price=Decimal("391.90"), quantity=1, filled_quantity=0, side="SELL"):
    from app.services.alice_blue.types import OrderUpdateEvent
    return OrderUpdateEvent(
        broker_order_id=broker_order_id, client_order_id=None, symbol="ITC", side=side,
        quantity=quantity, filled_quantity=filled_quantity, fill_price=Decimal("0"),
        status="PENDING", timestamp=datetime.now(timezone.utc), account_id=MASTER_ID,
        order_type=order_type, price=price, trigger_price=trigger_price,
    )


def test_pending_sl_is_detected_as_sl_pending_created():
    detector = MasterTradeDetector(master_account_id=MASTER_ID)
    event = detector.process_order_update(_pending_event(order_type="SL"))
    assert event is not None
    assert event.event_type == "SL_PENDING_CREATED"
    assert event.order_type == "SL"
    assert event.trigger_price == Decimal("391.90")
    assert event.price == Decimal("391.50")


def test_pending_sl_m_is_detected_as_sl_pending_created():
    detector = MasterTradeDetector(master_account_id=MASTER_ID)
    event = detector.process_order_update(
        _pending_event(order_type="SLM", price=Decimal("0"), trigger_price=Decimal("264.00"), side="BUY")
    )
    assert event is not None
    assert event.event_type == "SL_PENDING_CREATED"
    assert event.order_type == "SLM"
    assert event.trigger_price == Decimal("264.00")


# 8. Idempotency at the detector level: unchanged repeat -> no event

def test_unchanged_pending_sl_is_not_redetected():
    detector = MasterTradeDetector(master_account_id=MASTER_ID)
    first = detector.process_order_update(_pending_event())
    assert first is not None
    second = detector.process_order_update(_pending_event())  # identical
    assert second is None


# 7 (detector half). Genuine modification while still pending

def test_pending_sl_price_change_is_detected_as_modification():
    detector = MasterTradeDetector(master_account_id=MASTER_ID)
    detector.process_order_update(_pending_event(price=Decimal("391.50"), trigger_price=Decimal("391.90")))
    modified = detector.process_order_update(_pending_event(price=Decimal("390.00"), trigger_price=Decimal("390.40")))
    assert modified is not None
    assert modified.event_type == "SL_PENDING_MODIFIED"
    assert modified.trigger_price == Decimal("390.40")


# ---------------------------------------------------------------------------
# 3 / 4 / 5 / 6. End-to-end: pending SL creates exactly one follower child,
# at the master's EXACT price/trigger_price, never buffered.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pending_sl_creates_exactly_one_follower_child_with_exact_prices(db_session, mock_redis, sample_accounts, sample_strategy):
    follower = sample_accounts["follower1"]
    client = _sl_follower_client("BRK_SL_1", positions=_covering_long())
    detector = MasterTradeDetector(master_account_id=MASTER_ID)
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    results = []
    on_order_update = _wire_engine(pipeline, detector, {str(follower.id): client}, results)

    # A buffer is intentionally set non-zero here so a leak of the
    # marketable-LIMIT buffer into the SL path would be caught.
    original_buffer = app_settings.MARKET_ORDER_BUFFER_PERCENT
    app_settings.MARKET_ORDER_BUFFER_PERCENT = Decimal("1.0")
    try:
        await on_order_update(_pending_event(
            broker_order_id="26092900109495", order_type="SL",
            price=Decimal("391.50"), trigger_price=Decimal("391.90"), side="SELL",
        ))
    finally:
        app_settings.MARKET_ORDER_BUFFER_PERCENT = original_buffer

    assert [r[0] for r in results] == ["SL_PENDING_CREATED"]
    assert results[0][1]["status"] == "completed"
    client.place_order.assert_awaited_once()
    placed = client.place_order.call_args.args[0]
    assert placed.order_type == OrderType.SL
    assert placed.price == Decimal("391.50")          # exact master limit price
    assert placed.trigger_price == Decimal("391.90")  # exact master trigger price - no buffer applied

    rows = await _order_rows(db_session, master_broker_order_id="26092900109495")
    assert len(rows) == 1
    child = rows[0]
    assert child.source == OrderSource.SYSTEM
    assert child.order_type == "SL"
    assert child.status == OrderStatus.PENDING          # follower's own SL is also pending-trigger
    assert child.price == Decimal("391.50")
    assert child.trigger_price == Decimal("391.90")
    assert child.broker_order_id == "BRK_SL_1"

    ct_rows = (await db_session.execute(select(CopyTrade).where(CopyTrade.master_order_id == "26092900109495"))).scalars().all()
    assert len(ct_rows) == 1


@pytest.mark.asyncio
async def test_pending_sl_m_uses_exact_trigger_price_no_limit_price(db_session, mock_redis, sample_accounts, sample_strategy):
    follower = sample_accounts["follower1"]
    client = _sl_follower_client("BRK_SLM_1")
    detector = MasterTradeDetector(master_account_id=MASTER_ID)
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    results = []
    on_order_update = _wire_engine(pipeline, detector, {str(follower.id): client}, results)

    await on_order_update(_pending_event(
        broker_order_id="26092900007188", order_type="SLM",
        price=Decimal("0"), trigger_price=Decimal("264.00"), side="BUY",
    ))

    placed = client.place_order.call_args.args[0]
    assert placed.order_type == OrderType.SL_M
    assert placed.trigger_price == Decimal("264.00")

    row = (await _order_rows(db_session, master_broker_order_id="26092900007188"))[0]
    assert row.order_type == "SLM"
    assert row.trigger_price == Decimal("264.00")


# ---------------------------------------------------------------------------
# 7. Pending SL modification modifies the existing child - never a second one
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pending_sl_modification_modifies_existing_child_not_a_new_one(db_session, mock_redis, sample_accounts, sample_strategy):
    follower = sample_accounts["follower1"]
    client = _sl_follower_client("BRK_SL_MOD", positions=_covering_long())
    detector = MasterTradeDetector(master_account_id=MASTER_ID)
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    results = []
    on_order_update = _wire_engine(pipeline, detector, {str(follower.id): client}, results)

    await on_order_update(_pending_event(
        broker_order_id="ORD_SL_MOD_1", order_type="SL",
        price=Decimal("391.50"), trigger_price=Decimal("391.90"), side="SELL",
    ))
    client.place_order.assert_awaited_once()

    await on_order_update(_pending_event(
        broker_order_id="ORD_SL_MOD_1", order_type="SL",
        price=Decimal("390.00"), trigger_price=Decimal("390.40"), side="SELL",
    ))

    assert [r[0] for r in results] == ["SL_PENDING_CREATED", "SL_PENDING_MODIFIED"]
    assert results[1][1]["status"] == "completed"
    client.place_order.assert_awaited_once()   # still just once - never a second child
    client.modify_order.assert_awaited_once()
    _, kwargs = client.modify_order.call_args
    assert kwargs["broker_order_id"] == "BRK_SL_MOD"
    assert kwargs["price"] == Decimal("390.00")
    assert kwargs["trigger_price"] == Decimal("390.40")
    assert kwargs["order_type"] == OrderType.SL

    rows = await _order_rows(db_session, master_broker_order_id="ORD_SL_MOD_1")
    assert len(rows) == 1
    assert rows[0].price == Decimal("390.00")
    assert rows[0].trigger_price == Decimal("390.40")


# ---------------------------------------------------------------------------
# 9 / 10. Master SL trigger does not create another child, and cannot be
# misrouted into the missed-LIMIT-reprice or LIMIT->MARKET reconciliation
# logic (both pre-existing, both reprice/reconcile LIMIT children only).
# ---------------------------------------------------------------------------

def _trigger_lifecycle_book(client, child_id, pending_type, pending_price, pending_trigger, after_price, side):
    """Broker order book as the SL-trigger execution handler sees it:
    first read = child still TRIGGER_PENDING, second read (after the
    modify) = same order now a working LIMIT with no trigger."""
    client.get_order_book.side_effect = [
        [_record(child_id, pending_type, "TRIGGER_PENDING", price=pending_price,
                 trigger_price=pending_trigger, side=side)],
        [_record(child_id, "LIMIT", "OPEN", price=after_price, trigger_price=Decimal("0"), side=side)],
    ]


@pytest.mark.asyncio
async def test_sl_m_trigger_does_not_create_a_second_child(db_session, mock_redis, sample_accounts, sample_strategy):
    """SL-M triggers and fills as MARKET (the confirmed 26092900007188
    pattern). The SAME follower child is modified in place to a marketable
    LIMIT - never a second order, never a cancel."""
    from app.services.copy_engine.pipeline import compute_marketable_limit_price

    follower = sample_accounts["follower1"]
    client = _sl_follower_client("BRK_SLM_TRIG")
    detector = MasterTradeDetector(master_account_id=MASTER_ID)
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    results = []
    on_order_update = _wire_engine(pipeline, detector, {str(follower.id): client}, results)

    await on_order_update(_pending_event(
        broker_order_id="26092900007188", order_type="SLM", price=Decimal("0"),
        trigger_price=Decimal("264.00"), side="BUY",
    ))
    client.place_order.assert_awaited_once()

    target = compute_marketable_limit_price("BUY", Decimal("264.45"))
    _trigger_lifecycle_book(client, "BRK_SLM_TRIG", "SLM", Decimal("0"), Decimal("264.00"), target, BrokerOrderSide.BUY)

    from app.services.alice_blue.types import OrderUpdateEvent
    trigger_event = OrderUpdateEvent(
        broker_order_id="26092900007188", client_order_id=None, symbol="ITC", side="BUY",
        quantity=1, filled_quantity=1, fill_price=Decimal("264.45"), status="COMPLETE",
        timestamp=datetime.now(timezone.utc), account_id=MASTER_ID, order_type="MARKET", price=Decimal("0"),
    )
    await on_order_update(trigger_event)

    assert [r[0] for r in results] == ["SL_PENDING_CREATED", "ORDER_FILLED"]
    assert results[1][1]["executions"][str(follower.id)] == "MODIFIED: WORKING_MARKETABLE_LIMIT"
    client.place_order.assert_awaited_once()   # still just once - no duplicate/second child
    client.cancel_order.assert_not_called()
    client.modify_order.assert_awaited_once()
    assert client.modify_order.call_args.kwargs["broker_order_id"] == "BRK_SLM_TRIG"

    rows = await _order_rows(db_session, master_broker_order_id="26092900007188")
    assert len(rows) == 1   # exactly one child throughout
    assert rows[0].broker_order_id == "BRK_SLM_TRIG"
    assert rows[0].order_type == "LIMIT"


@pytest.mark.asyncio
async def test_native_sl_buy_trigger_reported_as_market_does_not_strand_the_follower(db_session, mock_redis, sample_accounts, sample_strategy):
    """Native SL (not SL-M) triggers and the master's fill is reported with
    order_type=MARKET (the confirmed live production pattern, master
    26100100168532 TATASTEEL BUY INTRADAY, 2026-10-01). The follower's own
    SL was still TRIGGER_PENDING at the broker; it is now pushed to execute
    by modifying the SAME order (26100100168552 in production) in place to
    a marketable LIMIT with the trigger removed - no second order, no
    cancel."""
    from app.services.copy_engine.pipeline import compute_marketable_limit_price

    follower = sample_accounts["follower1"]
    client = _sl_follower_client("BRK_SL_TATA_TRIG")
    detector = MasterTradeDetector(master_account_id=MASTER_ID)
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    results = []
    on_order_update = _wire_engine(pipeline, detector, {str(follower.id): client}, results)

    await on_order_update(_pending_event(
        broker_order_id="26100100168532", order_type="SL",
        price=Decimal("177.50"), trigger_price=Decimal("177.45"), side="BUY",
    ))
    client.place_order.assert_awaited_once()
    placed = client.place_order.call_args.args[0]
    assert placed.price == Decimal("177.50")
    assert placed.trigger_price == Decimal("177.45")

    target = compute_marketable_limit_price("BUY", Decimal("177.18"))
    _trigger_lifecycle_book(client, "BRK_SL_TATA_TRIG", "SL", Decimal("177.50"), Decimal("177.45"), target, BrokerOrderSide.BUY)

    from app.services.alice_blue.types import OrderUpdateEvent
    trigger_event = OrderUpdateEvent(
        broker_order_id="26100100168532", client_order_id=None, symbol="TATASTEEL", side="BUY",
        quantity=1, filled_quantity=1, fill_price=Decimal("177.18"), status="COMPLETE",
        timestamp=datetime.now(timezone.utc), account_id=MASTER_ID, order_type="MARKET", price=Decimal("0"),
    )
    await on_order_update(trigger_event)

    assert [r[0] for r in results] == ["SL_PENDING_CREATED", "ORDER_FILLED"]
    assert results[1][1]["executions"][str(follower.id)] == "MODIFIED: WORKING_MARKETABLE_LIMIT"
    client.place_order.assert_awaited_once()   # still just once - no duplicate/second child
    client.cancel_order.assert_not_called()
    client.modify_order.assert_awaited_once()
    kwargs = client.modify_order.call_args.kwargs
    assert kwargs["broker_order_id"] == "BRK_SL_TATA_TRIG"
    assert kwargs["order_type"] == OrderType.LIMIT
    assert kwargs["trigger_price"] == Decimal("0")
    assert kwargs["price"] == target

    rows = await _order_rows(db_session, master_broker_order_id="26100100168532")
    assert len(rows) == 1   # exactly one child - no orphan, no duplicate
    assert rows[0].order_type == "LIMIT"
    assert rows[0].status == OrderStatus.OPEN
    assert rows[0].trigger_price == Decimal("0")


@pytest.mark.asyncio
async def test_sl_trigger_as_limit_does_not_create_a_second_child_or_misfire_reprice(db_session, mock_redis, sample_accounts, sample_strategy):
    """SL triggers and fills as LIMIT at its own price (the confirmed
    26092900012070 pattern). Must NOT hit the instant-fill-LIMIT branch
    (which would create a duplicate child); the SAME pending SL child is
    modified in place by the SL-trigger execution path - one modify, never
    a second order or a cancel."""
    from app.services.copy_engine.pipeline import compute_marketable_limit_price

    follower = sample_accounts["follower1"]
    client = _sl_follower_client("BRK_SL_TRIG", positions=_covering_long())
    detector = MasterTradeDetector(master_account_id=MASTER_ID)
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    results = []
    on_order_update = _wire_engine(pipeline, detector, {str(follower.id): client}, results)

    await on_order_update(_pending_event(
        broker_order_id="26092900012070", order_type="SL",
        price=Decimal("263.60"), trigger_price=Decimal("263.50"), side="SELL",
    ))
    client.place_order.assert_awaited_once()

    target = compute_marketable_limit_price("SELL", Decimal("263.70"))
    _trigger_lifecycle_book(client, "BRK_SL_TRIG", "SL", Decimal("263.60"), Decimal("263.50"), target, BrokerOrderSide.SELL)

    from app.services.alice_blue.types import OrderUpdateEvent
    trigger_event = OrderUpdateEvent(
        broker_order_id="26092900012070", client_order_id=None, symbol="ITC", side="SELL",
        quantity=1, filled_quantity=1, fill_price=Decimal("263.70"), status="COMPLETE",
        timestamp=datetime.now(timezone.utc), account_id=MASTER_ID, order_type="LIMIT", price=Decimal("263.60"),
    )
    await on_order_update(trigger_event)

    assert [r[0] for r in results] == ["SL_PENDING_CREATED", "ORDER_FILLED"]
    assert results[1][1]["executions"][str(follower.id)] == "MODIFIED: WORKING_MARKETABLE_LIMIT"
    client.place_order.assert_awaited_once()   # NOT a duplicate LIMIT_CREATED child
    client.cancel_order.assert_not_called()
    client.modify_order.assert_awaited_once()   # exactly one in-place modify

    rows = await _order_rows(db_session, master_broker_order_id="26092900012070")
    assert len(rows) == 1
    assert rows[0].broker_order_id == "BRK_SL_TRIG"
    assert rows[0].order_type == "LIMIT"


@pytest.mark.asyncio
async def test_sl_trigger_does_not_create_a_child_when_all_placements_failed(db_session, mock_redis, sample_accounts):
    """If every follower placement failed (e.g. no funds), the detector
    must NOT have been told the order is "known" - the master's eventual
    trigger should still be free to be handled by the ordinary MARKET/
    LIMIT paths rather than silently vanishing as a false "already known,
    no-op". This proves mark_sl_child_created is conditional, not eager."""
    follower = sample_accounts["follower1"]
    client = AsyncMock()
    client.get_funds.side_effect = Exception("network down")
    detector = MasterTradeDetector(master_account_id=MASTER_ID)
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    results = []
    on_order_update = _wire_engine(pipeline, detector, {str(follower.id): client}, results)

    await on_order_update(_pending_event(
        broker_order_id="ORD_SL_ALLFAIL", order_type="SLM", price=Decimal("0"),
        trigger_price=Decimal("264.00"), side="BUY",
    ))
    assert results[0][0] == "SL_PENDING_CREATED"
    client.place_order.assert_not_called()   # funds fetch failed closed - no placement attempted
    assert "ORD_SL_ALLFAIL" not in detector._known_order_ids


# ---------------------------------------------------------------------------
# 11. Master cancels pending SL -> cancels the real follower child
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_master_cancel_while_pending_cancels_the_real_follower_child(db_session, mock_redis, sample_accounts, sample_strategy):
    follower = sample_accounts["follower1"]
    client = _sl_follower_client("BRK_SL_CANCEL", positions=_covering_long())
    detector = MasterTradeDetector(master_account_id=MASTER_ID)
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    results = []
    on_order_update = _wire_engine(pipeline, detector, {str(follower.id): client}, results)

    await on_order_update(_pending_event(
        broker_order_id="ORD_SL_CANCEL_1", order_type="SL",
        price=Decimal("391.50"), trigger_price=Decimal("391.90"), side="SELL",
    ))
    client.place_order.assert_awaited_once()

    from app.services.alice_blue.types import OrderUpdateEvent
    cancel_event = OrderUpdateEvent(
        broker_order_id="ORD_SL_CANCEL_1", client_order_id=None, symbol="ITC", side="SELL",
        quantity=1, filled_quantity=0, fill_price=Decimal("0"), status="CANCELLED",
        timestamp=datetime.now(timezone.utc), account_id=MASTER_ID, order_type="SL", price=Decimal("391.50"),
    )
    await on_order_update(cancel_event)

    assert [r[0] for r in results] == ["SL_PENDING_CREATED", "ORDER_CANCELLED"]
    client.cancel_order.assert_awaited_once()
    _, kwargs = client.cancel_order.call_args
    assert kwargs["broker_order_id"] == "BRK_SL_CANCEL"

    rows = await _order_rows(db_session, master_broker_order_id="ORD_SL_CANCEL_1", source=OrderSource.SYSTEM)
    assert len(rows) == 1
    assert rows[0].status == OrderStatus.CANCELLED


@pytest.mark.asyncio
async def test_master_cancel_never_touches_an_already_filled_child(db_session, mock_redis, sample_accounts, sample_strategy):
    """If the follower's own SL already triggered and filled independently
    before the master's cancel is observed, that fill must never be
    reversed or touched."""
    follower = sample_accounts["follower1"]
    client = _sl_follower_client("BRK_SL_FILLED", positions=_covering_long())
    detector = MasterTradeDetector(master_account_id=MASTER_ID)
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    results = []
    on_order_update = _wire_engine(pipeline, detector, {str(follower.id): client}, results)

    await on_order_update(_pending_event(
        broker_order_id="ORD_SL_FILLED_1", order_type="SL",
        price=Decimal("391.50"), trigger_price=Decimal("391.90"), side="SELL",
    ))
    child = (await _order_rows(db_session, master_broker_order_id="ORD_SL_FILLED_1"))[0]
    # Simulate the follower's own SL having independently triggered and
    # filled (as FollowerFillSyncer would record it) before the master's
    # cancel is observed.
    await pipeline.save_or_update_order(
        internal_order_id=child.internal_order_id, broker_order_id=child.broker_order_id,
        account_id=child.account_id, source=OrderSource.SYSTEM, exchange="NSE", symbol="ITC",
        side=OrderSide.SELL, order_type="SL", product_type="INTRADAY", quantity=1,
        filled_quantity=1, pending_quantity=0, price=Decimal("391.50"), trigger_price=Decimal("391.90"),
        status=OrderStatus.COMPLETE, master_broker_order_id="ORD_SL_FILLED_1",
    )

    from app.services.alice_blue.types import OrderUpdateEvent
    cancel_event = OrderUpdateEvent(
        broker_order_id="ORD_SL_FILLED_1", client_order_id=None, symbol="ITC", side="SELL",
        quantity=1, filled_quantity=0, fill_price=Decimal("0"), status="CANCELLED",
        timestamp=datetime.now(timezone.utc), account_id=MASTER_ID, order_type="SL", price=Decimal("391.50"),
    )
    await on_order_update(cancel_event)

    client.cancel_order.assert_not_called()   # never touches the already-filled child
    db_session.expire_all()  # save_or_update_order committed via its own session
    row = (await _order_rows(db_session, master_broker_order_id="ORD_SL_FILLED_1", source=OrderSource.SYSTEM))[0]
    assert row.status == OrderStatus.COMPLETE
    assert row.filled_quantity == 1


# ---------------------------------------------------------------------------
# 12. Restart recovery does not duplicate the child
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restart_recovery_seeds_pending_state_and_known_child(db_session, mock_redis, sample_accounts):
    """A pending SL that already has a durable follower child before
    restart must, after seeding, both (a) recognize a repeated identical
    poll as unchanged, and (b) recognize its eventual trigger as the SAME
    order - never a duplicate child."""
    follower = sample_accounts["follower1"]
    master_broker_order_id = "ORD_SL_RESTART_1"

    # Durable state exactly as it would be on disk pre-restart: master row
    # still PENDING, child already created and PENDING too.
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await pipeline.save_or_update_order(
        internal_order_id=f"EXT_{master_broker_order_id}_{MASTER_ID[:8]}", broker_order_id=master_broker_order_id,
        account_id=uuid.UUID(MASTER_ID), source=OrderSource.BROKER_EXTERNAL, exchange="NSE", symbol="ITC",
        side=OrderSide.SELL, order_type="SL", product_type="INTRADAY", quantity=1, filled_quantity=0,
        price=Decimal("391.50"), trigger_price=Decimal("391.90"), status=OrderStatus.PENDING,
    )
    await pipeline.save_or_update_order(
        internal_order_id="JT_CH_SL_ORD_SL_RESTART_1_restart", broker_order_id="BRK_SL_RESTART",
        account_id=follower.id, source=OrderSource.SYSTEM, exchange="NSE", symbol="ITC",
        side=OrderSide.SELL, order_type="SL", product_type="INTRADAY", quantity=1, filled_quantity=0,
        price=Decimal("391.50"), trigger_price=Decimal("391.90"), status=OrderStatus.PENDING,
        master_broker_order_id=master_broker_order_id,
    )

    # --- Simulate restart: fresh detector, seeded exactly as engine_loop.py does. ---
    detector = MasterTradeDetector(master_account_id=MASTER_ID)
    seed_rows = await _order_rows(db_session)
    seed_master_rows = [o for o in seed_rows if o.source != OrderSource.SYSTEM]
    seed_broker_order_ids = [o.broker_order_id for o in seed_master_rows if o.broker_order_id]
    child_rows = (await db_session.execute(
        select(Order.master_broker_order_id).where(
            Order.source == OrderSource.SYSTEM, Order.master_broker_order_id.in_(seed_broker_order_ids)
        ).distinct()
    )).all()
    known_child_master_order_ids = {row[0] for row in child_rows if row[0]}
    assert known_child_master_order_ids == {master_broker_order_id}

    detector.seed_state(seed_master_rows, known_child_master_order_ids=known_child_master_order_ids)
    assert master_broker_order_id in detector._known_order_ids
    assert master_broker_order_id in detector._sl_pending_state

    client = _sl_follower_client("BRK_SL_RESTART", positions=_covering_long())
    results = []
    on_order_update = _wire_engine(pipeline, detector, {str(follower.id): client}, results)

    # Post-restart: an unchanged pending poll must be a no-op (correctly
    # recognized via seeded _sl_pending_state, not treated as brand new).
    await on_order_update(_pending_event(
        broker_order_id=master_broker_order_id, order_type="SL",
        price=Decimal("391.50"), trigger_price=Decimal("391.90"), side="SELL",
    ))
    assert results == []
    client.place_order.assert_not_called()

    # Post-restart: the master fills. Must be recognized as the SAME
    # order (ORDER_FILLED via seeded _known_order_ids), never a new child.
    from app.services.alice_blue.types import OrderUpdateEvent
    fill_event = OrderUpdateEvent(
        broker_order_id=master_broker_order_id, client_order_id=None, symbol="ITC", side="SELL",
        quantity=1, filled_quantity=1, fill_price=Decimal("391.90"), status="COMPLETE",
        timestamp=datetime.now(timezone.utc), account_id=MASTER_ID, order_type="SL", price=Decimal("391.50"),
    )
    await on_order_update(fill_event)

    assert [r[0] for r in results] == ["ORDER_FILLED"]
    client.place_order.assert_not_called()
    rows = await _order_rows(db_session, master_broker_order_id=master_broker_order_id, source=OrderSource.SYSTEM)
    assert len(rows) == 1   # still exactly one child after restart + trigger


# ---------------------------------------------------------------------------
# 13. Multiple followers each get their own independent child
# ---------------------------------------------------------------------------

async def _make_follower(db_session, name):
    acc = Account(
        id=uuid.uuid4(), account_type=AccountType.FOLLOWER, display_name=name,
        alice_user_id=f"AB_{uuid.uuid4().hex[:8]}", is_active=True,
    )
    db_session.add(acc)
    await db_session.commit()
    await db_session.refresh(acc)
    return acc


@pytest.mark.asyncio
async def test_multiple_followers_each_get_their_own_independent_sl_child(db_session, mock_redis, sample_accounts, sample_strategy):
    follower1 = sample_accounts["follower1"]
    follower2 = await _make_follower(db_session, "Follower 2")
    await add_child(db_session, sample_strategy, follower2)

    client1 = _sl_follower_client("BRK_SL_F1", positions=_covering_long())
    client2 = _sl_follower_client("BRK_SL_F2", positions=_covering_long())
    detector = MasterTradeDetector(master_account_id=MASTER_ID)
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    results = []
    on_order_update = _wire_engine(
        pipeline, detector, {str(follower1.id): client1, str(follower2.id): client2}, results,
    )

    await on_order_update(_pending_event(
        broker_order_id="ORD_SL_MULTI_1", order_type="SL",
        price=Decimal("391.50"), trigger_price=Decimal("391.90"), side="SELL",
    ))

    assert results[0][1]["status"] == "completed"
    client1.place_order.assert_awaited_once()
    client2.place_order.assert_awaited_once()

    # Assert against the in-memory per-follower results, not a DB re-query:
    # the two followers' DB persistence genuinely races (asyncio.gather
    # against the test suite's single shared in-memory SQLite connection -
    # a test-infrastructure artifact; production runs against real
    # PostgreSQL with proper per-connection isolation). The result dict is
    # built directly from each follower's own broker call in memory and is
    # unaffected by that race - save_or_update_order's own DB_PERSISTENCE_
    # FAILED fallback (mirrored from _process_single_limit_follower) is
    # exactly what exists to keep a genuinely-placed order from ever being
    # lost if this happens for real.
    child_results = results[0][1]["results"]
    assert child_results[str(follower1.id)]["status"] == "SUCCESS"
    assert child_results[str(follower2.id)]["status"] == "SUCCESS"
    assert child_results[str(follower1.id)]["broker_order_id"] == "BRK_SL_F1"
    assert child_results[str(follower2.id)]["broker_order_id"] == "BRK_SL_F2"

    rows = await _order_rows(db_session, master_broker_order_id="ORD_SL_MULTI_1", source=OrderSource.SYSTEM)
    assert len(rows) == 2
    assert {str(r.account_id) for r in rows} == {str(follower1.id), str(follower2.id)}


@pytest.mark.asyncio
async def test_sell_position_safety_blocks_one_follower_without_covering_long(db_session, mock_redis, sample_accounts):
    """A pending SL SELL is gated by the existing follower SELL
    position-safety rule at placement time, exactly like LIMIT/MARKET -
    reused unchanged, never bypassed for the SL path specifically."""
    follower = sample_accounts["follower1"]
    client = _sl_follower_client("BRK_SL_NOPOS", positions=[])  # no covering long
    detector = MasterTradeDetector(master_account_id=MASTER_ID)
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    results = []
    on_order_update = _wire_engine(pipeline, detector, {str(follower.id): client}, results)

    original_bypass = app_settings.SELL_POSITION_SAFETY_BYPASS
    app_settings.SELL_POSITION_SAFETY_BYPASS = False
    try:
        await on_order_update(_pending_event(
            broker_order_id="ORD_SL_NOPOS_1", order_type="SL",
            price=Decimal("391.50"), trigger_price=Decimal("391.90"), side="SELL",
        ))
    finally:
        app_settings.SELL_POSITION_SAFETY_BYPASS = original_bypass

    client.place_order.assert_not_called()
    rows = await _order_rows(db_session, master_broker_order_id="ORD_SL_NOPOS_1", source=OrderSource.SYSTEM)
    assert len(rows) == 0   # no child ever written - blocked before the pre-write


# ---------------------------------------------------------------------------
# Native SL support at the broker-client level (MasterOrderPoller ->
# eligibility, and the same status flowing all the way into place_order's
# outgoing payload key).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_master_order_poller_dispatches_trigger_pending_instead_of_blocking_it(db_session, mock_redis):
    """Confirms the poller-level gate change directly: TRIGGER_PENDING is
    no longer fail-closed - it reaches on_order_update like any other
    understood status."""
    client = MagicMock()
    client.get_order_book = AsyncMock(return_value=[])
    dispatched = []
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    poller = MasterOrderPoller(
        master_account_id=MASTER_ID, client_provider=lambda _acc_id: client,
        on_order_update=lambda e: dispatched.append(e), pipeline=pipeline,
    )
    await poller.poll_once()  # empty baseline

    client.get_order_book = AsyncMock(return_value=[_pending("ORD_SL_POLLER_1", "SL")])
    count = await poller.poll_once()

    assert count == 1
    assert len(dispatched) == 1
    assert dispatched[0].status == "PENDING"
    assert dispatched[0].order_type == "SL"
