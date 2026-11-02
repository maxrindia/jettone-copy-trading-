"""
Zerodha Child, NSE/BSE cash equity: the Alice Blue Master order types the pipeline copies, end to end OFFLINE.

Real CopyEnginePipeline + real ZerodhaClient + the in-memory database; only Kite's HTTP is faked (zerodha_helpers.FakeKite, shaped from
Zerodha's documentation). The REAL gates decide: the ZERODHA_* role flags and the owner's Alice -> Zerodha canary, with no waiver fixture.
These tests prove what Jettone SENDS and how it records the result. They are CODE-LEVEL evidence: nothing here has been seen against a
real Zerodha account, and a fake accepts whatever it is sent.
"""
import itertools
import json

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.accounts import Account
from app.models.trades import CopyTrade, Order
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from conftest import TestingSessionLocal
from test_order_handling_matrix import _event, _setup
from zerodha_helpers import ALICE_STYLE_TOKEN, FakeKite, ok, make_client, position_row, seed_session

EXCHANGES = ("NSE", "BSE")
PRODUCTS = (("INTRADAY", "MIS"), ("LONGTERM", "CNC"))          # Alice Blue's product names -> the Zerodha product the Child must be sent
ORDER_TYPES = ("MARKET", "LIMIT", "SL", "SLM")
SIDES = ("BUY", "SELL")


@pytest.fixture
def zerodha_enabled(monkeypatch):
    """Exactly what the owner must switch on for Zerodha to be a Child of Alice Blue: the two role flags and the pair canary."""
    monkeypatch.setattr(settings, "ZERODHA_ENABLED", True)
    monkeypatch.setattr(settings, "ZERODHA_CHILD_TRADING_ENABLED", True)
    monkeypatch.setattr(settings, "ALICE_ZERODHA_CANARY_ENABLED", True)
    monkeypatch.setattr(settings, "SELL_POSITION_SAFETY_BYPASS", False)


async def zerodha_child(db_session):
    master, child = await _setup(db_session)
    async with TestingSessionLocal() as s:
        row = await s.get(Account, child.id)
        row.broker = "zerodha"
        await s.commit()
    return master, child


def master_event(master, oid, *, side, order_type, product, exchange, qty=10):
    price = {"MARKET": "800", "LIMIT": "800", "SL": "800", "SLM": "0"}[order_type]
    trigger = {"MARKET": "0", "LIMIT": "0", "SL": "795" if side == "BUY" else "805", "SLM": "795" if side == "BUY" else "805"}[order_type]
    return _event(master, oid, side=side, order_type=order_type, product=product, exchange=exchange, symbol="SBIN", qty=qty, price=price,
                  trigger=trigger, token=ALICE_STYLE_TOKEN)


def new_pipeline(mock_redis):
    return CopyEnginePipeline(validator=EventValidator(redis=mock_redis))


async def copy_to_zerodha(mock_redis, master, child, fake, event, *, pipeline=None):
    client = make_client(mock_redis, fake, account_id=str(child.id))
    await seed_session(mock_redis, account_id=str(child.id))
    pipeline = pipeline or new_pipeline(mock_redis)
    res = await pipeline.execute_replication(event=event, clients={str(child.id): client})
    return (res.get("results") or {}).get(str(child.id)), client, pipeline


def holds(exchange, product, qty=10):
    return ok({"net": [position_row(symbol="SBIN", exchange=exchange, product=product, qty=qty)], "day": []})


def expected_form(*, side, order_type, product, exchange, qty=10):
    """What Kite must be sent. A MARKET Master fill is copied as a marketable LIMIT at its fill price, never as a MARKET order."""
    kite_type = {"MARKET": "LIMIT", "LIMIT": "LIMIT", "SL": "SL", "SLM": "SL-M"}[order_type]
    form = {"tradingsymbol": "SBIN", "exchange": exchange, "transaction_type": side, "quantity": str(qty), "product": product,
            "order_type": kite_type, "validity": "DAY"}
    if kite_type in ("LIMIT", "SL"):
        form["price"] = "800"
    if kite_type in ("SL", "SL-M"):
        form["trigger_price"] = "795" if side == "BUY" else "805"
    if kite_type == "SL-M":
        form["market_protection"] = "-1"
    return form


