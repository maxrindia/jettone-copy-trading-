"""
Order-handling regression matrix around the 2026-10-14 NIFTY SELL incident.

Live evidence: a Child held 65 units of a NIFTY option. Alice Blue's order book
reported the Master's order with product LONGTERM, its position book reported
the Child's holding as NRML (same instrumentId). The SELL position-safety check
treated LONGTERM (CNC bucket) and NRML as different products, computed 0 held
and rejected a covered SELL.

Fix: on derivative exchanges (NFO/BFO/MCX/CDS) CNC/LONGTERM and NRML are one
carry holding; intraday (MIS) stays separate everywhere and equity products stay
strict. Position safety stays ON (SELL_POSITION_SAFETY_BYPASS=false here).

Also pins the order-type / product mapping the old application (64d1fc3) uses
when placing orders, so the fix cannot disturb MARKET, LIMIT, SL, SL-M,
intraday, delivery or option handling. The broker is always a mock.
"""
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.accounts import CopySetting, QuantityMode
from app.models.trades import Order
from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.types import (
    OrderRequest,
    OrderSide,
    OrderType,
    Position,
    ProductType,
)
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.risk_inputs import (
    UnknownProductTypeError,
    get_available_long,
    normalize_product_for_exchange,
)
from app.services.copy_engine.validator import EventValidator
from conftest import TestingSessionLocal
from strategy_helpers import link_strategy
from test_strategy_master_switching import _account, _child_client

NIFTY_TOKEN = "47183"


@pytest.fixture(autouse=True)
def _safety_on(monkeypatch):
    # The whole point: these pass with the position check ENABLED.
    monkeypatch.setattr(settings, "SELL_POSITION_SAFETY_BYPASS", False)


def _pos(qty, product, exchange="NFO", token=NIFTY_TOKEN, symbol="NIFTY19OCT26C23500"):
    return Position(symbol=symbol, quantity=qty, side="BUY" if qty >= 0 else "SELL",
                    average_price=Decimal("4.35"), account_id="acc", exchange=exchange,
                    product_type=product, instrument_id=token)


def _gate_client(mock_redis, positions):
    client = AsyncMock()
    client.redis = mock_redis
    client.get_positions_or_none.return_value = positions
    return client


# ─── 1. product normalization ───────────────────────────────────────────────

@pytest.mark.parametrize("exchange", ["NFO", "BFO", "MCX", "CDS"])
@pytest.mark.parametrize("product", ["CNC", "LONGTERM", "DELIVERY", "NRML", "MTF", "CARRYFORWARD"])
def test_carry_products_are_one_bucket_on_derivative_exchanges(exchange, product):
    assert normalize_product_for_exchange(product, exchange) == "NRML"


@pytest.mark.parametrize("exchange", ["NFO", "NSE", "BSE", "MCX"])
@pytest.mark.parametrize("product", ["MIS", "INTRADAY"])
def test_intraday_stays_separate_everywhere(exchange, product):
    assert normalize_product_for_exchange(product, exchange) == "MIS"


@pytest.mark.parametrize("exchange", ["NSE", "BSE", "", None])
def test_equity_products_stay_strict(exchange):
    assert normalize_product_for_exchange("LONGTERM", exchange) == "CNC"
    assert normalize_product_for_exchange("CNC", exchange) == "CNC"
    assert normalize_product_for_exchange("NRML", exchange) == "NRML"


def test_unknown_product_still_fails_closed():
    with pytest.raises(UnknownProductTypeError):
        normalize_product_for_exchange("BRACKET", "NFO")


# ─── 2. get_available_long: the safety gate itself ──────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("order_product", ["LONGTERM", "CNC", "NRML"])
async def test_incident_nfo_long_held_as_nrml_covers_a_carry_sell(mock_redis, order_product):
    client = _gate_client(mock_redis, [_pos(65, "NRML")])
    held = await get_available_long(client, "NFO", "NIFTY", order_product, provided_token=NIFTY_TOKEN)
    assert held == 65


@pytest.mark.asyncio
async def test_nfo_intraday_and_carry_are_never_combined(mock_redis):
    client = _gate_client(mock_redis, [_pos(65, "NRML"), _pos(130, "MIS")])
    assert await get_available_long(client, "NFO", "NIFTY", "MIS", provided_token=NIFTY_TOKEN) == 130
    assert await get_available_long(client, "NFO", "NIFTY", "LONGTERM", provided_token=NIFTY_TOKEN) == 65
    assert await get_available_long(client, "NFO", "NIFTY", "INTRADAY", provided_token=NIFTY_TOKEN) == 130


@pytest.mark.asyncio
async def test_other_contract_or_exchange_never_counts(mock_redis):
    client = _gate_client(mock_redis, [_pos(65, "NRML", token="99999"), _pos(65, "NRML", exchange="BFO")])
    assert await get_available_long(client, "NFO", "NIFTY", "LONGTERM", provided_token=NIFTY_TOKEN) == 0


