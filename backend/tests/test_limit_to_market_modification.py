"""
Production fix coverage: a master LIMIT order changed to MARKET (via the
Alice Blue app) while still pending must be detected as a modification of
the SAME order - never as a brand-new order - so the copy engine never
creates a duplicate child, and attempts (fails closed, never blindly
replaces) to propagate the order_type change to the existing child.

Root cause this guards against: MasterTradeDetector used to determine an
order's identity ("have I already seen this broker_order_id") partly by
requiring its CURRENT order_type == "LIMIT". A LIMIT order converted to
MARKET stopped satisfying that check, so its eventual fill was
misclassified as MARKET_ACCEPTED (a brand-new order) instead of
ORDER_FILLED (a fill of an order already replicated) - the copy engine then
placed a second, duplicate child order while the original LIMIT child was
never touched or cancelled.

No live trading is enabled and no real broker calls are made anywhere in
this file - all broker clients are AsyncMock test doubles and the DB is the
in-memory test SQLite engine (db_session fixture). Mirrors the existing
test_master_modification.py fixture style.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.accounts import Account, AccountType
from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.types import FundsData, OrderResult, OrderResultCategory, OrderType, OrderUpdateEvent
from app.services.copy_engine.detector import MasterTradeDetector
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator


def _limit_open_event(broker_order_id="ORD_L2M_1", quantity=100, price=Decimal("2950.00"), account_id="M1", filled_quantity=0):
    return OrderUpdateEvent(
        broker_order_id=broker_order_id,
        client_order_id=None,
        symbol="RELIANCE",
        side="BUY",
        quantity=quantity,
        filled_quantity=filled_quantity,
        fill_price=Decimal("0"),
        status="OPEN",
        timestamp=datetime.now(timezone.utc),
        account_id=account_id,
        order_type="LIMIT",
        price=price,
    )


def _market_open_event(broker_order_id="ORD_L2M_1", quantity=100, account_id="M1", filled_quantity=0, price=Decimal("0")):
    """Same order, reported by the broker as OPEN but now order_type=MARKET -
    the exact state Alice Blue's app produces right after converting a
    still-pending LIMIT order to MARKET, before it fills."""
    return OrderUpdateEvent(
        broker_order_id=broker_order_id,
        client_order_id=None,
        symbol="RELIANCE",
        side="BUY",
        quantity=quantity,
        filled_quantity=filled_quantity,
        fill_price=Decimal("0"),
        status="OPEN",
        timestamp=datetime.now(timezone.utc),
        account_id=account_id,
        order_type="MARKET",
        price=price,
    )


def _market_complete_event(broker_order_id="ORD_L2M_1", quantity=100, filled_quantity=100, fill_price=Decimal("2955.00"), account_id="M1"):
    """The order fills, now reported with order_type=MARKET (post-conversion)."""
    return OrderUpdateEvent(
        broker_order_id=broker_order_id,
        client_order_id=None,
        symbol="RELIANCE",
        side="BUY",
        quantity=quantity,
        filled_quantity=filled_quantity,
        fill_price=fill_price,
        status="COMPLETE",
        timestamp=datetime.now(timezone.utc),
        account_id=account_id,
        order_type="MARKET",
        price=Decimal("0"),
    )


# ---------------------------------------------------------------------------
# Detector-level: LIMIT -> MARKET is a modification, never a new order
# ---------------------------------------------------------------------------

def test_limit_to_market_conversion_while_open_is_detected_as_modified():
    """The order-type-change branch (1b) must fire: still OPEN, order_type
    now MARKET, broker_order_id already known from LIMIT_CREATED."""
    detector = MasterTradeDetector(master_account_id="M1")

    created = detector.process_order_update(_limit_open_event())
    assert created is not None
    assert created.event_type == "LIMIT_CREATED"

    converted = detector.process_order_update(_market_open_event())
    assert converted is not None
    assert converted.event_type == "ORDER_MODIFIED"
    assert converted.order_type == "MARKET"
    assert converted.master_order_id == "ORD_L2M_1"


def test_repeated_market_conversion_update_is_idempotent():
    """The exact same converted-to-MARKET OPEN update resent must be
    ignored, not re-fired as a second modification (requirement: repeated
    events must not create multiple children)."""
    detector = MasterTradeDetector(master_account_id="M1")
    detector.process_order_update(_limit_open_event())

    first = detector.process_order_update(_market_open_event())
    assert first is not None

    duplicate = detector.process_order_update(_market_open_event())
    assert duplicate is None


def test_market_conversion_then_fill_is_order_filled_not_a_new_order():
    """THE core regression: once converted to MARKET and then filled, the
    event must classify as ORDER_FILLED (a fill of the already-known order),
    never MARKET_ACCEPTED (which the pipeline replicates as a brand-new
    child - the production duplicate-order bug)."""
    detector = MasterTradeDetector(master_account_id="M1")
    detector.process_order_update(_limit_open_event())
    detector.process_order_update(_market_open_event())

    filled = detector.process_order_update(_market_complete_event())
    assert filled is not None
    assert filled.event_type == "ORDER_FILLED"


def test_market_conversion_skipping_the_open_step_still_fills_correctly():
    """Even if the poll interval misses the brief OPEN/MARKET window (the
    order converts and fills between two polls), the fill itself must still
    be recognized as belonging to the already-known LIMIT_CREATED order,
    not misclassified as a new MARKET order."""
    detector = MasterTradeDetector(master_account_id="M1")
    detector.process_order_update(_limit_open_event())

    filled = detector.process_order_update(_market_complete_event())
    assert filled is not None
    assert filled.event_type == "ORDER_FILLED"


def test_pure_market_order_fill_is_still_market_accepted():
    """Regression guard: a genuinely new MARKET order (never seen as LIMIT)
    that fills must be completely unaffected by this fix - still
    MARKET_ACCEPTED, still replicated as a new child. Preserves existing
    MARKET order behavior exactly."""
    detector = MasterTradeDetector(master_account_id="M1")

    filled = detector.process_order_update(
        _market_complete_event(broker_order_id="ORD_PURE_MKT_1", filled_quantity=100)
    )
    assert filled is not None
    assert filled.event_type == "MARKET_ACCEPTED"


def test_limit_price_modification_is_unaffected_by_order_type_tracking():
    """Regression guard: an ordinary LIMIT price modification (order_type
    unchanged throughout) must still be detected exactly as before."""
    detector = MasterTradeDetector(master_account_id="M1")
    detector.process_order_update(_limit_open_event(price=Decimal("2950.00")))

    modified = detector.process_order_update(_limit_open_event(price=Decimal("2999.00")))
    assert modified is not None
    assert modified.event_type == "ORDER_MODIFIED"
    assert modified.order_type == "LIMIT"
    assert modified.limit_price == Decimal("2999.00")

    # And it must still fill as ORDER_FILLED, not a new order.
    filled = detector.process_order_update(
        OrderUpdateEvent(
            broker_order_id="ORD_L2M_1", client_order_id=None, symbol="RELIANCE", side="BUY",
            quantity=100, filled_quantity=100, fill_price=Decimal("2999.00"), status="COMPLETE",
            timestamp=datetime.now(timezone.utc), account_id="M1", order_type="LIMIT", price=Decimal("0"),
        )
    )
    assert filled is not None
    assert filled.event_type == "ORDER_FILLED"


# ---------------------------------------------------------------------------
# Pipeline-level: order_type propagation, no duplicate child, fail-closed
# ---------------------------------------------------------------------------

async def _make_child(db_session, master_broker_order_id="ORD_L2M_1", status=OrderStatus.OPEN,
                       filled_quantity=0, quantity=100, order_type="LIMIT", broker_order_id="BRK_CH_L2M_1",
                       side=OrderSide.BUY):
    child_acc = Account(
        id=uuid.uuid4(), account_type=AccountType.FOLLOWER, display_name="Child Vikash",
        alice_user_id=f"AB_{uuid.uuid4().hex[:8]}", is_active=True,
    )
    db_session.add(child_acc)
    await db_session.commit()

    child_order = Order(
        id=uuid.uuid4(),
        internal_order_id=f"JT_CH_{uuid.uuid4().hex[:12]}",
        broker_order_id=broker_order_id,
        account_id=child_acc.id,
        master_broker_order_id=master_broker_order_id,
        source=OrderSource.SYSTEM,
        exchange="NSE",
        symbol="RELIANCE",
        side=side,
        order_type=order_type,
        product_type="MIS",
        quantity=quantity,
        filled_quantity=filled_quantity,
        pending_quantity=quantity - filled_quantity,
        price=Decimal("2950.00"),
        status=status,
    )
    db_session.add(child_order)
    await db_session.commit()
    return child_acc, child_order


@pytest.mark.asyncio
async def test_order_type_change_while_still_open_defers_until_master_fill(db_session, mock_redis):
    """A master seen as MARKET while STILL OPEN has not executed yet, so
    there is no confirmed price to build a marketable LIMIT from (Alice Blue
    reports price 0.0 for a MARKET order). The child must be left completely
    untouched and the conversion deferred to the fill event - repricing off
    that 0 would destroy the child's working price."""
    child_acc, child_order = await _make_child(db_session)

    child_client = AsyncMock()
    clients = {str(child_acc.id): child_client}

    detector = MasterTradeDetector(master_account_id="M1")
    detector.process_order_update(_limit_open_event())
    converted_event = detector.process_order_update(_market_open_event())
    assert converted_event.event_type == "ORDER_MODIFIED"

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=converted_event, clients=clients)

    assert res["status"] == "deferred_pending_master_fill"
    child_client.modify_order.assert_not_called()
    child_client.place_order.assert_not_called()

    # Child untouched: still LIMIT, still its original working price.
    await db_session.refresh(child_order)
    assert child_order.order_type == "LIMIT"
    assert child_order.price == Decimal("2950.00")

    rows = (
        await db_session.execute(select(Order).where(Order.master_broker_order_id == "ORD_L2M_1"))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == child_order.id


@pytest.mark.asyncio
async def test_order_type_change_never_creates_a_second_child_order(db_session, mock_redis):
    """End-to-end regression for the exact production symptom: detect the
    conversion, replicate it, then simulate the order's fill reaching the
    detector again - the fill must be ORDER_FILLED (a no-op for the
    pipeline), never routed through market replication, which is what used
    to place the duplicate second child."""
    child_acc, child_order = await _make_child(db_session)
    child_client = AsyncMock()
    child_client.modify_order.return_value = OrderResult(
        category=OrderResultCategory.SUCCESS, client_order_id=None, broker_order_id="BRK_CH_L2M_1",
        message="Modified successfully", raw_response={"stat": "Ok"}, timestamp=datetime.now(timezone.utc),
    )
    clients = {str(child_acc.id): child_client}

    detector = MasterTradeDetector(master_account_id="M1")
    detector.process_order_update(_limit_open_event())
    converted_event = detector.process_order_update(_market_open_event())

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await pipeline.execute_replication(event=converted_event, clients=clients)

    # The order now fills. This must NOT go down _execute_market_replication.
    fill_event = detector.process_order_update(_market_complete_event())
    assert fill_event.event_type == "ORDER_FILLED"

    fill_res = await pipeline.execute_replication(event=fill_event, clients=clients)
    assert fill_res["status"] == "completed"

    # The EXISTING child was repriced - never replaced.
    child_client.modify_order.assert_awaited_once()
    assert child_client.place_order.call_count == 0

    # Still exactly one child order for this master order - no duplicate.
    rows = (
        await db_session.execute(select(Order).where(Order.master_broker_order_id == "ORD_L2M_1"))
    ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_broker_rejection_of_order_type_change_marks_reconciliation_not_replace(db_session, mock_redis):
    """If Alice Blue's modify endpoint refuses the order_type change (broker
    support for LIMIT->MARKET via modify is NOT confirmed in this repo - see
    handle_master_modification's docstring), the existing
    _record_failed_child_modification path must fire: CopyTrade flips to
    RECONCILIATION_REQUIRED, and - critically - NO replacement order is
    created and the child's own Order row is left untouched."""
    child_acc, child_order = await _make_child(db_session)

    copy_trade = CopyTrade(
        id=uuid.uuid4(),
        master_order_id="ORD_L2M_1",
        follower_account_id=child_acc.id,
        copy_event_id=uuid.uuid4(),
        symbol="RELIANCE",
        exchange="NSE",
        side=OrderSide.BUY,
        quantity=100,
        product_type="MIS",
        estimated_price=Decimal("2950.00"),
        client_order_id="JT__L2M_CT_1",
        broker_order_id="BRK_CH_L2M_1",
        status=CopyTradeStatus.SUCCESS,
    )
    db_session.add(copy_trade)
    await db_session.commit()

    child_client = AsyncMock()
    child_client.modify_order.return_value = OrderResult(
        category=OrderResultCategory.REJECTED, client_order_id=None, broker_order_id=None,
        message="Broker does not support changing orderType via modify",
        raw_response={}, timestamp=datetime.now(timezone.utc),
    )
    clients = {str(child_acc.id): child_client}

    detector = MasterTradeDetector(master_account_id="M1")
    detector.process_order_update(_limit_open_event())
    fill_event = detector.process_order_update(_market_complete_event())

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=fill_event, clients=clients)

    assert res["status"] == "completed_with_failures"
    assert str(child_acc.id) in res["failed_accounts"]

    await db_session.refresh(copy_trade)
    assert copy_trade.status == CopyTradeStatus.RECONCILIATION_REQUIRED

    # The child's own Order row must be untouched (still LIMIT, unchanged
    # price/quantity) - no blind replace, no cancel.
    await db_session.refresh(child_order)
    assert child_order.order_type == "LIMIT"
    assert child_order.status == OrderStatus.OPEN

    # No new/replacement child order was created.
    rows = (
        await db_session.execute(select(Order).where(Order.master_broker_order_id == "ORD_L2M_1"))
    ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_already_filled_child_is_never_touched_by_the_reprice(db_session, mock_redis):
    """A child that has already COMPLETEd must never be modified or
    replaced - the conversion is moot once the child has filled, so this is
    a clean no-op with no broker call of any kind."""
    child_acc, child_order = await _make_child(db_session, status=OrderStatus.COMPLETE, filled_quantity=100)

    child_client = AsyncMock()
    clients = {str(child_acc.id): child_client}

    detector = MasterTradeDetector(master_account_id="M1")
    detector.process_order_update(_limit_open_event())
    fill_event = detector.process_order_update(_market_complete_event())

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=fill_event, clients=clients)

    assert res == {"status": "master_fill_updated"}
    child_client.modify_order.assert_not_called()
    child_client.place_order.assert_not_called()

    await db_session.refresh(child_order)
    assert child_order.status == OrderStatus.COMPLETE
    assert child_order.filled_quantity == 100
    assert child_order.order_type == "LIMIT"


@pytest.mark.asyncio
async def test_partially_filled_child_reprice_uses_remaining_quantity_only(db_session, mock_redis):
    """A PARTIALLY filled child (40/100) must be repriced for the REMAINING
    pending quantity only - filled_quantity must never be touched or
    duplicated."""
    child_acc, child_order = await _make_child(
        db_session, status=OrderStatus.PARTIAL, filled_quantity=40, quantity=100
    )

    child_client = AsyncMock()
    child_client.modify_order.return_value = OrderResult(
        category=OrderResultCategory.SUCCESS, client_order_id=None, broker_order_id="BRK_CH_L2M_1",
        message="Modified successfully", raw_response={"stat": "Ok"}, timestamp=datetime.now(timezone.utc),
    )
    clients = {str(child_acc.id): child_client}

    detector = MasterTradeDetector(master_account_id="M1")
    detector.process_order_update(_limit_open_event(quantity=100))
    fill_event = detector.process_order_update(
        _market_complete_event(quantity=100, filled_quantity=100, fill_price=Decimal("2955.00"))
    )

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=fill_event, clients=clients)

    assert res["status"] == "completed"
    child_client.modify_order.assert_awaited_once()
    _, call_kwargs = child_client.modify_order.call_args
    # Remaining pending quantity (100 - 40 already filled), never the full
    # master quantity and never the already-filled amount.
    assert call_kwargs["quantity"] == 60
    assert call_kwargs["order_type"] == OrderType.LIMIT  # never MARKET - EC965

    await db_session.refresh(child_order)
    assert child_order.filled_quantity == 40  # untouched
    assert child_order.pending_quantity == 60
    assert child_order.order_type == "LIMIT"
    assert child_order.price == Decimal("2955.00")  # repriced to the master's fill


