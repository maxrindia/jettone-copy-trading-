"""Modification-path safety with strategy-specific Masters.

Modification (handle_master_modification) still resolves each existing
child's CopySetting through the original lookup, fallback included, so the
quantity behavior of managing an already-copied order is unchanged. These
tests pin what that lookup must never allow:

- the global emergency stop and a per-account pause must block a child
  modification even when that child resolves WITHOUT a CopySetting (a
  disabled strategy whose Child was created as account_type MASTER, or a
  Child whose member toggle is off) - previously the RiskEngine block, the
  only stop check on this path, was skipped entirely for such a child;
- a modification only ever touches children of THAT master order, never a
  child of another strategy's order, including after a Master switch.

Broker clients are AsyncMock doubles; no real broker call is possible.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.accounts import Account, AccountType, CopySetting, QuantityMode
from app.models.audit import AuditLog
from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.types import FundsData, OrderResult, OrderResultCategory
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from strategy_helpers import link_strategy


async def _account(session, name, account_type=AccountType.FOLLOWER, *, copy_setting=True):
    acc = Account(id=uuid.uuid4(), account_type=account_type, display_name=name,
                  alice_user_id=f"AB_{name}_{uuid.uuid4().hex[:4]}", is_active=True)
    session.add(acc)
    await session.flush()
    if copy_setting:
        session.add(CopySetting(follower_account_id=acc.id, copy_enabled=True,
                                quantity_mode=QuantityMode.FIXED, fixed_qty=7))
    await session.commit()
    return acc


def _child_order(account_id, master_order_id, boid):
    return Order(
        id=uuid.uuid4(), internal_order_id=f"JT_{boid}", broker_order_id=boid, account_id=account_id,
        master_broker_order_id=master_order_id, source=OrderSource.SYSTEM, exchange="NSE", symbol="RELIANCE",
        side=OrderSide.BUY, order_type="LIMIT", product_type="MIS", quantity=7, filled_quantity=0,
        pending_quantity=7, price=Decimal("100.00"), status=OrderStatus.OPEN,
    )


def _client(boid):
    c = AsyncMock()
    c.get_funds.return_value = FundsData(account_id="f", available_margin=Decimal("1000000"),
                                         collateral=Decimal("0"), used_margin=Decimal("0"),
                                         updated_at=datetime.now(timezone.utc))
    c.get_positions.return_value = []
    c.get_positions_or_none.return_value = []
    c.modify_order.return_value = OrderResult(
        category=OrderResultCategory.SUCCESS, client_order_id=None, broker_order_id=boid, message="ok",
        raw_response={}, timestamp=datetime.now(timezone.utc),
    )
    return c


def _pipeline(mock_redis, *, global_stop=False, paused=()):
    async def provider():
        return global_stop, set(paused)

    return CopyEnginePipeline(validator=EventValidator(redis=mock_redis), stop_provider=provider)


async def _no_copy_setting_child(db_session):
    """A Child created as account_type MASTER (no CopySetting) in a strategy
    whose trading is off: the original lookup resolves it with no
    CopySetting at all."""
    master = await _account(db_session, "vikash")
    child = await _account(db_session, "dhinesh", AccountType.MASTER, copy_setting=False)
    await link_strategy(db_session, master, [child], trading_enabled=False)
    db_session.add(_child_order(child.id, "ORD_M1", "CH_1"))
    await db_session.commit()
    return master, child


@pytest.mark.asyncio
async def test_global_stop_blocks_modification_of_child_without_copy_setting(db_session, mock_redis):
    master, child = await _no_copy_setting_child(db_session)
    client = _client("CH_1")
    res = await _pipeline(mock_redis, global_stop=True).handle_master_modification(
        master_order_id="ORD_M1", new_price=Decimal("101"), new_quantity=50,
        clients={str(child.id): client}, master_account_id=str(master.id),
    )
    client.modify_order.assert_not_called()
    assert res["modifications"][str(child.id)] == "REJECTED: EMERGENCY_STOP_ACTIVE"
    reasons = (await db_session.execute(select(AuditLog.reason_code))).scalars().all()
    assert "EMERGENCY_STOP_ACTIVE" in reasons


@pytest.mark.asyncio
async def test_account_pause_blocks_modification_of_child_without_copy_setting(db_session, mock_redis):
    master, child = await _no_copy_setting_child(db_session)
    client = _client("CH_1")
    res = await _pipeline(mock_redis, paused=[str(child.id)]).handle_master_modification(
        master_order_id="ORD_M1", new_price=Decimal("101"), new_quantity=50,
        clients={str(child.id): client}, master_account_id=str(master.id),
    )
    client.modify_order.assert_not_called()
    assert res["modifications"][str(child.id)] == "REJECTED: FOLLOWER_EMERGENCY_STOP"


@pytest.mark.asyncio
async def test_without_any_stop_the_no_copy_setting_modification_is_unchanged(db_session, mock_redis):
    """The fix only adds the stop checks; with no stop active the existing
    multiplier-formula modification still happens exactly as before."""
    master, child = await _no_copy_setting_child(db_session)
    client = _client("CH_1")
    await _pipeline(mock_redis).handle_master_modification(
        master_order_id="ORD_M1", new_price=Decimal("101"), new_quantity=50,
        clients={str(child.id): client}, master_account_id=str(master.id),
    )
    client.modify_order.assert_awaited_once()
    assert client.modify_order.call_args.kwargs["quantity"] == 50  # 50 * 1.0, no CopySetting


@pytest.mark.asyncio
async def test_global_stop_still_blocks_child_with_copy_setting_unchanged(db_session, mock_redis):
    master = await _account(db_session, "m")
    child = await _account(db_session, "c")
    await link_strategy(db_session, master, [child])
    db_session.add(_child_order(child.id, "ORD_M2", "CH_2"))
    await db_session.commit()
    client = _client("CH_2")
    res = await _pipeline(mock_redis, global_stop=True).handle_master_modification(
        master_order_id="ORD_M2", new_price=Decimal("101"), new_quantity=50,
        clients={str(child.id): client}, master_account_id=str(master.id),
    )
    client.modify_order.assert_not_called()
    assert "EMERGENCY_STOP_ACTIVE" in res["modifications"][str(child.id)]


@pytest.mark.asyncio
async def test_modification_never_touches_another_strategys_child(db_session, mock_redis):
    """Two strategies, two Masters, one open child each. A modification of
    Normal's master order modifies only Normal's child - also when the
    caller passes the OTHER Master's id (as after a switch) - because
    children are selected by the master order id, never by account."""
    arun, dhinesh = await _account(db_session, "arun"), await _account(db_session, "dhinesh")
    n_child, i_child = await _account(db_session, "nchild"), await _account(db_session, "ichild")
    await link_strategy(db_session, arun, [n_child], name="Normal")
    await link_strategy(db_session, dhinesh, [i_child], name="Intraday")
    db_session.add(_child_order(n_child.id, "ORD_ARUN", "CH_N"))
    db_session.add(_child_order(i_child.id, "ORD_DHINESH", "CH_I"))
    await db_session.commit()
    clients = {str(n_child.id): _client("CH_N"), str(i_child.id): _client("CH_I")}

    for master_account_id in (str(arun.id), str(dhinesh.id)):
        await _pipeline(mock_redis).handle_master_modification(
            master_order_id="ORD_ARUN", new_price=Decimal("101"), new_quantity=7,
            clients=clients, master_account_id=master_account_id,
        )
    assert clients[str(n_child.id)].modify_order.await_count == 2
    assert clients[str(n_child.id)].modify_order.call_args.kwargs["broker_order_id"] == "CH_N"
    clients[str(i_child.id)].modify_order.assert_not_called()
