"""
R2 Batch A: Alice Blue placeorder response classification and retry safety.

What changed, and why each rule exists:

  F1  A rejection is read ONLY from the structured status fields (an EC### code or an explicit failure word in
      `status` / `result[0].status`). The old check looked for the letters "EC" anywhere in the raw body, so a symbol
      such as TECHM-EQ or RECLTD, or a message such as "SECURITY accepted", turned an ACCEPTED order into a REJECTED
      one - the broker held a live order that Jettone recorded as refused.
  F2  Only a response that PROVES an outcome is classified. HTTP 5xx / 408 / 429 / redirects, a 200 without a usable
      broker order id, an error status that also carries an order id, and any malformed body are AMBIGUOUS (TIMEOUT):
      never re-sent, resolved by the sweeper / operator. A 4xx with a structured body is a definite refusal; an
      HTTP 401 is a definite refusal too (the request was not authenticated, so it was not executed).
  RP  RetryPolicy re-sends only a NETWORK_ERROR result (provably never sent) or an exception TAGGED as raised before
      the request left (transport.mark_not_sent). Any other exception ends the attempts at once as UNKNOWN. A non-401
      response that merely mentions an expired session is just another status code - it is never read as "not placed".

Everything uses a mocked HTTP layer and an in-memory database: no broker, no network.
"""
import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select

from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderStatus
from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.types import OrderResult, OrderResultCategory
from app.services.brokers.transport import is_marked_not_sent, mark_not_sent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.reliability.retry import RetryPolicy
from app.services.reliability.unresolved_sends import find_unresolved_send
from conftest import TestingSessionLocal
from test_alice_blue_send_findings import RETRY, http_with, make, posts, raw, req
from test_duplicate_create_db_backstop import make_event
from test_manual_master_order import fake_master_client_patch  # noqa: F401
from test_marketable_limit_strategy import _funds
from test_unresolved_manual_sends import _manual, _manual_payload, live  # noqa: F401

JSON = {"content-type": "application/json"}
SECRET = "Bearer SECRET-TOKEN-abc123"


@pytest.fixture(autouse=True)
def no_real_database(monkeypatch):
    """A 401 makes the client mark the account's session invalid in the database; keep that on the test database."""
    monkeypatch.setattr("app.services.alice_blue.client.AsyncSessionLocal", TestingSessionLocal)


def ok_body(order_id="B1001", **extra):
    item = {"status": "OK", "brokerOrderId": order_id, "orderNo": order_id}
    item.update(extra)
    return json.dumps([{"status": "Ok", "message": "Success", "result": [item]}]).encode()


def err_body(code="EC941", message="Order rejected by RMS", order_id=""):
    return json.dumps([{"status": "Ok", "result": [{"status": code, "message": message, "brokerOrderId": order_id}]}]).encode()


async def place(mock_redis, response=None, side_effect=None, policy=True):
    http = await http_with(mock_redis, response=response, side_effect=side_effect)
    client = make(mock_redis, http)
    out = await (RETRY().execute(client.place_order, req()) if policy else client.place_order(req()))
    return out, http


# ══ F1: "EC" inside an accepted response is not an error ═════════════════════════════════

ACCEPTED_WITH_EC = [
    ok_body(tradingSymbol="TECHM-EQ"),
    ok_body(tradingSymbol="RECLTD"),
    ok_body(tradingSymbol="ECLERX"),
    ok_body(message="SECURITY order accepted by EXCHANGE"),
    json.dumps([{"status": "Ok", "message": "SECURE connection, ELECTRONIC order", "result": [
        {"status": "OK", "brokerOrderId": "B2002", "message": "RECEIVED"}]}]).encode(),
    ok_body(order_id="26EC00109500"),                               # EC inside the broker order id itself
]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ACCEPTED_WITH_EC, ids=["techm", "recltd", "eclerx", "security-msg", "secure-electronic", "ec-in-order-id"])
async def test_an_accepted_order_containing_the_letters_ec_is_success(mock_redis, body):
    out, http = await place(mock_redis, raw(200, body, headers=JSON))
    assert out.category == OrderResultCategory.SUCCESS and out.broker_order_id
    assert len(posts(http)) == 1


