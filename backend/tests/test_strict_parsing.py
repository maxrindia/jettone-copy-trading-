"""
Fail-closed numeric parsing for the Groww and Angel One adapters.

A value either parses to a finite number meeting its field's rules or the response is refused with the
adapter's typed failure - never TypeError / OverflowError / InvalidOperation, and never a silent zero.
Offline: in-process parsers, mocked transports, the real pipeline.
"""
from decimal import Decimal

import pytest

from app.services.alice_blue.types import OrderSide
from app.services.angel_one.client import AngelCapabilityUnavailable, AngelOneClient
from app.services.brokers.strict_numbers import (
    StrictNumberError, optional_decimal, strict_decimal, strict_int,
)
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.groww.client import GrowwCapabilityUnavailable
from test_groww_sell_safety import groww_enabled, _groww_child, CHILD_IP as GROWW_IP, _run as run_groww, _buy, _sell, _positions, _reasons  # noqa: F401
from test_angel_one_sell_safety import angel_enabled, _angel_child, CHILD_IP as ANGEL_IP, _run as run_angel, _buy as angel_buy, _reasons as angel_reasons  # noqa: F401
import angel_helpers as A
import groww_helpers as G

# Values that must never be accepted as a number for a required field.
BAD_ANY = [
    ("none", None), ("empty", ""), ("blank", "   "), ("nan_text", "NaN"), ("inf_text", "Infinity"),
    ("neg_inf_text", "-Infinity"), ("true", True), ("false", False), ("word", "abc"), ("list", []), ("dict", {}),
    ("huge_exp", "1e999999999"), ("overflowing", "1e999"), ("float_nan", float("nan")), ("float_inf", float("inf")),
    ("float_neg_inf", float("-inf")), ("huge_int", 10 ** 30), ("double_minus", "--5"),
]
FRACTIONAL = [("fractional", 1.5), ("fractional_text", "2.25")]
NEGATIVE = [("negative", -1), ("negative_text", "-3")]
ZERO = [("zero", 0), ("zero_text", "0")]
GOOD_FORMS = [10, "10", " 10 ", 10.0, Decimal("10")]


# ── the helper itself ───────────────────────────────────────────────────────

@pytest.mark.parametrize("name,bad", BAD_ANY, ids=[n for n, _ in BAD_ANY])
def test_strict_decimal_refuses_every_bad_value_with_its_own_typed_error(name, bad):
    with pytest.raises(StrictNumberError) as ei:
        strict_decimal(bad, "field")
    assert ei.value.field == "field" and "field" in str(ei.value)


@pytest.mark.parametrize("name,bad", BAD_ANY + FRACTIONAL, ids=[n for n, _ in BAD_ANY + FRACTIONAL])
def test_strict_int_refuses_bad_and_fractional_values(name, bad):
    with pytest.raises(StrictNumberError):
        strict_int(bad, "qty")


def test_strict_helpers_accept_real_numbers_in_every_json_form():
    for v in GOOD_FORMS:
        assert strict_decimal(v, "x") == Decimal(10) and strict_int(v, "x") == 10
    assert strict_decimal("-5.5", "x") == Decimal("-5.5")
    assert strict_decimal(800.55, "x") == Decimal("800.55")                # floats go through repr: no binary noise


def test_error_messages_never_contain_the_offending_value():
    for bad in ("SECRET-LOOKING-9876", "9" * 40, "Infinity"):
        with pytest.raises(StrictNumberError) as ei:
            strict_decimal(bad, "price")
        assert bad not in str(ei.value)


def test_sign_and_range_rules():
    with pytest.raises(StrictNumberError):
        strict_decimal(0, "p", positive=True)
    with pytest.raises(StrictNumberError):
        strict_decimal(-1, "p", allow_negative=False)
    with pytest.raises(StrictNumberError):
        strict_int(11, "q", maximum=10)
    with pytest.raises(StrictNumberError):
        strict_int(-1, "q", minimum=0)
    assert strict_int(0, "q", minimum=0) == 0


