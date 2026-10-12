import uuid
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock
from decimal import Decimal

from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.types import (
    OrderRequest,
    OrderResultCategory,
    OrderSide,
    OrderType,
    ProductType,
)


@pytest.mark.asyncio
async def test_a3_order_placement_success(mock_redis):
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = [
        {
            "status": "Ok",
            "message": "Success",
            "result": [
                {
                    "requestTime": "12:53:26 11-07-2026",
                    "brokerOrderId": "24082000012345",
                    "orderNo": "24082000012345",
                    "status": "OK",
                }
            ],
            "infoMessage": None,
        }
    ]

    called_url = []
    called_headers = []
    called_payload = []

    async def fake_request(method, url, **kwargs):
        called_url.append(url)
        called_headers.append(kwargs.get("headers", {}))
        called_payload.append(kwargs.get("json", []))
        return resp

    mock_http.request = AsyncMock(side_effect=fake_request)

    client = AliceBlueClient(
        account_id="acc-test",
        user_id="AB00101",
        api_secret="secret",
        redis=mock_redis,
        http_client=mock_http,
    )

    req = OrderRequest(
        symbol="NIFTY24AUG24500CE",
        exchange="NSE",
        side=OrderSide.BUY,
        quantity=50,
        product_type=ProductType.MIS,
        order_type=OrderType.LIMIT,
        price=Decimal("100.50"),
        client_order_id="CLIENT_ID_001",
        instrument_token="2277",
    )

    result = await client.place_order(req)

    # 1. Verify A3 URL and Bearer header
    assert called_url[0] == "https://a3.aliceblueonline.com/open-api/od/v1/orders/placeorder"
    assert called_headers[0]["Authorization"] == "Bearer A3_SESSION_JWT_TOKEN"

    # 2. Verify payload array structure and field mappings
    assert isinstance(called_payload[0], list)
    p0 = called_payload[0][0]
    assert p0["exchange"] == "NSE"
    assert p0["tradingSymbol"] == "NIFTY24AUG24500CE"
    assert p0["token"] == "2277"
    assert p0["instrumentId"] == "2277"
    assert p0["instrument_id"] == "2277"
    assert p0["instrumentToken"] == "2277"
    assert p0["qty"] == "50"
    assert p0["quantity"] == "50"
    assert p0["orderQuantity"] == "50"
    assert p0["price"] == "100.50"
    assert p0["priceType"] in ("L", "LIMIT")
    assert p0["orderType"] in ("Regular", "LIMIT")
    assert p0["transactionType"] == "BUY"
    assert p0["product"] == "INTRADAY"
    assert p0["ret"] == "DAY"
    assert p0["validity"] == "DAY"
    # "slTriggerPrice" is the confirmed broker field (matches the live
    # order-book response and modify_order's own documented contract) -
    # "triggerPrice" is not a field Alice Blue's A3 API recognizes.
    assert p0["slTriggerPrice"] == "0"
    assert "triggerPrice" not in p0

    # 3. Verify OrderResult category and broker order ID
    assert result.category == OrderResultCategory.SUCCESS
    assert result.broker_order_id == "24082000012345"
    assert result.client_order_id == "CLIENT_ID_001"


