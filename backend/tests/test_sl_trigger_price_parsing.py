"""
Confirmed production finding, 2026-09-29 (master order 26092900090102, ITC
SELL SL): Alice Blue's order-book response reports a native SL/SL-M order's
trigger price under the key "slTriggerPrice" - NOT "triggerPrice" or
"trgprc", the only two keys client.py's get_order_book() previously checked.
Live payload confirmed 'slTriggerPrice': 263.3 with no 'triggerPrice' key
present at all, so trigger_price_raw always evaluated to Decimal("0") for
every native SL/SL-M order, regardless of the separate TRIGGER_PENDING fix
(2026-09-29) - that fix correctly preserves whatever trigger_price it is
given; it was always being given zero.

This file covers ONLY that parsing gap. No replication, TRIGGER_PENDING,
SELL-safety, or MARKET/LIMIT behavior is touched or exercised beyond what's
needed to prove ordinary orders are unaffected.

No live trading and no real broker calls are made anywhere in this file -
httpx is a mocked AsyncClient and Redis is the in-memory mock_redis fixture.
"""
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.alice_blue.client import AliceBlueClient


def _client(mock_redis, mock_http):
    return AliceBlueClient(
        account_id="acc-test", user_id="AB00101", api_secret="secret",
        redis=mock_redis, http_client=mock_http,
    )


def _order_book_response(rows):
    resp = AsyncMock()
    resp.status_code = 200
    resp.json = lambda: {"result": rows}
    return resp


BASE_ROW = {
    "brokerOrderId": "26092900090102",
    "tradingSymbol": "ITC",
    "exchange": "NSE",
    "transactionType": "SELL",
    "quantity": 1,
    "filledQuantity": 0,
    "averageTradedPrice": 0,
    "orderStatus": "TRIGGER_PENDING",
    "product": "INTRADAY",
    "price": 263.2,
    "orderTime": "2026-09-29 11:16:39",
}


async def _fetch(mock_redis, row):
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
    client = _client(mock_redis, mock_http)
    mock_http.request = AsyncMock(return_value=_order_book_response([row]))
    orders = await client.get_order_book()
    assert len(orders) == 1
    return orders[0]


# 1. SL order with slTriggerPrice --------------------------------------------

@pytest.mark.asyncio
async def test_sl_order_parses_sl_trigger_price(mock_redis):
    """The exact confirmed live payload shape: orderType 'SL', a
    slTriggerPrice key, no triggerPrice key at all."""
    row = {**BASE_ROW, "orderType": "SL", "slTriggerPrice": 263.3}
    order = await _fetch(mock_redis, row)
    assert order.order_type == "SL"
    assert order.trigger_price == Decimal("263.3")


# 2. SL-M order with slTriggerPrice ------------------------------------------

@pytest.mark.asyncio
async def test_sl_m_order_parses_sl_trigger_price(mock_redis):
    row = {**BASE_ROW, "orderType": "SL-M", "price": 0.0, "slTriggerPrice": 264.0}
    order = await _fetch(mock_redis, row)
    assert order.order_type == "SLM"
    assert order.trigger_price == Decimal("264.0")


# 3. Existing triggerPrice field still works ---------------------------------

@pytest.mark.asyncio
async def test_existing_trigger_price_key_still_works(mock_redis):
    row = {**BASE_ROW, "orderType": "SL", "triggerPrice": 100.5}
    order = await _fetch(mock_redis, row)
    assert order.trigger_price == Decimal("100.5")


# 4. Existing trgprc field still works ---------------------------------------

@pytest.mark.asyncio
async def test_existing_trgprc_key_still_works(mock_redis):
    row = {**BASE_ROW, "orderType": "SL", "trgprc": 99.75}
    order = await _fetch(mock_redis, row)
    assert order.trigger_price == Decimal("99.75")


# Priority: slTriggerPrice wins over triggerPrice/trgprc if more than one is
# somehow present, since it is the confirmed real field for a native SL order.

@pytest.mark.asyncio
async def test_sl_trigger_price_takes_priority_over_legacy_keys(mock_redis):
    row = {**BASE_ROW, "orderType": "SL", "slTriggerPrice": 263.3, "triggerPrice": 0, "trgprc": 0}
    order = await _fetch(mock_redis, row)
    assert order.trigger_price == Decimal("263.3")


# 5. Missing trigger-price fields: unchanged default -------------------------

@pytest.mark.asyncio
async def test_missing_trigger_price_fields_default_to_zero(mock_redis):
    row = {**BASE_ROW, "orderType": "SL"}
    order = await _fetch(mock_redis, row)
    assert order.trigger_price == Decimal("0")


# 6. Normal MARKET/LIMIT order: no regression --------------------------------

@pytest.mark.asyncio
async def test_normal_limit_order_unaffected(mock_redis):
    row = {**BASE_ROW, "orderType": "LIMIT", "orderStatus": "COMPLETE", "filledQuantity": 1,
           "averageTradedPrice": 263.6}
    order = await _fetch(mock_redis, row)
    assert order.order_type == "LIMIT"
    assert order.trigger_price == Decimal("0")
    assert order.price == Decimal("263.2")


@pytest.mark.asyncio
async def test_normal_market_order_unaffected(mock_redis):
    row = {**BASE_ROW, "orderType": "MARKET", "orderStatus": "COMPLETE", "filledQuantity": 1,
           "price": 0.0, "averageTradedPrice": 264.45}
    order = await _fetch(mock_redis, row)
    assert order.order_type == "MARKET"
    assert order.trigger_price == Decimal("0")