def test_optional_decimal_distinguishes_not_applicable_from_garbage():
    assert optional_decimal({}, "price") == 0 and optional_decimal({"price": None}, "price") == 0
    assert optional_decimal({"price": "12.5"}, "price") == Decimal("12.5")
    for garbage in ("", "  ", "NaN", "abc", True, -1):                     # present but invalid: never read as 0
        with pytest.raises(StrictNumberError):
            optional_decimal({"price": garbage}, "price")
    with pytest.raises(StrictNumberError):                                 # applicable: required, strict, positive
        optional_decimal({}, "price", applicable=True)
    with pytest.raises(StrictNumberError):
        optional_decimal({"price": None}, "price", applicable=True)
    with pytest.raises(StrictNumberError):
        optional_decimal({"price": 0}, "price", applicable=True)


# ── Groww parsers ───────────────────────────────────────────────────────────

@pytest.fixture
def gc(mock_redis):
    return G.make_client(mock_redis, G.FakeGroww())


def must_refuse(call, exc=GrowwCapabilityUnavailable):
    with pytest.raises(exc) as ei:
        call()
    assert type(ei.value) is exc                                           # the documented typed failure, nothing else
    return ei.value


GROWW_ORDER = lambda **kw: G.order_row("1", kw.pop("status", "OPEN"), kw.pop("qty", 10), kw.pop("filled", 4), **kw)


@pytest.mark.parametrize("name,bad", BAD_ANY + FRACTIONAL + NEGATIVE + ZERO, ids=lambda x: x if isinstance(x, str) else "")
def test_groww_order_quantity_is_strict(gc, name, bad):
    must_refuse(lambda: gc.parse_order_row({**GROWW_ORDER(), "quantity": bad}))


@pytest.mark.parametrize("name,bad", BAD_ANY + FRACTIONAL + NEGATIVE, ids=lambda x: x if isinstance(x, str) else "")
def test_groww_filled_quantity_is_strict(gc, name, bad):
    must_refuse(lambda: gc.parse_order_row({**GROWW_ORDER(), "filled_quantity": bad}))


def test_groww_filled_quantity_cannot_exceed_the_order(gc):
    must_refuse(lambda: gc.parse_order_row({**GROWW_ORDER(), "filled_quantity": 11}))
    assert gc.parse_order_row({**GROWW_ORDER(status="EXECUTED", filled=10), "filled_quantity": 10}).filled_quantity == 10


@pytest.mark.parametrize("name,bad", BAD_ANY + NEGATIVE + ZERO, ids=lambda x: x if isinstance(x, str) else "")
def test_groww_a_fills_average_price_is_required_and_positive(gc, name, bad):
    must_refuse(lambda: gc.parse_order_row({**GROWW_ORDER(status="EXECUTED", filled=10), "average_fill_price": bad}))


@pytest.mark.parametrize("name,bad", BAD_ANY + NEGATIVE + ZERO, ids=lambda x: x if isinstance(x, str) else "")
def test_groww_a_limit_orders_price_is_required_and_positive(gc, name, bad):
    must_refuse(lambda: gc.parse_order_row({**GROWW_ORDER(), "price": bad}))


@pytest.mark.parametrize("name,bad", BAD_ANY + NEGATIVE + ZERO, ids=lambda x: x if isinstance(x, str) else "")
def test_groww_a_stop_orders_trigger_is_required_and_positive(gc, name, bad):
    must_refuse(lambda: gc.parse_order_row({**GROWW_ORDER(otype="SL_M", status="TRIGGER_PENDING", filled=0), "trigger_price": bad}))


@pytest.mark.parametrize("key", ["quantity", "filled_quantity", "order_status", "transaction_type", "groww_order_id", "order_type"])
def test_groww_missing_required_order_fields_are_refused(gc, key):
    row = GROWW_ORDER()
    del row[key]
    must_refuse(lambda: gc.parse_order_row(row))


def test_groww_unapplicable_fields_may_be_absent_or_null_but_not_garbage(gc):
    row = GROWW_ORDER(otype="MARKET", price=0, filled=0)                    # MARKET: no price/trigger/average fill price
    for k in ("price", "trigger_price", "average_fill_price"):
        row[k] = None
    assert gc.parse_order_row(row).price == 0
    for k in ("price", "trigger_price", "average_fill_price"):
        del row[k]
    assert gc.parse_order_row(row).trigger_price == 0
    for garbage in ("", "NaN", "abc", True, -1):
        must_refuse(lambda g=garbage: gc.parse_order_row({**row, "trigger_price": g}))