@pytest.mark.asyncio
async def test_a3_order_placement_sends_sl_trigger_price_for_native_sl(mock_redis):
    """Item 16 of the immediate-SL-replication feature: a native SL/SL-M
    order's trigger price must go out under "slTriggerPrice" - the
    confirmed broker field - never the old "triggerPrice" alias."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = [{
        "status": "Ok", "message": "Success",
        "result": [{"brokerOrderId": "26092900109500", "status": "OK"}],
    }]

    called_payload = []

    async def fake_request(method, url, **kwargs):
        called_payload.append(kwargs.get("json", []))
        return resp

    mock_http.request = AsyncMock(side_effect=fake_request)
    client = AliceBlueClient(
        account_id="acc-test", user_id="AB00101", api_secret="secret",
        redis=mock_redis, http_client=mock_http,
    )

    for order_type, expected_price_type in ((OrderType.SL, "SL"), (OrderType.SL_M, "SL-M")):
        called_payload.clear()
        req = OrderRequest(
            symbol="ITC", exchange="NSE", side=OrderSide.SELL, quantity=1,
            product_type=ProductType.MIS, order_type=order_type,
            price=Decimal("391.50"), trigger_price=Decimal("391.90"),
            client_order_id="CLIENT_SL_1", instrument_token="1660",
        )
        result = await client.place_order(req)

        p0 = called_payload[0][0]
        assert p0["priceType"] == expected_price_type
        assert p0["slTriggerPrice"] == "391.90"
        assert "triggerPrice" not in p0
        assert result.category == OrderResultCategory.SUCCESS


@pytest.mark.asyncio
async def test_a3_order_placement_validation_errors(mock_redis):
    client = AliceBlueClient(
        account_id="acc-test",
        user_id="AB00101",
        api_secret="secret",
        redis=mock_redis,
    )

    # Test invalid quantity
    req_qty = OrderRequest(
        symbol="RELIANCE",
        exchange="NSE",
        side=OrderSide.BUY,
        quantity=0,
        product_type=ProductType.MIS,
        order_type=OrderType.MARKET,
        client_order_id="CLIENT_ERR_01",
    )
    res_qty = await client.place_order(req_qty)
    assert res_qty.category == OrderResultCategory.REJECTED
    assert "quantity" in res_qty.message.lower()

    # Test missing limit price on LIMIT order
    req_limit = OrderRequest(
        symbol="RELIANCE",
        exchange="NSE",
        side=OrderSide.BUY,
        quantity=1,
        product_type=ProductType.MIS,
        order_type=OrderType.LIMIT,
        price=Decimal("0"),
        client_order_id="CLIENT_ERR_02",
    )
    res_limit = await client.place_order(req_limit)
    assert res_limit.category == OrderResultCategory.REJECTED
    assert "limit price" in res_limit.message.lower()


@pytest.mark.asyncio
async def test_a3_order_placement_rejected_with_ec_code(mock_redis):
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = [
        {
            "status": "Ok",
            "message": "Success",
            "result": [
                {
                    "status": "EC930",
                    "message": "Invalid parameter: 'orderType' should be one of the following values: {'LIMIT', 'MARKET', 'SL', 'SLM'}.",
                    "brokerOrderId": "",
                    "requestTime": "02-Sep-2026 11:43:20",
                }
            ],
            "infoMessage": None,
        }
    ]
    mock_http.request.return_value = resp

    client = AliceBlueClient(
        account_id="acc-test",
        user_id="AB00101",
        api_secret="secret",
        redis=mock_redis,
        http_client=mock_http,
    )

    req = OrderRequest(
        symbol="RELIANCE",
        exchange="NSE",
        side=OrderSide.BUY,
        quantity=1,
        product_type=ProductType.MIS,
        order_type=OrderType.MARKET,
        client_order_id="CLIENT_ID_002",
    )

    result = await client.place_order(req)
    assert result.category == OrderResultCategory.REJECTED
    assert "Invalid parameter" in result.message


@pytest.mark.asyncio
async def test_a3_order_placement_timeout(mock_redis):
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")

    mock_http.request.side_effect = httpx.TimeoutException("Read timed out")

    client = AliceBlueClient(
        account_id="acc-test",
        user_id="AB00101",
        api_secret="secret",
        redis=mock_redis,
        http_client=mock_http,
    )

    req = OrderRequest(
        symbol="NIFTY24AUG24500CE",
        exchange="NSE",
        side=OrderSide.BUY,
        quantity=50,
        product_type=ProductType.MIS,
        order_type=OrderType.MARKET,
        client_order_id="CLIENT_ID_003",
        # This test is about HTTP timeout handling, not instrument
        # resolution - an explicit token (matching the pattern already used
        # above) skips resolve_instrument_info entirely so the request
        # actually reaches the mocked HTTP layer instead of failing closed
        # first (see InstrumentResolutionError / test_ec941_instrument_id_error_parsing).
        instrument_token="2277",
    )

    result = await client.place_order(req)
    assert result.category == OrderResultCategory.TIMEOUT
    assert result.broker_order_id is None


@pytest.mark.asyncio
async def test_a3_order_placement_network_error(mock_redis):
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")

    mock_http.request.side_effect = httpx.ConnectError("Connection refused")

    client = AliceBlueClient(
        account_id="acc-test",
        user_id="AB00101",
        api_secret="secret",
        redis=mock_redis,
        http_client=mock_http,
    )

    req = OrderRequest(
        symbol="NIFTY24AUG24500CE",
        exchange="NSE",
        side=OrderSide.SELL,
        quantity=50,
        product_type=ProductType.MIS,
        order_type=OrderType.MARKET,
        client_order_id="CLIENT_ID_004",
        # Network-error handling test, not instrument resolution - see the
        # identical note on test_a3_order_placement_timeout above.
        instrument_token="2277",
    )

    result = await client.place_order(req)
    assert result.category == OrderResultCategory.NETWORK_ERROR
    assert result.broker_order_id is None


@pytest.mark.asyncio
async def test_ec965_market_orders_not_allowed(mock_redis):
    """TEST A: Unsupported MARKET order with EC965 returns REJECTED and clean message."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = [
        {
            "status": "Ok",
            "message": "Success",
            "result": [
                {
                    "status": "EC965",
                    "message": "Market orders are not allowed.",
                    "brokerOrderId": "",
                    "requestTime": "02-Sep-2026 12:30:00",
                }
            ],
            "infoMessage": None,
        }
    ]
    mock_http.request.return_value = resp

    client = AliceBlueClient(
        account_id="acc-test",
        user_id="AB00101",
        api_secret="secret",
        redis=mock_redis,
        http_client=mock_http,
    )

    req = OrderRequest(
        symbol="RELIANCE",
        exchange="NSE",
        side=OrderSide.BUY,
        quantity=1,
        product_type=ProductType.MIS,
        order_type=OrderType.MARKET,
        client_order_id="CLIENT_EC965",
    )

    result = await client.place_order(req)
    assert result.category == OrderResultCategory.REJECTED
    assert "Market orders are not allowed" in result.message
    assert result.broker_order_id is None


