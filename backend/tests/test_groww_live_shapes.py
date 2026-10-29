"""
The Groww response shapes confirmed by the read-only live capture of 2026-10-29 (empty account, production proxy).

Offline: the mocked transport in groww_helpers answers with the EXACT captured shapes - field names and JSON types only; every
number here is a placeholder, never a real balance. What the capture did NOT show (order or position rows, the status
vocabulary, a full page, an error body, any order operation) is not asserted as verified here.
"""
from decimal import Decimal

import pytest

from app.services.groww import contract as C
from app.services.groww.client import GrowwCapabilityUnavailable
from groww_helpers import ok
from test_groww_client import logged_in

# The captured margin payload: every field name the capture saw, with placeholder values of the captured JSON type.
LIVE_MARGIN = {
    "clear_cash": 1.5, "collateral_available": 0, "net_margin_used": 0.0,
    "adhoc_margin": 0, "brokerage_and_charges": 0.0, "collateral_used": 0,
    "commodity_margin_details": {}, "equity_margin_details": {}, "fno_margin_details": {},
}


def test_the_contract_records_the_live_shapes_without_claiming_live_verification():
    assert C.LIVE_CAPTURE_DATE == "2026-10-29"
    assert set(C.LIVE_VERIFIED_SHAPES) == {"order_list", "positions", "margin"}
    assert C.MARGIN_FUNDS_FIELDS == ("clear_cash", "collateral_available", "net_margin_used")
    assert set(LIVE_MARGIN) == set(C.MARGIN_FUNDS_FIELDS + C.MARGIN_OTHER_NUMERIC_FIELDS + C.MARGIN_OBJECT_FIELDS)
    assert C.GROWW_CONTRACT.live_verified is False and C.GROWW_CONTRACT.master_verified is False   # rows, statuses, paging unseen


# ── empty lists are successful, empty answers ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_captured_empty_order_list_is_an_empty_book(mock_redis):
    c, fake = await logged_in(mock_redis)
    fake.order_list_response = ok({"order_list": []})
    assert await c.get_order_book() == []


@pytest.mark.asyncio
async def test_the_captured_flat_positions_are_flat_not_unknown(mock_redis):
    c, fake = await logged_in(mock_redis)
    fake.positions_response = ok({"positions": []})
    assert await c.get_positions() == []
    assert await c.get_positions_or_none() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    ok({}), ok({"order_list": None}), ok({"order_list": {}}), ok({"order_list": "[]"}),
    (200, {"status": "FAILURE", "payload": {"order_list": []}}), (200, {"payload": {"order_list": []}}),
    (500, {"status": "SUCCESS", "payload": {"order_list": []}}), (200, {"status": "SUCCESS", "payload": []}),
])
async def test_an_order_list_that_is_not_the_captured_shape_is_refused(mock_redis, bad):
    c, fake = await logged_in(mock_redis)
    fake.order_list_response = bad
    with pytest.raises(Exception):
        await c.get_order_book()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    ok({}), ok({"positions": None}), ok({"positions": {}}), ok({"positions": "[]"}),
    (200, {"status": "FAILURE", "payload": {"positions": []}}), (200, {"payload": {"positions": []}}),
    (500, {"status": "SUCCESS", "payload": {"positions": []}}),
])
async def test_positions_that_are_not_the_captured_shape_are_unknown_never_flat(mock_redis, bad):
    c, fake = await logged_in(mock_redis)
    fake.positions_response = bad
    assert await c.get_positions_or_none() is None


# ── funds: the three live-verified numbers, strictly ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_captured_margin_shape_parses(mock_redis):
    c, fake = await logged_in(mock_redis)
    fake.margin_response = ok(dict(LIVE_MARGIN))
    f = await c.get_funds()
    assert (f.available_margin, f.collateral, f.used_margin) == (Decimal("1.5"), Decimal("0"), Decimal("0.0"))


@pytest.mark.asyncio
@pytest.mark.parametrize("field", C.MARGIN_FUNDS_FIELDS)
@pytest.mark.parametrize("value,why", [
    (None, "null"), (True, "boolean"), ("5000", "numeric string - the live field is a JSON number"), ("abc", "text"),
    ({}, "object"), ([], "list"), (float("nan"), "NaN"), (float("inf"), "infinite"),
])
async def test_a_funds_field_that_is_not_a_finite_json_number_refuses(mock_redis, field, value, why):
    c, fake = await logged_in(mock_redis)
    fake.margin_response = ok({**LIVE_MARGIN, field: value})
    with pytest.raises(GrowwCapabilityUnavailable):
        await c.get_funds()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", C.MARGIN_FUNDS_FIELDS)
async def test_a_missing_funds_field_refuses(mock_redis, field):
    c, fake = await logged_in(mock_redis)
    fake.margin_response = ok({k: v for k, v in LIVE_MARGIN.items() if k != field})
    with pytest.raises(GrowwCapabilityUnavailable):
        await c.get_funds()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["collateral_available", "net_margin_used"])
async def test_negative_collateral_or_used_margin_refuses_but_a_negative_cash_balance_is_real(mock_redis, field):
    c, fake = await logged_in(mock_redis)
    fake.margin_response = ok({**LIVE_MARGIN, field: -1})
    with pytest.raises(GrowwCapabilityUnavailable):
        await c.get_funds()
    fake.margin_response = ok({**LIVE_MARGIN, "clear_cash": -1})
    assert (await c.get_funds()).available_margin == Decimal("-1")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    (200, {"status": "FAILURE", "payload": dict(LIVE_MARGIN)}), (500, {"status": "SUCCESS", "payload": dict(LIVE_MARGIN)}),
    (200, {"status": "SUCCESS", "payload": None}), (200, {"status": "SUCCESS"}), (200, {}),
])
async def test_a_malformed_margin_envelope_refuses(mock_redis, bad):
    c, fake = await logged_in(mock_redis)
    fake.margin_response = bad
    with pytest.raises(Exception):
        await c.get_funds()


@pytest.mark.asyncio
async def test_the_other_margin_fields_are_not_required(mock_redis):
    """Only MARGIN_FUNDS_FIELDS are read; the rest of the captured payload may be absent without failing the read."""
    c, fake = await logged_in(mock_redis)
    fake.margin_response = ok({k: LIVE_MARGIN[k] for k in C.MARGIN_FUNDS_FIELDS})
    assert (await c.get_funds()).available_margin == Decimal("1.5")