# ---------------------------------------------------------------------------
# Restart safety: _known_order_ids must survive a process restart even when
# the master's own Order row's order_type has already been converted from
# LIMIT to MARKET but the order has not yet filled. Fixed by seed_state's
# known_child_master_order_ids parameter - see its docstring and
# engine_loop.py's seed-time query, both of which these tests exercise via
# the same query shape (rather than re-deriving detector.py-internal logic).
# ---------------------------------------------------------------------------

async def _known_child_master_order_ids(db_session, master_broker_order_ids):
    """Mirrors engine_loop.py's own seed-time child-lookup query exactly."""
    rows = (
        await db_session.execute(
            select(Order.master_broker_order_id)
            .where(
                Order.source == OrderSource.SYSTEM,
                Order.master_broker_order_id.in_(master_broker_order_ids),
            )
            .distinct()
        )
    ).all()
    return {row[0] for row in rows if row[0]}


def _restart_client():
    client = AsyncMock()
    client.get_funds.return_value = FundsData(
        account_id="f", available_margin=Decimal("500000"), collateral=Decimal("0"),
        used_margin=Decimal("0"), updated_at=datetime.now(timezone.utc),
    )
    client.get_positions.return_value = []
    client.place_order.return_value = OrderResult(
        category=OrderResultCategory.SUCCESS, client_order_id="c", broker_order_id="BRK_NEW_CHILD",
        message="ok", raw_response={"stat": "Ok"}, timestamp=datetime.now(timezone.utc),
    )
    client.modify_order.return_value = OrderResult(
        category=OrderResultCategory.SUCCESS, client_order_id=None, broker_order_id="BRK_CH_RESTART_1",
        message="Modified successfully", raw_response={"stat": "Ok"}, timestamp=datetime.now(timezone.utc),
    )
    return client


