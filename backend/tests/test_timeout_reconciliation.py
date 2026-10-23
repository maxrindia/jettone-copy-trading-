"""
Timeout reconciliation and ambiguous-outcome preservation.

* CopyTrade.submitted_at is written immediately before the broker call (LIMIT, SL and MARKET copy paths) and
  never moved afterwards, so the stale-order sweeper can select TIMEOUT trades.
* An ambiguous (TIMEOUT) LIMIT / SL send is kept unresolved - CopyTrade TIMEOUT, child Order PENDING with
  broker_status TIMEOUT, never REJECTED - until the sweeper reads the real result from the broker order book.
* Reconciliation is a compare-and-set, never resubmits, and is idempotent.

Offline only: mocked httpx transports, the real pipeline, retry policy and sweeper.
"""
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select, update

from app.models.accounts import Account
from app.models.audit import SystemEvent
from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderSide, OrderStatus
from app.services.alice_blue.types import OrderRecord
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.groww.client import order_reference_for
from app.services.reliability.stale_order_sweeper import StaleOrderSweeper
from conftest import TestingSessionLocal
import angel_helpers as A
import groww_helpers as G
from test_angel_one_sell_safety import angel_enabled, _angel_child, CHILD_IP as ANGEL_IP  # noqa: F401
from test_groww_sell_safety import groww_enabled, _groww_child, _positions  # noqa: F401
from test_order_send_safety import (  # noqa: F401
    _no_sleep, _run_groww, _trade, angel_creates, limit_buy, market_buy,
)
from test_order_handling_matrix import _event


# R5: these tests drive a Groww / Angel One Child through the pipeline, which needs the pair enabled.
pytestmark = pytest.mark.usefixtures("cross_broker_pairs_enabled")


def sl_buy(master, oid, qty=10):
    return _event(master, oid, side="BUY", order_type="SL", product="INTRADAY", exchange="NSE",
                  symbol="SBIN", qty=qty, price="800", trigger="795", token="3045")


async def _orders(child):
    async with TestingSessionLocal() as s:
        return (await s.execute(select(Order).where(Order.account_id == child.id))).scalars().all()


def _sweeper(client, grace=-1):   # -1: grace already elapsed (0 can tie with a coarse clock)
    return StaleOrderSweeper(order_book_provider=lambda account_id: client.get_order_book(), timeout_grace_seconds=grace)


async def _timeout_send(db_session, mock_redis, event_fn, oid, exception=None):
    master, child = await _groww_child(db_session)
    fake = G.FakeGroww()
    fake.positions_response = _positions()
    fake.create_exception = exception or httpx.ReadError("connection reset")
    out, client = await _run_groww(mock_redis, master, child, fake, event_fn(master, oid))
    return master, child, fake, client, out


# ── submitted_at is written at the right moment and only once ────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("event_fn", [limit_buy, market_buy, sl_buy], ids=["limit", "market", "sl"])
async def test_a_successful_send_records_submitted_at(db_session, mock_redis, groww_enabled, event_fn):
    master, child = await _groww_child(db_session)
    fake = G.FakeGroww()
    fake.positions_response = _positions()
    before = datetime.now(timezone.utc) - timedelta(seconds=5)
    out, _ = await _run_groww(mock_redis, master, child, fake, event_fn(master, "SUB-OK"))
    assert out["status"] == "SUCCESS", out
    trade = await _trade(child)
    assert trade.submitted_at is not None
    assert trade.submitted_at.replace(tzinfo=timezone.utc) >= before            # timezone-aware UTC like every other column
    assert len(fake.calls("/v1/order/create")) == 1


@pytest.mark.asyncio
async def test_submitted_at_is_stamped_before_the_broker_is_called(db_session, mock_redis, groww_enabled, monkeypatch):
    """A crash or hang inside the send must still leave the timestamp behind."""
    master, child = await _groww_child(db_session)
    fake = G.FakeGroww()
    fake.positions_response = _positions()
    seen = {}
    from app.services.groww.client import GrowwClient
    real_place = GrowwClient.place_order

    async def spy(self, order_req):
        async with TestingSessionLocal() as s:
            t = (await s.execute(select(CopyTrade).where(CopyTrade.client_order_id == order_req.client_order_id))).scalar_one()
            seen["submitted_at"] = t.submitted_at
        return await real_place(self, order_req)
    monkeypatch.setattr(GrowwClient, "place_order", spy)
    await _run_groww(mock_redis, master, child, fake, limit_buy(master, "SUB-BEFORE"))
    assert seen["submitted_at"] is not None


