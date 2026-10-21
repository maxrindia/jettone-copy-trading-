"""
Alice Blue order-send safety (R2A): an order that the broker may already have accepted is never sent again.

Background. The pipeline wraps place_order in RetryPolicy, which re-sends ONE result category, NETWORK_ERROR,
and also any exception place_order raises. That is only safe if NETWORK_ERROR means "the request provably never
left this process" (brokers/transport.py - the contract Groww and Angel One already honour). Alice Blue used to
return NETWORK_ERROR for EVERY httpx.RequestError, including failures after the request was written (ReadError,
WriteError, RemoteProtocolError), and let a response-parsing failure propagate as an exception: both were re-sent,
up to three times, and an exhausted NETWORK_ERROR was then recorded as a rejection although up to three live
orders could exist. This file originally PINNED that behaviour as a finding; it now pins the fix.

Contract asserted here (all with a mocked HTTP layer; nothing leaves the process):

  * provably-not-sent transport errors         -> NETWORK_ERROR, retried under the existing policy
  * every other transport error after the send -> TIMEOUT, exactly ONE POST, never retried
  * timeouts (including connect/pool timeouts) -> TIMEOUT, exactly ONE POST (existing, conservative)
  * an unreadable/unexpected response          -> TIMEOUT, exactly ONE POST, no exception reaches RetryPolicy
  * a 401 / expired session                    -> unchanged: propagates SessionExpiredError
  * normal accept / reject                     -> unchanged

Known ambiguous-response cases that are DELIBERATELY NOT changed here and are pinned as they are: see
test_unchanged_*_is_still_rejected below (a parsable 200 with no order id, and any non-200/400/422 status).
"""
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderStatus
from app.services.alice_blue.client import AliceBlueClient, SessionExpiredError
from app.services.alice_blue.exceptions import AuthenticationError
from app.services.alice_blue.types import OrderRequest, OrderResultCategory, OrderSide, OrderType, ProductType
from app.services.brokers.transport import SendState
from app.services.copy_engine.pipeline import CopyEnginePipeline, is_ambiguous_send
from app.services.copy_engine.validator import EventValidator
from app.services.reliability.retry import RetryPolicy
from conftest import TestingSessionLocal
from test_marketable_limit_strategy import _funds, _limit_event, _market_event

URL = "http://x"


def make(mock_redis, http, account_id="acc-test"):
    return AliceBlueClient(account_id=account_id, user_id="AB00101", api_secret="secret", redis=mock_redis, http_client=http)


def req(client_order_id="CID-R2A"):
    return OrderRequest(symbol="NIFTY24AUG24500CE", exchange="NSE", side=OrderSide.BUY, quantity=50,
                        product_type=ProductType.MIS, order_type=OrderType.LIMIT, price=1, client_order_id=client_order_id,
                        instrument_token="2277")


async def http_with(mock_redis, *, side_effect=None, response=None, account_id="acc-test"):
    http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set(f"alice:session:{account_id}", "TOKEN")
    if side_effect is not None:
        http.request.side_effect = side_effect
    if response is not None:
        http.request.return_value = response
    return http


def raw(status, content=b"", **kw):
    return httpx.Response(status, content=content, request=httpx.Request("POST", URL), **kw)


def posts(http):
    return [c for c in http.request.await_args_list if c.args[0] == "POST"]


RETRY = lambda: RetryPolicy(max_attempts=3, base_delay_sec=0.0)    # noqa: E731

# Errors that prove nothing was written: the only ones that may be reported (and retried) as NETWORK_ERROR.
NOT_SENT = [httpx.ConnectError("refused"), httpx.ProxyError("proxy refused"), httpx.UnsupportedProtocol("bad scheme"),
            httpx.LocalProtocolError("invalid request")]
# Failures that can follow a request the broker already accepted (non-timeout transport errors).
AMBIGUOUS_TRANSPORT = [httpx.ReadError("reset"), httpx.WriteError("pipe"), httpx.RemoteProtocolError("server disconnected"),
                       httpx.CloseError("close"), httpx.DecodingError("bad encoding"), httpx.TooManyRedirects("loop"),
                       httpx.RequestError("base class")]
