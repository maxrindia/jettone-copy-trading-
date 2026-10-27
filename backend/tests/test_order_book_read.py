"""
Contract tests for the normalized Master order-book read (brokers/order_book_read.py).

1. Equivalence: the poller's previous inline fetch (`_fetch_book`, copied verbatim from the commit before this change) is the
   oracle. For every client behaviour the new read must have the same effect on the poller (skip / seed on an empty book /
   process the same rows) and make exactly the same broker calls in the same order.
2. The four states on REAL clients over offline fakes (Alice Blue, Groww, Angel One): no network, no credentials, no broker.
3. Diagnostics never carry a broker message, and the read never raises.
Passing these says the read is honest about what it knows. It does NOT make any broker eligible as a Master: that is the
registry's and the capability profile's decision, asserted at the end.
"""
import ast
import inspect
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.types import OrderRecord, OrderSide
from app.services.brokers import order_book_read as R
from app.services.brokers.order_book_read import (
    OrderBookRead, OrderBookState, read_order_book, read_order_book_rows, read_order_book_single,
)
from test_master_poller_other_brokers import Driver

APP = Path(__file__).resolve().parent.parent / "app"


# ── the oracle: MasterOrderPoller._fetch_book as it was, verbatim ───────────────────────────────────────────

async def _reference_fetch_book(client: Any, *, checked: bool):
    raw: Any = None
    if checked:
        probe_fn = getattr(client, "get_order_book_raw", None)
        if callable(probe_fn):
            probe = probe_fn()
            if inspect.isawaitable(probe):
                raw = await probe
                if raw is None:
                    return None
    book = await client.get_order_book()
    if checked and isinstance(raw, list) and raw and not book:
        return None                      # the raw read saw orders but the parsed read found none: inconsistent
    return book


async def _old_effect(client, checked):
    """What the old poller did with the result: skip, or process these rows (seeding when checked)."""
    try:
        book = await _reference_fetch_book(client, checked=checked)
    except Exception:
        return ("skip", ())
    if book is None:
        return ("skip", ())
    if not checked and not book:
        return ("skip", ())              # processing [] on a later poll changes nothing: the same as skipping
    return ("process", tuple(book))


def _new_effect(read: OrderBookRead):
    if not read.is_known:
        return ("skip", ())
    if not read.checked and not read.records:
        return ("skip", ())              # the same normalisation as the oracle: an empty later poll changes nothing
    return ("process", read.records)


def rec(oid="1"):
    return OrderRecord(broker_order_id=oid, client_order_id=None, symbol="SBIN-EQ", exchange="NSE", side=OrderSide.BUY,
                       quantity=1, filled_quantity=0, average_price=Decimal("0"), status="OPEN",
                       order_timestamp=datetime.now(timezone.utc))


class Fake:
    """A scripted client that records every call, so call sequences can be compared exactly."""

    def __init__(self, book=None, raw=..., book_raises=None, raw_raises=None, raw_not_awaitable=False):
        self.calls = []
        self._book, self._raw, self._book_raises, self._raw_raises = book, raw, book_raises, raw_raises
        if raw is not ... or raw_raises or raw_not_awaitable:
            if raw_not_awaitable:
                self.get_order_book_raw = lambda: (self.calls.append("raw"), "not awaitable")[1]
            else:
                async def get_order_book_raw():
                    self.calls.append("raw")
                    if self._raw_raises:
                        raise self._raw_raises
                    return self._raw
                self.get_order_book_raw = get_order_book_raw

    async def get_order_book(self):
        self.calls.append("book")
        if self._book_raises:
            raise self._book_raises
        return list(self._book or [])


