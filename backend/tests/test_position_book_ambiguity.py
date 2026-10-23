"""
An ambiguous position book blocks SELL, and a Groww order must respect the instrument master's lot size.

Duplicate rows for one instrument and product cannot be told apart (stale vs current, long vs short legs, a paging
overlap), and the SELL gate sums every matching row - so such a book is "unknown", never combined.
Offline: mocked transports.
"""
import pytest
from sqlalchemy import select

from app.models.audit import AuditLog
from app.services.alice_blue.types import OrderRequest, OrderResultCategory, OrderSide, OrderType, ProductType
from app.services.angel_one.client import AngelCapabilityUnavailable
from app.services.brokers.position_checks import duplicate_position_key
from app.services.alice_blue.types import Position
from app.services.groww.client import GrowwCapabilityUnavailable
from app.services.groww.instruments import GrowwInstrumentStore
from conftest import TestingSessionLocal
import angel_helpers as A
import groww_helpers as G
from decimal import Decimal
from test_angel_one_sell_safety import angel_enabled, _angel_child, _run as angel_run, _sell as angel_sell  # noqa: F401
from test_groww_sell_safety import groww_enabled, _groww_child, _positions, _run as groww_run, _sell as groww_sell  # noqa: F401


# R5: these tests drive a Groww / Angel One Child through the pipeline, which needs the pair enabled.
pytestmark = pytest.mark.usefixtures("cross_broker_pairs_enabled")


def pos(symbol="SBIN", exchange="NSE", product="MIS", qty=10, iid=None):
    return Position(symbol=symbol, quantity=qty, side="BUY" if qty >= 0 else "SELL", average_price=Decimal("800"),
                    account_id="a", exchange=exchange, product_type=product, instrument_id=iid)


# ── the shared check ────────────────────────────────────────────────────────────────────────

def test_one_row_per_instrument_and_product_is_consistent():
    assert duplicate_position_key([pos(), pos(product="CNC"), pos(symbol="TCS"), pos(exchange="BSE")]) is None
    assert duplicate_position_key([pos(iid="3045"), pos(symbol="TCS", iid="3045")]) is None   # a shared id alone is not a repeat
    assert duplicate_position_key([]) is None


@pytest.mark.parametrize("a,b", [
    (pos(), pos(qty=-4)),                              # a long and a short leg of the same thing
    (pos(), pos()),                                    # an exact repeat
    (pos(symbol="sbin"), pos(symbol="SBIN ")),        # spelling differences
    (pos(symbol="SBIN-EQ"), pos(symbol="SBIN")),      # the -EQ spelling of the same equity
])
def test_repeated_instrument_and_product_is_detected(a, b):
    assert duplicate_position_key([a, b]) is not None


# ── Groww ───────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_groww_duplicate_rows_make_the_book_unknown(mock_redis):
    fake = G.FakeGroww()
    fake.positions_response = _positions(G.position_row(qty=10), G.position_row(qty=10))
    client = G.make_client(mock_redis, fake)
    await client.login()
    with pytest.raises(GrowwCapabilityUnavailable, match="more than once"):
        await client.get_positions()
    assert await client.get_positions_or_none() is None


@pytest.mark.asyncio
async def test_groww_distinct_rows_are_unaffected(mock_redis):
    fake = G.FakeGroww()
    fake.positions_response = _positions(G.position_row(qty=10), G.position_row(product="CNC", qty=5), G.position_row(symbol="TCS", qty=2))
    client = G.make_client(mock_redis, fake)
    await client.login()
    assert len(await client.get_positions()) == 3


@pytest.mark.asyncio
async def test_groww_sell_is_blocked_by_duplicate_rows_even_when_each_row_alone_would_cover_it(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = G.FakeGroww()
    fake.positions_response = _positions(G.position_row(qty=10, product="MIS"), G.position_row(qty=10, product="MIS"))
    out, _ = await groww_run(mock_redis, master, child, fake, groww_sell(master, "DUP-1", qty=10))
    assert out["status"] == "REJECTED" and fake.bodies("/v1/order/create") == []
    async with TestingSessionLocal() as s:
        reasons = [a.reason_code for a in (await s.execute(select(AuditLog))).scalars().all()]
    assert {"POSITION_FETCH_FAILED", "RISK_INPUTS_UNAVAILABLE"} & set(reasons)


# ── Angel One ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_angel_duplicate_rows_make_the_book_unknown(mock_redis):
    fake = A.FakeAngel(positions=[A.position_row(net="10"), A.position_row(net="10")])
    client = A.make_client(mock_redis, fake)
    await client.login()
    with pytest.raises(AngelCapabilityUnavailable, match="more than once"):
        await client.get_positions()
    assert await client.get_positions_or_none() is None


@pytest.mark.asyncio
async def test_angel_distinct_rows_are_unaffected(mock_redis):
    fake = A.FakeAngel(positions=[A.position_row(net="10"), A.position_row(product="DELIVERY", net="5")])
    client = A.make_client(mock_redis, fake)
    await client.login()
    assert len(await client.get_positions()) == 2


@pytest.mark.asyncio
async def test_angel_sell_is_blocked_by_duplicate_rows(db_session, mock_redis, angel_enabled):
    master, child = await _angel_child(db_session)
    fake = A.FakeAngel(positions=[A.position_row(net="10"), A.position_row(net="10")])
    out, _ = await angel_run(mock_redis, master, child, fake, angel_sell(master, "DUP-ANG", qty=10))
    assert out["status"] == "REJECTED" and fake.order_bodies() == []


# ── Groww lot size (from the instrument master's documented lot_size column) ─────────────────

def lot_store(lot):
    s = GrowwInstrumentStore()
    s.load_rows([{"exchange": "NSE", "trading_symbol": "LOTX", "instrument_type": "EQ", "segment": "CASH",
                  "isin": "INE000000777", "lot_size": str(lot), "freeze_quantity": "1000", "buy_allowed": "1", "sell_allowed": "1"}])
    return s


def req(qty, symbol="LOTX"):
    return OrderRequest(symbol=symbol, exchange="NSE", side=OrderSide.BUY, quantity=qty, product_type=ProductType.MIS,
                        order_type=OrderType.LIMIT, price=Decimal("100"), instrument_token=G.ALICE_STYLE_TOKEN,
                        client_order_id=f"JT_LIM_LOT{qty}_acc12345")


@pytest.mark.asyncio
@pytest.mark.parametrize("qty,sent", [(25, True), (50, True), (30, False), (1, False), (26, False)])
async def test_groww_quantity_must_be_a_whole_number_of_lots_and_is_never_rounded(mock_redis, qty, sent):
    fake = G.FakeGroww()
    client = G.make_client(mock_redis, fake, store=lot_store(25))
    await client.login()
    res = await client.place_order(req(qty))
    if sent:
        assert res.category == OrderResultCategory.SUCCESS and fake.bodies("/v1/order/create")[0]["quantity"] == qty
    else:
        assert res.category == OrderResultCategory.REJECTED and "lot size" in res.message
        assert fake.calls("/v1/order/create") == []


@pytest.mark.asyncio
async def test_groww_lot_size_one_changes_nothing(mock_redis):
    fake = G.FakeGroww()
    client = G.make_client(mock_redis, fake, store=lot_store(1))
    await client.login()
    assert (await client.place_order(req(7))).category == OrderResultCategory.SUCCESS
