"""
The follower SELL position guard fed with RAW Alice Blue position rows.

2026-10-14 incident: a Child's NIFTY option, bought as product LONGTERM, sat in the broker's position book
as NRML (same instrumentId) and the guard computed 0 long. The existing tests build `Position` objects by
hand; none of them pushes a raw broker row through the real parser (AliceBlueClient._parse_position_rows)
into the guard, so a parsing or key-spelling regression would pass them all. These do:

    raw JSON row -> AliceBlueClient.get_positions_or_none() -> get_available_long() -> pipeline

Pinned here: the incident shape end to end, every supported net-quantity key, aggregation of several rows of
one contract without mixing NRML and MIS, the symbol fallback for a row with no instrumentId, and the
fail-closed handling of a non-numeric quantity. The broker is always a mock; no network.
"""
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.audit import AuditLog
from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.types import OrderSide, ProductType
from app.services.copy_engine.risk_inputs import PositionFetchError, get_available_long
from conftest import TestingSessionLocal
from test_order_handling_matrix import NIFTY_TOKEN, _child, _event, _placed, _run, _setup

CONTRACT = "NIFTY29SEP26C23900"          # how the POSITION book spells the contract
QTY_KEYS = ["netQuantity", "Netqty", "netQty"]


@pytest.fixture(autouse=True)
def _safety_on(monkeypatch):
    monkeypatch.setattr(settings, "SELL_POSITION_SAFETY_BYPASS", False)


def row(qty, product="NRML", *, key="netQuantity", exchange="NFO", symbol=CONTRACT, token=NIFTY_TOKEN):
    """A raw /positions row as the broker sends it. token=... drops or blanks instrumentId when asked."""
    r = {"tradingSymbol": symbol, "exchange": exchange, "product": product, key: qty, "netAveragePrice": 6.7}
    if token is not None:
        r["instrumentId"] = token
    return r


async def raw_client(mock_redis, rows):
    """A real AliceBlueClient whose HTTP layer is a mock returning `rows` as the positions book."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
    resp = AsyncMock()
    resp.status_code = 200
    resp.json = lambda: {"result": rows}
    mock_http.request = AsyncMock(return_value=resp)
    return AliceBlueClient(account_id="acc-test", user_id="AB00101", api_secret="secret",
                           redis=mock_redis, http_client=mock_http)


async def held(mock_redis, rows, product, *, symbol="NIFTY", exchange="NFO", token=NIFTY_TOKEN):
    client = await raw_client(mock_redis, rows)
    return await get_available_long(client=client, exchange=exchange, symbol=symbol,
                                    product_type=product, provided_token=token)


async def pipeline_child(mock_redis, rows):
    """A pipeline Child whose position reads go through the REAL parser; order placement stays a mock."""
    child_client = _child([])
    real = await raw_client(mock_redis, rows)
    child_client.get_positions_or_none = real.get_positions_or_none
    return child_client


async def audit_reasons(account):
    async with TestingSessionLocal() as s:
        rows = (await s.execute(select(AuditLog).where(AuditLog.account_id == account.id))).scalars().all()
    return [r.reason_code for r in rows if r.reason_code]


# ── 1. the incident, end to end, for every net-quantity key ─────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("key", QTY_KEYS)
async def test_incident_longterm_sell_is_covered_by_the_raw_nrml_row(db_session, mock_redis, key):
    """Master SELL is LONGTERM; the Child's raw position row is NRML with the same instrumentId."""
    master, child = await _setup(db_session)
    client = await pipeline_child(mock_redis, [row(65, "NRML", key=key)])
    out = await _run(mock_redis, master, child, client, _event(master, f"M-RAW-{key}", product="LONGTERM"))
    assert out["status"] == "SUCCESS", out
    req = _placed(client)
    assert req.quantity == 65 and req.side == OrderSide.SELL and req.product_type == ProductType.CNC


@pytest.mark.asyncio
async def test_raw_intraday_row_does_not_cover_the_carry_sell(db_session, mock_redis):
    """Control for the test above: the same raw row as MIS must still be refused (the parse is not trivially
    returning a quantity)."""
    master, child = await _setup(db_session)
    client = await pipeline_child(mock_redis, [row(65, "MIS")])
    out = await _run(mock_redis, master, child, client, _event(master, "M-RAW-MIS", product="LONGTERM"))
    assert out["status"] == "REJECTED"
    assert "INSUFFICIENT_LONG_POSITION" in await audit_reasons(child)
    client.place_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", QTY_KEYS)
async def test_every_net_quantity_key_is_read_and_a_zero_row_is_ignored(mock_redis, key):
    assert await held(mock_redis, [row(65, "NRML", key=key)], "LONGTERM") == 65
    assert await held(mock_redis, [row(0, "NRML", key=key)], "LONGTERM") == 0


# ── 2. several rows of one contract: aggregate per product bucket, never across NRML and MIS ───

@pytest.mark.asyncio
async def test_rows_of_one_contract_are_summed_without_mixing_nrml_and_mis(mock_redis):
    rows = [
        row(40, "NRML"), row(25, "NRML"),                       # two carry rows, same contract
        row(130, "MIS"),                                        # intraday row, same contract
        row(500, "NRML", token="99999", symbol="NIFTY29SEP26P23000"),   # another contract
        row(500, "NRML", exchange="BFO"),                       # another exchange
    ]
    assert await held(mock_redis, rows, "LONGTERM") == 65       # 40 + 25, no MIS, nothing foreign
    assert await held(mock_redis, rows, "NRML") == 65
    assert await held(mock_redis, rows, "INTRADAY") == 130      # MIS alone
    assert await held(mock_redis, rows, "MIS") == 130


