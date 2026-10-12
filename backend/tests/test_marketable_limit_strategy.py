"""
Marketable LIMIT execution strategy regression coverage.

Root cause (confirmed via live production evidence + Alice Blue community
reports, not guessed): Alice Blue rejects MARKET orders placed through the
A3 API/vendor channel with EC965 "Market orders are not allowed", while the
same account places MARKET orders successfully via Alice Blue's own Web/
Mobile app. This is a broker/vendor-channel restriction, not a Jettone
payload bug (the payload was verified field-by-field against Alice Blue's
official docs with zero deviation).

CopyEnginePipeline._process_single_market_follower now converts a master's
CONFIRMED MARKET fill (never a mere acceptance - MasterTradeDetector only
emits a replication event on COMPLETE/PARTIAL broker status) into an
aggressive LIMIT order per follower:
  BUY:  price = master_fill_price * (1 + buffer/100)
  SELL: price = master_fill_price * (1 - buffer/100)
governed by settings.MARKET_ORDER_EXECUTION_MODE ("MARKETABLE_LIMIT" by
default) and settings.MARKET_ORDER_BUFFER_PERCENT (default 0, i.e. exactly
the master fill price). Any other
execution mode value preserves the original direct-MARKET behavior
unchanged (explicit rollback switch).

No live trading is enabled and no real broker calls are made anywhere in
this file - all broker clients are AsyncMock test doubles. Nothing in this
file can reach a real broker even if run against the live server: every
client is a mock, and the market being open or closed has no bearing on it.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.accounts import Account, AccountType
from app.models.audit import SystemEvent
from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.types import (
    FundsData,
    OrderResult,
    OrderResultCategory,
    OrderType,
    Position,
)
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from strategy_helpers import SAMPLE_MASTER_ACCOUNT_ID, add_child


def _market_event(
    quantity=100,
    price=Decimal("1000.00"),
    side="BUY",
    master_order_id="ORD_MKT_1",
    fill_sequence=None,
):
    return MasterTradeEvent(
        account_id=SAMPLE_MASTER_ACCOUNT_ID,
        master_order_id=master_order_id,
        symbol="RELIANCE", exchange="NSE", side=side, quantity=quantity,
        price=price, product_type="MIS",
        timestamp=datetime.now(timezone.utc), fill_sequence=fill_sequence or quantity,
        order_type="MARKET", event_type="MARKET_ACCEPTED", status="COMPLETE",
        total_quantity=quantity, filled_quantity=quantity, pending_quantity=0,
    )


def _limit_event(quantity=10, price=Decimal("2950.00"), master_order_id="ORD_LIM_UNAFFECTED"):
    return MasterTradeEvent(
        account_id=SAMPLE_MASTER_ACCOUNT_ID,
        master_order_id=master_order_id,
        symbol="RELIANCE", exchange="NSE", side="BUY", quantity=quantity,
        price=price, limit_price=price, product_type="MIS",
        timestamp=datetime.now(timezone.utc), fill_sequence=1,
        order_type="LIMIT", event_type="LIMIT_CREATED", status="OPEN",
    )


def _funds(available=Decimal("500000")):
    return FundsData(
        account_id="a", available_margin=available, collateral=Decimal("0"),
        used_margin=Decimal("0"), updated_at=datetime.now(timezone.utc),
    )


def _success_result(broker_order_id="BRK_OK"):
    return OrderResult(
        category=OrderResultCategory.SUCCESS, client_order_id=None, broker_order_id=broker_order_id,
        message="ok", raw_response={"stat": "Ok"}, timestamp=datetime.now(timezone.utc),
    )


def _allow_sell(client, redis, quantity, symbol="RELIANCE-EQ"):
    """Gives a mock client a covering LONG position so a follower SELL passes
    the position-safety gate. BUY replication never consults positions, so
    only the SELL cases here need it."""
    client.redis = redis
    positions = [
        Position(
            symbol=symbol, quantity=quantity, side="BUY",
            average_price=Decimal("1000.00"), account_id="acc",
            exchange="NSE", product_type="MIS",
        )
    ]
    client.get_positions.return_value = positions
    # get_available_long() reads get_positions_or_none(); mirror it here too.
    client.get_positions_or_none.return_value = positions


@pytest.fixture(autouse=True)
def _reset_market_order_settings():
    """Every test starts from the real defaults and restores them after,
    regardless of what an individual test monkeypatches."""
    orig_mode = settings.MARKET_ORDER_EXECUTION_MODE
    orig_buffer = settings.MARKET_ORDER_BUFFER_PERCENT
    yield
    settings.MARKET_ORDER_EXECUTION_MODE = orig_mode
    settings.MARKET_ORDER_BUFFER_PERCENT = orig_buffer


async def _events_of_type(event_type):
    from app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(select(SystemEvent).where(SystemEvent.event_type == event_type))
        ).scalars().all()


# ---------------------------------------------------------------------------
# 1-2. BUY / SELL marketable LIMIT conversion
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_market_buy_converts_to_marketable_limit_buy(db_session, mock_redis, sample_accounts, sample_strategy):
    assert settings.MARKET_ORDER_EXECUTION_MODE == "MARKETABLE_LIMIT"
    assert settings.MARKET_ORDER_BUFFER_PERCENT == Decimal("0")
    follower = sample_accounts["follower1"]
    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.place_order.return_value = _success_result("BRK_BUY_1")

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    event = _market_event(quantity=100, price=Decimal("125.83"), side="BUY")
    res = await pipeline.execute_replication(event=event, clients={str(follower.id): client})

    assert res["status"] == "completed"
    assert res["results"][str(follower.id)]["status"] == OrderResultCategory.SUCCESS.value

    sent_order = client.place_order.call_args.args[0]
    assert sent_order.order_type == OrderType.LIMIT
    assert sent_order.price == Decimal("125.83")  # exactly the master fill price, 0% buffer
    assert sent_order.quantity == 100


@pytest.mark.asyncio
async def test_market_sell_converts_to_marketable_limit_sell(db_session, mock_redis, sample_accounts, sample_strategy):
    assert settings.MARKET_ORDER_EXECUTION_MODE == "MARKETABLE_LIMIT"
    assert settings.MARKET_ORDER_BUFFER_PERCENT == Decimal("0")
    follower = sample_accounts["follower1"]
    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.place_order.return_value = _success_result("BRK_SELL_1")
    # A follower SELL now requires a covering LONG position (see
    # test_sell_position_safety.py). This test is about PRICING, so give it
    # a covering position to keep it an ALLOWED sell - the pricing assertions
    # below are unchanged.
    _allow_sell(client, mock_redis, quantity=100)

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    event = _market_event(quantity=100, price=Decimal("125.89"), side="SELL", master_order_id="ORD_MKT_SELL")
    res = await pipeline.execute_replication(event=event, clients={str(follower.id): client})

    assert res["status"] == "completed"
    sent_order = client.place_order.call_args.args[0]
    assert sent_order.order_type == OrderType.LIMIT
    assert sent_order.price == Decimal("125.89")  # exactly the master fill price, 0% buffer


# ---------------------------------------------------------------------------
# 3. Configurable buffer
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "side,expected_price",
    [("BUY", Decimal("1010.00")), ("SELL", Decimal("990.00"))],  # 1000 * 1.01 / 1000 * 0.99
)
@pytest.mark.asyncio
async def test_buffer_percent_is_configurable(db_session, mock_redis, sample_accounts, sample_strategy, side, expected_price):
    settings.MARKET_ORDER_BUFFER_PERCENT = Decimal("1.0")
    follower = sample_accounts["follower1"]
    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.place_order.return_value = _success_result("BRK_BUFFER_1")
    if side == "SELL":
        _allow_sell(client, mock_redis, quantity=10)

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    event = _market_event(quantity=10, price=Decimal("1000.00"), side=side, master_order_id=f"ORD_MKT_BUFFER_{side}")
    await pipeline.execute_replication(event=event, clients={str(follower.id): client})

    sent_order = client.place_order.call_args.args[0]
    assert sent_order.order_type == OrderType.LIMIT
    assert sent_order.price == expected_price


# ---------------------------------------------------------------------------
# 4. Decimal precision
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_price_calculation_uses_decimal_and_two_place_rounding(db_session, mock_redis, sample_accounts, sample_strategy):
    settings.MARKET_ORDER_BUFFER_PERCENT = Decimal("0.5")  # non-zero, so there is something to round
    follower = sample_accounts["follower1"]
    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.place_order.return_value = _success_result("BRK_DECIMAL_1")

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    # 333.33 * 1.005 = 335.00465 -> quantized to 335.00
    event = _market_event(quantity=10, price=Decimal("333.33"), side="BUY", master_order_id="ORD_MKT_DECIMAL")
    await pipeline.execute_replication(event=event, clients={str(follower.id): client})

    sent_order = client.place_order.call_args.args[0]
    assert isinstance(sent_order.price, Decimal)
    assert sent_order.price == Decimal("335.00")


# ---------------------------------------------------------------------------
# 5/8. Execution mode fallback preserves the original direct-MARKET behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_direct_market_mode_preserves_original_behavior(db_session, mock_redis, sample_accounts, sample_strategy):
    settings.MARKET_ORDER_EXECUTION_MODE = "DIRECT_MARKET"
    follower = sample_accounts["follower1"]
    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.place_order.return_value = _success_result("BRK_DIRECT_1")

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    event = _market_event(quantity=10, price=Decimal("1000.00"), side="BUY", master_order_id="ORD_MKT_DIRECT")
    await pipeline.execute_replication(event=event, clients={str(follower.id): client})

    sent_order = client.place_order.call_args.args[0]
    assert sent_order.order_type == OrderType.MARKET
    assert sent_order.price == Decimal("0")


# ---------------------------------------------------------------------------
# 9. Existing Master LIMIT flow is completely unaffected
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_master_limit_flow_never_touches_marketable_limit_logic(db_session, mock_redis, sample_accounts, sample_strategy):
    """A Master LIMIT order must still be replicated at its own exact limit
    price via replicate_limit_order - never converted, never buffered."""
    follower = sample_accounts["follower1"]
    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.place_order.return_value = _success_result("BRK_LIMIT_UNAFFECTED")

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    event = _limit_event(quantity=10, price=Decimal("2950.00"))
    res = await pipeline.execute_replication(event=event, clients={str(follower.id): client})

    assert res["status"] == "completed"
    sent_order = client.place_order.call_args.args[0]
    assert sent_order.order_type == OrderType.LIMIT
    assert sent_order.price == Decimal("2950.00")  # exact master limit price, no buffer applied


# ---------------------------------------------------------------------------
# 10. Master MARKET is never copied before an actual confirmed fill
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_market_order_open_status_does_not_trigger_replication():
    """MasterTradeDetector must not emit a replication event for a MARKET
    order that has only been accepted (status OPEN), not yet filled."""
    from app.services.alice_blue.types import OrderUpdateEvent
    from app.services.copy_engine.detector import MasterTradeDetector

    detector = MasterTradeDetector(master_account_id="master-1")
    open_event = OrderUpdateEvent(
        broker_order_id="BRK_OPEN_1", client_order_id=None, symbol="RELIANCE",
        side="BUY", quantity=10, filled_quantity=0, fill_price=Decimal("0"),
        status="OPEN", timestamp=datetime.now(timezone.utc), account_id="master-1",
        product_type="MIS", order_type="MARKET", price=Decimal("0"), trigger_price=Decimal("0"),
        exchange="NSE",
    )
    trade_event = detector.process_order_update(open_event)
    assert trade_event is None


# ---------------------------------------------------------------------------
# 12. Multiple followers processed independently; one failure doesn't stop others
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_one_follower_client_error_does_not_block_others(db_session, mock_redis, sample_accounts, sample_strategy):
    follower1 = sample_accounts["follower1"]
    follower2 = Account(
        id=uuid.uuid4(), account_type=AccountType.FOLLOWER,
        display_name="Follower 2", alice_user_id="AB_MKT_F2", is_active=True,
    )
    db_session.add(follower2)
    await db_session.commit()
    await add_child(db_session, sample_strategy, follower2)

    failing_client = AsyncMock()
    failing_client.get_funds.side_effect = RuntimeError("broker unreachable")

    working_client = AsyncMock()
    working_client.get_funds.return_value = _funds()
    working_client.place_order.return_value = _success_result("BRK_MULTI_OK")

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    event = _market_event(quantity=10, price=Decimal("1000.00"), master_order_id="ORD_MKT_MULTI")
    res = await pipeline.execute_replication(
        event=event,
        clients={str(follower1.id): failing_client, str(follower2.id): working_client},
    )

    assert res["status"] == "completed"
    assert res["results"][str(follower1.id)]["reasons"] == ["FUNDS_FETCH_FAILED"]
    assert res["results"][str(follower2.id)]["status"] == OrderResultCategory.SUCCESS.value
    working_client.place_order.assert_awaited_once()


# ---------------------------------------------------------------------------
# 13. Risk Engine rejection still applies - not bypassed by the new path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_risk_engine_rejection_prevents_order_placement(db_session, mock_redis, sample_accounts, sample_strategy, sample_copy_settings):
    follower = sample_accounts["follower1"]
    client = AsyncMock()
    # Tiny margin -> RiskEngine should reject on insufficient funds/exposure
    # for a 100-share order at ~1000/share, regardless of order type.
    client.get_funds.return_value = _funds(available=Decimal("1"))
    client.place_order.return_value = _success_result("BRK_SHOULD_NOT_BE_CALLED")

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    event = _market_event(quantity=100, price=Decimal("1000.00"), master_order_id="ORD_MKT_RISK")
    res = await pipeline.execute_replication(event=event, clients={str(follower.id): client})

    result = res["results"][str(follower.id)]
    assert result["status"] == "REJECTED"
    client.place_order.assert_not_awaited()


# ---------------------------------------------------------------------------
# 14. Reference price (master fill price) unavailable -> fail closed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_missing_reference_price_fails_closed_without_placing_order(db_session, mock_redis, sample_accounts, sample_strategy):
    follower = sample_accounts["follower1"]
    client = AsyncMock()
    client.get_funds.return_value = _funds()

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    event = _market_event(quantity=10, price=Decimal("0"), master_order_id="ORD_MKT_NO_PRICE")
    res = await pipeline.execute_replication(event=event, clients={str(follower.id): client})

    result = res["results"][str(follower.id)]
    assert result["status"] == "REJECTED"
    assert result["reasons"] == ["REFERENCE_PRICE_UNAVAILABLE"]
    client.place_order.assert_not_awaited()

    events = await _events_of_type("REFERENCE_PRICE_UNAVAILABLE")
    assert len(events) == 1
    assert events[0].severity.value == "WARNING"


# ---------------------------------------------------------------------------
# 15. Duplicate master fill does not create a duplicate child order
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_duplicate_master_fill_does_not_duplicate_child_order(db_session, mock_redis, sample_accounts, sample_strategy):
    follower = sample_accounts["follower1"]
    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.place_order.return_value = _success_result("BRK_DUP_1")

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    event = _market_event(quantity=10, price=Decimal("1000.00"), master_order_id="ORD_MKT_DUP")

    res1 = await pipeline.execute_replication(event=event, clients={str(follower.id): client})
    assert res1["results"][str(follower.id)]["status"] == OrderResultCategory.SUCCESS.value

    # Caught by the Redis first-line dedup in EventValidator, at the
    # whole-event level, before any per-follower processing happens again.
    res2 = await pipeline.execute_replication(event=event, clients={str(follower.id): client})
    assert res2["status"] == "rejected"
    assert res2["reason"] == "DUPLICATE_EVENT"

    client.place_order.assert_awaited_once()

    rows = (
        await db_session.execute(
            select(CopyTrade).where(CopyTrade.master_order_id == "ORD_MKT_DUP")
        )
    ).scalars().all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# 16. Partial master fill -> only the confirmed incremental quantity is copied,
#     and the resulting child Order/CopyTrade correctly reflect the new LIMIT type
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_partial_master_fill_copies_only_confirmed_quantity_as_marketable_limit(
    db_session, mock_redis, sample_accounts, sample_strategy
):
    follower = sample_accounts["follower1"]
    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.place_order.return_value = _success_result("BRK_PARTIAL_1")

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    # Master MARKET order for 100, but only 30 confirmed filled so far -
    # the detector would emit an event with quantity=30 (the incremental
    # fill), which is what reaches the pipeline.
    event = _market_event(quantity=30, price=Decimal("1000.00"), master_order_id="ORD_MKT_PARTIAL", fill_sequence=30)
    res = await pipeline.execute_replication(event=event, clients={str(follower.id): client})

    assert res["results"][str(follower.id)]["status"] == OrderResultCategory.SUCCESS.value
    sent_order = client.place_order.call_args.args[0]
    assert sent_order.quantity == 30
    assert sent_order.order_type == OrderType.LIMIT
    assert sent_order.price == Decimal("1000.00")

    order = (
        await db_session.execute(
            select(Order).where(Order.broker_order_id == "BRK_PARTIAL_1")
        )
    ).scalar_one()
    assert order.order_type == "LIMIT"
    assert order.quantity == 30
    assert order.status == OrderStatus.OPEN  # not yet confirmed filled by FollowerFillSyncer
