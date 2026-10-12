"""
Production fix coverage: resolve_instrument_info() must never hand back a
non-numeric value (e.g. the raw symbol "NIFTY") as if it were Alice Blue's
numeric instrumentId. Production evidence: a real master NFO order with
symbol "NIFTY" (no strike/expiry - the master's own order-book record
apparently carried only the bare underlying name) produced a copied child
order rejected with "Invalid 'instrumentId'. It must contain only numeric
characters." because resolve_instrument_info's old final fallback returned
the symbol string itself.

No live trading is enabled and no real broker calls are made anywhere in
this file - the broker HTTP client is always an AsyncMock test double, and
Redis is the in-memory mock_redis fixture.
"""
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.instruments import InstrumentResolutionError, resolve_instrument_info
from app.services.alice_blue.types import OrderRequest, OrderResultCategory, OrderSide, OrderType, ProductType


# ---------------------------------------------------------------------------
# resolve_instrument_info: static table + fail-closed fallback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nse_equity_resolves_via_known_instruments(mock_redis):
    token, trading_sym = await resolve_instrument_info(mock_redis, "NSE", "RELIANCE")
    assert token == "2885"
    assert token.isdigit()
    assert trading_sym == "RELIANCE-EQ"


@pytest.mark.asyncio
async def test_bse_equity_resolves_without_eq_suffix(mock_redis):
    """BSE symbols never carry the NSE "-EQ" convention - must stay unchanged."""
    token, trading_sym = await resolve_instrument_info(mock_redis, "BSE", "RELIANCE")
    assert token == "500325"
    assert trading_sym == "RELIANCE"


@pytest.mark.asyncio
async def test_nse_index_symbol_resolves_without_eq_suffix(mock_redis):
    token, trading_sym = await resolve_instrument_info(mock_redis, "NSE", "NIFTY")
    assert token == "26000"
    assert trading_sym == "NIFTY"


@pytest.mark.asyncio
async def test_nfo_hardcoded_nifty_option_strike_resolves(mock_redis):
    """A strike that DOES exist in the static KNOWN_INSTRUMENTS table
    resolves to its real numeric token - the table is legitimate for the
    strikes it actually has (requirement: don't break existing valid
    fallback)."""
    token, trading_sym = await resolve_instrument_info(mock_redis, "NFO", "NIFTY 24500 CE")
    assert token == "45100"
    assert token.isdigit()


@pytest.mark.asyncio
async def test_nfo_hardcoded_banknifty_option_strike_resolves(mock_redis):
    token, trading_sym = await resolve_instrument_info(mock_redis, "NFO", "BANKNIFTY 51500 CE")
    assert token == "46100"
    assert token.isdigit()


@pytest.mark.asyncio
async def test_nfo_bare_nifty_symbol_fails_closed_not_returned_as_token(mock_redis):
    """THE production bug: a bare "NIFTY" on NFO (no strike/expiry - not a
    real tradable contract identifier) is not in the static table and not
    cached. Must raise, never return "NIFTY" itself as if numeric."""
    with pytest.raises(InstrumentResolutionError) as exc_info:
        await resolve_instrument_info(mock_redis, "NFO", "NIFTY")
    assert "NIFTY" in str(exc_info.value)


@pytest.mark.asyncio
async def test_nfo_unlisted_strike_fails_closed(mock_redis):
    """A real, currently-tradable strike that simply isn't one of the
    static table's example rows (e.g. a strike far from the hardcoded
    24500/24600/24700 examples) must also fail closed, not silently
    resolve to a wrong/adjacent token."""
    with pytest.raises(InstrumentResolutionError):
        await resolve_instrument_info(mock_redis, "NFO", "NIFTY 25500 CE")


@pytest.mark.asyncio
async def test_finnifty_option_fails_closed(mock_redis):
    """FINNIFTY has ZERO entries anywhere in the static table - every
    FINNIFTY option must fail closed, not just the ones missing a strike
    match."""
    with pytest.raises(InstrumentResolutionError) as exc_info:
        await resolve_instrument_info(mock_redis, "NFO", "FINNIFTY 21000 CE")
    assert "FINNIFTY" in str(exc_info.value)


@pytest.mark.asyncio
async def test_nfo_future_contract_fails_closed_when_unlisted(mock_redis):
    with pytest.raises(InstrumentResolutionError):
        await resolve_instrument_info(mock_redis, "NFO", "TCS FUT")


