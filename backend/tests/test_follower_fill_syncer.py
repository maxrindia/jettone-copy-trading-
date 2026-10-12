"""
Issue #3 regression coverage: FollowerFillSyncer must poll each follower's
broker order book and keep the matching SYSTEM-sourced `Order` row's
filled_quantity/status current - the gap identified in the read-only
investigation (a LIMIT child order's Order row was previously frozen at
whatever the broker's initial placement acknowledgment said, forever).

Also covers the CopyTrade-sync added afterward: when a synced Order reaches
a terminal status, the matching CopyTrade row (if any) is updated too, so
the Demat card metrics (which read CopyTrade.status exclusively) don't stay
stuck showing a resolved order as pending/in-flight forever. See
follower_fill_syncer.py's module docstring for the full reasoning, matching
rules, and the CopyTradeStatus.CANCELLED-doesn't-exist caveat.

No live trading is enabled anywhere in this file - all broker clients are
AsyncMock test doubles.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.accounts import Account, AccountType
from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.types import OrderRecord
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.reliability.follower_fill_syncer import FollowerFillSyncer, STALE_SWEEPER_REASON_PREFIX
from app.services.reliability.stale_order_sweeper import StaleOrderSweeper


def _order_record(
    broker_order_id: str,
    status: str,
    filled_quantity: int,
    quantity: int = 100,
    average_price: Decimal = Decimal("0"),
    rejection_reason=None,
) -> OrderRecord:
    return OrderRecord(
        broker_order_id=broker_order_id,
        client_order_id=None,
        symbol="RELIANCE-EQ",
        exchange="NSE",
        side=OrderSide.BUY,
        quantity=quantity,
        filled_quantity=filled_quantity,
        average_price=average_price,
        status=status,
        order_timestamp=datetime.now(timezone.utc),
        order_type="LIMIT",
        product_type="MIS",
        price=Decimal("2950.00"),
        rejection_reason=rejection_reason,
    )


async def _seed_follower_order(
    db_session,
    account_id,
    broker_order_id: str,
    status: OrderStatus = OrderStatus.OPEN,
    filled_quantity: int = 0,
    quantity: int = 100,
    internal_order_id: str = None,
) -> Order:
    order = Order(
        id=uuid.uuid4(),
        internal_order_id=internal_order_id or f"JT_CH_TEST_{broker_order_id}",
        broker_order_id=broker_order_id,
        account_id=account_id,
        master_broker_order_id="ORD_MASTER_1",
        source=OrderSource.SYSTEM,
        exchange="NSE",
        symbol="RELIANCE-EQ",
        side=OrderSide.BUY,
        order_type="LIMIT",
        product_type="MIS",
        quantity=quantity,
        filled_quantity=filled_quantity,
        pending_quantity=quantity - filled_quantity,
        price=Decimal("2950.00"),
        status=status,
    )
    db_session.add(order)
    await db_session.commit()
    await db_session.refresh(order)
    return order


def _syncer(clients: dict, pipeline: CopyEnginePipeline) -> FollowerFillSyncer:
    return FollowerFillSyncer(
        follower_clients_provider=lambda: clients,
        pipeline=pipeline,
    )


@pytest.mark.asyncio
async def test_partial_fill_updates_order(db_session, mock_redis, sample_accounts):
    follower = sample_accounts["follower1"]
    order = await _seed_follower_order(db_session, follower.id, "BRK_PARTIAL_1", quantity=100)

    client = AsyncMock()
    client.get_order_book.return_value = [
        _order_record("BRK_PARTIAL_1", status="PARTIAL", filled_quantity=40)
    ]

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    syncer = _syncer({str(follower.id): client}, pipeline)

    updated = await syncer.sync_once()

    assert updated == 1
    await db_session.refresh(order)
    assert order.status == OrderStatus.PARTIAL
    assert order.filled_quantity == 40
    assert order.pending_quantity == 60


@pytest.mark.asyncio
async def test_complete_fill_updates_order(db_session, mock_redis, sample_accounts):
    follower = sample_accounts["follower1"]
    order = await _seed_follower_order(db_session, follower.id, "BRK_COMPLETE_1", quantity=100)

    client = AsyncMock()
    client.get_order_book.return_value = [
        _order_record("BRK_COMPLETE_1", status="COMPLETE", filled_quantity=100, average_price=Decimal("2951.50"))
    ]

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    syncer = _syncer({str(follower.id): client}, pipeline)

    updated = await syncer.sync_once()

    assert updated == 1
    await db_session.refresh(order)
    assert order.status == OrderStatus.COMPLETE
    assert order.filled_quantity == 100
    assert order.pending_quantity == 0
    assert order.average_price == Decimal("2951.50")


@pytest.mark.asyncio
async def test_noop_when_broker_state_unchanged(db_session, mock_redis, sample_accounts):
    follower = sample_accounts["follower1"]
    order = await _seed_follower_order(
        db_session, follower.id, "BRK_SAME_1", status=OrderStatus.OPEN, filled_quantity=0, quantity=100
    )
    original_updated_at = order.updated_at

    client = AsyncMock()
    # Same status, same filled_quantity as what's already stored - nothing changed.
    client.get_order_book.return_value = [
        _order_record("BRK_SAME_1", status="OPEN", filled_quantity=0)
    ]

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    syncer = _syncer({str(follower.id): client}, pipeline)

    updated = await syncer.sync_once()

    assert updated == 0
    await db_session.refresh(order)
    assert order.updated_at == original_updated_at


@pytest.mark.asyncio
async def test_terminal_orders_are_never_polled_or_touched(db_session, mock_redis, sample_accounts):
    follower = sample_accounts["follower1"]
    order = await _seed_follower_order(
        db_session, follower.id, "BRK_DONE_1", status=OrderStatus.COMPLETE, filled_quantity=100, quantity=100
    )

    client = AsyncMock()
    # Even if the broker book disagreed, a terminal Order row must be left alone.
    client.get_order_book.return_value = [
        _order_record("BRK_DONE_1", status="CANCELLED", filled_quantity=0)
    ]

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    syncer = _syncer({str(follower.id): client}, pipeline)

    updated = await syncer.sync_once()

    assert updated == 0
    await db_session.refresh(order)
    assert order.status == OrderStatus.COMPLETE
    assert order.filled_quantity == 100


@pytest.mark.asyncio
async def test_one_follower_failure_does_not_block_others(db_session, mock_redis, sample_accounts):
    follower1 = sample_accounts["follower1"]
    follower2 = Account(
        id=uuid.uuid4(),
        account_type=AccountType.FOLLOWER,
        display_name="Follower 2",
        alice_user_id="AB00202",
        is_active=True,
    )
    db_session.add(follower2)
    await db_session.commit()

    order1 = await _seed_follower_order(db_session, follower1.id, "BRK_FAIL_1", quantity=50)
    order2 = await _seed_follower_order(db_session, follower2.id, "BRK_OK_1", quantity=50)

    failing_client = AsyncMock()
    failing_client.get_order_book.side_effect = RuntimeError("broker unreachable")

    working_client = AsyncMock()
    working_client.get_order_book.return_value = [
        _order_record("BRK_OK_1", status="COMPLETE", filled_quantity=50)
    ]

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    syncer = _syncer(
        {str(follower1.id): failing_client, str(follower2.id): working_client}, pipeline
    )

    updated = await syncer.sync_once()

    assert updated == 1
    await db_session.refresh(order1)
    await db_session.refresh(order2)
    assert order1.status == OrderStatus.OPEN  # untouched - its client raised
    assert order2.status == OrderStatus.COMPLETE  # synced despite the other follower failing


@pytest.mark.asyncio
async def test_no_copytrade_created_when_none_exists(db_session, mock_redis, sample_accounts):
    """A fill sync must never CREATE a new CopyTrade row - it only ever
    updates one that already exists, and only on a terminal transition."""
    follower = sample_accounts["follower1"]
    await _seed_follower_order(db_session, follower.id, "BRK_CT_1", quantity=100)

    before = (await db_session.execute(select(CopyTrade))).scalars().all()
    assert before == []

    client = AsyncMock()
    client.get_order_book.return_value = [
        _order_record("BRK_CT_1", status="COMPLETE", filled_quantity=100)
    ]

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    syncer = _syncer({str(follower.id): client}, pipeline)

    await syncer.sync_once()

    after = (await db_session.execute(select(CopyTrade))).scalars().all()
    assert after == []


@pytest.mark.asyncio
async def test_non_terminal_transition_does_not_touch_copytrade(db_session, mock_redis, sample_accounts):
    """A PARTIAL fill (not yet terminal) must not touch CopyTrade at all -
    only a genuine terminal transition (COMPLETE/CANCELLED/REJECTED/FAILED)
    does."""
    follower = sample_accounts["follower1"]
    order = await _seed_follower_order(db_session, follower.id, "BRK_CT_PARTIAL", quantity=100)
    trade = CopyTrade(
        id=uuid.uuid4(), master_order_id=order.master_broker_order_id,
        follower_account_id=follower.id, copy_event_id=uuid.uuid4(),
        symbol="RELIANCE-EQ", exchange="NSE", side=OrderSide.BUY, quantity=100,
        product_type="MIS", estimated_price=Decimal("2950.00"),
        client_order_id="JT_LIM_CT_PARTIAL", status=CopyTradeStatus.PENDING,
    )
    db_session.add(trade)
    await db_session.commit()

    client = AsyncMock()
    client.get_order_book.return_value = [
        _order_record("BRK_CT_PARTIAL", status="PARTIAL", filled_quantity=40)
    ]

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    syncer = _syncer({str(follower.id): client}, pipeline)
    await syncer.sync_once()

    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.PENDING


@pytest.mark.asyncio
async def test_terminal_transition_syncs_matching_copytrade_by_master_order_and_follower(
    db_session, mock_redis, sample_accounts
):
    """
    LIMIT copies never populate CopyTrade.broker_order_id (a pre-existing,
    documented gap), so matching must fall back to
    (master_order_id, follower_account_id) restricted to a still-PENDING/
    SUBMITTED row.
    """
    follower = sample_accounts["follower1"]
    order = await _seed_follower_order(db_session, follower.id, "BRK_CT_2", quantity=100)
    trade = CopyTrade(
        id=uuid.uuid4(), master_order_id=order.master_broker_order_id,
        follower_account_id=follower.id, copy_event_id=uuid.uuid4(),
        symbol="RELIANCE-EQ", exchange="NSE", side=OrderSide.BUY, quantity=100,
        product_type="MIS", estimated_price=Decimal("2950.00"),
        client_order_id="JT_LIM_CT_2", status=CopyTradeStatus.PENDING,
        # broker_order_id deliberately left unset - matches the real LIMIT gap.
    )
    db_session.add(trade)
    await db_session.commit()

    client = AsyncMock()
    client.get_order_book.return_value = [
        _order_record("BRK_CT_2", status="COMPLETE", filled_quantity=100)
    ]

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    syncer = _syncer({str(follower.id): client}, pipeline)
    await syncer.sync_once()

    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.SUCCESS


@pytest.mark.asyncio
async def test_terminal_transition_syncs_matching_copytrade_by_broker_order_id(
    db_session, mock_redis, sample_accounts
):
    """MARKET copies DO populate CopyTrade.broker_order_id at creation, so
    that's the primary, more precise match key when it's available."""
    follower = sample_accounts["follower1"]
    order = await _seed_follower_order(db_session, follower.id, "BRK_CT_3", quantity=100)
    trade = CopyTrade(
        id=uuid.uuid4(), master_order_id=order.master_broker_order_id,
        follower_account_id=follower.id, copy_event_id=uuid.uuid4(),
        symbol="RELIANCE-EQ", exchange="NSE", side=OrderSide.BUY, quantity=100,
        product_type="MIS", estimated_price=Decimal("2950.00"),
        client_order_id="JT__CT_3", status=CopyTradeStatus.SUBMITTED,
        broker_order_id="BRK_CT_3",
    )
    db_session.add(trade)
    await db_session.commit()

    client = AsyncMock()
    client.get_order_book.return_value = [
        _order_record("BRK_CT_3", status="CANCELLED", filled_quantity=0)
    ]

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    syncer = _syncer({str(follower.id): client}, pipeline)
    await syncer.sync_once()

    await db_session.refresh(trade)
    # CopyTradeStatus has no dedicated CANCELLED value - maps to FAILED,
    # the same bucket the metrics endpoint groups into "cancelled_count".
    assert trade.status == CopyTradeStatus.FAILED


# ---------------------------------------------------------------------------
# A RECONCILIATION_REQUIRED flag raised by StaleOrderSweeper itself is cleared
# once the child's terminal state is known. Any other reconciliation flag is a
# real finding and must survive.
# ---------------------------------------------------------------------------

def _copy_trade(master_order_id, follower_id, status=CopyTradeStatus.PENDING, failure_reason=None,
                broker_order_id=None, age_seconds=0):
    return CopyTrade(
        id=uuid.uuid4(), master_order_id=master_order_id, follower_account_id=follower_id,
        copy_event_id=uuid.uuid4(), symbol="RELIANCE-EQ", exchange="NSE", side=OrderSide.BUY,
        quantity=100, product_type="MIS", estimated_price=Decimal("2950.00"),
        status=status, failure_reason=failure_reason, broker_order_id=broker_order_id,
        updated_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
    )


async def _sync(mock_redis, follower, book):
    client = AsyncMock()
    client.get_order_book.return_value = book
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await _syncer({str(follower.id): client}, pipeline).sync_once()


@pytest.mark.asyncio
async def test_flag_raised_by_the_real_sweeper_is_cleared_when_child_completes(db_session, mock_redis, sample_accounts):
    """End to end with the real StaleOrderSweeper, so the reason text it
    writes and the prefix the syncer recognises can never drift apart."""
    follower = sample_accounts["follower1"]
    order = await _seed_follower_order(db_session, follower.id, "BRK_STALE_1")
    trade = _copy_trade(order.master_broker_order_id, follower.id, age_seconds=150)
    db_session.add(trade)
    await db_session.commit()

    assert await StaleOrderSweeper(stale_threshold_seconds=120).sweep_once(db_session) == 1
    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.RECONCILIATION_REQUIRED
    assert trade.failure_reason.startswith(STALE_SWEEPER_REASON_PREFIX)

    await _sync(mock_redis, follower, [_order_record("BRK_STALE_1", status="COMPLETE", filled_quantity=100)])

    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.SUCCESS
    assert trade.failure_reason is None
    assert trade.broker_order_id == "BRK_STALE_1"
    assert trade.completed_at is not None


@pytest.mark.asyncio
async def test_stale_flag_on_a_cancelled_child_records_the_real_terminal_state(db_session, mock_redis, sample_accounts):
    follower = sample_accounts["follower1"]
    order = await _seed_follower_order(db_session, follower.id, "BRK_STALE_CXL")
    trade = _copy_trade(order.master_broker_order_id, follower.id, CopyTradeStatus.RECONCILIATION_REQUIRED,
                        "Stale order untouched for >120s")
    db_session.add(trade)
    await db_session.commit()

    await _sync(mock_redis, follower, [_order_record("BRK_STALE_CXL", status="CANCELLED", filled_quantity=0)])

    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.FAILED
    assert trade.failure_reason == "Order reached CANCELLED at broker"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [
    "Master order modified but child modify failed: Modify rejected: EC965",
    "Master order cancelled but child cancel failed: order not found",
    "Order still open at broker: OPEN",
    "CopyTrade flagged RECONCILIATION_REQUIRED on crash recovery startup",
    "INSUFFICIENT_LONG_POSITION: follower holds 0 long",
    None,
])
async def test_genuine_reconciliation_flag_is_never_cleared(db_session, mock_redis, sample_accounts, reason):
    """Failed modify / failed cancel / timeout / crash recovery / SELL safety:
    the child completing does not make the finding go away."""
    follower = sample_accounts["follower1"]
    order = await _seed_follower_order(db_session, follower.id, "BRK_GENUINE")
    trade = _copy_trade(order.master_broker_order_id, follower.id, CopyTradeStatus.RECONCILIATION_REQUIRED, reason)
    db_session.add(trade)
    await db_session.commit()

    await _sync(mock_redis, follower, [_order_record("BRK_GENUINE", status="COMPLETE", filled_quantity=100)])

    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.RECONCILIATION_REQUIRED
    assert trade.failure_reason == reason
    assert trade.broker_order_id is None
    # The Order row itself is still synced as before.
    await db_session.refresh(order)
    assert order.status == OrderStatus.COMPLETE


@pytest.mark.asyncio
async def test_stale_flag_with_a_broker_order_id_is_not_matched_by_the_fallback(db_session, mock_redis, sample_accounts):
    """The new clause only ever matches a row with NO broker order id, so it
    can never land on a row that belongs to a different child order."""
    follower = sample_accounts["follower1"]
    order = await _seed_follower_order(db_session, follower.id, "BRK_MINE")
    trade = _copy_trade(order.master_broker_order_id, follower.id, CopyTradeStatus.RECONCILIATION_REQUIRED,
                        "Stale order untouched for >120s", broker_order_id="BRK_SOMEONE_ELSE")
    db_session.add(trade)
    await db_session.commit()

    await _sync(mock_redis, follower, [_order_record("BRK_MINE", status="COMPLETE", filled_quantity=100)])

    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.RECONCILIATION_REQUIRED
    assert trade.broker_order_id == "BRK_SOMEONE_ELSE"
    assert trade.failure_reason == "Stale order untouched for >120s"


@pytest.mark.asyncio
async def test_only_the_matching_masters_stale_flag_is_cleared(db_session, mock_redis, sample_accounts):
    """Two master orders, one follower, both copies stale-flagged: only the
    child that actually completed clears its own row."""
    follower = sample_accounts["follower1"]
    order_a = await _seed_follower_order(db_session, follower.id, "BRK_A")
    order_b = await _seed_follower_order(db_session, follower.id, "BRK_B")
    order_a.master_broker_order_id, order_b.master_broker_order_id = "MASTER_A", "MASTER_B"
    trade_a = _copy_trade("MASTER_A", follower.id, CopyTradeStatus.RECONCILIATION_REQUIRED, "Stale order untouched for >120s")
    trade_b = _copy_trade("MASTER_B", follower.id, CopyTradeStatus.RECONCILIATION_REQUIRED, "Stale order untouched for >120s")
    db_session.add_all([trade_a, trade_b])
    await db_session.commit()

    await _sync(mock_redis, follower, [
        _order_record("BRK_A", status="COMPLETE", filled_quantity=100),
        _order_record("BRK_B", status="OPEN", filled_quantity=0),
    ])

    await db_session.refresh(trade_a)
    await db_session.refresh(trade_b)
    assert trade_a.status == CopyTradeStatus.SUCCESS and trade_a.broker_order_id == "BRK_A"
    assert trade_b.status == CopyTradeStatus.RECONCILIATION_REQUIRED and trade_b.broker_order_id is None


@pytest.mark.asyncio
async def test_same_master_other_followers_stale_flag_is_not_cleared(db_session, mock_redis, sample_accounts):
    """One master order copied to two followers: follower1's fill never
    touches follower2's row."""
    f1 = sample_accounts["follower1"]
    f2 = Account(id=uuid.uuid4(), account_type=AccountType.FOLLOWER, display_name="Follower 2",
                 alice_user_id="AB00202", is_active=True)
    db_session.add(f2)
    await db_session.commit()
    order = await _seed_follower_order(db_session, f1.id, "BRK_F1")
    trade_f1 = _copy_trade(order.master_broker_order_id, f1.id, CopyTradeStatus.RECONCILIATION_REQUIRED, "Stale order untouched for >120s")
    trade_f2 = _copy_trade(order.master_broker_order_id, f2.id, CopyTradeStatus.RECONCILIATION_REQUIRED, "Stale order untouched for >120s")
    db_session.add_all([trade_f1, trade_f2])
    await db_session.commit()

    await _sync(mock_redis, f1, [_order_record("BRK_F1", status="COMPLETE", filled_quantity=100)])

    await db_session.refresh(trade_f1)
    await db_session.refresh(trade_f2)
    assert trade_f1.status == CopyTradeStatus.SUCCESS
    assert trade_f2.status == CopyTradeStatus.RECONCILIATION_REQUIRED