@pytest.mark.asyncio
async def test_restart_after_limit_to_market_conversion_before_fill_stays_same_order(db_session, mock_redis, sample_accounts):
    """THE exact scenario from the follow-up report:
    1. Master LIMIT order exists, child LIMIT order exists.
    2. Master converts LIMIT -> MARKET (the child's own Order row is updated
       to order_type=MARKET too, per the earlier fix - see
       test_order_type_change_propagates_to_child_modify_order_and_updates_db).
    3. Jettone restarts BEFORE the master MARKET order fills - a fresh
       MasterTradeDetector is seeded from Order rows whose order_type is now
       MARKET, not LIMIT.
    4. Master order later fills.
    The fill must still be classified as ORDER_FILLED for the SAME existing
    child - never MARKET_ACCEPTED, never a second child order."""
    master = sample_accounts["master"]
    master_id = str(master.id)
    master_broker_order_id = "ORD_RESTART_L2M"

    # Durable state exactly as it would be on disk after step 2 above: the
    # child order already exists and is still a working LIMIT (a child is
    # never converted to MARKET - Alice Blue rejects that with EC965 - so
    # the pre-restart state is an un-repriced LIMIT child).
    child_acc, child_order = await _make_child(
        db_session, master_broker_order_id=master_broker_order_id, order_type="LIMIT",
        status=OrderStatus.OPEN, quantity=100, filled_quantity=0, broker_order_id="BRK_CH_RESTART_1",
    )

    # --- Simulate restart: fresh detector, seeded exactly as engine_loop.py
    # does - the master Order row's CURRENT order_type is MARKET
    # (post-conversion), plus the durable known-child set computed from the
    # DB via the child's master_broker_order_id. ---
    detector = MasterTradeDetector(master_account_id=master_id)
    known_child_ids = await _known_child_master_order_ids(db_session, [master_broker_order_id])
    assert known_child_ids == {master_broker_order_id}  # sanity: the query itself finds it

    detector.seed_state(
        [
            SimpleNamespace(
                broker_order_id=master_broker_order_id, filled_quantity=0,
                order_type="MARKET", quantity=100, price=Decimal("0"),
            )
        ],
        known_child_master_order_ids=known_child_ids,
    )

    # --- Post-restart: master order fills ---
    fill_event = detector.process_order_update(
        _market_complete_event(broker_order_id=master_broker_order_id, quantity=100, filled_quantity=100, account_id=master_id)
    )
    assert fill_event is not None
    assert fill_event.event_type == "ORDER_FILLED"  # must NOT be MARKET_ACCEPTED

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    client = _restart_client()
    res = await pipeline.execute_replication(event=fill_event, clients={str(child_acc.id): client})

    # The surviving child is repriced in place - it must never route through
    # market replication / place_order, which would create a second child.
    assert res["status"] == "completed"
    client.place_order.assert_not_called()
    client.modify_order.assert_awaited_once()
    await db_session.refresh(child_order)
    assert child_order.order_type == "LIMIT"
    assert child_order.broker_order_id == "BRK_CH_RESTART_1"  # identity preserved across restart

    # Still exactly one child order for this master order - no duplicate.
    rows = (
        await db_session.execute(select(Order).where(Order.master_broker_order_id == master_broker_order_id))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == child_order.id


@pytest.mark.asyncio
async def test_restart_genuinely_new_market_order_still_replicates_normally(db_session, mock_redis, sample_accounts, sample_strategy):
    """Control case for the fix above: a master order that is genuinely
    brand-new after a restart (no prior LIMIT history, no existing child)
    must still be detected as MARKET_ACCEPTED and replicated as a new child
    - the known_child_master_order_ids set must be scoped per-order and must
    not swallow unrelated orders."""
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]
    master_id = str(master.id)

    # An unrelated child exists for a DIFFERENT master order, to prove the
    # known-child set doesn't leak across orders.
    await _make_child(
        db_session, master_broker_order_id="ORD_OTHER_L2M", order_type="MARKET",
        status=OrderStatus.OPEN, quantity=50, filled_quantity=0, broker_order_id="BRK_CH_OTHER",
    )

    detector = MasterTradeDetector(master_account_id=master_id)
    known_child_ids = await _known_child_master_order_ids(db_session, ["ORD_OTHER_L2M"])
    detector.seed_state(
        [
            SimpleNamespace(
                broker_order_id="ORD_OTHER_L2M", filled_quantity=0,
                order_type="MARKET", quantity=50, price=Decimal("0"),
            )
        ],
        known_child_master_order_ids=known_child_ids,
    )

    # A brand-new MARKET order, never seen before restart, never in the
    # seeded set at all.
    fill_event = detector.process_order_update(
        _market_complete_event(broker_order_id="ORD_BRAND_NEW_MKT", quantity=100, filled_quantity=100, account_id=master_id)
    )
    assert fill_event is not None
    assert fill_event.event_type == "MARKET_ACCEPTED"

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    client = _restart_client()
    res = await pipeline.execute_replication(event=fill_event, clients={str(follower.id): client})

    assert res["status"] == "completed"
    client.place_order.assert_awaited_once()
    assert client.place_order.call_args.args[0].quantity == 100


