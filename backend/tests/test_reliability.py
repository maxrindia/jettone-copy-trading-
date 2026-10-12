import pytest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal
from app.models.accounts import Account, AccountType, CopySetting, QuantityMode
from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderSide, OrderSource, OrderStatus
from app.models.reconciliation import ReconciliationRecord
from app.models.audit import SystemEvent, EventSeverity
from app.services.alice_blue.types import (
    OrderResult,
    OrderResultCategory,
    Position,
)
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.reliability.emergency_stop import EmergencyStopService
from app.services.reliability.reconciliation import ReconciliationEngine
from app.services.reliability.retry import RetryPolicy
from app.services.reliability.stale_order_sweeper import StaleOrderSweeper
from strategy_helpers import SAMPLE_MASTER_ACCOUNT_ID


@pytest.mark.asyncio
async def test_duplicate_master_event_firing(mock_redis, db_session, sample_accounts, sample_strategy, sample_copy_settings):
    """
    5.1: Same master trade event fired twice -> second call caught by DB constraint, returns SKIPPED.
    """
    validator = EventValidator(redis=mock_redis)
    pipeline = CopyEnginePipeline(validator=validator)

    funds_client = AsyncMock()
    funds_client.get_funds.return_value = MagicMock(
        available_margin=Decimal("500000")
    )
    funds_client.place_order = AsyncMock(
        return_value=OrderResult(
            category=OrderResultCategory.SUCCESS,
            client_order_id="CLI_DUP",
            broker_order_id="BRK_DUP_1",
            message="Order placed successfully",
            raw_response={"status": "success"},
            timestamp=datetime.now(timezone.utc),
        )
    )

    event = MasterTradeEvent(
        account_id=SAMPLE_MASTER_ACCOUNT_ID,
        master_order_id="ORD_DUP_1001",
        symbol="NIFTY24AUG23500CE",
        exchange="NFO",
        side="BUY",
        quantity=25,
        price=Decimal("150.50"),
        product_type="MIS",
        timestamp=datetime.now(timezone.utc),
    )

    # First call: creates copy_trades record for follower
    res1 = await pipeline.execute_replication(event=event, clients={str(sample_accounts["follower1"].id): funds_client})
    assert res1["status"] == "completed"

    follower_id = str(sample_accounts["follower1"].id)
    assert res1["results"][follower_id]["status"] == "SUCCESS"

    # Second call with identical event: caught by Redis first-line deduplication
    res2 = await pipeline.execute_replication(event=event, clients={})
    assert res2["status"] == "rejected"
    assert res2["reason"] == "DUPLICATE_EVENT"

    # Layer 2: Test DB-level unique constraint directly by loading follower with copy_settings eager loaded
    stmt_acc = select(Account).where(Account.id == sample_accounts["follower1"].id).options(selectinload(Account.copy_settings))
    acc_loaded = (await db_session.execute(stmt_acc)).scalar_one()

    res_db = await pipeline._process_single_follower(
        follower=acc_loaded,
        event=event,
        client=funds_client,
        lot_size=25,
        global_emergency_stop=False,
    )
    assert res_db["status"] == "SKIPPED"
    assert res_db["reason"] == "DUPLICATE_EVENT"

    # Assert exactly 1 copy_trades row exists in DB
    stmt = select(CopyTrade).where(
        CopyTrade.master_order_id == "ORD_DUP_1001",
        CopyTrade.follower_account_id == sample_accounts["follower1"].id,
    )
    db_res = await db_session.execute(stmt)
    trades = db_res.scalars().all()
    assert len(trades) == 1


@pytest.mark.asyncio
async def test_retry_policy_transient_failure():
    """
    5.2: Bounded retry policy retries NETWORK_ERROR up to max_attempts.
    """
    policy = RetryPolicy(max_attempts=3, base_delay_sec=0.01, backoff_factor=1.0)
    mock_func = AsyncMock()
    mock_func.return_value = OrderResult(
        category=OrderResultCategory.NETWORK_ERROR,
        client_order_id="CLI_1",
        broker_order_id=None,
        message="Network unreachable",
        raw_response={},
        timestamp=datetime.now(timezone.utc),
    )

    res = await policy.execute(mock_func)
    assert res.category == OrderResultCategory.NETWORK_ERROR
    assert mock_func.call_count == 3