SCENARIOS = {
    # Alice Blue convention (offers a raw read)
    "alice_rows": dict(book=[rec("1"), rec("2")], raw=[{"brokerOrderId": "1"}, {"brokerOrderId": "2"}]),
    "alice_empty_proven": dict(book=[], raw=[]),
    "alice_raw_unavailable": dict(book=[], raw=None),
    "alice_raw_unavailable_but_parsed_rows": dict(book=[rec("1")], raw=None),
    "alice_raw_rows_parsed_empty": dict(book=[], raw=[{"brokerOrderId": "1"}]),
    "alice_raw_raises": dict(book=[rec("1")], raw_raises=RuntimeError("boom")),
    "alice_book_raises": dict(book_raises=RuntimeError("boom"), raw=[{"brokerOrderId": "1"}]),
    "raw_probe_not_awaitable": dict(book=[rec("1")], raw_not_awaitable=True),
    "raw_probe_not_awaitable_empty": dict(book=[], raw_not_awaitable=True),
    # raising convention (no raw read: Groww / Angel One style)
    "raising_rows": dict(book=[rec("7")]),
    "raising_empty": dict(book=[]),
    "raising_raises": dict(book_raises=ValueError("bad page")),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("checked", [True, False])
@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_the_new_read_has_exactly_the_old_effect_and_the_same_calls(name, checked):
    old_client, new_client = Fake(**SCENARIOS[name]), Fake(**SCENARIOS[name])
    old = await _old_effect(old_client, checked)
    read = await read_order_book(new_client, checked=checked)
    assert _new_effect(read) == old, (name, checked, read)
    assert new_client.calls == old_client.calls, (name, checked)


@pytest.mark.asyncio
@pytest.mark.parametrize("checked", [True, False])
async def test_a_bare_magicmock_client_behaves_as_before(checked):
    """Many tests build the poller with MagicMock clients: their get_order_book_raw is callable but not awaitable, which the old
    code ignored. The new read must ignore it too."""
    for book in ([rec("1")], []):
        client = MagicMock()
        client.get_order_book = AsyncMock(return_value=book)
        old = await _old_effect(client, checked)
        client2 = MagicMock()
        client2.get_order_book = AsyncMock(return_value=book)
        assert _new_effect(await read_order_book(client2, checked=checked)) == old


# ── states, explicitly ───────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("name,checked,state,reason", [
    ("alice_rows", True, OrderBookState.ORDERS, R.READ_OK),
    ("alice_empty_proven", True, OrderBookState.EMPTY, R.READ_OK),
    ("alice_raw_unavailable", True, OrderBookState.UNKNOWN, R.RAW_READ_UNAVAILABLE),
    ("alice_raw_rows_parsed_empty", True, OrderBookState.UNKNOWN, R.RAW_AND_PARSED_DISAGREE),
    ("alice_raw_raises", True, OrderBookState.FAILED, R.READ_RAISED),
    ("alice_book_raises", False, OrderBookState.FAILED, R.READ_RAISED),
    ("alice_empty_proven", False, OrderBookState.UNKNOWN, R.EMPTY_NOT_PROVEN),
    ("alice_rows", False, OrderBookState.ORDERS, R.READ_OK),
    ("raising_empty", False, OrderBookState.EMPTY, R.READ_OK),
    ("raising_empty", True, OrderBookState.EMPTY, R.READ_OK),
    ("raising_raises", True, OrderBookState.FAILED, R.READ_RAISED),
])
async def test_each_state_and_reason(name, checked, state, reason):
    read = await read_order_book(Fake(**SCENARIOS[name]), checked=checked)
    assert (read.state, read.reason, read.checked) == (state, reason, checked)
    assert read.is_known is (state in (OrderBookState.ORDERS, OrderBookState.EMPTY))
    if not read.is_known:
        assert read.records == ()                                    # unknown / failed never carries rows


@pytest.mark.asyncio
async def test_no_client_is_unknown_not_empty():
    read = await read_order_book(None, checked=True)
    assert read.state is OrderBookState.UNKNOWN and read.reason == R.NO_CLIENT and not read.is_known


@pytest.mark.asyncio
@pytest.mark.parametrize("checked", [True, False])
async def test_a_failure_carries_only_the_exception_type_never_the_broker_message(checked):
    secret = "Bearer eyJhbGciOi-session-token-SECRET apiSecret=xyz"
    read = await read_order_book(Fake(book_raises=RuntimeError(secret)), checked=checked)
    assert read.state is OrderBookState.FAILED and read.detail == "RuntimeError"
    assert "SECRET" not in repr(read) and "token" not in repr(read)


@pytest.mark.asyncio
async def test_the_read_never_raises_even_on_odd_returns():
    for bad in (None, (), "not a list"):
        client = Fake()
        client.get_order_book = AsyncMock(return_value=bad)
        await read_order_book(client, checked=True)          # must not raise


# ── real clients over offline fakes ───────────────────────────────────────────────────────────────────────────

