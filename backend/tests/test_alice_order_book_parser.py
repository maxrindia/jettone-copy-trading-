"""
Oracle tests for the extracted Alice Blue order-book parser (alice_blue/order_book.py).

`parse_order_book_rows` was moved, not rewritten, out of `AliceBlueClient.get_order_book`. This file pins that: the REFERENCE code below
is a verbatim copy of the old loop (commit c28860f, sliced mechanically from the source at extraction time), and every payload is
parsed by both. Where the old code raised, the new code must raise the same exception type; where it returned records they must be
equal field by field. The wrapper around the loop (HTTP status, JSON shape, a missing or non-list `result`) is pinned the same way
against a verbatim copy of the old wrapper. Nothing here touches a broker: the HTTP layer is a scripted fake.

Behaviours that look odd and are DELIBERATELY preserved (a "cleanup" would change what real traffic does):
* `get_order_book` returns [] for a failed read, a missing `result`, a non-list `result` - indistinguishable from an empty book; only
  `get_order_book_raw` can tell them apart (None / EC916 empty-book signal). That is why the checked read exists.
* a non-dict row, or a row with a non-numeric quantity / price, raises instead of being skipped;
* an unrecognised status is passed through upper-cased.
"""
import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.alice_blue.client import AliceBlueClient, order_book_row_id as client_row_id
from app.services.alice_blue.order_book import order_book_row_id as new_row_id, parse_order_book_rows
from app.services.alice_blue.types import OrderRecord, OrderSide
from pnl_live_fixtures import CHILD_ORDER_BOOK, MASTER_ORDER_BOOK

# ── REFERENCE: verbatim copy of the old implementation ─────────────────────────────────────────────────────────

def order_book_row_id(item: Dict[str, Any]) -> str:
    """The broker order id of one raw /orders/book row - the single
    definition shared by get_order_book() and the Master-switch preflight
    snapshot, so both always identify the same order the same way."""
    return str(item.get("brokerOrderId", item.get("Nstordno", item.get("orderId", ""))))


def reference_parse(raw_list):
    """VERBATIM copy of the loop in AliceBlueClient.get_order_book at commit c28860f (de-indented into a function)."""
    orders: List[OrderRecord] = []
    for item in raw_list:
        # Parse order timestamp if present (e.g. "12:53:26 11-07-2026")
        order_time_raw = item.get("orderTime")
        ts = datetime.now(timezone.utc)
        if order_time_raw:
            try:
                ts = datetime.strptime(str(order_time_raw).strip(), "%H:%M:%S %d-%m-%Y").replace(tzinfo=timezone.utc)
            except Exception:
                pass

        # Map transactionType / Trantype
        trans_type = str(item.get("transactionType", item.get("Trantype", ""))).upper()
        side = OrderSide.BUY if trans_type in ("BUY", "B") else OrderSide.SELL

        # Normalize orderStatus / Status
        raw_status = str(item.get("orderStatus", item.get("Status", "UNKNOWN"))).upper()
        status_map = {
            "COMPLETED": "COMPLETE",
            "COMPLETE": "COMPLETE",
            "OPEN": "OPEN",
            "PENDING": "OPEN",
            "REJECTED": "REJECTED",
            "CANCELLED": "CANCELLED",
            "CANCELED": "CANCELLED",
            "PARTIALLY_FILLED": "PARTIAL",
            "PARTIAL": "PARTIAL",
        }
        norm_status = status_map.get(raw_status, raw_status)

        order_type_raw = str(item.get("orderType", item.get("prctyp", "MARKET"))).upper()
        if order_type_raw in ("L", "LIMIT"):
            norm_otype = "LIMIT"
        elif order_type_raw in ("SL", "STOPLOSS"):
            norm_otype = "SL"
        elif order_type_raw in ("SL-M", "SLM"):
            norm_otype = "SLM"
        else:
            norm_otype = "MARKET"

        price_raw = Decimal(str(item.get("price", item.get("prc", "0")) or "0"))
        # Confirmed in production, 2026-09-29: a native SL order's order
        # book row reports its trigger price under "slTriggerPrice", not
        # "triggerPrice"/"trgprc" - those two keys are kept as fallbacks
        # for whichever (if any) response shape actually uses them.
        trigger_price_raw = Decimal(str(
            item.get("slTriggerPrice", item.get("triggerPrice", item.get("trgprc", "0"))) or "0"
        ))
        rej_reason = item.get("rejectionReason") or item.get("rejreason") or None

        # Alice Blue's order book carries its own numeric instrument
        # identity directly (e.g. "74056") even for NFO options whose
        # tradingSymbol is reported as just the bare underlying name
        # (e.g. "NIFTY" - confirmed live: formattedInstrumentName
        # "NIFTY 29th SEP 23900 CE" alongside tradingSymbol "NIFTY").
        # Capturing it here lets callers skip the ambiguous
        # symbol-based NFO resolution entirely instead of re-guessing
        # which of many same-named contracts was actually traded.
        instrument_id_raw = item.get("instrumentId", item.get("Token", item.get("token")))
        instrument_id = str(instrument_id_raw).strip() if instrument_id_raw not in (None, "") else None
        # The exchange only when the row actually reported one: `exchange` below falls back to "NSE", which must
        # not be paired with a token as if it were evidence.
        instrument_exchange_raw = item.get("exchange", item.get("Exchange"))
        instrument_exchange = (
            str(instrument_exchange_raw).strip() if instrument_exchange_raw not in (None, "") else None
        ) or None

        orders.append(
            OrderRecord(
                broker_order_id=order_book_row_id(item),
                client_order_id=item.get("orderTag") or None,
                symbol=str(item.get("tradingSymbol", item.get("Trsym", item.get("symbol", "")))),
                exchange=str(item.get("exchange", item.get("Exchange", "NSE"))),
                side=side,
                quantity=int(item.get("quantity", item.get("Qty", 0))),
                filled_quantity=int(item.get("filledQuantity", item.get("Fillshares", 0))),
                average_price=Decimal(str(item.get("averageTradedPrice", item.get("Avgprc", "0")))),
                status=norm_status,
                order_timestamp=ts,
                order_type=norm_otype,
                product_type=str(item.get("product", item.get("pCode", "MIS"))),
                price=price_raw,
                trigger_price=trigger_price_raw,
                rejection_reason=rej_reason,
                instrument_id=instrument_id,
                instrument_exchange=instrument_exchange,
            )
        )

    return orders


