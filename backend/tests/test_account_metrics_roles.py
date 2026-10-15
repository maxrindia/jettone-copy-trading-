"""
GET /accounts/metrics (the Demat card counters) follow STRATEGY roles.

A strategy's Master is whichever account the strategy points at, not
necessarily one created as account_type MASTER. Its own orders (placed on the
broker app/web or in Jettone) must show on its card; follower-side copy orders
are already counted through copy_trades and must not be counted twice.
"""
import uuid
from decimal import Decimal

import pytest

from app.models.accounts import AccountType
from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderSide, OrderSource, OrderStatus
from conftest import TestingSessionLocal
from test_dashboard_api import AUTH_HEADER, _account, _strategy, api_client  # noqa: F401  (api_client is a fixture)


def _order(account_id, *, source, status=OrderStatus.COMPLETE, boid=None, master_boid=None, symbol="CANBK"):
    boid = boid or f"B{uuid.uuid4().hex[:10]}"
    return Order(
        id=uuid.uuid4(), internal_order_id=f"I_{boid}", broker_order_id=boid, account_id=account_id,
        source=source, exchange="NSE", symbol=symbol, side=OrderSide.BUY, order_type="LIMIT",
        product_type="MIS", quantity=1, filled_quantity=1 if status == OrderStatus.COMPLETE else 0,
        pending_quantity=0 if status == OrderStatus.COMPLETE else 1, price=Decimal("100"),
        status=status, master_broker_order_id=master_boid,
    )


def _copy_trade(follower_id, status=CopyTradeStatus.SUCCESS):
    return CopyTrade(
        id=uuid.uuid4(), master_order_id=f"M{uuid.uuid4().hex[:8]}", follower_account_id=follower_id,
        copy_event_id=uuid.uuid4(), symbol="CANBK", exchange="NSE", side=OrderSide.BUY, quantity=1,
        product_type="MIS", estimated_price=Decimal("100"), client_order_id=f"JT_{uuid.uuid4().hex[:8]}",
        status=status,
    )


async def _metrics(api_client):
    resp = await api_client.get("/api/v1/accounts/metrics", headers=AUTH_HEADER)
    assert resp.status_code == 200, resp.text
    return resp.json()["metrics"]


@pytest.mark.asyncio
async def test_follower_typed_strategy_master_shows_its_own_orders(api_client, db_session):
    async with TestingSessionLocal() as session:
        master = await _account(session, AccountType.FOLLOWER, "Dhinesh", "AB_MM_MASTER")
        child = await _account(session, AccountType.FOLLOWER, "Vikash", "AB_MM_CHILD")
        await _strategy(session, "Master", master, [(child, 1.0, True)])
        for i in range(3):
            session.add(_order(master.id, source=OrderSource.BROKER_EXTERNAL))
        session.add(_order(master.id, source=OrderSource.BROKER_EXTERNAL, status=OrderStatus.OPEN))
        session.add(_order(master.id, source=OrderSource.INTERNAL_UI, status=OrderStatus.REJECTED))
        await session.commit()

    m = (await _metrics(api_client))[str(master.id)]
    assert m["orders_count"] == 5
    assert (m["completed_count"], m["pending_count"], m["rejected_count"]) == (3, 1, 1)


@pytest.mark.asyncio
async def test_master_that_was_a_child_is_not_double_counted(api_client, db_session):
    """Dhinesh carries SYSTEM copy orders + copy_trades from when he was a
    Child: 2 copies counted once (via copy_trades) plus 3 own orders = 5."""
    async with TestingSessionLocal() as session:
        master = await _account(session, AccountType.FOLLOWER, "Dhinesh", "AB_DD_MASTER")
        child = await _account(session, AccountType.FOLLOWER, "Vikash", "AB_DD_CHILD")
        await _strategy(session, "Master", master, [(child, 1.0, True)])
        for i in range(3):
            session.add(_order(master.id, source=OrderSource.BROKER_EXTERNAL))
        for i in range(2):
            session.add(_order(master.id, source=OrderSource.SYSTEM, master_boid=f"OLDM{i}"))
            session.add(_copy_trade(master.id))
        await session.commit()

    m = (await _metrics(api_client))[str(master.id)]
    assert m["orders_count"] == 5          # 3 own + 2 copies once, not 3 + 2 + 2
    assert m["completed_count"] == 5


@pytest.mark.asyncio
async def test_plain_child_counts_are_unchanged(api_client, db_session):
    async with TestingSessionLocal() as session:
        master = await _account(session, AccountType.FOLLOWER, "Dhinesh", "AB_CC_MASTER")
        child = await _account(session, AccountType.FOLLOWER, "Vikash", "AB_CC_CHILD")
        await _strategy(session, "Master", master, [(child, 1.0, True)])
        for i in range(2):
            session.add(_copy_trade(child.id))
            session.add(_order(child.id, source=OrderSource.SYSTEM, master_boid=f"M{i}"))
        session.add(_copy_trade(child.id, CopyTradeStatus.RECONCILIATION_REQUIRED))
        # A child's own external order is NOT added to its card (copy_trades is its source of truth).
        session.add(_order(child.id, source=OrderSource.BROKER_EXTERNAL))
        await session.commit()

    m = (await _metrics(api_client))[str(child.id)]
    assert m["orders_count"] == 3
    assert m["completed_count"] == 2
    assert m["needs_review_count"] == 1


@pytest.mark.asyncio
async def test_legacy_master_typed_account_still_shows_its_orders(api_client, db_session):
    async with TestingSessionLocal() as session:
        legacy = await _account(session, AccountType.MASTER, "Legacy", "AB_LG_MASTER")
        session.add(_order(legacy.id, source=OrderSource.BROKER_EXTERNAL))
        session.add(_order(legacy.id, source=OrderSource.INTERNAL_UI, status=OrderStatus.OPEN))
        await session.commit()

    m = (await _metrics(api_client))[str(legacy.id)]
    assert (m["orders_count"], m["completed_count"], m["pending_count"]) == (2, 1, 1)


@pytest.mark.asyncio
async def test_deleted_strategy_master_is_not_treated_as_master(api_client, db_session):
    from datetime import datetime, timezone

    async with TestingSessionLocal() as session:
        master = await _account(session, AccountType.FOLLOWER, "Old", "AB_DEL_MASTER")
        strat = await _strategy(session, "Gone", master, [])
        strat.deleted_at = datetime.now(timezone.utc)
        session.add(_order(master.id, source=OrderSource.BROKER_EXTERNAL))
        await session.commit()

    m = (await _metrics(api_client))[str(master.id)]
    assert m["orders_count"] == 0           # no longer a master of a live strategy; no copy_trades either
