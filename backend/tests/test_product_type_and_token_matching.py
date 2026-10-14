"""
Phase 2 of the 2026-09-23 root-cause investigation: two confirmed fixes.

FIX 1 - AliceBlueClient.place_order() product-type bug.
ProductType is declared `class ProductType(str, Enum)`, so
`isinstance(order.product_type, str)` is True for EVERY enum member - not
just genuine raw strings. This made a redundant second reclassification
block run unconditionally, computing `str(ProductType.CNC).upper()` ==
"PRODUCTTYPE.CNC" (Python's default Enum str(), never the bare value
"CNC"), matching no key in its own lookup table, and silently falling back
to "INTRADAY". Confirmed live in production: a master CNC BUY was copied to
the follower as an INTRADAY order (verified via the exact outgoing payload
logged and the broker's own order-book record for that broker_order_id).
The SELL-safety gate was NOT at fault - the follower genuinely never held
a CNC position, because the BUY itself was never actually placed as one.

FIX 2 - get_available_long() token-based position matching. Confirmed live:
for one real NFO option, the ORDER book reports tradingSymbol "NIFTY" while
the POSITION book reports "NIFTY29SEP26C23900" for the exact same contract
- but instrumentId ("74056") is identical and stable across both endpoints.
get_available_long() now matches PRIMARILY on (exchange, instrument_id,
normalized_product) when both the resolved query token and a position
row's own instrument_id are available, falling back to the original
(exchange, canonical_symbol, normalized_product) matching only when either
side lacks a token. Product and exchange must still match exactly in both
modes - token identity alone is never sufficient.

No live trading is enabled and no real broker calls are made anywhere in
this file - every HTTP client is a mocked httpx.AsyncClient / AsyncMock,
and Redis is the in-memory mock_redis fixture.
"""
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.types import (
    OrderRequest,
    OrderResultCategory,
    OrderSide,
    OrderType,
    Position,
    ProductType,
)
from app.services.copy_engine.risk_inputs import PositionFetchError, get_available_long


def _client(mock_redis, mock_http):
    return AliceBlueClient(
        account_id="acc-test", user_id="AB00101", api_secret="secret",
        redis=mock_redis, http_client=mock_http,
    )


