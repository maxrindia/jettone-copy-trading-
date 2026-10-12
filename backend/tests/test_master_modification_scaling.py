"""
Issue #6 regression coverage: CopyEnginePipeline.handle_master_modification
must scale the master's new quantity by each follower's own
StrategyMember.multiplier (the exact convention the LIMIT creation path,
_process_single_limit_follower, already uses - int(new_quantity *
multiplier)) instead of applying the master's raw quantity uniformly to
every follower, and must run the same funds/RiskEngine gate the creation
path uses before sending modify_order, failing closed per-follower without
affecting any other follower.

No live trading is enabled and no real broker calls are made anywhere in
this file - all broker clients are AsyncMock test doubles.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.models.accounts import Account, AccountType, CopySetting, QuantityMode
from app.models.strategies import GroupStrategy, StrategyMember
from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.types import OrderResult, OrderResultCategory
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator


def _modified_event(master_account_id, master_order_id="ORD_MOD_SCALE_1", quantity=150, price=Decimal("2999.00")):
    return MasterTradeEvent(
        master_order_id=master_order_id,
        symbol="RELIANCE",
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


def _child_order(account_id, master_order_id="ORD_MOD_SCALE_1", broker_order_id="BRK_1", quantity=100, filled_quantity=0, group_strategy_id=None):
    return Order(
        id=uuid.uuid4(),
        internal_order_id=f"JT_CH_{broker_order_id}",
        broker_order_id=broker_order_id,
        account_id=account_id,
        group_strategy_id=group_strategy_id,
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


async def _make_strategy(db_session, master, members):
    """members: list of (Account, multiplier) tuples."""
    strategy = GroupStrategy(
        id=uuid.uuid4(),
        name="Scaling Test Strategy",
        master_account_id=master.id,
        trading_enabled=True,
    )
    db_session.add(strategy)
    for account, multiplier in members:
        db_session.add(
            StrategyMember(
                id=uuid.uuid4(),
                strategy_id=strategy.id,
                account_id=account.id,
                multiplier=multiplier,
                trading_enabled=True,
            )
        )
    await db_session.commit()
    return strategy


@pytest.mark.asyncio
async def test_A_multiple_multipliers_scale_independently(db_session, mock_redis, sample_accounts):
    master = sample_accounts["master"]
    follower_a = sample_accounts["follower1"]
    follower_b = Account(id=uuid.uuid4(), account_type=AccountType.FOLLOWER, display_name="Follower B", alice_user_id="FB001", is_active=True)
    db_session.add(follower_b)
    await db_session.commit()

    await _make_strategy(db_session, master, [(follower_a, Decimal("1.0")), (follower_b, Decimal("2.0"))])

    db_session.add(_child_order(follower_a.id, broker_order_id="BRK_A"))
    db_session.add(_child_order(follower_b.id, broker_order_id="BRK_B"))
    await db_session.commit()

    client_a = AsyncMock()
    client_a.modify_order.return_value = _success_modify_result("BRK_A")
    client_b = AsyncMock()
    client_b.modify_order.return_value = _success_modify_result("BRK_B")

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_modified_event(master.id, quantity=150),
        clients={str(follower_a.id): client_a, str(follower_b.id): client_b},
    )

    assert res["status"] == "completed"
    client_a.modify_order.assert_awaited_once()
    client_b.modify_order.assert_awaited_once()
    assert client_a.modify_order.call_args.kwargs["quantity"] == 150   # 150 * 1.0
    assert client_b.modify_order.call_args.kwargs["quantity"] == 300   # 150 * 2.0


@pytest.mark.asyncio
async def test_B_partial_fill_preserved_when_scaling(db_session, mock_redis, sample_accounts):
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]
    await _make_strategy(db_session, master, [(follower, Decimal("2.0"))])

    db_session.add(_child_order(follower.id, broker_order_id="BRK_PARTIAL", quantity=200, filled_quantity=50))
    await db_session.commit()

    client = AsyncMock()
    client.modify_order.return_value = _success_modify_result("BRK_PARTIAL")

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await pipeline.execute_replication(
        event=_modified_event(master.id, quantity=150),
        clients={str(follower.id): client},
    )

    client.modify_order.assert_awaited_once()
    # (150 * 2.0) - 50 already filled = 250
    assert client.modify_order.call_args.kwargs["quantity"] == 250


@pytest.mark.asyncio
async def test_C_multiplier_below_one_matches_creation_convention(db_session, mock_redis, sample_accounts):
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]
    await _make_strategy(db_session, master, [(follower, Decimal("0.5"))])

    db_session.add(_child_order(follower.id, broker_order_id="BRK_HALF"))
    await db_session.commit()

    client = AsyncMock()
    client.modify_order.return_value = _success_modify_result("BRK_HALF")

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await pipeline.execute_replication(
        event=_modified_event(master.id, quantity=151),
        clients={str(follower.id): client},
    )

    client.modify_order.assert_awaited_once()
    # int(151 * 0.5) = int(75.5) = 75 - matches _process_single_limit_follower's
    # int(event.quantity * float(multiplier)) convention exactly.
    assert client.modify_order.call_args.kwargs["quantity"] == 75


@pytest.mark.asyncio
async def test_D_risk_rejection_prevents_broker_modify_call(db_session, mock_redis, sample_accounts):
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]
    await _make_strategy(db_session, master, [(follower, Decimal("1.0"))])
    db_session.add(
        CopySetting(
            follower_account_id=follower.id,
            copy_enabled=True,
            quantity_mode=QuantityMode.FIXED,
            max_trade_value=Decimal("1000"),  # will be breached by 150 * 2999.00
        )
    )
    db_session.add(_child_order(follower.id, broker_order_id="BRK_RISK"))
    await db_session.commit()

    from app.services.alice_blue.types import FundsData

    client = AsyncMock()
    client.get_funds.return_value = FundsData(
        account_id=str(follower.id), available_margin=Decimal("10000000"),
        collateral=Decimal("0"), used_margin=Decimal("0"), updated_at=datetime.now(timezone.utc),
    )
    client.get_positions.return_value = []
    client.modify_order.return_value = _success_modify_result("BRK_RISK")

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_modified_event(master.id, quantity=150, price=Decimal("2999.00")),
        clients={str(follower.id): client},
    )

    client.modify_order.assert_not_called()
    assert "REJECTED" in res["modifications"][str(follower.id)]


@pytest.mark.asyncio
async def test_E_funds_failure_fails_closed(db_session, mock_redis, sample_accounts):
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]
    await _make_strategy(db_session, master, [(follower, Decimal("1.0"))])
    db_session.add(_child_order(follower.id, broker_order_id="BRK_FUNDS"))
    await db_session.commit()

    client = AsyncMock()
    client.get_funds.side_effect = RuntimeError("broker session expired")
    client.modify_order.return_value = _success_modify_result("BRK_FUNDS")

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_modified_event(master.id, quantity=150),
        clients={str(follower.id): client},
    )

    client.modify_order.assert_not_called()
    assert "FUNDS_FETCH_FAILED" in res["modifications"][str(follower.id)]


@pytest.mark.asyncio
async def test_F_one_followers_risk_failure_does_not_affect_another(db_session, mock_redis, sample_accounts):
    from app.services.alice_blue.types import FundsData

    master = sample_accounts["master"]
    follower_ok = sample_accounts["follower1"]
    follower_fail = Account(id=uuid.uuid4(), account_type=AccountType.FOLLOWER, display_name="Follower Fail", alice_user_id="FF001", is_active=True)
    db_session.add(follower_fail)
    await db_session.commit()

    await _make_strategy(db_session, master, [(follower_ok, Decimal("1.0")), (follower_fail, Decimal("1.0"))])
    db_session.add(
        CopySetting(
            follower_account_id=follower_fail.id,
            copy_enabled=True,
            quantity_mode=QuantityMode.FIXED,
            max_trade_value=Decimal("1000"),
        )
    )
    db_session.add(_child_order(follower_ok.id, broker_order_id="BRK_OK"))
    db_session.add(_child_order(follower_fail.id, broker_order_id="BRK_FAIL"))
    await db_session.commit()

    client_ok = AsyncMock()
    client_ok.modify_order.return_value = _success_modify_result("BRK_OK")

    client_fail = AsyncMock()
    client_fail.get_funds.return_value = FundsData(
        account_id=str(follower_fail.id), available_margin=Decimal("10000000"),
        collateral=Decimal("0"), used_margin=Decimal("0"), updated_at=datetime.now(timezone.utc),
    )
    client_fail.get_positions.return_value = []
    client_fail.modify_order.return_value = _success_modify_result("BRK_FAIL")

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_modified_event(master.id, quantity=150, price=Decimal("2999.00")),
        clients={str(follower_ok.id): client_ok, str(follower_fail.id): client_fail},
    )

    client_ok.modify_order.assert_awaited_once()
    client_fail.modify_order.assert_not_called()
    assert res["modifications"][str(follower_ok.id)] == "MODIFIED"
    assert "REJECTED" in res["modifications"][str(follower_fail.id)]
