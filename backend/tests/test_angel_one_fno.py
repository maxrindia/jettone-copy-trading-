"""
Angel One F&O (NFO) on the same architecture as Alice Blue, Groww and Zerodha. OFFLINE: fakes only, nothing is sent to a broker.

What is evidenced here is Jettone's logic given SOME Angel One wire format (the synthetic contract of tests/angel_helpers.py): the scrip-master
reading, the canonical contract identity, the order body, the position book, the order book, the route policy and the copy pipeline for every
supported direction. It says nothing about Angel One's real status values, position keys or F&O quantity unit: those are not documented and
need a supervised capture (the ANGEL_ONE_FNO_QUANTITY_UNIT setting has no default for that reason).

Copy Quantity is unchanged and there is NO lot cap: FIXED is a quantity in units, MULTIPLIER is Master quantity x multiplier, neither is rounded;
a quantity that is not a whole number of the Child's lots is refused with its reason.
"""
import json
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from angel_helpers import FakeAngel, SYNTHETIC_CONTRACT, make_client, position_row
from app.core.config import settings
from app.models.accounts import Account, CopySetting, QuantityMode
from app.models.strategies import StrategyMember
from app.services.alice_blue.instruments import InstrumentResolutionError
from app.services.alice_blue.types import OrderRequest, OrderResultCategory, OrderSide, OrderType, ProductType
from app.services.angel_one import derivatives as angel_fno
from app.services.angel_one import instruments as angel_instruments
from app.services.angel_one.client import AngelCapabilityUnavailable, AngelOneError
from app.services.angel_one.instruments import AngelInstrumentStore
from app.services.brokers import registry
from app.services.brokers.fno_capabilities import FNO_CHILD_CAPABILITIES, child_gate_open
from app.services.copy_engine import derivative_routing
from app.services.copy_engine.risk_inputs import PositionSafetyError, get_available_long
from app.services.groww import instruments as gi
from app.services.instruments import terms
from app.services.instruments.contracts import ResolutionVerdict, make_contract, resolve_for_broker
from conftest import TestingSessionLocal
from test_alice_cross_broker_resolution import EXPIRY, NOW, store_with
from test_fno_child_validation import alice_event, new_pipeline, skipped
from test_groww_fno_master import fno_store
from test_groww_equity_lifecycle import _setup

ANGEL_IP = "151.245.182.10"
CE_SYMBOL = "NIFTY26NOV2623500CE"          # Angel One's spelling: underlying + DDMMMYY + strike + CE
CE_TOKEN = "71001"
FUT_SYMBOL = "NIFTY26NOV26FUT"
FUT_TOKEN = "71002"
LOT = 75


def rec(symbol=CE_SYMBOL, token=CE_TOKEN, *, name="NIFTY", itype="OPTIDX", expiry="26NOV2026", strike="2350000.000000", lot="75", tick="5.000000"):
    return {"token": token, "symbol": symbol, "name": name, "exch_seg": "NFO", "instrumenttype": itype, "expiry": expiry, "strike": strike,
            "lotsize": lot, "tick_size": tick}


def fut_rec():
    return rec(FUT_SYMBOL, FUT_TOKEN, itype="FUTIDX", strike="-1.000000")


def angel_store(*extra) -> AngelInstrumentStore:
    s = AngelInstrumentStore()
    s.load_records([
        {"token": "3045", "symbol": "SBIN-EQ", "name": "SBIN", "exch_seg": "NSE"},
        rec(), fut_rec(), *extra,
    ])
    s._snapshot_at = datetime.now(timezone.utc)        # what load_file records after ONE complete load; load_records alone leaves freshness unknown
    return s