# ---------------------------------------------------------------------------
# Missed-intermediate-state reconciliation: the poller's interval misses the
# OPEN+MARKET window entirely (master converts LIMIT->MARKET AND fills
# between two polls), so no ORDER_MODIFIED is ever produced - only
# ORDER_FILLED. execute_replication's ORDER_FILLED branch must reconcile the
# existing stale-LIMIT child to MARKET via handle_master_modification, never
# via _execute_market_replication (which would create a second child). THE
# confirmed production incident: master 26092200137366 CANBK BUY.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missed_intermediate_state_order_filled_reconciles_existing_child_to_market(db_session, mock_redis):
    """The exact production incident: master LIMIT -> child LIMIT created,
    then the master converts to MARKET and fills before the next poll (the
    OPEN+MARKET intermediate state is never observed). The detector
    correctly classifies the fill as ORDER_FILLED (same order, no
    duplicate) - the pipeline must now REPRICE the EXISTING child to a
    marketable LIMIT rather than leaving it stranded at its stale price.
    The child stays LIMIT: Alice Blue rejects orderType=MARKET with
    EC965."""
    child_acc, child_order = await _make_child(
        db_session, master_broker_order_id="ORD_MISSED_1", order_type="LIMIT",
        status=OrderStatus.OPEN, quantity=100, filled_quantity=0, broker_order_id="BRK_CH_MISSED_1",
    )

    child_client = AsyncMock()
    child_client.modify_order.return_value = OrderResult(
        category=OrderResultCategory.SUCCESS, client_order_id=None, broker_order_id="BRK_CH_MISSED_1",
        message="Modified successfully", raw_response={"stat": "Ok"}, timestamp=datetime.now(timezone.utc),
    )
    clients = {str(child_acc.id): child_client}

    detector = MasterTradeDetector(master_account_id="M1")
    detector.process_order_update(_limit_open_event(broker_order_id="ORD_MISSED_1", quantity=100))

    # The intermediate OPEN+MARKET poll is skipped entirely - next
    # observation is already COMPLETE+MARKET.
    fill_event = detector.process_order_update(
        _market_complete_event(broker_order_id="ORD_MISSED_1", quantity=100, filled_quantity=100)
    )
    assert fill_event.event_type == "ORDER_FILLED"  # correctly correlated, not a new order

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=fill_event, clients=clients)

    assert res["status"] == "completed"
    assert res["modifications"][str(child_acc.id)] == "MODIFIED"

    child_client.modify_order.assert_awaited_once()
    _, call_kwargs = child_client.modify_order.call_args
    assert call_kwargs["broker_order_id"] == "BRK_CH_MISSED_1"  # same broker order, never replaced
    assert call_kwargs["order_type"] == OrderType.LIMIT          # never MARKET - EC965
    assert call_kwargs["price"] == Decimal("2955.00")            # the master's confirmed fill

    await db_session.refresh(child_order)
    assert child_order.order_type == "LIMIT"
    assert child_order.price == Decimal("2955.00")
    assert child_order.broker_order_id == "BRK_CH_MISSED_1"

    # Still exactly one child order for this master order - no duplicate.
    rows = (
        await db_session.execute(select(Order).where(Order.master_broker_order_id == "ORD_MISSED_1"))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == child_order.id


@pytest.mark.asyncio
async def test_missed_intermediate_state_broker_rejection_keeps_child_visible_no_replacement(db_session, mock_redis):
    """If the broker refuses the LIMIT->MARKET modify triggered by the
    missed-intermediate-state reconciliation path, the existing
    _record_failed_child_modification path must fire - CopyTrade flips to
    RECONCILIATION_REQUIRED, the child stays visible and untouched, and no
    replacement child is created."""
    child_acc, child_order = await _make_child(
        db_session, master_broker_order_id="ORD_MISSED_REJ_1", order_type="LIMIT",
        status=OrderStatus.OPEN, quantity=100, filled_quantity=0, broker_order_id="BRK_CH_MISSED_REJ_1",
    )

    copy_trade = CopyTrade(
        id=uuid.uuid4(),
        master_order_id="ORD_MISSED_REJ_1",
        follower_account_id=child_acc.id,
        copy_event_id=uuid.uuid4(),
        symbol="RELIANCE",
        exchange="NSE",
        side=OrderSide.BUY,
        quantity=100,
        product_type="MIS",
        estimated_price=Decimal("2950.00"),
        client_order_id="JT__MISSED_REJ_CT_1",
        broker_order_id="BRK_CH_MISSED_REJ_1",
        status=CopyTradeStatus.SUCCESS,
    )
    db_session.add(copy_trade)
    await db_session.commit()

    child_client = AsyncMock()
    child_client.modify_order.return_value = OrderResult(
        category=OrderResultCategory.REJECTED, client_order_id=None, broker_order_id=None,
        message="Broker does not support changing orderType via modify",
        raw_response={}, timestamp=datetime.now(timezone.utc),
    )
    clients = {str(child_acc.id): child_client}

    detector = MasterTradeDetector(master_account_id="M1")
    detector.process_order_update(_limit_open_event(broker_order_id="ORD_MISSED_REJ_1", quantity=100))
    fill_event = detector.process_order_update(
        _market_complete_event(broker_order_id="ORD_MISSED_REJ_1", quantity=100, filled_quantity=100)
    )

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=fill_event, clients=clients)

    assert res["status"] == "completed_with_failures"
    assert str(child_acc.id) in res["failed_accounts"]

    await db_session.refresh(copy_trade)
    assert copy_trade.status == CopyTradeStatus.RECONCILIATION_REQUIRED

    # The child's own Order row must be untouched - still LIMIT, still OPEN,
    # same broker_order_id - no blind replace, no cancel.
    await db_session.refresh(child_order)
    assert child_order.order_type == "LIMIT"
    assert child_order.status == OrderStatus.OPEN
    assert child_order.broker_order_id == "BRK_CH_MISSED_REJ_1"

    # No replacement child was created.
    rows = (
        await db_session.execute(select(Order).where(Order.master_broker_order_id == "ORD_MISSED_REJ_1"))
    ).scalars().all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Marketable-LIMIT repricing: the delivery mechanism for a master
