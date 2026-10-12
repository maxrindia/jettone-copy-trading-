import pytest
import httpx
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.exceptions import AuthenticationError, IPNotWhitelistedError


@pytest.mark.asyncio
async def test_successful_a3_authentication_and_redis_cache(mock_redis):
    # Mock httpx response for A3 getUserDetails endpoint
    mock_http = AsyncMock(spec=httpx.AsyncClient)

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "stat": "Ok",
        "clientId": "AB00101",
        "userSession": "JWT_SESSION_TOKEN_XYZ_12345",
    }

    mock_http.post.return_value = resp

    client = AliceBlueClient(
        account_id="acc-test-1",
        user_id="AB00101",
        api_secret="secret_xyz",
        redis=mock_redis,
        http_client=mock_http,
    )

    token = await client.authenticate_with_auth_code("AUTH_CODE_VALID_123")
    assert token == "JWT_SESSION_TOKEN_XYZ_12345"

    # Verify Redis set was called with correct key
    assert mock_redis._storage.get("alice:session:acc-test-1") == "JWT_SESSION_TOKEN_XYZ_12345"

    # get_session should read directly from Redis
    cached_token = await client.get_session()
    assert cached_token == "JWT_SESSION_TOKEN_XYZ_12345"


@pytest.mark.asyncio
async def test_a3_ip_not_whitelisted_error(mock_redis):
    mock_http = AsyncMock(spec=httpx.AsyncClient)

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "stat": "Not_Ok",
        "emsg": "Client IP not whitelisted for API access",
    }

    mock_http.post.return_value = resp

    client = AliceBlueClient(
        account_id="acc-test-ip",
        user_id="AB00101",
        api_secret="secret_xyz",
        redis=mock_redis,
        http_client=mock_http,
    )

    with pytest.raises(IPNotWhitelistedError) as exc_info:
        await client.authenticate_with_auth_code("AUTH_CODE_TEST")

    assert "IP restriction" in str(exc_info.value) or "whitelisted" in str(exc_info.value)


@pytest.mark.asyncio
async def test_a3_invalid_auth_code_failure(mock_redis):
    mock_http = AsyncMock(spec=httpx.AsyncClient)

    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"stat": "Not_ok", "emsg": "Invalid auth code"}

    mock_http.post.return_value = resp

    client = AliceBlueClient(
        account_id="acc-test-bad",
        user_id="AB00101",
        api_secret="wrong_secret",
        redis=mock_redis,
        http_client=mock_http,
    )

    with pytest.raises(AuthenticationError) as exc_info:
        await client.authenticate_with_auth_code("EXPIRED_CODE")

    assert "Invalid auth code" in str(exc_info.value)


@pytest.mark.asyncio
async def test_a3_get_funds_success_and_headers(mock_redis):
    # Pre-set valid token in Redis
    await mock_redis.set("alice:session:acc-funds-1", "JWT_A3_FUNDS_TOKEN")

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "status": "Ok",
        "message": "Success",
        "result": [
            {
                "tradingLimit": 50000,
                "openingCashLimit": 50000.0,
                "collateralMargin": 15000.0,
                "utilizedMargin": 5000.0,
                "blockedForPayout": 0,
            }
        ],
    }

    called_url = []
    called_headers = []

    async def fake_request(method, url, **kwargs):
        called_url.append(url)
        called_headers.append(kwargs.get("headers", {}))
        return resp

    mock_http.request = AsyncMock(side_effect=fake_request)

    client = AliceBlueClient(
        account_id="acc-funds-1",
        user_id="AB00101",
        api_secret="secret_xyz",
        redis=mock_redis,
        http_client=mock_http,
    )

    funds = await client.get_funds(max_cache_age_sec=0)

    # 1. Verify URL is the official A3 limits endpoint
    assert called_url[0] == "https://a3.aliceblueonline.com/open-api/od/v1/limits"

    # 2. Verify Authorization header contains ONLY Bearer <userSession>
    assert called_headers[0]["Authorization"] == "Bearer JWT_A3_FUNDS_TOKEN"

    # 3. Verify financial field mapping
    assert funds.available_margin == 50000
    assert funds.collateral == 15000
    assert funds.used_margin == 5000

    # 4. Verify Redis caching
    cached = await mock_redis.get("alice:funds:acc-funds-1")
    assert cached is not None