# Timeouts: TIMEOUT, one POST (existing behaviour). Connect/pool timeouts are provably not sent but stay conservative.
TIMEOUTS = [httpx.ReadTimeout("read"), httpx.WriteTimeout("write"), httpx.ConnectTimeout("connect"), httpx.PoolTimeout("pool")]


def ident(e):
    return type(e).__name__


# ── transport errors ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("exc", AMBIGUOUS_TRANSPORT, ids=ident)
async def test_a_post_send_transport_error_is_ambiguous_and_sent_exactly_once(mock_redis, exc):
    http = await http_with(mock_redis, side_effect=exc)
    client = make(mock_redis, http)
    result = await client.place_order(req())
    assert result.category == OrderResultCategory.TIMEOUT and result.broker_order_id is None
    assert "not resent" in result.message and type(exc).__name__ in result.message
    assert is_ambiguous_send(result)
    http.request.reset_mock()
    out = await RETRY().execute(client.place_order, req())
    assert out.category == OrderResultCategory.TIMEOUT
    assert len(posts(http)) == 1                                  # POSTs through the real RetryPolicy: ONE


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", TIMEOUTS, ids=ident)
async def test_timeouts_stay_ambiguous_and_are_sent_exactly_once(mock_redis, exc):
    http = await http_with(mock_redis, side_effect=exc)
    client = make(mock_redis, http)
    out = await RETRY().execute(client.place_order, req())
    assert out.category == OrderResultCategory.TIMEOUT
    assert len(posts(http)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", NOT_SENT, ids=ident)
async def test_a_failure_that_proves_nothing_was_sent_is_still_retryable(mock_redis, exc):
    http = await http_with(mock_redis, side_effect=exc)
    client = make(mock_redis, http)
    assert (await client.place_order(req())).category == OrderResultCategory.NETWORK_ERROR
    http.request.reset_mock()
    out = await RETRY().execute(client.place_order, req())
    assert out.category == OrderResultCategory.NETWORK_ERROR      # exhausted: nothing ever left the process
    assert len(posts(http)) == 3                                  # the existing retry policy is unchanged


@pytest.mark.asyncio
async def test_a_retryable_failure_followed_by_success_places_exactly_one_order(mock_redis):
    ok = raw(200, b'[{"status":"Ok","result":[{"status":"OK","brokerOrderId":"B1"}]}]', headers={"content-type": "application/json"})
    http = await http_with(mock_redis, side_effect=[httpx.ConnectError("refused"), ok])
    out = await RETRY().execute(make(mock_redis, http).place_order, req())
    assert out.category == OrderResultCategory.SUCCESS and out.broker_order_id == "B1"
    assert len(posts(http)) == 2                                  # one that never left, one that did


@pytest.mark.asyncio
async def test_an_unexpected_exception_after_the_send_is_ambiguous_not_raised(mock_redis):
    http = await http_with(mock_redis, side_effect=RuntimeError("boom inside the transport"))
    out = await RETRY().execute(make(mock_redis, http).place_order, req())
    assert out.category == OrderResultCategory.TIMEOUT
    assert len(posts(http)) == 1


@pytest.mark.asyncio
async def test_an_exception_before_anything_is_sent_still_propagates_unchanged(mock_redis):
    """No session -> AuthenticationError raised by _get_headers before any request. Unchanged: it propagates
    (a retry cannot duplicate an order that was never sent)."""
    http = AsyncMock(spec=httpx.AsyncClient)                      # no session key set
    with pytest.raises(AuthenticationError):
        await make(mock_redis, http).place_order(req())
    http.request.assert_not_called()


# ── the send marker ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_send_marker_is_set_only_when_a_request_is_issued(mock_redis):
    state = SendState()
    http = AsyncMock(spec=httpx.AsyncClient)
    with pytest.raises(AuthenticationError):                      # no session: nothing issued
        await make(mock_redis, http)._request_with_reauth("POST", "/p", state=state)
    assert state.sent is False
    http2 = await http_with(mock_redis, response=raw(200, b"{}"))
    await make(mock_redis, http2)._request_with_reauth("POST", "/p", state=state)
    assert state.sent is True
    await make(mock_redis, http2)._request_with_reauth("GET", "/p")   # state is optional: other callers unchanged


# ── response parsing ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 400, 422])
async def test_an_unparsable_body_is_ambiguous_and_sent_exactly_once(mock_redis, status):
    http = await http_with(mock_redis, response=raw(status, b"<html>gateway</html>"))
    out = await RETRY().execute(make(mock_redis, http).place_order, req())
    assert out.category == OrderResultCategory.TIMEOUT
    assert "unreadable response" in out.message and f"HTTP {status}" in out.message
    assert len(posts(http)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body,expected", [
    (b'["not-a-dict"]', OrderResultCategory.TIMEOUT),      # item.get on a str: used to raise into the retry loop
    (b'[1, 2]', OrderResultCategory.TIMEOUT),              # item.get on an int: same
    (b'"just a string"', OrderResultCategory.REJECTED),    # no exception: no order id, no error -> existing REJECTED
], ids=["str_item", "int_items", "str_body"])
async def test_a_body_of_an_unexpected_shape_never_raises_into_the_retry_loop(mock_redis, body, expected):
    http = await http_with(mock_redis, response=raw(200, body, headers={"content-type": "application/json"}))
    out = await RETRY().execute(make(mock_redis, http).place_order, req())
    assert out.category == expected
    assert len(posts(http)) == 1                                # never a resend, whatever the shape