@pytest.mark.asyncio
async def test_a_message_that_merely_mentions_an_error_code_is_not_a_status(mock_redis):
    """Only status fields decide. An EC code in free text next to a valid order id does not turn it into a rejection."""
    out, _ = await place(mock_redis, raw(200, ok_body(message="see EC941 in the FAQ"), headers=JSON))
    assert out.category == OrderResultCategory.SUCCESS


@pytest.mark.asyncio
@pytest.mark.parametrize("code,message", [("EC930", "Invalid order type"), ("EC941", "Insufficient funds"),
                                          ("EC965", "Market orders are not allowed."), ("ec123", "lower-case code")])
async def test_a_structured_ec_status_without_an_order_id_is_a_definite_rejection(mock_redis, code, message):
    out, http = await place(mock_redis, raw(200, err_body(code, message), headers=JSON))
    assert out.category == OrderResultCategory.REJECTED and message in out.message
    assert out.broker_order_id is None and len(posts(http)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    b'[{"status":"Not_Ok","result":[{"status":"EC965","message":"Market orders are not allowed"}]}]',
    b'{"status":"EC965","message":"Market orders are not allowed.","result":null}',
    b'[{"status":"Ok","result":[{"status":"REJECTED","message":"Rejected by RMS"}]}]',
    b'[{"status":"Rejected","message":"Rejected by RMS"}]',
    b'[{"status":"FAILED","emsg":"Failed"}]',
], ids=["not-ok-top-with-ec-result", "top-level-ec-null-result", "result-rejected", "top-rejected", "top-failed"])
async def test_other_structured_failure_shapes_are_definite_rejections(mock_redis, body):
    out, http = await place(mock_redis, raw(200, body, headers=JSON))
    assert out.category == OrderResultCategory.REJECTED and len(posts(http)) == 1


@pytest.mark.asyncio
async def test_an_error_status_that_also_carries_an_order_id_is_contradictory_and_ambiguous(mock_redis):
    out, http = await place(mock_redis, raw(200, err_body("EC941", "weird", order_id="B7777"), headers=JSON))
    assert out.category == OrderResultCategory.TIMEOUT and "contradictory" in out.message
    assert out.broker_order_id is None and len(posts(http)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("body,why", [
    (b'{"status": ', "truncated JSON"),
    (b"", "empty body"),
    (b"null", "JSON null"),
    (b'"just a string"', "JSON string"),
    (b"[]", "empty list"),
    (b"[1, 2]", "list of ints"),
    (b'[{"status":"Ok","result":[]}]', "empty result list"),
    (b'[{"status":"Ok","result":["x"]}]', "result item not an object"),
    (b'[{"status":"Ok","result":[{"status":"OK","brokerOrderId":"   "}]}]', "blank order id"),
    (b'[{"status":"Ok","result":[{"status":"OK"}]}]', "no order id field"),
    (b"<html>gateway</html>", "html"),
], ids=lambda v: v if isinstance(v, str) else None)
async def test_malformed_or_idless_200_bodies_are_ambiguous_with_one_post(mock_redis, body, why):
    out, http = await place(mock_redis, raw(200, body, headers=JSON))
    assert out.category == OrderResultCategory.TIMEOUT, why
    assert out.broker_order_id is None and len(posts(http)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status_value", ["OK", "ok", "Ok", "", "PLACED"])
async def test_a_missing_or_unfamiliar_non_error_status_with_an_order_id_is_not_a_rejection(mock_redis, status_value):
    body = json.dumps([{"status": "Ok", "result": [{"status": status_value, "brokerOrderId": "B9"}]}]).encode()
    out, _ = await place(mock_redis, raw(200, body, headers=JSON))
    assert out.category == OrderResultCategory.SUCCESS and out.broker_order_id == "B9"


# ══ F2: HTTP status classes ════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("status", [408, 429, 500, 501, 502, 503, 504, 599, 301, 302, 307, 201, 202, 204])
async def test_statuses_that_do_not_prove_an_outcome_are_ambiguous_one_post_and_sanitized(mock_redis, status):
    body = f'{{"message":"internal detail {SECRET}"}}'.encode()
    out, http = await place(mock_redis, raw(status, body, headers=JSON))
    assert out.category == OrderResultCategory.TIMEOUT
    assert f"HTTP {status}" in out.message and out.raw_response == {"http_status": status}   # code kept, body dropped
    assert "SECRET" not in out.message and "SECRET" not in json.dumps(out.raw_response)
    assert len(posts(http)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 403, 404, 409, 422])
async def test_a_4xx_with_a_structured_body_is_a_definite_rejection(mock_redis, status):
    body = json.dumps({"status": "Not_Ok", "message": "Invalid parameter"}).encode()
    out, http = await place(mock_redis, raw(status, body, headers=JSON))
    assert out.category == OrderResultCategory.REJECTED and f"HTTP {status}" in out.message and "Invalid parameter" in out.message
    assert len(posts(http)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 403, 404, 422])
