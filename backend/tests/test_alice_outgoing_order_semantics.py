"""
Golden pins of what Jettone sends to Alice Blue, written BEFORE any broker-neutral refactor so that a later stage
cannot change an outgoing order unnoticed.

  A. The exact wire payload of AliceBlueClient.place_order for each order type, product and side.
  B. The marketable-LIMIT arithmetic and the default buffer.
  C. Through the real pipeline with a NON-ZERO buffer (0.5%): a Master LIMIT is copied at exactly the Master's
     price; only a Master MARKET fill converted to a LIMIT is buffered (BUY up, SELL down).

The default buffer is 0 (changed 2026-09-10 at the owner's request), so any check that LIMIT is "never buffered"
must run with a non-zero buffer or it proves nothing. The broker is a mock; no request leaves the process.
"""
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core.config import Settings, settings
from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.types import OrderRequest, OrderResultCategory, OrderSide, OrderType, ProductType
from app.services.copy_engine.pipeline import CopyEnginePipeline, compute_marketable_limit_price
from app.services.copy_engine.validator import EventValidator
from test_marketable_limit_strategy import _allow_sell, _funds, _limit_event, _market_event, _success_result

RELIANCE_TOKEN = "2885"                        # KNOWN_INSTRUMENTS[("NSE", "RELIANCE")]


