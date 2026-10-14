"""Endpoint tests for GET /dashboard/summary and GET /dashboard/realized-timeline.

Runs the real FastAPI app via ASGI transport with overridden dependencies, the
same pattern as test_api_endpoints.py. No network, no engine lifespan, no
broker writes - PnlService's broker reads come from FakeBrokerClient.
"""
import uuid
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock

from app.api.accounts import get_broker_client_provider
from app.api.dashboard import get_live_client_account_ids, get_live_master_id
from app.core.database import get_db
from app.core.redis import get_redis
from app.core.trading_day import trading_day, trading_day_start_utc
from app.main import app
from app.models.accounts import Account, AccountType
from app.models.audit import AuditLog, EventSeverity, SystemEvent
from app.models.strategies import GroupStrategy, StrategyMember
from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from conftest import TestingSessionLocal
from pnl_live_fixtures import (
    CHILD_ORDER_BOOK,
    CHILD_POSITIONS,
    MASTER_ORDER_BOOK,
    MASTER_POSITIONS,
    FakeBrokerClient,
    on_day,
)

TOKEN = "test-internal-token"
AUTH_HEADER = {"X-Internal-Token": TOKEN}


@pytest_asyncio.fixture
async def api_client(mock_redis, db_session):
    async def override_db():
        async with TestingSessionLocal() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_redis] = lambda: mock_redis
    app.dependency_overrides[get_broker_client_provider] = lambda: (lambda _account_id: None)
    app.dependency_overrides[get_live_master_id] = lambda: None
    app.dependency_overrides[get_live_client_account_ids] = lambda: set()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


async def _account(session, account_type, name, alice_user_id, is_active=True, session_valid=True):
    acc = Account(
        id=uuid.uuid4(), account_type=account_type, display_name=name, alice_user_id=alice_user_id,
        is_active=is_active, session_valid=session_valid,
    )
    session.add(acc)
    await session.flush()
    return acc


async def _master_and_child(session, master_book=MASTER_ORDER_BOOK, child_book=CHILD_ORDER_BOOK,
                             master_positions=MASTER_POSITIONS, child_positions=CHILD_POSITIONS):
    day = trading_day()
    master = await _account(session, AccountType.MASTER, "Dhinesh", "AB_MASTER")
    child = await _account(session, AccountType.FOLLOWER, "vikash", "AB_CHILD")
    await session.commit()
    clients = {
        str(master.id): FakeBrokerClient(on_day(master_book, day) if master_book else None, master_positions),
        str(child.id): FakeBrokerClient(on_day(child_book, day) if child_book else None, child_positions),
    }
    return master, child, clients


def _install(clients, live_master_id=None, live_client_ids=None):
    app.dependency_overrides[get_broker_client_provider] = lambda: clients.get
    app.dependency_overrides[get_live_master_id] = lambda: live_master_id
    app.dependency_overrides[get_live_client_account_ids] = lambda: set(live_client_ids or ())


async def _strategy(session, name, master, members):
    """members: list of (account, multiplier, trading_enabled)."""
    strat = GroupStrategy(id=uuid.uuid4(), name=name, master_account_id=master.id if master else None)
    session.add(strat)
    await session.flush()
    for account, multiplier, trading_enabled in members:
        session.add(StrategyMember(
            id=uuid.uuid4(), strategy_id=strat.id, account_id=account.id,
            multiplier=Decimal(str(multiplier)), trading_enabled=trading_enabled,
        ))
    await session.commit()
    return strat


# ── combined P&L: distinct-account sum, never double-counted ────────────────

@pytest.mark.asyncio
async def test_summary_combines_distinct_account_pnl_not_strategy_members(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session)
        await _strategy(session, "master", master, [(child, 1.0, True)])
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    assert resp.status_code == 200
    body = resp.json()

    assert body["pnl"]["combined_realized"] == "-3.00"
    assert body["pnl"]["combined_total"] == "-3.00"
    assert body["pnl"]["accounts_unavailable"] == 0
    per_account = {a["account_id"]: a for a in body["pnl"]["accounts"]}
    assert per_account[str(master.id)]["total_pnl"] == "-1.52"
    assert per_account[str(child.id)]["total_pnl"] == "-1.48"
    # The child's account P&L is not the same number as the master's.
    assert per_account[str(master.id)]["total_pnl"] != per_account[str(child.id)]["total_pnl"]
    # Old turnover figure must never appear anywhere in the payload.
    assert "4200.16" not in resp.text
    assert "4200.18" not in resp.text


@pytest.mark.asyncio
async def test_summary_shows_unavailable_not_a_partial_sum_when_one_account_is_down(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session)
    clients[str(child.id)] = FakeBrokerClient(None, None)  # broker unreachable for the child
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    body = resp.json()

    assert body["pnl"]["combined_realized"] is None
    assert body["pnl"]["combined_total"] is None
    assert body["pnl"]["accounts_unavailable"] == 1
    per_account = {a["account_id"]: a for a in body["pnl"]["accounts"]}
    assert per_account[str(master.id)]["total_pnl"] == "-1.52"  # the healthy account's own value is still shown
    assert per_account[str(child.id)]["pnl_status"] == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_summary_open_positions_combined_when_both_flat(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session)
    _install(clients, live_master_id=str(master.id))
    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    assert resp.json()["open_positions"]["total"] == 0  # both flat in the fixture


@pytest.mark.asyncio
async def test_summary_open_positions_unavailable_when_one_accounts_positions_cannot_be_read(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session)
    clients[str(child.id)] = FakeBrokerClient(CHILD_ORDER_BOOK, None)
    _install(clients, live_master_id=str(master.id))
    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    assert resp.json()["open_positions"]["total"] is None


@pytest.mark.asyncio
async def test_summary_empty_state_with_no_accounts(api_client):
    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["pnl"]["combined_total"] is None  # never "0.00" for zero accounts
    assert body["pnl"]["accounts"] == []
    assert body["status"]["master"] is None
    assert body["status"]["children"] == {"total": 0, "connected": 0}
    assert body["copy_fill"]["target_quantity"] == 0
    assert body["copy_fill"]["rate_percent"] is None
    assert body["execution_edge"]["total"] is None
    assert body["issues"]["count"] == 0
    assert body["strategies"] == {"list": [], "live_master_id": None, "ambiguous": False}


# ── copy fill rate ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_copy_fill_rate_from_live_fixture(api_client, db_session):
    day = trading_day()
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session)
        for row in on_day(CHILD_ORDER_BOOK, day):
            filled = row["filledQuantity"]
            session.add(Order(
                internal_order_id=f"JT_CH_{row['brokerOrderId']}", broker_order_id=row["brokerOrderId"],
                account_id=child.id, source=OrderSource.SYSTEM,
                master_broker_order_id="MASTER_" + row["brokerOrderId"], exchange="NSE", symbol=row["tradingSymbol"],
                side=OrderSide.BUY if row["transactionType"] == "BUY" else OrderSide.SELL,
                order_type="LIMIT", product_type="INTRADAY", quantity=row["quantity"], filled_quantity=filled,
                price=Decimal(str(row["price"])), average_price=Decimal(str(row["averageTradedPrice"])),
                status=OrderStatus.COMPLETE if filled == row["quantity"] else OrderStatus.CANCELLED,
            ))
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    copy_fill = resp.json()["copy_fill"]
    assert copy_fill["target_quantity"] == 35  # 1+1+1+1+10+5+5+10+1(cancelled)
    assert copy_fill["filled_quantity"] == 34
    assert copy_fill["filled_orders"] == 8
    assert copy_fill["rejected_orders"] == 1
    assert copy_fill["working_orders"] == 0
    assert copy_fill["rate_percent"] == pytest.approx(34 / 35 * 100, abs=0.05)


@pytest.mark.asyncio
async def test_copy_fill_rate_excludes_orders_without_a_master_reference(api_client, db_session):
    """A manual square-off (source=SYSTEM, master_broker_order_id=None) is not
    a copy and must not distort the fill rate."""
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        session.add(Order(
            internal_order_id="JT_SQOFF", broker_order_id="SQ1", account_id=child.id,
            source=OrderSource.SYSTEM, master_broker_order_id=None, exchange="NSE", symbol="CANBK",
            side=OrderSide.SELL, order_type="MARKET", product_type="INTRADAY", quantity=100, filled_quantity=100,
            price=Decimal("0"), average_price=Decimal("0"), status=OrderStatus.COMPLETE,
        ))
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    assert resp.json()["copy_fill"] == {
        "filled_quantity": 0, "target_quantity": 0, "rate_percent": None,
        "filled_orders": 0, "working_orders": 0, "rejected_orders": 0, "considered_orders": 0,
    }


# ── copy execution edge ──────────────────────────────────────────────────────