@pytest.mark.asyncio
async def test_an_accepted_order_is_still_success(mock_redis):
    http = await http_with(mock_redis, response=raw(200, b'[{"status":"Ok","result":[{"status":"OK","brokerOrderId":"B777"}]}]',
                                                    headers={"content-type": "application/json"}))
    out = await RETRY().execute(make(mock_redis, http).place_order, req())
    assert out.category == OrderResultCategory.SUCCESS and out.broker_order_id == "B777"
    assert len(posts(http)) == 1


@pytest.mark.asyncio
async def test_a_definite_rejection_is_still_a_rejection(mock_redis):
    body = b'[{"status":"Not_Ok","result":[{"status":"EC965","message":"Market orders are not allowed"}]}]'
    http = await http_with(mock_redis, response=raw(200, body, headers={"content-type": "application/json"}))
    out = await RETRY().execute(make(mock_redis, http).place_order, req())
    assert out.category == OrderResultCategory.REJECTED and "Market orders are not allowed" in out.message
    assert len(posts(http)) == 1


@pytest.mark.asyncio
async def test_a_401_still_propagates_session_expiry_and_is_not_turned_into_an_ambiguous_result(mock_redis):
    http = await http_with(mock_redis, response=raw(401, b'{"message":"unauthorized"}'))
    with pytest.raises(SessionExpiredError):
        await make(mock_redis, http).place_order(req())
    assert len(posts(http)) == 1                                  # re-auth cannot succeed here; no second POST


# Unchanged and deliberately NOT part of R2A. Recorded so the open question stays visible.
@pytest.mark.asyncio
async def test_unchanged_a_parsable_200_with_no_order_id_is_still_rejected(mock_redis):
    """OPEN: HTTP 200 whose JSON carries no order id and no error is reported as REJECTED today. Whether that
    should be ambiguous needs the broker's documented behaviour; not changed here."""
    http = await http_with(mock_redis, response=raw(200, b"{}", headers={"content-type": "application/json"}))
    out = await make(mock_redis, http).place_order(req())
    assert out.category == OrderResultCategory.REJECTED


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_unchanged_a_5xx_status_is_still_rejected(mock_redis, status):
    """OPEN: a gateway/server error after the send is reported as REJECTED today (the body is never read for
    statuses other than 200/400/422). It is not retried, so it cannot duplicate, but it may be a false
    definitive rejection. Not changed here."""
    http = await http_with(mock_redis, response=raw(status, b"upstream failure"))
    out = await RETRY().execute(make(mock_redis, http).place_order, req())
    assert out.category == OrderResultCategory.REJECTED
    assert len(posts(http)) == 1