@pytest.mark.asyncio
async def test_retry_policy_timeout_is_terminal():
    """
    5.2/R4: TIMEOUT is never retried - a timed-out request may still have
    executed at the broker; re-sending risks a duplicate position.
    """
    policy = RetryPolicy(max_attempts=3, base_delay_sec=0.01)
    mock_func = AsyncMock()
    mock_func.return_value = OrderResult(
        category=OrderResultCategory.TIMEOUT,
        client_order_id="CLI_1",
        broker_order_id=None,
        message="Request timed out",
        raw_response={},
        timestamp=datetime.now(timezone.utc),
    )

    res = await policy.execute(mock_func)
    assert res.category == OrderResultCategory.TIMEOUT
    assert mock_func.call_count == 1


@pytest.mark.asyncio
async def test_retry_policy_exception_returns_unknown_result():
    """
    5.2/R4: an exception inside the wrapped call returns a proper UNKNOWN
    OrderResult instead of raising TypeError (regression test).
    """
    policy = RetryPolicy(max_attempts=2, base_delay_sec=0.01)
    mock_func = AsyncMock(side_effect=RuntimeError("boom"))

    res = await policy.execute(mock_func)
    assert res.category == OrderResultCategory.UNKNOWN
    assert "boom" in res.message
    assert mock_func.call_count == 2


@pytest.mark.asyncio
async def test_retry_policy_rejected_no_retry():
    """
    5.2: Broker REJECTED category is NOT retried.
    """
    policy = RetryPolicy(max_attempts=3, base_delay_sec=0.01)
    mock_func = AsyncMock()
    mock_func.return_value = OrderResult(
        category=OrderResultCategory.REJECTED,
        client_order_id="CLI_1",
        broker_order_id=None,
        message="Insufficient margin",
        raw_response={},
        timestamp=datetime.now(timezone.utc),
    )

    res = await policy.execute(mock_func)
    assert res.category == OrderResultCategory.REJECTED
    assert mock_func.call_count == 1


@pytest.mark.asyncio
async def test_stale_order_sweeper(db_session, sample_accounts):
    """
    5.3: Stale orders in PENDING untouched > 120s are marked RECONCILIATION_REQUIRED.
    """
    stale_trade = CopyTrade(
        id=uuid.uuid4(),
        master_order_id="ORD_STALE_1",
        follower_account_id=sample_accounts["follower1"].id,
        copy_event_id=uuid.uuid4(),
        symbol="NIFTY",
        exchange="NFO",
        side=OrderSide.BUY,
        quantity=25,
        product_type="MIS",
        status=CopyTradeStatus.PENDING,
        updated_at=datetime.now(timezone.utc) - timedelta(seconds=150),
    )
    db_session.add(stale_trade)
    await db_session.commit()

    sweeper = StaleOrderSweeper(stale_threshold_seconds=120)
    flagged = await sweeper.sweep_once(db_session)
    assert flagged == 1

    await db_session.refresh(stale_trade)
    assert stale_trade.status == CopyTradeStatus.RECONCILIATION_REQUIRED


def _pending_child_order(follower_id, master_order_id, order_type="SL", status=OrderStatus.PENDING,
                          broker_status="SUCCESS"):
    """A follower child Order row for the sweeper's (master_broker_order_id,
    account_id) join - mirrors what CopyEnginePipeline actually writes for a
    native SL/SL-M child (see pipeline.py's _process_single_sl_follower).

    broker_status defaults to "SUCCESS" - the real, confirmed-live value
    (2026-10-01, master 26100100168532 / follower 26100100168552): it is a
    placement-acknowledgement literal written once at creation, never the
    broker's actual lifecycle state, and never corrected while the order
    stays genuinely unchanged-pending (see follower_fill_syncer.py's
    "skip the write when nothing changed" optimization). The sweeper's
    pending-SL determination must therefore key off `status`
    (OrderStatus.PENDING, the already-normalized/reliable field), not this
    raw string - these tests default broker_status to the actual buggy
    value specifically to prove that."""
    return Order(
        id=uuid.uuid4(),
        internal_order_id=f"JT_SWEEP_{uuid.uuid4().hex[:16]}",
        broker_order_id=f"BRK_SWEEP_{uuid.uuid4().hex[:10]}",
        account_id=follower_id,
        master_broker_order_id=master_order_id,
        source=OrderSource.SYSTEM,
        exchange="NSE",
        symbol="ITC",
        side=OrderSide.BUY,
        order_type=order_type,
        product_type="MIS",
        quantity=1,
        filled_quantity=0,
        pending_quantity=1,
        price=Decimal("265.00"),
        trigger_price=Decimal("265.10"),
        status=status,
        broker_status=broker_status,
    )