async def _placed_product(mock_redis, product_type, symbol="RELIANCE", exchange="NSE"):
    """Places one order through the real place_order() code path and
    returns the exact 'product' field it sent to the broker."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")

    resp = AsyncMock()
    resp.status_code = 200
    resp.json = lambda: [
        {"status": "Ok", "message": "Success", "result": [{"status": "OK", "brokerOrderId": "B1", "orderNo": "B1"}]}
    ]
    called_payload = []

    async def fake_request(method, url, **kwargs):
        called_payload.append(kwargs.get("json", []))
        return resp

    mock_http.request = AsyncMock(side_effect=fake_request)
    client = _client(mock_redis, mock_http)

    req = OrderRequest(
        symbol=symbol, exchange=exchange, side=OrderSide.BUY, quantity=1,
        product_type=product_type, order_type=OrderType.LIMIT,
        price=Decimal("100.00"), client_order_id="CLIENT_1",
    )
    result = await client.place_order(req)
    assert result.category == OrderResultCategory.SUCCESS
    return called_payload[0][0]["product"], called_payload[0][0]["productType"]


# ---------------------------------------------------------------------------
# FIX 1: ProductType.* must reach the broker payload correctly
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_product_type_cnc_reaches_broker_as_longterm(mock_redis):
    """THE exact production incident: a positional/delivery BUY must be
    sent as LONGTERM, never silently downgraded to INTRADAY."""
    product, product_type_field = await _placed_product(mock_redis, ProductType.CNC)
    assert product == "LONGTERM"
    assert product_type_field == "LONGTERM"


@pytest.mark.asyncio
async def test_product_type_nrml_reaches_broker_as_mtf(mock_redis):
    product, _ = await _placed_product(mock_redis, ProductType.NRML)
    assert product == "MTF"


@pytest.mark.asyncio
async def test_product_type_mis_still_reaches_broker_as_intraday(mock_redis):
    """The one case that happened to already work correctly - must remain
    unchanged by the fix."""
    product, _ = await _placed_product(mock_redis, ProductType.MIS)
    assert product == "INTRADAY"


@pytest.mark.asyncio
async def test_product_type_enum_never_falls_back_to_the_old_buggy_string_path(mock_redis):
    """Direct proof the enum branch is now authoritative: str(ProductType.CNC)
    would produce 'PRODUCTTYPE.CNC' if the old string-classification block
    ran on it - confirm that value is NEVER what gets sent."""
    product, _ = await _placed_product(mock_redis, ProductType.CNC)
    assert product != "PRODUCTTYPE.CNC"
    assert product != "INTRADAY"  # the bug's silent fallback
    assert product == "LONGTERM"


@pytest.mark.asyncio
async def test_raw_string_product_type_is_still_supported_for_compatibility(mock_redis):
    """No production caller currently passes a raw string (all four
    OrderRequest construction sites convert to a ProductType enum first),
    but the fallback string-classification branch is preserved for any
    caller that genuinely does - a real string 'CNC' must still resolve to
    LONGTERM via that branch, not be broken by the enum-first fix."""
    product, _ = await _placed_product(mock_redis, "CNC")
    assert product == "LONGTERM"


# ---------------------------------------------------------------------------
# FIX 2: token-based position matching in get_available_long()
# ---------------------------------------------------------------------------

def _nifty_position(quantity=65, product_type="NRML", instrument_id="74056",
                    symbol="NIFTY29SEP26C23900", exchange="NFO"):
    return Position(
        symbol=symbol, quantity=quantity, side="BUY",
        average_price=Decimal("6.70"), account_id="acc",
        exchange=exchange, product_type=product_type, instrument_id=instrument_id,
    )


def _client_with_positions(mock_redis, positions):
    client = AsyncMock()
    client.redis = mock_redis
    client.get_positions_or_none.return_value = positions
    return client


@pytest.mark.asyncio
async def test_token_match_finds_position_despite_different_symbol_spelling(mock_redis):
    """THE exact production case: order-book symbol 'NIFTY', position-book
    symbol 'NIFTY29SEP26C23900', same instrument_id '74056'. Symbol-only
    matching would find nothing; token matching must find the real qty."""
    client = _client_with_positions(mock_redis, [_nifty_position(quantity=65)])

    available = await get_available_long(
        client=client, exchange="NFO", symbol="NIFTY",
        product_type="NRML", provided_token="74056",
    )
    assert available == 65


@pytest.mark.asyncio
async def test_same_token_different_product_does_not_match(mock_redis):
    """The NRML position must NOT license a SELL requested under MIS, even
    though the instrument_id is identical - product separation is never
    bypassed by token matching."""
    client = _client_with_positions(mock_redis, [_nifty_position(product_type="NRML")])

    available = await get_available_long(
        client=client, exchange="NFO", symbol="NIFTY",
        product_type="MIS", provided_token="74056",
    )
    assert available == 0


@pytest.mark.asyncio
async def test_nfo_carry_order_product_matches_the_nrml_position(mock_redis):
    """Changed 2026-10-14 (was: ..._cnc_also_does_not_match). On NFO there is no
    delivery: a LONGTERM/CNC order IS the NRML position Alice Blue reports
    (confirmed live: order `product: LONGTERM`, position `product: NRML`, same
    instrumentId). Treating them as different made a covered SELL look naked."""
    client = _client_with_positions(mock_redis, [_nifty_position(product_type="NRML")])

    for requested in ("CNC", "LONGTERM"):
        available = await get_available_long(
            client=client, exchange="NFO", symbol="NIFTY",
            product_type=requested, provided_token="74056",
        )
        assert available == 65, requested


@pytest.mark.asyncio
async def test_same_token_same_product_different_exchange_does_not_match(mock_redis):
    """Exchange is checked independently of - and before - identity
    matching, in both token and symbol modes."""
    client = _client_with_positions(
        mock_redis, [_nifty_position(product_type="NRML", exchange="BFO")]
    )

    available = await get_available_long(
        client=client, exchange="NFO", symbol="NIFTY",
        product_type="NRML", provided_token="74056",
    )
    assert available == 0


@pytest.mark.asyncio
async def test_symbol_fallback_still_works_when_no_instrument_id_available(mock_redis):
    """Existing equity behavior, unchanged: when a position row carries no
    instrument_id at all, matching must fall back to symbol comparison -
    exactly as it did before this fix."""
    position = Position(
        symbol="RELIANCE-EQ", quantity=10, side="BUY",
        average_price=Decimal("2900.00"), account_id="acc",
        exchange="NSE", product_type="MIS", instrument_id=None,
    )
    client = _client_with_positions(mock_redis, [position])

    available = await get_available_long(
        client=client, exchange="NSE", symbol="RELIANCE", product_type="MIS",
    )
    assert available == 10


@pytest.mark.asyncio
async def test_symbol_fallback_used_when_query_has_no_provided_token_but_resolves_one(mock_redis):
    """Even without provided_token, resolve_instrument_info still resolves
    a token from the static table for a known equity - this must not
    accidentally break matching against a row that legitimately shares
    that resolved token (RELIANCE, both order-book and position-book
    spellings normalize to the same instrument via the existing resolver)."""
    position = Position(
        symbol="RELIANCE-EQ", quantity=7, side="BUY",
        average_price=Decimal("2900.00"), account_id="acc",
        exchange="NSE", product_type="MIS", instrument_id="2885",
    )
    client = _client_with_positions(mock_redis, [position])

    available = await get_available_long(
        client=client, exchange="NSE", symbol="RELIANCE", product_type="MIS",
    )
    assert available == 7


@pytest.mark.asyncio
async def test_get_available_long_still_fails_closed_on_fetch_failure_with_token_matching(mock_redis):
    """Phase 1's fail-closed guarantee is untouched by this fix."""
    client = AsyncMock()
    client.redis = mock_redis
    client.get_positions_or_none.return_value = None

    with pytest.raises(PositionFetchError):
        await get_available_long(
            client=client, exchange="NFO", symbol="NIFTY",
            product_type="NRML", provided_token="74056",
        )
