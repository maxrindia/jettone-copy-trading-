"""
Temporary SELL_POSITION_SAFETY_BYPASS switch: skips only the follower SELL
position-safety gate. Delete this file together with the switch.

No live trading and no real broker calls - the client is an AsyncMock.
"""
from decimal import Decimal

import pytest

from app.core.config import settings
from app.services.alice_blue.types import OrderResultCategory, OrderSide
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator

from tests.test_sell_position_safety import (
    _buy_market_event,
    _client,
    _sell_limit_event,
    _sell_market_event,
)


@pytest.fixture
def bypass_on(monkeypatch):
    monkeypatch.setattr(settings, "SELL_POSITION_SAFETY_BYPASS", True)


def test_bypass_defaults_to_off():
    assert settings.SELL_POSITION_SAFETY_BYPASS is False


@pytest.mark.asyncio
async def test_gate_still_blocks_when_bypass_off(db_session, mock_redis, sample_accounts, sample_strategy):
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_sell_market_event(quantity=1), clients={str(follower.id): client}
    )

    assert res["results"][str(follower.id)]["reasons"] == ["INSUFFICIENT_LONG_POSITION"]
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_bypass_places_market_sell_for_flat_follower(db_session, mock_redis, sample_accounts, sample_strategy, bypass_on):
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_sell_market_event(quantity=1), clients={str(follower.id): client}
    )

    assert res["results"][str(follower.id)]["status"] == OrderResultCategory.SUCCESS.value
    sent = client.place_order.call_args.args[0]
    assert sent.side == OrderSide.SELL
    assert sent.quantity == 1
    client.get_positions_or_none.assert_not_called()


@pytest.mark.asyncio
async def test_bypass_places_limit_sell_for_flat_follower(db_session, mock_redis, sample_accounts, sample_strategy, bypass_on):
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await pipeline.execute_replication(
        event=_sell_limit_event(quantity=1), clients={str(follower.id): client}
    )

    client.place_order.assert_awaited_once()
    sent = client.place_order.call_args.args[0]
    assert sent.side == OrderSide.SELL
    assert sent.quantity == 1
    client.get_positions_or_none.assert_not_called()


@pytest.mark.asyncio
async def test_bypass_ignores_position_fetch_failure(db_session, mock_redis, sample_accounts, sample_strategy, bypass_on):
    """Today's POSITION_FETCH_FAILED case: with the bypass on it no longer blocks."""
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions_error=RuntimeError("result null"))

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_sell_market_event(quantity=1), clients={str(follower.id): client}
    )

    assert res["results"][str(follower.id)]["status"] == OrderResultCategory.SUCCESS.value
    client.place_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_bypass_does_not_change_buy(db_session, mock_redis, sample_accounts, sample_strategy, bypass_on):
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_buy_market_event(quantity=3), clients={str(follower.id): client}
    )

    assert res["results"][str(follower.id)]["status"] == OrderResultCategory.SUCCESS.value
    sent = client.place_order.call_args.args[0]
    assert sent.side == OrderSide.BUY
    assert sent.quantity == 3


async def _flat_follower_sell_reprice(db_session, mock_redis):
    """Master SELL LIMIT converted to MARKET and filled at 2955.00, with one
    open child SELL LIMIT whose follower holds no position at all."""
    from app.services.copy_engine.detector import MasterTradeDetector
    from tests.test_limit_to_market_modification import (
        _limit_open_event,
        _make_child,
        _market_complete_event,
        _reprice_client,
    )

    child_acc, child_order = await _make_child(
        db_session, broker_order_id="BRK_SELL_FLAT", side=OrderSide.SELL
    )
    client = _reprice_client("BRK_SELL_FLAT", redis=mock_redis, positions=[])

    detector = MasterTradeDetector(master_account_id="M1")
    detector.process_order_update(_limit_open_event())
    fill_event = detector.process_order_update(_market_complete_event(fill_price=Decimal("2955.00")))
    fill_event.side = "SELL"

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=fill_event, clients={str(child_acc.id): client})
    return child_acc, child_order, client, res


@pytest.mark.asyncio
async def test_bypass_reprices_sell_child_for_flat_follower(db_session, mock_redis, bypass_on):
    from app.services.alice_blue.types import OrderType

    child_acc, child_order, client, res = await _flat_follower_sell_reprice(db_session, mock_redis)

    assert res["status"] == "completed"
    client.modify_order.assert_awaited_once()
    _, kwargs = client.modify_order.call_args
    assert kwargs["broker_order_id"] == "BRK_SELL_FLAT"
    assert kwargs["order_type"] == OrderType.LIMIT
    assert kwargs["price"] == Decimal("2955.00")  # master fill, default 0% buffer
    client.get_positions_or_none.assert_not_called()
    client.get_positions.assert_not_called()
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_sell_reprice_still_blocked_for_flat_follower_when_bypass_off(db_session, mock_redis):
    from app.models.trades import OrderStatus

    child_acc, child_order, client, res = await _flat_follower_sell_reprice(db_session, mock_redis)

    assert res["status"] == "completed_with_failures"
    assert res["modifications"][str(child_acc.id)] == "SELL_REPRICE_BLOCKED: INSUFFICIENT_LONG_POSITION"
    client.modify_order.assert_not_called()
    client.place_order.assert_not_called()

    await db_session.refresh(child_order)
    assert child_order.price == Decimal("2950.00")
    assert child_order.status == OrderStatus.OPEN


@pytest.mark.asyncio
async def test_bypass_does_not_skip_risk_engine(db_session, mock_redis, sample_accounts, sample_strategy, sample_copy_settings, bypass_on):
    """The fixture's CopySetting only allows NIFTY24AUG23500CE, so a YESBANK
    SELL must still be rejected by RiskEngine, which runs before the gate."""
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_sell_market_event(quantity=1, symbol="YESBANK"),
        clients={str(follower.id): client},
    )

    result = res["results"][str(follower.id)]
    assert result["status"] == "REJECTED"
    assert "INSUFFICIENT_LONG_POSITION" not in result["reasons"]
    client.place_order.assert_not_called()
