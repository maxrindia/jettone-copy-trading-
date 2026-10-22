"""
Shared order-state transitions for every copy path (LIMIT, SL, MARKET) and every send outcome, plus the
sweeper's reconciliation of the unresolved ones - including the Alice Blue path, with a mocked client only.

Outcome semantics under test (see pipeline.AMBIGUOUS_SEND_CATEGORIES):
  SUCCESS        accepted                       -> Order OPEN (LIMIT/MARKET) / PENDING (SL)
  REJECTED       broker declined                -> definitive rejection
  NETWORK_ERROR  adapter proved it never left   -> definitive failure
  TIMEOUT        no usable answer after sending -> UNRESOLVED
  UNKNOWN        RetryPolicy: place_order RAISED -> UNRESOLVED (an exception can follow an accepted request)
Unresolved = CopyTrade TIMEOUT, Order PENDING + broker_status TIMEOUT, no broker id, not completed, never resent.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.models.audit import AuditLog, SystemEvent
from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderStatus
from app.services.alice_blue.types import OrderRecord, OrderResult, OrderResultCategory, OrderSide
from app.services.copy_engine.pipeline import CopyEnginePipeline, UNKNOWN_OUTCOME_REASON, is_ambiguous_send
from app.services.copy_engine.validator import EventValidator
from app.services.reliability.retry import RetryPolicy
from app.services.reliability.stale_order_sweeper import StaleOrderSweeper
from conftest import TestingSessionLocal
from test_order_handling_matrix import _child, _event, _setup

SECRET_TEXT = "Traceback ... token=SUPERSECRET123 at C:\\srv\\app\\client.py"


def res(category, message="m", broker_order_id=None, raw=None):
    return OrderResult(category=category, client_order_id=None, broker_order_id=broker_order_id, message=message,
                       raw_response=raw if raw is not None else {}, timestamp=datetime.now(timezone.utc))


def ev(master, oid, kind):
    if kind == "limit":
        return _event(master, oid, side="BUY", order_type="LIMIT", product="INTRADAY", exchange="NSE", symbol="SBIN",
                      qty=10, price="800", token="3045")
    if kind == "sl":
        return _event(master, oid, side="BUY", order_type="SL", product="INTRADAY", exchange="NSE", symbol="SBIN",
                      qty=10, price="800", trigger="795", token="3045")
    return _event(master, oid, side="BUY", order_type="MARKET", product="INTRADAY", exchange="NSE", symbol="SBIN",
                  qty=10, price="800", token="3045")


async def run(db_session, mock_redis, kind, outcome, oid="ST-1"):
    master, child = await _setup(db_session)
    client = _child([])
    if isinstance(outcome, Exception):
        client.place_order.side_effect = outcome
    else:
        client.place_order.return_value = outcome
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis),
                                  retry_policy=RetryPolicy(max_attempts=3, base_delay_sec=0.0))
    r = await pipeline.execute_replication(event=ev(master, oid, kind), clients={str(child.id): client})
    return master, child, client, (r.get("results") or {}).get(str(child.id))


async def rows(child):
    async with TestingSessionLocal() as s:
        trade = (await s.execute(select(CopyTrade).where(CopyTrade.follower_account_id == child.id))).scalar_one()
        orders = (await s.execute(select(Order).where(Order.account_id == child.id))).scalars().all()
        audits = (await s.execute(select(AuditLog).where(AuditLog.account_id == child.id))).scalars().all()
    return trade, orders, audits


KINDS = ["limit", "sl", "market"]


# ── classification ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cat,expected", [
    (OrderResultCategory.TIMEOUT, True), (OrderResultCategory.UNKNOWN, True),
    (OrderResultCategory.SUCCESS, False), (OrderResultCategory.REJECTED, False), (OrderResultCategory.NETWORK_ERROR, False),
])
def test_only_timeout_and_unknown_are_ambiguous(cat, expected):
    assert is_ambiguous_send(res(cat)) is expected


@pytest.mark.asyncio
async def test_unknown_is_produced_only_after_place_order_raised():
    """The sole producer of UNKNOWN is RetryPolicy, and only when place_order raised (or fell through) - i.e. when it
    cannot be shown that the request never reached the broker."""
    async def returns_every_category(category):
        async def f():
            return res(category)
        return await RetryPolicy(max_attempts=2, base_delay_sec=0.0).execute(f)
    for cat in (OrderResultCategory.SUCCESS, OrderResultCategory.REJECTED, OrderResultCategory.TIMEOUT):
        assert (await returns_every_category(cat)).category == cat

    async def raises():
        raise RuntimeError(SECRET_TEXT)
    out = await RetryPolicy(max_attempts=1, base_delay_sec=0.0).execute(raises)
    assert out.category == OrderResultCategory.UNKNOWN


# ── ambiguous outcomes are unresolved on every path ───────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("category", [OrderResultCategory.TIMEOUT, OrderResultCategory.UNKNOWN])
async def test_ambiguous_outcome_is_unresolved_never_failed_or_rejected(db_session, mock_redis, kind, category):
    master, child, client, out = await run(db_session, mock_redis, kind, res(category, message=SECRET_TEXT, raw={"k": "v"}))
    assert out["status"] == "TIMEOUT" and out["broker_order_id"] is None
    assert client.place_order.await_count == 1                                    # an ambiguous result is never re-sent
    trade, orders, audits = await rows(child)
    assert trade.status == CopyTradeStatus.TIMEOUT and trade.broker_order_id is None
    assert trade.submitted_at is not None and trade.completed_at is None          # sent, but not complete
    assert len(orders) == 1
    o = orders[0]
    assert o.status == OrderStatus.PENDING and o.broker_status == "TIMEOUT"
    assert o.broker_order_id is None and o.rejection_reason is None and o.pending_quantity == o.quantity
    assert o.status not in (OrderStatus.FAILED, OrderStatus.REJECTED)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_unknown_outcome_never_persists_raw_exception_text(db_session, mock_redis, kind):
    master, child, client, out = await run(db_session, mock_redis, kind, res(OrderResultCategory.UNKNOWN, message=SECRET_TEXT))
    trade, orders, audits = await rows(child)
    blob = " ".join([trade.failure_reason or "", str(trade.broker_response), *(o.rejection_reason or "" for o in orders),
                     *(a.reason_code or "" for a in audits)])
    assert "SUPERSECRET123" not in blob and "Traceback" not in blob and "C:\\srv" not in blob
    assert trade.failure_reason == UNKNOWN_OUTCOME_REASON


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["limit", "market"])          # the SL branch has never written an AuditLog row
async def test_ambiguous_outcome_is_audited_but_not_as_a_success(db_session, mock_redis, kind):
    master, child, client, out = await run(db_session, mock_redis, kind, res(OrderResultCategory.TIMEOUT, message="no reply"))
    trade, orders, audits = await rows(child)
    assert audits and all(a.event_type != "COPY_EXECUTED" for a in audits)


# ── definitive outcomes are unchanged ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_confirmed_rejection_is_definitive(db_session, mock_redis, kind):
    master, child, client, out = await run(db_session, mock_redis, kind, res(OrderResultCategory.REJECTED, message="Insufficient margin"))
    assert out["status"] == "REJECTED"
    trade, orders, _ = await rows(child)
    assert orders[0].status == OrderStatus.REJECTED and orders[0].rejection_reason == "Insufficient margin"
    assert trade.status != CopyTradeStatus.TIMEOUT
    if kind == "market":
        assert trade.status == CopyTradeStatus.REJECTED and trade.completed_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_provably_unsent_network_error_is_a_definitive_failure(db_session, mock_redis, kind):
    master, child, client, out = await run(db_session, mock_redis, kind, res(OrderResultCategory.NETWORK_ERROR, message="connect refused"))
    assert out["status"] != "TIMEOUT"
    trade, orders, _ = await rows(child)
    assert orders[0].status in (OrderStatus.REJECTED, OrderStatus.FAILED)
    assert trade.status != CopyTradeStatus.TIMEOUT
    assert client.place_order.await_count == 3                                    # retried: the one category proven safe to re-send


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,order_status", [("limit", OrderStatus.OPEN), ("sl", OrderStatus.PENDING), ("market", OrderStatus.OPEN)])
async def test_success_is_unchanged(db_session, mock_redis, kind, order_status):
    master, child, client, out = await run(db_session, mock_redis, kind, res(OrderResultCategory.SUCCESS, broker_order_id="B-1"))
    assert out["status"] == "SUCCESS" and out["broker_order_id"] == "B-1"
    trade, orders, _ = await rows(child)
    assert orders[0].status == order_status and orders[0].broker_order_id == "B-1"
    assert trade.submitted_at is not None and trade.status != CopyTradeStatus.TIMEOUT


# ── Batch A: a place_order that RAISES (untagged) is attempted once, then recorded unresolved ──

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_a_raised_exception_is_not_retried_and_is_recorded_unresolved(db_session, mock_redis, kind):
    """RetryPolicy used to re-send after any raised exception, so an adapter that raised AFTER the broker accepted an
    order was re-sent up to three times. An exception without proof that the request never left now ends the attempts
    at once: ONE placement call, recorded unresolved (not failed), exactly as before."""
    master, child, client, out = await run(db_session, mock_redis, kind, RuntimeError(SECRET_TEXT))
    assert client.place_order.await_count == 1
    trade, orders, _ = await rows(child)
    assert out["status"] == "TIMEOUT" and trade.status == CopyTradeStatus.TIMEOUT
    assert orders[0].status == OrderStatus.PENDING and orders[0].broker_status == "TIMEOUT"


# ── reconciliation of the unresolved rows, all three paths, with mocked Alice-style order books ──

def book_row(cid, status, qty=10, filled=None, broker_id="BRK-9", reason=None):
    return OrderRecord(broker_order_id=broker_id, client_order_id=cid, symbol="SBIN", exchange="NSE", side=OrderSide.BUY,
                       quantity=qty, filled_quantity=qty if filled is None and status == "COMPLETE" else (filled or 0),
                       average_price=Decimal("800"), status=status, order_timestamp=datetime.now(timezone.utc),
                       rejection_reason=reason)


# negative = "the grace window has already elapsed": with 0 the strict `<` against a coarse clock can tie
ELAPSED = -1


def sweeper(book, grace=ELAPSED):
    async def provider(account_id):
        if isinstance(book, Exception):
            raise book
        return book
    return StaleOrderSweeper(order_book_provider=provider, timeout_grace_seconds=grace)


async def sweep(book, grace=ELAPSED):
    async with TestingSessionLocal() as db:
        return await sweeper(book, grace)._resolve_timeouts(db)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("category", [OrderResultCategory.TIMEOUT, OrderResultCategory.UNKNOWN])
@pytest.mark.parametrize("book_status,trade_status,order_status", [
    ("COMPLETE", CopyTradeStatus.SUCCESS, OrderStatus.COMPLETE),
    ("REJECTED", CopyTradeStatus.REJECTED, OrderStatus.REJECTED),
    ("CANCELLED", CopyTradeStatus.FAILED, OrderStatus.CANCELLED),
    ("OPEN", CopyTradeStatus.RECONCILIATION_REQUIRED, OrderStatus.OPEN),
    ("PARTIAL", CopyTradeStatus.SUCCESS, OrderStatus.PARTIAL),
])
async def test_reconciliation_updates_trade_and_order_consistently(db_session, mock_redis, kind, category, book_status, trade_status, order_status):
    master, child, client, out = await run(db_session, mock_redis, kind, res(category))
    trade, _, _ = await rows(child)
    filled = 4 if book_status == "PARTIAL" else None
    assert await sweep([book_row(trade.client_order_id, book_status, filled=filled)]) == 1
    after, orders, _ = await rows(child)
    assert after.status == trade_status and after.broker_order_id == "BRK-9"
    assert orders[0].status == order_status and orders[0].broker_order_id == "BRK-9"
    assert orders[0].broker_status == book_status
    if trade_status != CopyTradeStatus.RECONCILIATION_REQUIRED:
        assert after.completed_at is not None
    assert client.place_order.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_continued_uncertainty_leaves_everything_unresolved_and_alerts_once(db_session, mock_redis, kind):
    master, child, client, out = await run(db_session, mock_redis, kind, res(OrderResultCategory.TIMEOUT))
    trade, _, _ = await rows(child)
    other = [book_row("SOMEONE_ELSES_ORDER", "COMPLETE")]                        # an unmatched broker order must not be adopted
    for _ in range(3):
        assert await sweep(other) == 0
    after, orders, _ = await rows(child)
    assert after.status == CopyTradeStatus.TIMEOUT and after.broker_order_id is None
    assert orders[0].status == OrderStatus.PENDING and orders[0].broker_order_id is None
    async with TestingSessionLocal() as s:
        events = (await s.execute(select(SystemEvent).where(SystemEvent.event_type == "TIMEOUT_UNRESOLVED"))).scalars().all()
    assert len(events) == 1
    assert client.place_order.await_count == 1


@pytest.mark.asyncio
async def test_an_unavailable_order_book_changes_nothing_and_a_later_run_still_resolves(db_session, mock_redis):
    master, child, client, out = await run(db_session, mock_redis, "market", res(OrderResultCategory.TIMEOUT))
    trade, _, _ = await rows(child)
    assert await sweep(ConnectionError("down")) == 0
    after, orders, _ = await rows(child)
    assert after.status == CopyTradeStatus.TIMEOUT and orders[0].status == OrderStatus.PENDING
    assert await sweep([book_row(trade.client_order_id, "COMPLETE")]) == 1
    assert (await rows(child))[0].status == CopyTradeStatus.SUCCESS


@pytest.mark.asyncio
async def test_alice_style_book_without_our_client_order_id_cannot_be_matched_and_stays_manual_review(db_session, mock_redis):
    """Alice Blue's placeorder payload carries no order tag, so its order book returns client_order_id=None
    (get_order_book reads `orderTag`). The sweeper therefore never guesses: a timed-out Alice order that DID land
    stays TIMEOUT and is flagged once for manual verification."""
    master, child, client, out = await run(db_session, mock_redis, "market", res(OrderResultCategory.TIMEOUT))
    trade, _, _ = await rows(child)
    landed = book_row(None, "COMPLETE")                                          # same symbol / side / quantity, no tag
    for _ in range(2):
        assert await sweep([landed]) == 0
    assert (await rows(child))[0].status == CopyTradeStatus.TIMEOUT
    async with TestingSessionLocal() as s:
        events = (await s.execute(select(SystemEvent).where(SystemEvent.event_type == "TIMEOUT_UNRESOLVED"))).scalars().all()
    assert len(events) == 1


@pytest.mark.asyncio
async def test_legacy_market_timeout_rows_with_a_failed_order_are_reconciled(db_session, mock_redis):
    """Rows written before this change: Order FAILED + broker_status TIMEOUT, completed_at set, submitted_at NULL."""
    master, child, client, out = await run(db_session, mock_redis, "market", res(OrderResultCategory.TIMEOUT))
    trade, orders, _ = await rows(child)
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    async with TestingSessionLocal() as s:
        await s.execute(update(CopyTrade).where(CopyTrade.id == trade.id).values(submitted_at=None, completed_at=old))
        await s.execute(update(Order).where(Order.id == orders[0].id).values(status=OrderStatus.FAILED, pending_quantity=0))
        await s.commit()
    assert await sweep([book_row(trade.client_order_id, "COMPLETE")], grace=30) == 1
    after, new_orders, _ = await rows(child)
    assert after.status == CopyTradeStatus.SUCCESS
    assert new_orders[0].status == OrderStatus.COMPLETE and new_orders[0].broker_order_id == "BRK-9"


@pytest.mark.asyncio
async def test_only_eligible_timeout_trades_are_reconciled(db_session, mock_redis):
    master, child, client, out = await run(db_session, mock_redis, "market", res(OrderResultCategory.REJECTED, message="no"))
    trade, orders, _ = await rows(child)
    assert trade.status == CopyTradeStatus.REJECTED
    assert await sweep([book_row(trade.client_order_id, "COMPLETE")]) == 0       # a rejection is never flipped by a book match
    assert (await rows(child))[0].status == CopyTradeStatus.REJECTED


@pytest.mark.asyncio
async def test_duplicate_and_concurrent_reconciliation_apply_once(db_session, mock_redis):
    master, child, client, out = await run(db_session, mock_redis, "limit", res(OrderResultCategory.TIMEOUT))
    trade, _, _ = await rows(child)
    book = [book_row(trade.client_order_id, "REJECTED", reason="RMS")]
    assert [await sweep(book), await sweep(book)] == [1, 0]


@pytest.mark.asyncio
async def test_a_concurrent_resolver_that_wins_the_race_is_not_overwritten(db_session, mock_redis):
    master, child, client, out = await run(db_session, mock_redis, "market", res(OrderResultCategory.UNKNOWN))
    trade, _, _ = await rows(child)

    async def racing(account_id):
        async with TestingSessionLocal() as other:
            await other.execute(update(CopyTrade).where(CopyTrade.id == trade.id).values(
                status=CopyTradeStatus.SUCCESS, broker_order_id="WINNER"))
            await other.commit()
        return [book_row(trade.client_order_id, "REJECTED")]
    async with TestingSessionLocal() as db:
        assert await StaleOrderSweeper(order_book_provider=racing, timeout_grace_seconds=ELAPSED)._resolve_timeouts(db) == 0
    after, orders, _ = await rows(child)
    assert after.status == CopyTradeStatus.SUCCESS and after.broker_order_id == "WINNER"
    assert orders[0].status == OrderStatus.PENDING and orders[0].broker_order_id is None


@pytest.mark.asyncio
async def test_two_identical_unresolved_orders_are_never_guessed_between(db_session, mock_redis):
    master, child, client, out = await run(db_session, mock_redis, "market", res(OrderResultCategory.TIMEOUT))
    trade, orders, _ = await rows(child)
    o = orders[0]
    async with TestingSessionLocal() as s:
        s.add(Order(internal_order_id=o.internal_order_id + "_B", account_id=child.id, source=o.source, exchange=o.exchange,
                    symbol=o.symbol, side=o.side, order_type=o.order_type, product_type=o.product_type, quantity=o.quantity,
                    pending_quantity=o.quantity, price=o.price, status=OrderStatus.PENDING, broker_status="TIMEOUT",
                    master_broker_order_id=o.master_broker_order_id, broker=o.broker))
        await s.commit()
    assert await sweep([book_row(trade.client_order_id, "COMPLETE")]) == 1
    _, both, _ = await rows(child)
    assert len(both) == 2 and all(x.broker_order_id is None and x.status == OrderStatus.PENDING for x in both)