def _pending_copy_trade(master_order_id, follower_id, age_seconds):
    return CopyTrade(
        id=uuid.uuid4(),
        master_order_id=master_order_id,
        follower_account_id=follower_id,
        copy_event_id=uuid.uuid4(),
        symbol="ITC",
        exchange="NSE",
        side=OrderSide.BUY,
        quantity=1,
        product_type="MIS",
        status=CopyTradeStatus.PENDING,
        updated_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
    )


@pytest.mark.asyncio
async def test_normal_order_older_than_120s_is_flagged(db_session, sample_accounts):
    """No matching Order at all (plain/never-placed case) - existing 120s
    rule applies unchanged."""
    trade = _pending_copy_trade("ORD_PLAIN_STALE", sample_accounts["follower1"].id, age_seconds=150)
    db_session.add(trade)
    await db_session.commit()

    sweeper = StaleOrderSweeper(stale_threshold_seconds=120)
    flagged = await sweeper.sweep_once(db_session)

    assert flagged == 1
    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.RECONCILIATION_REQUIRED


@pytest.mark.asyncio
async def test_normal_order_newer_than_120s_is_not_flagged(db_session, sample_accounts):
    trade = _pending_copy_trade("ORD_PLAIN_FRESH", sample_accounts["follower1"].id, age_seconds=60)
    db_session.add(trade)
    await db_session.commit()

    sweeper = StaleOrderSweeper(stale_threshold_seconds=120)
    flagged = await sweeper.sweep_once(db_session)

    assert flagged == 0
    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.PENDING


@pytest.mark.asyncio
async def test_sl_trigger_pending_at_119s_is_not_flagged(db_session, sample_accounts):
    follower_id = sample_accounts["follower1"].id
    master_order_id = "ORD_SL_119S"

    db_session.add(_pending_child_order(follower_id, master_order_id))
    trade = _pending_copy_trade(master_order_id, follower_id, age_seconds=119)
    db_session.add(trade)
    await db_session.commit()

    sweeper = StaleOrderSweeper(stale_threshold_seconds=120)
    flagged = await sweeper.sweep_once(db_session)

    assert flagged == 0
    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.PENDING


@pytest.mark.asyncio
async def test_sl_trigger_pending_at_3599s_is_not_flagged(db_session, sample_accounts):
    """Past the normal 120s threshold but still under the SL-pending
    3600s ceiling - must NOT be flagged."""
    follower_id = sample_accounts["follower1"].id
    master_order_id = "ORD_SL_3599S"

    db_session.add(_pending_child_order(follower_id, master_order_id))
    trade = _pending_copy_trade(master_order_id, follower_id, age_seconds=3599)
    db_session.add(trade)
    await db_session.commit()

    sweeper = StaleOrderSweeper(stale_threshold_seconds=120)
    flagged = await sweeper.sweep_once(db_session)

    assert flagged == 0
    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.PENDING


@pytest.mark.asyncio
async def test_sl_trigger_pending_at_3600s_is_flagged(db_session, sample_accounts):
    """Crossing the wider SL-pending ceiling still eventually surfaces a
    genuinely orphaned pending SL - never permanently invisible."""
    follower_id = sample_accounts["follower1"].id
    master_order_id = "ORD_SL_3600S"

    db_session.add(_pending_child_order(follower_id, master_order_id))
    trade = _pending_copy_trade(master_order_id, follower_id, age_seconds=3600)
    db_session.add(trade)
    await db_session.commit()

    sweeper = StaleOrderSweeper(stale_threshold_seconds=120)
    flagged = await sweeper.sweep_once(db_session)

    assert flagged == 1
    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.RECONCILIATION_REQUIRED
    assert "120s" in trade.failure_reason  # crossing the threshold only ever produces the existing warning text


@pytest.mark.asyncio
async def test_slm_trigger_pending_uses_same_3600s_threshold(db_session, sample_accounts):
    follower_id = sample_accounts["follower1"].id
    master_order_id = "ORD_SLM_3600S"

    db_session.add(_pending_child_order(follower_id, master_order_id, order_type="SLM"))
    trade = _pending_copy_trade(master_order_id, follower_id, age_seconds=3600)
    db_session.add(trade)
    await db_session.commit()

    sweeper = StaleOrderSweeper(stale_threshold_seconds=120)
    flagged = await sweeper.sweep_once(db_session)

    assert flagged == 1
    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.RECONCILIATION_REQUIRED