@pytest.mark.asyncio
async def test_net_short_derivative_offers_nothing_to_sell(mock_redis):
    client = _gate_client(mock_redis, [_pos(-65, "NRML")])
    assert await get_available_long(client, "NFO", "NIFTY", "LONGTERM", provided_token=NIFTY_TOKEN) == 0


@pytest.mark.asyncio
async def test_equity_delivery_and_intraday_stay_strict(mock_redis):
    eq = lambda qty, product: _pos(qty, product, exchange="NSE", token="383", symbol="BEL-EQ")  # noqa: E731
    client = _gate_client(mock_redis, [eq(2, "CNC"), eq(5, "MIS")])
    assert await get_available_long(client, "NSE", "BEL", "LONGTERM", provided_token="383") == 2
    assert await get_available_long(client, "NSE", "BEL", "INTRADAY", provided_token="383") == 5
    client = _gate_client(mock_redis, [eq(2, "NRML")])           # equity NRML is NOT delivery
    assert await get_available_long(client, "NSE", "BEL", "LONGTERM", provided_token="383") == 0


# ─── 3. broker payload: order types and products (unchanged from 64d1fc3) ───

async def _payload(mock_redis, *, order_type, product, exchange="NFO", price="4.40", trigger="0"):
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
    resp = AsyncMock()
    resp.status_code = 200
    resp.json = lambda: {"status": "Ok", "message": "Success", "result": [{"brokerOrderId": "B1"}]}
    sent = []

    async def fake_request(method, url, **kwargs):
        sent.append(kwargs.get("json"))
        return resp

    mock_http.request = AsyncMock(side_effect=fake_request)
    client = AliceBlueClient(account_id="acc-test", user_id="AB00101", api_secret="secret",
                             redis=mock_redis, http_client=mock_http)
    req = OrderRequest(symbol="NIFTY", exchange=exchange, side=OrderSide.SELL, quantity=65,
                       product_type=product, order_type=order_type, price=Decimal(price),
                       trigger_price=Decimal(trigger), instrument_token=NIFTY_TOKEN)
    result = await client.place_order(req)
    body = sent[0]
    return result, (body[0] if isinstance(body, list) else body)


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type,price,trigger,price_type,a3_type,sent_price,sent_trigger", [
    (OrderType.MARKET, "0", "0", "MKT", "MARKET", "0", "0"),
    (OrderType.LIMIT, "4.40", "0", "L", "LIMIT", "4.40", "0"),
    (OrderType.SL, "4.10", "4.20", "SL", "SL", "4.10", "4.20"),
    (OrderType.SL_M, "0", "4.20", "SL-M", "SLM", "0", "4.20"),
])
async def test_order_type_mapping(mock_redis, order_type, price, trigger, price_type, a3_type, sent_price, sent_trigger):
    result, body = await _payload(mock_redis, order_type=order_type, product=ProductType.NRML, price=price, trigger=trigger)
    assert result.broker_order_id == "B1"
    assert (body["priceType"], body["orderType"]) == (price_type, a3_type)
    assert body["price"] == sent_price and body["slTriggerPrice"] == sent_trigger
    assert body["qty"] == "65" and body["transactionType"] == "SELL"
    assert body["instrumentId"] == NIFTY_TOKEN


@pytest.mark.asyncio
@pytest.mark.parametrize("product,expected", [
    (ProductType.MIS, "INTRADAY"),
    (ProductType.CNC, "LONGTERM"),
    (ProductType.NRML, "MTF"),
])
async def test_product_mapping(mock_redis, product, expected):
    _, body = await _payload(mock_redis, order_type=OrderType.LIMIT, product=product)
    assert body["product"] == expected


# ─── 4. pipeline end to end (safety ON) ─────────────────────────────────────

def _event(master, order_id, *, side="SELL", order_type="MARKET", product="LONGTERM", exchange="NFO",
           symbol="NIFTY", qty=65, price="4.25", trigger="0", token=NIFTY_TOKEN):
    kind = {"MARKET": "MARKET_ACCEPTED", "LIMIT": "LIMIT_CREATED", "SL": "SL_PENDING_CREATED",
            "SLM": "SL_PENDING_CREATED"}[order_type]
    is_market = order_type == "MARKET"
    return MasterTradeEvent(
        master_order_id=order_id, symbol=symbol, exchange=exchange, side=side, quantity=qty,
        price=Decimal(price), limit_price=Decimal("0") if is_market else Decimal(price),
        trigger_price=Decimal(trigger), product_type=product, timestamp=datetime.now(timezone.utc),
        account_id=str(master.id), order_type=order_type, event_type=kind,
        fill_sequence=qty if is_market else 1, source="BROKER_EXTERNAL",
        status="COMPLETE" if is_market else ("TRIGGER_PENDING" if kind == "SL_PENDING_CREATED" else "OPEN"),
        total_quantity=qty, filled_quantity=qty if is_market else 0,
        pending_quantity=0 if is_market else qty, instrument_id=token,
    )