# LIMIT -> MARKET conversion. Alice Blue rejects orderType=MARKET on modify
# with EC965 "Market orders are not allowed" (confirmed in production), so
# the child is REPRICED and keeps order_type=LIMIT and its broker order id.
# ---------------------------------------------------------------------------

def _reprice_client(broker_order_id="BRK_CH_L2M_1", result=None, positions=None, redis=None):
    client = AsyncMock()
    client.redis = redis
    client.get_funds.return_value = FundsData(
        account_id="f", available_margin=Decimal("500000"), collateral=Decimal("0"),
        used_margin=Decimal("0"), updated_at=datetime.now(timezone.utc),
    )
    client.get_positions.return_value = positions if positions is not None else []
    # get_available_long() reads get_positions_or_none(); mirror it here too.
    client.get_positions_or_none.return_value = positions if positions is not None else []
    client.modify_order.return_value = result or OrderResult(
        category=OrderResultCategory.SUCCESS, client_order_id=None, broker_order_id=broker_order_id,
        message="Modified successfully", raw_response={"stat": "Ok"}, timestamp=datetime.now(timezone.utc),
    )
    return client


@pytest.mark.asyncio
async def test_buy_reprice_uses_marketable_buy_pricing_from_master_fill(db_session, mock_redis):
    """BUY: the child is repriced using the EXISTING marketable-limit BUY
    strategy (fill price + buffer), derived from the master's CONFIRMED
    fill - never from the superseded pre-conversion LIMIT price."""
    from app.core.config import settings as app_settings

    child_acc, child_order = await _make_child(db_session, broker_order_id="BRK_BUY_RP")
    client = _reprice_client("BRK_BUY_RP", redis=mock_redis)

    original_buffer = app_settings.MARKET_ORDER_BUFFER_PERCENT
    app_settings.MARKET_ORDER_BUFFER_PERCENT = Decimal("1.0")
    try:
        detector = MasterTradeDetector(master_account_id="M1")
        detector.process_order_update(_limit_open_event())
        fill_event = detector.process_order_update(
            _market_complete_event(fill_price=Decimal("3000.00"))
        )
        pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
        res = await pipeline.execute_replication(event=fill_event, clients={str(child_acc.id): client})
    finally:
        app_settings.MARKET_ORDER_BUFFER_PERCENT = original_buffer

    assert res["status"] == "completed"
    _, kwargs = client.modify_order.call_args
    assert kwargs["order_type"] == OrderType.LIMIT
    assert kwargs["price"] == Decimal("3030.00")   # 3000 * 1.01, BUY pays up
    assert kwargs["broker_order_id"] == "BRK_BUY_RP"
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_sell_reprice_uses_marketable_sell_pricing_and_passes_position_safety(db_session, mock_redis):
    """SELL: uses the marketable-limit SELL strategy (fill price - buffer),
    and is only allowed because the follower holds a covering long - the
    deployed position-safety rule still applies to a reprice, since making a
    SELL marketable is what actually causes it to execute."""
    from app.core.config import settings as app_settings
    from app.services.alice_blue.types import Position

    child_acc, child_order = await _make_child(
        db_session, broker_order_id="BRK_SELL_RP", side=OrderSide.SELL
    )
    client = _reprice_client(
        "BRK_SELL_RP", redis=mock_redis,
        positions=[Position(symbol="RELIANCE-EQ", quantity=100, side="BUY",
                            average_price=Decimal("2950.00"), account_id="a",
                            exchange="NSE", product_type="MIS")],
    )

    original_buffer = app_settings.MARKET_ORDER_BUFFER_PERCENT
    app_settings.MARKET_ORDER_BUFFER_PERCENT = Decimal("1.0")
    try:
        detector = MasterTradeDetector(master_account_id="M1")
        detector.process_order_update(_limit_open_event())
        fill_event = detector.process_order_update(
            _market_complete_event(fill_price=Decimal("3000.00"))
        )
        fill_event.side = "SELL"
        pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
        res = await pipeline.execute_replication(event=fill_event, clients={str(child_acc.id): client})
    finally:
        app_settings.MARKET_ORDER_BUFFER_PERCENT = original_buffer

    assert res["status"] == "completed"
    _, kwargs = client.modify_order.call_args
    assert kwargs["order_type"] == OrderType.LIMIT
    assert kwargs["price"] == Decimal("2970.00")   # 3000 * 0.99, SELL sells down
    assert kwargs["broker_order_id"] == "BRK_SELL_RP"

    await db_session.refresh(child_order)
    assert child_order.order_type == "LIMIT"
    assert child_order.broker_order_id == "BRK_SELL_RP"