@pytest.mark.asyncio
async def test_sl_order_not_trigger_pending_uses_existing_120s_threshold(db_session, sample_accounts):
    """A native SL order whose status has already moved past PENDING
    (regardless of its stale broker_status string) gets no special
    treatment - the normal 120s rule applies exactly as for any other
    order type."""
    follower_id = sample_accounts["follower1"].id
    master_order_id = "ORD_SL_NOT_PENDING"

    db_session.add(_pending_child_order(follower_id, master_order_id, status=OrderStatus.OPEN))
    trade = _pending_copy_trade(master_order_id, follower_id, age_seconds=150)
    db_session.add(trade)
    await db_session.commit()

    sweeper = StaleOrderSweeper(stale_threshold_seconds=120)
    flagged = await sweeper.sweep_once(db_session)

    assert flagged == 1
    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.RECONCILIATION_REQUIRED


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [OrderStatus.COMPLETE, OrderStatus.CANCELLED, OrderStatus.REJECTED])
async def test_sl_order_in_terminal_status_uses_existing_120s_threshold(db_session, sample_accounts, status):
    """SL child that has already completed/been cancelled/been rejected -
    status is no longer PENDING, so it must never be mistaken for a
    genuinely pending SL, regardless of what the stale broker_status
    string says."""
    follower_id = sample_accounts["follower1"].id
    master_order_id = f"ORD_SL_{status.value}"

    db_session.add(_pending_child_order(follower_id, master_order_id, status=status))
    trade = _pending_copy_trade(master_order_id, follower_id, age_seconds=150)
    db_session.add(trade)
    await db_session.commit()

    sweeper = StaleOrderSweeper(stale_threshold_seconds=120)
    flagged = await sweeper.sweep_once(db_session)

    assert flagged == 1
    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.RECONCILIATION_REQUIRED


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type", ["LIMIT", "MARKET"])
async def test_non_sl_order_type_never_gets_the_wider_threshold(db_session, sample_accounts, order_type):
    """Even if a LIMIT/MARKET child's status were somehow PENDING, the
    order_type gate must still exclude it - the wider ceiling is strictly
    scoped to native SL/SL-M."""
    follower_id = sample_accounts["follower1"].id
    master_order_id = f"ORD_{order_type}_NOT_SL"

    db_session.add(_pending_child_order(follower_id, master_order_id, order_type=order_type))
    trade = _pending_copy_trade(master_order_id, follower_id, age_seconds=150)
    db_session.add(trade)
    await db_session.commit()

    sweeper = StaleOrderSweeper(stale_threshold_seconds=120)
    flagged = await sweeper.sweep_once(db_session)

    assert flagged == 1
    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.RECONCILIATION_REQUIRED


@pytest.mark.asyncio
async def test_tatasteel_regression_sl_pending_with_success_broker_status_gets_grace(db_session, sample_accounts):
    """The exact real production case (2026-10-01): master 26100100168532
    TATASTEEL BUY SL INTRADAY triggered and filled; follower
    26100100168552 remained genuinely TRIGGER_PENDING at the broker
    (confirmed via a live, read-only broker order-book query), but its
    Order.broker_status was stuck at the placement-ack literal "SUCCESS"
    (never "TRIGGER_PENDING") - exactly the previously-broken condition.
    The CopyTrade row (correctly still PENDING, not yet resolved) must
    receive the 3600s grace, not be flagged after 120s."""
    follower_id = sample_accounts["follower1"].id
    master_order_id = "26100100168532"

    child = _pending_child_order(follower_id, master_order_id, order_type="SL")
    child.broker_order_id = "26100100168552"
    child.symbol = "TATASTEEL"
    child.product_type = "INTRADAY"
    child.price = Decimal("177.50")
    child.trigger_price = Decimal("177.45")
    db_session.add(child)

    trade = _pending_copy_trade(master_order_id, follower_id, age_seconds=150)
    db_session.add(trade)
    await db_session.commit()

    sweeper = StaleOrderSweeper(stale_threshold_seconds=120)
    flagged = await sweeper.sweep_once(db_session)

    assert flagged == 0
    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.PENDING


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type", ["SL", "SLM"])
async def test_pending_sl_at_121s_with_success_broker_status_is_not_flagged(db_session, sample_accounts, order_type):
    """Just past the normal threshold (live case 2026-10-05: WIPRO child
    26100500125855 flagged at 123s while legitimately waiting on its
    trigger). Order.status PENDING + SL/SLM gets the SL grace even though
    broker_status is the placement literal "SUCCESS"."""
    follower_id = sample_accounts["follower1"].id
    master_order_id = f"ORD_{order_type}_121S"

    db_session.add(_pending_child_order(follower_id, master_order_id, order_type=order_type, broker_status="SUCCESS"))
    trade = _pending_copy_trade(master_order_id, follower_id, age_seconds=121)
    db_session.add(trade)
    await db_session.commit()

    sweeper = StaleOrderSweeper(stale_threshold_seconds=120)
    flagged = await sweeper.sweep_once(db_session)

    assert flagged == 0
    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.PENDING
    assert trade.failure_reason is None


