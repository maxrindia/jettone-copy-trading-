"""
Pins the Alice Blue A3 cancel/modify HTTP contract.

Production evidence (2026-09-09, four consecutive live cancels): the old
path POST /open-api/od/v1/orders/cancelorder is not a POST route on A3 and
answered every cancel with

    HTTP 400  "HTTP method POST is not supported by this URL"

which is why Master CANCEL never reached the child. The documented contract
is POST /open-api/od/v1/orders/cancel with {"brokerOrderId": ...}, and
POST /open-api/od/v1/orders/modify with brokerOrderId plus the modification
fields. modify_order had the same wrong path but had never executed in
production (the poller never dispatched ORDER_MODIFIED), so its failure was
latent rather than observed.

These tests assert the URL and the request body directly, so a silent
regression back to the non-routable path fails here rather than in a live
market test.

No live trading is enabled and no real broker calls are made anywhere in
this file - _request_with_reauth is always an AsyncMock and no socket is
ever opened.
"""
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.types import OrderResultCategory, OrderType


def _client(mock_redis):
    return AliceBlueClient(
        account_id="ce85cdc0-3eb4-4741-9341-881968963618",
        user_id="AB_TEST",
        api_secret="secret",
        redis=mock_redis,
        http_client=MagicMock(),
    )


def _response(status_code=200, payload=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"content-type": "application/json"}
    resp.json.return_value = payload if payload is not None else {"stat": "Ok"}
    resp.text = str(payload)
    return resp


# ── A. CANCEL ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_A1_cancel_uses_documented_endpoint(mock_redis):
    client = _client(mock_redis)
    client._request_with_reauth = AsyncMock(return_value=_response())

    await client.cancel_order(broker_order_id="26090900064746", symbol="YESBANK", exchange="NSE")

    client._request_with_reauth.assert_awaited_once()
    method, path = client._request_with_reauth.call_args.args
    assert method == "POST"
    assert path == "/open-api/od/v1/orders/cancel"
    assert "cancelorder" not in path


@pytest.mark.asyncio
async def test_A2_cancel_sends_broker_order_id_payload(mock_redis):
    client = _client(mock_redis)
    client._request_with_reauth = AsyncMock(return_value=_response())

    await client.cancel_order(broker_order_id="26090900064746", symbol="YESBANK", exchange="NSE")

    body = client._request_with_reauth.call_args.kwargs["json"]
    # Cancel works live as a bare object and must NOT be turned into an array.
    assert isinstance(body, dict)
    assert body == {"brokerOrderId": "26090900064746"}


@pytest.mark.asyncio
async def test_A3_cancel_success_is_reported_as_success(mock_redis):
    client = _client(mock_redis)
    client._request_with_reauth = AsyncMock(return_value=_response(200, {"stat": "Ok"}))

    res = await client.cancel_order(broker_order_id="26090900064746")

    assert res.category == OrderResultCategory.SUCCESS
    assert res.broker_order_id == "26090900064746"


@pytest.mark.asyncio
async def test_A4_cancel_http_failure_is_rejected_not_success(mock_redis):
    """The exact production failure must surface as REJECTED, carrying the
    broker's own text so 20354b0's alerting can quote it."""
    client = _client(mock_redis)
    failure = _response(400, None)
    failure.headers = {"content-type": "text/html"}
    failure.text = "<html><body>HTTP method POST is not supported by this URL</body></html>"
    client._request_with_reauth = AsyncMock(return_value=failure)

    res = await client.cancel_order(broker_order_id="26090900064746")

    assert res.category == OrderResultCategory.REJECTED
    assert res.category != OrderResultCategory.SUCCESS
    assert "not supported" in res.message


# ── B. MODIFY ─────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_B6_modify_uses_documented_endpoint(mock_redis):
    client = _client(mock_redis)
    client._request_with_reauth = AsyncMock(return_value=_response())

    await client.modify_order(
        broker_order_id="26090900064746",
        quantity=7,
        price=Decimal("22.60"),
        order_type=OrderType.LIMIT,
        symbol="YESBANK",
        exchange="NSE",
    )

    method, path = client._request_with_reauth.call_args.args
    assert method == "POST"
    assert path == "/open-api/od/v1/orders/modify"
    assert "modifyorder" not in path


