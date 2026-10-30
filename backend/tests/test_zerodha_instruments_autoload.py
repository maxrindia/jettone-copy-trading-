"""The Zerodha instrument store is filled (read-only) after a login and before an order when it is empty; a failure never guesses."""
from decimal import Decimal

import httpx
import pytest

from app.services.alice_blue.types import OrderRequest, OrderResultCategory, OrderSide, OrderType, ProductType
from app.services.zerodha.client import ZerodhaClient
from app.services.zerodha.instruments import ZerodhaInstrumentStore
from zerodha_helpers import CSV_HEADER, CSV_ROWS, FIXED_NOW, FakeKite, make_store, seed_session


class _Kite(FakeKite):
    def __init__(self, dump_status=200):
        super().__init__()
        self.dump_status = dump_status

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/instruments/"):
            self.requests.append(request)
            if self.dump_status != 200:
                return httpx.Response(self.dump_status, json={"status": "error", "message": "x", "error_type": "GeneralException"})
            exchange = request.url.path.rsplit("/", 1)[-1]
            rows = [r for r in CSV_ROWS if r.rsplit(",", 1)[-1] == exchange]          # each dump holds only its own exchange
            return httpx.Response(200, content="\n".join([CSV_HEADER] + rows).encode())
        return await super().__call__(request)


def _client(redis, fake, store):
    return ZerodhaClient(
        account_id="acc-zerodha", label="AB1234", api_key="KITE-KEY", api_secret="KITE-SECRET", redis=redis,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake)), clock=lambda: FIXED_NOW, instruments=store,
    )


def _req():
    return OrderRequest(symbol="SBIN-EQ", exchange="NSE", side=OrderSide.BUY, quantity=1, product_type=ProductType.CNC,
                        order_type=OrderType.LIMIT, price=Decimal("800"), client_order_id="jt-1")


@pytest.mark.asyncio
async def test_an_empty_store_is_filled_from_both_exchanges_before_an_order(mock_redis):
    fake, store = _Kite(), ZerodhaInstrumentStore()
    await seed_session(mock_redis)
    result = await _client(mock_redis, fake, store).place_order(_req())
    assert result.category is OrderResultCategory.SUCCESS and store.size > 0
    assert {r.url.path for r in fake.calls("/instruments/")} == {"/instruments/NSE", "/instruments/BSE"}
    assert all(r.method == "GET" for r in fake.calls("/instruments/"))


@pytest.mark.asyncio
async def test_a_loaded_store_is_not_fetched_again(mock_redis):
    fake = _Kite()
    await seed_session(mock_redis)
    await _client(mock_redis, fake, make_store()).place_order(_req())
    assert fake.calls("/instruments/") == []


@pytest.mark.asyncio
async def test_a_failed_dump_sends_nothing_and_rejects_cleanly(mock_redis):
    fake, store = _Kite(dump_status=500), ZerodhaInstrumentStore()
    await seed_session(mock_redis)
    result = await _client(mock_redis, fake, store).place_order(_req())
    assert result.category is OrderResultCategory.REJECTED and "not loaded" in result.message
    assert fake.calls("/orders/regular", "POST") == [] and store.size == 0