def test_groww_non_object_rows_are_refused(gc):
    for row in (None, [], "x", 5):
        must_refuse(lambda r=row: gc.parse_order_row(r))
        must_refuse(lambda r=row: gc.parse_position_row(r))


@pytest.mark.parametrize("name,bad", BAD_ANY + FRACTIONAL, ids=lambda x: x if isinstance(x, str) else "")
def test_groww_position_quantity_is_strict(gc, name, bad):
    must_refuse(lambda: gc.parse_position_row({**G.position_row(), "quantity": bad}))


@pytest.mark.parametrize("name,bad", BAD_ANY + NEGATIVE + ZERO, ids=lambda x: x if isinstance(x, str) else "")
def test_groww_an_open_positions_price_is_required_and_positive(gc, name, bad):
    must_refuse(lambda: gc.parse_position_row({**G.position_row(qty=10), "net_price": bad}))


def test_groww_a_closed_positions_price_may_be_absent_or_null_but_not_garbage(gc):
    closed = {k: v for k, v in G.position_row(qty=0).items() if k != "net_price"}
    assert gc.parse_position_row(closed).quantity == 0
    assert gc.parse_position_row({**closed, "net_price": None}).average_price == 0
    must_refuse(lambda: gc.parse_position_row({**closed, "net_price": "NaN"}))


@pytest.mark.parametrize("key", ["quantity", "net_price", "trading_symbol", "product", "exchange"])
def test_groww_missing_required_position_fields_are_refused(gc, key):
    row = G.position_row(qty=10)
    del row[key]
    must_refuse(lambda: gc.parse_position_row(row))


def test_groww_a_short_position_is_legitimate(gc):
    assert gc.parse_position_row(G.position_row(qty=-4)).quantity == -4


# ── Groww funds and the modify guard ────────────────────────────────────────

async def groww_funds(redis, payload):
    fake = G.FakeGroww()
    fake.margin_response = G.ok(payload)
    c = G.make_client(redis, fake)
    await c.login()
    return await c.get_funds()


GOOD_MARGIN = {"clear_cash": 5000.5, "net_margin_used": 100, "collateral_available": 250}


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["clear_cash", "net_margin_used", "collateral_available"])
@pytest.mark.parametrize("name,bad", BAD_ANY, ids=[n for n, _ in BAD_ANY])
async def test_groww_every_margin_field_is_strict(mock_redis, field, name, bad):
    with pytest.raises(GrowwCapabilityUnavailable) as ei:
        await groww_funds(mock_redis, {**GOOD_MARGIN, field: bad})
    assert type(ei.value) is GrowwCapabilityUnavailable


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["clear_cash", "net_margin_used", "collateral_available"])
async def test_groww_missing_margin_fields_are_refused(mock_redis, field):
    payload = {k: v for k, v in GOOD_MARGIN.items() if k != field}
    with pytest.raises(GrowwCapabilityUnavailable):
        await groww_funds(mock_redis, payload)


@pytest.mark.asyncio
async def test_groww_margin_sign_rules(mock_redis):
    assert (await groww_funds(mock_redis, {**GOOD_MARGIN, "clear_cash": -250})).available_margin == Decimal("-250")
    for field in ("net_margin_used", "collateral_available"):
        with pytest.raises(GrowwCapabilityUnavailable):
            await groww_funds(mock_redis, {**GOOD_MARGIN, field: -1})


@pytest.mark.asyncio
@pytest.mark.parametrize("name,bad", BAD_ANY + FRACTIONAL + NEGATIVE, ids=lambda x: x if isinstance(x, str) else "")
async def test_groww_modify_guard_refuses_unusable_filled_quantity_without_leaking_an_untyped_error(mock_redis, name, bad):
    fake = G.FakeGroww()
    fake.detail_response = G.ok({**G.order_row("GRW1", "OPEN", 10, 0), "filled_quantity": bad})
    c = G.make_client(mock_redis, fake)
    await c.login()
    res = await c.modify_order("GRW1", 7, Decimal("801"), symbol="SBIN")   # must return, never raise
    assert res.category.value == "REJECTED"
    assert fake.calls("/v1/order/modify") == []


@pytest.mark.asyncio
async def test_groww_modify_guard_missing_filled_quantity_is_refused(mock_redis):
    fake = G.FakeGroww()
    detail = G.order_row("GRW1", "OPEN", 10, 0)
    del detail["filled_quantity"]
    fake.detail_response = G.ok(detail)
    c = G.make_client(mock_redis, fake)
    await c.login()
    assert (await c.modify_order("GRW1", 7, Decimal("801"), symbol="SBIN")).category.value == "REJECTED"
    assert fake.calls("/v1/order/modify") == []