# ── the old wrapper around the loop, verbatim (AliceBlueClient.get_order_book at c28860f) ────────────────────────

def reference_wrapper(status_code, payload):
    if status_code != 200:
        return []
    data = payload
    raw_list = data.get("result", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
    if not isinstance(raw_list, list):
        raw_list = []
    return reference_parse(raw_list)


def row(**over):
    base = {"brokerOrderId": "100", "tradingSymbol": "SBIN-EQ", "exchange": "NSE", "transactionType": "BUY", "quantity": 10,
            "filledQuantity": 0, "averageTradedPrice": 0, "orderStatus": "OPEN", "product": "INTRADAY", "orderType": "LIMIT",
            "price": 500.5, "orderTime": "10:15:30 27-10-2026", "orderTag": "JT_1"}
    base.update(over)
    return {k: v for k, v in base.items() if v is not ...}              # `...` removes the key


def alt(**over):
    """The legacy (Noren-style) key variants the parser also accepts."""
    base = {"Nstordno": "200", "Trsym": "INFY-EQ", "Exchange": "NSE", "Trantype": "S", "Qty": 5, "Fillshares": 2,
            "Avgprc": "1500.25", "Status": "complete", "pCode": "NRML", "prctyp": "SL", "prc": "1499.5", "trgprc": "1499.0",
            "rejreason": "RMS:Blocked", "Token": "1594"}
    base.update(over)
    return {k: v for k, v in base.items() if v is not ...}


MATRIX = {
    "live_master_book_2026_09_11": list(MASTER_ORDER_BOOK),
    "live_child_book_2026_09_11": list(CHILD_ORDER_BOOK),
    "empty": [],
    "one_open_limit": [row()],
    "legacy_keys": [alt()],
    "mixed_keys": [row(), alt(), row(brokerOrderId="101", transactionType="SELL")],
    "side_B_and_S_and_garbage": [row(transactionType="B"), row(brokerOrderId="2", transactionType="S"),
                                 row(brokerOrderId="3", transactionType="what"), row(brokerOrderId="4", transactionType=...)],
    "every_mapped_status": [row(brokerOrderId=str(i), orderStatus=s) for i, s in enumerate(
        ["COMPLETED", "complete", "OPEN", "pending", "REJECTED", "Cancelled", "CANCELED", "PARTIALLY_FILLED", "PARTIAL"])],
    "unmapped_and_trigger_pending_pass_through": [row(brokerOrderId="1", orderStatus="TRIGGER_PENDING"),
                                                  row(brokerOrderId="2", orderStatus="weird"), row(brokerOrderId="3", orderStatus=...)],
    "order_types": [row(brokerOrderId=str(i), orderType=t) for i, t in enumerate(
        ["L", "LIMIT", "SL", "STOPLOSS", "SL-M", "SLM", "MARKET", "anything", ...])],
    "trigger_price_key_precedence": [row(slTriggerPrice="99", triggerPrice="98", trgprc="97"),
                                     row(brokerOrderId="2", triggerPrice="98", trgprc="97"), row(brokerOrderId="3", trgprc="97"),
                                     row(brokerOrderId="4", slTriggerPrice="")],
    "price_blank_or_none": [row(price=""), row(brokerOrderId="2", price=None), row(brokerOrderId="3", price=...)],
    "instrument_identity_variants": [row(instrumentId="74056", exchange="NFO"), row(brokerOrderId="2", instrumentId="  3045  "),
                                     row(brokerOrderId="3", instrumentId=""), row(brokerOrderId="4", instrumentId=None, token="9"),
                                     row(brokerOrderId="5", exchange=""), row(brokerOrderId="6", exchange=...)],
    "rejection_reason_keys": [row(rejectionReason="RMS:Margin"), row(brokerOrderId="2", rejreason="x"), row(brokerOrderId="3")],
    "numeric_strings": [row(quantity="12", filledQuantity="3", averageTradedPrice="101.10")],
    "missing_optional_fields": [{"brokerOrderId": "9"}, {}],
    "order_id_fallbacks": [{"orderId": "77"}, {"Nstordno": "78", "brokerOrderId": ...}],
}
RAISING = {
    "non_dict_row": [row(), "not a dict"],
    "none_row": [None],
    "list_row": [[1, 2]],
    "non_numeric_quantity": [row(quantity="abc")],
    "none_quantity": [row(quantity=None)],
    "non_numeric_price": [row(price="12,5")],
    "non_numeric_average": [row(averageTradedPrice="n/a")],
    "float_string_quantity": [row(quantity="1.5")],
}
TIMESTAMPS = {
    "valid_noren_style": "12:53:26 11-07-2026",
    "invalid_format": "2026-10-27 10:00:00",
    "garbage": "yesterday",
    "empty": "",
    "none": None,
}


def _same(a: OrderRecord, b: OrderRecord, *, check_now: bool):
    da, db = dict(vars(a)), dict(vars(b))
    ta, tb = da.pop("order_timestamp"), db.pop("order_timestamp")
    assert da == db, (da, db)
    if check_now:                                                       # both fell back to "now": two calls, same instant +- noise
        assert abs(ta - tb) < timedelta(seconds=5) and abs(ta - datetime.now(timezone.utc)) < timedelta(seconds=30)
    else:
        assert ta == tb


def _has_parseable_time(r):
    t = r.get("orderTime") if isinstance(r, dict) else None
    if not t:
        return False
    try:
        datetime.strptime(str(t).strip(), "%H:%M:%S %d-%m-%Y")
        return True
    except Exception:
        return False


@pytest.mark.parametrize("name", sorted(MATRIX))
def test_the_extracted_parser_equals_the_old_loop_field_by_field(name):
    rows = MATRIX[name]
    old, new = reference_parse(copy.deepcopy(rows)), parse_order_book_rows(copy.deepcopy(rows))
    assert len(old) == len(new) == len(rows)
    for r, o, n in zip(rows, old, new):
        _same(o, n, check_now=not _has_parseable_time(r))


@pytest.mark.parametrize("name", sorted(RAISING))
def test_the_extracted_parser_raises_exactly_when_the_old_loop_raised(name):
    rows = RAISING[name]
    with pytest.raises(Exception) as old:
        reference_parse(copy.deepcopy(rows))
    with pytest.raises(Exception) as new:
        parse_order_book_rows(copy.deepcopy(rows))
    assert type(new.value) is type(old.value), (name, old.value, new.value)


@pytest.mark.parametrize("name", sorted(TIMESTAMPS))
def test_order_time_handling_is_unchanged(name):
    rows = [row(orderTime=TIMESTAMPS[name])]
    old, new = reference_parse(copy.deepcopy(rows))[0], parse_order_book_rows(copy.deepcopy(rows))[0]
    _same(old, new, check_now=name != "valid_noren_style")
    if name == "valid_noren_style":
        assert new.order_timestamp == datetime(2026, 7, 11, 12, 53, 26, tzinfo=timezone.utc)


def test_live_captured_rows_parse_to_the_expected_identities():
    ids = [r.broker_order_id for r in parse_order_book_rows(copy.deepcopy(MASTER_ORDER_BOOK))]
    assert ids == [order_book_row_id(r) for r in MASTER_ORDER_BOOK] and all(ids)


def test_a_parse_failure_is_never_a_silently_shortened_book():
    with pytest.raises(Exception):
        parse_order_book_rows([row(), row(brokerOrderId="2", quantity="abc"), row(brokerOrderId="3")])


def test_the_row_id_helper_is_one_definition_with_the_old_precedence():
    assert client_row_id is new_row_id                                   # the client re-exports the moved helper, not a copy
    cases = [{"brokerOrderId": "A", "Nstordno": "B", "orderId": "C"}, {"Nstordno": "B", "orderId": "C"}, {"orderId": "C"}, {},
             {"brokerOrderId": None}, {"brokerOrderId": 123}, {"brokerOrderId": ""}]
    for case in cases:
        assert new_row_id(case) == order_book_row_id(case), case          # `order_book_row_id` here is the verbatim OLD helper
    assert new_row_id({"brokerOrderId": "A", "Nstordno": "B", "orderId": "C"}) == "A"
    assert new_row_id({"Nstordno": "B", "orderId": "C"}) == "B"
    assert new_row_id({"brokerOrderId": None}) == "None"                  # str(None): the old behaviour, kept


# ── the wrapper: get_order_book on the real client over a scripted HTTP layer ───────────────────────────────────

def _client(mock_redis, *, status=200, payload=None, json_raises=False, request_raises=None):
    http = AsyncMock(spec=httpx.AsyncClient)
    resp = AsyncMock()
    resp.status_code = status
    if json_raises:
        def boom():
            raise ValueError("not json")
        resp.json = boom
    else:
        resp.json = lambda: payload
    http.request = AsyncMock(side_effect=request_raises) if request_raises else AsyncMock(return_value=resp)
    return AliceBlueClient(account_id="acc-test", user_id="AB00101", api_secret="secret", redis=mock_redis, http_client=http), http


WRAPPER = {
    "result_rows": (200, {"result": [row(), row(brokerOrderId="2")]}),
    "result_empty_list": (200, {"result": []}),
    "result_missing": (200, {"status": "Ok"}),
    "result_null": (200, {"result": None}),
    "result_not_a_list_dict": (200, {"result": {"brokerOrderId": "1"}}),
    "result_not_a_list_string": (200, {"result": "oops"}),
    "body_is_a_list": (200, [row(), row(brokerOrderId="2")]),
    "body_is_an_empty_list": (200, []),
    "body_is_a_string": (200, "text"),
    "body_is_none": (200, None),
    "ec916_empty_book": (200, {"status": "EC916", "message": "No orders found for this user.", "result": None}),
    "http_500": (500, {"result": [row()]}),
    "http_404": (404, {}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(WRAPPER))
async def test_get_order_book_returns_what_the_old_wrapper_returned(mock_redis, name):
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
    status, payload = WRAPPER[name]
    client, http = _client(mock_redis, status=status, payload=copy.deepcopy(payload))
    new = await client.get_order_book()
    old = reference_wrapper(status, copy.deepcopy(payload))
    assert len(new) == len(old)
    for o, n in zip(old, new):
        _same(o, n, check_now=False)
    assert http.request.await_count == 1                                   # one GET, as before


@pytest.mark.asyncio
async def test_get_order_book_still_returns_empty_on_a_network_error_and_raises_on_a_non_json_body(mock_redis):
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
    client, _ = _client(mock_redis, request_raises=httpx.ConnectError("down"))
    assert await client.get_order_book() == []                              # the failure that looks like an empty book
    client, _ = _client(mock_redis, json_raises=True)
    with pytest.raises(ValueError):
        await client.get_order_book()


@pytest.mark.asyncio
async def test_the_parsed_and_the_raw_read_still_disagree_where_they_always_did(mock_redis):
    """Not a bug to fix here: this is WHY the raw read and the checked read exist."""
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
    client, _ = _client(mock_redis, payload={"status": "Ok"})
    assert await client.get_order_book() == []                              # parsed: missing result == empty book
    client, _ = _client(mock_redis, payload={"status": "Ok"})
    assert await client.get_order_book_raw() is None                        # raw: not proven, so None
    client, _ = _client(mock_redis, payload={"status": "EC916", "message": "No orders found for this user.", "result": None})
    assert await client.get_order_book_raw() == []                          # raw: the documented empty-book answer
