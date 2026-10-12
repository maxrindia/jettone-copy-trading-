"""
Issue #4 regression coverage: ReconciliationEngine.compute_expected_positions
must read follower Order rows (source=SYSTEM), never CopyTrade, using
filled_quantity (not quantity); run_for_account must fail closed instead of
silently comparing against an assumed-empty actual-position map when no
broker data source is available; and crash recovery must flag ambiguous
SYSTEM Order rows left PENDING with no broker_order_id, without mutating
their status or guessing at automated recovery.

No live trading is enabled and no real broker calls are made anywhere in
this file - all broker clients are AsyncMock test doubles.
"""
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.accounts import Account, AccountType
from app.models.audit import SystemEvent
from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.types import Position
from app.services.reliability.reconciliation import ReconciliationEngine


def _order(
    account_id,
    symbol="NIFTY",
    side=OrderSide.BUY,
    order_type="MARKET",
    quantity=100,
    filled_quantity=0,
    pending_quantity=None,
    status=OrderStatus.PENDING,
    broker_order_id=None,
    internal_order_id=None,
) -> Order:
    return Order(
        id=uuid.uuid4(),
        internal_order_id=internal_order_id or f"JT_CH_{uuid.uuid4().hex[:12]}",
        broker_order_id=broker_order_id,
        account_id=account_id,
        master_broker_order_id="ORD_MASTER_1",
        source=OrderSource.SYSTEM,
        exchange="NFO",
        symbol=symbol,
        side=side,
        order_type=order_type,
        product_type="MIS",
        quantity=quantity,
        filled_quantity=filled_quantity,
        pending_quantity=pending_quantity if pending_quantity is not None else max(0, quantity - filled_quantity),
        price=Decimal("100.00"),
        status=status,
    )


# ---------------------------------------------------------------------------
# A. Expected position calculation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_order_contributes_filled_quantity(db_session, sample_accounts):
    follower = sample_accounts["follower1"]
    db_session.add(_order(follower.id, filled_quantity=100, quantity=100, status=OrderStatus.COMPLETE, broker_order_id="BRK_1"))
    await db_session.commit()

    positions = await ReconciliationEngine.compute_expected_positions(follower.id, db_session)
    assert positions == {"NIFTY": 100}


@pytest.mark.asyncio
async def test_partial_order_contributes_only_filled_quantity(db_session, sample_accounts):
    follower = sample_accounts["follower1"]
    db_session.add(_order(follower.id, filled_quantity=40, quantity=100, status=OrderStatus.PARTIAL, broker_order_id="BRK_2"))
    await db_session.commit()

    positions = await ReconciliationEngine.compute_expected_positions(follower.id, db_session)
    assert positions == {"NIFTY": 40}  # not 100


@pytest.mark.asyncio
async def test_partial_then_cancelled_retains_filled_quantity(db_session, sample_accounts):
    follower = sample_accounts["follower1"]
    db_session.add(_order(follower.id, filled_quantity=30, quantity=100, status=OrderStatus.CANCELLED, broker_order_id="BRK_3"))
    await db_session.commit()

    positions = await ReconciliationEngine.compute_expected_positions(follower.id, db_session)
    assert positions == {"NIFTY": 30}  # the 30 already filled before cancellation still counts


@pytest.mark.asyncio
async def test_pending_zero_filled_contributes_nothing(db_session, sample_accounts):
    follower = sample_accounts["follower1"]
    db_session.add(_order(follower.id, filled_quantity=0, quantity=100, status=OrderStatus.PENDING))
    await db_session.commit()

    positions = await ReconciliationEngine.compute_expected_positions(follower.id, db_session)
    assert positions == {}


@pytest.mark.asyncio
async def test_rejected_and_failed_zero_filled_contribute_nothing(db_session, sample_accounts):
    follower = sample_accounts["follower1"]
    db_session.add(_order(follower.id, symbol="RELIANCE", filled_quantity=0, status=OrderStatus.REJECTED, broker_order_id="BRK_4"))
    db_session.add(_order(follower.id, symbol="TCS", filled_quantity=0, status=OrderStatus.FAILED, broker_order_id="BRK_5"))
    await db_session.commit()

    positions = await ReconciliationEngine.compute_expected_positions(follower.id, db_session)
    assert positions == {}


@pytest.mark.asyncio
async def test_buy_and_sell_net_correctly(db_session, sample_accounts):
    follower = sample_accounts["follower1"]
    db_session.add(_order(follower.id, side=OrderSide.BUY, filled_quantity=100, quantity=100, status=OrderStatus.COMPLETE, broker_order_id="BRK_6"))
    db_session.add(_order(follower.id, side=OrderSide.SELL, filled_quantity=40, quantity=40, status=OrderStatus.COMPLETE, broker_order_id="BRK_7"))
    await db_session.commit()

    positions = await ReconciliationEngine.compute_expected_positions(follower.id, db_session)
    assert positions == {"NIFTY": 60}  # 100 bought - 40 sold