@pytest.mark.asyncio
async def test_valid_limit_order_placement(mock_redis):
    """TEST B: Valid LIMIT order with price sends correct instrumentId, orderType='LIMIT', price='2950.00'."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = [
        {
            "status": "Ok",
            "message": "Success",
            "result": [
                {
                    "brokerOrderId": "24082000099999",
                    "orderNo": "24082000099999",
                    "status": "OK",
                }
            ],
        }
    ]

    called_payload = []
    async def fake_request(method, url, **kwargs):
        called_payload.append(kwargs.get("json", []))
        return resp

    mock_http.request = AsyncMock(side_effect=fake_request)

    client = AliceBlueClient(
        account_id="acc-test",
        user_id="AB00101",
        api_secret="secret",
        redis=mock_redis,
        http_client=mock_http,
    )

    req = OrderRequest(
        symbol="RELIANCE",
        exchange="NSE",
        side=OrderSide.BUY,
        quantity=1,
        product_type=ProductType.MIS,
        order_type=OrderType.LIMIT,
        price=Decimal("2950"),
        client_order_id="CLIENT_LIMIT_01",
    )

    result = await client.place_order(req)
    assert result.category == OrderResultCategory.SUCCESS
    assert result.broker_order_id == "24082000099999"

    p0 = called_payload[0][0]
    assert p0["instrumentId"] == "2885"
    assert p0["quantity"] == "1"
    assert p0["priceType"] in ("L", "LIMIT")
    assert p0["orderType"] in ("Regular", "LIMIT")
    assert p0["price"] == "2950.00"


def test_manual_order_request_validation():
    """TEST C, D, E: ManualOrderRequest validation for LIMIT orders requiring price > 0."""
    from pydantic import ValidationError
    from app.api.broker import ManualOrderRequest

    # Valid LIMIT order with price
    req_valid = ManualOrderRequest(
        strategy_id=uuid.uuid4(),
        symbol="RELIANCE",
        side=OrderSide.BUY,
        quantity=1,
        order_type=OrderType.LIMIT,
        price=Decimal("2950"),
    )
    assert req_valid.price == Decimal("2950")

    # TEST C: LIMIT with missing/zero price
    with pytest.raises(ValidationError) as exc_info_zero:
        ManualOrderRequest(
            strategy_id=uuid.uuid4(),
            symbol="RELIANCE",
            side=OrderSide.BUY,
            quantity=1,
            order_type=OrderType.LIMIT,
            price=Decimal("0"),
        )
    assert "greater than 0" in str(exc_info_zero.value)

    # TEST D & E: LIMIT with negative price
    with pytest.raises(ValidationError) as exc_info_neg:
        ManualOrderRequest(
            strategy_id=uuid.uuid4(),
            symbol="RELIANCE",
            side=OrderSide.BUY,
            quantity=1,
            order_type=OrderType.LIMIT,
            price=Decimal("-100"),
        )
    assert "Input should be greater than or equal to 0" in str(exc_info_neg.value) or "greater than 0" in str(exc_info_neg.value)


@pytest.mark.asyncio
async def test_ec941_instrument_id_error_parsing(mock_redis):
    """TEST F: HTTP 200 + EC941 returns REJECTED and no valid broker order ID."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = [
        {
            "status": "Ok",
            "message": "Success",
            "result": [
                {
                    "status": "EC941",
                    "message": "Invalid parameter: 'instrumentId' cannot be empty or null.",
                    "brokerOrderId": "",
                }
            ],
        }
    ]
    mock_http.request.return_value = resp

    client = AliceBlueClient(
        account_id="acc-test",
        user_id="AB00101",
        api_secret="secret",
        redis=mock_redis,
        http_client=mock_http,
    )

    req = OrderRequest(
        symbol="UNKNOWN_SYM",
        exchange="NSE",
        side=OrderSide.BUY,
        quantity=1,
        product_type=ProductType.MIS,
        order_type=OrderType.MARKET,
        client_order_id="CLIENT_EC941",
        # This test is specifically about parsing a BROKER-side EC941
        # instrumentId rejection, not Jettone's own instrument resolution -
        # an explicit token lets the (deliberately unresolvable) symbol
        # still reach the mocked broker response below, instead of being
        # caught earlier by InstrumentResolutionError (the exact production
        # bug this whole fix closes: an unresolvable symbol no longer
        # reaches Alice Blue at all - see
        # test_instrument_resolution_fails_closed_for_unknown_symbol for
        # that behavior's own dedicated test).
        instrument_token="9999",
    )

    result = await client.place_order(req)
    assert result.category == OrderResultCategory.REJECTED
    assert result.broker_order_id is None
    assert "instrumentId" in result.message


@pytest.mark.asyncio
async def test_market_order_is_never_silently_converted(mock_redis):
    """TEST H: MARKET order stays MARKET/MKT in outgoing payload."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = [{"status": "Ok", "result": [{"brokerOrderId": "888", "status": "OK"}]}]

    called_payload = []
    async def fake_request(method, url, **kwargs):
        called_payload.append(kwargs.get("json", []))
        return resp
    mock_http.request = AsyncMock(side_effect=fake_request)

    client = AliceBlueClient(
        account_id="acc-test",
        user_id="AB00101",
        api_secret="secret",
        redis=mock_redis,
        http_client=mock_http,
    )

    req = OrderRequest(
        symbol="RELIANCE",
        exchange="NSE",
        side=OrderSide.BUY,
        quantity=1,
        product_type=ProductType.MIS,
        order_type=OrderType.MARKET,
    )

    await client.place_order(req)
    p0 = called_payload[0][0]
    assert p0["priceType"] == "MKT"
    assert p0["orderType"] == "MARKET"
    assert p0["transactionType"] == "BUY"
    assert p0["tradingSymbol"] == "RELIANCE-EQ"
    assert p0["instrumentId"] == "2885"
    assert p0["price"] == "0"