@pytest.mark.asyncio
async def test_stamping_never_overwrites_an_existing_timestamp(db_session, mock_redis, groww_enabled):
    master, child, fake, client, out = await _timeout_send(db_session, mock_redis, limit_buy, "SUB-ONCE")
    trade = await _trade(child)
    first = trade.submitted_at
    assert first is not None
    await CopyEnginePipeline(validator=EventValidator(redis=mock_redis))._stamp_submitted(trade.client_order_id)
    assert (await _trade(child)).submitted_at == first


@pytest.mark.asyncio
async def test_a_failed_stamp_does_not_block_the_send(db_session, mock_redis, monkeypatch):
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))

    def broken():
        raise RuntimeError("db hiccup")
    monkeypatch.setattr("app.services.copy_engine.pipeline.AsyncSessionLocal", broken)
    await pipeline._stamp_submitted("anything")                                   # swallowed and logged, never raised


# ── an ambiguous LIMIT / SL send stays unresolved ──────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("event_fn", [limit_buy, sl_buy], ids=["limit", "sl"])
async def test_groww_ambiguous_send_is_kept_unresolved_with_one_request(db_session, mock_redis, groww_enabled, event_fn):
    master, child, fake, client, out = await _timeout_send(db_session, mock_redis, event_fn, "AMB-U")
    assert out["status"] == "TIMEOUT" and out["broker_order_id"] is None
    assert len(fake.calls("/v1/order/create")) == 1                              # no retry after an ambiguous send
    trade = await _trade(child)
    assert trade.status == CopyTradeStatus.TIMEOUT and trade.broker_order_id is None
    assert trade.submitted_at is not None and trade.failure_reason
    rows = await _orders(child)
    assert len(rows) == 1
    o = rows[0]
    assert o.status == OrderStatus.PENDING and o.broker_status == "TIMEOUT"
    assert o.broker_order_id is None and o.rejection_reason is None              # neither success nor a definitive rejection


@pytest.mark.asyncio
async def test_angel_ambiguous_limit_send_is_kept_unresolved_with_one_request(db_session, mock_redis, angel_enabled):
    master, child = await _angel_child(db_session)
    fake = A.FakeAngel()
    fake.place_exception = httpx.ReadError("connection reset")
    fake.routes["getPosition"] = (200, {"status": True, "data": []})
    client = A.make_client(mock_redis, fake, account_id=str(child.id), static_ip=ANGEL_IP)
    await client.login()
    res = await CopyEnginePipeline(validator=EventValidator(redis=mock_redis)).execute_replication(
        event=limit_buy(master, "AMB-ANG-LIM"), clients={str(child.id): client})
    out = res["results"][str(child.id)]
    assert out["status"] == "TIMEOUT", out
    assert len(angel_creates(fake)) == 1
    trade = await _trade(child)
    assert trade.status == CopyTradeStatus.TIMEOUT and trade.submitted_at is not None
    o = (await _orders(child))[0]
    assert o.status == OrderStatus.PENDING and o.broker_status == "TIMEOUT" and o.rejection_reason is None


