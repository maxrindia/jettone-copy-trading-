"""
DEMONSTRATED, NOT FIXED (the Alice Blue adapter is out of scope without approval): two ways the Alice Blue
order-send path can re-send an order that the broker may already have accepted. These tests pin today's
behaviour with a mocked HTTP layer so the finding is reproducible; when the adapter is fixed they must be
flipped deliberately.

1. AliceBlueClient.place_order maps EVERY httpx.RequestError that is not a TimeoutException - including
   post-send failures such as ReadError / RemoteProtocolError - to NETWORK_ERROR, the one category
   RetryPolicy re-sends. (The Groww and Angel One adapters return NETWORK_ERROR only for errors that prove
   the request never left; see brokers/transport.py.)
2. An unparsable 200 body makes place_order raise (resp.json()), and RetryPolicy retries raised exceptions.
"""
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.types import OrderRequest, OrderResultCategory, OrderSide, OrderType, ProductType
from app.services.reliability.retry import RetryPolicy


def make(mock_redis, http):
    return AliceBlueClient(account_id="acc-test", user_id="AB00101", api_secret="secret", redis=mock_redis, http_client=http)


def req():
    return OrderRequest(symbol="NIFTY24AUG24500CE", exchange="NSE", side=OrderSide.BUY, quantity=50,
                        product_type=ProductType.MIS, order_type=OrderType.LIMIT, price=1, client_order_id="CID-FINDING",
                        instrument_token="2277")


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [httpx.ReadError("reset"), httpx.RemoteProtocolError("server disconnected"), httpx.WriteError("pipe")])
async def test_finding_post_send_transport_errors_are_reported_as_retryable_network_errors(mock_redis, exc):
    http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "TOKEN")
    http.request.side_effect = exc
    client = make(mock_redis, http)
    assert (await client.place_order(req())).category == OrderResultCategory.NETWORK_ERROR
    http.request.reset_mock()
    out = await RetryPolicy(max_attempts=3, base_delay_sec=0.0).execute(client.place_order, req())
    assert out.category == OrderResultCategory.NETWORK_ERROR
    assert http.request.await_count == 3                         # the order was transmitted up to three times


@pytest.mark.asyncio
async def test_finding_an_unparsable_200_body_raises_and_is_retried(mock_redis):
    http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "TOKEN")
    http.request.return_value = httpx.Response(200, content=b"<html>not json</html>", request=httpx.Request("POST", "http://x"))
    client = make(mock_redis, http)
    out = await RetryPolicy(max_attempts=3, base_delay_sec=0.0).execute(client.place_order, req())
    assert out.category == OrderResultCategory.UNKNOWN
    assert http.request.await_count == 3