# ---------------------------------------------------------------------------
# Phase 2: the syncer must not write a stale order_type/price back over a
# newer pipeline-persisted SL -> LIMIT conversion. The snapshot it works from
# is taken BEFORE the order-book round trip, and the pipeline can convert the
# same child inside that window.
# ---------------------------------------------------------------------------


async def _sync_with_concurrent_pipeline_write(
    db_session, mock_redis, account_id, broker_order_id, *, converted_type, converted_price, book_record
):
    """Runs one sync pass where the pipeline converts the child to LIMIT while
    the syncer is awaiting the order book - the exact race window."""
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    order = await _seed_follower_order(
        db_session, account_id, broker_order_id, status=OrderStatus.PENDING
    )
    # The seeded row is the pre-conversion SL state the syncer will snapshot.
    order.order_type = "SL"
    order.price = Decimal("503.85")
    order.trigger_price = Decimal("503.85")
    await db_session.commit()

    async def _get_order_book():
        # Simulates _persist_triggered_sl_child_state landing during the fetch.
        from app.core.database import AsyncSessionLocal
        from sqlalchemy import update as sa_update

        async with AsyncSessionLocal() as session:
            await session.execute(
                sa_update(Order)
                .where(Order.internal_order_id == order.internal_order_id)
                .values(order_type=converted_type, price=converted_price, trigger_price=Decimal("0"))
            )
            await session.commit()
        return [book_record]

    client = AsyncMock()
    client.get_order_book = _get_order_book
    await _syncer({str(account_id): client}, pipeline).sync_once()

    await db_session.refresh(order)
    return order


