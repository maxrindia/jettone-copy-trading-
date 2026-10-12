"""
Issue #6 Finding 3 regression coverage: manual follower square-off
(POST /strategies/{id}/members/{account_id}/square-off) placed a real
broker MARKET order but never persisted it into the Order table, silently
desynchronizing that follower's tracked position from broker reality and
producing a permanent, recurring ReconciliationEngine mismatch.

The fix:
- AliceBlueClient.square_off_position() now returns
  (OrderResult, target_side, target_qty) instead of just OrderResult, and
  gained an optional client_order_id param - both backward compatible
  (the pre-existing single caller's positional/keyword usage without
  client_order_id behaves identically).
- square_off_child_position() pre-writes a SYSTEM Order (PENDING) before
  contacting the broker using that exact target_side/qty/client_order_id,
  then finalizes it to COMPLETE/REJECTED after the broker responds. A
  broker SUCCESS followed by a DB persistence failure is handled fail-open
  (never reports the trade as failed) but loudly (CRITICAL log +
  SQUARE_OFF_PERSISTENCE_FAILED SystemEvent).

No live trading is enabled and no real broker calls are made anywhere in
this file - AliceBlueClient.place_order is monkeypatched.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.strategies import SquareOffRequest, square_off_child_position
from app.core.crypto import encrypt_secret
from app.models.accounts import Account, AccountType, ApiCredential
from app.models.audit import SystemEvent
from app.models.strategies import GroupStrategy, StrategyMember
from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.types import OrderResult, OrderResultCategory
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.reliability.reconciliation import ReconciliationEngine


async def _setup_follower_strategy(db_session, alice_user_id="AB_SQ_1"):
    master = Account(
        id=uuid.uuid4(), account_type=AccountType.MASTER, display_name="Master",
        alice_user_id=f"{alice_user_id}_M", is_active=True,
    )
    follower = Account(
        id=uuid.uuid4(), account_type=AccountType.FOLLOWER, display_name="Follower",
        alice_user_id=alice_user_id, is_active=True,
    )
    db_session.add(master)
    db_session.add(follower)
    await db_session.commit()

    db_session.add(ApiCredential(
        id=uuid.uuid4(), account_id=follower.id,
        encrypted_api_secret=encrypt_secret("dummy-secret"),
    ))
    await db_session.commit()

    strategy = GroupStrategy(
        id=uuid.uuid4(), name="SquareOff Test Strategy",
        master_account_id=master.id, trading_enabled=True,
    )
    db_session.add(strategy)
    await db_session.commit()

    member = StrategyMember(
        id=uuid.uuid4(), strategy_id=strategy.id, account_id=follower.id,
        multiplier=Decimal("1.0"), trading_enabled=True,
    )
    db_session.add(member)
    await db_session.commit()

    await db_session.refresh(follower)
    return master, follower, strategy, member


def _broker_success(broker_order_id="BRK_SQOFF_1"):
    async def _place_order(self, req):
        return OrderResult(
            category=OrderResultCategory.SUCCESS,
            client_order_id=req.client_order_id,
            broker_order_id=broker_order_id,
            message="Order placed successfully",
            raw_response={"stat": "Ok"},
            timestamp=datetime.now(timezone.utc),
        )
    return _place_order


def _broker_rejected(message="EC965: Market orders are not allowed"):
    async def _place_order(self, req):
        return OrderResult(
            category=OrderResultCategory.REJECTED,
            client_order_id=req.client_order_id,
            broker_order_id=None,
            message=message,
            raw_response={"emsg": message},
            timestamp=datetime.now(timezone.utc),
        )
    return _place_order


@pytest.mark.asyncio
async def test_A_square_off_buy_position_persists_sell_order_complete(db_session, mock_redis, monkeypatch):
    master, follower, strategy, member = await _setup_follower_strategy(db_session)
    monkeypatch.setattr(AliceBlueClient, "place_order", _broker_success("BRK_SQOFF_A"))

    payload = SquareOffRequest(symbol="reliance", exchange="nse", quantity=100, side="BUY", product_type="MIS")
    resp = await square_off_child_position(
        strategy_id=strategy.id, account_id=follower.id, payload=payload, db=db_session, redis=mock_redis,
    )

    assert resp["status"] == "success"
    assert resp["broker_order_id"] == "BRK_SQOFF_A"

    stmt = select(Order).where(Order.account_id == follower.id, Order.source == OrderSource.SYSTEM)
    orders = (await db_session.execute(stmt)).scalars().all()
    assert len(orders) == 1
    order = orders[0]
    assert order.symbol == "RELIANCE"
    assert order.side == OrderSide.SELL  # opposite of the BUY position being closed
    assert order.quantity == 100
    assert order.filled_quantity == 100
    assert order.pending_quantity == 0
    assert order.status == OrderStatus.COMPLETE
    assert order.master_broker_order_id is None
    assert order.broker_order_id == "BRK_SQOFF_A"


@pytest.mark.asyncio
async def test_B_square_off_sell_position_persists_buy_order(db_session, mock_redis, monkeypatch):
    master, follower, strategy, member = await _setup_follower_strategy(db_session, alice_user_id="AB_SQ_2")
    monkeypatch.setattr(AliceBlueClient, "place_order", _broker_success("BRK_SQOFF_B"))

    payload = SquareOffRequest(symbol="TCS", exchange="NSE", quantity=50, side="SELL", product_type="MIS")
    resp = await square_off_child_position(
        strategy_id=strategy.id, account_id=follower.id, payload=payload, db=db_session, redis=mock_redis,
    )
    assert resp["status"] == "success"

    stmt = select(Order).where(Order.account_id == follower.id, Order.source == OrderSource.SYSTEM)
    order = (await db_session.execute(stmt)).scalars().one()
    assert order.side == OrderSide.BUY  # opposite of the SELL position being closed
    assert order.quantity == 50
    assert order.filled_quantity == 50


@pytest.mark.asyncio
async def test_C_square_off_rejected_by_broker_not_left_pending(db_session, mock_redis, monkeypatch):
    master, follower, strategy, member = await _setup_follower_strategy(db_session, alice_user_id="AB_SQ_3")
    monkeypatch.setattr(AliceBlueClient, "place_order", _broker_rejected())

    payload = SquareOffRequest(symbol="INFY", exchange="NSE", quantity=20, side="BUY", product_type="MIS")

    with pytest.raises(HTTPException) as exc_info:
        await square_off_child_position(
            strategy_id=strategy.id, account_id=follower.id, payload=payload, db=db_session, redis=mock_redis,
        )
    assert exc_info.value.status_code == 400

    stmt = select(Order).where(Order.account_id == follower.id, Order.source == OrderSource.SYSTEM)
    order = (await db_session.execute(stmt)).scalars().one()
    assert order.status != OrderStatus.PENDING
    assert order.status == OrderStatus.REJECTED
    assert order.filled_quantity == 0


@pytest.mark.asyncio
async def test_D_reconciliation_reflects_closed_position_after_square_off(db_session, mock_redis, monkeypatch):
    master, follower, strategy, member = await _setup_follower_strategy(db_session, alice_user_id="AB_SQ_4")

    # Pre-existing held position: 100 shares bought via prior copy replication.
    db_session.add(Order(
        id=uuid.uuid4(),
        internal_order_id="JT_CH_PRIOR_HOLD",
        broker_order_id="BRK_PRIOR_HOLD",
        account_id=follower.id,
        source=OrderSource.SYSTEM,
        exchange="NSE",
        symbol="WIPRO",
        side=OrderSide.BUY,
        order_type="MARKET",
        product_type="MIS",
        quantity=100,
        filled_quantity=100,
        pending_quantity=0,
        price=Decimal("400.00"),
        status=OrderStatus.COMPLETE,
    ))
    await db_session.commit()

    positions_before = await ReconciliationEngine.compute_expected_positions(follower.id, db_session)
    assert positions_before.get("WIPRO") == 100

    monkeypatch.setattr(AliceBlueClient, "place_order", _broker_success("BRK_SQOFF_D"))
    payload = SquareOffRequest(symbol="WIPRO", exchange="NSE", quantity=100, side="BUY", product_type="MIS")
    await square_off_child_position(
        strategy_id=strategy.id, account_id=follower.id, payload=payload, db=db_session, redis=mock_redis,
    )

    positions_after = await ReconciliationEngine.compute_expected_positions(follower.id, db_session)
    assert positions_after.get("WIPRO", 0) == 0


@pytest.mark.asyncio
async def test_E_db_persistence_failure_after_broker_success_fails_open(db_session, mock_redis, monkeypatch):
    master, follower, strategy, member = await _setup_follower_strategy(db_session, alice_user_id="AB_SQ_5")
    monkeypatch.setattr(AliceBlueClient, "place_order", _broker_success("BRK_SQOFF_E"))

    original_save = CopyEnginePipeline.save_or_update_order

    async def failing_finalize(self, *args, **kwargs):
        if kwargs.get("status") == OrderStatus.COMPLETE:
            raise RuntimeError("simulated DB failure")
        return await original_save(self, *args, **kwargs)

    monkeypatch.setattr(CopyEnginePipeline, "save_or_update_order", failing_finalize)

    payload = SquareOffRequest(symbol="HDFC", exchange="NSE", quantity=10, side="BUY", product_type="MIS")
    resp = await square_off_child_position(
        strategy_id=strategy.id, account_id=follower.id, payload=payload, db=db_session, redis=mock_redis,
    )

    # Broker already succeeded - must still report success, never a false failure.
    assert resp["status"] == "success"
    assert resp["broker_order_id"] == "BRK_SQOFF_E"

    from app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        events = (await session.execute(
            select(SystemEvent).where(SystemEvent.event_type == "SQUARE_OFF_PERSISTENCE_FAILED")
        )).scalars().all()
    assert len(events) == 1
    assert events[0].account_id == follower.id
    assert events[0].metadata_payload["broker_order_id"] == "BRK_SQOFF_E"
    assert events[0].metadata_payload["symbol"] == "HDFC"
    assert events[0].metadata_payload["quantity"] == 10


@pytest.mark.asyncio
async def test_F_existing_caller_still_works_without_client_order_id(monkeypatch):
    """Direct unit test of AliceBlueClient.square_off_position in isolation,
    confirming the pre-existing (and only) caller's usage pattern - no
    client_order_id passed - still behaves identically to before."""
    client = AliceBlueClient(account_id="acc1", user_id="U1", api_secret="x", redis=AsyncMock())
    monkeypatch.setattr(AliceBlueClient, "place_order", _broker_success("BRK_COMPAT"))

    result, target_side, qty = await client.square_off_position(
        symbol="reliance", exchange="nse", quantity=100, side="BUY", product="MIS",
    )
    assert result.category == OrderResultCategory.SUCCESS
    assert target_side.value == "SELL"
    assert qty == 100
    assert result.client_order_id.startswith("SQ_OFF_")

    result2, target_side2, qty2 = await client.square_off_position(
        symbol="tcs", exchange="nse", quantity=50, side="SELL", product="MIS",
    )
    assert target_side2.value == "BUY"
    assert qty2 == 50

    # quantity == 0 still rejected exactly as before.
    result3, _, qty3 = await client.square_off_position(
        symbol="infy", exchange="nse", quantity=0, side="BUY", product="MIS",
    )
    assert result3.category == OrderResultCategory.REJECTED
    assert qty3 == 0
