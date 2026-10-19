"""
Groww identifiers: the order reference and the instrument identity are the two values that cross the wire
and that Jettone derives itself. Both must be valid by Groww's documented rules (order_reference_id: 8-20
alphanumeric, at most two hyphens) and impossible to influence with hostile input. Offline: mocked transport.
"""
import hashlib
import re
from decimal import Decimal

import pytest

from app.services.alice_blue.instruments import InstrumentResolutionError
from app.services.alice_blue.types import OrderRequest, OrderResultCategory, OrderSide, OrderType, ProductType
from app.services.groww.client import REFERENCE_PREFIX, order_reference_for
from app.services.groww.instruments import GrowwInstrumentStore
import groww_helpers as G

DOC_RULE = re.compile(r"^(?=(?:[^-]*-){0,2}[^-]*$)[A-Za-z0-9-]{8,20}$")     # 8-20 alphanumeric, at most two hyphens
STRICT = re.compile(r"^[A-Za-z0-9]{20}$")                                    # what Jettone actually produces


def pipeline_ids():
    """Every shape of client_order_id the copy pipeline creates (LIMIT, SL, MARKET per fill), incl. very long ones."""
    out = []
    for m in ("2610190001234", "M" * 40, "x" * 300, "a-b_c", "ÄÖÜ-ünïcode-订单"):
        for a in ("0f0e9d4c", "ffffffff"):
            out += [f"JT_LIM_{m[:16]}_{a}", f"JT_SL_{m[:16]}_{a}"]
            out += [f"JT__{m[:24]}__{a}__{n}" for n in (1, 7, 65)]
    return out


@pytest.mark.parametrize("cid", pipeline_ids())
def test_every_pipeline_client_order_id_yields_a_valid_groww_reference(cid):
    ref = order_reference_for(cid)
    assert STRICT.match(ref) and DOC_RULE.match(ref) and ref.startswith(REFERENCE_PREFIX)
    assert ref == order_reference_for(cid)                                    # deterministic: an ambiguous order can be looked up


@pytest.mark.parametrize("cid", ["", " ", "\n", "../../etc/passwd", "a" * 5000, "'; DROP TABLE orders;--", "\x00", "💥"])
def test_hostile_or_degenerate_ids_still_yield_a_valid_reference(cid):
    ref = order_reference_for(cid)
    assert STRICT.match(ref)


def test_references_do_not_collide_across_orders_and_fills():
    ids = [f"JT__{m:024d}__{a:08x}__{n}" for m in range(40) for a in range(5) for n in range(1, 6)]
    refs = {order_reference_for(i) for i in ids}
    assert len(refs) == len(ids)


def test_reference_is_a_one_way_digest_of_the_id():
    cid = "JT_LIM_ORDER123_acc12345"
    assert order_reference_for(cid) == "JT" + hashlib.sha256(cid.encode()).hexdigest()[:18]
    assert cid not in order_reference_for(cid)                                # no account or order id leaks to the broker


def test_a_missing_id_gets_a_fresh_valid_reference_each_time():
    a, b = order_reference_for(None), order_reference_for(None)
    assert STRICT.match(a) and STRICT.match(b) and a != b


# ── instrument identity ──────────────────────────────────────────────────────────────────────

def order(**over):
    base = dict(symbol="SBIN", exchange="NSE", side=OrderSide.BUY, quantity=5, product_type=ProductType.MIS,
                order_type=OrderType.LIMIT, price=Decimal("800.5"), instrument_token=G.ALICE_STYLE_TOKEN,
                client_order_id="JT_LIM_IDENT_acc12345")
    base.update(over)
    return OrderRequest(**base)


HOSTILE_SYMBOLS = ["SBIN\nINFY", "SBIN/../INFY", 'SBIN"', "SBIN;", "SBIN INFY", "SB\x00IN", "ＳＢＩＮ", "SBIN-EQ-EQ", "-EQ", "", "   ",
                   "SBIN%20", "S" * 500, "SBIN-BE", "NIFTY24AUG24500CE"]