@pytest.mark.asyncio
async def test_confirmed_rejection_is_still_a_definitive_rejection(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = G.FakeGroww()
    fake.positions_response = _positions()
    fake.create_response = (400, {"status": "FAILURE", "payload": {"remark": "Insufficient margin"}})
    out, _ = await _run_groww(mock_redis, master, child, fake, limit_buy(master, "REJ-1"))
    assert out["status"] == "REJECTED"
    o = (await _orders(child))[0]
    assert o.status == OrderStatus.REJECTED and o.broker_status == "REJECTED" and o.rejection_reason
    assert (await _trade(child)).status != CopyTradeStatus.TIMEOUT
    assert len(fake.calls("/v1/order/create")) == 1


@pytest.mark.asyncio
async def test_successful_limit_placement_is_unchanged(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = G.FakeGroww()
    fake.positions_response = _positions()
    out, _ = await _run_groww(mock_redis, master, child, fake, limit_buy(master, "OK-1"))
    assert out["status"] == "SUCCESS" and out["broker_order_id"] == "GRW-NEW-1"
    o = (await _orders(child))[0]
    assert o.status == OrderStatus.OPEN and o.broker_status == "SUCCESS" and o.broker_order_id == "GRW-NEW-1"
    assert (await _trade(child)).status == CopyTradeStatus.SUBMITTED           # R3: finalised on accept; the fill syncer owns the rest


@pytest.mark.asyncio
async def test_provably_unsent_errors_are_not_reported_as_unresolved(db_session, mock_redis, groww_enabled, monkeypatch):
    _no_sleep(monkeypatch)
    master, child = await _groww_child(db_session)
    fake = G.FakeGroww()
    fake.positions_response = _positions()
    fake.create_exception = httpx.ConnectError("refused")
    out, _ = await _run_groww(mock_redis, master, child, fake, limit_buy(master, "NS-1"))
    assert out["status"] == "REJECTED"                                          # nothing was sent: not ambiguous
    assert (await _orders(child))[0].status == OrderStatus.REJECTED


# ── sweeper selection ─────────────────────────────────────────────────────────────────────────

async def _landed_book(client, fake, trade, status="EXECUTED", broker_id="GRW-LANDED"):
    fake.order_list_response = G.ok({"order_list": [
        G.order_row(broker_id, status, trade.quantity, trade.quantity if status == "EXECUTED" else 0,
                    ref=order_reference_for(trade.client_order_id))]})


@pytest.mark.asyncio
@pytest.mark.parametrize("event_fn", [limit_buy, market_buy, sl_buy], ids=["limit", "market", "sl"])
async def test_sweeper_reconciles_a_fresh_timeout_without_any_test_help(db_session, mock_redis, groww_enabled, event_fn):
    """No manual timestamp: the row exactly as the pipeline wrote it is selected once the grace period passes."""
    master, child, fake, client, out = await _timeout_send(db_session, mock_redis, event_fn, "SW-FRESH")
    trade = await _trade(child)
    await _landed_book(client, fake, trade)
    async with TestingSessionLocal() as db:
        assert await _sweeper(client, grace=3600)._resolve_timeouts(db) == 0     # still inside the grace window
    async with TestingSessionLocal() as db:
        assert await _sweeper(client, grace=-1)._resolve_timeouts(db) == 1
    after = await _trade(child)
    assert after.status == CopyTradeStatus.SUCCESS and after.broker_order_id == "GRW-LANDED"
    assert len(fake.calls("/v1/order/create")) == 1                              # never resent


@pytest.mark.asyncio
async def test_sweeper_moves_the_child_order_to_what_the_broker_holds(db_session, mock_redis, groww_enabled):
    master, child, fake, client, out = await _timeout_send(db_session, mock_redis, limit_buy, "SW-ORD")
    trade = await _trade(child)
    await _landed_book(client, fake, trade, status="OPEN", broker_id="GRW-WORKING")
    async with TestingSessionLocal() as db:
        await _sweeper(client)._resolve_timeouts(db)
    o = (await _orders(child))[0]
    assert o.broker_order_id == "GRW-WORKING" and o.status == OrderStatus.OPEN and o.pending_quantity == trade.quantity
    assert (await _trade(child)).status == CopyTradeStatus.RECONCILIATION_REQUIRED


@pytest.mark.asyncio
@pytest.mark.parametrize("book_status,order_status,trade_status", [
    ("EXECUTED", OrderStatus.COMPLETE, CopyTradeStatus.SUCCESS),
    ("REJECTED", OrderStatus.REJECTED, CopyTradeStatus.REJECTED),
    ("CANCELLED", OrderStatus.CANCELLED, CopyTradeStatus.FAILED),
])
async def test_sweeper_applies_each_terminal_broker_result(db_session, mock_redis, groww_enabled, book_status, order_status, trade_status):
    master, child, fake, client, out = await _timeout_send(db_session, mock_redis, limit_buy, "SW-TERM")
    trade = await _trade(child)
    await _landed_book(client, fake, trade, status=book_status)
    async with TestingSessionLocal() as db:
        assert await _sweeper(client)._resolve_timeouts(db) == 1
    assert (await _trade(child)).status == trade_status
    o = (await _orders(child))[0]
    assert o.status == order_status and o.broker_order_id == "GRW-LANDED"


@pytest.mark.asyncio
async def test_sweeper_leaves_an_unmatched_timeout_unresolved_and_alerts_once(db_session, mock_redis, groww_enabled):
    master, child, fake, client, out = await _timeout_send(db_session, mock_redis, limit_buy, "SW-NONE")
    fake.order_list_response = G.ok({"order_list": []})
    for _ in range(3):
        async with TestingSessionLocal() as db:
            assert await _sweeper(client)._resolve_timeouts(db) == 0
    async with TestingSessionLocal() as db:
        events = (await db.execute(select(SystemEvent).where(SystemEvent.event_type == "TIMEOUT_UNRESOLVED"))).scalars().all()
    assert len(events) == 1                                                       # no alert on every 60 s sweep
    assert (await _trade(child)).status == CopyTradeStatus.TIMEOUT
    o = (await _orders(child))[0]
    assert o.status == OrderStatus.PENDING and o.broker_status == "TIMEOUT"
    assert len(fake.calls("/v1/order/create")) == 1


@pytest.mark.asyncio
async def test_sweeper_with_an_unavailable_order_book_changes_nothing(db_session, mock_redis, groww_enabled):
    master, child, fake, client, out = await _timeout_send(db_session, mock_redis, limit_buy, "SW-DOWN")

    async def down(account_id):
        raise ConnectionError("broker unreachable")
    async with TestingSessionLocal() as db:
        assert await StaleOrderSweeper(order_book_provider=down, timeout_grace_seconds=-1)._resolve_timeouts(db) == 0
    assert (await _trade(child)).status == CopyTradeStatus.TIMEOUT


# ── rows that existed before submitted_at was ever written ────────────────────────────────────

async def _legacy_timeout_trade(child, *, submitted_at=None, completed_at=None, updated_at=None, status=CopyTradeStatus.TIMEOUT, cid="JT_LEGACY_1"):
    async with TestingSessionLocal() as s:
        t = CopyTrade(id=uuid.uuid4(), master_order_id="M-LEGACY", follower_account_id=child.id,
                      copy_event_id=uuid.uuid4(), symbol="SBIN", exchange="NSE", side=OrderSide.BUY,
                      quantity=10, product_type="INTRADAY", client_order_id=cid, status=status,
                      submitted_at=submitted_at, completed_at=completed_at)
        s.add(t)
        await s.commit()
        if updated_at is not None:
            await s.execute(update(CopyTrade).where(CopyTrade.id == t.id).values(updated_at=updated_at))
            await s.commit()
        return t.id


def _book(cid, status="COMPLETE"):
    return [OrderRecord(broker_order_id="BRK-1", client_order_id=cid, symbol="SBIN", exchange="NSE", side=OrderSide.BUY,
                        quantity=10, filled_quantity=10, average_price=0, status=status, order_timestamp=datetime.now(timezone.utc))]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["completed_at", "updated_at"])
async def test_legacy_rows_with_a_null_submitted_at_are_selected_via_the_fallback(db_session, kind):
    _, child = await _groww_child(db_session)
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    tid = await _legacy_timeout_trade(child, completed_at=old if kind == "completed_at" else None,
                                      updated_at=old if kind == "updated_at" else None)
    sweeper = StaleOrderSweeper(order_book_provider=AsyncProviderFor(_book("JT_LEGACY_1")), timeout_grace_seconds=30)
    async with TestingSessionLocal() as db:
        assert await sweeper._resolve_timeouts(db) == 1
    async with TestingSessionLocal() as s:
        assert (await s.get(CopyTrade, tid)).status == CopyTradeStatus.SUCCESS


@pytest.mark.asyncio
async def test_a_legacy_row_inside_the_grace_window_is_not_touched(db_session):
    _, child = await _groww_child(db_session)
    tid = await _legacy_timeout_trade(child, completed_at=datetime.now(timezone.utc))
    sweeper = StaleOrderSweeper(order_book_provider=AsyncProviderFor(_book("JT_LEGACY_1")), timeout_grace_seconds=3600)
    async with TestingSessionLocal() as db:
        assert await sweeper._resolve_timeouts(db) == 0
    async with TestingSessionLocal() as s:
        assert (await s.get(CopyTrade, tid)).status == CopyTradeStatus.TIMEOUT


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [CopyTradeStatus.PENDING, CopyTradeStatus.SUBMITTED, CopyTradeStatus.SUCCESS,
                                    CopyTradeStatus.REJECTED, CopyTradeStatus.FAILED, CopyTradeStatus.RECONCILIATION_REQUIRED])