@pytest.mark.asyncio
async def test_redis_cache_hit_resolves_a_real_nfo_contract(mock_redis):
    """Proves the fix's intended path: once load_and_cache_contract_master
    has populated Redis for NFO (this test simulates that having already
    happened), a real live contract not in the tiny static table resolves
    correctly via cache - exactly what engine_loop.py's new NFO auto-sync
    is meant to make happen in production."""
    await mock_redis.set("alice:instrument:NFO:NIFTY25SEP26000CE", "987654:NIFTY25SEP26000CE")
    token, trading_sym = await resolve_instrument_info(mock_redis, "NFO", "NIFTY25SEP26000CE")
    assert token == "987654"
    assert token.isdigit()
    assert trading_sym == "NIFTY25SEP26000CE"


@pytest.mark.asyncio
async def test_explicit_provided_token_bypasses_resolution_entirely(mock_redis):
    """An explicitly supplied token is trusted directly - no lookup at all,
    regardless of symbol/exchange."""
    token, trading_sym = await resolve_instrument_info(mock_redis, "NFO", "ANYTHING", provided_token="123456")
    assert token == "123456"


# ---------------------------------------------------------------------------
# AliceBlueClient.place_order: fails closed before ever contacting the broker
# ---------------------------------------------------------------------------

def _client(mock_redis, mock_http):
    return AliceBlueClient(
        account_id="acc-test", user_id="AB00101", api_secret="secret",
        redis=mock_redis, http_client=mock_http,
    )


@pytest.mark.asyncio
async def test_place_order_rejects_unresolvable_nfo_instrument_without_calling_broker(mock_redis):
    """The exact production scenario: a child copy of a master NFO order
    whose symbol is just "NIFTY". Must be REJECTED locally - the broker's
    HTTP endpoint must never even be called."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
    client = _client(mock_redis, mock_http)

    req = OrderRequest(
        symbol="NIFTY",
        exchange="NFO",
        side=OrderSide.BUY,
        quantity=65,
        product_type=ProductType.MIS,
        order_type=OrderType.LIMIT,
        price=Decimal("2.00"),
        client_order_id="CLIENT_NFO_FAIL",
    )

    result = await client.place_order(req)
    assert result.category == OrderResultCategory.REJECTED
    assert result.broker_order_id is None
    assert "instrument" in result.message.lower()
    mock_http.request.assert_not_called()


@pytest.mark.asyncio
async def test_place_order_succeeds_for_resolvable_nfo_instrument(mock_redis):
    """A strike that DOES resolve (via the static table) must still place
    normally, with the real numeric instrumentId in the payload - the fix
    must not break legitimate NFO orders."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")

    resp = AsyncMock()
    resp.status_code = 200
    resp.json = lambda: [
        {"status": "Ok", "message": "Success", "result": [{"status": "OK", "brokerOrderId": "NFO123", "orderNo": "NFO123"}]}
    ]

    called_payload = []

    async def fake_request(method, url, **kwargs):
        called_payload.append(kwargs.get("json", []))
        return resp

    mock_http.request = AsyncMock(side_effect=fake_request)
    client = _client(mock_redis, mock_http)

    req = OrderRequest(
        symbol="NIFTY 24500 CE",
        exchange="NFO",
        side=OrderSide.BUY,
        quantity=65,
        product_type=ProductType.MIS,
        order_type=OrderType.LIMIT,
        price=Decimal("2.00"),
        client_order_id="CLIENT_NFO_OK",
    )

    result = await client.place_order(req)
    assert result.category == OrderResultCategory.SUCCESS
    assert called_payload[0][0]["instrumentId"] == "45100"
    assert called_payload[0][0]["instrumentId"].isdigit()


@pytest.mark.asyncio
async def test_place_order_defends_against_a_non_numeric_cached_token(mock_redis):
    """Defense in depth: even a value that DID resolve (e.g. a corrupted
    Redis cache entry) must be numeric or place_order refuses to send it -
    this does not rely solely on resolve_instrument_info's own fallback
    fix."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
    await mock_redis.set("alice:instrument:NFO:BADCACHE", "NOT_A_NUMBER:BADCACHE")
    client = _client(mock_redis, mock_http)

    req = OrderRequest(
        symbol="BADCACHE", exchange="NFO", side=OrderSide.BUY, quantity=1,
        product_type=ProductType.MIS, order_type=OrderType.MARKET,
        client_order_id="CLIENT_BAD_CACHE",
    )

    result = await client.place_order(req)
    assert result.category == OrderResultCategory.REJECTED
    mock_http.request.assert_not_called()