# ── the scrip master ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def test_a_self_consistent_option_and_future_are_loaded_and_resolved_exactly():
    s = angel_store()
    d = s.derivative(CE_SYMBOL)
    assert (d.token, d.underlying, d.kind, d.expiry, d.strike, d.lot_size, d.tick_size, d.freeze_quantity) == \
           (CE_TOKEN, "NIFTY", "CE", date(2026, 11, 26), Decimal("23500"), LOT, Decimal("0.05"), 0)
    assert s.derivative_by_token(CE_TOKEN).tradingsymbol == CE_SYMBOL and s.derivative(FUT_SYMBOL).strike is None
    assert s.find_derivative("CE", "NIFTY", EXPIRY, Decimal("23500")).token == CE_TOKEN
    assert s.find_derivative("FUT", "NIFTY", EXPIRY, None).token == FUT_TOKEN


@pytest.mark.parametrize("bad", [
    rec(strike="23500.000000"),                      # the strike scale does not agree with the symbol: a wrong assumption loads NOTHING
    rec(expiry="27NOV2026"),                         # expiry field disagrees with the symbol
    rec(expiry="2026-11-26"),                        # an unparseable spelling
    rec(name="BANKNIFTY"),                           # underlying disagrees
    rec(itype="FUTIDX"),                             # an option symbol on a future record
    rec(itype=""),
    rec(lot="0"), rec(lot=""),                       # no usable lot size
    rec(symbol="NIFTY26NOV2623500XX"),               # not CE / PE / FUT
])
def test_a_row_that_disagrees_with_itself_is_not_loaded(bad):
    s = AngelInstrumentStore()
    s.load_records([bad])
    assert s.derivative_count == 0
    with pytest.raises(InstrumentResolutionError):
        s.derivative(bad["symbol"])


def test_a_duplicate_contract_is_ambiguous_and_an_empty_catalog_is_not_loaded():
    s = angel_store(rec(token="71999"))
    with pytest.raises(InstrumentResolutionError, match="ambiguous"):
        s.derivative(CE_SYMBOL)
    with pytest.raises(InstrumentResolutionError, match="ambiguous"):
        s.find_derivative("CE", "NIFTY", EXPIRY, Decimal("23500"))
    with pytest.raises(InstrumentResolutionError, match="not loaded"):
        AngelInstrumentStore().derivative(CE_SYMBOL)


def test_the_equity_path_is_unchanged_and_an_nfo_symbol_is_still_not_an_equity():
    s = angel_store()
    assert s.resolve("NSE", "SBIN") == ("3045", "SBIN-EQ")
    with pytest.raises(InstrumentResolutionError, match="equity only"):
        s.resolve("NFO", CE_SYMBOL)


def test_the_canonical_key_of_an_angel_row_is_the_key_alice_computes_for_the_same_contract(monkeypatch):
    monkeypatch.setattr(terms, "store", store_with())
    d = angel_store().derivative(CE_SYMBOL)
    key = make_contract(d.kind, "NFO", d.underlying, d.expiry, d.strike).key
    assert key == derivative_routing.master_contract_key("alicebluenew", "NFO", "50001")


# ── the route policy: every supported direction ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def stores(monkeypatch):
    monkeypatch.setattr(terms, "store", store_with(loaded_at=datetime.now(timezone.utc)))
    monkeypatch.setattr(gi, "store", fno_store())
    monkeypatch.setattr(angel_instruments, "store", angel_store())
    for name in ("GROWW_FNO_CHILD_ENABLED", "ANGEL_ONE_FNO_CHILD_ENABLED"):
        monkeypatch.setattr(settings, name, True)
    return None


def key_of(symbol=CE_SYMBOL):
    d = angel_store().derivative(symbol)
    return make_contract(d.kind, "NFO", d.underlying, d.expiry, d.strike).key


@pytest.mark.parametrize("master,child", [("alicebluenew", "angelone"), ("groww", "angelone"), ("angelone", "alicebluenew"), ("angelone", "groww")])
def test_every_listed_direction_with_angel_one_resolves_in_the_childs_own_catalog(stores, monkeypatch, master, child):
    monkeypatch.setattr(settings, "CROSS_BROKER_FNO_PAIRS", f"{master}>{child}")
    decision = derivative_routing.route(master, child, "NFO", key_of())
    assert decision.allowed, decision.skip
    if child == "angelone":
        assert decision.child_symbol == CE_SYMBOL and decision.child_token == CE_TOKEN        # Angel One's OWN symbol and token
    elif child == "alicebluenew":
        assert decision.child_token == "50001"                                                  # Alice's own token, never Angel One's
    else:
        assert decision.child_symbol and decision.child_symbol != CE_SYMBOL                    # Groww's own trading symbol