@pytest.mark.asyncio
async def test_B7_modify_sends_broker_order_id_and_fields(mock_redis):
    client = _client(mock_redis)
    client._request_with_reauth = AsyncMock(return_value=_response())

    await client.modify_order(
        broker_order_id="26090900064746",
        quantity=7,
        price=Decimal("22.60"),
        order_type=OrderType.LIMIT,
        symbol="YESBANK",
        exchange="NSE",
    )

    body = client._request_with_reauth.call_args.kwargs["json"]
    # Documented A3 modify envelope: a single JSON OBJECT, not an array.
    # A JSON-array envelope was tried in production and produced HTTP 400
    # with an empty body (the server could not deserialize it), so this
    # pins the object shape against a repeat of that regression.
    assert isinstance(body, dict), "modify body must be a single JSON object"
    # EXPERIMENT 1 body: the four documented Required fields plus the three
    # unambiguous Optional fields whose values are proven by place_order.
    assert body == {
        "brokerOrderId": "26090900064746",
        "quantity": 7,
        "orderType": "LIMIT",
        "price": "22.60",
        "validity": "DAY",
        "disclosedQuantity": "0",
        "trailingSLAmount": "",
    }


@pytest.mark.asyncio
async def test_B7e_modify_excludes_the_fields_we_must_not_invent(mock_redis):
    """deviceId, marketProtection(Percent) and the SL/multi-leg fields are
    deliberately NOT sent: no sourceable value, an ambiguous name in A3's own
    docs, and wrong order type respectively."""
    client = _client(mock_redis)
    client._request_with_reauth = AsyncMock(return_value=_response())

    await client.modify_order(
        broker_order_id="26090900064746",
        quantity=7,
        price=Decimal("22.60"),
        order_type=OrderType.LIMIT,
    )

    body = client._request_with_reauth.call_args.kwargs["json"]
    for excluded in (
        "deviceId",
        "marketProtection",
        "marketProtectionPercent",
        "slLegPrice",
        "targetLegPrice",
        "slTriggerPrice",
    ):
        assert excluded not in body, f"{excluded} must not be sent on the LIMIT path"


@pytest.mark.asyncio
async def test_B7f_experiment1_values_match_place_order_conventions(mock_redis):
    """The three added values are lifted from the working place_order body,
    not invented: validity "DAY", disclosedQty "0", trailingStopLoss ""."""
    client = _client(mock_redis)
    client._request_with_reauth = AsyncMock(return_value=_response())

    await client.modify_order(
        broker_order_id="26090900064746", quantity=7, price=Decimal("22.60")
    )

    body = client._request_with_reauth.call_args.kwargs["json"]
    assert body["validity"] == "DAY"
    assert body["disclosedQuantity"] == "0"
    assert body["trailingSLAmount"] == ""


@pytest.mark.asyncio
async def test_B7b_modify_sends_no_legacy_aliases(mock_redis):
    """The pre-fix body carried eight non-documented aliases; none may
    reappear."""
    client = _client(mock_redis)
    client._request_with_reauth = AsyncMock(return_value=_response())

    await client.modify_order(
        broker_order_id="26090900064746",
        quantity=7,
        price=Decimal("22.60"),
        order_type=OrderType.LIMIT,
        symbol="YESBANK",
        exchange="NSE",
    )

    body = client._request_with_reauth.call_args.kwargs["json"]
    for legacy in (
        "orderNumber",
        "nestOrderNumber",
        "tradingSymbol",
        "exchange",
        "newQuantity",
        "newPrice",
        "triggerPrice",
        "priceType",
    ):
        assert legacy not in body, f"non-documented alias {legacy} still sent"


@pytest.mark.asyncio
async def test_B7c_modify_omits_optional_sl_trigger_for_plain_limit(mock_redis):
    """slTriggerPrice is conditional - it must not be sent when no trigger
    price is set, which is always the case for Jettone's LIMIT path."""
    client = _client(mock_redis)
    client._request_with_reauth = AsyncMock(return_value=_response())

    await client.modify_order(
        broker_order_id="26090900064746", quantity=7, price=Decimal("22.60")
    )

    assert "slTriggerPrice" not in client._request_with_reauth.call_args.kwargs["json"]


@pytest.mark.asyncio
async def test_B7d_modify_uses_documented_name_when_trigger_is_set(mock_redis):
    client = _client(mock_redis)
    client._request_with_reauth = AsyncMock(return_value=_response())

    await client.modify_order(
        broker_order_id="26090900064746",
        quantity=7,
        price=Decimal("22.60"),
        trigger_price=Decimal("22.10"),
    )

    body = client._request_with_reauth.call_args.kwargs["json"]
    assert body["slTriggerPrice"] == "22.10"
    assert "triggerPrice" not in body


@pytest.mark.asyncio
async def test_B_modify_failure_is_rejected_not_success(mock_redis):
    client = _client(mock_redis)
    failure = _response(400, None)
    failure.headers = {"content-type": "text/html"}
    failure.text = "<html><body>HTTP method POST is not supported by this URL</body></html>"
    client._request_with_reauth = AsyncMock(return_value=failure)

    res = await client.modify_order(
        broker_order_id="26090900064746", quantity=7, price=Decimal("22.60")
    )

    assert res.category == OrderResultCategory.REJECTED
    assert "not supported" in res.message