@pytest.mark.asyncio
async def test_sell_reprice_blocked_when_follower_has_no_covering_long(db_session, mock_redis):
    """The deployed SELL position-safety rule is NOT bypassed by the reprice
    path: with no covering long, the child is left untouched rather than
    being made marketable and pushed into a short."""
    child_acc, child_order = await _make_child(
        db_session, broker_order_id="BRK_SELL_NOPOS", side=OrderSide.SELL
    )
    client = _reprice_client("BRK_SELL_NOPOS", redis=mock_redis, positions=[])

    detector = MasterTradeDetector(master_account_id="M1")
    detector.process_order_update(_limit_open_event())
    fill_event = detector.process_order_update(_market_complete_event())
    fill_event.side = "SELL"

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=fill_event, clients={str(child_acc.id): client})

    assert res["status"] == "completed_with_failures"
    assert "INSUFFICIENT_LONG_POSITION" in res["modifications"][str(child_acc.id)]
    client.modify_order.assert_not_called()
    client.place_order.assert_not_called()

    await db_session.refresh(child_order)
    assert child_order.price == Decimal("2950.00")  # untouched
    assert child_order.status == OrderStatus.OPEN


@pytest.mark.asyncio
async def test_no_existing_child_records_reconciliation_and_creates_nothing(db_session, mock_redis):
    """No child exists for this master order: one must NOT be invented now -
    that is the duplicate-child bug this whole path exists to avoid."""
    client = _reprice_client(redis=mock_redis)

    detector = MasterTradeDetector(master_account_id="M1")
    detector.process_order_update(_limit_open_event())
    fill_event = detector.process_order_update(_market_complete_event())

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=fill_event, clients={"acc": client})

    assert res["status"] == "reconciliation_required"
    client.place_order.assert_not_called()
    client.modify_order.assert_not_called()

    rows = (await db_session.execute(select(Order))).scalars().all()
    assert len(rows) == 0