# ── end to end through the real pipeline and the real RetryPolicy ─────────────────────────

async def child_state(master_order_id):
    async with TestingSessionLocal() as s:
        trade = (await s.execute(select(CopyTrade).where(CopyTrade.master_order_id == master_order_id))).scalar_one()
        order = (await s.execute(select(Order).where(Order.master_broker_order_id == master_order_id))).scalar_one()
    return trade, order


async def run_pipeline(db_session, mock_redis, accounts, event, *, side_effect=None, response=None):
    follower = accounts["follower1"]
    http = await http_with(mock_redis, side_effect=side_effect, response=response, account_id=str(follower.id))
    real = make(mock_redis, http, account_id=str(follower.id))
    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.place_order = real.place_order                         # the REAL Alice place_order on the mocked transport
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis), retry_policy=RETRY())
    await pipeline.execute_replication(event=event, clients={str(follower.id): client})
    return http


AMBIGUOUS_E2E = [
    ("read_error", dict(side_effect=httpx.ReadError("reset after the request was written"))),
    ("write_error", dict(side_effect=httpx.WriteError("broken pipe"))),
    ("remote_protocol_error", dict(side_effect=httpx.RemoteProtocolError("server disconnected"))),
    ("unparsable_200", dict(response=raw(200, b"<html>gateway</html>"))),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,transport", AMBIGUOUS_E2E, ids=[n for n, _ in AMBIGUOUS_E2E])
async def test_limit_copy_with_an_ambiguous_send_posts_once_and_stays_reconcilable(
        db_session, mock_redis, sample_accounts, sample_strategy, name, transport):
    mid = f"R2A_LIM_{name}"
    http = await run_pipeline(db_session, mock_redis, sample_accounts,
                              _limit_event(quantity=10, price=Decimal("100"), master_order_id=mid), **transport)
    assert len(posts(http)) == 1                                  # exactly one POST to the broker
    trade, order = await child_state(mid)
    assert trade.status == CopyTradeStatus.TIMEOUT                # the state the stale-order sweeper reconciles
    assert trade.submitted_at is not None                         # the moment it may have reached the broker
    assert "not resent" in (trade.failure_reason or "") or "unknown" in (trade.failure_reason or "").lower()
    assert order.status == OrderStatus.PENDING and order.broker_status == "TIMEOUT"
    assert order.status != OrderStatus.REJECTED                   # no false definitive rejection


@pytest.mark.asyncio
async def test_market_copy_with_an_ambiguous_send_posts_once_and_stays_reconcilable(
        db_session, mock_redis, sample_accounts, sample_strategy):
    mid = "R2A_MKT_read_error"
    http = await run_pipeline(db_session, mock_redis, sample_accounts,
                              _market_event(quantity=10, price=Decimal("100"), master_order_id=mid),
                              side_effect=httpx.ReadError("reset after the request was written"))
    assert len(posts(http)) == 1
    trade, order = await child_state(mid)
    assert trade.status == CopyTradeStatus.TIMEOUT and trade.submitted_at is not None
    assert order.status != OrderStatus.REJECTED and order.broker_status == "TIMEOUT"


@pytest.mark.asyncio
async def test_limit_copy_with_a_connect_failure_is_still_retried_and_still_a_definitive_failure(
        db_session, mock_redis, sample_accounts, sample_strategy):
    """Control: a failure that proves nothing was sent keeps the existing behaviour (3 attempts, never a TIMEOUT)."""
    mid = "R2A_LIM_connect_error"
    http = await run_pipeline(db_session, mock_redis, sample_accounts,
                              _limit_event(quantity=10, price=Decimal("100"), master_order_id=mid),
                              side_effect=httpx.ConnectError("refused"))
    assert len(posts(http)) == 3
    trade, order = await child_state(mid)
    assert trade.status != CopyTradeStatus.TIMEOUT
    assert order.status == OrderStatus.REJECTED and order.broker_status == "REJECTED"