def _alice(mock_redis, *, status=200, rows=None, raises=None):
    http = AsyncMock(spec=httpx.AsyncClient)
    resp = AsyncMock()
    resp.status_code = status
    resp.json = lambda: {"result": rows or []}
    http.request = AsyncMock(side_effect=raises) if raises else AsyncMock(return_value=resp)
    return AliceBlueClient(account_id="acc-test", user_id="AB00101", api_secret="secret", redis=mock_redis, http_client=http)


def _alice_row(oid):
    return {"brokerOrderId": oid, "tradingSymbol": "SBIN", "exchange": "NSE", "transactionType": "BUY", "quantity": 1,
            "filledQuantity": 0, "averageTradedPrice": 0, "orderStatus": "OPEN", "product": "INTRADAY", "orderType": "LIMIT",
            "price": 500.0, "orderTime": "2026-10-27 10:00:00"}


@pytest.mark.asyncio
@pytest.mark.parametrize("checked,kw,state,reason", [
    (True, dict(rows=[_alice_row("A1")]), OrderBookState.ORDERS, R.READ_OK),
    (True, dict(rows=[]), OrderBookState.EMPTY, R.READ_OK),
    (True, dict(status=500), OrderBookState.UNKNOWN, R.RAW_READ_UNAVAILABLE),
    (True, dict(raises=httpx.ConnectError("down")), OrderBookState.UNKNOWN, R.RAW_READ_UNAVAILABLE),
    (False, dict(rows=[_alice_row("A1")]), OrderBookState.ORDERS, R.READ_OK),
    (False, dict(rows=[]), OrderBookState.UNKNOWN, R.EMPTY_NOT_PROVEN),            # an empty book and a failure look alike
    (False, dict(status=500), OrderBookState.UNKNOWN, R.EMPTY_NOT_PROVEN),          # Alice's [] on failure is not "empty"
    (False, dict(raises=httpx.ConnectError("down")), OrderBookState.UNKNOWN, R.EMPTY_NOT_PROVEN),
])
async def test_real_alice_blue_client(mock_redis, checked, kw, state, reason):
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
    read = await read_order_book(_alice(mock_redis, **kw), checked=checked)
    assert (read.state, read.reason) == (state, reason)
    if state is OrderBookState.ORDERS:
        assert [r.broker_order_id for r in read.records] == ["A1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["groww", "angel"])
@pytest.mark.parametrize("checked", [True, False])
async def test_real_raising_convention_clients(mock_redis, broker, checked):
    drv = await Driver(broker).start(mock_redis)
    drv.set_book([drv.row("1001", "open")])
    read = await read_order_book(drv.client, checked=checked)
    assert read.state is OrderBookState.ORDERS and [r.broker_order_id for r in read.records] == ["1001"]
    drv.set_book([])
    read = await read_order_book(drv.client, checked=checked)
    assert read.state is OrderBookState.EMPTY                         # they raise on failure, so [] is a real empty book
    drv.set_raw(500, {"error": "down"})
    read = await read_order_book(drv.client, checked=checked)
    assert read.state is OrderBookState.FAILED and read.reason == R.READ_RAISED and read.records == ()


# ── structure and eligibility ─────────────────────────────────────────────────────────────────────────────────

def test_the_poller_reads_the_book_only_through_the_contract():
    src = (APP / "services/reliability/master_order_poller.py").read_text(encoding="utf-8")
    calls = {n.func.attr for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "get_order_book" not in calls and "get_order_book_raw" not in calls
    assert "read_order_book" in src


def test_the_contract_module_makes_no_eligibility_decision_and_imports_no_client():
    tree = ast.parse((APP / "services/brokers/order_book_read.py").read_text(encoding="utf-8"))
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert mods <= {"dataclasses", "enum", "typing", "app.services.alice_blue.types"}, mods
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"can_be_master", "check_pair", "settings"}


def test_a_passing_read_contract_does_not_make_any_new_broker_a_master():
    from app.services.brokers.pairs import SUPPORTED_PAIRS
    from app.services.brokers.registry import can_be_master
    assert can_be_master("alicebluenew") is True
    for broker in ("groww", "angelone", "zerodha"):
        assert can_be_master(broker) is False, broker
    assert set(SUPPORTED_PAIRS) == {("alicebluenew", "alicebluenew")}


# ── raw rows: one call, verbatim, explicit states (Master-switch preflight and crash recovery read this way) ────────

class RawFake:
    def __init__(self, result=..., raises=None, not_awaitable=False, no_raw=False):
        self.calls = 0
        self._result, self._raises = result, raises
        if no_raw:
            return
        if not_awaitable:
            def get_order_book_raw():
                self.calls += 1
                return "not awaitable"
        else:
            async def get_order_book_raw():
                self.calls += 1
                if self._raises:
                    raise self._raises
                return self._result
        self.get_order_book_raw = get_order_book_raw


@pytest.mark.asyncio
@pytest.mark.parametrize("client,state,reason", [
    (None, OrderBookState.UNKNOWN, R.NO_CLIENT),
    (RawFake(no_raw=True), OrderBookState.UNKNOWN, R.RAW_READ_NOT_SUPPORTED),
    (RawFake(result=None), OrderBookState.UNKNOWN, R.RAW_READ_UNAVAILABLE),
    (RawFake(not_awaitable=True), OrderBookState.UNKNOWN, R.RAW_READ_UNAVAILABLE),
    (RawFake(result={"not": "a list"}), OrderBookState.UNKNOWN, R.RAW_NOT_A_LIST),
    (RawFake(result="rows"), OrderBookState.UNKNOWN, R.RAW_NOT_A_LIST),
    (RawFake(raises=RuntimeError("boom")), OrderBookState.FAILED, R.READ_RAISED),
    (RawFake(raises=ConnectionError("down")), OrderBookState.FAILED, R.READ_RAISED),
    (RawFake(result=[]), OrderBookState.EMPTY, R.READ_OK),
    (RawFake(result=[{"brokerOrderId": "1"}]), OrderBookState.ORDERS, R.READ_OK),
])
async def test_the_raw_rows_read_states(client, state, reason):
    read = await read_order_book_rows(client)
    assert (read.state, read.reason) == (state, reason)
    assert read.is_known is (state in (OrderBookState.ORDERS, OrderBookState.EMPTY))
    if not read.is_known:
        assert read.rows == () and read.records == ()                 # an unknown read never carries rows
    if client is not None and hasattr(client, "calls"):
        assert client.calls <= 1                                       # one broker call at most, never a retry


@pytest.mark.asyncio
async def test_raw_rows_are_returned_verbatim_not_filtered_or_validated():
    rows = [{"brokerOrderId": "1"}, "not a dict", None, {"orderStatus": 5}]
    read = await read_order_book_rows(RawFake(result=rows))
    assert read.state is OrderBookState.ORDERS and list(read.rows) == rows


@pytest.mark.asyncio
async def test_a_raw_failure_names_only_the_exception_type():
    read = await read_order_book_rows(RawFake(raises=RuntimeError("Bearer SECRET-TOKEN apiSecret=xyz")))
    assert read.detail == "RuntimeError" and "SECRET" not in repr(read)


@pytest.mark.asyncio
async def test_an_empty_raw_list_is_a_proven_empty_book_and_none_is_not():
    assert (await read_order_book_rows(RawFake(result=[]))).state is OrderBookState.EMPTY
    assert (await read_order_book_rows(RawFake(result=None))).state is OrderBookState.UNKNOWN


# ── single-response checked read: parity with the two-call read, and one GET instead of two ─────────────────────

from test_alice_order_book_parser import _client as alice_client, row as alice_row  # noqa: E402

PAYLOADS = {
    "rows": dict(payload={"result": [alice_row(), alice_row(brokerOrderId="2", orderStatus="COMPLETE", filledQuantity=10)]}),
    "one_row": dict(payload={"result": [alice_row()]}),
    "empty_list": dict(payload={"result": []}),
    "ec916_empty_book": dict(payload={"status": "EC916", "message": "No orders found for this user.", "result": None}),
    "result_missing": dict(payload={"status": "Ok"}),
    "result_null_without_signal": dict(payload={"result": None}),
    "result_not_a_list": dict(payload={"result": {"brokerOrderId": "1"}}),
    "body_is_a_list": dict(payload=[alice_row()]),
    "http_500": dict(status=500, payload={"result": [alice_row()]}),
    "http_404": dict(status=404, payload={}),
    "network_error": dict(request_raises=httpx.ConnectError("down")),
    "non_json_body": dict(json_raises=True),
    "malformed_row_quantity": dict(payload={"result": [alice_row(), alice_row(brokerOrderId="2", quantity="abc")]}),
}


def _ids(read):
    return [r.broker_order_id for r in read.records]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(PAYLOADS))