MATRIX = list(itertools.product(EXCHANGES, PRODUCTS, ORDER_TYPES, SIDES))


@pytest.mark.asyncio
@pytest.mark.parametrize("exchange,product,order_type,side", MATRIX, ids=[f"{e}-{p[0]}-{t}-{s}" for e, p, t, s in MATRIX])
async def test_every_equity_entry_shape_is_sent_to_zerodha_correctly(db_session, mock_redis, zerodha_enabled, exchange, product, order_type, side):
    alice_product, kite_product = product
    master, child = await zerodha_child(db_session)
    fake = FakeKite()
    fake.positions_response = holds(exchange, kite_product)                  # a SELL needs a confirmed long on the Child's own broker
    event = master_event(master, f"M-{exchange}-{alice_product}-{order_type}-{side}", side=side, order_type=order_type,
                         product=alice_product, exchange=exchange)
    out, _, _ = await copy_to_zerodha(mock_redis, master, child, fake, event)

    assert out and out.get("status") == "SUCCESS", out
    (request,) = fake.calls("/orders/regular", "POST")                       # exactly one order, in the one variety Jettone places
    form = fake.form(request)
    for key, value in expected_form(side=side, order_type=order_type, product=kite_product, exchange=exchange).items():
        assert form.get(key) == value, (key, form)
    kite_type = form["order_type"]
    assert ("price" in form) == (kite_type in ("LIMIT", "SL")) and ("trigger_price" in form) == (kite_type in ("SL", "SL-M"))
    assert ("market_protection" in form) == (kite_type == "SL-M")            # automatic protection only on market-style orders
    assert form["tag"].isalnum() and form["tag"].startswith("JT") and len(form["tag"]) <= 20
    assert ALICE_STYLE_TOKEN not in json.dumps(form)                         # the Master broker's token never reaches Kite
    assert request.headers["X-Kite-Version"] == "3" and request.headers["Authorization"].startswith("token ")

    async with TestingSessionLocal() as s:
        orders = (await s.execute(select(Order).where(Order.account_id == child.id))).scalars().all()
        copies = (await s.execute(select(CopyTrade).where(CopyTrade.follower_account_id == child.id))).scalars().all()
    assert len(orders) == 1 and orders[0].broker == "ZERODHA" and orders[0].broker_order_id == "240101000000099"
    assert len(copies) == 1


@pytest.mark.asyncio
async def test_without_the_canary_alice_to_zerodha_is_not_copied(db_session, mock_redis, monkeypatch):
    for name in ("ZERODHA_ENABLED", "ZERODHA_CHILD_TRADING_ENABLED"):
        monkeypatch.setattr(settings, name, True)
    master, child = await zerodha_child(db_session)
    fake = FakeKite()
    out, _, _ = await copy_to_zerodha(mock_redis, master, child, fake, master_event(master, "NOCAN", side="BUY", order_type="LIMIT",
                                                                                   product="INTRADAY", exchange="NSE"))
    assert not fake.calls("/orders/regular", "POST")                         # the role flags alone never open the pair
    assert out is None or out.get("status") != "SUCCESS"


@pytest.mark.asyncio
async def test_with_the_role_flags_off_nothing_is_copied_even_with_the_canary(db_session, mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "ALICE_ZERODHA_CANARY_ENABLED", True)
    master, child = await zerodha_child(db_session)
    fake = FakeKite()
    out, _, _ = await copy_to_zerodha(mock_redis, master, child, fake, master_event(master, "NOFLAG", side="BUY", order_type="LIMIT",
                                                                                   product="INTRADAY", exchange="NSE"))
    assert not fake.calls("/orders/regular", "POST")
