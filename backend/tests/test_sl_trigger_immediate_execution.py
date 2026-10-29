"""
Master native SL/SL-M triggers -> the EXISTING follower SL/SL-M child is
pushed to execute immediately by modifying the SAME broker order in place
(SL/SL-M -> marketable LIMIT, no trigger sent). Never cancel, never a second
order; the broker order book is read before and after the modify and is the
source of truth.

No live trading and no real broker calls are made anywhere in this file -
every broker client is an AsyncMock and the DB is the in-memory test SQLite
engine (db_session fixture).
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.audit import SystemEvent
from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.types import (
    FundsData, OrderRecord, OrderResult, OrderResultCategory, OrderSide as BrokerOrderSide,
    OrderType, Position,
)
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline, compute_marketable_limit_price
from app.services.copy_engine.validator import EventValidator

MASTER_ID = "caf4726b-c48c-4e54-b899-ceb0f7259da8"
TOKEN = "1660"


def _record(broker_order_id, order_type, status, price, trigger_price=Decimal("0"),
            quantity=1, filled_quantity=0, side=BrokerOrderSide.BUY):
    return OrderRecord(
        broker_order_id=broker_order_id, client_order_id=None, symbol="ITC", exchange="NSE",
        side=side, quantity=quantity, filled_quantity=filled_quantity, average_price=price,
        status=status, order_timestamp=datetime.now(timezone.utc), order_type=order_type,
        product_type="INTRADAY", price=price, trigger_price=trigger_price, instrument_id=TOKEN,
    )


def _result(category=OrderResultCategory.SUCCESS, message="ok", broker_order_id=None):
    return OrderResult(
        category=category, client_order_id=None, broker_order_id=broker_order_id,
        message=message, raw_response={}, timestamp=datetime.now(timezone.utc),
    )


def _client(books, modify_result=None, long_qty=10):
    client = AsyncMock()
    client.get_funds.return_value = FundsData(
        account_id="f", available_margin=Decimal("500000"), collateral=Decimal("0"),
        used_margin=Decimal("0"), updated_at=datetime.now(timezone.utc),
    )
    client.get_positions_or_none.return_value = [Position(
        symbol="ITC-EQ", quantity=long_qty, side="BUY", average_price=Decimal("260"),
        account_id="a", exchange="NSE", product_type="MIS", instrument_id=TOKEN,
    )]
    client.get_order_book.side_effect = books
    client.modify_order.return_value = modify_result or _result()
    return client


async def _child(db_session, follower_id, master_id, broker_id, order_type="SL", side=OrderSide.BUY,
                 quantity=1, price=Decimal("265.00"), trigger=Decimal("264.90")):
    row = Order(
        id=uuid.uuid4(), internal_order_id=f"JT_CH_{uuid.uuid4().hex[:16]}", broker_order_id=broker_id,
        account_id=follower_id, master_broker_order_id=master_id, source=OrderSource.SYSTEM,
        exchange="NSE", symbol="ITC", side=side, order_type=order_type, product_type="MIS",
        quantity=quantity, filled_quantity=0, pending_quantity=quantity, price=price,
        trigger_price=trigger, status=OrderStatus.PENDING, broker_status="SUCCESS",
    )
    db_session.add(row)
    await db_session.commit()
    return row


def _fill_event(master_id, side="BUY", fill=Decimal("265.20"), reported_type="MARKET",
                event_type="ORDER_FILLED", status="COMPLETE"):
    return MasterTradeEvent(
        master_order_id=master_id, symbol="ITC", exchange="NSE", side=side, quantity=1,
        price=fill, limit_price=Decimal("265.00") if reported_type == "LIMIT" else Decimal("0"),
        product_type="MIS", timestamp=datetime.now(timezone.utc), account_id=MASTER_ID,
        order_type=reported_type, event_type=event_type, status=status, total_quantity=1,
        filled_quantity=1, instrument_id=TOKEN,
    )


async def _row(db_session, row_id):
    db_session.expire_all()
    return (await db_session.execute(select(Order).where(Order.id == row_id))).scalar_one()


async def _events(db_session, event_type):
    return (await db_session.execute(select(SystemEvent).where(SystemEvent.event_type == event_type))).scalars().all()


def _pipeline(mock_redis):
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    # The Master is not a stored account in these tests; declare its broker (Alice Blue -> Alice Blue keeps the token native).
    pipeline._master_brokers[MASTER_ID] = "alicebluenew"
    return pipeline


# ---------------------------------------------------------------------------
# 1-7. SL / SL-M, BUY / SELL: same order modified in place to marketable LIMIT
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("order_type,side,reported", [
    ("SL", "BUY", "MARKET"),
    ("SL", "SELL", "LIMIT"),
    ("SLM", "BUY", "MARKET"),
    ("SLM", "SELL", "MARKET"),
])
async def test_triggered_master_converts_existing_child_to_marketable_limit(
    db_session, mock_redis, sample_accounts, order_type, side, reported,
):
    follower = sample_accounts["follower1"]
    master_id = f"M_{order_type}_{side}"
    child_id = f"C_{order_type}_{side}"
    fill = Decimal("265.20")
    target = compute_marketable_limit_price(side, fill)
    broker_side = BrokerOrderSide.BUY if side == "BUY" else BrokerOrderSide.SELL
    pending_price = Decimal("0") if order_type == "SLM" else Decimal("265.00")

    row = await _child(db_session, follower.id, master_id, child_id, order_type=order_type,
                       side=OrderSide.BUY if side == "BUY" else OrderSide.SELL, price=pending_price)
    client = _client([
        [_record(child_id, order_type, "TRIGGER_PENDING", pending_price, Decimal("264.90"), side=broker_side)],
        [_record(child_id, "LIMIT", "OPEN", target, side=broker_side)],
    ])

    res = await _pipeline(mock_redis).execute_replication(
        event=_fill_event(master_id, side=side, fill=fill, reported_type=reported),
        clients={str(follower.id): client},
    )

    assert res["executions"][str(follower.id)] == "MODIFIED: WORKING_MARKETABLE_LIMIT"
    client.modify_order.assert_awaited_once()
    kwargs = client.modify_order.call_args.kwargs
    assert kwargs["broker_order_id"] == child_id                 # 5. same brokerOrderId
    assert kwargs["order_type"] == OrderType.LIMIT
    assert kwargs["trigger_price"] == Decimal("0")               # 6. trigger removed
    assert kwargs["price"] == target                             # 7. existing pricing helper
    client.place_order.assert_not_called()
    client.cancel_order.assert_not_called()

    persisted = await _row(db_session, row.id)
    assert persisted.broker_order_id == child_id
    assert persisted.order_type == "LIMIT"
    assert persisted.trigger_price == Decimal("0")
    assert persisted.price == target
    assert persisted.status == OrderStatus.OPEN


# ---------------------------------------------------------------------------
# 8. Partially filled (already triggered, stranded at its own limit):
#    only the remaining quantity is repriced.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_partially_filled_child_modifies_only_remaining_quantity(db_session, mock_redis, sample_accounts):
    follower = sample_accounts["follower1"]
    fill = Decimal("265.20")
    target = compute_marketable_limit_price("BUY", fill)
    row = await _child(db_session, follower.id, "M_PARTIAL", "C_PARTIAL", quantity=10)
    client = _client([
        [_record("C_PARTIAL", "LIMIT", "PARTIAL", Decimal("265.00"), quantity=10, filled_quantity=4)],
        [_record("C_PARTIAL", "LIMIT", "PARTIAL", target, quantity=10, filled_quantity=4)],
    ])

    res = await _pipeline(mock_redis).execute_replication(
        event=_fill_event("M_PARTIAL", fill=fill), clients={str(follower.id): client},
    )

    assert res["executions"][str(follower.id)] == "EXECUTED: BROKER_PARTIAL"
    assert client.modify_order.call_args.kwargs["quantity"] == 6   # 10 - 4 already filled
    persisted = await _row(db_session, row.id)
    assert persisted.status == OrderStatus.PARTIAL
    assert persisted.filled_quantity == 4
    client.place_order.assert_not_called()
    client.cancel_order.assert_not_called()


# ---------------------------------------------------------------------------
# 9. Already filled at the broker: never modified.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_already_filled_child_is_never_modified(db_session, mock_redis, sample_accounts):
    follower = sample_accounts["follower1"]
    row = await _child(db_session, follower.id, "M_DONE", "C_DONE")
    client = _client([[_record("C_DONE", "LIMIT", "COMPLETE", Decimal("264.95"), filled_quantity=1)]])

    res = await _pipeline(mock_redis).execute_replication(
        event=_fill_event("M_DONE"), clients={str(follower.id): client},
    )

    assert res["executions"][str(follower.id)] == "NO_ACTION: BROKER_COMPLETE"
    client.modify_order.assert_not_called()
    client.place_order.assert_not_called()
    client.cancel_order.assert_not_called()
    assert (await _row(db_session, row.id)).status == OrderStatus.PENDING  # left for follower_fill_syncer


# ---------------------------------------------------------------------------
# 10. Broker rejects SL -> LIMIT: child untouched, reconciliation recorded.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_broker_rejects_conversion_leaves_child_untouched(db_session, mock_redis, sample_accounts):
    follower = sample_accounts["follower1"]
    row = await _child(db_session, follower.id, "M_REJ", "C_REJ")
    pending = _record("C_REJ", "SL", "TRIGGER_PENDING", Decimal("265.00"), Decimal("264.90"))
    client = _client(
        [[pending], [pending]],
        modify_result=_result(OrderResultCategory.REJECTED, "orderType change not allowed"),
    )

    res = await _pipeline(mock_redis).execute_replication(
        event=_fill_event("M_REJ"), clients={str(follower.id): client},
    )

    assert res["status"] == "completed_with_failures"
    assert res["executions"][str(follower.id)] == "FAILED: NOT_CONFIRMED"
    persisted = await _row(db_session, row.id)
    assert persisted.order_type == "SL"
    assert persisted.status == OrderStatus.PENDING
    assert persisted.trigger_price == Decimal("264.90")
    assert len(await _events(db_session, "CHILD_MODIFICATION_FAILED")) == 1
    client.place_order.assert_not_called()
    client.cancel_order.assert_not_called()


# ---------------------------------------------------------------------------
# 11. Repeated master fill event: one modify only, never a second order.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_repeated_master_fill_event_does_not_modify_twice(db_session, mock_redis, sample_accounts):
    follower = sample_accounts["follower1"]
    fill = Decimal("265.20")
    target = compute_marketable_limit_price("BUY", fill)
    await _child(db_session, follower.id, "M_REPEAT", "C_REPEAT")
    client = _client([
        [_record("C_REPEAT", "SL", "TRIGGER_PENDING", Decimal("265.00"), Decimal("264.90"))],
        [_record("C_REPEAT", "LIMIT", "OPEN", target)],
    ])
    pipeline = _pipeline(mock_redis)
    event = _fill_event("M_REPEAT", fill=fill)

    await pipeline.execute_replication(event=event, clients={str(follower.id): client})
    second = await pipeline.execute_replication(event=event, clients={str(follower.id): client})

    assert second == {"status": "master_fill_updated"}   # existing branch: child already at target
    client.modify_order.assert_awaited_once()
    client.place_order.assert_not_called()
    client.cancel_order.assert_not_called()
    rows = (await db_session.execute(select(Order).where(Order.master_broker_order_id == "M_REPEAT"))).scalars().all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# 12. Child triggers on its own around the same time: no duplicate, the
#     broker's executed state is kept and the CopyTrade synced.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_child_triggers_independently_during_modify(db_session, mock_redis, sample_accounts):
    follower = sample_accounts["follower1"]
    row = await _child(db_session, follower.id, "M_RACE", "C_RACE")
    db_session.add(CopyTrade(
        id=uuid.uuid4(), master_order_id="M_RACE", follower_account_id=follower.id,
        copy_event_id=uuid.uuid4(), symbol="ITC", exchange="NSE", side=OrderSide.BUY,
        quantity=1, product_type="MIS", status=CopyTradeStatus.PENDING,
    ))
    await db_session.commit()
    client = _client(
        [
            [_record("C_RACE", "SL", "TRIGGER_PENDING", Decimal("265.00"), Decimal("264.90"))],
            [_record("C_RACE", "LIMIT", "COMPLETE", Decimal("264.95"), Decimal("264.90"), filled_quantity=1)],
        ],
        modify_result=_result(OrderResultCategory.REJECTED, "order already executed"),
    )

    res = await _pipeline(mock_redis).execute_replication(
        event=_fill_event("M_RACE"), clients={str(follower.id): client},
    )

    assert res["status"] == "completed"
    assert res["executions"][str(follower.id)] == "EXECUTED: BROKER_COMPLETE"
    persisted = await _row(db_session, row.id)
    assert persisted.status == OrderStatus.COMPLETE
    assert persisted.filled_quantity == 1
    assert persisted.broker_order_id == "C_RACE"
    trade = (await db_session.execute(select(CopyTrade).where(CopyTrade.master_order_id == "M_RACE"))).scalar_one()
    assert trade.status == CopyTradeStatus.SUCCESS
    assert await _events(db_session, "CHILD_MODIFICATION_FAILED") == []   # executed is not a failure
    client.place_order.assert_not_called()
    client.cancel_order.assert_not_called()


# ---------------------------------------------------------------------------
# 13. Read-back after the modify. Alice Blue keeps the old slTriggerPrice on
#     a converted row (confirmed live, 2026-10-05, child 26100500126150), so
#     the trigger value is not evidence either way: status, order type and
#     price decide.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["BUY", "SELL"])
async def test_open_limit_at_target_with_retained_trigger_is_accepted(db_session, mock_redis, sample_accounts, side):
    """The live false alarm: OPEN / LIMIT / target price with the old trigger
    still reported is a successful conversion, not CHILD_MODIFICATION_FAILED."""
    follower = sample_accounts["follower1"]
    fill = Decimal("265.20")
    target = compute_marketable_limit_price(side, fill)
    broker_side = BrokerOrderSide.BUY if side == "BUY" else BrokerOrderSide.SELL
    master_id, child_id = f"M_KEEP_{side}", f"C_KEEP_{side}"
    row = await _child(db_session, follower.id, master_id, child_id,
                       side=OrderSide.BUY if side == "BUY" else OrderSide.SELL)
    client = _client([
        [_record(child_id, "SL", "TRIGGER_PENDING", Decimal("265.00"), Decimal("264.90"), side=broker_side)],
        [_record(child_id, "LIMIT", "OPEN", target, Decimal("264.90"), side=broker_side)],
    ])

    res = await _pipeline(mock_redis).execute_replication(
        event=_fill_event(master_id, side=side, fill=fill), clients={str(follower.id): client},
    )

    assert res["status"] == "completed"
    assert res["executions"][str(follower.id)] == "MODIFIED: WORKING_MARKETABLE_LIMIT"
    client.modify_order.assert_awaited_once()
    assert client.modify_order.call_args.kwargs["broker_order_id"] == child_id
    client.place_order.assert_not_called()
    client.cancel_order.assert_not_called()
    persisted = await _row(db_session, row.id)
    assert persisted.broker_order_id == child_id
    assert persisted.order_type == "LIMIT"
    assert persisted.price == target
    assert persisted.status == OrderStatus.OPEN
    assert persisted.trigger_price == Decimal("264.90")   # stored as the broker reports it
    assert await _events(db_session, "CHILD_MODIFICATION_FAILED") == []


@pytest.mark.asyncio
async def test_open_limit_at_wrong_price_is_still_rejected(db_session, mock_redis, sample_accounts):
    """Child triggered on its own and rests at its own limit while our modify
    did not apply: OPEN / LIMIT but not at the target price."""
    follower = sample_accounts["follower1"]
    row = await _child(db_session, follower.id, "M_WRONGPX", "C_WRONGPX")
    client = _client([
        [_record("C_WRONGPX", "SL", "TRIGGER_PENDING", Decimal("265.00"), Decimal("264.90"))],
        [_record("C_WRONGPX", "LIMIT", "OPEN", Decimal("265.00"), Decimal("264.90"))],
    ])

    res = await _pipeline(mock_redis).execute_replication(
        event=_fill_event("M_WRONGPX"), clients={str(follower.id): client},
    )

    assert res["status"] == "completed_with_failures"
    assert res["executions"][str(follower.id)] == "FAILED: NOT_CONFIRMED"
    persisted = await _row(db_session, row.id)
    assert persisted.order_type == "SL"            # DB not updated on an unconfirmed modify
    assert persisted.status == OrderStatus.PENDING
    assert persisted.price == Decimal("265.00")
    assert len(await _events(db_session, "CHILD_MODIFICATION_FAILED")) == 1
    client.place_order.assert_not_called()
    client.cancel_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("after_type,after_status", [("SL", "TRIGGER_PENDING"), ("SL", "OPEN"), ("SLM", "TRIGGER_PENDING")])
async def test_child_still_sl_after_reported_success_is_still_rejected(
    db_session, mock_redis, sample_accounts, after_type, after_status,
):
    """Broker answered SUCCESS but the row is still an SL / still waiting on
    its trigger: never assumed converted, even at the target price."""
    follower = sample_accounts["follower1"]
    fill = Decimal("265.20")
    target = compute_marketable_limit_price("BUY", fill)
    master_id, child_id = f"M_STILL_{after_type}_{after_status}", f"C_STILL_{after_type}_{after_status}"
    row = await _child(db_session, follower.id, master_id, child_id)
    client = _client([
        [_record(child_id, "SL", "TRIGGER_PENDING", Decimal("265.00"), Decimal("264.90"))],
        [_record(child_id, after_type, after_status, target, Decimal("264.90"))],
    ])

    res = await _pipeline(mock_redis).execute_replication(
        event=_fill_event(master_id, fill=fill), clients={str(follower.id): client},
    )

    assert res["executions"][str(follower.id)] == "FAILED: NOT_CONFIRMED"
    persisted = await _row(db_session, row.id)
    assert persisted.order_type == "SL"
    assert persisted.status == OrderStatus.PENDING
    assert len(await _events(db_session, "CHILD_MODIFICATION_FAILED")) == 1


@pytest.mark.asyncio
async def test_complete_with_retained_trigger_is_unchanged(db_session, mock_redis, sample_accounts):
    """COMPLETE handling is untouched: executed under the same brokerOrderId
    is success whatever trigger the broker still reports (the three live
    conversions that filled before the read-back, 2026-10-05)."""
    follower = sample_accounts["follower1"]
    fill = Decimal("265.20")
    target = compute_marketable_limit_price("BUY", fill)
    row = await _child(db_session, follower.id, "M_DONE", "C_DONE")
    client = _client([
        [_record("C_DONE", "SL", "TRIGGER_PENDING", Decimal("265.00"), Decimal("264.90"))],
        [_record("C_DONE", "LIMIT", "COMPLETE", target, Decimal("264.90"), filled_quantity=1)],
    ])

    res = await _pipeline(mock_redis).execute_replication(
        event=_fill_event("M_DONE", fill=fill), clients={str(follower.id): client},
    )

    assert res["status"] == "completed"
    assert res["executions"][str(follower.id)] == "EXECUTED: BROKER_COMPLETE"
    persisted = await _row(db_session, row.id)
    assert persisted.status == OrderStatus.COMPLETE
    assert persisted.filled_quantity == 1
    assert persisted.order_type == "LIMIT"
    assert await _events(db_session, "CHILD_MODIFICATION_FAILED") == []
    client.modify_order.assert_awaited_once()
    client.place_order.assert_not_called()
    client.cancel_order.assert_not_called()


# ---------------------------------------------------------------------------
# Extra guards
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_triggered_child_stranded_at_its_own_limit_is_repriced(db_session, mock_redis, sample_accounts):
    """Child triggered on its own but sits OPEN at a limit the market moved
    past: repriced via the plain LIMIT -> LIMIT modify."""
    follower = sample_accounts["follower1"]
    fill = Decimal("265.20")
    target = compute_marketable_limit_price("BUY", fill)
    row = await _child(db_session, follower.id, "M_STRAND", "C_STRAND")
    client = _client([
        [_record("C_STRAND", "LIMIT", "OPEN", Decimal("265.00"))],
        [_record("C_STRAND", "LIMIT", "OPEN", target)],
    ])

    res = await _pipeline(mock_redis).execute_replication(
        event=_fill_event("M_STRAND", fill=fill), clients={str(follower.id): client},
    )

    assert res["executions"][str(follower.id)] == "MODIFIED: WORKING_MARKETABLE_LIMIT"
    assert client.modify_order.call_args.kwargs["price"] == target
    assert (await _row(db_session, row.id)).status == OrderStatus.OPEN


@pytest.mark.asyncio
async def test_risk_gate_failure_leaves_child_untouched(db_session, mock_redis, sample_accounts):
    follower = sample_accounts["follower1"]
    row = await _child(db_session, follower.id, "M_FUNDS", "C_FUNDS")
    client = _client([[_record("C_FUNDS", "SL", "TRIGGER_PENDING", Decimal("265.00"), Decimal("264.90"))]])
    client.get_funds.side_effect = RuntimeError("funds endpoint down")

    res = await _pipeline(mock_redis).execute_replication(
        event=_fill_event("M_FUNDS"), clients={str(follower.id): client},
    )

    assert res["executions"][str(follower.id)] == "REJECTED: FUNDS_FETCH_FAILED"
    client.modify_order.assert_not_called()
    assert (await _row(db_session, row.id)).order_type == "SL"


@pytest.mark.asyncio
async def test_sell_without_covering_long_is_blocked_when_bypass_off(db_session, mock_redis, sample_accounts, monkeypatch):
    from app.core.config import settings as app_settings
    monkeypatch.setattr(app_settings, "SELL_POSITION_SAFETY_BYPASS", False)
    follower = sample_accounts["follower1"]
    await _child(db_session, follower.id, "M_SELLBLK", "C_SELLBLK", side=OrderSide.SELL)
    client = _client(
        [[_record("C_SELLBLK", "SL", "TRIGGER_PENDING", Decimal("265.00"), Decimal("264.90"), side=BrokerOrderSide.SELL)]],
        long_qty=0,
    )

    res = await _pipeline(mock_redis).execute_replication(
        event=_fill_event("M_SELLBLK", side="SELL"), clients={str(follower.id): client},
    )

    assert res["executions"][str(follower.id)] == "SELL_BLOCKED: INSUFFICIENT_LONG_POSITION"
    client.modify_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type,status", [("ORDER_CANCELLED", "CANCELLED"), ("ORDER_REJECTED", "REJECTED")])
async def test_cancelled_or_rejected_master_never_enters_execution_path(
    db_session, mock_redis, sample_accounts, event_type, status,
):
    follower = sample_accounts["follower1"]
    await _child(db_session, follower.id, f"M_{status}", f"C_{status}")
    client = _client([[_record(f"C_{status}", "SL", "CANCELLED", Decimal("265.00"), Decimal("264.90"))]])
    client.cancel_order.return_value = _result(broker_order_id=f"C_{status}")

    await _pipeline(mock_redis).execute_replication(
        event=_fill_event(f"M_{status}", event_type=event_type, status=status),
        clients={str(follower.id): client},
    )

    client.modify_order.assert_not_called()
    client.place_order.assert_not_called()