async def sent_payload(mock_redis, **order_kwargs):
    """Places one order through the real AliceBlueClient.place_order and returns the single outgoing row."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
    resp = AsyncMock()
    resp.status_code = 200
    resp.json = lambda: [{"status": "Ok", "message": "Success", "result": [{"status": "OK", "brokerOrderId": "B1"}]}]
    sent = []

    async def fake_request(method, url, **kwargs):
        sent.append((method, url, kwargs.get("json")))
        return resp

    mock_http.request = AsyncMock(side_effect=fake_request)
    client = AliceBlueClient(account_id="acc-test", user_id="AB00101", api_secret="secret",
                             redis=mock_redis, http_client=mock_http)
    base = dict(symbol="RELIANCE", exchange="NSE", side=OrderSide.BUY, quantity=7, product_type=ProductType.MIS,
                order_type=OrderType.MARKET, client_order_id="CLIENT_1")
    result = await client.place_order(OrderRequest(**{**base, **order_kwargs}))
    assert result.category == OrderResultCategory.SUCCESS
    assert len(sent) == 1                                                       # exactly one transmission
    method, url, body = sent[0]
    assert method == "POST" and url.endswith("/open-api/od/v1/orders/placeorder")
    return body[0]


# ── A. wire payload ────────────────────────────────────────────────────────────────────────

GOLDEN = [
    # order_type, product, side, price, trigger | price_type, order_type, product, side, price, trigger
    (OrderType.MARKET, ProductType.MIS, OrderSide.BUY, "0", "0", ("MKT", "MARKET", "INTRADAY", "BUY", "0", "0")),
    (OrderType.MARKET, ProductType.MIS, OrderSide.BUY, "55", "9", ("MKT", "MARKET", "INTRADAY", "BUY", "0", "0")),   # price/trigger ignored
    (OrderType.LIMIT, ProductType.CNC, OrderSide.SELL, "125.83", "7", ("L", "LIMIT", "LONGTERM", "SELL", "125.83", "0")),  # trigger ignored
    (OrderType.LIMIT, ProductType.NRML, OrderSide.BUY, "1000", "0", ("L", "LIMIT", "MTF", "BUY", "1000.00", "0")),
    (OrderType.LIMIT, ProductType.MIS, OrderSide.SELL, "2950.05", "0", ("L", "LIMIT", "INTRADAY", "SELL", "2950.05", "0")),
    (OrderType.SL, ProductType.MIS, OrderSide.SELL, "99.5", "100", ("SL", "SL", "INTRADAY", "SELL", "99.50", "100.00")),
    (OrderType.SL_M, ProductType.NRML, OrderSide.BUY, "0", "250.5", ("SL-M", "SLM", "MTF", "BUY", "0", "250.50")),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type,product,side,price,trigger,expected", GOLDEN,
                         ids=[f"{g[0].value}-{g[1].value}-{g[2].value}-{g[3]}" for g in GOLDEN])
async def test_alice_wire_payload_is_pinned(mock_redis, order_type, product, side, price, trigger, expected):
    row = await sent_payload(mock_redis, order_type=order_type, product_type=product, side=side,
                             price=Decimal(price), trigger_price=Decimal(trigger))
    price_type, a3_type, product_str, trans, price_str, trigger_str = expected
    assert (row["priceType"], row["orderType"]) == (price_type, a3_type)
    assert (row["price_type"], row["order_type"]) == (price_type, a3_type)
    assert row["product"] == row["productType"] == row["product_type"] == product_str
    assert row["transactionType"] == row["transType"] == trans
    assert row["price"] == price_str and row["slTriggerPrice"] == trigger_str
    assert row["qty"] == row["quantity"] == row["orderQuantity"] == "7"
    assert row["instrumentId"] == RELIANCE_TOKEN and row["exchange"] == "NSE"
    assert row["tradingSymbol"] == "RELIANCE-EQ"
    assert row["validity"] == "DAY" and row["orderComplexity"] == "regular"


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type", [OrderType.MARKET, OrderType.LIMIT])
async def test_no_client_order_reference_is_sent_to_alice_blue(mock_redis, order_type):
    """The basis of the Alice profile's CLIENT_REFERENCE = NOT_SUPPORTED: the caller's client_order_id is NOT
    transmitted - no field name hints at a tag/reference, and its value appears nowhere in the payload. (It is
    why Alice cannot look an ambiguous order up by reference.)"""
    import re
    row = await sent_payload(mock_redis, order_type=order_type, price=Decimal("10"), client_order_id="CLIENT_REF_XYZ")
    assert not [k for k in row if re.search(r"client|reference|tag|remark|order_?id$", k, re.IGNORECASE)], sorted(row)
    assert "CLIENT_REF_XYZ" not in str(row)


@pytest.mark.asyncio
async def test_an_explicit_instrument_token_is_what_goes_on_the_wire(mock_redis):
    row = await sent_payload(mock_redis, symbol="NIFTY", exchange="NFO", instrument_token="47183",
                             order_type=OrderType.LIMIT, price=Decimal("4.25"), product_type=ProductType.CNC)
    assert row["instrumentId"] == "47183" and row["product"] == "LONGTERM" and row["price"] == "4.25"


@pytest.mark.asyncio
async def test_invalid_orders_are_refused_before_any_request(mock_redis):
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    client = AliceBlueClient(account_id="acc-test", user_id="AB00101", api_secret="secret",
                             redis=mock_redis, http_client=mock_http)
    for bad in (dict(quantity=0), dict(order_type=OrderType.LIMIT, price=Decimal("0"))):
        base = dict(symbol="RELIANCE", exchange="NSE", side=OrderSide.BUY, quantity=1,
                    product_type=ProductType.MIS, order_type=OrderType.MARKET)
        result = await client.place_order(OrderRequest(**{**base, **bad}))
        assert result.category == OrderResultCategory.REJECTED
    mock_http.request.assert_not_called()


# ── B. marketable-LIMIT arithmetic ─────────────────────────────────────────────────────────

def test_the_shipped_default_buffer_is_zero():
    assert Settings.model_fields["MARKET_ORDER_BUFFER_PERCENT"].default == Decimal("0")


@pytest.mark.parametrize("buffer,side,reference,expected", [
    ("0", "BUY", "125.83", "125.83"),
    ("0", "SELL", "125.83", "125.83"),
    ("0.5", "BUY", "1000", "1005.00"),
    ("0.5", "SELL", "1000", "995.00"),
    ("0.5", "BUY", "333.33", "335.00"),                  # 335.00465 quantised
    ("0.5", "sell", "333.33", "331.66"),                 # 331.66335 quantised; side is case-insensitive
])
def test_marketable_limit_price_arithmetic(monkeypatch, buffer, side, reference, expected):
    monkeypatch.setattr(settings, "MARKET_ORDER_BUFFER_PERCENT", Decimal(buffer))
    assert compute_marketable_limit_price(side, Decimal(reference)) == Decimal(expected)


@pytest.mark.parametrize("reference", [None, Decimal("0"), Decimal("-1")])
def test_no_usable_reference_price_means_no_price(monkeypatch, reference):
    monkeypatch.setattr(settings, "MARKET_ORDER_BUFFER_PERCENT", Decimal("0.5"))
    assert compute_marketable_limit_price("BUY", reference) is None


# ── C. through the real pipeline, buffer ON ────────────────────────────────────────────────

async def replicate(db_session, mock_redis, accounts, event, *, sell_qty=None):
    follower = accounts["follower1"]
    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.place_order.return_value = _success_result("BRK_PIN")
    if sell_qty:
        _allow_sell(client, mock_redis, quantity=sell_qty)
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=event, clients={str(follower.id): client})
    assert res["status"] == "completed", res
    client.place_order.assert_awaited_once()
    return client.place_order.call_args.args[0]


@pytest.mark.asyncio
async def test_master_limit_is_copied_at_exactly_the_master_price_even_with_a_buffer(
        db_session, mock_redis, sample_accounts, sample_strategy, monkeypatch):
    monkeypatch.setattr(settings, "MARKET_ORDER_BUFFER_PERCENT", Decimal("0.5"))
    sent = await replicate(db_session, mock_redis, sample_accounts, _limit_event(quantity=10, price=Decimal("2950.00")))
    assert sent.order_type == OrderType.LIMIT
    assert sent.price == Decimal("2950.00")                    # not 2964.75: the buffer never touches a LIMIT
    assert sent.quantity == 10 and sent.side == OrderSide.BUY and sent.product_type == ProductType.MIS


@pytest.mark.asyncio
@pytest.mark.parametrize("side,expected", [("BUY", Decimal("1005.00")), ("SELL", Decimal("995.00"))])
async def test_only_a_converted_master_market_fill_is_buffered(
        db_session, mock_redis, sample_accounts, sample_strategy, monkeypatch, side, expected):
    monkeypatch.setattr(settings, "MARKET_ORDER_BUFFER_PERCENT", Decimal("0.5"))
    event = _market_event(quantity=10, price=Decimal("1000.00"), side=side, master_order_id=f"ORD_PIN_{side}")
    sent = await replicate(db_session, mock_redis, sample_accounts, event, sell_qty=10 if side == "SELL" else None)
    assert sent.order_type == OrderType.LIMIT and sent.price == expected
    assert sent.quantity == 10 and sent.product_type == ProductType.MIS
    assert sent.side == (OrderSide.BUY if side == "BUY" else OrderSide.SELL)


@pytest.mark.asyncio
async def test_a_converted_market_fill_is_unbuffered_at_the_shipped_default(
        db_session, mock_redis, sample_accounts, sample_strategy):
    assert settings.MARKET_ORDER_BUFFER_PERCENT == Decimal("0")
    event = _market_event(quantity=3, price=Decimal("125.83"), side="BUY", master_order_id="ORD_PIN_DEFAULT")
    sent = await replicate(db_session, mock_redis, sample_accounts, event)
    assert sent.order_type == OrderType.LIMIT and sent.price == Decimal("125.83") and sent.quantity == 3