@pytest.mark.asyncio
async def test_market_and_limit_orders_do_not_double_count(db_session, sample_accounts):
    """Two independent Order rows (one MARKET, one LIMIT) for the same
    account/symbol must simply sum their own filled_quantity each - no
    special-casing by order_type, no double-count."""
    follower = sample_accounts["follower1"]
    db_session.add(_order(follower.id, order_type="MARKET", filled_quantity=100, quantity=100, status=OrderStatus.COMPLETE, broker_order_id="BRK_MKT_1"))
    db_session.add(_order(follower.id, order_type="LIMIT", filled_quantity=25, quantity=50, status=OrderStatus.PARTIAL, broker_order_id="BRK_LIM_1"))
    await db_session.commit()

    positions = await ReconciliationEngine.compute_expected_positions(follower.id, db_session)
    assert positions == {"NIFTY": 125}  # 100 + 25, each counted exactly once


@pytest.mark.asyncio
async def test_copytrade_is_never_queried_for_expected_positions(db_session, sample_accounts):
    """A CopyTrade row with no corresponding Order row must contribute
    nothing - proves compute_expected_positions reads Order only."""
    follower = sample_accounts["follower1"]
    db_session.add(
        CopyTrade(
            id=uuid.uuid4(),
            master_order_id="ORD_MASTER_1",
            follower_account_id=follower.id,
            copy_event_id=uuid.uuid4(),
            symbol="NIFTY",
            exchange="NFO",
            side=OrderSide.BUY,
            quantity=999,
            product_type="MIS",
            status=CopyTradeStatus.SUCCESS,
        )
    )
    await db_session.commit()

    positions = await ReconciliationEngine.compute_expected_positions(follower.id, db_session)
    assert positions == {}  # not {"NIFTY": 999}


# ---------------------------------------------------------------------------
# B. Actual-position retrieval must fail closed, not default to empty
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_for_account_fetches_real_positions_via_broker_client(db_session, sample_accounts):
    """The production wiring path: no actual_positions, only a broker_client
    - run_for_account must actually call get_positions(), not silently
    treat the account as flat."""
    follower = sample_accounts["follower1"]
    db_session.add(_order(follower.id, filled_quantity=50, quantity=50, status=OrderStatus.COMPLETE, broker_order_id="BRK_8"))
    await db_session.commit()

    client = AsyncMock()
    client.get_positions.return_value = [
        Position(symbol="NIFTY", exchange="NFO", product_type="MIS", quantity=50, side="BUY", average_price=Decimal("100"), account_id=str(follower.id))
    ]

    mismatches = await ReconciliationEngine.run_for_account(follower.id, db_session, broker_client=client)

    client.get_positions.assert_awaited_once()
    assert mismatches == []  # expected 50, actual 50 - no discrepancy


@pytest.mark.asyncio
async def test_run_for_account_fails_closed_with_no_data_source(db_session, sample_accounts):
    """This is the exact bug found in the read-only investigation: neither
    actual_positions nor broker_client supplied. Must NOT silently compare
    expected positions against an assumed-empty actual map (which would
    misreport every real position as a full mismatch) - must fail closed
    and report why."""
    follower = sample_accounts["follower1"]
    db_session.add(_order(follower.id, filled_quantity=50, quantity=50, status=OrderStatus.COMPLETE, broker_order_id="BRK_9"))
    await db_session.commit()

    mismatches = await ReconciliationEngine.run_for_account(follower.id, db_session)

    assert mismatches == []  # not a fabricated "50 vs 0" mismatch

    events = (
        await db_session.execute(select(SystemEvent).where(SystemEvent.event_type == "RECONCILIATION_FAILED"))
    ).scalars().all()
    assert len(events) == 1
    assert events[0].metadata_payload.get("reason") == "NO_BROKER_CLIENT"


@pytest.mark.asyncio
async def test_run_for_account_fails_closed_when_broker_client_raises(db_session, sample_accounts):
    follower = sample_accounts["follower1"]
    db_session.add(_order(follower.id, filled_quantity=50, quantity=50, status=OrderStatus.COMPLETE, broker_order_id="BRK_10"))
    await db_session.commit()

    client = AsyncMock()
    client.get_positions.side_effect = RuntimeError("broker session expired")

    mismatches = await ReconciliationEngine.run_for_account(follower.id, db_session, broker_client=client)

    assert mismatches == []
    events = (
        await db_session.execute(select(SystemEvent).where(SystemEvent.event_type == "RECONCILIATION_FAILED"))
    ).scalars().all()
    assert len(events) == 1


