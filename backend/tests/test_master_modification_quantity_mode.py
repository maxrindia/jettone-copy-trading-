"""
Issue #8 regression coverage: CopyEnginePipeline.handle_master_modification
must compute each follower's modified quantity using the SAME rules as the
LIMIT creation path fixed in Issue #7 (_process_single_limit_follower) -
QuantityCalculator when a CopySetting exists, the plain multiplier formula
(now with lot-size flooring) when it doesn't - instead of always using only
StrategyMember.multiplier regardless of the follower's configuration.

No live trading is enabled and no real broker calls are made anywhere in
this file - all broker clients are AsyncMock test doubles.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings as app_settings
from app.models.accounts import Account, AccountType, CopySetting, QuantityMode
from app.models.strategies import GroupStrategy, StrategyMember
from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.types import FundsData, OrderResult, OrderResultCategory
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator


def _modified_event(master_account_id, master_order_id="ORD_MOD_QM_1", quantity=150, price=Decimal("100.00"), symbol="RELIANCE"):
    return MasterTradeEvent(
        master_order_id=master_order_id,
        symbol=symbol,
        exchange="NSE",
        side="BUY",
        quantity=quantity,
        price=price,
        limit_price=price,
        product_type="MIS",
        timestamp=datetime.now(timezone.utc),
        fill_sequence=1,
        account_id=str(master_account_id),
        order_type="LIMIT",
        event_type="ORDER_MODIFIED",
        status="OPEN",
    )


def _child_order(account_id, master_order_id="ORD_MOD_QM_1", broker_order_id="BRK_1", quantity=100, filled_quantity=0, symbol="RELIANCE"):
    return Order(
        id=uuid.uuid4(),
        internal_order_id=f"JT_CH_{broker_order_id}",
        broker_order_id=broker_order_id,
        account_id=account_id,
        master_broker_order_id=master_order_id,
        source=OrderSource.SYSTEM,
        exchange="NSE",
        symbol=symbol,
        side=OrderSide.BUY,
        order_type="LIMIT",
        product_type="MIS",
        quantity=quantity,
        filled_quantity=filled_quantity,
        pending_quantity=quantity - filled_quantity,
        price=Decimal("95.00"),
        status=OrderStatus.PARTIAL if filled_quantity > 0 else OrderStatus.OPEN,
    )


def _success_modify_result(broker_order_id):
    return OrderResult(
        category=OrderResultCategory.SUCCESS,
        client_order_id=None,
        broker_order_id=broker_order_id,
        message="Modified successfully",
        raw_response={"stat": "Ok"},
        timestamp=datetime.now(timezone.utc),
    )


def _funds(available=Decimal("500000")):
    return FundsData(
        account_id="a", available_margin=available, collateral=Decimal("0"),
        used_margin=Decimal("0"), updated_at=datetime.now(timezone.utc),
    )


async def _make_strategy(db_session, master, members):
    """members: list of (Account, multiplier) tuples."""
    strategy = GroupStrategy(
        id=uuid.uuid4(), name="Mod QuantityMode Test Strategy",
        master_account_id=master.id, trading_enabled=True,
    )
    db_session.add(strategy)
    for account, multiplier in members:
        db_session.add(StrategyMember(
            id=uuid.uuid4(), strategy_id=strategy.id, account_id=account.id,
            multiplier=multiplier, trading_enabled=True,
        ))
    await db_session.commit()
    return strategy


def _client(broker_order_id, funds_available=Decimal("500000")):
    client = AsyncMock()
    client.get_funds.return_value = _funds(funds_available)
    client.get_positions.return_value = []
    client.modify_order.return_value = _success_modify_result(broker_order_id)
    return client


@pytest.mark.asyncio
async def test_fixed_mode_modification_uses_fixed_qty(db_session, mock_redis, sample_accounts):
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]
    await _make_strategy(db_session, master, [(follower, Decimal("1.0"))])
    db_session.add(CopySetting(follower_account_id=follower.id, copy_enabled=True, quantity_mode=QuantityMode.FIXED, fixed_qty=15))
    db_session.add(_child_order(follower.id, broker_order_id="BRK_FIXED", quantity=100, filled_quantity=0))
    await db_session.commit()

    client = _client("BRK_FIXED")
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_modified_event(master.id, quantity=150), clients={str(follower.id): client},
    )

    client.modify_order.assert_awaited_once()
    # fixed_qty=15 must win, NOT int(150 * multiplier=1.0)=150
    assert client.modify_order.call_args.kwargs["quantity"] == 15
    assert res["modifications"][str(follower.id)] == "MODIFIED"


@pytest.mark.asyncio
async def test_ratio_mode_modification_scales_new_quantity(db_session, mock_redis, sample_accounts):
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]
    await _make_strategy(db_session, master, [(follower, Decimal("1.0"))])
    db_session.add(CopySetting(follower_account_id=follower.id, copy_enabled=True, quantity_mode=QuantityMode.RATIO, ratio=Decimal("0.5")))
    db_session.add(_child_order(follower.id, broker_order_id="BRK_RATIO", quantity=100, filled_quantity=0))
    await db_session.commit()

    client = _client("BRK_RATIO")
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await pipeline.execute_replication(
        event=_modified_event(master.id, quantity=150), clients={str(follower.id): client},
    )

    client.modify_order.assert_awaited_once()
    assert client.modify_order.call_args.kwargs["quantity"] == 75  # 150 * 0.5


@pytest.mark.asyncio
async def test_capital_proportional_mode_modification_scales_by_follower_funds(db_session, mock_redis, sample_accounts):
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]
    await _make_strategy(db_session, master, [(follower, Decimal("1.0"))])
    db_session.add(CopySetting(follower_account_id=follower.id, copy_enabled=True, quantity_mode=QuantityMode.CAPITAL_PROPORTIONAL))
    db_session.add(_child_order(follower.id, broker_order_id="BRK_CAPPROP", quantity=100, filled_quantity=0))
    await db_session.commit()

    # MASTER_CAPITAL is 500000 (conftest env default); follower has half that.
    client = _client("BRK_CAPPROP", funds_available=app_settings.MASTER_CAPITAL / 2)
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await pipeline.execute_replication(
        event=_modified_event(master.id, quantity=150), clients={str(follower.id): client},
    )

    client.modify_order.assert_awaited_once()
    assert client.modify_order.call_args.kwargs["quantity"] == 75  # 150 * (250000/500000)


@pytest.mark.asyncio
async def test_risk_based_mode_modification_capped_by_max_trade_value(db_session, mock_redis, sample_accounts):
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]
    await _make_strategy(db_session, master, [(follower, Decimal("1.0"))])
    db_session.add(CopySetting(
        follower_account_id=follower.id, copy_enabled=True, quantity_mode=QuantityMode.RISK_BASED,
        max_trade_value=Decimal("2000"),  # caps quantity at 2000/100.00 = 20
    ))
    db_session.add(_child_order(follower.id, broker_order_id="BRK_RISKBASED", quantity=100, filled_quantity=0))
    await db_session.commit()

    # capital-proportional base would be 150 * (500000/500000) = 150, capped to 20 by max_trade_value.
    client = _client("BRK_RISKBASED", funds_available=app_settings.MASTER_CAPITAL)
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await pipeline.execute_replication(
        event=_modified_event(master.id, quantity=150, price=Decimal("100.00")), clients={str(follower.id): client},
    )

    client.modify_order.assert_awaited_once()
    assert client.modify_order.call_args.kwargs["quantity"] == 20


@pytest.mark.asyncio
async def test_no_copy_setting_preserves_multiplier_fallback(db_session, mock_redis, sample_accounts):
    """Regression: a follower with no CopySetting is unaffected by this fix
    and still uses the plain multiplier formula exactly as before."""
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]
    await _make_strategy(db_session, master, [(follower, Decimal("2.0"))])
    db_session.add(_child_order(follower.id, broker_order_id="BRK_NOSETTINGS", quantity=100, filled_quantity=0))
    await db_session.commit()

    client = _client("BRK_NOSETTINGS")
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await pipeline.execute_replication(
        event=_modified_event(master.id, quantity=150), clients={str(follower.id): client},
    )

    client.modify_order.assert_awaited_once()
    assert client.modify_order.call_args.kwargs["quantity"] == 300  # 150 * multiplier(2.0)


@pytest.mark.asyncio
async def test_lot_size_flooring_applied_on_multiplier_fallback(db_session, mock_redis, sample_accounts):
    """New behavior added by this fix: the no-CopySetting fallback path now
    applies lot-size flooring, matching the LIMIT creation path - previously
    handle_master_modification never floored to lot size at all."""
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]
    await _make_strategy(db_session, master, [(follower, Decimal("1.0"))])
    db_session.add(_child_order(follower.id, broker_order_id="BRK_LOT", quantity=200, filled_quantity=0, symbol="NIFTY24AUG"))
    await db_session.commit()

    client = _client("BRK_LOT")
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis), lot_sizes={"NIFTY": 50, "DEFAULT": 1})
    # new_quantity=173 * multiplier(1.0) = 173 -> floored to nearest 50 -> 150
    await pipeline.execute_replication(
        event=_modified_event(master.id, quantity=173, symbol="NIFTY24AUG"), clients={str(follower.id): client},
    )

    client.modify_order.assert_awaited_once()
    assert client.modify_order.call_args.kwargs["quantity"] == 150


@pytest.mark.asyncio
async def test_partial_fill_preserved_with_quantity_mode(db_session, mock_redis, sample_accounts):
    """Already-filled quantity must still be subtracted from (and never
    exceed) the newly computed target, and must never itself be touched."""
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]
    await _make_strategy(db_session, master, [(follower, Decimal("1.0"))])
    db_session.add(CopySetting(follower_account_id=follower.id, copy_enabled=True, quantity_mode=QuantityMode.RATIO, ratio=Decimal("2.0")))
    db_session.add(_child_order(follower.id, broker_order_id="BRK_PARTIAL_QM", quantity=200, filled_quantity=50))
    await db_session.commit()

    client = _client("BRK_PARTIAL_QM")
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await pipeline.execute_replication(
        event=_modified_event(master.id, quantity=150), clients={str(follower.id): client},
    )

    client.modify_order.assert_awaited_once()
    # target = 150 * 2.0 = 300; remaining = 300 - 50 already filled = 250
    assert client.modify_order.call_args.kwargs["quantity"] == 250


@pytest.mark.asyncio
async def test_independent_followers_different_quantity_modes(db_session, mock_redis, sample_accounts):
    """Two followers with different configurations (one CopySetting-based,
    one fallback) are each computed independently and correctly in the same
    modification event, with no cross-contamination."""
    master = sample_accounts["master"]
    follower_fixed = sample_accounts["follower1"]
    follower_fallback = Account(id=uuid.uuid4(), account_type=AccountType.FOLLOWER, display_name="Follower Fallback", alice_user_id="AB_FALLBACK", is_active=True)
    db_session.add(follower_fallback)
    await db_session.commit()

    await _make_strategy(db_session, master, [(follower_fixed, Decimal("1.0")), (follower_fallback, Decimal("3.0"))])
    db_session.add(CopySetting(follower_account_id=follower_fixed.id, copy_enabled=True, quantity_mode=QuantityMode.FIXED, fixed_qty=10))
    db_session.add(_child_order(follower_fixed.id, broker_order_id="BRK_IND_FIXED", quantity=100, filled_quantity=0))
    db_session.add(_child_order(follower_fallback.id, broker_order_id="BRK_IND_FALLBACK", quantity=100, filled_quantity=0))
    await db_session.commit()

    client_fixed = _client("BRK_IND_FIXED")
    client_fallback = _client("BRK_IND_FALLBACK")
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await pipeline.execute_replication(
        event=_modified_event(master.id, quantity=150),
        clients={str(follower_fixed.id): client_fixed, str(follower_fallback.id): client_fallback},
    )

    client_fixed.modify_order.assert_awaited_once()
    client_fallback.modify_order.assert_awaited_once()
    assert client_fixed.modify_order.call_args.kwargs["quantity"] == 10          # FIXED fixed_qty
    assert client_fallback.modify_order.call_args.kwargs["quantity"] == 450      # 150 * multiplier(3.0)