@pytest.mark.parametrize("body", [b"<html>Forbidden</html>", b"", b"not json", b'"str"'])
async def test_a_4xx_with_an_unreadable_body_is_ambiguous(mock_redis, status, body):
    out, http = await place(mock_redis, raw(status, body))
    assert out.category == OrderResultCategory.TIMEOUT and "unreadable response" in out.message
    assert len(posts(http)) == 1


@pytest.mark.asyncio
async def test_a_long_broker_message_is_truncated(mock_redis):
    out, _ = await place(mock_redis, raw(200, err_body("EC941", "x" * 5000), headers=JSON))
    assert out.category == OrderResultCategory.REJECTED and len(out.message) < 400


# ══ 401: definite, one POST, no re-authentication retry ═══════════════════════════════════

@pytest.mark.asyncio
async def test_a_401_is_a_definite_rejection_with_one_post_and_invalidates_the_session(mock_redis):
    account_id = str(uuid.uuid4())
    http = await http_with(mock_redis, response=raw(401, b'{"message":"unauthorized"}'), account_id=account_id)
    client = make(mock_redis, http, account_id=account_id)
    out = await RETRY().execute(client.place_order, req())
    assert out.category == OrderResultCategory.REJECTED and "HTTP 401" in out.message
    assert len(posts(http)) == 1
    assert await mock_redis.get(f"alice:session:{account_id}") is None          # the refused session is dropped


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 408, 429, 500, 502, 503, 504])
@pytest.mark.parametrize("body", [b'{"message":"session expired"}', b'{"message":"Invalid session, please login again"}'])
async def test_a_non_401_response_mentioning_an_expired_session_sends_exactly_once(mock_redis, status, body):
    """The wording used to make _is_auth_failure raise SessionExpiredError, which RetryPolicy answered by sending the
    order again (three POSTs observed). It is now just a status code: one POST whatever the body says."""
    out, http = await place(mock_redis, raw(status, body, headers=JSON))
    assert len(posts(http)) == 1
    expected = OrderResultCategory.REJECTED if status == 403 else OrderResultCategory.TIMEOUT
    assert out.category == expected


@pytest.mark.asyncio
async def test_a_200_mentioning_an_expired_session_without_an_order_id_is_ambiguous(mock_redis):
    out, http = await place(mock_redis, raw(200, b'{"status":"Ok","message":"session expired","result":[]}', headers=JSON))
    assert out.category == OrderResultCategory.TIMEOUT and len(posts(http)) == 1


# ══ transport failures (unchanged R2A rules, re-asserted with exact counts) ═════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("exc,posts_expected,category", [
    (httpx.ConnectError("refused"), 3, OrderResultCategory.NETWORK_ERROR),         # proven not sent: retried
    (httpx.ReadError("reset"), 1, OrderResultCategory.TIMEOUT),
    (httpx.RemoteProtocolError("server disconnected"), 1, OrderResultCategory.TIMEOUT),
    (httpx.ReadTimeout("slow"), 1, OrderResultCategory.TIMEOUT),
    (RuntimeError("boom in transport"), 1, OrderResultCategory.TIMEOUT),
], ids=["connect-error", "read-error", "remote-protocol", "read-timeout", "runtime-error"])
async def test_transport_failures_send_only_when_provably_unsent(mock_redis, exc, posts_expected, category):
    out, http = await place(mock_redis, side_effect=exc)
    assert out.category == category and len(posts(http)) == posts_expected


# ══ RetryPolicy ═══════════════════════════════════════════════════════════════════════════

def result(category):
    return OrderResult(category=category, client_order_id="C", broker_order_id=None, message="m", raw_response={},
                       timestamp=__import__("datetime").datetime.now(__import__("datetime").timezone.utc))


