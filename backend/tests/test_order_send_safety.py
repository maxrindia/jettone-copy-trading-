"""
Duplicate-order safety for the Groww and Angel One adapters.

The pipeline wraps place_order in RetryPolicy, which retries NETWORK_ERROR results AND any exception.
So an order may be re-sent only when the failure PROVES it was never transmitted; every other failure
is an ambiguous outcome that must come back as TIMEOUT after exactly one create request.

Everything is offline: mocked httpx transports, the real RetryPolicy, the real pipeline and sweeper.
"""
import json
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.models.audit import SystemEvent
from app.models.trades import CopyTrade, CopyTradeStatus
from app.services.alice_blue.types import OrderRequest, OrderResultCategory, OrderSide, OrderType, ProductType
from app.services.brokers.transport import is_provably_not_sent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.groww.client import GrowwClient, order_reference_for
from app.services.reliability.retry import RetryPolicy
from app.services.reliability.stale_order_sweeper import StaleOrderSweeper
from conftest import TestingSessionLocal
import angel_helpers as A
import groww_helpers as G
from test_angel_one_sell_safety import angel_enabled, _angel_child, CHILD_IP as ANGEL_IP, ALICE_STYLE_TOKEN  # noqa: F401
from test_groww_sell_safety import groww_enabled, _groww_child, CHILD_IP as GROWW_IP, _positions  # noqa: F401
from test_order_handling_matrix import _event

NOT_SENT = [
    ("ConnectError", lambda: httpx.ConnectError("refused")),
    ("ConnectTimeout", lambda: httpx.ConnectTimeout("connect timed out")),
    ("PoolTimeout", lambda: httpx.PoolTimeout("no connection")),
    ("ProxyError", lambda: httpx.ProxyError("proxy refused the tunnel")),
    ("UnsupportedProtocol", lambda: httpx.UnsupportedProtocol("bad scheme")),
    ("LocalProtocolError", lambda: httpx.LocalProtocolError("invalid request")),
]
AMBIGUOUS = [
    ("ReadError", lambda: httpx.ReadError("connection reset")),
    ("WriteError", lambda: httpx.WriteError("broken pipe")),
    ("ReadTimeout", lambda: httpx.ReadTimeout("no response")),
    ("WriteTimeout", lambda: httpx.WriteTimeout("write stalled")),
    ("RemoteProtocolError", lambda: httpx.RemoteProtocolError("server disconnected")),
    ("CloseError", lambda: httpx.CloseError("close failed")),
    ("DecodingError", lambda: httpx.DecodingError("bad encoding")),
    ("TooManyRedirects", lambda: httpx.TooManyRedirects("redirect loop")),
    ("TimeoutException(base)", lambda: httpx.TimeoutException("timed out")),
    ("NetworkError(base)", lambda: httpx.NetworkError("network")),
    ("TransportError(base)", lambda: httpx.TransportError("transport")),
    ("RequestError(base)", lambda: httpx.RequestError("request")),
]


def order(client_order_id="JT_LIM_ORDER123_acc12345", **over):
    base = dict(symbol="SBIN", exchange="NSE", side=OrderSide.BUY, quantity=5, product_type=ProductType.MIS,
                order_type=OrderType.LIMIT, price=Decimal("800.5"), instrument_token="99999",
                client_order_id=client_order_id)
    base.update(over)
    return OrderRequest(**base)


def policy():
    return RetryPolicy(max_attempts=3, base_delay_sec=0.0)


async def groww(redis, fake=None):
    fake = fake or G.FakeGroww()
    c = G.make_client(redis, fake)
    await c.login()
    return c, fake


async def angel(redis, fake=None):
    fake = fake or A.FakeAngel()
    c = A.make_client(redis, fake)
    await c.login()
    return c, fake


def angel_creates(fake):
    return fake.calls("placeOrder")


# ── the classifier itself ───────────────────────────────────────────────────