@pytest.mark.asyncio
async def test_no_matching_order_keeps_fail_safe_120s_behavior(db_session, sample_accounts):
    """No follower Order row exists at all for this CopyTrade - must not be
    silently exempted; the join finding nothing fails closed to the normal
    120s rule rather than risking a permanent blind spot."""
    trade = _pending_copy_trade("ORD_NO_MATCHING_ORDER", sample_accounts["follower1"].id, age_seconds=150)
    db_session.add(trade)
    await db_session.commit()

    sweeper = StaleOrderSweeper(stale_threshold_seconds=120)
    flagged = await sweeper.sweep_once(db_session)

    assert flagged == 1
    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.RECONCILIATION_REQUIRED


@pytest.mark.asyncio
async def test_reconciliation_engine_mismatch(db_session, sample_accounts):
    """
    5.4: Discrepancy between expected DB position and actual broker positions
    creates a ReconciliationRecord.

    Issue #4: expected position now comes from Order (source=SYSTEM,
    filled_quantity), not CopyTrade. A CopyTrade row representing the exact
    same fill is also seeded here (as CopyEnginePipeline's MARKET path
    really does write both for one executed order) to prove the two are not
    additively double-counted - expected_qty must be 50, not 100.
    """
    follower_id = sample_accounts["follower1"].id

    # Order (source=SYSTEM) is what reconciliation now reads: follower
    # bought 50 NIFTY, fully filled.
    order = Order(
        id=uuid.uuid4(),
        internal_order_id="JT_CH_ORD_REC_1_001",
        broker_order_id="BRK_REC_1",
        account_id=follower_id,
        master_broker_order_id="ORD_REC_1",
        source=OrderSource.SYSTEM,
        exchange="NFO",
        symbol="NIFTY",
        side=OrderSide.BUY,
        order_type="MARKET",
        product_type="MIS",
        quantity=50,
        filled_quantity=50,
        pending_quantity=0,
        price=Decimal("150.0"),
        status=OrderStatus.COMPLETE,
    )
    db_session.add(order)

    # Same logical fill, also represented in CopyTrade (as the real MARKET
    # placement path writes both tables for one order) - must NOT also be
    # summed into the expected position.
    trade = CopyTrade(
        id=uuid.uuid4(),
        master_order_id="ORD_REC_1",
        follower_account_id=follower_id,
        copy_event_id=uuid.uuid4(),
        symbol="NIFTY",
        exchange="NFO",
        side=OrderSide.BUY,
        quantity=50,
        product_type="MIS",
        status=CopyTradeStatus.SUCCESS,
    )
    db_session.add(trade)
    await db_session.commit()

    # Actual broker position is 25 (mismatch of -25)
    actual_positions = [
        Position(
            symbol="NIFTY",
            exchange="NFO",
            product_type="MIS",
            quantity=25,
            side="BUY",
            average_price=Decimal("150.0"),
            account_id=str(follower_id),
        )
    ]

    mismatches = await ReconciliationEngine.run_for_account(
        account_id=follower_id,
        db=db_session,
        actual_positions=actual_positions,
    )

    assert len(mismatches) == 1
    assert mismatches[0].symbol == "NIFTY"
    assert mismatches[0].expected_qty == 50  # NOT 100 - Order and CopyTrade are not summed together
    assert mismatches[0].actual_qty == 25
    assert mismatches[0].discrepancy_qty == -25


@pytest.mark.asyncio
async def test_emergency_stop_toggle(mock_redis, db_session):
    """
    5.6: Global emergency stop toggle works with Redis and creates audit SystemEvent.
    """
    service = EmergencyStopService(mock_redis)

    # Initial state
    assert not (await service.is_global_active())

    # Activate
    await service.activate_global(db_session, reason="Test emergency stop")
    assert await service.is_global_active()

    # Verify audit event written to DB
    stmt = select(SystemEvent).where(SystemEvent.event_type == "EMERGENCY_STOP_ACTIVATED")
    res = await db_session.execute(stmt)
    event = res.scalar_one_or_none()
    assert event is not None
    assert event.severity == EventSeverity.CRITICAL

    # Deactivate
    await service.deactivate_global(db_session, reason="Test resume")
    assert not (await service.is_global_active())