@pytest.mark.asyncio
async def test_an_untagged_exception_is_attempted_once_and_is_unknown():
    func = AsyncMock(side_effect=RuntimeError("boom"))
    out = await RetryPolicy(max_attempts=3, base_delay_sec=0.0).execute(func)
    assert out.category == OrderResultCategory.UNKNOWN and "boom" in out.message and func.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [False, None, 1, "yes", 0])
async def test_only_an_exact_true_tag_counts_as_proof(value):
    exc = RuntimeError("x")
    exc.request_not_sent = value
    assert is_marked_not_sent(exc) is False
    func = AsyncMock(side_effect=exc)
    out = await RetryPolicy(max_attempts=3, base_delay_sec=0.0).execute(func)
    assert out.category == OrderResultCategory.UNKNOWN and func.await_count == 1


@pytest.mark.asyncio
async def test_an_exception_tagged_as_raised_before_sending_is_retried_then_a_definite_failure():
    func = AsyncMock(side_effect=lambda: (_ for _ in ()).throw(mark_not_sent(RuntimeError("no session"))))
    out = await RetryPolicy(max_attempts=3, base_delay_sec=0.0).execute(func)
    assert func.await_count == 3
    assert out.category == OrderResultCategory.NETWORK_ERROR and "not sent" in out.message.lower()


@pytest.mark.asyncio
async def test_a_tagged_exception_followed_by_success_places_once():
    calls = []

    async def func():
        calls.append(1)
        if len(calls) == 1:
            raise mark_not_sent(RuntimeError("transient, never sent"))
        return result(OrderResultCategory.SUCCESS)

    out = await RetryPolicy(max_attempts=3, base_delay_sec=0.0).execute(func)
    assert out.category == OrderResultCategory.SUCCESS and len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("category,expected_calls", [
    (OrderResultCategory.NETWORK_ERROR, 3), (OrderResultCategory.TIMEOUT, 1), (OrderResultCategory.UNKNOWN, 1),
    (OrderResultCategory.REJECTED, 1), (OrderResultCategory.SUCCESS, 1)])
async def test_result_categories_are_retried_exactly_as_before(category, expected_calls):
    func = AsyncMock(return_value=result(category))
    await RetryPolicy(max_attempts=3, base_delay_sec=0.0).execute(func)
    assert func.await_count == expected_calls


# ── the client tags exactly the exceptions that escaped before the request left ─────────────

@pytest.mark.asyncio
async def test_no_session_is_raised_before_any_request_and_is_tagged(mock_redis):
    http = AsyncMock(spec=httpx.AsyncClient)                       # no session key set
    client = make(mock_redis, http)
    with pytest.raises(Exception) as err:
        await client.place_order(req())
    assert is_marked_not_sent(err.value) and http.request.await_count == 0
    out = await RETRY().execute(client.place_order, req())
    assert out.category == OrderResultCategory.NETWORK_ERROR and http.request.await_count == 0   # retried, never sent


@pytest.mark.asyncio
async def test_an_infrastructure_error_before_the_send_is_tagged_and_retried_without_a_request(mock_redis):
    http = await http_with(mock_redis, response=raw(200, ok_body(), headers=JSON))
    client = make(mock_redis, http)
    with patch("app.services.alice_blue.client.resolve_instrument_info", AsyncMock(side_effect=RuntimeError("contract master down"))) as resolve:
        out = await RETRY().execute(client.place_order, req())
    assert out.category == OrderResultCategory.NETWORK_ERROR
    assert resolve.await_count == 3 and len(posts(http)) == 0


@pytest.mark.asyncio
async def test_an_exception_after_the_send_is_never_tagged_and_never_retried(mock_redis):
    http = await http_with(mock_redis, response=raw(200, ok_body(), headers=JSON))
    client = make(mock_redis, http)

    async def sent_then_raises(order, state):
        state.sent = True
        raise RuntimeError("blew up after the request left")

    with patch.object(client, "_place_order_once", sent_then_raises):
        with pytest.raises(RuntimeError) as err:
            await client.place_order(req())
        assert not is_marked_not_sent(err.value)
        calls = []

        async def counting(order, state):
            calls.append(1)
            await sent_then_raises(order, state)

        with patch.object(client, "_place_order_once", counting):
            out = await RetryPolicy(max_attempts=3, base_delay_sec=0.0).execute(client.place_order, req())
    assert out.category == OrderResultCategory.UNKNOWN and len(calls) == 1