def test_a_direction_that_is_not_listed_or_whose_gate_is_closed_keeps_the_original_refusal(stores, monkeypatch):
    monkeypatch.setattr(settings, "CROSS_BROKER_FNO_PAIRS", "alicebluenew>angelone")
    assert not derivative_routing.route("groww", "angelone", "NFO", key_of()).allowed
    monkeypatch.setattr(settings, "ANGEL_ONE_FNO_CHILD_ENABLED", False)
    closed = derivative_routing.route("alicebluenew", "angelone", "NFO", key_of())
    assert not closed.allowed and "CHILD_GATE_CLOSED:angelone" in closed.skip.detail


def test_an_unloaded_or_stale_or_missing_angel_catalog_is_never_a_route(stores, monkeypatch):
    monkeypatch.setattr(settings, "CROSS_BROKER_FNO_PAIRS", "alicebluenew>angelone")
    monkeypatch.setattr(angel_instruments, "store", AngelInstrumentStore())
    assert resolve_for_broker("angelone", make_contract("CE", "NFO", "NIFTY", EXPIRY, "23500"), now=NOW).verdict is ResolutionVerdict.CATALOG_NOT_LOADED
    assert not derivative_routing.route("alicebluenew", "angelone", "NFO", key_of()).allowed
    s = angel_store()
    got = resolve_for_broker("angelone", make_contract("CE", "NFO", "NIFTY", EXPIRY, "99999"), now=NOW, angel_store=s)
    assert got.verdict in (ResolutionVerdict.STALE, ResolutionVerdict.UNMAPPED)                # never RESOLVED


def test_the_capability_matrix_is_honest_about_angel_one_and_the_gate_is_closed_by_default():
    cap = FNO_CHILD_CAPABILITIES["angelone"]
    assert {cap.place, cap.modify, cap.cancel, cap.reconcile} == {"CODE_ONLY"}
    from app.core.config import Settings
    s = Settings(_env_file=None)
    assert (s.ANGEL_ONE_FNO_CHILD_ENABLED, s.ANGEL_ONE_FNO_MASTER_ENABLED, s.ANGEL_ONE_FNO_QUANTITY_UNIT) == (False, False, "")
    assert not hasattr(s, "FNO_CHILD_MAX_LOTS") and child_gate_open("angelone") is False


# ── the Angel One client ─────────────────────────────────────────────────────────────────────────────────────────────────────

class AngelFno(FakeAngel):
    """FakeAngel plus the order book, modify and cancel."""

    def __init__(self):
        super().__init__()
        self.book = []

    def __call__(self, request):
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "getOrderBook":
            self.requests.append(request)
            return httpx.Response(200, json={"status": True, "data": self.book})
        if name in ("modifyOrder", "cancelOrder"):
            self.requests.append(request)
            return httpx.Response(200, json={"status": True, "message": "SUCCESS", "data": {"orderid": json.loads(request.content).get("orderid")}})
        return super().__call__(request)


def book_row(*, oid="ANG-1", symbol=CE_SYMBOL, token=CE_TOKEN, qty="75", filled="0", status="syn_open", side="BUY", product="CARRYFORWARD"):
    return {"orderid": oid, "tradingsymbol": symbol, "symboltoken": token, "exchange": "NFO", "transactiontype": side, "ordertype": "LIMIT",
            "producttype": product, "price": "2.35", "triggerprice": "0", "quantity": qty, "filledshares": filled, "unfilledshares": str(int(qty) - int(filled)),
            "averageprice": "2.35" if int(filled) else "0", "status": status, "text": ""}