@pytest.mark.parametrize("name,make", NOT_SENT, ids=[n for n, _ in NOT_SENT])
def test_only_these_errors_prove_the_request_was_not_sent(name, make):
    assert is_provably_not_sent(make())


@pytest.mark.parametrize("name,make", AMBIGUOUS, ids=[n for n, _ in AMBIGUOUS])
def test_every_other_transport_error_is_ambiguous(name, make):
    assert not is_provably_not_sent(make())


def test_non_transport_exceptions_are_never_proof():
    for exc in (RuntimeError("x"), ValueError("x"), KeyError("x"), OSError("x"), TimeoutError("x")):
        assert not is_provably_not_sent(exc)


# ── Groww: real retry policy, exact request counts ──────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("name,make", NOT_SENT, ids=[n for n, _ in NOT_SENT])
async def test_groww_retries_only_errors_that_prove_nothing_was_sent(mock_redis, name, make):
    c, fake = await groww(mock_redis)
    fake.create_exception = make()
    res = await policy().execute(c.place_order, order())
    assert res.category == OrderResultCategory.NETWORK_ERROR and res.broker_order_id is None
    assert len(fake.calls("/v1/order/create")) == 3                      # the policy's 3 attempts: nothing ever left
    assert fake.calls("/v1/order/status/reference") == []                # no lookup: there is nothing to find


@pytest.mark.asyncio
@pytest.mark.parametrize("name,make", AMBIGUOUS, ids=[n for n, _ in AMBIGUOUS])
async def test_groww_never_resends_after_an_ambiguous_transport_error(mock_redis, name, make):
    c, fake = await groww(mock_redis)
    fake.create_exception = make()
    res = await policy().execute(c.place_order, order())
    assert res.category == OrderResultCategory.TIMEOUT and res.broker_order_id is None
    assert len(fake.calls("/v1/order/create")) == 1                      # exactly one request
    assert len(fake.calls("/v1/order/status/reference")) == 1            # one read-only lookup, no second create