@pytest.mark.asyncio
async def test_read_only_requests_keep_their_reauth_behaviour(mock_redis):
    """_request_with_reauth without a send marker (reads) is unchanged: a 401 still forces re-authentication, which
    fails closed with SessionExpiredError."""
    account_id = str(uuid.uuid4())
    http = await http_with(mock_redis, response=raw(401, b"unauthorized"), account_id=account_id)
    client = make(mock_redis, http, account_id=account_id)
    from app.services.alice_blue.exceptions import SessionExpiredError
    with pytest.raises(SessionExpiredError):
        await client._request_with_reauth("GET", "/open-api/od/v1/orders/book")


# ══ integration: exact placement counts through every path ═══════════════════════════════

KINDS = ["LIMIT", "SL", "MARKET"]
AMBIGUOUS_TRANSPORTS = {
    "http-503": dict(response=raw(503, b"upstream failure")),
    "http-504-session-wording": dict(response=raw(504, b'{"message":"session expired"}', headers=JSON)),
    "http-429": dict(response=raw(429, b"slow down")),
    "200-no-order-id": dict(response=raw(200, b'{"status":"Ok","result":[]}', headers=JSON)),
    "200-malformed": dict(response=raw(200, b"<html>gateway</html>")),
    "read-error": dict(side_effect=httpx.ReadError("reset")),
}


async def run_kind(db_session, mock_redis, accounts, kind, mid, **transport):
    follower = accounts["follower1"]
    http = await http_with(mock_redis, account_id=str(follower.id), **transport)
    real = make(mock_redis, http, account_id=str(follower.id))
    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.place_order = real.place_order
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis), retry_policy=RETRY())
    result = await pipeline.execute_replication(event=make_event(kind, mid), clients={str(follower.id): client})
    async with TestingSessionLocal() as s:
        trade = (await s.execute(select(CopyTrade).where(CopyTrade.master_order_id == mid))).scalar_one()
        order = (await s.execute(select(Order).where(Order.master_broker_order_id == mid))).scalar_one()
    return http, result["results"][str(follower.id)], trade, order


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("name", list(AMBIGUOUS_TRANSPORTS))
async def test_pipeline_an_ambiguous_response_posts_once_and_stays_unresolved(db_session, mock_redis, sample_accounts, sample_strategy, kind, name):
    http, outcome, trade, order = await run_kind(db_session, mock_redis, sample_accounts, kind, f"BA_{kind}_{name}", **AMBIGUOUS_TRANSPORTS[name])
    assert len(posts(http)) == 1                                              # exactly ONE placement POST
    assert outcome["status"] == "TIMEOUT"
    assert trade.status == CopyTradeStatus.TIMEOUT and trade.submitted_at is not None
    assert order.status == OrderStatus.PENDING and order.broker_status == "TIMEOUT" and order.broker_order_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_pipeline_a_401_is_a_definite_rejection_with_one_post(db_session, mock_redis, sample_accounts, sample_strategy, kind):
    http, outcome, trade, order = await run_kind(db_session, mock_redis, sample_accounts, kind, f"BA_{kind}_401",
                                                 response=raw(401, b'{"message":"unauthorized"}'))
    assert len(posts(http)) == 1
    assert outcome["status"] == "REJECTED"
    assert order.status == OrderStatus.REJECTED and trade.status != CopyTradeStatus.TIMEOUT


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_pipeline_an_accepted_symbol_with_ec_is_a_success(db_session, mock_redis, sample_accounts, sample_strategy, kind):
    http, outcome, trade, order = await run_kind(db_session, mock_redis, sample_accounts, kind, f"BA_{kind}_EC",
                                                 response=raw(200, ok_body("B5005", tradingSymbol="TECHM-EQ"), headers=JSON))
    assert len(posts(http)) == 1 and outcome["status"] != "REJECTED"
    assert order.broker_order_id == "B5005" and order.status != OrderStatus.REJECTED


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_pipeline_a_provably_unsent_failure_is_retried_then_failed_not_unresolved(db_session, mock_redis, sample_accounts, sample_strategy, kind):
    http, outcome, trade, order = await run_kind(db_session, mock_redis, sample_accounts, kind, f"BA_{kind}_CONN",
                                                 side_effect=httpx.ConnectError("refused"))
    assert len(posts(http)) == 3                                              # every attempt provably never left
    assert trade.status != CopyTradeStatus.TIMEOUT and order.status != OrderStatus.PENDING


