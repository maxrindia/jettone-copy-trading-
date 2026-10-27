"""SELL-position guard with a Zerodha Child (offline): resolved with Zerodha's own dump, matched on tradingsymbol + exchange + product,
never on another broker's token, and unknown positions fail closed."""
import pytest

from app.services.alice_blue.instruments import InstrumentResolutionError
from app.services.copy_engine.risk_inputs import PositionFetchError, get_available_long
from zerodha_helpers import ALICE_STYLE_TOKEN, FakeKite, ok, position_row, seed_session, make_client


async def ready(redis):
    fake = FakeKite()
    c = make_client(redis, fake)
    await seed_session(redis)
    return c, fake


@pytest.mark.asyncio
async def test_long_position_is_found_by_symbol_exchange_and_product_ignoring_the_master_token(mock_redis):
    c, fake = await ready(mock_redis)
    fake.positions_response = ok({"net": [position_row(qty=10, product="CNC")], "day": []})
    assert await get_available_long(c, "NSE", "SBIN-EQ", "CNC", provided_token=ALICE_STYLE_TOKEN) == 10


@pytest.mark.asyncio
async def test_other_product_exchange_or_symbol_never_counts(mock_redis):
    c, fake = await ready(mock_redis)
    fake.positions_response = ok({"net": [position_row(qty=10, product="MIS"), position_row(symbol="TCS", qty=7, product="CNC"),
                                          position_row(exchange="BSE", qty=4, product="CNC")], "day": []})
    assert await get_available_long(c, "NSE", "SBIN", "CNC") == 0


@pytest.mark.asyncio
async def test_short_or_flat_licenses_nothing(mock_redis):
    c, fake = await ready(mock_redis)
    fake.positions_response = ok({"net": [position_row(qty=-5, product="CNC")], "day": []})
    assert await get_available_long(c, "NSE", "SBIN", "CNC") == 0


@pytest.mark.asyncio
async def test_unreadable_position_book_fails_closed(mock_redis):
    c, fake = await ready(mock_redis)
    fake.positions_response = ok({"day": []})
    with pytest.raises(PositionFetchError):
        await get_available_long(c, "NSE", "SBIN", "CNC")
    fake.session_rejected = True
    with pytest.raises(PositionFetchError):
        await get_available_long(c, "NSE", "SBIN", "CNC")


@pytest.mark.asyncio
async def test_unknown_instrument_fails_closed_before_any_position_read(mock_redis):
    c, fake = await ready(mock_redis)
    with pytest.raises(InstrumentResolutionError):
        await get_available_long(c, "NSE", "NOSUCH", "CNC")
    assert not fake.calls("/portfolio/positions")


@pytest.mark.asyncio
async def test_a_bare_master_underlying_never_matches_a_zerodha_derivative_position(mock_redis):
    c, fake = await ready(mock_redis)
    fake.positions_response = ok({"net": [position_row(symbol="NIFTY26NOV23500CE", exchange="NFO", product="NRML", qty=75)], "day": []})
    with pytest.raises(InstrumentResolutionError):
        await get_available_long(c, "NFO", "NIFTY", "NRML", provided_token="74056")      # Master spelling is not a Zerodha symbol
    assert await get_available_long(c, "NFO", "NIFTY26NOV23500CE", "NRML") == 75