async def _copy_pair(session, master_account, child_account, side, master_price, child_price, qty=1,
                      master_boid="M1", child_boid="C1", group_strategy_id=None, created_at=None):
    # The "master order" row must live on the actual master account - the
    # execution-edge lookup is scoped to MASTER-type accounts (see
    # dashboard.py's master_rows query), so putting it on the follower's own
    # account (as this helper did before) would no longer be found at all.
    master_kwargs = {"created_at": created_at} if created_at is not None else {}
    session.add(Order(
        internal_order_id=f"EXT_{master_boid}", broker_order_id=master_boid, account_id=master_account.id,
        source=OrderSource.BROKER_EXTERNAL, exchange="NSE", symbol="CANBK", side=side, order_type="MARKET",
        product_type="INTRADAY", quantity=qty, filled_quantity=qty, price=Decimal("0"),
        average_price=Decimal(str(master_price)), status=OrderStatus.COMPLETE, **master_kwargs,
    ))
    child_kwargs = {"created_at": created_at} if created_at is not None else {}
    session.add(Order(
        internal_order_id=f"JT_CH_{child_boid}", broker_order_id=child_boid, account_id=child_account.id,
        source=OrderSource.SYSTEM, master_broker_order_id=master_boid, exchange="NSE", symbol="CANBK",
        side=side, order_type="LIMIT", product_type="INTRADAY", quantity=qty, filled_quantity=qty,
        price=Decimal(str(child_price)), average_price=Decimal(str(child_price)), status=OrderStatus.COMPLETE,
        group_strategy_id=group_strategy_id, **child_kwargs,
    ))