async def test_only_timeout_trades_are_reconciled(db_session, status):
    _, child = await _groww_child(db_session)
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    tid = await _legacy_timeout_trade(child, submitted_at=old, status=status)
    sweeper = StaleOrderSweeper(order_book_provider=AsyncProviderFor(_book("JT_LEGACY_1")), timeout_grace_seconds=-1)
    async with TestingSessionLocal() as db:
        assert await sweeper._resolve_timeouts(db) == 0
    async with TestingSessionLocal() as s:
        assert (await s.get(CopyTrade, tid)).status == status


class AsyncProviderFor:
    def __init__(self, book):
        self.book = book

    async def __call__(self, account_id):
        return self.book


# ── duplicate and concurrent sweeper runs ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_running_the_sweeper_twice_resolves_once_and_does_nothing_the_second_time(db_session, mock_redis, groww_enabled):
    master, child, fake, client, out = await _timeout_send(db_session, mock_redis, limit_buy, "SW-TWICE")
    await _landed_book(client, fake, await _trade(child))
    results = []
    for _ in range(2):
        async with TestingSessionLocal() as db:
            results.append(await _sweeper(client)._resolve_timeouts(db))
    assert results == [1, 0]
    assert (await _trade(child)).status == CopyTradeStatus.SUCCESS
    assert len(fake.calls("/v1/order/create")) == 1