@pytest.mark.parametrize("symbol", HOSTILE_SYMBOLS)
def test_hostile_or_unknown_symbols_never_resolve(symbol):
    with pytest.raises(InstrumentResolutionError):
        G.make_store().resolve("NSE", symbol)


@pytest.mark.parametrize("exchange", ["NFO", "MCX", "CDS", "", None, "NSE;", "N SE", "NSEX"])
def test_non_equity_or_malformed_exchanges_never_resolve(exchange):
    with pytest.raises(InstrumentResolutionError):
        G.make_store().resolve(exchange, "SBIN")


@pytest.mark.parametrize("symbol,exchange", [(" sbin-eq ", " nse "), ("Sbin", "Nse"), ("SBIN-EQ", "NSE")])
def test_accepted_spellings_resolve_to_the_masters_own_identity(symbol, exchange):
    inst = G.make_store().resolve(exchange, symbol)
    assert (inst.exchange, inst.trading_symbol, inst.isin) == ("NSE", "SBIN", G.SBIN_ISIN)


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", HOSTILE_SYMBOLS)
async def test_a_hostile_symbol_is_refused_before_any_request_leaves(mock_redis, symbol):
    fake = G.FakeGroww()
    client = G.make_client(mock_redis, fake)
    await client.login()
    res = await client.place_order(order(symbol=symbol))
    assert res.category == OrderResultCategory.REJECTED and res.broker_order_id is None
    assert fake.calls("/v1/order/create") == [] and fake.calls("/v1/order/status/reference") == []


@pytest.mark.asyncio
async def test_the_wire_symbol_is_the_masters_not_the_callers(mock_redis):
    fake = G.FakeGroww()
    client = G.make_client(mock_redis, fake)
    await client.login()
    await client.place_order(order(symbol=" sbin-eq "))
    body = fake.bodies("/v1/order/create")[0]
    assert body["trading_symbol"] == "SBIN" and body["exchange"] == "NSE"
    assert STRICT.match(body["order_reference_id"])


def test_duplicate_master_rows_are_ambiguous_and_refused():
    s = GrowwInstrumentStore()
    row = {"exchange": "NSE", "trading_symbol": "DUPL", "instrument_type": "EQ", "segment": "CASH", "isin": "INE000000001",
           "lot_size": "1", "freeze_quantity": "100", "buy_allowed": "1", "sell_allowed": "1"}
    s.load_rows([row, dict(row, isin="INE000000002")])
    with pytest.raises(InstrumentResolutionError, match="ambiguous"):
        s.resolve("NSE", "DUPL")


@pytest.mark.parametrize("bad", [
    {"segment": "FNO"}, {"instrument_type": "IDX"}, {"instrument_type": "FUT"}, {"exchange": "MCX"}, {"trading_symbol": ""},
])
def test_non_equity_master_rows_are_never_loaded(bad):
    s = GrowwInstrumentStore()
    row = {"exchange": "NSE", "trading_symbol": "ROWX", "instrument_type": "EQ", "segment": "CASH", "isin": "INE000000009",
           "lot_size": "1", "freeze_quantity": "0", "buy_allowed": "1", "sell_allowed": "1"}
    row.update(bad)
    assert s.load_rows([row]) == 0 and s.size == 0


@pytest.mark.parametrize("buy,sell", [("", ""), ("maybe", "0"), (None, None), ("0", "0")])
@pytest.mark.asyncio
async def test_unreadable_or_disallowed_trade_flags_refuse_the_order(mock_redis, buy, sell):
    s = GrowwInstrumentStore()
    s.load_rows([{"exchange": "NSE", "trading_symbol": "SBIN", "instrument_type": "EQ", "segment": "CASH", "isin": G.SBIN_ISIN,
                  "lot_size": "1", "freeze_quantity": "0", "buy_allowed": buy, "sell_allowed": sell}])
    fake = G.FakeGroww()
    client = G.make_client(mock_redis, fake, store=s)
    await client.login()
    for side in (OrderSide.BUY, OrderSide.SELL):
        res = await client.place_order(order(side=side))
        assert res.category == OrderResultCategory.REJECTED
    assert fake.calls("/v1/order/create") == []