@pytest.mark.asyncio
@pytest.mark.parametrize("second,expected", [(25, "SUCCESS"), (24, "REJECTED")])
async def test_the_aggregate_is_what_the_pipeline_compares_with_the_sell_quantity(db_session, mock_redis, second, expected):
    """40 + 25 = 65 covers a 65 SELL; 40 + 24 = 64 does not - and an intraday row never tops it up."""
    master, child = await _setup(db_session)
    client = await pipeline_child(mock_redis, [row(40, "NRML"), row(second, "NRML"), row(100, "MIS")])
    out = await _run(mock_redis, master, child, client, _event(master, f"M-AGG-{second}", product="LONGTERM"))
    assert out["status"] == expected, out
    if expected == "REJECTED":
        assert "INSUFFICIENT_LONG_POSITION" in await audit_reasons(child)
        client.place_order.assert_not_called()


# ── 3. symbol fallback when the position row carries no instrumentId ───────────────────────────

NO_ID_FORMS = {"key_absent": None, "null": "__null__", "empty": ""}


def no_id_row(form, qty=65, product="NRML", symbol="NIFTY", exchange="NFO"):
    r = row(qty, product, symbol=symbol, exchange=exchange, token=None)
    if NO_ID_FORMS[form] is not None:
        r["instrumentId"] = None if NO_ID_FORMS[form] == "__null__" else NO_ID_FORMS[form]
    return r


@pytest.mark.asyncio
@pytest.mark.parametrize("form", list(NO_ID_FORMS))
async def test_row_without_instrument_id_matches_by_symbol_spelling(mock_redis, form):
    """No id on the row -> the documented fallback compares symbols. The master's order-book spelling is
    'NIFTY', so a row spelled 'NIFTY' counts."""
    assert await held(mock_redis, [no_id_row(form, symbol="NIFTY")], "LONGTERM") == 65


@pytest.mark.asyncio
async def test_fallback_counts_a_row_spelled_like_the_order_that_is_being_sold(mock_redis):
    rows = [no_id_row("key_absent", symbol=CONTRACT)]
    assert await held(mock_redis, rows, "LONGTERM", symbol=CONTRACT) == 65


@pytest.mark.asyncio
async def test_fallback_does_not_equate_the_bare_underlying_with_the_full_contract_name(mock_redis):
    """Current, fail-closed behaviour worth pinning: without an id on the row, 'NIFTY' (order book) is NOT
    treated as 'NIFTY29SEP26C23900' (position book), so the guard reads 0 and the SELL is refused rather
    than risking a match against the wrong strike/expiry."""
    rows = [no_id_row("key_absent", symbol=CONTRACT)]
    assert await held(mock_redis, rows, "LONGTERM", symbol="NIFTY") == 0


@pytest.mark.asyncio
async def test_fallback_still_enforces_product_and_exchange(mock_redis):
    assert await held(mock_redis, [no_id_row("key_absent", product="MIS")], "LONGTERM") == 0
    assert await held(mock_redis, [no_id_row("key_absent", product="NRML")], "LONGTERM") == 65
    assert await held(mock_redis, [no_id_row("key_absent", exchange="BFO")], "LONGTERM") == 0


# ── 4. a non-numeric net quantity fails closed ─────────────────────────────────────────────────

BAD_QUANTITIES = ["abc", "", None, "12.5"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", BAD_QUANTITIES, ids=["text", "empty", "null", "decimal_text"])
async def test_non_numeric_quantity_is_a_fetch_failure_never_zero(mock_redis, bad):
    client = await raw_client(mock_redis, [row(bad, "NRML")])
    with pytest.raises(PositionFetchError):
        await get_available_long(client=client, exchange="NFO", symbol="NIFTY",
                                 product_type="LONGTERM", provided_token=NIFTY_TOKEN)


@pytest.mark.asyncio
async def test_a_bad_row_for_another_contract_also_fails_closed(mock_redis):
    """The whole book is parsed before matching, so one unreadable row anywhere blocks the SELL (it cannot be
    proven irrelevant). Pinned so a change to this is a conscious one."""
    client = await raw_client(mock_redis, [row(65, "NRML"), row("abc", "MIS", token="99999", symbol="OTHER")])
    with pytest.raises(PositionFetchError):
        await get_available_long(client=client, exchange="NFO", symbol="NIFTY",
                                 product_type="LONGTERM", provided_token=NIFTY_TOKEN)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["abc", None], ids=["text", "null"])
async def test_pipeline_rejects_with_position_fetch_failed_and_places_nothing(db_session, mock_redis, bad):
    master, child = await _setup(db_session)
    client = await pipeline_child(mock_redis, [row(bad, "NRML")])
    out = await _run(mock_redis, master, child, client, _event(master, f"M-BAD-{bad}", product="LONGTERM"))
    assert out["status"] == "REJECTED", out
    reasons = await audit_reasons(child)
    assert "POSITION_FETCH_FAILED" in reasons and "INSUFFICIENT_LONG_POSITION" not in reasons
    client.place_order.assert_not_called()
