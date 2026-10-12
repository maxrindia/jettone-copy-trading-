"""
P1.1 regression coverage: LIMIT replication must run through the same
funds/RiskEngine pre-trade checks as MARKET replication before ever
submitting a follower broker order.

Mirrors the existing MARKET-side pattern in
tests/test_r3_hardening.py::test_funds_fetch_failure_rejects_copy and
tests/test_r4_runtime.py's daily-loss/exposure tests, driven through the
same public entrypoint (pipeline.execute_replication) so the whole
validator -> eligible-followers -> risk-gate -> broker-submit chain is
exercised exactly as production code runs it.

No live trading is enabled anywhere in this file: these are direct
CopyEnginePipeline unit/integration tests against mocked broker clients and
an in-memory DB, not the FastAPI app / ENGINE_LIVE_TRADING_ENABLED gate.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.accounts import CopySetting, QuantityMode
from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderSide
from app.services.alice_blue.types import FundsData, OrderResult, OrderResultCategory, Position
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from strategy_helpers import SAMPLE_MASTER_ACCOUNT_ID


def _make_limit_event(master_order_id="ORD_LIMIT_1", quantity=100, limit_price=Decimal("100.00")):
    return MasterTradeEvent(
        account_id=SAMPLE_MASTER_ACCOUNT_ID,
        master_order_id=master_order_id,
        symbol="RELIANCE",
        exchange="NSE",
        side="BUY",
        quantity=quantity,
        price=limit_price,
        limit_price=limit_price,
        product_type="MIS",
        timestamp=datetime.now(timezone.utc),
        fill_sequence=1,
        order_type="LIMIT",
        event_type="LIMIT_CREATED",
        status="OPEN",
    )


def _make_funds(available=Decimal("500000")):
    return FundsData(
        account_id="a",
        available_margin=available,
        collateral=Decimal("0"),
        used_margin=Decimal("0"),
        updated_at=datetime.now(timezone.utc),
    )


def _success_result(broker_order_id="BRK_LIMIT_OK"):
    return OrderResult(
        category=OrderResultCategory.SUCCESS,
        client_order_id=None,
        broker_order_id=broker_order_id,
        message="Order placed successfully",
        raw_response={"stat": "Ok"},
        timestamp=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_insufficient_margin_prevents_limit_broker_submission(db_session, sample_accounts, sample_strategy):
    """Funds ARE fetched successfully but are too low for the order value ->
    RiskEngine's INSUFFICIENT_MARGIN check must block the LIMIT submission."""
    follower = sample_accounts["follower1"]
    db_session.add(
        CopySetting(
            follower_account_id=follower.id,
            copy_enabled=True,
            quantity_mode=QuantityMode.FIXED,
            fixed_qty=100,
        )
    )
    await db_session.commit()

    client = AsyncMock()
    client.get_funds.return_value = _make_funds(available=Decimal("10"))  # far below 100*100=10,000

    validator = EventValidator(redis=AsyncMock(set=AsyncMock(return_value=True)))
    pipeline = CopyEnginePipeline(validator=validator)

    fid = str(follower.id)
    res = await pipeline.execute_replication(event=_make_limit_event(), clients={fid: client})

    assert res["results"][fid]["status"] == "REJECTED"
    assert "INSUFFICIENT_MARGIN" in res["results"][fid]["reasons"]
    client.place_order.assert_not_called()

    # Consistent with MARKET risk-rejection: no Order/CopyTrade row pre-written.
    orders = (await db_session.execute(select(Order).where(Order.account_id == follower.id))).scalars().all()
    trades = (await db_session.execute(select(CopyTrade).where(CopyTrade.follower_account_id == follower.id))).scalars().all()
    assert orders == []
    assert trades == []