# ── Angel One parsers ───────────────────────────────────────────────────────

ANGEL_ROW = {"orderid": "9", "tradingsymbol": "SBIN-EQ", "symboltoken": "3045", "exchange": "NSE", "transactiontype": "SELL",
             "ordertype": "LIMIT", "producttype": "INTRADAY", "price": "800", "triggerprice": "0", "quantity": "10",
             "filledshares": "4", "unfilledshares": "6", "averageprice": "799.5", "status": "syn_part", "text": ""}
SM = A.SYNTHETIC_CONTRACT.status_map


def parse_angel(**over):
    return AngelOneClient.parse_order_row({**ANGEL_ROW, **over}, SM)


def angel_refuse(call):
    with pytest.raises(AngelCapabilityUnavailable) as ei:
        call()
    assert type(ei.value) is AngelCapabilityUnavailable


@pytest.mark.parametrize("name,bad", BAD_ANY + FRACTIONAL + NEGATIVE + ZERO, ids=lambda x: x if isinstance(x, str) else "")
def test_angel_order_quantity_is_strict(name, bad):
    angel_refuse(lambda: parse_angel(quantity=bad))


@pytest.mark.parametrize("name,bad", BAD_ANY + FRACTIONAL + NEGATIVE, ids=lambda x: x if isinstance(x, str) else "")
def test_angel_filled_shares_is_strict(name, bad):
    angel_refuse(lambda: parse_angel(filledshares=bad))


def test_angel_filled_shares_cannot_exceed_the_order():
    angel_refuse(lambda: parse_angel(filledshares="11"))


@pytest.mark.parametrize("name,bad", BAD_ANY + NEGATIVE + ZERO, ids=lambda x: x if isinstance(x, str) else "")
def test_angel_a_fills_average_price_is_required_and_positive(name, bad):
    angel_refuse(lambda: parse_angel(averageprice=bad))


@pytest.mark.parametrize("name,bad", BAD_ANY + NEGATIVE + ZERO, ids=lambda x: x if isinstance(x, str) else "")
def test_angel_a_limit_orders_price_is_required_and_positive(name, bad):
    angel_refuse(lambda: parse_angel(price=bad))


@pytest.mark.parametrize("key", ["quantity", "filledshares", "orderid", "transactiontype", "status", "price", "averageprice"])
def test_angel_missing_required_order_fields_are_refused(key):
    row = dict(ANGEL_ROW)
    del row[key]
    angel_refuse(lambda: AngelOneClient.parse_order_row(row, SM))


def test_angel_non_object_rows_are_refused():
    for row in (None, [], "x", 5):
        angel_refuse(lambda r=row: AngelOneClient.parse_order_row(r, SM))


@pytest.fixture
def ac(mock_redis):
    return A.make_client(mock_redis, A.FakeAngel())


KEYS = A.SYNTHETIC_CONTRACT.positions


@pytest.mark.parametrize("name,bad", BAD_ANY + FRACTIONAL, ids=lambda x: x if isinstance(x, str) else "")
def test_angel_position_quantity_is_strict(ac, name, bad):
    angel_refuse(lambda: ac.parse_position_row({**A.position_row(), KEYS.net_qty: bad}, KEYS))


@pytest.mark.parametrize("name,bad", BAD_ANY + NEGATIVE + ZERO, ids=lambda x: x if isinstance(x, str) else "")
def test_angel_an_open_positions_price_is_required_and_positive(ac, name, bad):
    angel_refuse(lambda: ac.parse_position_row({**A.position_row(net="10"), KEYS.avg_price: bad}, KEYS))


def test_angel_a_closed_position_and_a_short_are_handled(ac):
    closed = {k: v for k, v in A.position_row(net="0").items() if k != KEYS.avg_price}
    assert ac.parse_position_row(closed, KEYS).quantity == 0
    assert ac.parse_position_row(A.position_row(net="-4"), KEYS).quantity == -4