@pytest.mark.asyncio
async def test_cancelled_child_is_not_replaced(db_session, mock_redis):
    """A cancelled/rejected child means the conversion went undelivered.
    Record it - but never create a replacement order."""
    child_acc, child_order = await _make_child(
        db_session, status=OrderStatus.CANCELLED, broker_order_id="BRK_CANX"
    )
    client = _reprice_client("BRK_CANX", redis=mock_redis)

    detector = MasterTradeDetector(master_account_id="M1")
    detector.process_order_update(_limit_open_event())
    fill_event = detector.process_order_update(_market_complete_event())

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=fill_event, clients={str(child_acc.id): client})

    assert res["status"] == "reconciliation_required"
    client.place_order.assert_not_called()
    client.modify_order.assert_not_called()

    rows = (
        await db_session.execute(select(Order).where(Order.master_broker_order_id == "ORD_L2M_1"))
    ).scalars().all()
    assert len(rows) == 1  # no replacement


@pytest.mark.asyncio
async def test_missing_master_fill_price_blocks_the_reprice(db_session, mock_redis):
    """With no usable master execution price there is nothing to price
    against - the child must be left exactly as it is rather than repriced
    to zero."""
    child_acc, child_order = await _make_child(db_session, broker_order_id="BRK_NOPRICE")
    client = _reprice_client("BRK_NOPRICE", redis=mock_redis)

    detector = MasterTradeDetector(master_account_id="M1")
    detector.process_order_update(_limit_open_event())
    fill_event = detector.process_order_update(_market_complete_event(fill_price=Decimal("0")))

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=fill_event, clients={str(child_acc.id): client})

    assert res["status"] == "reconciliation_required"
    client.modify_order.assert_not_called()
    client.place_order.assert_not_called()

    await db_session.refresh(child_order)
    assert child_order.price == Decimal("2950.00")  # untouched, never zeroed