@pytest.mark.asyncio
async def test_a_concurrent_resolution_is_never_overwritten(db_session, mock_redis, groww_enabled):
    """Another process resolves the trade between this run's read and its write: compare-and-set on status == TIMEOUT
    means this run applies nothing, and the other result (and the Order row) survive."""
    master, child, fake, client, out = await _timeout_send(db_session, mock_redis, limit_buy, "SW-RACE")
    trade = await _trade(child)
    await _landed_book(client, fake, trade, status="REJECTED")

    async def racing_provider(account_id):
        async with TestingSessionLocal() as other:
            await other.execute(update(CopyTrade).where(CopyTrade.id == trade.id).values(
                status=CopyTradeStatus.SUCCESS, broker_order_id="WON-THE-RACE", failure_reason=None))
            await other.commit()
        return await client.get_order_book()

    async with TestingSessionLocal() as db:
        resolved = await StaleOrderSweeper(order_book_provider=racing_provider, timeout_grace_seconds=-1)._resolve_timeouts(db)
    assert resolved == 0
    after = await _trade(child)
    assert after.status == CopyTradeStatus.SUCCESS and after.broker_order_id == "WON-THE-RACE"
    o = (await _orders(child))[0]
    assert o.status == OrderStatus.PENDING and o.broker_order_id is None          # the losing run wrote nothing
    assert len(fake.calls("/v1/order/create")) == 1


@pytest.mark.asyncio
async def test_several_unresolved_child_orders_are_never_guessed_between(db_session, mock_redis, groww_enabled):
    """Two MARKET fills of one master order can leave two identical unresolved Order rows; neither is touched."""
    master, child, fake, client, out = await _timeout_send(db_session, mock_redis, market_buy, "SW-AMBIG")
    trade = await _trade(child)
    async with TestingSessionLocal() as s:
        original = (await s.execute(select(Order).where(Order.account_id == child.id))).scalar_one()
        twin = Order(internal_order_id=original.internal_order_id + "_2", account_id=child.id, source=original.source,
                     exchange=original.exchange, symbol=original.symbol, side=original.side, order_type=original.order_type,
                     product_type=original.product_type, quantity=original.quantity, filled_quantity=0,
                     pending_quantity=0, price=original.price, status=original.status, broker_status="TIMEOUT",
                     master_broker_order_id=original.master_broker_order_id, broker=original.broker)
        s.add(twin)
        await s.commit()
    await _landed_book(client, fake, trade)
    async with TestingSessionLocal() as db:
        assert await _sweeper(client)._resolve_timeouts(db) == 1
    rows = await _orders(child)
    assert len(rows) == 2 and all(r.broker_order_id is None and r.broker_status == "TIMEOUT" for r in rows)
    assert (await _trade(child)).status == CopyTradeStatus.SUCCESS                # the trade itself is still resolved


# ── ambiguity is never resubmitted by anything in this flow ───────────────────────────────────

@pytest.mark.asyncio
async def test_no_path_resubmits_an_ambiguous_order(db_session, mock_redis, groww_enabled):
    master, child, fake, client, out = await _timeout_send(db_session, mock_redis, limit_buy, "NO-RESEND")
    fake.order_list_response = G.ok({"order_list": []})
    sweeper = _sweeper(client)
    for _ in range(3):
        async with TestingSessionLocal() as db:
            await sweeper.sweep_once(db)
    # the same Master order replayed through the pipeline is deduplicated, not resent
    await CopyEnginePipeline(validator=EventValidator(redis=mock_redis)).execute_replication(
        event=limit_buy(master, "NO-RESEND"), clients={str(child.id): client})
    assert len(fake.calls("/v1/order/create")) == 1