async def angel_funds(redis, rms):
    fake = A.FakeAngel(rms=rms)
    fake.routes["getRMS"] = (200, {"status": True, "data": rms})
    c = A.make_client(redis, fake)
    await c.login()
    return await c.get_funds()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["syn_avail", "syn_used", "syn_coll"])
@pytest.mark.parametrize("name,bad", BAD_ANY, ids=[n for n, _ in BAD_ANY])
async def test_angel_every_funds_field_is_strict(mock_redis, field, name, bad):
    good = {"syn_avail": "5000", "syn_used": "100", "syn_coll": "250"}
    with pytest.raises(AngelCapabilityUnavailable) as ei:
        await angel_funds(mock_redis, {**good, field: bad})
    assert type(ei.value) is AngelCapabilityUnavailable


# ── pipeline level: malformed data fails closed and no child order is created ─────

@pytest.mark.asyncio
@pytest.mark.parametrize("row", [
    {**G.position_row(qty=10), "quantity": None},
    {**G.position_row(qty=10), "quantity": "NaN"},
    {**G.position_row(qty=10), "net_price": None},
    {**G.position_row(qty=10), "net_price": "Infinity"},
], ids=["qty_null", "qty_nan", "price_null", "price_infinity"])
async def test_pipeline_groww_malformed_positions_reject_a_sell_with_no_order(db_session, mock_redis, groww_enabled, row):
    master, child = await _groww_child(db_session)
    fake = G.FakeGroww()
    fake.positions_response = G.ok({"positions": [row]})
    out, _ = await run_groww(mock_redis, master, child, fake, _sell(master, "STRICT-S1"))
    assert out["status"] == "REJECTED" and fake.bodies("/v1/order/create") == []
    assert {"POSITION_FETCH_FAILED", "RISK_INPUTS_UNAVAILABLE"} & set(await _reasons(child))


@pytest.mark.asyncio
@pytest.mark.parametrize("margin", [
    {"clear_cash": "Infinity", "net_margin_used": 0, "collateral_available": 0},
    {"clear_cash": "NaN", "net_margin_used": 0, "collateral_available": 0},
    {"clear_cash": None, "net_margin_used": 0, "collateral_available": 0},
    {"clear_cash": 1000000, "net_margin_used": "abc", "collateral_available": 0},
    {"clear_cash": 1000000, "net_margin_used": 0},
], ids=["cash_infinity", "cash_nan", "cash_null", "used_text", "collateral_missing"])
async def test_pipeline_groww_malformed_margin_rejects_a_buy_with_no_order(db_session, mock_redis, groww_enabled, margin):
    master, child = await _groww_child(db_session)
    fake = G.FakeGroww()
    fake.positions_response = _positions()
    fake.margin_response = G.ok(margin)
    out, _ = await run_groww(mock_redis, master, child, fake, _buy(master, "STRICT-B1"))
    assert out["status"] == "REJECTED" and fake.bodies("/v1/order/create") == []


@pytest.mark.asyncio
async def test_pipeline_groww_a_stored_fill_without_a_price_does_not_become_a_zero_price(db_session, mock_redis, groww_enabled):
    """Order-book rows feed fill tracking: a filled order with no usable price is refused outright."""
    master, child = await _groww_child(db_session)
    fake = G.FakeGroww()
    c = G.make_client(mock_redis, fake, account_id=str(child.id), static_ip=GROWW_IP)
    await c.login()
    fake.order_list_response = G.ok({"order_list": [{**G.order_row("1", "EXECUTED", 10, 10), "average_fill_price": None}]})
    with pytest.raises(GrowwCapabilityUnavailable):
        await c.get_order_book()


@pytest.mark.asyncio
@pytest.mark.parametrize("rms", [
    {"syn_avail": "Infinity", "syn_used": "0", "syn_coll": "0"},
    {"syn_avail": None, "syn_used": "0", "syn_coll": "0"},
], ids=["infinity", "null"])
async def test_pipeline_angel_malformed_funds_reject_a_buy_with_no_order(db_session, mock_redis, angel_enabled, rms):
    master, child = await _angel_child(db_session)
    fake = A.FakeAngel()
    fake.routes["getPosition"] = (200, {"status": True, "data": []})
    fake.routes["getRMS"] = (200, {"status": True, "data": rms})
    out, _ = await run_angel(mock_redis, master, child, fake, angel_buy(master, "STRICT-A1"))
    assert out["status"] == "REJECTED" and fake.order_bodies() == []