@pytest.mark.asyncio
async def test_run_crash_recovery_resolves_a_real_client_per_account(db_session, sample_accounts):
    """The client_provider plumbing added for the production crash-recovery
    path: each active FOLLOWER account's broker client must actually be
    looked up and used, not left as None.

    P2 Finding #6: the MASTER account is deliberately excluded from this
    loop (see test_master_reconciliation_exclusion.py) - master orders are
    never source=SYSTEM, so compute_expected_positions is structurally
    always {} for a master account and comparing it against real broker
    positions would always be a false-positive mismatch. client_provider
    must therefore never be consulted for the master here."""
    follower = sample_accounts["follower1"]
    db_session.add(_order(follower.id, filled_quantity=10, quantity=10, status=OrderStatus.COMPLETE, broker_order_id="BRK_11"))
    await db_session.commit()

    seen_account_ids = []
    client = AsyncMock()
    client.get_positions.return_value = []

    def client_provider(account_id: str):
        seen_account_ids.append(account_id)
        return client

    await ReconciliationEngine.run_crash_recovery(db_session, client_provider=client_provider)

    assert str(follower.id) in seen_account_ids
    assert str(sample_accounts["master"].id) not in seen_account_ids
    client.get_positions.assert_awaited()


# ---------------------------------------------------------------------------
# C. Crash recovery: ambiguous Order rows
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flag_ambiguous_orders_reports_pending_with_no_broker_id(db_session, sample_accounts):
    follower = sample_accounts["follower1"]
    ambiguous = _order(follower.id, status=OrderStatus.PENDING, broker_order_id=None, internal_order_id="JT_CH_AMBIG_1")
    db_session.add(ambiguous)
    await db_session.commit()

    count = await ReconciliationEngine.flag_ambiguous_orders(db_session)

    assert count == 1
    events = (
        await db_session.execute(select(SystemEvent).where(SystemEvent.event_type == "CRASH_RECOVERY_AMBIGUOUS_ORDER"))
    ).scalars().all()
    assert len(events) == 1
    assert events[0].metadata_payload["internal_order_id"] == "JT_CH_AMBIG_1"

    # Must NOT mutate the order's status - no evidence of the real outcome exists.
    await db_session.refresh(ambiguous)
    assert ambiguous.status == OrderStatus.PENDING


@pytest.mark.asyncio
async def test_flag_ambiguous_orders_ignores_orders_with_broker_id_or_non_pending(db_session, sample_accounts):
    """A PENDING order that already has a broker_order_id (broker responded,
    just not yet filled) is not ambiguous - it's tracked normally. Neither
    is any non-PENDING order."""
    follower = sample_accounts["follower1"]
    db_session.add(_order(follower.id, status=OrderStatus.PENDING, broker_order_id="BRK_KNOWN", internal_order_id="JT_CH_NOT_AMBIG_1"))
    db_session.add(_order(follower.id, status=OrderStatus.OPEN, broker_order_id="BRK_OPEN_1", internal_order_id="JT_CH_NOT_AMBIG_2"))
    await db_session.commit()

    count = await ReconciliationEngine.flag_ambiguous_orders(db_session)

    assert count == 0
    events = (
        await db_session.execute(select(SystemEvent).where(SystemEvent.event_type == "CRASH_RECOVERY_AMBIGUOUS_ORDER"))
    ).scalars().all()
    assert events == []


# ---------------------------------------------------------------------------
# D/12. Existing CopyTrade-based crash recovery behavior is untouched
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_copytrade_pending_flagging_still_works_unchanged(db_session, sample_accounts):
    """run_crash_recovery's original CopyTrade PENDING/SUBMITTED ->
    RECONCILIATION_REQUIRED behavior must be completely unaffected by the
    Order-related additions."""
    follower = sample_accounts["follower1"]
    trade = CopyTrade(
        id=uuid.uuid4(),
        master_order_id="ORD_STUCK_1",
        follower_account_id=follower.id,
        copy_event_id=uuid.uuid4(),
        symbol="NIFTY",
        exchange="NFO",
        side=OrderSide.BUY,
        quantity=25,
        product_type="MIS",
        status=CopyTradeStatus.PENDING,
    )
    db_session.add(trade)
    await db_session.commit()

    recovered = await ReconciliationEngine.run_crash_recovery(db_session)

    assert recovered == 1
    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.RECONCILIATION_REQUIRED