async def test_the_single_read_reaches_the_same_verdict_as_the_two_call_checked_read(mock_redis, name):
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
    two_client, two_http = alice_client(mock_redis, **PAYLOADS[name])
    one_client, one_http = alice_client(mock_redis, **PAYLOADS[name])
    two = await read_order_book(two_client, checked=True)
    one = await read_order_book_single(one_client)
    assert (one.state, _ids(one)) == (two.state, _ids(two)), (name, one, two)
    assert one.is_known is two.is_known and one.checked is True
    # the proof of the reduction: never more broker GETs than before, and one instead of two whenever the book was readable
    assert one_http.request.await_count <= two_http.request.await_count
    if two_http.request.await_count == 2:
        assert one_http.request.await_count == 1, name


@pytest.mark.asyncio
async def test_a_readable_book_costs_one_get_instead_of_two(mock_redis):
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
    for payload in (PAYLOADS["rows"], PAYLOADS["empty_list"], PAYLOADS["ec916_empty_book"]):
        c2, h2 = alice_client(mock_redis, **payload)
        c1, h1 = alice_client(mock_redis, **payload)
        await read_order_book(c2, checked=True)
        await read_order_book_single(c1)
        assert (h2.request.await_count, h1.request.await_count) == (2, 1)


@pytest.mark.asyncio
async def test_the_single_read_rows_and_records_describe_the_same_response(mock_redis):
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
    client, http = alice_client(mock_redis, **PAYLOADS["rows"])
    read = await read_order_book_single(client)
    assert read.state is OrderBookState.ORDERS and http.request.await_count == 1
    assert [r["brokerOrderId"] for r in read.rows] == _ids(read) == ["100", "2"]