async def _setup(db_session):
    master = await _account(db_session, "master")
    child = await _account(db_session, "child")
    await link_strategy(db_session, master, [(child, "1.0")], name="matrix")
    async with TestingSessionLocal() as s:
        cs = (await s.execute(select(CopySetting).where(CopySetting.follower_account_id == child.id))).scalar_one()
        cs.quantity_mode = QuantityMode.MULTIPLIER
        await s.commit()
    return master, child


def _child(positions):
    client = _child_client("BRK_CHILD")
    client.get_positions_or_none.return_value = positions
    client.get_positions.return_value = positions
    return client


async def _run(mock_redis, master, child, client, event):
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=event, clients={str(child.id): client})
    return res["results"][str(child.id)]


def _placed(client):
    req = client.place_order.call_args.args[0] if client.place_order.call_args.args else client.place_order.call_args.kwargs.get("order")
    return req


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type,price,trigger", [
    ("MARKET", "4.25", "0"),
    ("LIMIT", "4.25", "0"),
    ("SL", "4.10", "4.20"),
    ("SLM", "0", "4.20"),
])
async def test_incident_nifty_carry_sell_is_copied_with_safety_on(db_session, mock_redis, order_type, price, trigger):
    master, child = await _setup(db_session)
    client = _child([_pos(65, "NRML")])
    out = await _run(mock_redis, master, child, client,
                     _event(master, f"M-{order_type}", order_type=order_type, price=price, trigger=trigger))
    assert out["status"] == "SUCCESS", out
    req = _placed(client)
    assert req.quantity == 65 and req.side == OrderSide.SELL
    assert req.product_type == ProductType.CNC          # LONGTERM sent back exactly as the Master traded it
    if order_type == "LIMIT":
        assert req.order_type == OrderType.LIMIT and req.price == Decimal("4.25")   # exact master limit
    if order_type in ("SL", "SLM"):
        assert req.trigger_price == Decimal(trigger)


@pytest.mark.asyncio
async def test_nifty_sell_without_a_holding_is_still_blocked(db_session, mock_redis):
    master, child = await _setup(db_session)
    client = _child([])
    out = await _run(mock_redis, master, child, client, _event(master, "M-NAKED"))
    assert out["status"] == "REJECTED"
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_nifty_carry_sell_is_not_covered_by_an_intraday_holding(db_session, mock_redis):
    master, child = await _setup(db_session)
    client = _child([_pos(65, "MIS")])
    out = await _run(mock_redis, master, child, client, _event(master, "M-MIS"))
    assert out["status"] == "REJECTED"
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_options_intraday_sell_covered_by_intraday_holding(db_session, mock_redis):
    master, child = await _setup(db_session)
    client = _child([_pos(65, "MIS")])
    out = await _run(mock_redis, master, child, client, _event(master, "M-OPT-MIS", product="INTRADAY"))
    assert out["status"] == "SUCCESS", out
    assert _placed(client).product_type == ProductType.MIS


@pytest.mark.asyncio
@pytest.mark.parametrize("product,position_product,expected", [
    ("INTRADAY", "MIS", "SUCCESS"),     # equity intraday
    ("LONGTERM", "CNC", "SUCCESS"),     # equity delivery
    ("LONGTERM", "MIS", "REJECTED"),    # delivery SELL never covered by intraday
    ("LONGTERM", "NRML", "REJECTED"),   # equity stays strict
])
async def test_equity_sell_matrix(db_session, mock_redis, product, position_product, expected):
    master, child = await _setup(db_session)
    client = _child([_pos(1, position_product, exchange="NSE", token="383", symbol="BEL-EQ")])
    out = await _run(mock_redis, master, child, client,
                     _event(master, f"M-EQ-{product}-{position_product}", exchange="NSE", symbol="BEL",
                            qty=1, price="374.75", product=product, token="383"))
    assert out["status"] == expected, out


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type", ["MARKET", "LIMIT"])
async def test_buys_are_copied_without_any_holding(db_session, mock_redis, order_type):
    """Entries need no position (only SELLs are position-gated)."""
    master, child = await _setup(db_session)
    client = _child([])                                   # flat account
    out = await _run(mock_redis, master, child, client,
                     _event(master, f"M-BUY-{order_type}", side="BUY", order_type=order_type, price="4.40"))
    assert out["status"] == "SUCCESS", out
    assert _placed(client).quantity == 65
    async with TestingSessionLocal() as s:
        row = (await s.execute(select(Order).where(Order.account_id == child.id))).scalar_one()
    assert row.quantity == 65