@pytest.mark.asyncio
async def test_repeated_fill_event_does_not_reprice_twice(db_session, mock_redis):
    """Idempotency: a child already sitting at the target marketable price
    needs nothing. A redelivered/duplicate master execution must not
    re-modify it and must not create a second child."""
    child_acc, child_order = await _make_child(db_session, broker_order_id="BRK_IDEM")
    client = _reprice_client("BRK_IDEM", redis=mock_redis)

    detector = MasterTradeDetector(master_account_id="M1")
    detector.process_order_update(_limit_open_event())
    fill_event = detector.process_order_update(_market_complete_event())

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    first = await pipeline.execute_replication(event=fill_event, clients={str(child_acc.id): client})
    assert first["status"] == "completed"
    assert client.modify_order.await_count == 1

    second = await pipeline.execute_replication(event=fill_event, clients={str(child_acc.id): client})
    assert second == {"status": "master_fill_updated"}
    assert client.modify_order.await_count == 1  # not repriced again

    rows = (
        await db_session.execute(select(Order).where(Order.master_broker_order_id == "ORD_L2M_1"))
    ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_one_follower_failure_does_not_affect_the_other(db_session, mock_redis):
    """Each existing child is repriced independently: a broker rejection for
    one follower must neither skip nor duplicate the other."""
    acc_ok, order_ok = await _make_child(db_session, broker_order_id="BRK_MULTI_OK")
    acc_bad, order_bad = await _make_child(db_session, broker_order_id="BRK_MULTI_BAD")

    client_ok = _reprice_client("BRK_MULTI_OK", redis=mock_redis)
    client_bad = _reprice_client(
        "BRK_MULTI_BAD", redis=mock_redis,
        result=OrderResult(
            category=OrderResultCategory.REJECTED, client_order_id=None, broker_order_id=None,
            message="rejected", raw_response={}, timestamp=datetime.now(timezone.utc),
        ),
    )

    detector = MasterTradeDetector(master_account_id="M1")
    detector.process_order_update(_limit_open_event())
    fill_event = detector.process_order_update(_market_complete_event())

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=fill_event,
        clients={str(acc_ok.id): client_ok, str(acc_bad.id): client_bad},
    )

    assert res["status"] == "completed_with_failures"
    assert res["modifications"][str(acc_ok.id)] == "MODIFIED"
    assert str(acc_bad.id) in res["failed_accounts"]

    # The healthy follower was repriced; neither was duplicated.
    await db_session.refresh(order_ok)
    assert order_ok.price == Decimal("2955.00")
    await db_session.refresh(order_bad)
    assert order_bad.price == Decimal("2950.00")  # untouched

    rows = (
        await db_session.execute(select(Order).where(Order.master_broker_order_id == "ORD_L2M_1"))
    ).scalars().all()
    assert len(rows) == 2