@pytest.mark.asyncio
async def test_execution_edge_buy_worse_when_child_pays_more(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        await _copy_pair(session, master, child, OrderSide.BUY, master_price="100.00", child_price="100.75", qty=1)
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    edge = resp.json()["execution_edge"]
    assert edge["total"] == "-0.75"
    assert edge["average_per_share"] == "-0.75"
    assert edge["total_quantity"] == 1
    assert edge["direction"] == "children_worse"


@pytest.mark.asyncio
async def test_execution_edge_buy_better_when_child_pays_less(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        await _copy_pair(session, master, child, OrderSide.BUY, master_price="100.00", child_price="99.50", qty=1)
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    edge = resp.json()["execution_edge"]
    assert edge["total"] == "0.50"
    assert edge["average_per_share"] == "0.50"
    assert edge["direction"] == "children_better"
    assert edge["items"][0] == {
        "symbol": "CANBK", "side": "BUY", "time": edge["items"][0]["time"], "quantity": 1,
        "master_price": "100.00", "child_price": "99.50",
        "edge_per_share": "0.50", "impact": "0.50",
        "child_account_id": str(child.id), "group_strategy_id": None,
    }


@pytest.mark.asyncio
async def test_execution_edge_sell_worse_when_child_receives_less(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        await _copy_pair(session, master, child, OrderSide.SELL, master_price="100.00", child_price="99.10", qty=1)
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    edge = resp.json()["execution_edge"]
    assert edge["total"] == "-0.90"
    assert edge["direction"] == "children_worse"


@pytest.mark.asyncio
async def test_execution_edge_sell_better_when_child_receives_more(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        await _copy_pair(session, master, child, OrderSide.SELL, master_price="100.00", child_price="100.30", qty=1)
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    edge = resp.json()["execution_edge"]
    assert edge["total"] == "0.30"
    assert edge["direction"] == "children_better"


@pytest.mark.asyncio
async def test_execution_edge_is_quantity_weighted_not_a_flat_sum(api_client, db_session):
    """A 1-share fill and a 1,000-share fill must NOT count equally: the
    same per-share edge on a bigger fill must move the total proportionally
    more. This is the exact bug this change fixes."""
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        # Same +0.10/share edge on both, but very different sizes.
        await _copy_pair(session, master, child, OrderSide.BUY, master_price="100.10", child_price="100.00",
                          qty=1, master_boid="M1", child_boid="C1")
        await _copy_pair(session, master, child, OrderSide.BUY, master_price="100.10", child_price="100.00",
                          qty=1000, master_boid="M2", child_boid="C2")
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    edge = resp.json()["execution_edge"]
    # (0.10 * 1) + (0.10 * 1000) = 100.10, not 0.10 + 0.10 = 0.20 (the old unweighted sum).
    assert edge["total"] == "100.10"
    assert edge["total_quantity"] == 1001
    assert edge["average_per_share"] == "0.10"
    items_by_qty = {item["quantity"]: item for item in edge["items"]}  # avoid depending on tie-break order
    assert (items_by_qty[1]["edge_per_share"], items_by_qty[1]["impact"]) == ("0.10", "0.10")
    assert (items_by_qty[1000]["edge_per_share"], items_by_qty[1000]["impact"]) == ("0.10", "100.00")


@pytest.mark.asyncio
async def test_execution_edge_multiple_fills_and_average_per_share(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        # +0.01/share x 1 = +0.01 ; +0.03/share x 1 = +0.03 (the live-fixture worked example)
        await _copy_pair(session, master, child, OrderSide.BUY, "123.54", "123.53", qty=1, master_boid="M1", child_boid="C1")
        await _copy_pair(session, master, child, OrderSide.SELL, "123.52", "123.55", qty=1, master_boid="M2", child_boid="C2")
        # A larger fill with a worse edge, to prove the average is impact-weighted, not item-averaged.
        await _copy_pair(session, master, child, OrderSide.BUY, "100.00", "101.00", qty=10, master_boid="M3", child_boid="C3")
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    edge = resp.json()["execution_edge"]
    # total = 0.01 + 0.03 + (10 * -1.00) = -9.96 ; quantity = 1 + 1 + 10 = 12
    assert edge["total"] == "-9.96"
    assert edge["sample_count"] == 3
    assert edge["total_quantity"] == 12
    assert edge["average_per_share"] == "-0.83"  # -9.96 / 12, NOT the plain average of the three per-share edges
    assert edge["direction"] == "children_worse"
    # Execution edge is never folded into a P&L figure.
    body = resp.json()
    assert body["pnl"]["combined_total"] != edge["total"]


@pytest.mark.asyncio
async def test_execution_edge_zero_filled_quantity_is_excluded(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        session.add(Order(
            internal_order_id="EXT_M_ZERO", broker_order_id="M_ZERO", account_id=child.id,
            source=OrderSource.BROKER_EXTERNAL, exchange="NSE", symbol="CANBK", side=OrderSide.BUY,
            order_type="MARKET", product_type="INTRADAY", quantity=5, filled_quantity=5, price=Decimal("0"),
            average_price=Decimal("100.00"), status=OrderStatus.COMPLETE,
        ))
        # Copy exists but never filled (still OPEN) - zero quantity must not contribute.
        session.add(Order(
            internal_order_id="JT_CH_ZERO", broker_order_id="C_ZERO", account_id=child.id,
            source=OrderSource.SYSTEM, master_broker_order_id="M_ZERO", exchange="NSE", symbol="CANBK",
            side=OrderSide.BUY, order_type="LIMIT", product_type="INTRADAY", quantity=5, filled_quantity=0,
            price=Decimal("100.00"), average_price=Decimal("0"), status=OrderStatus.OPEN,
        ))
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    edge = resp.json()["execution_edge"]
    assert edge == {"total": None, "average_per_share": None, "total_quantity": 0, "sample_count": 0,
                     "direction": None, "items": []}


@pytest.mark.asyncio
async def test_execution_edge_missing_master_price_is_excluded(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        session.add(Order(
            internal_order_id="EXT_M_NOPRICE", broker_order_id="M_NOPRICE", account_id=child.id,
            source=OrderSource.BROKER_EXTERNAL, exchange="NSE", symbol="CANBK", side=OrderSide.BUY,
            order_type="MARKET", product_type="INTRADAY", quantity=5, filled_quantity=5, price=Decimal("0"),
            average_price=Decimal("0"), status=OrderStatus.COMPLETE,  # broker never reported a fill price
        ))
        session.add(Order(
            internal_order_id="JT_CH_NOPRICE", broker_order_id="C_NOPRICE", account_id=child.id,
            source=OrderSource.SYSTEM, master_broker_order_id="M_NOPRICE", exchange="NSE", symbol="CANBK",
            side=OrderSide.BUY, order_type="LIMIT", product_type="INTRADAY", quantity=5, filled_quantity=5,
            price=Decimal("100.00"), average_price=Decimal("100.00"), status=OrderStatus.COMPLETE,
        ))
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    edge = resp.json()["execution_edge"]
    assert edge["sample_count"] == 0
    assert edge["total"] is None


@pytest.mark.asyncio
async def test_execution_edge_never_binds_a_follower_order_as_the_master(api_client, db_session):
    """broker_order_id is only unique per (broker, account_id) - see Order's
    own uq_orders_broker_account_broker_order constraint - so a follower's
    own order can coincidentally share a broker_order_id string with the
    master's. The master lookup must be scoped to the master account, never
    just 'any order with this id'."""
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        decoy_follower = await _account(session, AccountType.FOLLOWER, "Decoy", "AB_DECOY")
        # A completely unrelated follower order that happens to share the
        # SAME broker_order_id string as the real master order below.
        session.add(Order(
            internal_order_id="JT_CH_DECOY", broker_order_id="COLLIDE1", account_id=decoy_follower.id,
            source=OrderSource.SYSTEM, exchange="NSE", symbol="CANBK", side=OrderSide.BUY,
            order_type="LIMIT", product_type="INTRADAY", quantity=1, filled_quantity=1,
            price=Decimal("999.99"), average_price=Decimal("999.99"), status=OrderStatus.COMPLETE,
        ))
        # The real master order, same broker_order_id string, on the actual master account.
        session.add(Order(
            internal_order_id="EXT_COLLIDE1", broker_order_id="COLLIDE1", account_id=master.id,
            source=OrderSource.BROKER_EXTERNAL, exchange="NSE", symbol="CANBK", side=OrderSide.BUY,
            order_type="MARKET", product_type="INTRADAY", quantity=1, filled_quantity=1,
            price=Decimal("0"), average_price=Decimal("100.10"), status=OrderStatus.COMPLETE,
        ))
        # The real child's copy of the master's order.
        session.add(Order(
            internal_order_id="JT_CH_REAL", broker_order_id="C_REAL", account_id=child.id,
            source=OrderSource.SYSTEM, master_broker_order_id="COLLIDE1", exchange="NSE", symbol="CANBK",
            side=OrderSide.BUY, order_type="LIMIT", product_type="INTRADAY", quantity=1, filled_quantity=1,
            price=Decimal("100.00"), average_price=Decimal("100.00"), status=OrderStatus.COMPLETE,
        ))
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    edge = resp.json()["execution_edge"]
    assert edge["sample_count"] == 1
    assert edge["items"][0]["master_price"] == "100.10"  # the real master's price
    assert edge["items"][0]["edge_per_share"] == "0.10"  # 100.10 - 100.00
    assert edge["total"] == "0.10"
    # If the decoy follower order had been bound instead, this would be "999.99".
    assert edge["items"][0]["master_price"] != "999.99"


@pytest.mark.asyncio
async def test_execution_edge_empty_data(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    edge = resp.json()["execution_edge"]
    assert edge == {"total": None, "average_per_share": None, "total_quantity": 0, "sample_count": 0,
                     "direction": None, "items": []}


# ── execution issues ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_issues_excludes_known_false_positive_position_mismatch(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        session.add(AuditLog(event_type="RISK_REJECTED", account_id=child.id, symbol="CANBK", side="BUY",
                              quantity=5, reason_code="INSUFFICIENT_MARGIN"))
        session.add(SystemEvent(event_type="CANCELLATION_VERIFICATION_FAILED", severity=EventSeverity.CRITICAL,
                                 account_id=child.id, message="Could not verify post-cancel fill state"))
        session.add(SystemEvent(event_type="POSITION_MISMATCH_DETECTED", severity=EventSeverity.CRITICAL,
                                 account_id=child.id, message="Known false positive - must be excluded"))
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    issues = resp.json()["issues"]
    assert issues["count"] == 2
    types = {item["type"] for item in issues["items"]}
    assert types == {"RISK_REJECTED", "CANCELLATION_VERIFICATION_FAILED"}
    assert "POSITION_MISMATCH_DETECTED" not in [item["type"] for item in issues["items"]]


@pytest.mark.asyncio
async def test_issues_preserve_a_legitimate_zero_quantity(api_client, db_session):
    """quantity=0 is a real, valid value (e.g. a ZERO_LOT_RESULT fail-closed
    skip, per QuantityCalculator) - it must render as 'x0' in the issue
    message, not be silently dropped by Python's truthiness."""
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        session.add(AuditLog(event_type="RISK_REJECTED", account_id=child.id, symbol="CANBK", side="BUY",
                              quantity=0, reason_code="ZERO_LOT_RESULT"))
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    issues = resp.json()["issues"]
    assert issues["count"] == 1
    assert "CANBK BUY x0" in issues["items"][0]["message"]


@pytest.mark.asyncio
async def test_issues_respect_ist_trading_day_boundary(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        start = trading_day_start_utc(trading_day())
        session.add(AuditLog(event_type="COPY_FAILED", account_id=child.id, created_at=start - timedelta(minutes=1)))
        session.add(AuditLog(event_type="COPY_FAILED", account_id=child.id, created_at=start + timedelta(minutes=1)))
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    assert resp.json()["issues"]["count"] == 1  # yesterday's issue (IST) is excluded


# ── strategy performance: every real strategy, only one ever RUNNING ────────

@pytest.mark.asyncio
async def test_strategy_performance_lists_every_real_strategy_running_shows_own_pnl(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session)
        strat = await _strategy(session, "master", master, [(child, 1.0, True)])
        await _copy_pair(session, master, child, OrderSide.BUY, "123.54", "123.53",
                          master_boid="M1", child_boid="C1", group_strategy_id=strat.id)
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    strategies = resp.json()["strategies"]
    assert strategies["ambiguous"] is False
    assert strategies["live_master_id"] == str(master.id)
    assert len(strategies["list"]) == 1
    row = strategies["list"][0]
    assert row["name"] == "master"
    assert row["status"] == "RUNNING"
    assert row["status_reason"] is None
    assert row["master"]["display_name"] == "Dhinesh"
    assert row["follower_count"] == 1
    assert row["followers"][0]["multiplier"] == "1.00"  # StrategyMember.multiplier is Numeric(6,2)
    assert row["pnl"]["master_pnl"] == "-1.52"
    assert row["pnl"]["followers_pnl"] == "-1.48"
    assert row["pnl"]["total_pnl"] == "-3.00"
    assert row["pnl"]["unavailable_reason"] is None
    assert row["execution"]["execution_impact"] == "0.01"
    assert row["execution"]["rate_percent"] == 100.0


@pytest.mark.asyncio
async def test_strategy_performance_rounds_pnl_once_not_per_member(db_session):
    """A strategy's master_pnl/followers_pnl/total_pnl must sum each
    account's RAW (unrounded) Decimal total and round exactly once - summing
    already-rounded per-account strings would double-round. Two followers
    each contributing exactly 0.005 round individually to "0.01" apiece
    (old, buggy sum: "0.02"); their raw sum is 0.010, which rounds once to
    the correct "0.01"."""
    from datetime import datetime, timezone

    from app.api.dashboard import CopyExecution, _build_strategy_performance, _load_strategies
    from app.services.pnl.calculator import reconcile_with_broker
    from app.services.pnl.service import AccountPnlSummary

    async with TestingSessionLocal() as session:
        master = await _account(session, AccountType.MASTER, "Master", "AB_ROUND_M")
        child_a = await _account(session, AccountType.FOLLOWER, "ChildA", "AB_ROUND_A")
        child_b = await _account(session, AccountType.FOLLOWER, "ChildB", "AB_ROUND_B")
        await _strategy(session, "rounding", master, [(child_a, 1.0, True), (child_b, 1.0, True)])
        await session.commit()

    def summary(account_id: str, total: Decimal) -> AccountPnlSummary:
        return AccountPnlSummary(
            account_id=account_id, trading_day=trading_day(), as_of=datetime.now(timezone.utc), status="OK",
            realized=total, unrealized=Decimal("0"), total=total, open_positions_count=0,
            broker_check=reconcile_with_broker(None, None), notes=[],
        )

    pnl_summaries = {
        str(master.id): summary(str(master.id), Decimal("1.00")),
        str(child_a.id): summary(str(child_a.id), Decimal("0.005")),
        str(child_b.id): summary(str(child_b.id), Decimal("0.005")),
    }
    empty_execution = CopyExecution(
        filled_quantity=0, target_quantity=0, filled_orders=0, working_orders=0, rejected_orders=0,
        considered_orders=0, impact_total=None, total_filled_quantity=0, average_edge_per_share=None,
        edge_items=[], impact_total_by_account={},
    )

    async with TestingSessionLocal() as session:
        strategies = await _load_strategies(session)
    payload = _build_strategy_performance(
        strategies, str(master.id), {str(master.id)}, pnl_summaries, {}, empty_execution, is_today=True
    )

    row = payload["list"][0]
    assert row["pnl"]["followers_pnl"] == "0.01"  # NOT "0.02" (0.01 + 0.01 double-rounded)
    assert row["pnl"]["total_pnl"] == "1.01"       # NOT "1.02"


@pytest.mark.asyncio
async def test_strategy_pnl_no_historical_orders_is_a_verified_zero(api_client, db_session):
    """PnlService (live broker reads) has no historical capability, but
    Jettone's own `orders` table does - a strategy with genuinely no order
    activity on a past day has a real, verified realized P&L of exactly
    ₹0.00, not the same "unavailable" bucket as data that couldn't be
    trusted. master_pnl/followers_pnl/total_pnl (gross totals - need
    unrealized, which has no historical source) stay unavailable."""
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session)
        await _strategy(session, "master", master, [(child, 1.0, True)])
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary?range=yesterday", headers=AUTH_HEADER)
    row = resp.json()["strategies"]["list"][0]
    assert row["pnl"] == {
        "master_pnl": None, "followers_pnl": None, "total_pnl": None,
        "realized_pnl": "0.00", "unavailable_reason": None, "partial_note": None,
    }


@pytest.mark.asyncio
async def test_strategy_pnl_yesterday_reflects_real_historical_orders(api_client, db_session):
    """A real round-trip fill on the master account yesterday must show up
    as that day's realized P&L when Yesterday is selected."""
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session)
        await _strategy(session, "master", master, [(child, 1.0, True)])
    _install(clients, live_master_id=str(master.id))

    yesterday = trading_day() - timedelta(days=1)
    yesterday_utc = trading_day_start_utc(yesterday) + timedelta(hours=6)
    async with TestingSessionLocal() as session:
        session.add(Order(
            id=uuid.uuid4(), internal_order_id="HIST_BUY", broker_order_id="HIST_BUY",
            account_id=master.id, source=OrderSource.BROKER_EXTERNAL, broker="ALICE_BLUE",
            exchange="NSE", symbol="RELIANCE", side=OrderSide.BUY, order_type="MARKET", product_type="MIS",
            quantity=10, filled_quantity=10, pending_quantity=0,
            price=Decimal("100.00"), average_price=Decimal("100.00"), status=OrderStatus.COMPLETE,
            created_at=yesterday_utc,
        ))
        session.add(Order(
            id=uuid.uuid4(), internal_order_id="HIST_SELL", broker_order_id="HIST_SELL",
            account_id=master.id, source=OrderSource.BROKER_EXTERNAL, broker="ALICE_BLUE",
            exchange="NSE", symbol="RELIANCE", side=OrderSide.SELL, order_type="MARKET", product_type="MIS",
            quantity=10, filled_quantity=10, pending_quantity=0,
            price=Decimal("105.00"), average_price=Decimal("105.00"), status=OrderStatus.COMPLETE,
            created_at=yesterday_utc + timedelta(hours=1),
        ))
        await session.commit()

    resp = await api_client.get("/api/v1/dashboard/summary?range=yesterday", headers=AUTH_HEADER)
    body = resp.json()
    assert body["pnl"]["combined_realized"] == "50.00"
    assert body["pnl"]["combined_unrealized"] is None
    assert body["pnl"]["combined_total"] is None
    assert body["pnl"]["unrealized_unavailable_reason"] is not None

    row = body["strategies"]["list"][0]
    assert row["pnl"]["realized_pnl"] == "50.00"
    assert row["pnl"]["total_pnl"] is None


@pytest.mark.asyncio
async def test_strategy_pnl_unavailable_when_historical_position_never_closes(api_client, db_session):
    """A position still open at the end of a past day - a real overnight
    carry, or the flat-at-open assumption being wrong - must never be
    priced with today's LTP or silently dropped; realized P&L for that
    account/range fails closed instead."""
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session)
        await _strategy(session, "master", master, [(child, 1.0, True)])
    _install(clients, live_master_id=str(master.id))

    yesterday = trading_day() - timedelta(days=1)
    yesterday_utc = trading_day_start_utc(yesterday) + timedelta(hours=6)
    async with TestingSessionLocal() as session:
        session.add(Order(
            id=uuid.uuid4(), internal_order_id="HIST_OPEN", broker_order_id="HIST_OPEN",
            account_id=master.id, source=OrderSource.BROKER_EXTERNAL, broker="ALICE_BLUE",
            exchange="NSE", symbol="RELIANCE", side=OrderSide.BUY, order_type="MARKET", product_type="MIS",
            quantity=10, filled_quantity=10, pending_quantity=0,
            price=Decimal("100.00"), average_price=Decimal("100.00"), status=OrderStatus.COMPLETE,
            created_at=yesterday_utc,
        ))
        await session.commit()

    resp = await api_client.get("/api/v1/dashboard/summary?range=yesterday", headers=AUTH_HEADER)
    body = resp.json()
    assert body["pnl"]["combined_realized"] is None
    assert body["pnl"]["realized_unavailable_reason"] is not None
    row = body["strategies"]["list"][0]
    assert row["pnl"]["realized_pnl"] is None
    assert row["pnl"]["unavailable_reason"] is not None


@pytest.mark.asyncio
async def test_strategy_pnl_is_master_anchored_and_excludes_unverifiable_follower(api_client, db_session):
    """A follower's own unresolved historical carry must never blank out the
    strategy's realized P&L when the master's own figure is fully verified -
    the master's real trading activity is what this number primarily means.
    The excluded follower is named in `partial_note`, never silently dropped
    or guessed as zero (mirrors the real Dhinesh/vikash production case)."""
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session)
        await _strategy(session, "master", master, [(child, 1.0, True)])
    _install(clients, live_master_id=str(master.id))

    yesterday = trading_day() - timedelta(days=1)
    yesterday_utc = trading_day_start_utc(yesterday) + timedelta(hours=6)
    async with TestingSessionLocal() as session:
        # Master: a clean, fully-closed round trip - realized +50.
        session.add(Order(
            id=uuid.uuid4(), internal_order_id="M_BUY", broker_order_id="M_BUY",
            account_id=master.id, source=OrderSource.BROKER_EXTERNAL, broker="ALICE_BLUE",
            exchange="NSE", symbol="RELIANCE", side=OrderSide.BUY, order_type="MARKET", product_type="MIS",
            quantity=10, filled_quantity=10, pending_quantity=0,
            price=Decimal("100.00"), average_price=Decimal("100.00"), status=OrderStatus.COMPLETE,
            created_at=yesterday_utc,
        ))
        session.add(Order(
            id=uuid.uuid4(), internal_order_id="M_SELL", broker_order_id="M_SELL",
            account_id=master.id, source=OrderSource.BROKER_EXTERNAL, broker="ALICE_BLUE",
            exchange="NSE", symbol="RELIANCE", side=OrderSide.SELL, order_type="MARKET", product_type="MIS",
            quantity=10, filled_quantity=10, pending_quantity=0,
            price=Decimal("105.00"), average_price=Decimal("105.00"), status=OrderStatus.COMPLETE,
            created_at=yesterday_utc + timedelta(hours=1),
        ))
        # Child (vikash): a copied fill left open - never closed at the broker.
        session.add(Order(
            id=uuid.uuid4(), internal_order_id="C_BUY", broker_order_id="C_BUY",
            account_id=child.id, source=OrderSource.SYSTEM, broker="ALICE_BLUE",
            exchange="NSE", symbol="YESBANK", side=OrderSide.BUY, order_type="MARKET", product_type="MIS",
            quantity=5, filled_quantity=5, pending_quantity=0,
            price=Decimal("20.00"), average_price=Decimal("20.00"), status=OrderStatus.COMPLETE,
            created_at=yesterday_utc,
        ))
        await session.commit()

    resp = await api_client.get("/api/v1/dashboard/summary?range=yesterday", headers=AUTH_HEADER)
    row = resp.json()["strategies"]["list"][0]
    assert row["pnl"]["realized_pnl"] == "50.00"
    assert row["pnl"]["unavailable_reason"] is None
    assert row["pnl"]["partial_note"] is not None
    assert "vikash" in row["pnl"]["partial_note"]


@pytest.mark.asyncio
async def test_strategy_not_running_when_master_not_live(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        strat = await _strategy(session, "master", master, [(child, 1.0, True)])
    _install(clients, live_master_id=None)  # engine has no live master session

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    row = resp.json()["strategies"]["list"][0]
    assert row["id"] == str(strat.id)
    assert row["status"] == "NOT_RUNNING"
    assert row["status_reason"] == "No live master session"


@pytest.mark.asyncio
async def test_strategy_disabled_is_its_own_status(api_client, db_session):
    from sqlalchemy import select

    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        strat = await _strategy(session, "paused", master, [(child, 1.0, True)])
        row = (await session.execute(select(GroupStrategy).where(GroupStrategy.id == strat.id))).scalar_one()
        row.trading_enabled = False
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    row = resp.json()["strategies"]["list"][0]
    assert row["status"] == "DISABLED"
    assert row["status_reason"] == "Trading disabled for this strategy"


@pytest.mark.asyncio
async def test_disabled_strategy_is_never_picked_as_running_even_if_master_live(api_client, db_session):
    """A disabled strategy must never show RUNNING, even when its master is
    the live one and it would otherwise be the sole candidate."""
    from sqlalchemy import select

    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        strat = await _strategy(session, "off", master, [(child, 1.0, True)])
        row = (await session.execute(select(GroupStrategy).where(GroupStrategy.id == strat.id))).scalar_one()
        row.trading_enabled = False
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    strategies = resp.json()["strategies"]
    assert strategies["list"][0]["status"] == "DISABLED"
    assert strategies["ambiguous"] is False


@pytest.mark.asyncio
async def test_multiple_qualifying_strategies_flagged_ambiguous(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        await _strategy(session, "first", master, [(child, 1.0, True)])
        await _strategy(session, "second", master, [(child, 2.0, True)])
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    strategies = resp.json()["strategies"]
    assert strategies["ambiguous"] is True
    by_name = {row["name"]: row for row in strategies["list"]}
    assert by_name["first"]["status"] == "RUNNING"  # stable display choice only
    assert by_name["second"]["status"] == "NOT_RUNNING"
    assert "engine runs one strategy per master" in by_name["second"]["status_reason"]


# ── orders by strategy ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_orders_by_strategy_groups_real_copies_including_unassigned(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        strat = await _strategy(session, "Intraday Alpha", master, [(child, 1.0, True)])
        await _copy_pair(session, master, child, OrderSide.BUY, "100.00", "100.00",
                          master_boid="M1", child_boid="C1", group_strategy_id=strat.id)
        await _copy_pair(session, master, child, OrderSide.SELL, "101.00", "101.00",
                          master_boid="M2", child_boid="C2", group_strategy_id=strat.id)
        # A fallback-mode copy with no strategy link (see the engine's
        # documented "no GroupStrategy for this master" fallback).
        await _copy_pair(session, master, child, OrderSide.BUY, "102.00", "102.00",
                          master_boid="M3", child_boid="C3", group_strategy_id=None)
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    by_strategy = resp.json()["orders_by_strategy"]
    by_name = {row["strategy_name"]: row["count"] for row in by_strategy}
    assert by_name == {"Intraday Alpha": 2, "Unassigned": 1}


@pytest.mark.asyncio
async def test_orders_by_strategy_never_hides_a_configured_strategy_with_zero_orders(api_client, db_session):
    """A real, configured GroupStrategy with no copy activity in the
    selected range must still appear (at count 0) - the same strategy list
    Strategy Performance uses, so the two sections never silently disagree
    on which strategies exist."""
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        active = await _strategy(session, "master", master, [(child, 1.0, True)])
        await _strategy(session, "intraday", master, [])  # configured, but never copied to
        await _copy_pair(session, master, child, OrderSide.BUY, "100.00", "100.00",
                          master_boid="M1", child_boid="C1", group_strategy_id=active.id)
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    by_strategy = resp.json()["orders_by_strategy"]
    by_name = {row["strategy_name"]: row["count"] for row in by_strategy}
    assert by_name == {"master": 1, "intraday": 0}


# ── real trading impact ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_real_trading_impact_reflects_the_running_strategy(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session)
        strat = await _strategy(session, "master", master, [(child, 1.0, True)])
        await _copy_pair(session, master, child, OrderSide.BUY, "123.54", "123.53",
                          master_boid="M1", child_boid="C1", group_strategy_id=strat.id)
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    impact = resp.json()["real_trading_impact"]
    assert impact["strategy_name"] == "master"
    assert impact["master_pnl"] == "-1.52"
    assert impact["followers_pnl"] == "-1.48"
    assert impact["copy_execution_impact"] == "0.01"
    assert impact["copied_orders"] == 1
    assert impact["filled_copies"] == 1
    assert impact["failed_missed_copies"] == 0


@pytest.mark.asyncio
async def test_real_trading_impact_is_null_when_nothing_is_running(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        await _strategy(session, "master", master, [(child, 1.0, True)])
    _install(clients, live_master_id=None)

    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    assert resp.json()["real_trading_impact"] is None


# ── date range: order-table metrics honor it, platform state never does ─────

@pytest.mark.asyncio
async def test_range_defaults_to_today_and_platform_row_is_range_independent(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        await _strategy(session, "master", master, [(child, 1.0, True)])
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    today_resp = (await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)).json()
    assert today_resp["range"] == {"key": "today", "start": today_resp["trading_day"],
                                    "end": today_resp["trading_day"], "is_today": True, "includes_today": True}

    yesterday_resp = (await api_client.get("/api/v1/dashboard/summary?range=yesterday", headers=AUTH_HEADER)).json()
    assert yesterday_resp["range"]["is_today"] is False
    # Platform-row facts (accounts/strategies) never change with the date filter.
    assert yesterday_resp["platform"] == today_resp["platform"] == {
        "demat_accounts": {"total": 2, "connected": 2},  # _account() defaults is_active/session_valid True
        "static_ip": None,
        "strategies": {"total": 1, "active": 1},
    }


@pytest.mark.asyncio
async def test_range_yesterday_includes_only_yesterdays_orders(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        yesterday_start = trading_day_start_utc(trading_day() - timedelta(days=1))
        await _copy_pair(session, master, child, OrderSide.BUY, "100.00", "100.00",
                          master_boid="Y1", child_boid="YC1", created_at=yesterday_start + timedelta(hours=2))
        await _copy_pair(session, master, child, OrderSide.BUY, "100.00", "100.00",
                          master_boid="T1", child_boid="TC1")  # today (default created_at)
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    today = (await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)).json()
    assert today["copy_fill"]["considered_orders"] == 1

    yesterday = (await api_client.get("/api/v1/dashboard/summary?range=yesterday", headers=AUTH_HEADER)).json()
    assert yesterday["copy_fill"]["considered_orders"] == 1
    assert yesterday["copy_fill"]["filled_quantity"] == 1


@pytest.mark.asyncio
async def test_range_7d_and_30d_include_a_week_old_order(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        old_start = trading_day_start_utc(trading_day() - timedelta(days=5))
        await _copy_pair(session, master, child, OrderSide.BUY, "100.00", "100.00",
                          master_boid="W1", child_boid="WC1", created_at=old_start + timedelta(hours=1))
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    resp_7d = (await api_client.get("/api/v1/dashboard/summary?range=7d", headers=AUTH_HEADER)).json()
    assert resp_7d["copy_fill"]["considered_orders"] == 1
    resp_30d = (await api_client.get("/api/v1/dashboard/summary?range=30d", headers=AUTH_HEADER)).json()
    assert resp_30d["copy_fill"]["considered_orders"] == 1
    resp_today = (await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)).json()
    assert resp_today["copy_fill"]["considered_orders"] == 0


@pytest.mark.asyncio
async def test_range_7d_combines_historical_realized_with_todays_live_pnl(api_client, db_session):
    """A "Last 7 Days" window always extends through Today, so its realized
    P&L must be the completed historical days (from Jettone's own orders)
    PLUS Today's own live realized (the fixture's known -1.52/-1.48) - never
    just one or the other. Unrealized/total use Today's live snapshot,
    since an open position's unrealized P&L is a point-in-time fact as of
    right now, which is also the end of a range whose end is Today."""
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session)  # live fixture: -1.52 / -1.48, both flat
        await _strategy(session, "master", master, [(child, 1.0, True)])
    _install(clients, live_master_id=str(master.id))

    five_days_ago = trading_day_start_utc(trading_day() - timedelta(days=5))
    async with TestingSessionLocal() as session:
        session.add(Order(
            id=uuid.uuid4(), internal_order_id="HIST7_BUY", broker_order_id="HIST7_BUY",
            account_id=master.id, source=OrderSource.BROKER_EXTERNAL, broker="ALICE_BLUE",
            exchange="NSE", symbol="TCS", side=OrderSide.BUY, order_type="MARKET", product_type="MIS",
            quantity=1, filled_quantity=1, pending_quantity=0,
            price=Decimal("1000.00"), average_price=Decimal("1000.00"), status=OrderStatus.COMPLETE,
            created_at=five_days_ago + timedelta(hours=1),
        ))
        session.add(Order(
            id=uuid.uuid4(), internal_order_id="HIST7_SELL", broker_order_id="HIST7_SELL",
            account_id=master.id, source=OrderSource.BROKER_EXTERNAL, broker="ALICE_BLUE",
            exchange="NSE", symbol="TCS", side=OrderSide.SELL, order_type="MARKET", product_type="MIS",
            quantity=1, filled_quantity=1, pending_quantity=0,
            price=Decimal("1050.00"), average_price=Decimal("1050.00"), status=OrderStatus.COMPLETE,
            created_at=five_days_ago + timedelta(hours=2),
        ))
        await session.commit()

    resp = await api_client.get("/api/v1/dashboard/summary?range=7d", headers=AUTH_HEADER)
    body = resp.json()
    # historical +50.00 (TCS round trip) + live today (-1.52 + -1.48 = -3.00)
    assert body["pnl"]["combined_realized"] == "47.00"
    assert body["pnl"]["combined_unrealized"] == "0.00"  # both accounts flat today - Today's live snapshot
    assert body["pnl"]["combined_total"] == "47.00"
    assert body["pnl"]["unrealized_unavailable_reason"] is None


@pytest.mark.asyncio
async def test_range_custom_honors_explicit_dates(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        old_day = trading_day() - timedelta(days=3)
        await _copy_pair(session, master, child, OrderSide.BUY, "100.00", "100.00",
                          master_boid="C1", child_boid="CC1",
                          created_at=trading_day_start_utc(old_day) + timedelta(hours=1))
        await session.commit()
    _install(clients, live_master_id=str(master.id))

    old_iso = old_day.isoformat()
    resp = await api_client.get(f"/api/v1/dashboard/summary?range=custom&from={old_iso}&to={old_iso}", headers=AUTH_HEADER)
    body = resp.json()
    assert body["range"] == {"key": "custom", "start": old_iso, "end": old_iso, "is_today": False, "includes_today": False}
    assert body["copy_fill"]["considered_orders"] == 1


@pytest.mark.asyncio
async def test_range_custom_falls_back_to_today_on_bad_input(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
    _install(clients, live_master_id=str(master.id))

    resp = await api_client.get("/api/v1/dashboard/summary?range=custom", headers=AUTH_HEADER)  # no from/to
    assert resp.json()["range"]["key"] == "today"


# ── realized-timeline endpoint: per strategy, combined master + followers ───

@pytest.mark.asyncio
async def test_realized_timeline_combines_master_and_followers_into_one_strategy_line(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session)
        await _strategy(session, "master", master, [(child, 1.0, True)])
    _install(clients)

    resp = await api_client.get("/api/v1/dashboard/realized-timeline", headers=AUTH_HEADER)
    assert resp.status_code == 200
    body = resp.json()
    strategies = list(body["strategies"].values())
    assert len(strategies) == 1
    strat = strategies[0]
    assert strat["name"] == "master"
    assert strat["status"] == "OK"
    # Master alone ends at -1.52, child alone at -1.48; combined must be their
    # raw-Decimal sum (-3.00), not either account's own line.
    assert strat["points"][-1]["cumulative_realized"] == "-3.00"
    assert strat["points"][0]["cumulative_realized"] != "-1.52"


@pytest.mark.asyncio
async def test_realized_timeline_strategy_unavailable_when_any_member_is(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        await _strategy(session, "master", master, [(child, 1.0, True)])
    _install(clients)  # no broker clients -> both accounts UNAVAILABLE

    resp = await api_client.get("/api/v1/dashboard/realized-timeline", headers=AUTH_HEADER)
    strat = list(resp.json()["strategies"].values())[0]
    # The original (main-dashboard) contract is unchanged; `daily` is additive.
    assert {k: strat[k] for k in ("name", "status", "points")} == {"name": "master", "status": "UNAVAILABLE", "points": []}
    # Today's live P&L is unavailable, so the only daily entry is null (not 0)
    # and, with zero usable points, the strategy carries an explicit reason.
    assert [d["daily_pnl"] for d in strat["daily"]] == [None]
    assert strat["unavailable_reason"]


@pytest.mark.asyncio
async def test_realized_timeline_empty_when_no_strategies_configured(api_client, db_session):
    resp = await api_client.get("/api/v1/dashboard/realized-timeline", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json()["strategies"] == {}


@pytest.mark.asyncio
async def test_realized_timeline_yesterday_uses_historical_orders(api_client, db_session):
    """A past, fully-historical range (no live component) must chart
    Jettone's own verified historical fills, ending at the same total the
    Yesterday P&L card shows."""
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        await _strategy(session, "master", master, [(child, 1.0, True)])
    _install(clients, live_master_id=str(master.id))

    yesterday_utc = trading_day_start_utc(trading_day() - timedelta(days=1))
    async with TestingSessionLocal() as session:
        session.add(Order(
            id=uuid.uuid4(), internal_order_id="TL_BUY", broker_order_id="TL_BUY",
            account_id=master.id, source=OrderSource.BROKER_EXTERNAL, broker="ALICE_BLUE",
            exchange="NSE", symbol="RELIANCE", side=OrderSide.BUY, order_type="MARKET", product_type="MIS",
            quantity=10, filled_quantity=10, pending_quantity=0,
            price=Decimal("100.00"), average_price=Decimal("100.00"), status=OrderStatus.COMPLETE,
            created_at=yesterday_utc + timedelta(hours=1),
        ))
        session.add(Order(
            id=uuid.uuid4(), internal_order_id="TL_SELL", broker_order_id="TL_SELL",
            account_id=master.id, source=OrderSource.BROKER_EXTERNAL, broker="ALICE_BLUE",
            exchange="NSE", symbol="RELIANCE", side=OrderSide.SELL, order_type="MARKET", product_type="MIS",
            quantity=10, filled_quantity=10, pending_quantity=0,
            price=Decimal("105.00"), average_price=Decimal("105.00"), status=OrderStatus.COMPLETE,
            created_at=yesterday_utc + timedelta(hours=2),
        ))
        await session.commit()

    resp = await api_client.get("/api/v1/dashboard/realized-timeline?range=yesterday", headers=AUTH_HEADER)
    body = resp.json()
    assert body["range"]["key"] == "yesterday"
    strat = list(body["strategies"].values())[0]
    assert strat["status"] == "OK"
    assert strat["points"][-1]["cumulative_realized"] == "50.00"


@pytest.mark.asyncio
async def test_realized_timeline_7d_continues_the_curve_into_todays_live_points(api_client, db_session):
    """A range extending through Today must not restart the cumulative
    curve at zero for Today's own points - they continue from wherever the
    historical portion left off."""
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session)  # live fixture ends at -3.00 combined
        await _strategy(session, "master", master, [(child, 1.0, True)])
    _install(clients, live_master_id=str(master.id))

    five_days_ago = trading_day_start_utc(trading_day() - timedelta(days=5))
    async with TestingSessionLocal() as session:
        session.add(Order(
            id=uuid.uuid4(), internal_order_id="TL7_BUY", broker_order_id="TL7_BUY",
            account_id=master.id, source=OrderSource.BROKER_EXTERNAL, broker="ALICE_BLUE",
            exchange="NSE", symbol="TCS", side=OrderSide.BUY, order_type="MARKET", product_type="MIS",
            quantity=1, filled_quantity=1, pending_quantity=0,
            price=Decimal("1000.00"), average_price=Decimal("1000.00"), status=OrderStatus.COMPLETE,
            created_at=five_days_ago + timedelta(hours=1),
        ))
        session.add(Order(
            id=uuid.uuid4(), internal_order_id="TL7_SELL", broker_order_id="TL7_SELL",
            account_id=master.id, source=OrderSource.BROKER_EXTERNAL, broker="ALICE_BLUE",
            exchange="NSE", symbol="TCS", side=OrderSide.SELL, order_type="MARKET", product_type="MIS",
            quantity=1, filled_quantity=1, pending_quantity=0,
            price=Decimal("1050.00"), average_price=Decimal("1050.00"), status=OrderStatus.COMPLETE,
            created_at=five_days_ago + timedelta(hours=2),
        ))
        await session.commit()

    resp = await api_client.get("/api/v1/dashboard/realized-timeline?range=7d", headers=AUTH_HEADER)
    strat = list(resp.json()["strategies"].values())[0]
    assert strat["status"] == "OK"
    cumulative = [p["cumulative_realized"] for p in strat["points"]]
    # The historical BUY realizes nothing (opens the position); the
    # historical SELL five days ago realizes +50.00; Today's live points
    # continue from there down to -3.00 (the fixture's combined total),
    # never restarting the curve at zero for Today.
    assert "50.00" in cumulative
    assert cumulative[-1] == "47.00"
    assert cumulative.index("50.00") < len(cumulative) - 1


@pytest.mark.asyncio
async def test_realized_timeline_is_master_anchored_and_excludes_unverifiable_follower(api_client, db_session):
    """The chart's detailed per-fill curve must mirror the same
    master-anchoring as the P&L KPI (_strategy_pnl): a follower with an
    unresolved historical position must never blank the master's own
    verified curve - only flip status to PARTIAL and leave that follower's
    points out of the merge (mirrors the real Dhinesh/vikash production
    case)."""
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        await _strategy(session, "master", master, [(child, 1.0, True)])
    _install(clients, live_master_id=str(master.id))

    yesterday_utc = trading_day_start_utc(trading_day() - timedelta(days=1))
    async with TestingSessionLocal() as session:
        # Master: a clean, fully-closed round trip - realizes +50.
        session.add(Order(
            id=uuid.uuid4(), internal_order_id="TLM_BUY", broker_order_id="TLM_BUY",
            account_id=master.id, source=OrderSource.BROKER_EXTERNAL, broker="ALICE_BLUE",
            exchange="NSE", symbol="RELIANCE", side=OrderSide.BUY, order_type="MARKET", product_type="MIS",
            quantity=10, filled_quantity=10, pending_quantity=0,
            price=Decimal("100.00"), average_price=Decimal("100.00"), status=OrderStatus.COMPLETE,
            created_at=yesterday_utc + timedelta(hours=1),
        ))
        session.add(Order(
            id=uuid.uuid4(), internal_order_id="TLM_SELL", broker_order_id="TLM_SELL",
            account_id=master.id, source=OrderSource.BROKER_EXTERNAL, broker="ALICE_BLUE",
            exchange="NSE", symbol="RELIANCE", side=OrderSide.SELL, order_type="MARKET", product_type="MIS",
            quantity=10, filled_quantity=10, pending_quantity=0,
            price=Decimal("105.00"), average_price=Decimal("105.00"), status=OrderStatus.COMPLETE,
            created_at=yesterday_utc + timedelta(hours=2),
        ))
        # Child (vikash): a copied fill left open - never closed at the broker.
        session.add(Order(
            id=uuid.uuid4(), internal_order_id="TLC_BUY", broker_order_id="TLC_BUY",
            account_id=child.id, source=OrderSource.SYSTEM, broker="ALICE_BLUE",
            exchange="NSE", symbol="YESBANK", side=OrderSide.BUY, order_type="MARKET", product_type="MIS",
            quantity=5, filled_quantity=5, pending_quantity=0,
            price=Decimal("20.00"), average_price=Decimal("20.00"), status=OrderStatus.COMPLETE,
            created_at=yesterday_utc + timedelta(hours=1),
        ))
        await session.commit()

    resp = await api_client.get("/api/v1/dashboard/realized-timeline?range=yesterday", headers=AUTH_HEADER)
    strat = list(resp.json()["strategies"].values())[0]
    assert strat["status"] == "PARTIAL"
    assert strat["points"][-1]["cumulative_realized"] == "50.00"


@pytest.mark.asyncio
async def test_realized_timeline_unavailable_when_historical_position_never_closes(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        await _strategy(session, "master", master, [(child, 1.0, True)])
    _install(clients, live_master_id=str(master.id))

    yesterday_utc = trading_day_start_utc(trading_day() - timedelta(days=1))
    async with TestingSessionLocal() as session:
        session.add(Order(
            id=uuid.uuid4(), internal_order_id="TL_OPEN", broker_order_id="TL_OPEN",
            account_id=master.id, source=OrderSource.BROKER_EXTERNAL, broker="ALICE_BLUE",
            exchange="NSE", symbol="RELIANCE", side=OrderSide.BUY, order_type="MARKET", product_type="MIS",
            quantity=10, filled_quantity=10, pending_quantity=0,
            price=Decimal("100.00"), average_price=Decimal("100.00"), status=OrderStatus.COMPLETE,
            created_at=yesterday_utc + timedelta(hours=1),
        ))
        await session.commit()

    resp = await api_client.get("/api/v1/dashboard/realized-timeline?range=yesterday", headers=AUTH_HEADER)
    strat = list(resp.json()["strategies"].values())[0]
    assert strat["status"] == "UNAVAILABLE"
    assert strat["points"] == []


# ── realized-timeline `daily[]`: per-day (NOT cumulative) P&L, per-day isolation ──

def _hist_order(account, tag, day, offset, side, qty, price, *, exchange="NSE", symbol="RELIANCE", product="MIS"):
    return Order(
        id=uuid.uuid4(), internal_order_id=tag, broker_order_id=tag,
        account_id=account.id, source=OrderSource.BROKER_EXTERNAL, broker="ALICE_BLUE",
        exchange=exchange, symbol=symbol, side=side, order_type="MARKET", product_type=product,
        quantity=qty, filled_quantity=qty, pending_quantity=0,
        price=Decimal(str(price)), average_price=Decimal(str(price)), status=OrderStatus.COMPLETE,
        created_at=trading_day_start_utc(day) + offset,
    )


def _round_trip(account, tag, day, buy, sell, qty=10, **kw):
    return [
        _hist_order(account, f"{tag}_B", day, timedelta(hours=1), OrderSide.BUY, qty, buy, **kw),
        _hist_order(account, f"{tag}_S", day, timedelta(hours=2), OrderSide.SELL, qty, sell, **kw),
    ]


@pytest.mark.asyncio
async def test_realized_timeline_daily_is_per_day_isolated_and_not_cumulative(api_client, db_session):
    """Sep 22-style PARTIAL day (NSE + unidentifiable NFO), a fully valid day,
    a NSE LONGTERM day and an NFO-only day: every day keeps its own value, the
    bad ones never erase the good ones, and the values are NOT running totals."""
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session)  # Today live = -3.00 combined
        await _strategy(session, "master", master, [(child, 1.0, True)])
    _install(clients, live_master_id=str(master.id))

    today = trading_day()
    d5, d4, d3, d2 = (today - timedelta(days=n) for n in (5, 4, 3, 2))
    async with TestingSessionLocal() as session:
        session.add_all(
            _round_trip(master, "P1", d5, 100, 105)                                           # +50
            + _round_trip(master, "P1N", d5, 200, 210, qty=65, exchange="NFO", symbol="NIFTY")  # unidentifiable
            + _round_trip(master, "P2", d4, 100, 103)                                         # +30 (not 80)
            + _round_trip(master, "P3", d3, 100, 102, product="LONGTERM")                     # +20 LONGTERM
            + _round_trip(master, "P4", d2, 200, 210, qty=65, exchange="NFO", symbol="NIFTY")  # NFO-only day
        )
        await session.commit()

    resp = await api_client.get("/api/v1/dashboard/realized-timeline?range=7d", headers=AUTH_HEADER)
    assert resp.status_code == 200
    strat = list(resp.json()["strategies"].values())[0]
    by = {x["date"]: x for x in strat["daily"]}

    assert by[d5.isoformat()]["daily_pnl"] == "50.00" and by[d5.isoformat()]["status"] == "PARTIAL"
    assert any("NFO" in r for r in by[d5.isoformat()]["reasons"])
    assert by[d4.isoformat()]["daily_pnl"] == "30.00" and by[d4.isoformat()]["status"] == "OK"
    assert by[d3.isoformat()]["daily_pnl"] == "20.00" and by[d3.isoformat()]["status"] == "OK"
    # NFO-only day: unknown, not a fabricated 0.
    assert by[d2.isoformat()]["daily_pnl"] is None and by[d2.isoformat()]["status"] == "UNAVAILABLE"
    # Today's live path is unchanged: its own realized only (-3.00 combined), no history added.
    assert by[today.isoformat()]["daily_pnl"] == "-3.00"
    assert strat["unavailable_reason"] is None
    # The shared per-fill cumulative `points` contract is untouched (main dashboard).
    assert set(strat) >= {"name", "status", "points", "daily", "unavailable_reason"}
    assert all(set(p) == {"time", "cumulative_realized"} for p in strat["points"])


@pytest.mark.asyncio
async def test_realized_timeline_daily_survives_the_all_or_nothing_points_failure(api_client, db_session):
    """The exact production failure: an NFO fill in the window makes the
    legacy cumulative `points` UNAVAILABLE. `daily` must still deliver every
    valid day."""
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        await _strategy(session, "master", master, [(child, 1.0, True)])
    _install(clients, live_master_id=str(master.id))

    today = trading_day()
    d3, d2 = today - timedelta(days=3), today - timedelta(days=2)
    async with TestingSessionLocal() as session:
        session.add_all(
            _round_trip(master, "Q1", d3, 100, 105, product="LONGTERM")
            + _round_trip(master, "Q2", d2, 200, 210, qty=65, exchange="NFO", symbol="NIFTY")
        )
        await session.commit()

    resp = await api_client.get("/api/v1/dashboard/realized-timeline?range=7d", headers=AUTH_HEADER)
    strat = list(resp.json()["strategies"].values())[0]
    assert strat["status"] == "UNAVAILABLE" and strat["points"] == []      # legacy behavior preserved
    usable = [x for x in strat["daily"] if x["daily_pnl"] is not None]
    assert [(x["date"], x["daily_pnl"]) for x in usable] == [(d3.isoformat(), "50.00")]
    assert strat["unavailable_reason"] is None                              # a usable point exists


@pytest.mark.asyncio
async def test_realized_timeline_daily_unavailable_reason_only_with_zero_usable_points(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        await _strategy(session, "master", master, [(child, 1.0, True)])
    _install(clients, live_master_id=str(master.id))

    yesterday = trading_day() - timedelta(days=1)
    async with TestingSessionLocal() as session:
        session.add_all(_round_trip(master, "R1", yesterday, 200, 210, qty=65, exchange="NFO", symbol="NIFTY"))
        await session.commit()

    resp = await api_client.get("/api/v1/dashboard/realized-timeline?range=yesterday", headers=AUTH_HEADER)
    strat = list(resp.json()["strategies"].values())[0]
    assert all(x["daily_pnl"] is None for x in strat["daily"])
    assert strat["unavailable_reason"]


@pytest.mark.asyncio
async def test_realized_timeline_daily_uses_ist_trading_day_boundaries(api_client, db_session):
    """A fill at 23:59 IST belongs to that IST day; 00:01 IST the next morning
    belongs to the next day (regardless of the UTC calendar date)."""
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        await _strategy(session, "master", master, [(child, 1.0, True)])
    _install(clients, live_master_id=str(master.id))

    d = trading_day() - timedelta(days=3)
    async with TestingSessionLocal() as session:
        session.add_all([
            _hist_order(master, "IST_B1", d, timedelta(hours=23, minutes=58), OrderSide.BUY, 10, 100),
            _hist_order(master, "IST_S1", d, timedelta(hours=23, minutes=59), OrderSide.SELL, 10, 101),   # +10 on d
            _hist_order(master, "IST_B2", d, timedelta(hours=24, minutes=1), OrderSide.BUY, 10, 100),
            _hist_order(master, "IST_S2", d, timedelta(hours=24, minutes=2), OrderSide.SELL, 10, 103),    # +30 on d+1
        ])
        await session.commit()

    resp = await api_client.get("/api/v1/dashboard/realized-timeline?range=7d", headers=AUTH_HEADER)
    strat = list(resp.json()["strategies"].values())[0]
    by = {x["date"]: x["daily_pnl"] for x in strat["daily"]}
    assert by[d.isoformat()] == "10.00"
    assert by[(d + timedelta(days=1)).isoformat()] == "30.00"


@pytest.mark.asyncio
async def test_realized_timeline_daily_does_not_double_count_master_listed_as_member(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _master_and_child(session, None, None, None, None)
        await _strategy(session, "master", master, [(master, 1.0, True), (child, 1.0, True)])
    _install(clients, live_master_id=str(master.id))

    yesterday = trading_day() - timedelta(days=1)
    async with TestingSessionLocal() as session:
        session.add_all(_round_trip(master, "DC", yesterday, 100, 105))
        await session.commit()

    resp = await api_client.get("/api/v1/dashboard/realized-timeline?range=yesterday", headers=AUTH_HEADER)
    strat = list(resp.json()["strategies"].values())[0]
    assert [(x["date"], x["daily_pnl"]) for x in strat["daily"]] == [(yesterday.isoformat(), "50.00")]


# -- Active Strategies: a FOLLOWER-typed strategy Master is a live master ------

async def _follower_typed_master(session, *, trading_enabled=True, session_valid=True):
    """The real production shape: both accounts were created as FOLLOWER; the
    strategy's Master is whichever account the strategy points at."""
    master = await _account(session, AccountType.FOLLOWER, "Dhinesh", "AB_FT_MASTER", session_valid=session_valid)
    child = await _account(session, AccountType.FOLLOWER, "Vikash", "AB_FT_CHILD")
    strat = await _strategy(session, "Master", master, [(child, 1.0, True)])
    strat.trading_enabled = trading_enabled
    await session.commit()
    clients = {str(master.id): FakeBrokerClient(None, None), str(child.id): FakeBrokerClient(None, None)}
    return master, child, clients


@pytest.mark.asyncio
async def test_enabled_strategy_with_follower_typed_master_counts_as_active(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _follower_typed_master(session)
    # no account_type MASTER account -> the engine's legacy master id is None
    _install(clients, live_master_id=None, live_client_ids={str(master.id), str(child.id)})

    body = (await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)).json()
    assert body["platform"]["strategies"] == {"total": 1, "active": 1}
    row = body["strategies"]["list"][0]
    assert row["status"] == "RUNNING" and row["status_reason"] is None


@pytest.mark.asyncio
async def test_strategy_master_without_a_live_client_is_not_active(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _follower_typed_master(session)
    _install(clients, live_master_id=None, live_client_ids={str(child.id)})   # master has no broker client

    body = (await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)).json()
    assert body["platform"]["strategies"]["active"] == 0
    assert body["strategies"]["list"][0]["status"] == "NOT_RUNNING"


@pytest.mark.asyncio
async def test_strategy_master_with_an_invalid_session_is_not_active(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _follower_typed_master(session, session_valid=False)
    _install(clients, live_master_id=None, live_client_ids={str(master.id), str(child.id)})

    body = (await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)).json()
    assert body["platform"]["strategies"]["active"] == 0


@pytest.mark.asyncio
async def test_disabled_strategy_is_not_active_even_with_a_live_master(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _follower_typed_master(session, trading_enabled=False)
    _install(clients, live_master_id=None, live_client_ids={str(master.id), str(child.id)})

    body = (await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)).json()
    assert body["platform"]["strategies"]["active"] == 0
    assert body["strategies"]["list"][0]["status"] == "DISABLED"


@pytest.mark.asyncio
async def test_a_live_client_that_is_only_a_child_never_makes_a_strategy_active(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _follower_typed_master(session)
    _install(clients, live_master_id=None, live_client_ids={str(child.id)})   # Vikash is live, but only a Child

    body = (await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)).json()
    assert body["platform"]["strategies"]["active"] == 0


# -- status.master / children / execution pairing follow STRATEGY roles -------

async def _summary(api_client):
    resp = await api_client.get("/api/v1/dashboard/summary", headers=AUTH_HEADER)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_status_master_and_children_come_from_the_strategy_not_account_type(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _follower_typed_master(session)
        await _account(session, AccountType.FOLLOWER, "Unassigned", "AB_LOOSE")
        await session.commit()
    _install(clients, live_master_id=None, live_client_ids={str(master.id), str(child.id)})

    body = await _summary(api_client)
    assert body["status"]["master"]["display_name"] == "Dhinesh"
    assert body["status"]["master"]["is_live"] is True
    # Only Vikash is a Child: the FOLLOWER-typed Master and the unassigned account are not.
    assert body["status"]["children"] == {"total": 1, "connected": 1}


@pytest.mark.asyncio
async def test_a_master_listed_as_its_own_member_is_never_counted_as_a_child(api_client, db_session):
    async with TestingSessionLocal() as session:
        master = await _account(session, AccountType.FOLLOWER, "Dhinesh", "AB_SELF_M")
        child = await _account(session, AccountType.FOLLOWER, "Vikash", "AB_SELF_C")
        await _strategy(session, "Master", master, [(child, 1.0, True), (master, 1.0, True)])
        await session.commit()
    _install({}, live_master_id=None, live_client_ids=set())

    body = await _summary(api_client)
    assert body["status"]["children"]["total"] == 1


@pytest.mark.asyncio
async def test_multiple_strategies_count_unique_children_and_show_the_running_master(api_client, db_session):
    async with TestingSessionLocal() as session:
        m1 = await _account(session, AccountType.FOLLOWER, "MasterOne", "AB_MS_M1")
        m2 = await _account(session, AccountType.FOLLOWER, "MasterTwo", "AB_MS_M2")
        c1 = await _account(session, AccountType.FOLLOWER, "ChildOne", "AB_MS_C1")
        c2 = await _account(session, AccountType.FOLLOWER, "ChildTwo", "AB_MS_C2", session_valid=False)
        c3 = await _account(session, AccountType.FOLLOWER, "ChildThree", "AB_MS_C3")
        s1 = await _strategy(session, "One", m1, [(c1, 1.0, True)])
        s2 = await _strategy(session, "Two", m2, [(c2, 1.0, True), (c3, 1.0, True)])
        s1.trading_enabled = False            # only strategy Two can be running
        await session.commit()
    live = {str(m1.id), str(m2.id), str(c1.id), str(c2.id), str(c3.id)}
    _install({}, live_master_id=None, live_client_ids=live)

    body = await _summary(api_client)
    assert body["platform"]["strategies"] == {"total": 2, "active": 1}
    assert body["status"]["master"]["display_name"] == "MasterTwo"           # the running strategy's Master
    assert body["status"]["children"] == {"total": 3, "connected": 2}      # ChildTwo has no valid session


@pytest.mark.asyncio
async def test_inactive_strategy_master_is_shown_honestly_and_not_live(api_client, db_session):
    async with TestingSessionLocal() as session:
        master = await _account(session, AccountType.FOLLOWER, "Dhinesh", "AB_IN_M", is_active=False, session_valid=False)
        child = await _account(session, AccountType.FOLLOWER, "Vikash", "AB_IN_C")
        await _strategy(session, "Master", master, [(child, 1.0, True)])
        await session.commit()
    _install({}, live_master_id=None, live_client_ids={str(master.id), str(child.id)})

    body = await _summary(api_client)
    assert body["platform"]["strategies"]["active"] == 0
    master_card = body["status"]["master"]
    assert master_card["display_name"] == "Dhinesh"
    assert master_card["session_valid"] is False and master_card["is_live"] is False
    assert body["status"]["children"] == {"total": 1, "connected": 1}


@pytest.mark.asyncio
async def test_execution_impact_pairs_with_a_follower_typed_strategy_master(api_client, db_session):
    async with TestingSessionLocal() as session:
        master, child, clients = await _follower_typed_master(session)
        await _copy_pair(session, master, child, OrderSide.BUY, master_price="100.00", child_price="100.75", qty=1)
        await session.commit()
    _install(clients, live_master_id=None, live_client_ids={str(master.id), str(child.id)})

    edge = (await _summary(api_client))["execution_edge"]
    assert edge["total"] == "-0.75"
    assert edge["total_quantity"] == 1
    assert edge["direction"] == "children_worse"


@pytest.mark.asyncio
async def test_execution_impact_ignores_a_master_order_from_a_non_master_account(api_client, db_session):
    """The pairing is still scoped to master accounts: an order with the same
    broker id on a plain Child (not a strategy Master, not MASTER-typed) is
    never bound as 'the master fill'."""
    async with TestingSessionLocal() as session:
        master, child, clients = await _follower_typed_master(session)
        other = await _account(session, AccountType.FOLLOWER, "Other", "AB_NOT_MASTER")
        await _copy_pair(session, other, child, OrderSide.BUY, master_price="100.00", child_price="100.75", qty=1)
        await session.commit()
    _install(clients, live_master_id=None, live_client_ids={str(master.id), str(child.id)})

    edge = (await _summary(api_client))["execution_edge"]
    assert edge["total_quantity"] == 0
