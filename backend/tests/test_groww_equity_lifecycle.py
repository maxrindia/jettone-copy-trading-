"""
Groww Child, NSE/BSE cash equity: the Alice Blue Master order types the pipeline copies, end to end OFFLINE.

Real CopyEnginePipeline + real GrowwClient + the in-memory database; only Groww's HTTP is faked (groww_helpers.FakeGroww, shaped from
Groww's documentation and the response shapes recorded from the live account). These tests prove what Jettone SENDS and how it records
the result. They are CODE-LEVEL evidence: a fake accepts whatever it is sent, so none of this is proof that Groww accepts an order of
that shape. What has and has not been seen live is listed in the Phase A report, not here.
"""
import json
import itertools

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.accounts import Account
from app.models.trades import CopyTrade, Order
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from conftest import TestingSessionLocal
from groww_helpers import FakeGroww, make_client, ok, order_row, position_row
from test_order_handling_matrix import _event, _setup

pytestmark = pytest.mark.usefixtures("cross_broker_pairs_enabled")

CHILD_IP = "151.245.182.60"
EXCHANGES = ("NSE", "BSE")
# Alice Blue's own product names -> the Groww product the Child must be sent.
PRODUCTS = (("INTRADAY", "MIS"), ("LONGTERM", "CNC"))
ORDER_TYPES = ("MARKET", "LIMIT", "SL", "SLM")
SIDES = ("BUY", "SELL")


@pytest.fixture
def groww_enabled(monkeypatch):
    monkeypatch.setattr(settings, "GROWW_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_CHILD_TRADING_ENABLED", True)
    monkeypatch.setattr(settings, "SELL_POSITION_SAFETY_BYPASS", False)


async def groww_child(db_session):
    master, child = await _setup(db_session)
    async with TestingSessionLocal() as s:
        row = await s.get(Account, child.id)
        row.broker = "groww"
        row.static_ip_address = CHILD_IP
        await s.commit()
    return master, child


def master_event(master, oid, *, side, order_type, product, exchange, qty=10):
    price = {"MARKET": "800", "LIMIT": "800", "SL": "800", "SLM": "0"}[order_type]
    trigger = {"MARKET": "0", "LIMIT": "0", "SL": "795" if side == "BUY" else "805", "SLM": "795" if side == "BUY" else "805"}[order_type]
    return _event(master, oid, side=side, order_type=order_type, product=product, exchange=exchange, symbol="SBIN",
                  qty=qty, price=price, trigger=trigger, token="99999")


def new_pipeline(mock_redis):
    return CopyEnginePipeline(validator=EventValidator(redis=mock_redis))


async def copy_to_groww(mock_redis, master, child, fake, event, *, pipeline=None):
    client = make_client(mock_redis, fake, account_id=str(child.id), static_ip=CHILD_IP)
    await client.login()
    pipeline = pipeline or new_pipeline(mock_redis)
    res = await pipeline.execute_replication(event=event, clients={str(child.id): client})
    return (res.get("results") or {}).get(str(child.id)), client, pipeline


def holds(exchange, product, qty=10):
    return ok({"positions": [position_row(symbol="SBIN", exchange=exchange, product=product, qty=qty)]})


def expected_body(*, side, order_type, product, exchange, qty=10):
    """What Groww must be sent for this Master order. A MARKET Master fill is copied as a marketable LIMIT at its fill price."""
    groww_type = {"MARKET": "LIMIT", "LIMIT": "LIMIT", "SL": "SL", "SLM": "SL_M"}[order_type]
    body = {"trading_symbol": "SBIN", "exchange": exchange, "segment": "CASH", "transaction_type": side, "quantity": qty,
            "product": product, "order_type": groww_type, "validity": "DAY"}
    if groww_type in ("LIMIT", "SL"):
        body["price"] = 800.0
    if groww_type in ("SL", "SL_M"):
        body["trigger_price"] = 795.0 if side == "BUY" else 805.0
    return body


MATRIX = list(itertools.product(EXCHANGES, PRODUCTS, ORDER_TYPES, SIDES))


@pytest.mark.asyncio
@pytest.mark.parametrize("exchange,product,order_type,side", MATRIX,
                         ids=[f"{e}-{p[0]}-{t}-{s}" for e, p, t, s in MATRIX])
async def test_every_equity_entry_shape_is_sent_to_groww_correctly(db_session, mock_redis, groww_enabled, exchange, product, order_type, side):
    alice_product, groww_product = product
    master, child = await groww_child(db_session)
    fake = FakeGroww()
    fake.positions_response = holds(exchange, groww_product)            # a SELL needs a confirmed long on the Child's own broker
    event = master_event(master, f"M-{exchange}-{alice_product}-{order_type}-{side}", side=side, order_type=order_type,
                         product=alice_product, exchange=exchange)
    out, _, _ = await copy_to_groww(mock_redis, master, child, fake, event)

    assert out and out.get("status") == "SUCCESS", out
    (body,) = fake.bodies("/v1/order/create")                            # exactly one request, whatever the order type
    want = expected_body(side=side, order_type=order_type, product=groww_product, exchange=exchange)
    for key, value in want.items():
        assert body.get(key) == value, (key, body)
    if "price" not in want:
        assert "price" not in body                                       # SL-M carries no price
    if "trigger_price" not in want:
        assert "trigger_price" not in body                               # LIMIT / MARKET copies carry no trigger
    assert body["order_reference_id"].startswith("JT") and 8 <= len(body["order_reference_id"]) <= 20
    assert "99999" not in json.dumps(body)                               # the Master broker's token never reaches Groww

    async with TestingSessionLocal() as s:
        orders = (await s.execute(select(Order).where(Order.account_id == child.id))).scalars().all()
        copies = (await s.execute(select(CopyTrade).where(CopyTrade.follower_account_id == child.id))).scalars().all()
    assert len(orders) == 1 and orders[0].broker == "GROWW" and orders[0].broker_order_id == "GRW-NEW-1"
    assert len(copies) == 1
