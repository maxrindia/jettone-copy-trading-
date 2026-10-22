"""
R2C Batch B: an AMBIGUOUS broker outcome (TIMEOUT / UNKNOWN) on a manual master order or a manual square-off.

Before: any non-SUCCESS result was stored as a definite REJECTED order and reported as a failure, so the user
retried - a second real master order (and, for a LIMIT, a second replication to every Child) or a second opposite
MARKET order that reverses the position.

Now: the order is stored UNRESOLVED (Order PENDING, no broker id, broker_status "TIMEOUT" - the shape the copy
pipeline already uses for an ambiguous child send), a CRITICAL system event carries what is needed to verify it at the
broker, the response says the outcome is unknown and not to retry, and a new manual order / square-off for the same
account + symbol is refused (HTTP 409) while the unresolved one is outstanding. Definite rejections keep their old
behaviour and never block.

The StaleOrderSweeper does not resolve these rows (it walks CopyTrade rows only), so the block NEVER expires on its
own: it is cleared only by the explicit, verified resolve workflow (tests below). 60 minutes is only an escalation
point. Two simultaneous requests for the same account + symbol cannot both reach the broker (send_claim_lock).

Everything here is mocked: no broker, no network, in-memory database, live trading never enabled.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.broker import ManualOrderRequest, place_manual_master_order
from app.api.strategies import SquareOffRequest, square_off_child_position
from app.models.accounts import Account, AccountType
from app.models.audit import SystemEvent
from app.models.trades import Order, OrderSide as DBOrderSide, OrderSource, OrderStatus
from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.exceptions import AuthenticationError, SessionExpiredError
from app.services.alice_blue.types import (
    OrderRecord,
    OrderResult,
    OrderResultCategory,
    OrderSide,
    OrderType,
)
from app.services.alice_blue.types import OrderSide as BrokerOrderSide
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.reliability.master_order_poller import MasterOrderPoller
from app.services.reliability.stale_order_sweeper import StaleOrderSweeper
from app.services.reliability import unresolved_sends
from app.services.reliability.unresolved_sends import (
    UNRESOLVED_ESCALATION_MINUTES,
    ResolutionError,
    find_unresolved_send,
    list_unresolved_sends,
    resolve_unresolved_send,
)
from conftest import TestingSessionLocal
from test_manual_master_order import _order_result, fake_master_client_patch  # noqa: F401
from test_marketable_limit_strategy import _limit_event
from test_square_off_persistence import _broker_rejected, _broker_success, _setup_follower_strategy

AMBIGUOUS = [OrderResultCategory.TIMEOUT, OrderResultCategory.UNKNOWN]


def _manual_payload(symbol="reliance-eq", exchange="nse", order_type=OrderType.LIMIT):
    return ManualOrderRequest(
        strategy_id=uuid.uuid4(), symbol=symbol, exchange=exchange, side=OrderSide.BUY, quantity=10,
        product_type="MIS", order_type=order_type,
        price=Decimal("2500.00") if order_type == OrderType.LIMIT else None,
    )


async def _manual(payload, db_session, mock_redis):
    with patch("app.api.broker.is_master_trading_enabled", AsyncMock(return_value=True)):
        return await place_manual_master_order(payload=payload, db=db_session, redis=mock_redis)


@pytest.fixture
def live(monkeypatch):
    monkeypatch.setattr("app.api.broker.settings.ENGINE_LIVE_TRADING_ENABLED", True)


async def _seed_order(db_session, account_id, symbol="RELIANCE", exchange="NSE", status=OrderStatus.PENDING,
                      broker_order_id=None, broker_status="TIMEOUT", age_minutes=0, master_broker_order_id=None,
                      source=OrderSource.INTERNAL_UI):
    order = Order(
        id=uuid.uuid4(), internal_order_id=f"SEED_{uuid.uuid4().hex[:10]}", broker_order_id=broker_order_id,
        account_id=account_id, source=source, exchange=exchange, symbol=symbol, side=DBOrderSide.BUY,
        order_type="MARKET", product_type="MIS", quantity=10, filled_quantity=0, pending_quantity=10,
        price=Decimal("0"), status=status, broker_status=broker_status, master_broker_order_id=master_broker_order_id,
    )
    db_session.add(order)
    await db_session.commit()
    if age_minutes:
        order.created_at = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
        await db_session.commit()
    return order


async def _events(db_session, event_type):
    return (await db_session.execute(select(SystemEvent).where(SystemEvent.event_type == event_type))).scalars().all()


# ══ manual master order ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("category", AMBIGUOUS)
@pytest.mark.parametrize("order_type", [OrderType.LIMIT, OrderType.MARKET])
async def test_manual_ambiguous_send_is_stored_unresolved_not_rejected(
        mock_redis, db_session, fake_master_client_patch, live, category, order_type):
    master, broker_client = fake_master_client_patch
    broker_client.place_order = AsyncMock(return_value=_order_result(category, message="no answer"))

    with patch.object(CopyEnginePipeline, "execute_replication", new=AsyncMock()) as replicate:
        response = await _manual(_manual_payload(order_type=order_type), db_session, mock_redis)

    broker_client.place_order.assert_awaited_once()
    replicate.assert_not_awaited()                                  # nothing is copied from an order of unknown fate
    assert response["placed"] is False and response["outcome_unknown"] is True
    assert response["category"] == category.value
    assert "Do NOT retry" in response["message"] and response["client_order_id"] in response["message"]
    assert response["master_order_id"] is None and response["replication"] is None

    (order,) = (await db_session.execute(select(Order).where(Order.internal_order_id.like("JT_MANUAL_%")))).scalars().all()
    assert order.status == OrderStatus.PENDING and order.broker_status == "TIMEOUT"
    assert order.broker_order_id is None and order.rejection_reason is None
    assert order.pending_quantity == 10 and order.filled_quantity == 0
    assert order.internal_order_id == response["client_order_id"] and order.source == OrderSource.INTERNAL_UI

    (event,) = await _events(db_session, "MANUAL_ORDER_UNRESOLVED_SEND")
    assert event.severity.value == "CRITICAL" and event.account_id == master.id
    meta = event.metadata_payload
    assert meta["client_order_id"] == order.internal_order_id and meta["symbol"] == "RELIANCE-EQ"
    assert meta["quantity"] == 10 and meta["category"] == category.value and meta["side"] == "BUY"


@pytest.mark.asyncio
@pytest.mark.parametrize("category", AMBIGUOUS)
async def test_manual_retry_is_blocked_and_never_reaches_the_broker(
        mock_redis, db_session, fake_master_client_patch, live, category):
    master, broker_client = fake_master_client_patch
    broker_client.place_order = AsyncMock(return_value=_order_result(category))
    await _manual(_manual_payload(), db_session, mock_redis)

    with pytest.raises(HTTPException) as blocked:
        await _manual(_manual_payload(), db_session, mock_redis)
    assert blocked.value.status_code == 409 and "unknown broker outcome" in blocked.value.detail

    assert broker_client.place_order.await_count == 1                # the retry never reached the broker
    rows = (await db_session.execute(select(Order).where(Order.internal_order_id.like("JT_MANUAL_%")))).scalars().all()
    assert len(rows) == 1                                            # and wrote nothing


@pytest.mark.asyncio
async def test_manual_block_matches_the_symbol_with_or_without_the_eq_suffix(
        mock_redis, db_session, fake_master_client_patch, live):
    master, broker_client = fake_master_client_patch
    broker_client.place_order = AsyncMock(return_value=_order_result(OrderResultCategory.TIMEOUT))
    await _manual(_manual_payload(symbol="reliance"), db_session, mock_redis)
    with pytest.raises(HTTPException) as blocked:
        await _manual(_manual_payload(symbol="RELIANCE-EQ"), db_session, mock_redis)
    assert blocked.value.status_code == 409


@pytest.mark.asyncio
async def test_manual_block_does_not_touch_other_symbols_accounts_or_exchanges(
        mock_redis, db_session, fake_master_client_patch, live):
    master, broker_client = fake_master_client_patch
    other_account = uuid.uuid4()
    db_session.add(Account(id=other_account, account_type=AccountType.FOLLOWER, display_name="Other",
                           alice_user_id="AB_OTHER", is_active=True))
    await db_session.commit()
    await _seed_order(db_session, master.id, symbol="RELIANCE", exchange="NSE")        # unresolved on the master
    await _seed_order(db_session, other_account, symbol="TCS", exchange="NSE")          # unrelated account

    broker_client.place_order = AsyncMock(side_effect=[
        _order_result(OrderResultCategory.SUCCESS, "BRK_1"), _order_result(OrderResultCategory.SUCCESS, "BRK_2")])
    with patch.object(CopyEnginePipeline, "execute_replication", new=AsyncMock(return_value={})):
        ok_symbol = await _manual(_manual_payload(symbol="INFY"), db_session, mock_redis)         # other symbol
        ok_exchange = await _manual(_manual_payload(symbol="RELIANCE", exchange="BSE"), db_session, mock_redis)
    assert ok_symbol["placed"] is True and ok_exchange["placed"] is True
    assert broker_client.place_order.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", [
    dict(status=OrderStatus.REJECTED, broker_status="REJECTED"),                         # definite rejection
    dict(status=OrderStatus.REJECTED, broker_status=None),                               # status alone excludes it
    dict(status=OrderStatus.FAILED, broker_status=None),
    dict(status=OrderStatus.OPEN, broker_order_id="BRK_X", broker_status="OPEN"),        # reached the broker
    dict(status=OrderStatus.PENDING, broker_order_id="BRK_SL", broker_status="SUCCESS"),  # native SL awaiting trigger
], ids=["rejected", "rejected-no-broker-status", "failed", "has-broker-id", "pending-sl-with-id"])
async def test_manual_block_ignores_orders_that_are_not_outstanding_unresolved_sends(
        mock_redis, db_session, fake_master_client_patch, live, seed):
    master, broker_client = fake_master_client_patch
    await _seed_order(db_session, master.id, **seed)
    broker_client.place_order = AsyncMock(return_value=_order_result(OrderResultCategory.SUCCESS, "BRK_NEW"))
    with patch.object(CopyEnginePipeline, "execute_replication", new=AsyncMock(return_value={})):
        response = await _manual(_manual_payload(symbol="RELIANCE"), db_session, mock_redis)
    assert response["placed"] is True
    broker_client.place_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_definite_failures_stay_rejected_and_do_not_block(
        mock_redis, db_session, fake_master_client_patch, live):
    master, broker_client = fake_master_client_patch
    for category in (OrderResultCategory.REJECTED, OrderResultCategory.NETWORK_ERROR):
        broker_client.place_order = AsyncMock(return_value=_order_result(category, message="definite"))
        response = await _manual(_manual_payload(), db_session, mock_redis)
        assert response["placed"] is False and "outcome_unknown" not in response
    rows = (await db_session.execute(select(Order).where(Order.internal_order_id.like("JT_MANUAL_%")))).scalars().all()
    assert len(rows) == 2 and all(r.status == OrderStatus.REJECTED and r.broker_status == "REJECTED" for r in rows)
    assert await _events(db_session, "MANUAL_ORDER_UNRESOLVED_SEND") == []


@pytest.mark.asyncio
async def test_manual_response_is_unchanged_when_the_alert_cannot_be_sent(
        mock_redis, db_session, fake_master_client_patch, live):
    master, broker_client = fake_master_client_patch
    broker_client.place_order = AsyncMock(return_value=_order_result(OrderResultCategory.UNKNOWN))
    with patch("app.services.reliability.unresolved_sends.emit_system_event", AsyncMock(side_effect=RuntimeError("alert down"))):
        response = await _manual(_manual_payload(), db_session, mock_redis)
    assert response["outcome_unknown"] is True
    (order,) = (await db_session.execute(select(Order).where(Order.internal_order_id.like("JT_MANUAL_%")))).scalars().all()
    assert order.status == OrderStatus.PENDING and order.broker_status == "TIMEOUT"


# ══ manual square-off ═════════════════════════════════════════════════════════════════════

def _ambiguous_broker(category):
    async def _place_order(self, req):
        return OrderResult(category=category, client_order_id=req.client_order_id, broker_order_id=None,
                           message="no answer", raw_response={}, timestamp=datetime.now(timezone.utc))
    return _place_order


def _square_off(symbol="INFY", exchange="NSE", quantity=20, side="BUY"):
    return SquareOffRequest(symbol=symbol, exchange=exchange, quantity=quantity, side=side, product_type="MIS")


async def _do_square_off(strategy, follower, payload, db_session, mock_redis):
    return await square_off_child_position(
        strategy_id=strategy.id, account_id=follower.id, payload=payload, db=db_session, redis=mock_redis)


async def _square_off_orders(db_session, follower):
    return (await db_session.execute(
        select(Order).where(Order.account_id == follower.id, Order.source == OrderSource.SYSTEM))).scalars().all()


@pytest.mark.asyncio
@pytest.mark.parametrize("category", AMBIGUOUS)
async def test_square_off_ambiguous_send_is_stored_unresolved_not_rejected(db_session, mock_redis, monkeypatch, category):
    master, follower, strategy, member = await _setup_follower_strategy(db_session, alice_user_id=f"AB_R2C_{category.value}")
    place = AsyncMock(side_effect=_ambiguous_broker(category))
    monkeypatch.setattr(AliceBlueClient, "place_order", lambda self, req: place(self, req))

    with pytest.raises(HTTPException) as exc:
        await _do_square_off(strategy, follower, _square_off(), db_session, mock_redis)
    assert exc.value.status_code == 502
    assert "Do NOT retry" in exc.value.detail and "outcome UNKNOWN" in exc.value.detail
    assert place.await_count == 1

    (order,) = await _square_off_orders(db_session, follower)
    assert order.status == OrderStatus.PENDING and order.broker_status == "TIMEOUT"
    assert order.broker_order_id is None and order.rejection_reason is None
    assert order.side == DBOrderSide.SELL and order.quantity == 20 and order.pending_quantity == 20
    assert order.filled_quantity == 0 and order.master_broker_order_id is None
    assert order.internal_order_id in exc.value.detail

    (event,) = await _events(db_session, "SQUARE_OFF_UNRESOLVED_SEND")
    assert event.severity.value == "CRITICAL" and event.account_id == follower.id
    meta = event.metadata_payload
    assert meta["client_order_id"] == order.internal_order_id and meta["symbol"] == "INFY"
    assert meta["quantity"] == 20 and meta["side"] == "SELL" and meta["strategy_id"] == str(strategy.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("category", AMBIGUOUS)
async def test_square_off_retry_is_blocked_and_never_reaches_the_broker(db_session, mock_redis, monkeypatch, category):
    master, follower, strategy, member = await _setup_follower_strategy(db_session, alice_user_id=f"AB_R2C_RT_{category.value}")
    place = AsyncMock(side_effect=_ambiguous_broker(category))
    monkeypatch.setattr(AliceBlueClient, "place_order", lambda self, req: place(self, req))
    with pytest.raises(HTTPException):
        await _do_square_off(strategy, follower, _square_off(), db_session, mock_redis)

    with pytest.raises(HTTPException) as blocked:
        await _do_square_off(strategy, follower, _square_off(symbol="INFY-EQ"), db_session, mock_redis)
    assert blocked.value.status_code == 409 and "unknown broker outcome" in blocked.value.detail
    assert place.await_count == 1
    assert len(await _square_off_orders(db_session, follower)) == 1


@pytest.mark.asyncio
async def test_square_off_block_is_limited_to_the_same_account_and_symbol(db_session, mock_redis, monkeypatch):
    master, follower, strategy, member = await _setup_follower_strategy(db_session, alice_user_id="AB_R2C_SCOPE")
    await _seed_order(db_session, follower.id, symbol="INFY", source=OrderSource.SYSTEM)
    monkeypatch.setattr(AliceBlueClient, "place_order", _broker_success("BRK_OTHER"))
    resp = await _do_square_off(strategy, follower, _square_off(symbol="TCS"), db_session, mock_redis)   # other symbol
    assert resp["status"] == "success"
    with pytest.raises(HTTPException) as blocked:                                                          # same symbol
        await _do_square_off(strategy, follower, _square_off(symbol="INFY"), db_session, mock_redis)
    assert blocked.value.status_code == 409


@pytest.mark.asyncio
async def test_square_off_definite_rejection_stays_rejected_and_does_not_block(db_session, mock_redis, monkeypatch):
    master, follower, strategy, member = await _setup_follower_strategy(db_session, alice_user_id="AB_R2C_DEF")
    monkeypatch.setattr(AliceBlueClient, "place_order", _broker_rejected())
    with pytest.raises(HTTPException) as exc:
        await _do_square_off(strategy, follower, _square_off(), db_session, mock_redis)
    assert exc.value.status_code == 400
    (order,) = await _square_off_orders(db_session, follower)
    assert order.status == OrderStatus.REJECTED and order.broker_status == "REJECTED"
    assert await _events(db_session, "SQUARE_OFF_UNRESOLVED_SEND") == []

    monkeypatch.setattr(AliceBlueClient, "place_order", _broker_success("BRK_AFTER_REJECT"))     # a retry is allowed
    resp = await _do_square_off(strategy, follower, _square_off(), db_session, mock_redis)
    assert resp["status"] == "success"


# ══ the guard itself, and the sweeper gap ═════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_guard_treats_a_send_still_in_flight_as_outstanding(db_session, sample_accounts):
    """A pre-written PENDING order with no broker id and no broker_status is a send in flight (or one whose result
    could not be recorded): it blocks too, which also stops a double-click."""
    account = sample_accounts["master"]
    fresh = await _seed_order(db_session, account.id, broker_status=None)
    assert (await find_unresolved_send(db_session, account.id, "RELIANCE", "NSE")).id == fresh.id


@pytest.mark.asyncio
async def test_the_stale_order_sweeper_does_not_resolve_unresolved_manual_orders(db_session, sample_accounts):
    """BLOCKER, pinned on purpose: the sweeper only walks CopyTrade rows and a manual order has none, so nothing
    clears an unresolved manual order or square-off automatically (and Alice Blue returns no tag to match anyway).
    The user must verify at the broker; the block expires on its own after UNRESOLVED_SEND_BLOCK_MINUTES."""
    account = sample_accounts["master"]
    order = await _seed_order(db_session, account.id, age_minutes=30)
    book = AsyncMock(return_value=[])
    sweeper = StaleOrderSweeper(order_book_provider=book)
    await sweeper.sweep_once(db_session)
    await db_session.refresh(order)
    assert order.status == OrderStatus.PENDING and order.broker_status == "TIMEOUT" and order.broker_order_id is None
    book.assert_not_awaited()


# ══ the block never expires on its own; only an explicit, verified resolution clears it ══════

OLD = 5 * 24 * 60            # five days, in minutes


async def _unresolved_state(db_session, order):
    await db_session.refresh(order)
    return order.status, order.broker_status, order.broker_order_id, order.rejection_reason


@pytest.mark.asyncio
async def test_an_old_unresolved_manual_order_still_blocks_and_is_escalated_once(
        mock_redis, db_session, fake_master_client_patch, live):
    master, broker_client = fake_master_client_patch
    old = await _seed_order(db_session, master.id, age_minutes=OLD)
    before = await _unresolved_state(db_session, old)

    for _ in range(2):                                                       # two blocked attempts
        with pytest.raises(HTTPException) as blocked:
            await _manual(_manual_payload(symbol="RELIANCE"), db_session, mock_redis)
        assert blocked.value.status_code == 409
        assert "escalated" in blocked.value.detail and "unresolved-orders/resolve" in blocked.value.detail
        assert "does not clear by itself" in blocked.value.detail
    broker_client.place_order.assert_not_awaited()
    assert await _unresolved_state(db_session, old) == before                # age alone changed nothing
    assert before[0] == OrderStatus.PENDING

    (event,) = await _events(db_session, "UNRESOLVED_SEND_ESCALATED")        # once, not once per attempt
    assert event.severity.value == "CRITICAL" and event.metadata_payload["client_order_id"] == old.internal_order_id


@pytest.mark.asyncio
async def test_a_recent_unresolved_order_is_not_escalated_yet(mock_redis, db_session, fake_master_client_patch, live):
    master, broker_client = fake_master_client_patch
    await _seed_order(db_session, master.id, age_minutes=UNRESOLVED_ESCALATION_MINUTES - 30)
    with pytest.raises(HTTPException) as blocked:
        await _manual(_manual_payload(symbol="RELIANCE"), db_session, mock_redis)
    assert "escalated" not in blocked.value.detail
    assert await _events(db_session, "UNRESOLVED_SEND_ESCALATED") == []


@pytest.mark.asyncio
async def test_an_old_unresolved_order_still_blocks_a_square_off(db_session, mock_redis, monkeypatch):
    master, follower, strategy, member = await _setup_follower_strategy(db_session, alice_user_id="AB_R2C_OLD")
    old = await _seed_order(db_session, follower.id, symbol="INFY", source=OrderSource.SYSTEM, age_minutes=OLD)
    place = AsyncMock()
    monkeypatch.setattr(AliceBlueClient, "place_order", place)
    with pytest.raises(HTTPException) as blocked:
        await _do_square_off(strategy, follower, _square_off(symbol="INFY"), db_session, mock_redis)
    assert blocked.value.status_code == 409 and "escalated" in blocked.value.detail
    place.assert_not_awaited()
    assert (await _unresolved_state(db_session, old))[0] == OrderStatus.PENDING


# ── explicit resolution ───────────────────────────────────────────────────────────────────

async def _resolve(db_session, order, **kwargs):
    args = dict(outcome="NOT_PLACED", note="checked order book and positions: absent", verified_at_broker=True,
                broker_order_id=None)
    args.update(kwargs)
    return await resolve_unresolved_send(db_session, order.internal_order_id, **args)


@pytest.mark.asyncio
async def test_resolving_as_not_placed_rejects_the_order_audits_it_and_unblocks(
        mock_redis, db_session, fake_master_client_patch, live):
    master, broker_client = fake_master_client_patch
    old = await _seed_order(db_session, master.id, age_minutes=OLD)
    resolved = await _resolve(db_session, old)
    assert resolved.status == OrderStatus.REJECTED and resolved.broker_status == "REJECTED"
    assert resolved.broker_order_id is None and resolved.pending_quantity == 0
    assert "Operator verified not placed at broker" in resolved.rejection_reason and "absent" in resolved.rejection_reason

    (event,) = await _events(db_session, "UNRESOLVED_SEND_RESOLVED")
    assert event.severity.value == "WARNING" and event.metadata_payload["outcome"] == "NOT_PLACED"
    assert event.metadata_payload["previous"]["broker_status"] == "TIMEOUT"

    broker_client.place_order = AsyncMock(return_value=_order_result(OrderResultCategory.SUCCESS, "BRK_AFTER"))
    with patch.object(CopyEnginePipeline, "execute_replication", new=AsyncMock(return_value={})):
        response = await _manual(_manual_payload(symbol="RELIANCE"), db_session, mock_redis)
    assert response["placed"] is True                                         # the retry is now allowed


@pytest.mark.asyncio
async def test_resolving_as_placed_records_the_broker_order_and_unblocks(db_session, sample_accounts):
    account = sample_accounts["master"]
    old = await _seed_order(db_session, account.id, age_minutes=OLD)
    resolved = await _resolve(db_session, old, outcome="PLACED", broker_order_id="BRK_FOUND_1",
                              note="found in order book as OPEN")
    assert resolved.status == OrderStatus.OPEN and resolved.broker_order_id == "BRK_FOUND_1"
    assert resolved.broker_status == "OPEN" and resolved.pending_quantity == resolved.quantity
    assert await find_unresolved_send(db_session, account.id, "RELIANCE", "NSE") is None
    (event,) = await _events(db_session, "UNRESOLVED_SEND_RESOLVED")
    assert event.metadata_payload["outcome"] == "PLACED" and event.metadata_payload["broker_order_id"] == "BRK_FOUND_1"


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs,status", [
    (dict(verified_at_broker=False), 422),
    (dict(note="  "), 422),
    (dict(note="abc"), 422),
    (dict(outcome="MAYBE"), 422),
    (dict(outcome="PLACED"), 422),                                  # PLACED needs the broker's order id
    (dict(outcome="NOT_PLACED", broker_order_id="BRK_1"), 422),     # NOT_PLACED must not carry one
], ids=["not-verified", "blank-note", "short-note", "bad-outcome", "placed-without-id", "not-placed-with-id"])
async def test_resolution_requires_an_explicit_verified_request_and_changes_nothing_otherwise(
        db_session, sample_accounts, kwargs, status):
    old = await _seed_order(db_session, sample_accounts["master"].id)
    before = await _unresolved_state(db_session, old)
    with pytest.raises(ResolutionError) as err:
        await _resolve(db_session, old, **kwargs)
    assert err.value.status_code == status
    assert await _unresolved_state(db_session, old) == before
    assert await _events(db_session, "UNRESOLVED_SEND_RESOLVED") == []


@pytest.mark.asyncio
async def test_resolution_refuses_anything_that_is_not_an_outstanding_unresolved_send(db_session, sample_accounts):
    account = sample_accounts["master"]
    with pytest.raises(ResolutionError) as missing:
        await resolve_unresolved_send(db_session, "NO_SUCH_ORDER", "NOT_PLACED", "checked the broker", True)
    assert missing.value.status_code == 404

    for seed in (dict(status=OrderStatus.OPEN, broker_order_id="BRK_REAL", broker_status="OPEN"),
                 dict(status=OrderStatus.REJECTED, broker_status="REJECTED"),
                 dict(status=OrderStatus.PENDING, broker_order_id="BRK_SL", broker_status="SUCCESS")):
        order = await _seed_order(db_session, account.id, **seed)
        before = await _unresolved_state(db_session, order)
        with pytest.raises(ResolutionError) as err:
            await _resolve(db_session, order)
        assert err.value.status_code == 409 and "not an unresolved send" in str(err.value)
        assert await _unresolved_state(db_session, order) == before          # a real or finished order is never rewritten

    once = await _seed_order(db_session, account.id)
    await _resolve(db_session, once)
    with pytest.raises(ResolutionError) as twice:                            # resolving twice is refused
        await _resolve(db_session, once, outcome="PLACED", broker_order_id="BRK_LATE", note="changed my mind")
    assert twice.value.status_code == 409
    assert (await _unresolved_state(db_session, once))[0] == OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_resolving_as_placed_with_a_broker_id_already_recorded_is_refused(db_session, sample_accounts):
    account = sample_accounts["master"]
    await _seed_order(db_session, account.id, symbol="TCS", status=OrderStatus.OPEN, broker_order_id="BRK_DUP",
                      broker_status="OPEN")
    unresolved = await _seed_order(db_session, account.id)
    before = await _unresolved_state(db_session, unresolved)
    with pytest.raises(ResolutionError) as err:
        await _resolve(db_session, unresolved, outcome="PLACED", broker_order_id="BRK_DUP", note="found it")
    assert err.value.status_code == 409
    assert await _unresolved_state(db_session, unresolved) == before


@pytest.mark.asyncio
async def test_the_list_shows_only_outstanding_unresolved_sends(db_session, sample_accounts):
    account = sample_accounts["master"]
    keep = await _seed_order(db_session, account.id, age_minutes=OLD)
    await _seed_order(db_session, account.id, symbol="TCS", status=OrderStatus.REJECTED, broker_status="REJECTED")
    await _seed_order(db_session, account.id, symbol="INFY", status=OrderStatus.OPEN, broker_order_id="B1", broker_status="OPEN")
    assert [o.id for o in await list_unresolved_sends(db_session)] == [keep.id]


@pytest.mark.asyncio
async def test_the_http_handlers_resolve_and_map_errors(db_session, sample_accounts):
    from app.api.broker import ResolveUnresolvedRequest, list_unresolved_orders, resolve_unresolved_order

    old = await _seed_order(db_session, sample_accounts["master"].id)
    listing = await list_unresolved_orders(db=db_session)
    assert listing["count"] == 1 and listing["orders"][0]["client_order_id"] == old.internal_order_id

    body = dict(client_order_id=old.internal_order_id, outcome="NOT_PLACED", verified_at_broker=True,
                note="checked order book and positions")
    done = await resolve_unresolved_order(ResolveUnresolvedRequest(**body), db=db_session)
    assert done["resolved"] is True and done["order"]["status"] == "REJECTED"
    with pytest.raises(HTTPException) as again:
        await resolve_unresolved_order(ResolveUnresolvedRequest(**body), db=db_session)
    assert again.value.status_code == 409
    with pytest.raises(HTTPException) as unknown:
        await resolve_unresolved_order(ResolveUnresolvedRequest(**{**body, "client_order_id": "NOPE"}), db=db_session)
    assert unknown.value.status_code == 404


# ── what an exception from place_order proves: only the exact pre-send AuthenticationError is "not sent" ─
#
# AuthenticationError (exactly) is raised by get_session()/_get_headers() BEFORE the request is issued. A
# SessionExpiredError (its subclass) is raised after the POST went out; any other exception, or a cancellation, cannot
# be proven pre-send either. Only the first is recorded FAILED / "Not sent"; the rest leave the pre-written row
# unresolved so the retry guard keeps blocking until the operator verifies it at the broker.
#
# NOT covered here (Batch A, Alice Blue client): _is_auth_failure also matches a non-401 gateway response whose body
# mentions an expired session, and the copy pipeline's RetryPolicy re-sends after the resulting exception.

NO_SESSION = AuthenticationError("acc", "No active Alice Blue session found in Redis.")
UNCERTAIN = [
    pytest.param(SessionExpiredError("acc", "Session still rejected after forced re-auth"), id="post-send-session-expired"),
    pytest.param(RuntimeError("boom"), id="runtime-error"),
    pytest.param(ValueError("odd"), id="value-error"),
]


@pytest.mark.asyncio
async def test_manual_order_with_a_pre_send_auth_failure_is_recorded_not_sent_and_does_not_block(
        mock_redis, db_session, fake_master_client_patch, live):
    master, broker_client = fake_master_client_patch
    broker_client.place_order = AsyncMock(side_effect=NO_SESSION)
    with pytest.raises(AuthenticationError):
        await _manual(_manual_payload(), db_session, mock_redis)
    (order,) = (await db_session.execute(select(Order).where(Order.internal_order_id.like("JT_MANUAL_%")))).scalars().all()
    assert order.status == OrderStatus.FAILED and order.broker_status == "FAILED" and order.pending_quantity == 0
    assert order.rejection_reason == "Not sent: no active broker session"
    assert await find_unresolved_send(db_session, master.id, "RELIANCE-EQ", "NSE") is None

    broker_client.place_order = AsyncMock(return_value=_order_result(OrderResultCategory.SUCCESS, "BRK_OK"))
    with patch.object(CopyEnginePipeline, "execute_replication", new=AsyncMock(return_value={})):
        assert (await _manual(_manual_payload(), db_session, mock_redis))["placed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", UNCERTAIN)
async def test_manual_order_with_an_uncertain_exception_stays_unresolved_and_blocks(
        mock_redis, db_session, fake_master_client_patch, live, exc):
    master, broker_client = fake_master_client_patch
    broker_client.place_order = AsyncMock(side_effect=exc)
    with pytest.raises(type(exc)):
        await _manual(_manual_payload(), db_session, mock_redis)
    (order,) = (await db_session.execute(select(Order).where(Order.internal_order_id.like("JT_MANUAL_%")))).scalars().all()
    assert order.status == OrderStatus.PENDING and order.broker_status is None and order.broker_order_id is None
    assert order.rejection_reason is None                                    # not labelled "not sent"
    assert (await find_unresolved_send(db_session, master.id, "RELIANCE-EQ", "NSE")).id == order.id

    with pytest.raises(HTTPException) as blocked:                             # the retry never reaches the broker
        await _manual(_manual_payload(), db_session, mock_redis)
    assert blocked.value.status_code == 409
    assert broker_client.place_order.await_count == 1


@pytest.mark.asyncio
async def test_manual_order_cancelled_in_flight_stays_unresolved_and_blocks(
        mock_redis, db_session, fake_master_client_patch, live):
    """A cancellation (BaseException, not Exception) mid-send is not caught: the request may have left, so the row
    stays unresolved and blocking, and the claim lock is still released."""
    master, broker_client = fake_master_client_patch
    broker_client.place_order = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await _manual(_manual_payload(), db_session, mock_redis)
    (order,) = (await db_session.execute(select(Order).where(Order.internal_order_id.like("JT_MANUAL_%")))).scalars().all()
    assert order.status == OrderStatus.PENDING and order.broker_status is None
    assert unresolved_sends._claim_locks == {}
    with pytest.raises(HTTPException) as blocked:
        await _manual(_manual_payload(), db_session, mock_redis)
    assert blocked.value.status_code == 409 and broker_client.place_order.await_count == 1


def _raising_broker(exc):
    async def _place_order(self, req):
        raise exc
    return _place_order


@pytest.mark.asyncio
async def test_square_off_with_a_pre_send_auth_failure_is_recorded_not_sent_and_does_not_block(db_session, mock_redis, monkeypatch):
    master, follower, strategy, member = await _setup_follower_strategy(db_session, alice_user_id="AB_R2C_NOSESS")
    monkeypatch.setattr(AliceBlueClient, "place_order", _raising_broker(NO_SESSION))
    with pytest.raises(AuthenticationError):
        await _do_square_off(strategy, follower, _square_off(), db_session, mock_redis)
    (order,) = await _square_off_orders(db_session, follower)
    assert order.status == OrderStatus.FAILED and order.broker_status == "FAILED" and order.pending_quantity == 0
    assert order.rejection_reason == "Not sent: no active broker session"

    monkeypatch.setattr(AliceBlueClient, "place_order", _broker_success("BRK_AFTER_NOSESS"))
    assert (await _do_square_off(strategy, follower, _square_off(), db_session, mock_redis))["status"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", UNCERTAIN)
async def test_square_off_with_an_uncertain_exception_stays_unresolved_and_blocks(db_session, mock_redis, monkeypatch, exc):
    master, follower, strategy, member = await _setup_follower_strategy(
        db_session, alice_user_id=f"AB_R2C_UNC_{type(exc).__name__}")
    monkeypatch.setattr(AliceBlueClient, "place_order", _raising_broker(exc))
    with pytest.raises(type(exc)):
        await _do_square_off(strategy, follower, _square_off(), db_session, mock_redis)
    (order,) = await _square_off_orders(db_session, follower)
    assert order.status == OrderStatus.PENDING and order.broker_status is None and order.rejection_reason is None

    place = AsyncMock()
    monkeypatch.setattr(AliceBlueClient, "place_order", place)
    with pytest.raises(HTTPException) as blocked:
        await _do_square_off(strategy, follower, _square_off(), db_session, mock_redis)
    assert blocked.value.status_code == 409
    place.assert_not_awaited()


@pytest.mark.asyncio
async def test_square_off_cancelled_in_flight_stays_unresolved_and_blocks(db_session, mock_redis, monkeypatch):
    master, follower, strategy, member = await _setup_follower_strategy(db_session, alice_user_id="AB_R2C_CANCEL")
    monkeypatch.setattr(AliceBlueClient, "place_order", _raising_broker(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await _do_square_off(strategy, follower, _square_off(), db_session, mock_redis)
    (order,) = await _square_off_orders(db_session, follower)
    assert order.status == OrderStatus.PENDING and order.broker_status is None
    assert unresolved_sends._claim_locks == {}
    place = AsyncMock()
    monkeypatch.setattr(AliceBlueClient, "place_order", place)
    with pytest.raises(HTTPException) as blocked:
        await _do_square_off(strategy, follower, _square_off(), db_session, mock_redis)
    assert blocked.value.status_code == 409
    place.assert_not_awaited()


# ══ copy-pipeline children are not handled by the manual guard / list / resolver ═════════════

async def _copy_child(db_session, account_id, symbol="INFY", master_order_id="M_COPY_1", broker_status="TIMEOUT"):
    return await _seed_order(db_session, account_id, symbol=symbol, source=OrderSource.SYSTEM,
                             master_broker_order_id=master_order_id, broker_status=broker_status)


@pytest.mark.asyncio
async def test_copy_pipeline_children_are_not_listed_blocking_or_resolvable_here(db_session, mock_redis, monkeypatch):
    master, follower, strategy, member = await _setup_follower_strategy(db_session, alice_user_id="AB_R2C_COPYX")
    child = await _copy_child(db_session, follower.id)
    manual = await _seed_order(db_session, follower.id, symbol="TCS", source=OrderSource.SYSTEM)       # a square-off row
    before = await _unresolved_state(db_session, child)

    assert [o.id for o in await list_unresolved_sends(db_session)] == [manual.id]                    # child not listed
    assert await find_unresolved_send(db_session, follower.id, "INFY", "NSE") is None                # child does not block

    monkeypatch.setattr(AliceBlueClient, "place_order", _broker_success("BRK_NOT_BLOCKED"))
    assert (await _do_square_off(strategy, follower, _square_off(symbol="INFY"), db_session, mock_redis))["status"] == "success"

    with pytest.raises(ResolutionError) as refused:
        await _resolve(db_session, child)
    assert refused.value.status_code == 409 and "copy-pipeline child" in str(refused.value)
    with pytest.raises(ResolutionError):
        await _resolve(db_session, child, outcome="PLACED", broker_order_id="BRK_X", note="found it")
    assert await _unresolved_state(db_session, child) == before                                       # untouched
    assert await _events(db_session, "UNRESOLVED_SEND_RESOLVED") == []
    assert (await find_unresolved_send(db_session, follower.id, "TCS", "NSE")).id == manual.id        # manual rows still block


@pytest.mark.asyncio
async def test_a_manual_master_order_and_a_square_off_stay_eligible_for_resolution(db_session, sample_accounts):
    account = sample_accounts["master"]
    master_order = await _seed_order(db_session, account.id, source=OrderSource.INTERNAL_UI)
    square_off = await _seed_order(db_session, account.id, symbol="TCS", source=OrderSource.SYSTEM)
    assert master_order.master_broker_order_id is None and square_off.master_broker_order_id is None
    assert (await _resolve(db_session, master_order)).status == OrderStatus.REJECTED
    assert (await _resolve(db_session, square_off)).status == OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_a_copy_child_leftover_with_no_broker_status_does_not_block_manual_orders_or_square_off(
        db_session, mock_redis, sample_accounts, sample_strategy, monkeypatch):
    """The copy pipeline pre-writes the child Order before its `if not client: return NO_CLIENT` check, which would leave a
    PENDING row with no broker id and no broker_status. Through execute_replication that branch is not reachable today
    (with no client the funds check fails first and nothing is written - asserted below), but a leftover of that shape
    must still never block a manual order or square-off."""
    follower = sample_accounts["follower1"]
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    result = await pipeline.execute_replication(
        event=_limit_event(quantity=10, price=Decimal("100"), master_order_id="N2_NOCLIENT"), clients={})
    assert result["results"][str(follower.id)] == {"status": "REJECTED", "reasons": ["FUNDS_FETCH_FAILED"]}
    async with TestingSessionLocal() as s:
        assert (await s.execute(select(Order).where(Order.master_broker_order_id == "N2_NOCLIENT"))).scalars().all() == []

    master, f2, strategy, member = await _setup_follower_strategy(db_session, alice_user_id="AB_R2C_LEFTOVER")
    leftover = await _seed_order(db_session, f2.id, symbol="RELIANCE", source=OrderSource.SYSTEM,
                                 master_broker_order_id="M_LEFTOVER", broker_status=None)
    assert leftover.status == OrderStatus.PENDING and leftover.broker_order_id is None and leftover.broker_status is None
    assert await find_unresolved_send(db_session, f2.id, "RELIANCE", "NSE") is None
    assert await list_unresolved_sends(db_session) == []
    monkeypatch.setattr(AliceBlueClient, "place_order", _broker_success("BRK_SQ_LEFTOVER"))
    assert (await _do_square_off(strategy, f2, _square_off(symbol="RELIANCE"), db_session, mock_redis))["status"] == "success"


# ══ N1: the poller recorded the real order first; PLACED links to it ══════════════════════════

def _book_record(broker_order_id, symbol="RELIANCE", side=BrokerOrderSide.BUY, quantity=10):
    return OrderRecord(
        broker_order_id=broker_order_id, client_order_id=None, symbol=symbol, exchange="NSE", side=side, quantity=quantity,
        filled_quantity=0, average_price=Decimal("0"), status="OPEN", order_timestamp=datetime.now(timezone.utc),
        order_type="LIMIT", product_type="MIS", price=Decimal("2500.00"), trigger_price=Decimal("0"), instrument_id=None)


class PollerHarness:
    """MasterOrderPoller on a mocked order book. The handler mimics the first step of EngineLoop.persist_and_process_event
    (it records the dispatched order as BROKER_EXTERNAL) and keeps the dispatched events; copying itself is not run."""

    def __init__(self, master, mock_redis):
        self.book, self.dispatched = [], []
        self.pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
        client = MagicMock()
        client.get_order_book = AsyncMock(side_effect=lambda: list(self.book))
        self.poller = MasterOrderPoller(master_account_id=str(master.id), client_provider=lambda _a: client,
                                        on_order_update=self._handler, pipeline=self.pipeline, check_interval_seconds=5)

    async def _handler(self, event):
        self.dispatched.append(event)
        await self.pipeline.save_or_update_order(
            internal_order_id=f"EXT_{event.broker_order_id}_{str(event.account_id)[:8]}", broker_order_id=event.broker_order_id,
            account_id=uuid.UUID(event.account_id), source=OrderSource.BROKER_EXTERNAL, exchange=event.exchange, symbol=event.symbol,
            side=DBOrderSide.BUY if event.side == "BUY" else DBOrderSide.SELL, order_type=event.order_type,
            product_type=event.product_type, quantity=event.quantity, filled_quantity=event.filled_quantity, price=event.price,
            status=OrderStatus.OPEN, broker_status=event.status)

    async def baseline_then_discover(self, *records):
        await self.poller.poll_once()
        self.book.extend(records)
        return await self.poller.poll_once()


async def _ambiguous_manual_order(mock_redis, db_session, broker_client, **payload):
    broker_client.place_order = AsyncMock(return_value=_order_result(OrderResultCategory.TIMEOUT))
    return await _manual(_manual_payload(**payload), db_session, mock_redis)


async def _row_snapshot(db_session, internal_order_id):
    order = (await db_session.execute(
        select(Order).where(Order.internal_order_id == internal_order_id).execution_options(populate_existing=True)
    )).scalar_one()
    return (order.status, order.broker_status, order.broker_order_id, order.rejection_reason, order.quantity, order.updated_at)


@pytest.mark.asyncio
async def test_poller_then_resolver_links_the_real_order_and_cancels_the_phantom(
        mock_redis, db_session, fake_master_client_patch, live):
    master, broker_client = fake_master_client_patch
    response = await _ambiguous_manual_order(mock_redis, db_session, broker_client)
    phantom_id = response["client_order_id"]
    harness = PollerHarness(master, mock_redis)
    assert await harness.baseline_then_discover(_book_record("BRK_REAL_1")) == 1       # the poller rediscovers it as external
    external_id = f"EXT_BRK_REAL_1_{str(master.id)[:8]}"
    external_before = await _row_snapshot(db_session, external_id)
    assert (await find_unresolved_send(db_session, master.id, "RELIANCE-EQ", "NSE")).internal_order_id == phantom_id

    resolved = await resolve_unresolved_send(db_session, phantom_id, "PLACED", "found BRK_REAL_1 in the order book", True, "BRK_REAL_1")
    assert resolved.status == OrderStatus.CANCELLED and resolved.broker_status == "SUPERSEDED"
    assert resolved.broker_order_id is None and resolved.pending_quantity == 0
    assert "Superseded by broker order BRK_REAL_1" in resolved.rejection_reason and external_id in resolved.rejection_reason
    assert await _row_snapshot(db_session, external_id) == external_before              # the real row is untouched

    assert await find_unresolved_send(db_session, master.id, "RELIANCE-EQ", "NSE") is None
    assert await list_unresolved_sends(db_session) == []
    (event,) = await _events(db_session, "UNRESOLVED_SEND_RESOLVED")
    assert event.metadata_payload["outcome"] == "PLACED" and event.metadata_payload["superseded_by"] == external_id
    with pytest.raises(ResolutionError):                                                 # and it cannot be resolved again
        await resolve_unresolved_send(db_session, phantom_id, "NOT_PLACED", "second try", True)


@pytest.mark.asyncio
@pytest.mark.parametrize("book,why", [
    (dict(symbol="TCS"), "symbol"),
    (dict(side=BrokerOrderSide.SELL), "side"),
    (dict(quantity=11), "quantity"),
], ids=["symbol", "side", "quantity"])
async def test_a_mismatched_recorded_order_is_refused_and_neither_row_changes(
        mock_redis, db_session, fake_master_client_patch, live, book, why):
    master, broker_client = fake_master_client_patch
    response = await _ambiguous_manual_order(mock_redis, db_session, broker_client)
    phantom_id = response["client_order_id"]
    harness = PollerHarness(master, mock_redis)
    await harness.baseline_then_discover(_book_record("BRK_OTHER_1", **book))
    external_id = f"EXT_BRK_OTHER_1_{str(master.id)[:8]}"
    phantom_before, external_before = await _row_snapshot(db_session, phantom_id), await _row_snapshot(db_session, external_id)

    with pytest.raises(ResolutionError) as err:
        await resolve_unresolved_send(db_session, phantom_id, "PLACED", "found it, I think", True, "BRK_OTHER_1")
    assert err.value.status_code == 409 and why in str(err.value) and "nothing changed" in str(err.value)
    assert await _row_snapshot(db_session, phantom_id) == phantom_before
    assert await _row_snapshot(db_session, external_id) == external_before
    assert await _events(db_session, "UNRESOLVED_SEND_RESOLVED") == []
    assert await find_unresolved_send(db_session, master.id, "RELIANCE-EQ", "NSE") is not None      # still blocking


@pytest.mark.asyncio
async def test_a_broker_id_recorded_on_another_account_is_refused(mock_redis, db_session, fake_master_client_patch, live, sample_accounts):
    master, broker_client = fake_master_client_patch
    response = await _ambiguous_manual_order(mock_redis, db_session, broker_client)
    phantom_id = response["client_order_id"]
    other = sample_accounts["follower1"]
    await _seed_order(db_session, other.id, symbol="RELIANCE", status=OrderStatus.OPEN, broker_order_id="BRK_ELSEWHERE",
                      broker_status="OPEN", source=OrderSource.BROKER_EXTERNAL)
    before = await _row_snapshot(db_session, phantom_id)
    with pytest.raises(ResolutionError) as err:
        await resolve_unresolved_send(db_session, phantom_id, "PLACED", "found it", True, "BRK_ELSEWHERE")
    assert err.value.status_code == 409 and "different account" in str(err.value)
    assert await _row_snapshot(db_session, phantom_id) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [OrderSource.INTERNAL_UI, OrderSource.SYSTEM], ids=["jettone-row", "system-row"])
async def test_only_a_broker_external_row_can_be_linked(mock_redis, db_session, fake_master_client_patch, live, source):
    master, broker_client = fake_master_client_patch
    response = await _ambiguous_manual_order(mock_redis, db_session, broker_client)
    phantom_id = response["client_order_id"]
    existing = await _seed_order(db_session, master.id, symbol="RELIANCE", status=OrderStatus.OPEN, broker_order_id="BRK_SAME",
                                 broker_status="OPEN", source=source)
    existing.quantity = 10
    await db_session.commit()
    before = await _row_snapshot(db_session, phantom_id)
    with pytest.raises(ResolutionError) as err:
        await resolve_unresolved_send(db_session, phantom_id, "PLACED", "found it", True, "BRK_SAME")
    assert err.value.status_code == 409
    assert await _row_snapshot(db_session, phantom_id) == before


@pytest.mark.asyncio
async def test_resolving_before_the_poller_sees_the_order_means_it_is_never_replicated(
        mock_redis, db_session, fake_master_client_patch, live):
    """Documents the operational limitation: PLACED records the id on the INTERNAL_UI row, and the poller never
    replicates Jettone-originated orders. Resolving does not copy anything either, so the Children were not copied -
    that has to be done by hand."""
    master, broker_client = fake_master_client_patch
    response = await _ambiguous_manual_order(mock_redis, db_session, broker_client)
    resolved = await resolve_unresolved_send(db_session, response["client_order_id"], "PLACED", "found BRK_EARLY in the book", True, "BRK_EARLY")
    assert resolved.status == OrderStatus.OPEN and resolved.broker_order_id == "BRK_EARLY"
    assert resolved.source == OrderSource.INTERNAL_UI

    harness = PollerHarness(master, mock_redis)
    assert await harness.baseline_then_discover(_book_record("BRK_EARLY")) == 0
    assert harness.dispatched == []                                                       # nothing is copied, ever


# ══ simultaneous requests ═════════════════════════════════════════════════════════════════
#
# The guard used to be a plain check followed (a few awaits later) by the pre-write, so two simultaneous requests for
# the same account + symbol could both see "nothing outstanding" and both send. `send_claim_lock` makes the check and
# the pre-write one step per account + exchange + symbol. These tests make the race window explicit: a probe wraps the
# guard, records how many requests are INSIDE it at once and holds each one there until a second arrives (or a short
# timeout). Without the lock both requests are inside together and both reach the broker; with it they never are.

class GuardProbe:
    def __init__(self, original, wait_seconds):
        self.original, self.wait_seconds = original, wait_seconds
        self.inside = 0
        self.max_inside = 0
        self._both = asyncio.Event()

    async def __call__(self, *args, **kwargs):
        self.inside += 1
        self.max_inside = max(self.max_inside, self.inside)
        if self.inside >= 2:
            self._both.set()
        try:
            result = await self.original(*args, **kwargs)
            try:
                await asyncio.wait_for(self._both.wait(), self.wait_seconds)
            except asyncio.TimeoutError:
                pass
            return result
        finally:
            self.inside -= 1


async def _manual_in_own_session(payload, mock_redis):
    async with TestingSessionLocal() as session:
        try:
            return await _manual(payload, session, mock_redis)
        except HTTPException as exc:
            return exc


@pytest.mark.asyncio
async def test_simultaneous_manual_orders_for_one_symbol_cannot_both_reach_the_broker(
        mock_redis, db_session, fake_master_client_patch, live, monkeypatch):
    master, broker_client = fake_master_client_patch
    broker_client.place_order = AsyncMock(side_effect=[
        _order_result(OrderResultCategory.TIMEOUT), _order_result(OrderResultCategory.SUCCESS, "BRK_SECOND")])
    probe = GuardProbe(find_unresolved_send, wait_seconds=0.3)
    monkeypatch.setattr("app.api.broker.find_unresolved_send", probe)

    with patch.object(CopyEnginePipeline, "execute_replication", new=AsyncMock(return_value={})):
        results = await asyncio.gather(
            _manual_in_own_session(_manual_payload(), mock_redis), _manual_in_own_session(_manual_payload(), mock_redis))

    assert probe.max_inside == 1                                    # never two requests inside the guard together
    assert broker_client.place_order.await_count == 1               # so only one reached the broker
    blocked = [r for r in results if isinstance(r, HTTPException)]
    sent = [r for r in results if isinstance(r, dict)]
    assert len(blocked) == 1 and blocked[0].status_code == 409
    assert len(sent) == 1 and sent[0]["outcome_unknown"] is True
    rows = (await db_session.execute(select(Order).where(Order.internal_order_id.like("JT_MANUAL_%")))).scalars().all()
    assert len(rows) == 1
    assert unresolved_sends._claim_locks == {}                      # no lock entry is left behind


@pytest.mark.asyncio
async def test_simultaneous_manual_orders_for_different_symbols_are_not_serialised(
        mock_redis, db_session, fake_master_client_patch, live, monkeypatch):
    master, broker_client = fake_master_client_patch
    broker_client.place_order = AsyncMock(side_effect=[
        _order_result(OrderResultCategory.SUCCESS, "BRK_A"), _order_result(OrderResultCategory.SUCCESS, "BRK_B")])
    probe = GuardProbe(find_unresolved_send, wait_seconds=3.0)
    monkeypatch.setattr("app.api.broker.find_unresolved_send", probe)

    with patch.object(CopyEnginePipeline, "execute_replication", new=AsyncMock(return_value={})):
        results = await asyncio.gather(
            _manual_in_own_session(_manual_payload(symbol="RELIANCE"), mock_redis),
            _manual_in_own_session(_manual_payload(symbol="INFY"), mock_redis))

    assert probe.max_inside == 2                                    # unrelated symbols run side by side
    assert all(isinstance(r, dict) and r["placed"] is True for r in results)
    assert broker_client.place_order.await_count == 2


async def _square_off_in_own_session(strategy, follower, payload, mock_redis):
    async with TestingSessionLocal() as session:
        try:
            return await _do_square_off(strategy, follower, payload, session, mock_redis)
        except HTTPException as exc:
            return exc


def _counting_broker(outcomes):
    calls = []

    async def _place_order(self, req):
        calls.append(req.client_order_id)
        category, broker_order_id = outcomes[len(calls) - 1]
        return OrderResult(category=category, client_order_id=req.client_order_id, broker_order_id=broker_order_id,
                           message="x", raw_response={}, timestamp=datetime.now(timezone.utc))

    return _place_order, calls


@pytest.mark.asyncio
async def test_simultaneous_square_offs_for_one_symbol_cannot_both_reach_the_broker(db_session, mock_redis, monkeypatch):
    master, follower, strategy, member = await _setup_follower_strategy(db_session, alice_user_id="AB_R2C_RACE")
    place, calls = _counting_broker([(OrderResultCategory.TIMEOUT, None), (OrderResultCategory.SUCCESS, "BRK_SECOND")])
    monkeypatch.setattr(AliceBlueClient, "place_order", place)
    probe = GuardProbe(find_unresolved_send, wait_seconds=0.3)
    monkeypatch.setattr("app.api.strategies.find_unresolved_send", probe)

    results = await asyncio.gather(
        _square_off_in_own_session(strategy, follower, _square_off(symbol="INFY"), mock_redis),
        _square_off_in_own_session(strategy, follower, _square_off(symbol="INFY-EQ"), mock_redis))

    assert probe.max_inside == 1
    assert len(calls) == 1                                          # only one MARKET order was ever sent
    codes = sorted(r.status_code for r in results if isinstance(r, HTTPException))
    assert codes == [409, 502]                                      # one blocked, one outcome-unknown
    assert len(await _square_off_orders(db_session, follower)) == 1
    assert unresolved_sends._claim_locks == {}


@pytest.mark.asyncio
async def test_simultaneous_square_offs_for_different_symbols_both_proceed(db_session, mock_redis, monkeypatch):
    master, follower, strategy, member = await _setup_follower_strategy(db_session, alice_user_id="AB_R2C_RACE2")
    place, calls = _counting_broker([(OrderResultCategory.SUCCESS, "BRK_1"), (OrderResultCategory.SUCCESS, "BRK_2")])
    monkeypatch.setattr(AliceBlueClient, "place_order", place)
    probe = GuardProbe(find_unresolved_send, wait_seconds=3.0)
    monkeypatch.setattr("app.api.strategies.find_unresolved_send", probe)

    results = await asyncio.gather(
        _square_off_in_own_session(strategy, follower, _square_off(symbol="INFY"), mock_redis),
        _square_off_in_own_session(strategy, follower, _square_off(symbol="TCS"), mock_redis))

    assert probe.max_inside == 2
    assert [r["status"] for r in results] == ["success", "success"] and len(calls) == 2


@pytest.mark.asyncio
async def test_the_claim_lock_is_released_when_the_guard_blocks(mock_redis, db_session, fake_master_client_patch, live):
    master, broker_client = fake_master_client_patch
    await _seed_order(db_session, master.id)
    for _ in range(2):                                              # a blocked request must not leave the lock held
        with pytest.raises(HTTPException):
            await _manual(_manual_payload(symbol="RELIANCE"), db_session, mock_redis)
    assert unresolved_sends._claim_locks == {}