AMBIGUOUS_RESPONSES = [
    ("http_500", (500, {})), ("http_502_html", (502, "<html>bad gateway</html>")), ("http_503_failure", (503, {"status": "FAILURE"})),
    ("http_504", (504, {})), ("200_not_json", (200, b"definitely not json")), ("200_plain_text", (200, "OK")),
    ("200_json_list", (200, [1, 2, 3])), ("200_empty_object", (200, {})),
    ("200_success_no_payload", (200, {"status": "SUCCESS"})),
    ("200_success_empty_payload", (200, {"status": "SUCCESS", "payload": {}})),
    ("200_success_blank_id", (200, {"status": "SUCCESS", "payload": {"groww_order_id": ""}})),
    ("200_success_null_id", (200, {"status": "SUCCESS", "payload": {"groww_order_id": None}})),
    ("200_success_bool_id", (200, {"status": "SUCCESS", "payload": {"groww_order_id": True}})),
    ("200_success_payload_not_object", (200, {"status": "SUCCESS", "payload": "x"})),
    ("http_302", (302, {})),
]
DEFINITIVE_RESPONSES = [
    ("http_400", (400, {"status": "FAILURE"})), ("http_404", (404, {})), ("http_422_text", (422, "bad request")),
    ("http_429", (429, {})), ("200_failure", (200, {"status": "FAILURE"})),
    ("400_with_remark", (400, {"status": "FAILURE", "payload": {"remark": "Insufficient margin"}})),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,resp", AMBIGUOUS_RESPONSES, ids=[n for n, _ in AMBIGUOUS_RESPONSES])
async def test_groww_unusable_responses_are_ambiguous_and_sent_once(mock_redis, name, resp):
    c, fake = await groww(mock_redis)
    fake.create_response = resp
    res = await policy().execute(c.place_order, order())
    assert res.category == OrderResultCategory.TIMEOUT and res.broker_order_id is None
    assert len(fake.calls("/v1/order/create")) == 1 and len(fake.calls("/v1/order/status/reference")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("name,resp", DEFINITIVE_RESPONSES, ids=[n for n, _ in DEFINITIVE_RESPONSES])
async def test_groww_definitive_rejections_are_rejected_without_a_lookup(mock_redis, name, resp):
    c, fake = await groww(mock_redis)
    fake.create_response = resp
    res = await policy().execute(c.place_order, order())
    assert res.category == OrderResultCategory.REJECTED
    assert len(fake.calls("/v1/order/create")) == 1 and fake.calls("/v1/order/status/reference") == []


@pytest.mark.asyncio
async def test_groww_ambiguous_send_that_actually_landed_resolves_to_success_without_a_resend(mock_redis):
    c, fake = await groww(mock_redis)
    fake.create_exception = httpx.ReadError("reset after accept")
    fake.reference_response = G.ok({"groww_order_id": "GRW-LATE", "order_status": "ACKED", "filled_quantity": 0,
                                    "remark": "", "order_reference_id": "x"})
    res = await policy().execute(c.place_order, order())
    assert res.category == OrderResultCategory.SUCCESS and res.broker_order_id == "GRW-LATE"
    assert len(fake.calls("/v1/order/create")) == 1


@pytest.mark.asyncio
async def test_groww_lookup_failure_after_an_ambiguous_send_is_still_a_timeout(mock_redis):
    c, fake = await groww(mock_redis)
    fake.create_exception = httpx.ReadError("reset")
    fake.reference_response = (500, {})                                   # the lookup itself fails
    res = await policy().execute(c.place_order, order())
    assert res.category == OrderResultCategory.TIMEOUT and len(fake.calls("/v1/order/create")) == 1


@pytest.mark.asyncio
async def test_groww_unexpected_exception_after_send_is_an_ambiguous_timeout_not_a_retry(mock_redis, monkeypatch):
    c, fake = await groww(mock_redis)
    fake.create_response = (400, {"status": "FAILURE"})

    def boom(status, data):
        raise ValueError("unexpected")
    monkeypatch.setattr(GrowwClient, "_is_definitive_rejection", staticmethod(boom))
    res = await policy().execute(c.place_order, order())
    assert res.category == OrderResultCategory.TIMEOUT
    assert len(fake.calls("/v1/order/create")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["get_session", "limiter", "body"])
async def test_groww_unexpected_exception_before_send_is_a_clean_rejection_with_no_request(mock_redis, monkeypatch, where):
    c, fake = await groww(mock_redis)
    if where == "get_session":
        monkeypatch.setattr(c, "get_session", AsyncMock(side_effect=RuntimeError("secret detail")))
    elif where == "limiter":
        monkeypatch.setattr(c._orders_limiter, "acquire", AsyncMock(side_effect=RuntimeError("secret detail")))
    else:
        monkeypatch.setattr(c, "build_order_body", lambda **k: (_ for _ in ()).throw(RuntimeError("secret detail")))
    res = await policy().execute(c.place_order, order())
    assert res.category == OrderResultCategory.REJECTED
    assert fake.calls("/v1/order/create") == []
    assert "secret detail" not in res.message                             # sanitized


@pytest.mark.asyncio
async def test_groww_place_order_never_raises(mock_redis, monkeypatch):
    c, fake = await groww(mock_redis)
    monkeypatch.setattr(c, "_place_order", AsyncMock(side_effect=MemoryError("x")))
    res = await c.place_order(order())                                    # even a non-Exception-subclass-of-interest path returns
    assert res.category in (OrderResultCategory.REJECTED, OrderResultCategory.TIMEOUT)


# ── Groww: reference mapping for the sweeper ────────────────────────────────

@pytest.mark.asyncio
async def test_groww_records_the_reference_before_it_sends(mock_redis):
    c, fake = await groww(mock_redis)
    seen = {}
    fake.create_hook = lambda request: seen.update(dict(mock_redis._storage))
    await c.place_order(order("JT_LIM_ORDER123_acc12345"))
    key = f"groww:ref:acc-groww:{order_reference_for('JT_LIM_ORDER123_acc12345')}"
    assert seen.get(key) == "JT_LIM_ORDER123_acc12345"                    # present at the moment the create was issued
    assert mock_redis._expiry[key] > 2 * 24 * 3600                        # kept for days, for the sweeper


@pytest.mark.asyncio
async def test_groww_does_not_send_if_the_reference_cannot_be_recorded(mock_redis):
    c, fake = await groww(mock_redis)
    original = mock_redis.set.side_effect

    async def failing(key, *a, **k):
        if str(key).startswith("groww:ref:"):
            raise ConnectionError("redis down")
        return await original(key, *a, **k)
    mock_redis.set.side_effect = failing
    res = await c.place_order(order())
    assert res.category == OrderResultCategory.REJECTED and fake.calls("/v1/order/create") == []


@pytest.mark.asyncio
async def test_groww_order_book_restores_the_jettone_client_order_id(mock_redis):
    c, fake = await groww(mock_redis)
    await c.place_order(order("JT_LIM_ORDER123_acc12345"))
    ref = order_reference_for("JT_LIM_ORDER123_acc12345")
    fake.order_list_response = G.ok({"order_list": [
        G.order_row("GRW-A", "EXECUTED", 5, 5, ref=ref),                          # ours
        G.order_row("GRW-B", "OPEN", 5, 0, ref="JTunmapped0000000000"),           # ours-looking but never recorded
        G.order_row("GRW-C", "OPEN", 5, 0, ref="MANUALORDER1234"),                # placed in the Groww app
    ]})
    book = {r.broker_order_id: r.client_order_id for r in await c.get_order_book()}
    assert book == {"GRW-A": "JT_LIM_ORDER123_acc12345", "GRW-B": "JTunmapped0000000000", "GRW-C": "MANUALORDER1234"}


@pytest.mark.asyncio
async def test_groww_order_book_survives_the_mapping_store_failing(mock_redis):
    c, fake = await groww(mock_redis)
    ref = order_reference_for("JT_X_acc12345")
    fake.order_list_response = G.ok({"order_list": [G.order_row("GRW-A", "OPEN", 5, 0, ref=ref)]})
    original = mock_redis.get.side_effect

    async def failing(key):
        if str(key).startswith("groww:ref:"):
            raise ConnectionError("redis down")
        return await original(key)
    mock_redis.get.side_effect = failing
    (rec,) = await c.get_order_book()
    assert rec.client_order_id == ref                                         # left unmapped -> manual review, no exception


# ── Angel One: real retry policy, exact request counts ──────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("name,make", NOT_SENT, ids=[n for n, _ in NOT_SENT])
async def test_angel_retries_only_errors_that_prove_nothing_was_sent(mock_redis, name, make):
    c, fake = await angel(mock_redis)
    fake.place_exception = make()
    res = await policy().execute(c.place_order, order())
    assert res.category == OrderResultCategory.NETWORK_ERROR
    assert len(angel_creates(fake)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("name,make", AMBIGUOUS, ids=[n for n, _ in AMBIGUOUS])
async def test_angel_never_resends_after_an_ambiguous_transport_error(mock_redis, name, make):
    c, fake = await angel(mock_redis)
    fake.place_exception = make()
    res = await policy().execute(c.place_order, order())
    assert res.category == OrderResultCategory.TIMEOUT and res.broker_order_id is None
    assert len(angel_creates(fake)) == 1                                       # one request, and nothing to look up by


ANGEL_AMBIGUOUS = [
    ("http_500", (500, {})), ("http_502_html", (502, "<html>bad gateway</html>")), ("http_504", (504, {"status": False})),
    ("200_not_json", (200, b"nope")), ("200_json_list", (200, [1])), ("200_empty_object", (200, {})),
    ("200_status_true_no_data", (200, {"status": True})), ("200_status_true_null_data", (200, {"status": True, "data": None})),
    ("200_blank_orderid", (200, {"status": True, "data": {"orderid": ""}})),
    ("200_bool_orderid", (200, {"status": True, "data": {"orderid": True}})),
    ("200_data_not_object", (200, {"status": True, "data": "x"})), ("http_302", (302, {})),
]
ANGEL_DEFINITIVE = [
    ("http_400", (400, {"status": False, "message": "bad"})), ("http_422", (422, {})), ("http_429", (429, {"message": "slow down"})),
    ("200_status_false", (200, {"status": False, "message": "Insufficient funds", "errorcode": "AB4036", "data": None})),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,resp", ANGEL_AMBIGUOUS, ids=[n for n, _ in ANGEL_AMBIGUOUS])
async def test_angel_unusable_responses_are_ambiguous_and_sent_once(mock_redis, name, resp):
    c, fake = await angel(mock_redis)
    fake.place_response = resp
    res = await policy().execute(c.place_order, order())
    assert res.category == OrderResultCategory.TIMEOUT and len(angel_creates(fake)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("name,resp", ANGEL_DEFINITIVE, ids=[n for n, _ in ANGEL_DEFINITIVE])
async def test_angel_definitive_rejections_are_rejected(mock_redis, name, resp):
    c, fake = await angel(mock_redis)
    fake.place_response = resp
    res = await policy().execute(c.place_order, order())
    assert res.category == OrderResultCategory.REJECTED and len(angel_creates(fake)) == 1


@pytest.mark.asyncio
async def test_angel_unexpected_exception_after_send_is_timeout_before_send_is_rejected(mock_redis, monkeypatch):
    c, fake = await angel(mock_redis)
    fake.place_response = (400, {"status": False})
    monkeypatch.setattr(c, "_well_formed", staticmethod(lambda resp: (_ for _ in ()).throw(ValueError("unexpected"))))
    res = await policy().execute(c.place_order, order())
    assert res.category == OrderResultCategory.TIMEOUT and len(angel_creates(fake)) == 1
    c2, fake2 = await angel(mock_redis)
    monkeypatch.setattr(c2, "get_session", AsyncMock(side_effect=RuntimeError("secret detail")))
    res2 = await policy().execute(c2.place_order, order())
    assert res2.category == OrderResultCategory.REJECTED and angel_creates(fake2) == [] and "secret detail" not in res2.message


# ── pipeline-level: one request, a TIMEOUT result, and what the sweeper can do with it ──

def market_buy(master, oid, qty=10):
    """A Master MARKET fill (see test_timeout_reconciliation.py for the LIMIT and SL copy paths)."""
    return _event(master, oid, side="BUY", order_type="MARKET", product="INTRADAY", exchange="NSE",
                  symbol="SBIN", qty=qty, price="800", token=ALICE_STYLE_TOKEN)


def limit_buy(master, oid, qty=10):
    return _event(master, oid, side="BUY", order_type="LIMIT", product="INTRADAY", exchange="NSE",
                  symbol="SBIN", qty=qty, price="800", token=ALICE_STYLE_TOKEN)


def _no_sleep(monkeypatch):
    monkeypatch.setattr("app.services.reliability.retry.asyncio.sleep", AsyncMock())


async def _run_groww(mock_redis, master, child, fake, event):
    client = G.make_client(mock_redis, fake, account_id=str(child.id), static_ip=GROWW_IP)
    await client.login()
    res = await CopyEnginePipeline(validator=EventValidator(redis=mock_redis)).execute_replication(
        event=event, clients={str(child.id): client})
    return (res.get("results") or {}).get(str(child.id)), client


async def _age_trade(child):
    """Give the TIMEOUT trade a submitted_at in the past, so the sweeper's grace window has elapsed."""
    from datetime import datetime, timedelta, timezone
    async with TestingSessionLocal() as s:
        t = (await s.execute(select(CopyTrade).where(CopyTrade.follower_account_id == child.id))).scalar_one()
        t.submitted_at = datetime.now(timezone.utc) - timedelta(hours=1)
        await s.commit()


@pytest.mark.asyncio
async def test_a_timed_out_send_records_when_it_was_submitted(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = G.FakeGroww()
    fake.positions_response = _positions()
    fake.create_exception = httpx.ReadError("reset")
    await _run_groww(mock_redis, master, child, fake, market_buy(master, "GAP-1"))
    assert (await _trade(child)).submitted_at is not None


async def _trade(child):
    async with TestingSessionLocal() as s:
        return (await s.execute(select(CopyTrade).where(CopyTrade.follower_account_id == child.id))).scalar_one()


@pytest.mark.asyncio
async def test_pipeline_groww_ambiguous_send_is_one_request_and_a_timeout(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = G.FakeGroww()
    fake.positions_response = _positions()
    fake.create_exception = httpx.ReadError("connection reset")
    out, _ = await _run_groww(mock_redis, master, child, fake, market_buy(master, "AMB-1"))
    assert out["status"] == "TIMEOUT", out
    assert len(fake.calls("/v1/order/create")) == 1
    trade = await _trade(child)
    assert trade.status == CopyTradeStatus.TIMEOUT and trade.broker_order_id is None


@pytest.mark.asyncio
async def test_pipeline_limit_path_sends_once_and_never_reports_success_for_an_ambiguous_send(db_session, mock_redis, groww_enabled):
    """An ambiguous LIMIT send: one create request, no resend, no success, and the outcome stays unresolved (CopyTrade
    TIMEOUT, Order not REJECTED) rather than being recorded as a definitive broker rejection."""
    master, child = await _groww_child(db_session)
    fake = G.FakeGroww()
    fake.positions_response = _positions()
    fake.create_exception = httpx.ReadError("connection reset")
    out, _ = await _run_groww(mock_redis, master, child, fake, limit_buy(master, "AMB-LIM-1"))
    assert out["status"] != "SUCCESS" and out["broker_order_id"] is None
    assert len(fake.calls("/v1/order/create")) == 1
    from app.models.trades import Order, OrderStatus
    async with TestingSessionLocal() as s:
        rows = (await s.execute(select(Order).where(Order.account_id == child.id))).scalars().all()
    assert rows and all(r.broker_order_id is None for r in rows)
    assert out["status"] == "TIMEOUT"
    assert all(r.status != OrderStatus.REJECTED and r.rejection_reason is None for r in rows)
    trade = await _trade(child)
    assert trade.status == CopyTradeStatus.TIMEOUT and "outcome unknown" in (trade.failure_reason or "")


@pytest.mark.asyncio
async def test_pipeline_groww_unusable_5xx_is_one_request_and_a_timeout(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = G.FakeGroww()
    fake.positions_response = _positions()
    fake.create_response = (503, "<html>unavailable</html>")
    out, _ = await _run_groww(mock_redis, master, child, fake, market_buy(master, "AMB-2"))
    assert out["status"] == "TIMEOUT" and len(fake.calls("/v1/order/create")) == 1


@pytest.mark.asyncio
async def test_pipeline_groww_not_sent_error_is_retried_up_to_the_policy_limit(db_session, mock_redis, groww_enabled, monkeypatch):
    _no_sleep(monkeypatch)
    master, child = await _groww_child(db_session)
    fake = G.FakeGroww()
    fake.positions_response = _positions()
    fake.create_exception = httpx.ConnectError("refused")
    out, _ = await _run_groww(mock_redis, master, child, fake, market_buy(master, "AMB-3"))
    assert out["status"] != "SUCCESS" and out["status"] != "TIMEOUT"
    assert len(fake.calls("/v1/order/create")) == 3                      # allowed only because none of them was transmitted


@pytest.mark.asyncio
async def test_sweeper_reconciles_a_groww_timeout_through_the_reference_map(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = G.FakeGroww()
    fake.positions_response = _positions()
    fake.create_exception = httpx.ReadError("reset after the broker accepted it")
    out, client = await _run_groww(mock_redis, master, child, fake, market_buy(master, "SWEEP-1"))
    assert out["status"] == "TIMEOUT"
    trade = await _trade(child)
    # The order did land at Groww; its book entry carries our (hashed) reference.
    fake.order_list_response = G.ok({"order_list": [
        G.order_row("GRW-LANDED", "EXECUTED", trade.quantity, trade.quantity, ref=order_reference_for(trade.client_order_id))]})
    await _age_trade(child)
    sweeper = StaleOrderSweeper(order_book_provider=lambda account_id: client.get_order_book(), timeout_grace_seconds=0)
    async with TestingSessionLocal() as db:
        assert await sweeper._resolve_timeouts(db) == 1
    resolved = await _trade(child)
    assert resolved.status == CopyTradeStatus.SUCCESS and resolved.broker_order_id == "GRW-LANDED"
    assert len(fake.calls("/v1/order/create")) == 1                      # reconciliation never re-sends


@pytest.mark.asyncio
async def test_sweeper_flags_a_groww_timeout_that_is_not_in_the_book_and_does_not_resend(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = G.FakeGroww()
    fake.positions_response = _positions()
    fake.create_exception = httpx.ReadError("reset")
    out, client = await _run_groww(mock_redis, master, child, fake, market_buy(master, "SWEEP-2"))
    fake.order_list_response = G.ok({"order_list": []})
    await _age_trade(child)
    sweeper = StaleOrderSweeper(order_book_provider=lambda account_id: client.get_order_book(), timeout_grace_seconds=0)
    async with TestingSessionLocal() as db:
        assert await sweeper._resolve_timeouts(db) == 0
        events = (await db.execute(select(SystemEvent).where(SystemEvent.event_type == "TIMEOUT_UNRESOLVED"))).scalars().all()
    assert len(events) == 1
    assert (await _trade(child)).status == CopyTradeStatus.TIMEOUT
    assert len(fake.calls("/v1/order/create")) == 1


@pytest.mark.asyncio
async def test_pipeline_angel_ambiguous_send_is_one_request_a_timeout_and_manual_review(db_session, mock_redis, angel_enabled):
    master, child = await _angel_child(db_session)
    fake = A.FakeAngel()
    fake.place_exception = httpx.ReadError("connection reset")
    # an order that did land: Angel One's book can never be matched to our id (placeOrder has no client reference)
    fake.routes["getOrderBook"] = (200, {"status": True, "data": [
        {"orderid": "ANG-LANDED", "tradingsymbol": "SBIN-EQ", "symboltoken": "3045", "exchange": "NSE",
         "transactiontype": "BUY", "ordertype": "LIMIT", "producttype": "INTRADAY", "price": "800",
         "triggerprice": "0", "quantity": "10", "filledshares": "10", "averageprice": "800", "status": "syn_done", "text": ""}]})
    fake.routes["getPosition"] = (200, {"status": True, "data": []})
    client = A.make_client(mock_redis, fake, account_id=str(child.id), static_ip=ANGEL_IP)
    await client.login()
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=market_buy(master, "AMB-ANG-1"), clients={str(child.id): client})
    out = (res.get("results") or {}).get(str(child.id))
    assert out["status"] == "TIMEOUT", out
    assert len(angel_creates(fake)) == 1
    await _age_trade(child)
    sweeper = StaleOrderSweeper(order_book_provider=lambda account_id: client.get_order_book(), timeout_grace_seconds=0)
    async with TestingSessionLocal() as db:
        assert await sweeper._resolve_timeouts(db) == 0                  # cannot be matched automatically
        events = (await db.execute(select(SystemEvent).where(SystemEvent.event_type == "TIMEOUT_UNRESOLVED"))).scalars().all()
    assert len(events) == 1                                                # surfaced for manual review
    assert (await _trade(child)).status == CopyTradeStatus.TIMEOUT
    assert len(angel_creates(fake)) == 1                                   # and never resent