@pytest.mark.asyncio
async def test_a_parse_failure_is_failed_never_a_shortened_book(mock_redis):
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
    client, _ = alice_client(mock_redis, **PAYLOADS["malformed_row_quantity"])
    read = await read_order_book_single(client)
    assert read.state is OrderBookState.FAILED and read.reason == R.READ_RAISED and read.detail == "ValueError"
    assert read.records == () and read.rows == ()


@pytest.mark.asyncio
async def test_the_one_documented_divergence_non_dict_rows_are_dropped_by_the_raw_read(mock_redis):
    """`get_order_book_raw` has always dropped non-dict rows; the parsed read raises on them. The single read follows the raw read
    (so do the Master-switch preflight and crash recovery today). Alice Blue never sends such rows; pinned so a change is visible."""
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
    payload = dict(payload={"result": [alice_row(), "stray", None]})
    two = await read_order_book(alice_client(mock_redis, **payload)[0], checked=True)
    one = await read_order_book_single(alice_client(mock_redis, **payload)[0])
    assert two.state is OrderBookState.FAILED
    assert one.state is OrderBookState.ORDERS and _ids(one) == ["100"]


@pytest.mark.asyncio
async def test_a_client_without_the_raw_read_or_the_parser_is_read_as_before():
    plain = Fake(book=[rec("7")])                                      # raising convention: no raw read
    read = await read_order_book_single(plain)
    assert read.state is OrderBookState.ORDERS and plain.calls == ["book"]
    no_parser = Fake(book=[rec("1")], raw=[{"brokerOrderId": "1"}])   # raw read but no parse_order_book_rows
    read = await read_order_book_single(no_parser)
    assert read.state is OrderBookState.ORDERS and no_parser.calls == ["raw", "book"]      # the two-call path, unchanged
    assert (await read_order_book_single(None)).reason == R.NO_CLIENT


@pytest.mark.asyncio
async def test_a_bare_magicmock_client_still_takes_the_plain_path():
    client = MagicMock()
    client.get_order_book = AsyncMock(return_value=[rec("1")])
    read = await read_order_book_single(client)
    assert read.state is OrderBookState.ORDERS and [r.broker_order_id for r in read.records] == ["1"]


@pytest.mark.asyncio
async def test_a_raising_raw_probe_is_failed():
    client = Fake(raw_raises=RuntimeError("boom"), book=[rec("1")])
    client.parse_order_book_rows = lambda rows: []
    read = await read_order_book_single(client)
    assert read.state is OrderBookState.FAILED and read.detail == "RuntimeError"
