"""
Groww instrument-master loading: validated, atomic, single-flight, cached and fail-closed.

Offline: the download is always a fake coroutine or a mocked transport; no request leaves the process. The CSV rows are copied from the
public Groww instruments file (columns and values as published) so the real format, not a hand-made one, is what gets parsed.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from app.core.config import settings
from app.services.alice_blue.instruments import InstrumentResolutionError
from app.services.alice_blue.types import OrderResultCategory
from app.services.groww import instruments as I
from app.services.groww.instruments import GrowwInstrumentStore, load_instruments_if_enabled
import groww_helpers as G
from test_groww_client import FakeGroww, logged_in, req

URL = "https://example.invalid/instrument.csv"
HEADER = ("exchange,exchange_token,trading_symbol,groww_symbol,name,instrument_type,segment,series,isin,underlying_symbol,"
          "underlying_exchange_token,expiry_date,strike_price,lot_size,tick_size,freeze_quantity,is_reserved,buy_allowed,"
          "sell_allowed,internal_trading_symbol,is_intraday")
LIVE_ROWS = [
    "NSE,10794,CANBK,NSE-CANBK,Canara Bank,EQ,CASH,EQ,INE476A01022,,,,,1,0.01,,,1,1,CANBK-EQ,1",
    "NSE,14366,IDEA,NSE-IDEA,Vodafone Idea,EQ,CASH,EQ,INE669E01016,,,,,1,0.01,,,1,1,IDEA-EQ,1",
    "NSE,11915,YESBANK,NSE-YESBANK,Yes Bank,EQ,CASH,EQ,INE528G01035,,,,,1,0.01,,,1,1,YESBANK-EQ,1",
    "BSE,532483,CANBK,BSE-CANBK,Canara Bank,EQ,CASH,A,INE476A01022,,,,,1,0.05,,,1,1,CANBK,1",
    "BSE,532822,IDEA,BSE-IDEA,Vodafone Idea,EQ,CASH,A,INE669E01016,,,,,1,0.01,,,1,1,IDEA,1",
]
EXTRA_ROW = "NSE,3045,SBIN,NSE-SBIN,State Bank of India,EQ,CASH,EQ,INE062A01020,,,,,1,0.05,,,1,1,SBIN-EQ,1"
FNO_ROW = "NSE,26000,NIFTY26NOV23500CE,NSE-NIFTY,Nifty,CE,FNO,,,NIFTY,,2026-11-26,23500,75,0.05,1800,,1,1,NIFTY,0"


def csv_text(*rows, header=HEADER):
    return "\n".join([header, *rows]) + "\n"


LIVE = csv_text(*LIVE_ROWS)


class Tick:
    """A mutable clock so ages are exact and no test sleeps."""

    def __init__(self):
        self.now = datetime(2026, 10, 30, 4, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class Fetch:
    def __init__(self, text=LIVE, exc=None, gate=None):
        self.text, self.exc, self.gate, self.calls = text, exc, gate, 0

    async def __call__(self, url):
        assert url == URL
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.exc is not None:
            raise self.exc
        return self.text


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(settings, "GROWW_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_INSTRUMENT_MASTER_URL", URL)
    monkeypatch.setattr(I, "MIN_EQUITY_ROWS", 3)            # the fixtures are tiny; the production floor is exercised below


# ── the real file format and the three symbols of the 30 Oct rejections ────────────────────────────────────────────────────────

def test_live_format_resolves_yesbank_idea_and_canara_bank():
    store = GrowwInstrumentStore()
    assert store.replace_from_csv_text(LIVE) == 5
    for symbol, isin in (("YESBANK", "INE528G01035"), ("IDEA", "INE669E01016"), ("CANBK", "INE476A01022")):
        inst = store.resolve("NSE", symbol)
        assert (inst.exchange, inst.trading_symbol, inst.isin) == ("NSE", symbol, isin)
        assert inst.lot_size == 1 and inst.lot_size_stated and inst.buy_allowed is True and inst.sell_allowed is True
    assert store.resolve("NSE", "IDEA-EQ").trading_symbol == "IDEA"            # an Alice-style suffix resolves to Groww symbol
    assert store.resolve("BSE", "CANBK").exchange == "BSE"
    with pytest.raises(InstrumentResolutionError):
        store.resolve("NSE", "CANARA BANK")                                    # a company name is not a trading symbol


def test_derivatives_and_unknown_symbols_never_resolve():
    store = GrowwInstrumentStore()
    store.replace_from_csv_text(csv_text(*LIVE_ROWS, FNO_ROW))
    assert store.size == 5
    for symbol in ("NIFTY26NOV23500CE", "NOSUCH"):
        with pytest.raises(InstrumentResolutionError):
            store.resolve("NSE", symbol)


# ── configuration gates: no URL or Groww disabled means no download and not ready ──────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,url", [(False, URL), (True, "")])
async def test_disabled_or_unconfigured_never_downloads(monkeypatch, enabled, url):
    monkeypatch.setattr(settings, "GROWW_ENABLED", enabled)
    monkeypatch.setattr(settings, "GROWW_INSTRUMENT_MASTER_URL", url)
    fetch, store = Fetch(), GrowwInstrumentStore()
    assert await store.ensure_loaded(fetch) is False and fetch.calls == 0 and store.size == 0 and not store.is_ready
    assert await load_instruments_if_enabled(store) == 0


@pytest.mark.asyncio
async def test_startup_load_reports_the_loaded_count(monkeypatch):
    async def fetch(url):
        return LIVE
    monkeypatch.setattr(I, "_fetch_text", fetch)
    store = GrowwInstrumentStore()
    assert await load_instruments_if_enabled(store) == 5 and store.is_ready


# ── malformed, empty and truncated documents never mark the store ready ────────────────────────────────────────────────────────

BAD_DOCUMENTS = {
    "empty": "",
    "garbage": "<html>Service Unavailable</html>",
    "header_only": csv_text(),
    "missing_required_column": csv_text(*[r for r in LIVE_ROWS], header=HEADER.replace("buy_allowed", "buy_ok")),
    "too_few_rows": csv_text(*LIVE_ROWS[:2]),
    "no_equity_rows": csv_text(FNO_ROW, FNO_ROW, FNO_ROW),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(BAD_DOCUMENTS))
async def test_bad_documents_leave_the_store_empty_and_not_ready(name, caplog):
    caplog.set_level(logging.ERROR, logger="groww_instruments")
    store = GrowwInstrumentStore()
    assert await store.ensure_loaded(Fetch(text=BAD_DOCUMENTS[name])) is False
    assert store.size == 0 and not store.is_ready and store.snapshot_loaded_at is None
    with pytest.raises(InstrumentResolutionError, match="not loaded"):
        store.resolve("NSE", "YESBANK")
    assert any("could not be loaded" in r.getMessage() and "ValueError" in r.getMessage() for r in caplog.records)


def test_the_production_floor_rejects_a_truncated_document(monkeypatch):
    monkeypatch.undo()                                      # drop the small test floor
    monkeypatch.setattr(settings, "GROWW_ENABLED", True)
    store = GrowwInstrumentStore()
    with pytest.raises(ValueError, match="expected at least 1000"):
        store.replace_from_csv_text(LIVE, min_rows=I.MIN_EQUITY_ROWS)
    assert store.size == 0


def test_a_bad_replacement_leaves_the_previous_catalog_untouched():
    store = GrowwInstrumentStore()
    store.replace_from_csv_text(LIVE)
    for text in ("", "<html/>", csv_text(), csv_text(*LIVE_ROWS, header=HEADER.replace("segment", "seg"))):
        with pytest.raises(ValueError):
            store.replace_from_csv_text(text)
    assert store.size == 5 and store.resolve("NSE", "YESBANK").trading_symbol == "YESBANK"


def test_a_second_load_replaces_instead_of_accumulating_duplicates():
    store = GrowwInstrumentStore()
    store.replace_from_csv_text(LIVE)
    store.replace_from_csv_text(csv_text(*LIVE_ROWS, EXTRA_ROW))
    assert store.size == 6
    assert store.resolve("NSE", "YESBANK").trading_symbol == "YESBANK"             # still exactly one match, not "ambiguous (2)"
    assert store.snapshot_loaded_at is not None


# ── network failures, cooldown and recovery ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_network_failure_is_not_ready_is_not_hammered_and_recovers():
    tick = Tick()
    store = GrowwInstrumentStore(tick)
    down = Fetch(exc=httpx.ConnectError("down"))
    assert await store.ensure_loaded(down) is False and down.calls == 1
    assert await store.ensure_loaded(down) is False and down.calls == 1            # inside the cooldown: no second download
    tick.advance(seconds=61)
    assert await store.ensure_loaded(down) is False and down.calls == 2
    tick.advance(seconds=61)
    up = Fetch()
    assert await store.ensure_loaded(up) is True and up.calls == 1 and store.is_ready
    assert store.resolve("NSE", "IDEA").trading_symbol == "IDEA"


@pytest.mark.asyncio
async def test_http_error_status_is_a_failure(monkeypatch):
    class Bad:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url):
            return httpx.Response(503, request=httpx.Request("GET", url), content=b"busy")
    monkeypatch.setattr(I.httpx, "AsyncClient", Bad)
    store = GrowwInstrumentStore()
    assert await store.ensure_loaded() is False and store.size == 0


# ── concurrency: one shared download ───────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_concurrent_callers_share_one_download():
    gate = asyncio.Event()
    fetch, store = Fetch(gate=gate), GrowwInstrumentStore()
    waiters = [asyncio.ensure_future(store.ensure_loaded(fetch)) for _ in range(25)]
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not any(w.done() for w in waiters)              # none returned "ready" before the download finished
    gate.set()
    assert await asyncio.gather(*waiters) == [True] * 25
    assert fetch.calls == 1 and store.size == 5


@pytest.mark.asyncio
async def test_concurrent_callers_after_a_failure_do_not_each_retry():
    gate = asyncio.Event()
    fetch, store = Fetch(exc=httpx.ConnectError("down"), gate=gate), GrowwInstrumentStore()
    waiters = [asyncio.ensure_future(store.ensure_loaded(fetch)) for _ in range(10)]
    await asyncio.sleep(0)
    gate.set()
    assert await asyncio.gather(*waiters) == [False] * 10 and fetch.calls == 1


# ── caching, staleness and a failed refresh ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_fresh_catalog_is_served_without_a_download():
    tick = Tick()
    store = GrowwInstrumentStore(tick)
    await store.ensure_loaded(Fetch())
    tick.advance(hours=11)
    again = Fetch()
    assert await store.ensure_loaded(again) is True and again.calls == 0


@pytest.mark.asyncio
async def test_a_soft_stale_catalog_keeps_serving_while_it_refreshes_behind():
    tick = Tick()
    store = GrowwInstrumentStore(tick)
    await store.ensure_loaded(Fetch())
    tick.advance(hours=13)
    gate = asyncio.Event()
    refresh = Fetch(text=csv_text(*LIVE_ROWS, EXTRA_ROW), gate=gate)
    assert await asyncio.wait_for(store.ensure_loaded(refresh), 1) is True            # an order is not held up by the download
    assert store.size == 5 and store.resolve("NSE", "YESBANK").trading_symbol == "YESBANK"
    gate.set()
    await store._background
    assert store.size == 6 and store.resolve("NSE", "SBIN").trading_symbol == "SBIN"
    assert refresh.calls == 1


@pytest.mark.asyncio
async def test_a_failed_refresh_keeps_the_previous_catalog_until_the_hard_limit():
    tick = Tick()
    store = GrowwInstrumentStore(tick)
    await store.ensure_loaded(Fetch())
    tick.advance(hours=13)
    broken = Fetch(text="<html>oops</html>")
    assert await store.ensure_loaded(broken) is True
    await store._background
    assert store.size == 5 and store.resolve("NSE", "IDEA").trading_symbol == "IDEA"      # the cache survived a malformed refresh
    tick.advance(hours=24)                                                                   # 37 h old: past the hard limit
    assert not store.is_ready
    with pytest.raises(InstrumentResolutionError, match="stale"):
        store.resolve("NSE", "IDEA")
    assert await store.ensure_loaded(Fetch(exc=httpx.ConnectError("down"))) is False         # still failing: stays refused
    with pytest.raises(InstrumentResolutionError, match="stale"):
        store.resolve("NSE", "IDEA")
    tick.advance(seconds=61)
    assert await store.ensure_loaded(Fetch()) is True and store.is_ready                     # recovery
    assert store.resolve("NSE", "IDEA").trading_symbol == "IDEA"


def test_a_catalog_with_unknown_freshness_is_not_called_stale():
    store = GrowwInstrumentStore(Tick())
    store.load_rows([{"exchange": "NSE", "trading_symbol": "SBIN", "instrument_type": "EQ", "segment": "CASH",
                      "buy_allowed": "1", "sell_allowed": "1"}])
    assert store.is_ready and store.resolve("NSE", "SBIN").trading_symbol == "SBIN"


# ── the order path loads lazily and still fails closed ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_order_loads_the_master_lazily_once(mock_redis, monkeypatch):
    fetch = Fetch()
    monkeypatch.setattr(I, "_fetch_text", fetch)
    fake = FakeGroww()
    client, _ = await logged_in(mock_redis, fake, store=GrowwInstrumentStore())
    first = await client.place_order(req(symbol="YESBANK-EQ", quantity=1, price=Decimal("15.21"), client_order_id="jt-1"))
    second = await client.place_order(req(symbol="IDEA", quantity=1, price=Decimal("11.75"), client_order_id="jt-2"))
    assert first.category is OrderResultCategory.SUCCESS and second.category is OrderResultCategory.SUCCESS
    assert fetch.calls == 1
    import json
    sent = [json.loads(r.content) for r in fake.calls("/v1/order/create")]
    assert [(b["trading_symbol"], b["exchange"], b["segment"]) for b in sent] == [("YESBANK", "NSE", "CASH"), ("IDEA", "NSE", "CASH")]


@pytest.mark.asyncio
async def test_an_order_is_not_sent_when_the_master_cannot_be_loaded(mock_redis, monkeypatch):
    monkeypatch.setattr(I, "_fetch_text", Fetch(exc=httpx.ConnectError("down")))
    fake = FakeGroww()
    client, _ = await logged_in(mock_redis, fake, store=GrowwInstrumentStore())
    result = await client.place_order(req(symbol="YESBANK", quantity=1, client_order_id="jt-3"))
    assert result.category is OrderResultCategory.REJECTED and "not loaded" in result.message
    assert fake.calls("/v1/order/create") == []


@pytest.mark.asyncio
async def test_resolve_instrument_loads_lazily_too(mock_redis, monkeypatch):
    monkeypatch.setattr(I, "_fetch_text", Fetch())
    client = G.make_client(mock_redis, FakeGroww(), store=GrowwInstrumentStore())
    assert await client.resolve_instrument("NSE", "CANBK") == ("INE476A01022", "CANBK")