@pytest.fixture
def fno_open(monkeypatch):
    monkeypatch.setattr(settings, "ANGEL_ONE_FNO_CHILD_ENABLED", True)
    monkeypatch.setattr(settings, "ANGEL_ONE_FNO_MASTER_ENABLED", True)
    monkeypatch.setattr(settings, "ANGEL_ONE_FNO_QUANTITY_UNIT", "UNITS")
    monkeypatch.setattr(settings, "SELL_POSITION_SAFETY_BYPASS", False)


async def client_for(mock_redis, fake=None):
    fake = fake or AngelFno()
    client = make_client(mock_redis, fake, store=angel_store())
    await client.login()
    return fake, client


def order(qty=75, *, side=OrderSide.BUY, product=ProductType.NRML, otype=OrderType.LIMIT, price="2.35", trigger="0", token="99999"):
    return OrderRequest(symbol=CE_SYMBOL, exchange="NFO", side=side, quantity=qty, product_type=product, order_type=otype, price=Decimal(price),
                        trigger_price=Decimal(trigger), client_order_id="JT_1", instrument_token=token)


@pytest.mark.asyncio
async def test_a_buy_is_sent_with_angel_ones_own_symbol_token_exchange_and_carry_product(mock_redis, fno_open):
    fake, client = await client_for(mock_redis)
    res = await client.place_order(order())
    (body,) = fake.order_bodies()
    assert res.category == OrderResultCategory.SUCCESS
    assert (body["tradingsymbol"], body["symboltoken"], body["exchange"], body["producttype"], body["transactiontype"], body["quantity"], body["price"]) == \
           (CE_SYMBOL, CE_TOKEN, "NFO", "CARRYFORWARD", "BUY", "75", "2.35") and "99999" not in json.dumps(body)         # the Master broker's token never crosses


