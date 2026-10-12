"""
Regression coverage for the swallowed child-cancel failure observed in
production on 2026-09-08 15:09:04 IST.

The master's IDEA LIMIT order was cancelled from the Alice Blue mobile app.
Jettone dispatched the cancel to the follower, Alice Blue answered the
cancelorder call with HTTP 400, and the pipeline recorded nothing: no ERROR
log, no SystemEvent, no CopyTrade update. handle_master_cancellation still
returned {"status": "completed"} and EngineLoop logged the whole replication
as "completed". A broker-refused cancel was therefore indistinguishable from
a clean one in every artifact an operator would check, while the child order
was potentially still live at the broker and able to fill into a position the
master no longer held.

The fix surfaces both failure branches (non-SUCCESS result and raised
exception): ERROR log, CRITICAL CHILD_CANCELLATION_FAILED SystemEvent, the
CopyTrade moved to RECONCILIATION_REQUIRED, and a "completed_with_failures"
status so the caller stops reporting success. The Order row is deliberately
left untouched - the child's real state is unknown, and FollowerFillSyncer
stays the only writer that moves it on verified broker data.

No live trading is enabled and no real broker calls are made anywhere in this
file - all broker clients are AsyncMock test doubles.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.accounts import Account, AccountType
from app.models.audit import SystemEvent
from app.models.trades import (
    CopyTrade,
    CopyTradeStatus,
    Order,
    OrderSide,
    OrderSource,
    OrderStatus,
)
from app.services.alice_blue.types import OrderResult, OrderResultCategory
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator

MASTER_ORDER_ID = "26090800155200"
CHILD_ORDER_ID = "26090800155214"


async def _follower(db_session):
    acc = Account(
        id=uuid.uuid4(),
        account_type=AccountType.FOLLOWER,
        display_name="vikash",
        alice_user_id="AB_VIKASH",
        is_active=True,
    )
    db_session.add(acc)
    await db_session.commit()
    return acc


async def _open_child_order(db_session, account_id, broker_order_id=CHILD_ORDER_ID):
    order = Order(
        id=uuid.uuid4(),
        internal_order_id=f"JT_CH_{broker_order_id}",
        broker_order_id=broker_order_id,
        account_id=account_id,
        master_broker_order_id=MASTER_ORDER_ID,
        source=OrderSource.SYSTEM,
        exchange="NSE",
        symbol="IDEA",
        side=OrderSide.BUY,
        order_type="LIMIT",
        product_type="INTRADAY",
        quantity=1,
        filled_quantity=0,
        pending_quantity=1,
        price=Decimal("15.43"),
        status=OrderStatus.OPEN,
    )
    db_session.add(order)
    await db_session.commit()
    return order


async def _copy_trade(db_session, account_id):
    trade = CopyTrade(
        id=uuid.uuid4(),
        master_order_id=MASTER_ORDER_ID,
        follower_account_id=account_id,
        copy_event_id=uuid.uuid4(),
        broker_order_id=CHILD_ORDER_ID,
        symbol="IDEA",
        exchange="NSE",
        side=OrderSide.BUY,
        quantity=1,
        product_type="INTRADAY",
        status=CopyTradeStatus.SUBMITTED,
    )
    db_session.add(trade)
    await db_session.commit()
    return trade


def _cancel_refused():
    """Mirrors Alice Blue answering cancelorder with HTTP 400."""
    return OrderResult(
        category=OrderResultCategory.REJECTED,
        client_order_id=None,
        broker_order_id=CHILD_ORDER_ID,
        message="HTTP 400: Bad Request",
        raw_response={"stat": "Not_Ok", "emsg": "Bad Request"},
        timestamp=datetime.now(timezone.utc),
    )


async def _events(db_session, event_type="CHILD_CANCELLATION_FAILED"):
    rows = (
        await db_session.execute(
            select(SystemEvent).where(SystemEvent.event_type == event_type)
        )
    ).scalars().all()
    return rows


@pytest.mark.asyncio
async def test_broker_refused_cancel_is_not_reported_as_completed(db_session, mock_redis):
    """The production case: cancel_order returns a non-SUCCESS category."""
    follower = await _follower(db_session)
    order = await _open_child_order(db_session, follower.id)
    trade = await _copy_trade(db_session, follower.id)

    client = AsyncMock()
    client.cancel_order.return_value = _cancel_refused()

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.handle_master_cancellation(
        master_order_id=MASTER_ORDER_ID,
        clients={str(follower.id): client},
    )

    # The caller must no longer see a blanket success.
    assert res["status"] == "completed_with_failures"
    assert res["failed_accounts"] == [str(follower.id)]
    assert "CANCEL_FAILED" in res["cancellations"][str(follower.id)]

    # A CRITICAL operator-visible event must exist.
    events = await _events(db_session)
    assert len(events) == 1
    assert events[0].account_id == follower.id
    assert CHILD_ORDER_ID in events[0].message

    # The CopyTrade must be flagged for reconciliation, not left SUBMITTED.
    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.RECONCILIATION_REQUIRED
    assert "child cancel failed" in trade.failure_reason

    # The Order row must be left untouched - its true state is unknown.
    await db_session.refresh(order)
    assert order.status == OrderStatus.OPEN
    assert order.filled_quantity == 0
    assert order.pending_quantity == 1


@pytest.mark.asyncio
async def test_cancel_exception_is_also_surfaced(db_session, mock_redis):
    """The raised-exception branch must alert identically, not just log."""
    follower = await _follower(db_session)
    order = await _open_child_order(db_session, follower.id)
    trade = await _copy_trade(db_session, follower.id)

    client = AsyncMock()
    client.cancel_order.side_effect = TimeoutError("broker timed out")

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.handle_master_cancellation(
        master_order_id=MASTER_ORDER_ID,
        clients={str(follower.id): client},
    )

    assert res["status"] == "completed_with_failures"
    assert "ERROR" in res["cancellations"][str(follower.id)]

    events = await _events(db_session)
    assert len(events) == 1
    assert "TimeoutError" in events[0].message

    await db_session.refresh(trade)
    assert trade.status == CopyTradeStatus.RECONCILIATION_REQUIRED

    await db_session.refresh(order)
    assert order.status == OrderStatus.OPEN


@pytest.mark.asyncio
async def test_one_follower_failure_does_not_block_another(db_session, mock_redis):
    """A refused cancel for one follower must not stop the other's cancel."""
    failing = await _follower(db_session)
    working = Account(
        id=uuid.uuid4(),
        account_type=AccountType.FOLLOWER,
        display_name="second",
        alice_user_id="AB_SECOND",
        is_active=True,
    )
    db_session.add(working)
    await db_session.commit()

    await _open_child_order(db_session, failing.id)
    await _open_child_order(db_session, working.id, broker_order_id="BRK_OK")

    failing_client = AsyncMock()
    failing_client.cancel_order.return_value = _cancel_refused()

    ok_client = AsyncMock()
    ok_client.cancel_order.return_value = OrderResult(
        category=OrderResultCategory.SUCCESS,
        client_order_id=None,
        broker_order_id="BRK_OK",
        message="Cancelled successfully",
        raw_response={"stat": "Ok"},
        timestamp=datetime.now(timezone.utc),
    )
    ok_client.get_order_book.return_value = []

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.handle_master_cancellation(
        master_order_id=MASTER_ORDER_ID,
        clients={str(failing.id): failing_client, str(working.id): ok_client},
    )

    assert res["status"] == "completed_with_failures"
    assert res["failed_accounts"] == [str(failing.id)]
    # The healthy follower was still attempted.
    ok_client.cancel_order.assert_awaited_once()
    assert str(working.id) in res["cancellations"]