@pytest.mark.asyncio
async def test_stale_snapshot_cannot_overwrite_converted_order_type(db_session, mock_redis, sample_accounts):
    """1. A stale snapshot must not revert order_type from LIMIT back to SL."""
    follower = sample_accounts["follower1"]
    order = await _sync_with_concurrent_pipeline_write(
        db_session, mock_redis, follower.id, "BRK_RACE_TYPE",
        converted_type="LIMIT", converted_price=Decimal("504.65"),
        book_record=_order_record("BRK_RACE_TYPE", status="COMPLETE", filled_quantity=100,
                                  average_price=Decimal("504.65")),
    )
    assert order.order_type == "LIMIT"   # not reverted to "SL"


@pytest.mark.asyncio
async def test_stale_snapshot_cannot_overwrite_converted_price(db_session, mock_redis, sample_accounts):
    """2. A stale snapshot must not revert price to the trigger-era value."""
    follower = sample_accounts["follower1"]
    order = await _sync_with_concurrent_pipeline_write(
        db_session, mock_redis, follower.id, "BRK_RACE_PRICE",
        converted_type="LIMIT", converted_price=Decimal("504.65"),
        book_record=_order_record("BRK_RACE_PRICE", status="OPEN", filled_quantity=0),
    )
    assert order.price == Decimal("504.65")   # not reverted to 503.85