# ── manual master order and square-off with the REAL Alice place_order on a mocked transport ──

@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(AMBIGUOUS_TRANSPORTS))
async def test_manual_master_order_ambiguous_response_posts_once_and_blocks_the_retry(mock_redis, db_session, fake_master_client_patch, live, name):
    master, broker_client = fake_master_client_patch
    http = await http_with(mock_redis, account_id=str(master.id), **AMBIGUOUS_TRANSPORTS[name])
    broker_client.place_order = make(mock_redis, http, account_id=str(master.id)).place_order
    response = await _manual(_manual_payload(), db_session, mock_redis)
    assert response["placed"] is False and response["outcome_unknown"] is True and len(posts(http)) == 1
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as blocked:
        await _manual(_manual_payload(), db_session, mock_redis)
    assert blocked.value.status_code == 409 and len(posts(http)) == 1          # the retry never reached the transport
    assert (await find_unresolved_send(db_session, master.id, "RELIANCE-EQ", "NSE")) is not None


@pytest.mark.asyncio
async def test_manual_master_order_401_is_a_definite_rejection_that_does_not_block(mock_redis, db_session, fake_master_client_patch, live):
    master, broker_client = fake_master_client_patch
    http = await http_with(mock_redis, account_id=str(master.id), response=raw(401, b'{"message":"unauthorized"}'))
    broker_client.place_order = make(mock_redis, http, account_id=str(master.id)).place_order
    response = await _manual(_manual_payload(), db_session, mock_redis)
    assert response["placed"] is False and "outcome_unknown" not in response and len(posts(http)) == 1
    (order,) = (await db_session.execute(select(Order).where(Order.internal_order_id.like("JT_MANUAL_%")))).scalars().all()
    assert order.status == OrderStatus.REJECTED
    assert await find_unresolved_send(db_session, master.id, "RELIANCE-EQ", "NSE") is None


@pytest.mark.asyncio
async def test_manual_master_order_non_401_session_wording_is_unresolved_not_rejected(mock_redis, db_session, fake_master_client_patch, live):
    master, broker_client = fake_master_client_patch
    http = await http_with(mock_redis, account_id=str(master.id), response=raw(503, b'{"message":"session expired"}', headers=JSON))
    broker_client.place_order = make(mock_redis, http, account_id=str(master.id)).place_order
    response = await _manual(_manual_payload(), db_session, mock_redis)
    assert response["outcome_unknown"] is True and len(posts(http)) == 1
    (order,) = (await db_session.execute(select(Order).where(Order.internal_order_id.like("JT_MANUAL_%")))).scalars().all()
    assert order.status == OrderStatus.PENDING and order.broker_status == "TIMEOUT"


@pytest.mark.asyncio
async def test_square_off_ambiguous_response_posts_once_and_blocks_the_retry(db_session, mock_redis, monkeypatch):
    from fastapi import HTTPException
    from test_unresolved_manual_sends import _do_square_off, _square_off, _square_off_orders, _setup_follower_strategy
    master, follower, strategy, member = await _setup_follower_strategy(db_session, alice_user_id="AB_BA_SQ")
    await mock_redis.set(f"alice:session:{follower.id}", "TOKEN")
    seen = []

    async def fake_request(self, method, url, **kwargs):
        seen.append((method, url))
        return raw(503, b'{"message":"session expired"}', headers=JSON)

    monkeypatch.setattr(httpx.AsyncClient, "request", fake_request)
    with pytest.raises(HTTPException) as unknown:
        await _do_square_off(strategy, follower, _square_off(symbol="RELIANCE"), db_session, mock_redis)
    assert unknown.value.status_code == 502 and "UNKNOWN" in unknown.value.detail
    placements = [s for s in seen if s[0] == "POST" and "placeorder" in s[1]]
    assert len(placements) == 1
    (order,) = await _square_off_orders(db_session, follower)
    assert order.status == OrderStatus.PENDING and order.broker_status == "TIMEOUT"

    with pytest.raises(HTTPException) as blocked:
        await _do_square_off(strategy, follower, _square_off(symbol="RELIANCE"), db_session, mock_redis)
    assert blocked.value.status_code == 409
    assert len([s for s in seen if s[0] == "POST" and "placeorder" in s[1]]) == 1