@pytest.mark.asyncio
@pytest.mark.parametrize("product,expected", [(ProductType.NRML, "CARRYFORWARD"), (ProductType.CNC, "CARRYFORWARD"), (ProductType.MIS, "INTRADAY")])
async def test_the_f_and_o_products_are_carry_or_intraday_never_delivery_or_margin(mock_redis, fno_open, product, expected):
    fake, client = await client_for(mock_redis)
    await client.place_order(order(product=product))
    assert fake.order_bodies()[0]["producttype"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("otype,trigger,variety,ordertype", [(OrderType.SL, "2.0", "STOPLOSS", "STOPLOSS_LIMIT"), (OrderType.SL_M, "2.0", "STOPLOSS", "STOPLOSS_MARKET"),
                                                                (OrderType.MARKET, "0", "NORMAL", "MARKET")])
async def test_every_order_type_maps_for_f_and_o(mock_redis, fno_open, otype, trigger, variety, ordertype):
    fake, client = await client_for(mock_redis)
    assert (await client.place_order(order(otype=otype, trigger=trigger))).category == OrderResultCategory.SUCCESS
    assert (fake.order_bodies()[0]["variety"], fake.order_bodies()[0]["ordertype"]) == (variety, ordertype)


@pytest.mark.asyncio
@pytest.mark.parametrize("qty", [150, 75 * 20])
async def test_there_is_no_lot_cap_any_whole_number_of_lots_is_sent_exactly(mock_redis, fno_open, qty):
    fake, client = await client_for(mock_redis)
    assert (await client.place_order(order(qty))).category == OrderResultCategory.SUCCESS and fake.order_bodies()[0]["quantity"] == str(qty)


@pytest.mark.asyncio
@pytest.mark.parametrize("qty,why", [(1, "whole number of lots"), (74, "whole number of lots"), (76, "whole number of lots"), (0, "whole number of lots")])
async def test_an_invalid_lot_quantity_is_refused_with_its_reason_and_never_rounded(mock_redis, fno_open, qty, why):
    fake, client = await client_for(mock_redis)
    res = await client.place_order(order(qty))
    assert res.category == OrderResultCategory.REJECTED and why in res.message and fake.order_bodies() == []


@pytest.mark.asyncio
async def test_the_quantity_unit_has_no_default_and_lots_sends_the_lot_count(mock_redis, fno_open, monkeypatch):
    monkeypatch.setattr(settings, "ANGEL_ONE_FNO_QUANTITY_UNIT", "")
    fake, client = await client_for(mock_redis)
    res = await client.place_order(order(150))
    assert res.category == OrderResultCategory.REJECTED and "unit is not configured" in res.message and fake.order_bodies() == []
    monkeypatch.setattr(settings, "ANGEL_ONE_FNO_QUANTITY_UNIT", "LOTS")
    assert (await client.place_order(order(150))).category == OrderResultCategory.SUCCESS and fake.order_bodies()[0]["quantity"] == "2"


@pytest.mark.asyncio
async def test_with_the_gate_closed_an_f_and_o_order_is_refused_exactly_as_before(mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "ANGEL_ONE_FNO_CHILD_ENABLED", False)
    fake, client = await client_for(mock_redis)
    res = await client.place_order(order())
    assert res.category == OrderResultCategory.REJECTED and "equity only" in res.message and fake.order_bodies() == []


@pytest.mark.asyncio
async def test_a_freeze_quantity_is_enforced_only_when_the_catalog_states_one(mock_redis, fno_open, monkeypatch):
    fake, client = await client_for(mock_redis)
    d = client._instruments.derivative(CE_SYMBOL)
    monkeypatch.setitem(client._instruments._derivatives, CE_SYMBOL, [type(d)(**{**d.__dict__, "freeze_quantity": 1800})])
    assert "freeze quantity" in (await client.place_order(order(75 * 25))).message and fake.order_bodies() == []
    assert (await client.place_order(order(75 * 24))).category == OrderResultCategory.SUCCESS


@pytest.mark.asyncio
async def test_modify_and_cancel_an_f_and_o_order(mock_redis, fno_open):
    fake, client = await client_for(mock_redis)
    ok = await client.modify_order("ANG-1", 150, Decimal("2.40"), symbol=CE_SYMBOL, exchange="NFO", product_type=ProductType.NRML)
    body = json.loads(fake.calls("modifyOrder")[0].content)
    assert ok.category == OrderResultCategory.SUCCESS and (body["orderid"], body["quantity"], body["price"], body["producttype"], body["symboltoken"]) == \
           ("ANG-1", "150", "2.40", "CARRYFORWARD", CE_TOKEN) and "transactiontype" not in body
    bad = await client.modify_order("ANG-1", 100, Decimal("2.40"), symbol=CE_SYMBOL, exchange="NFO", product_type=ProductType.NRML)
    assert bad.category == OrderResultCategory.REJECTED and len(fake.calls("modifyOrder")) == 1                    # not a lot multiple: nothing sent
    assert (await client.cancel_order("ANG-1", CE_SYMBOL, "NFO")).category == OrderResultCategory.SUCCESS


# ── the order book (reconciliation of status and fills) ──────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_f_and_o_order_is_read_in_units_with_the_canonical_contract_key(mock_redis, fno_open):
    fake, client = await client_for(mock_redis)
    fake.book = [book_row(qty="150", filled="75", status="syn_part")]
    (r,) = await client.get_order_book()
    assert (r.symbol, r.exchange, r.quantity, r.filled_quantity, r.status, r.instrument_id) == (CE_SYMBOL, "NFO", 150, 75, "PARTIAL", CE_TOKEN)
    assert r.contract_key == key_of()


@pytest.mark.asyncio
async def test_lots_are_multiplied_out_and_a_row_that_cannot_be_read_is_unsupported_not_guessed(mock_redis, fno_open, monkeypatch):
    fake, client = await client_for(mock_redis)
    monkeypatch.setattr(settings, "ANGEL_ONE_FNO_QUANTITY_UNIT", "LOTS")
    fake.book = [book_row(qty="2", filled="1", status="syn_part")]
    assert [(r.quantity, r.filled_quantity) for r in await client.get_order_book()] == [(150, 75)]
    monkeypatch.setattr(settings, "ANGEL_ONE_FNO_QUANTITY_UNIT", "UNITS")
    fake.book = [book_row(qty="74"), book_row(oid="X2", symbol="NIFTY26NOV2699999CE", token="1")]
    assert [r.status for r in await client.get_order_book()] == ["UNSUPPORTED_FNO_QUANTITY", "UNSUPPORTED_FNO_INSTRUMENT"]
    monkeypatch.setattr(settings, "ANGEL_ONE_FNO_QUANTITY_UNIT", "")
    fake.book = [book_row()]
    assert [r.status for r in await client.get_order_book()] == ["UNSUPPORTED_FNO_UNIT"]


@pytest.mark.asyncio
async def test_the_master_key_is_attached_only_with_the_master_gate_and_cash_rows_are_untouched(mock_redis, fno_open, monkeypatch):
    fake, client = await client_for(mock_redis)
    cash = {**book_row(oid="C1", symbol="SBIN-EQ", token="3045", qty="10"), "exchange": "NSE", "producttype": "INTRADAY"}
    fake.book = [book_row(), cash]
    monkeypatch.setattr(settings, "ANGEL_ONE_FNO_MASTER_ENABLED", False)
    nfo, eq = await client.get_order_book()
    assert nfo.contract_key is None and (eq.symbol, eq.quantity, eq.contract_key) == ("SBIN-EQ", 10, None)
    monkeypatch.setattr(settings, "ANGEL_ONE_FNO_CHILD_ENABLED", False)
    monkeypatch.setattr(settings, "ANGEL_ONE_FNO_MASTER_ENABLED", False)
    nfo, _ = await client.get_order_book()
    assert nfo.contract_key is None and nfo.status == "OPEN"                                    # every F&O gate closed: the row is exactly what it was


# ── the position book and the SELL guard ─────────────────────────────────────────────────────────────────────────────────────────

def fno_pos(net="75", *, product="CARRYFORWARD", token=CE_TOKEN, symbol=CE_SYMBOL):
    return position_row(symbol=symbol, token=token, exchange="NFO", product=product, net=net, avg="2.35")


async def available(client, product="LONGTERM"):
    return await get_available_long(client, "NFO", CE_SYMBOL, product)


@pytest.mark.asyncio
async def test_an_f_and_o_long_is_found_through_the_same_guard_alice_blue_uses(mock_redis, fno_open):
    fake, client = await client_for(mock_redis)
    fake.positions = [fno_pos("150")]
    assert await available(client) == 150                                                       # LONGTERM / NRML / CARRYFORWARD are one carry holding on NFO


@pytest.mark.asyncio
@pytest.mark.parametrize("positions", [[], [fno_pos("-75")], [fno_pos("75", product="INTRADAY")], [fno_pos("75", token="71002", symbol=FUT_SYMBOL)]],
                         ids=["flat", "short", "other-product", "other-contract"])
async def test_a_sell_has_no_available_long_when_flat_short_or_in_another_product_or_contract(mock_redis, fno_open, positions):
    fake, client = await client_for(mock_redis)
    fake.positions = positions
    assert await available(client) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("prepare", [
    lambda f: setattr(f, "positions_response", (500, {"status": False})),                      # the book cannot be read
    lambda f: setattr(f, "positions", [fno_pos("74")]),                                         # not a whole number of lots
    lambda f: setattr(f, "positions", [fno_pos("75", token="1", symbol="NIFTY26NOV2699999CE")]),   # an open position the scrip master does not hold
], ids=["unreadable", "not-lots", "unknown-instrument"])
async def test_an_f_and_o_book_that_cannot_be_read_with_confidence_fails_closed(mock_redis, fno_open, prepare):
    fake, client = await client_for(mock_redis)
    prepare(fake)
    assert await client.get_positions_or_none() is None
    with pytest.raises(PositionSafetyError):
        await available(client)


@pytest.mark.asyncio
async def test_an_unconfigured_unit_makes_an_open_f_and_o_position_unknown_but_a_flat_one_is_fine(mock_redis, fno_open, monkeypatch):
    fake, client = await client_for(mock_redis)
    monkeypatch.setattr(settings, "ANGEL_ONE_FNO_QUANTITY_UNIT", "")
    fake.positions = [fno_pos("0")]
    assert [p.quantity for p in await client.get_positions()] == [0]
    fake.positions = [fno_pos("75")]
    assert await client.get_positions_or_none() is None


@pytest.mark.asyncio
async def test_lots_are_multiplied_out_in_the_position_book(mock_redis, fno_open, monkeypatch):
    fake, client = await client_for(mock_redis)
    monkeypatch.setattr(settings, "ANGEL_ONE_FNO_QUANTITY_UNIT", "LOTS")
    fake.positions = [fno_pos("2")]
    assert await available(client) == 150


@pytest.mark.asyncio
async def test_with_the_child_gate_closed_the_position_book_is_exactly_what_it_was(mock_redis, fno_open, monkeypatch):
    monkeypatch.setattr(settings, "ANGEL_ONE_FNO_CHILD_ENABLED", False)
    fake, client = await client_for(mock_redis)
    fake.positions = [fno_pos("74"), position_row()]                                            # would be unreadable with the gate open
    assert sorted(p.quantity for p in await client.get_positions()) == [10, 74]


# ── the copy pipeline: Alice Blue Master -> Angel One Child, FIXED and MULTIPLIER ───────────────────────────────────────────────

@pytest.fixture
def angel_route(monkeypatch, fno_open):
    monkeypatch.setattr(terms, "store", store_with(loaded_at=datetime.now(timezone.utc)))
    monkeypatch.setattr(angel_instruments, "store", angel_store())
    for name, value in (("ANGEL_ONE_ENABLED", True), ("ANGEL_ONE_CHILD_TRADING_ENABLED", True)):
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr(settings, "CANARY_PAIRS", "alicebluenew>angelone")
    monkeypatch.setattr(settings, "CROSS_BROKER_FNO_PAIRS", "alicebluenew>angelone")
    monkeypatch.setattr(registry, "ANGEL_CONTRACT", SYNTHETIC_CONTRACT)


async def angel_child(db_session, mode=QuantityMode.MULTIPLIER, fixed=1, multiplier="1"):
    master, child = await _setup(db_session)
    async with TestingSessionLocal() as s:
        row = await s.get(Account, child.id)
        row.broker, row.static_ip_address = "angelone", ANGEL_IP
        cs = (await s.execute(select(CopySetting).where(CopySetting.follower_account_id == child.id))).scalar_one()
        cs.quantity_mode, cs.fixed_qty = mode, fixed
        m = (await s.execute(select(StrategyMember).where(StrategyMember.account_id == child.id))).scalar_one()
        m.multiplier = Decimal(multiplier)
        await s.commit()
    return master, child


async def send(db_session, mock_redis, *, event_kwargs=None, fake=None, oid="A-1", **policy):
    master, child = await angel_child(db_session, **policy)
    fake = fake or AngelFno()
    client = make_client(mock_redis, fake, store=angel_store(), account_id=str(child.id))
    await client.login()
    res = await new_pipeline(mock_redis).execute_replication(event=alice_event(master, oid, **(event_kwargs or {})), clients={str(child.id): client})
    return fake, child, (res.get("results") or {}).get(str(child.id))


@pytest.mark.asyncio
async def test_an_alice_f_and_o_order_is_placed_at_angel_one_with_angel_ones_own_contract(db_session, mock_redis, angel_route):
    fake, child, out = await send(db_session, mock_redis)
    (body,) = fake.order_bodies()
    assert out["status"] == "SUCCESS" and (body["tradingsymbol"], body["symboltoken"], body["exchange"], body["producttype"], body["quantity"]) == \
           (CE_SYMBOL, CE_TOKEN, "NFO", "CARRYFORWARD", "75") and "50001" not in json.dumps(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("fixed,master_qty,sent", [(75, 75, "75"), (75, 750, "75"), (150, 75, "150"), (1500, 75, "1500")])
async def test_fixed_is_the_exact_quantity_in_units(db_session, mock_redis, angel_route, fixed, master_qty, sent):
    fake, child, out = await send(db_session, mock_redis, mode=QuantityMode.FIXED, fixed=fixed, event_kwargs={"qty": master_qty})
    assert out["status"] == "SUCCESS" and [b["quantity"] for b in fake.order_bodies()] == [sent]


@pytest.mark.asyncio
@pytest.mark.parametrize("fixed", [1, 74, 76, 100])
async def test_a_fixed_quantity_that_is_not_a_whole_number_of_lots_is_refused_never_rounded(db_session, mock_redis, angel_route, fixed):
    fake, child, out = await send(db_session, mock_redis, mode=QuantityMode.FIXED, fixed=fixed)
    assert fake.order_bodies() == [] and any("FNO_QUANTITY_NOT_LOT_MULTIPLE" in r for r in await skipped(child))


@pytest.mark.asyncio
@pytest.mark.parametrize("mult,master_qty,sent", [("1", 75, "75"), ("2", 75, "150"), ("1.5", 150, "225"), ("0.5", 150, "75")])
async def test_multiplier_is_master_quantity_times_the_multiplier(db_session, mock_redis, angel_route, mult, master_qty, sent):
    fake, child, out = await send(db_session, mock_redis, multiplier=mult, event_kwargs={"qty": master_qty})
    assert out["status"] == "SUCCESS" and [b["quantity"] for b in fake.order_bodies()] == [sent]


@pytest.mark.asyncio
@pytest.mark.parametrize("mult,master_qty", [("0.5", 75), ("0.75", 150), ("1.01", 150), ("1.5", 75)])
async def test_a_multiplier_that_gives_a_fractional_lot_is_refused_never_floored(db_session, mock_redis, angel_route, mult, master_qty):
    fake, child, out = await send(db_session, mock_redis, multiplier=mult, event_kwargs={"qty": master_qty})
    assert fake.order_bodies() == [] and any("FNO_QUANTITY_NOT_LOT_MULTIPLE" in r for r in await skipped(child))


@pytest.mark.asyncio
async def test_the_per_child_risk_limits_apply_to_an_angel_one_f_and_o_order(db_session, mock_redis, angel_route):
    master, child = await angel_child(db_session)
    async with TestingSessionLocal() as s:
        cs = (await s.execute(select(CopySetting).where(CopySetting.follower_account_id == child.id))).scalar_one()
        cs.max_trade_value = Decimal("1")
        await s.commit()
    fake = AngelFno()
    client = make_client(mock_redis, fake, store=angel_store(), account_id=str(child.id))
    await client.login()
    res = await new_pipeline(mock_redis).execute_replication(event=alice_event(master, "R-1"), clients={str(child.id): client})
    assert fake.order_bodies() == [] and "MAX_TRADE_VALUE_BREACHED" in json.dumps(res, default=str)


@pytest.mark.asyncio
async def test_a_sell_needs_a_confirmed_long_and_is_then_sent(db_session, mock_redis, angel_route):
    fake, child, out = await send(db_session, mock_redis, event_kwargs={"side": "SELL"})
    assert fake.order_bodies() == [] and (out is None or out["status"] != "SUCCESS")              # flat book: refused
    held = AngelFno()
    held.positions = [fno_pos("75")]
    fake, child, out = await send(db_session, mock_redis, event_kwargs={"side": "SELL"}, fake=held, oid="A-2")
    assert out["status"] == "SUCCESS" and held.order_bodies()[0]["transactiontype"] == "SELL"


@pytest.mark.asyncio
async def test_with_the_f_and_o_child_gate_closed_nothing_is_sent_to_angel_one(db_session, mock_redis, angel_route, monkeypatch):
    monkeypatch.setattr(settings, "ANGEL_ONE_FNO_CHILD_ENABLED", False)
    fake, child, out = await send(db_session, mock_redis)
    assert fake.order_bodies() == [] and any("CHILD_GATE_CLOSED:angelone" in r for r in await skipped(child))