@pytest.mark.asyncio
async def test_max_trade_value_breach_prevents_limit_broker_submission(db_session, sample_accounts, sample_strategy):
    follower = sample_accounts["follower1"]
    db_session.add(
        CopySetting(
            follower_account_id=follower.id,
            copy_enabled=True,
            quantity_mode=QuantityMode.FIXED,
            fixed_qty=100,  # order value will be 100 * 100.00 = 10,000
            max_trade_value=Decimal("5000"),
        )
    )
    await db_session.commit()

    client = AsyncMock()
    client.get_funds.return_value = _make_funds()

    validator = EventValidator(redis=AsyncMock(set=AsyncMock(return_value=True)))
    pipeline = CopyEnginePipeline(validator=validator)

    fid = str(follower.id)
    res = await pipeline.execute_replication(event=_make_limit_event(), clients={fid: client})

    assert res["results"][fid]["status"] == "REJECTED"
    assert "MAX_TRADE_VALUE_BREACHED" in res["results"][fid]["reasons"]
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_max_daily_loss_breach_prevents_limit_broker_submission(db_session, sample_accounts, sample_strategy):
    follower = sample_accounts["follower1"]
    db_session.add(
        CopySetting(
            follower_account_id=follower.id,
            copy_enabled=True,
            quantity_mode=QuantityMode.FIXED,
            max_daily_loss=Decimal("1000"),
        )
    )
    # Prior SUCCESSFUL BUY today already deployed 5,000 of capital -> counted as today's loss exposure.
    db_session.add(
        CopyTrade(
            master_order_id="ORD_EARLIER_TODAY",
            follower_account_id=follower.id,
            copy_event_id=uuid.uuid4(),
            symbol="TCS",
            exchange="NSE",
            side=OrderSide.BUY,
            quantity=50,
            product_type="MIS",
            estimated_price=Decimal("100.00"),
            status=CopyTradeStatus.SUCCESS,
            completed_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    client = AsyncMock()
    client.get_funds.return_value = _make_funds()

    validator = EventValidator(redis=AsyncMock(set=AsyncMock(return_value=True)))
    pipeline = CopyEnginePipeline(validator=validator)

    fid = str(follower.id)
    res = await pipeline.execute_replication(event=_make_limit_event(), clients={fid: client})

    assert res["results"][fid]["status"] == "REJECTED"
    assert "MAX_DAILY_LOSS_BREACHED" in res["results"][fid]["reasons"]
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_max_exposure_breach_prevents_limit_broker_submission(db_session, sample_accounts, sample_strategy):
    follower = sample_accounts["follower1"]
    db_session.add(
        CopySetting(
            follower_account_id=follower.id,
            copy_enabled=True,
            quantity_mode=QuantityMode.FIXED,
            fixed_qty=100,
            max_exposure=Decimal("5000"),
        )
    )
    await db_session.commit()

    client = AsyncMock()
    client.get_funds.return_value = _make_funds()
    # Existing open exposure already at 4,000; +10,000 new order value breaches 5,000 cap.
    client.get_positions.return_value = [
        Position(symbol="INFY", quantity=40, side="BUY", average_price=Decimal("100.00"), account_id=str(follower.id)),
    ]

    validator = EventValidator(redis=AsyncMock(set=AsyncMock(return_value=True)))
    pipeline = CopyEnginePipeline(validator=validator)

    fid = str(follower.id)
    res = await pipeline.execute_replication(event=_make_limit_event(), clients={fid: client})

    assert res["results"][fid]["status"] == "REJECTED"
    assert "MAX_EXPOSURE_BREACHED" in res["results"][fid]["reasons"]
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_funds_fetch_failure_fails_closed_for_limit_order(db_session, sample_accounts, sample_strategy):
    """No CopySetting needed: the funds check runs unconditionally, before
    the RiskEngine block, for every follower."""
    follower = sample_accounts["follower1"]

    client = AsyncMock()
    client.get_funds.side_effect = RuntimeError("broker unreachable")

    validator = EventValidator(redis=AsyncMock(set=AsyncMock(return_value=True)))
    pipeline = CopyEnginePipeline(validator=validator)

    fid = str(follower.id)
    res = await pipeline.execute_replication(event=_make_limit_event(), clients={fid: client})

    assert res["results"][fid]["status"] == "REJECTED"
    assert "FUNDS_FETCH_FAILED" in res["results"][fid]["reasons"]
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_risk_inputs_unavailable_fails_closed_for_limit_order(db_session, sample_accounts, sample_strategy):
    """Funds succeed, but open-exposure lookup (broker positions call) blows
    up -> the LIMIT order must still not be placed."""
    follower = sample_accounts["follower1"]
    db_session.add(
        CopySetting(
            follower_account_id=follower.id,
            copy_enabled=True,
            quantity_mode=QuantityMode.FIXED,
        )
    )
    await db_session.commit()

    client = AsyncMock()
    client.get_funds.return_value = _make_funds()
    client.get_positions.side_effect = RuntimeError("positions endpoint down")

    validator = EventValidator(redis=AsyncMock(set=AsyncMock(return_value=True)))
    pipeline = CopyEnginePipeline(validator=validator)

    fid = str(follower.id)
    res = await pipeline.execute_replication(event=_make_limit_event(), clients={fid: client})

    assert res["results"][fid]["status"] == "REJECTED"
    assert "RISK_INPUTS_UNAVAILABLE" in res["results"][fid]["reasons"]
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_allowed_limit_order_reaches_broker_exactly_once(db_session, sample_accounts, sample_strategy):
    """Everything within limits -> the follower LIMIT order is submitted
    exactly once, and the persisted Order reflects the broker's response."""
    follower = sample_accounts["follower1"]
    db_session.add(
        CopySetting(
            follower_account_id=follower.id,
            copy_enabled=True,
            quantity_mode=QuantityMode.FIXED,
            max_trade_value=Decimal("50000"),
            max_daily_loss=Decimal("50000"),
            max_exposure=Decimal("50000"),
        )
    )
    await db_session.commit()

    client = AsyncMock()
    client.get_funds.return_value = _make_funds()
    client.get_positions.return_value = []
    client.place_order.return_value = _success_result()

    validator = EventValidator(redis=AsyncMock(set=AsyncMock(return_value=True)))
    pipeline = CopyEnginePipeline(validator=validator)

    fid = str(follower.id)
    res = await pipeline.execute_replication(event=_make_limit_event(), clients={fid: client})

    assert res["status"] == "completed"
    assert res["results"][fid]["status"] == "SUCCESS"
    client.place_order.assert_awaited_once()

    row = (await db_session.execute(select(Order).where(Order.account_id == follower.id))).scalar_one()
    assert row.broker_order_id == "BRK_LIMIT_OK"
    assert row.order_type == "LIMIT"
