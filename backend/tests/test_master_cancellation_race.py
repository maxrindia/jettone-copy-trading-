"""
Issue #6 Finding 2 regression coverage: handle_master_cancellation must not
finalize a follower's cancelled Order row using the STALE filled_quantity it
read before the async client.cancel_order() call. A fill can land at the
broker in the race window between that read and the cancel confirming, and
the pre-fix code never re-verified anything afterward - it could mark a
follower's order CANCELLED with filled_quantity=0 even though shares were
actually bought/sold, and that terminal status then permanently excludes the
row from FollowerFillSyncer's polling (TERMINAL_STATUSES), losing the fill.

The fix re-fetches the broker's order book (client.get_order_book()) after a
successful cancel, matches by broker_order_id, and uses THAT filled_quantity
as source of truth. If verification fails or the order can't be found, the
row is left unchanged and a SystemEvent is emitted (fail closed).

No live trading is enabled and no real broker calls are made anywhere in
this file - all broker clients are AsyncMock test doubles.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.accounts import Account, AccountType
from app.models.audit import SystemEvent
from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.types import OrderRecord, OrderResult, OrderResultCategory
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator


def _child_order(account_id, master_order_id="M_ORD_RACE", broker_order_id="BRK_1", quantity=100, filled_quantity=0):
    status = OrderStatus.PARTIAL if filled_quantity > 0 else OrderStatus.OPEN
    return Order(
        id=uuid.uuid4(),
        internal_order_id=f"JT_CH_{broker_order_id}",
        broker_order_id=broker_order_id,
        account_id=account_id,
        master_broker_order_id=master_order_id,
        source=OrderSource.SYSTEM,
        exchange="NSE",
        symbol="RELIANCE",
        side=OrderSide.BUY,
        order_type="LIMIT",
        product_type="MIS",
        quantity=quantity,
        filled_quantity=filled_quantity,
        pending_quantity=quantity - filled_quantity,
        price=Decimal("2950.00"),
        status=status,
    )


def _cancel_success(broker_order_id):
    return OrderResult(
        category=OrderResultCategory.SUCCESS,
        client_order_id=None,
        broker_order_id=broker_order_id,
        message="Cancelled successfully",
        raw_response={"stat": "Ok"},
        timestamp=datetime.now(timezone.utc),
    )


def _order_record(broker_order_id, quantity, filled_quantity, status="CANCELLED"):
    return OrderRecord(
        broker_order_id=broker_order_id,
        client_order_id=None,
        symbol="RELIANCE",
        exchange="NSE",
        side="BUY",
        quantity=quantity,
        filled_quantity=filled_quantity,
        average_price=Decimal("2950.00"),
        status=status,
        order_timestamp=datetime.now(timezone.utc),
        order_type="LIMIT",
    )


async def _add_follower(db_session, alice_user_id="AB999"):
    acc = Account(id=uuid.uuid4(), account_type=AccountType.FOLLOWER, display_name="Follower", alice_user_id=alice_user_id, is_active=True)
    db_session.add(acc)
    await db_session.commit()
    return acc


@pytest.mark.asyncio
async def test_1_no_race_regression_confirms_matching_zero_fill(db_session, mock_redis):
    """No race: broker's post-cancel state matches the pre-cancel read (0
    filled) -> still CANCELLED, filled_quantity=0, pending=0."""
    follower = await _add_follower(db_session)
    order = _child_order(follower.id, broker_order_id="BRK_NORACE", quantity=100, filled_quantity=0)
    db_session.add(order)
    await db_session.commit()

    client = AsyncMock()
    client.cancel_order.return_value = _cancel_success("BRK_NORACE")
    client.get_order_book.return_value = [_order_record("BRK_NORACE", 100, 0, status="CANCELLED")]

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.handle_master_cancellation(
        master_order_id="M_ORD_RACE",
        clients={str(follower.id): client},
    )

    assert res["status"] == "completed"
    assert res["cancellations"][str(follower.id)] == "CANCELLED"

    await db_session.refresh(order)
    assert order.status == OrderStatus.CANCELLED
    assert order.filled_quantity == 0
    assert order.pending_quantity == 0


@pytest.mark.asyncio
async def test_1b_no_race_regression_preexisting_partial_fill(db_session, mock_redis):
    """No race, but the order already had a partial fill before cancellation
    (mirrors the pre-existing test_master_cancellation_cancels_pending_child_only
    scenario) -> PARTIAL, filled_quantity preserved, pending=0."""
    follower = await _add_follower(db_session)
    order = _child_order(follower.id, broker_order_id="BRK_PARTIAL_NORACE", quantity=100, filled_quantity=50)
    db_session.add(order)
    await db_session.commit()

    client = AsyncMock()
    client.cancel_order.return_value = _cancel_success("BRK_PARTIAL_NORACE")
    client.get_order_book.return_value = [_order_record("BRK_PARTIAL_NORACE", 100, 50, status="CANCELLED")]

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.handle_master_cancellation(
        master_order_id="M_ORD_RACE",
        clients={str(follower.id): client},
    )

    assert res["cancellations"][str(follower.id)] == "PARTIAL"
    await db_session.refresh(order)
    assert order.status == OrderStatus.PARTIAL
    assert order.filled_quantity == 50
    assert order.pending_quantity == 0


@pytest.mark.asyncio
async def test_2_full_fill_during_cancel_race_is_captured(db_session, mock_redis):
    """Race: pre-cancel read shows filled_quantity=0, but the broker actually
    fully filled the order by the time cancel confirmed. Post-cancel
    verification must catch this and persist filled_quantity=100, never
    silently mark CANCELLED/0."""
    follower = await _add_follower(db_session)
    order = _child_order(follower.id, broker_order_id="BRK_RACE_FULL", quantity=100, filled_quantity=0)
    db_session.add(order)
    await db_session.commit()

    client = AsyncMock()
    client.cancel_order.return_value = _cancel_success("BRK_RACE_FULL")
    # Broker's true post-cancel state: fully filled before the cancel landed.
    client.get_order_book.return_value = [_order_record("BRK_RACE_FULL", 100, 100, status="COMPLETE")]

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.handle_master_cancellation(
        master_order_id="M_ORD_RACE",
        clients={str(follower.id): client},
    )

    assert res["cancellations"][str(follower.id)] == "PARTIAL"
    await db_session.refresh(order)
    assert order.status == OrderStatus.PARTIAL
    assert order.filled_quantity == 100
    assert order.pending_quantity == 0


@pytest.mark.asyncio
async def test_3_partial_fill_during_cancel_race_is_captured_and_never_reduced(db_session, mock_redis):
    """Race: pre-cancel read shows filled_quantity=0, broker's true post-cancel
    state is a 40-share partial fill -> persisted as 40, not lost as 0."""
    follower = await _add_follower(db_session)
    order = _child_order(follower.id, broker_order_id="BRK_RACE_PARTIAL", quantity=100, filled_quantity=0)
    db_session.add(order)
    await db_session.commit()

    client = AsyncMock()
    client.cancel_order.return_value = _cancel_success("BRK_RACE_PARTIAL")
    client.get_order_book.return_value = [_order_record("BRK_RACE_PARTIAL", 100, 40, status="CANCELLED")]

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.handle_master_cancellation(
        master_order_id="M_ORD_RACE",
        clients={str(follower.id): client},
    )

    assert res["cancellations"][str(follower.id)] == "PARTIAL"
    await db_session.refresh(order)
    assert order.filled_quantity == 40
    assert order.pending_quantity == 0


@pytest.mark.asyncio
async def test_3b_confirmed_filled_quantity_never_regresses_below_recorded_value(db_session, mock_redis):
    """Defensive invariant: even if the broker's post-cancel read somehow
    reports a filled_quantity lower than what was already recorded (should
    not happen for a real broker, but the pipeline must never regress a
    recorded fill downward), the persisted value must not decrease."""
    follower = await _add_follower(db_session)
    order = _child_order(follower.id, broker_order_id="BRK_RACE_NOREGRESS", quantity=100, filled_quantity=30)
    db_session.add(order)
    await db_session.commit()

    client = AsyncMock()
    client.cancel_order.return_value = _cancel_success("BRK_RACE_NOREGRESS")
    client.get_order_book.return_value = [_order_record("BRK_RACE_NOREGRESS", 100, 25, status="CANCELLED")]

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await pipeline.handle_master_cancellation(
        master_order_id="M_ORD_RACE",
        clients={str(follower.id): client},
    )

    await db_session.refresh(order)
    assert order.filled_quantity == 30  # never reduced below the already-recorded value


@pytest.mark.asyncio
async def test_4_post_cancel_verification_exception_fails_closed(db_session, mock_redis):
    """get_order_book() raising after a successful cancel must NOT result in
    the row being marked CANCELLED on unverified (stale) data. The row is
    left unchanged and a SystemEvent is emitted."""
    follower = await _add_follower(db_session)
    order = _child_order(follower.id, broker_order_id="BRK_VERIFY_FAIL", quantity=100, filled_quantity=0)
    db_session.add(order)
    await db_session.commit()

    client = AsyncMock()
    client.cancel_order.return_value = _cancel_success("BRK_VERIFY_FAIL")
    client.get_order_book.side_effect = RuntimeError("broker session expired")

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.handle_master_cancellation(
        master_order_id="M_ORD_RACE",
        clients={str(follower.id): client},
    )

    assert "VERIFICATION_FAILED" in res["cancellations"][str(follower.id)]

    await db_session.refresh(order)
    assert order.status == OrderStatus.OPEN  # unchanged, NOT CANCELLED
    assert order.filled_quantity == 0
    assert order.pending_quantity == 100  # unchanged

    from app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        events = (await session.execute(
            select(SystemEvent).where(SystemEvent.event_type == "CANCELLATION_VERIFICATION_FAILED")
        )).scalars().all()
    assert len(events) == 1
    assert events[0].account_id == follower.id


@pytest.mark.asyncio
async def test_5_order_missing_from_broker_order_book_fails_closed(db_session, mock_redis):
    """Broker order book returns successfully but doesn't contain this
    broker_order_id -> same fail-closed treatment as an outright fetch
    failure: row unchanged, SystemEvent emitted."""
    follower = await _add_follower(db_session)
    order = _child_order(follower.id, broker_order_id="BRK_MISSING", quantity=100, filled_quantity=0)
    db_session.add(order)
    await db_session.commit()

    client = AsyncMock()
    client.cancel_order.return_value = _cancel_success("BRK_MISSING")
    client.get_order_book.return_value = [_order_record("SOME_OTHER_ORDER", 50, 50, status="COMPLETE")]

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.handle_master_cancellation(
        master_order_id="M_ORD_RACE",
        clients={str(follower.id): client},
    )

    assert "VERIFICATION_FAILED" in res["cancellations"][str(follower.id)]

    await db_session.refresh(order)
    assert order.status == OrderStatus.OPEN
    assert order.filled_quantity == 0

    from app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        events = (await session.execute(
            select(SystemEvent).where(SystemEvent.event_type == "CANCELLATION_VERIFICATION_FAILED")
        )).scalars().all()
    assert len(events) == 1


@pytest.mark.asyncio
async def test_6_one_followers_verification_failure_does_not_affect_another(db_session, mock_redis):
    """Follower A's verification fails closed; Follower B's succeeds and is
    correctly finalized regardless."""
    follower_a = await _add_follower(db_session, alice_user_id="AB_A")
    follower_b = await _add_follower(db_session, alice_user_id="AB_B")

    order_a = _child_order(follower_a.id, broker_order_id="BRK_A_FAIL", quantity=100, filled_quantity=0)
    order_b = _child_order(follower_b.id, broker_order_id="BRK_B_OK", quantity=100, filled_quantity=0)
    db_session.add(order_a)
    db_session.add(order_b)
    await db_session.commit()

    client_a = AsyncMock()
    client_a.cancel_order.return_value = _cancel_success("BRK_A_FAIL")
    client_a.get_order_book.side_effect = RuntimeError("network error")

    client_b = AsyncMock()
    client_b.cancel_order.return_value = _cancel_success("BRK_B_OK")
    client_b.get_order_book.return_value = [_order_record("BRK_B_OK", 100, 0, status="CANCELLED")]

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.handle_master_cancellation(
        master_order_id="M_ORD_RACE",
        clients={str(follower_a.id): client_a, str(follower_b.id): client_b},
    )

    assert "VERIFICATION_FAILED" in res["cancellations"][str(follower_a.id)]
    assert res["cancellations"][str(follower_b.id)] == "CANCELLED"

    await db_session.refresh(order_a)
    await db_session.refresh(order_b)
    assert order_a.status == OrderStatus.OPEN  # unaffected, unchanged
    assert order_b.status == OrderStatus.CANCELLED  # correctly finalized


@pytest.mark.asyncio
async def test_7_market_child_order_cancelled_when_master_cancels_from_mobile(db_session, mock_redis):
    """
    Reproduces the reported production issue: a master MARKET order placed
    from Alice Blue Mobile/Web is copied to a follower as a MARKET order too
    (_process_single_market_follower). Since that placement no longer marks
    the child COMPLETE purely on broker-acceptance (OrderResult carries no
    fill confirmation - see pipeline.py), the child stays OPEN until
    FollowerFillSyncer confirms the real fill. If the master then cancels
    the still-open remainder from mobile before that happens,
    handle_master_cancellation must find and cancel this MARKET child order -
    previously it would already be (incorrectly) COMPLETE and invisible to
    this function's OPEN/PARTIAL/PENDING filter.
    """
    follower = await _add_follower(db_session, alice_user_id="AB_MARKET_CANCEL")
    order = _child_order(
        follower.id,
        master_order_id="M_ORD_MARKET_CANCEL",
        broker_order_id="BRK_MARKET_CANCEL",
        quantity=50,
        filled_quantity=0,
    )
    order.order_type = "MARKET"
    db_session.add(order)
    await db_session.commit()

    client = AsyncMock()
    client.cancel_order.return_value = _cancel_success("BRK_MARKET_CANCEL")
    client.get_order_book.return_value = [_order_record("BRK_MARKET_CANCEL", 50, 0, status="CANCELLED")]

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.handle_master_cancellation(
        master_order_id="M_ORD_MARKET_CANCEL",
        clients={str(follower.id): client},
    )

    assert res["cancellations"][str(follower.id)] == "CANCELLED"
    client.cancel_order.assert_awaited_once()

    await db_session.refresh(order)
    assert order.status == OrderStatus.CANCELLED
    assert order.pending_quantity == 0


@pytest.mark.asyncio
async def test_8_cancellation_syncs_matching_copy_trade_status(db_session, mock_redis):
    """
    handle_master_cancellation updates Order directly, bypassing
    save_or_update_order/FollowerFillSyncer - without also syncing the
    matching CopyTrade row, a follower's Demat card metrics (which read
    CopyTrade.status exclusively, see accounts.py::get_all_account_metrics)
    would keep showing this order as pending/in-flight forever after it was
    actually cancelled.
    """
    from app.models.trades import CopyTrade, CopyTradeStatus

    follower = await _add_follower(db_session, alice_user_id="AB_CT_SYNC")
    order = _child_order(
        follower.id,
        master_order_id="M_ORD_CT_SYNC",
        broker_order_id="BRK_CT_SYNC",
        quantity=25,
        filled_quantity=0,
    )
    db_session.add(order)
    trade = CopyTrade(
        id=uuid.uuid4(),
        master_order_id="M_ORD_CT_SYNC",
        follower_account_id=follower.id,
        copy_event_id=uuid.uuid4(),
        symbol="RELIANCE",
        exchange="NSE",
        side=OrderSide.BUY,
        quantity=25,
        product_type="MIS",
        estimated_price=Decimal("2950.00"),
        client_order_id="JT_LIM_CT_SYNC",
        status=CopyTradeStatus.PENDING,
    )
    db_session.add(trade)
    await db_session.commit()

    client = AsyncMock()
    client.cancel_order.return_value = _cancel_success("BRK_CT_SYNC")
    client.get_order_book.return_value = [_order_record("BRK_CT_SYNC", 25, 0, status="CANCELLED")]

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.handle_master_cancellation(
        master_order_id="M_ORD_CT_SYNC",
        clients={str(follower.id): client},
    )
    assert res["cancellations"][str(follower.id)] == "CANCELLED"

    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.FAILED
    assert "cancel" in (trade.failure_reason or "").lower()


@pytest.mark.asyncio
async def test_9_partial_fill_cancellation_syncs_copy_trade_as_success(db_session, mock_redis):
    """
    A PARTIAL cancellation outcome (some quantity genuinely filled before
    the remainder was cancelled at the broker) must also sync the matching
    CopyTrade row - previously only the zero-fill CANCELLED case did, so a
    partially-filled-then-cancelled order's CopyTrade stayed stuck at
    PENDING/SUBMITTED forever despite the Order row being correctly
    finalized as PARTIAL. Maps to CopyTradeStatus.SUCCESS (not FAILED) since
    real shares were actually bought/sold - the same bucket a full fill
    reaches (completed_count/pnl in the metrics endpoint).
    """
    from app.models.trades import CopyTrade, CopyTradeStatus

    follower = await _add_follower(db_session, alice_user_id="AB_CT_PARTIAL")
    order = _child_order(
        follower.id,
        master_order_id="M_ORD_CT_PARTIAL",
        broker_order_id="BRK_CT_PARTIAL",
        quantity=25,
        filled_quantity=0,
    )
    db_session.add(order)
    trade = CopyTrade(
        id=uuid.uuid4(),
        master_order_id="M_ORD_CT_PARTIAL",
        follower_account_id=follower.id,
        copy_event_id=uuid.uuid4(),
        symbol="RELIANCE",
        exchange="NSE",
        side=OrderSide.BUY,
        quantity=25,
        product_type="MIS",
        estimated_price=Decimal("2950.00"),
        client_order_id="JT_LIM_CT_PARTIAL",
        status=CopyTradeStatus.PENDING,
    )
    db_session.add(trade)
    await db_session.commit()

    client = AsyncMock()
    client.cancel_order.return_value = _cancel_success("BRK_CT_PARTIAL")
    # 10 of 25 filled by the time the cancel confirmed at the broker.
    client.get_order_book.return_value = [_order_record("BRK_CT_PARTIAL", 25, 10, status="PARTIAL")]

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.handle_master_cancellation(
        master_order_id="M_ORD_CT_PARTIAL",
        clients={str(follower.id): client},
    )
    assert res["cancellations"][str(follower.id)] == "PARTIAL"

    await db_session.refresh(order)
    assert order.status == OrderStatus.PARTIAL
    assert order.filled_quantity == 10

    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.SUCCESS
    assert "10" in (trade.failure_reason or "")