@pytest.mark.asyncio
async def test_a3_get_order_book_success(mock_redis):
    await mock_redis.set("alice:session:acc-ob-1", "JWT_A3_OB_TOKEN")

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "status": "Ok",
        "message": "Success",
        "result": [
            {
                "clientId": "912444",
                "brokerOrderId": "25071400112229",
                "exchange": "NSE",
                "tradingSymbol": "ACC",
                "transactionType": "BUY",
                "quantity": 30,
                "product": "LONGTERM",
                "orderType": "LIMIT",
                "price": 1120.15,
                "averageTradedPrice": 1120.15,
                "filledQuantity": 30,
                "orderTag": "TAG_001",
                "orderStatus": "COMPLETE",
                "orderTime": "12:53:26 11-07-2026",
            }
        ],
    }

    called_url = []
    called_headers = []

    async def fake_request(method, url, **kwargs):
        called_url.append(url)
        called_headers.append(kwargs.get("headers", {}))
        return resp

    mock_http.request = AsyncMock(side_effect=fake_request)

    client = AliceBlueClient(
        account_id="acc-ob-1",
        user_id="AB00101",
        api_secret="secret_xyz",
        redis=mock_redis,
        http_client=mock_http,
    )

    orders = await client.get_order_book()

    # 1. Verify URL is the official A3 orders book endpoint
    assert called_url[0] == "https://a3.aliceblueonline.com/open-api/od/v1/orders/book"

    # 2. Verify Authorization header contains ONLY Bearer <userSession>
    assert called_headers[0]["Authorization"] == "Bearer JWT_A3_OB_TOKEN"

    # 3. Verify order record field mapping
    assert len(orders) == 1
    ord_rec = orders[0]
    assert ord_rec.broker_order_id == "25071400112229"
    assert ord_rec.client_order_id == "TAG_001"
    assert ord_rec.symbol == "ACC"
    assert ord_rec.exchange == "NSE"
    assert ord_rec.side.value == "BUY"
    assert ord_rec.quantity == 30
    assert ord_rec.filled_quantity == 30
    assert ord_rec.average_price == Decimal("1120.15")
    assert ord_rec.status == "COMPLETE"


@pytest.mark.asyncio
async def test_a3_get_positions_success(mock_redis):
    await mock_redis.set("alice:session:acc-pos-1", "JWT_A3_POS_TOKEN")

    mock_http = AsyncMock(spec=httpx.AsyncClient)
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "status": "Ok",
        "message": "Success",
        "result": [
            {
                "instrumentId": "14366",
                "tradingSymbol": "IDEA-EQ",
                "exchange": "NSE",
                "product": "CNC",
                "netQuantity": 50,
                "netAveragePrice": 7.69,
            },
            {
                "instrumentId": "2885",
                "tradingSymbol": "RELIANCE-EQ",
                "exchange": "NSE",
                "product": "MIS",
                "netQuantity": -20,
                "netAveragePrice": 2950.50,
            },
            {
                "instrumentId": "1594",
                "tradingSymbol": "INFY-EQ",
                "exchange": "NSE",
                "product": "MIS",
                "netQuantity": 0,  # Zero quantity must be filtered out
                "netAveragePrice": 1800.00,
            },
        ],
    }

    called_url = []
    called_headers = []

    async def fake_request(method, url, **kwargs):
        called_url.append(url)
        called_headers.append(kwargs.get("headers", {}))
        return resp

    mock_http.request = AsyncMock(side_effect=fake_request)

    client = AliceBlueClient(
        account_id="acc-pos-1",
        user_id="AB00101",
        api_secret="secret_xyz",
        redis=mock_redis,
        http_client=mock_http,
    )

    positions = await client.get_positions()

    # 1. Verify URL is the official A3 positions endpoint
    assert called_url[0] == "https://a3.aliceblueonline.com/open-api/od/v1/positions"

    # 2. Verify Authorization header contains ONLY Bearer <userSession>
    assert called_headers[0]["Authorization"] == "Bearer JWT_A3_POS_TOKEN"

    # 3. Verify zero quantity is filtered out (only 2 items remain)
    assert len(positions) == 2

    # 4. Verify Positive quantity -> BUY
    pos_long = positions[0]
    assert pos_long.symbol == "IDEA-EQ"
    assert pos_long.quantity == 50
    assert pos_long.side == "BUY"
    assert pos_long.average_price == Decimal("7.69")
    assert pos_long.exchange == "NSE"
    assert pos_long.product_type == "CNC"
    assert pos_long.account_id == "acc-pos-1"

    # 5. Verify Negative quantity -> SELL
    pos_short = positions[1]
    assert pos_short.symbol == "RELIANCE-EQ"
    assert pos_short.quantity == -20
    assert pos_short.side == "SELL"
    assert pos_short.average_price == Decimal("2950.50")
    assert pos_short.product_type == "MIS"