@pytest.mark.asyncio
async def test_broker_owned_fields_still_update_through_the_race(db_session, mock_redis, sample_accounts):
    """3. Status, fill quantity and average price are the syncer's own fields
    and must still be written from the fresh broker record."""
    follower = sample_accounts["follower1"]
    order = await _sync_with_concurrent_pipeline_write(
        db_session, mock_redis, follower.id, "BRK_RACE_FILL",
        converted_type="LIMIT", converted_price=Decimal("504.65"),
        book_record=_order_record("BRK_RACE_FILL", status="COMPLETE", filled_quantity=100,
                                  average_price=Decimal("504.70")),
    )
    assert order.status == OrderStatus.COMPLETE
    assert order.filled_quantity == 100
    assert order.pending_quantity == 0
    assert order.average_price == Decimal("504.70")
    assert order.broker_status == "COMPLETE"
    # and the pipeline-owned fields are still the converted ones
    assert order.order_type == "LIMIT"
    assert order.price == Decimal("504.65")


@pytest.mark.asyncio
async def test_no_race_leaves_order_type_and_price_untouched(db_session, mock_redis, sample_accounts):
    """4. Without a concurrent write the values are unchanged - the re-read is
    behaviourally identical to the previous snapshot pass-through."""
    follower = sample_accounts["follower1"]
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    order = await _seed_follower_order(db_session, follower.id, "BRK_NORACE", status=OrderStatus.OPEN)

    client = AsyncMock()
    client.get_order_book.return_value = [
        _order_record("BRK_NORACE", status="PARTIAL", filled_quantity=40)
    ]
    await _syncer({str(follower.id): client}, pipeline).sync_once()

    await db_session.refresh(order)
    assert order.order_type == "LIMIT"
    assert order.price == Decimal("2950.00")
    assert order.status == OrderStatus.PARTIAL
    assert order.filled_quantity == 40


@pytest.mark.asyncio
async def test_row_deleted_mid_pass_is_skipped_without_error(db_session, mock_redis, sample_accounts):
    """5. If the Order row disappears during the order-book fetch the pass
    skips it rather than resurrecting it through the upsert."""
    from app.core.database import AsyncSessionLocal
    from sqlalchemy import delete as sa_delete

    follower = sample_accounts["follower1"]
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    order = await _seed_follower_order(db_session, follower.id, "BRK_GONE", status=OrderStatus.OPEN)
    internal_id = order.internal_order_id

    async def _get_order_book():
        async with AsyncSessionLocal() as session:
            await session.execute(sa_delete(Order).where(Order.internal_order_id == internal_id))
            await session.commit()
        return [_order_record("BRK_GONE", status="COMPLETE", filled_quantity=100)]

    client = AsyncMock()
    client.get_order_book = _get_order_book
    await _syncer({str(follower.id): client}, pipeline).sync_once()

    async with AsyncSessionLocal() as session:
        remaining = (
            await session.execute(select(Order).where(Order.internal_order_id == internal_id))
        ).scalar_one_or_none()
    assert remaining is None   # not recreated by the upsert
