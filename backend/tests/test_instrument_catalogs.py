"""
R4b: read-only catalog adapters over the existing Groww / Angel One stores, and the Alice Blue not-loaded stub.
Fixtures only - no broker, Redis, database or network. Nothing here changes how the stores load or resolve.
"""
import ast
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.services.alice_blue import instruments as alice_instruments
from app.services.angel_one.instruments import AngelInstrumentStore
from app.services.brokers.capabilities import Verification
from app.services.brokers.profiles import build_profile
from app.services.groww import instruments as groww_instruments
from app.services.groww.instruments import GrowwInstrumentStore
from app.services.instruments import catalogs as catalogs_module
from app.services.instruments.catalogs import (
    AliceCatalog,
    AngelCatalog,
    GrowwCatalog,
    default_catalogs,
)
from app.services.instruments.comparator import compare_instrument
from app.services.instruments.model import BrokerInstrument, BrokerView, Confidence, Verdict
from app.services.instruments.normalizer import normalize_equity

NOW = datetime(2026, 10, 22, 6, 0, tzinfo=timezone.utc)
LOADED = NOW - timedelta(hours=2)
REL, INFY, HDFC = "INE002A01018", "INE009A01021", "INE040A01034"

GROWW = build_profile("groww")
ANGEL = build_profile("angelone")
ALICE = build_profile("alicebluenew")
DOCS = Verification.VERIFIED_DOCS
ANY = Verification.UNVERIFIED


def clock():
    return LOADED


def groww_row(exchange="NSE", symbol="RELIANCE", isin=REL, lot="1", itype="EQ", segment="CASH"):
    return {"exchange": exchange, "trading_symbol": symbol, "instrument_type": itype, "segment": segment, "isin": isin,
            "lot_size": lot, "freeze_quantity": "100", "buy_allowed": "true", "sell_allowed": "true"}


def groww_csv(*rows):
    cols = ["exchange", "trading_symbol", "instrument_type", "segment", "isin", "lot_size", "freeze_quantity",
            "buy_allowed", "sell_allowed"]
    return "\n".join([",".join(cols)] + [",".join(str(r.get(c, "")) for c in cols) for r in rows])


def groww_store(*rows, loader="csv"):
    s = GrowwInstrumentStore(clock=clock)
    if loader == "csv":
        s.load_csv_text(groww_csv(*rows))
    else:
        s.load_rows(list(rows))
    return s


def angel_store(tmp_path, *records):
    s = AngelInstrumentStore(clock=clock)
    path = tmp_path / "scrip.json"
    path.write_text(json.dumps(list(records)), encoding="utf-8")
    s.load_file(str(path))
    return s


def acct(token, symbol, exch="NSE"):
    return {"token": token, "symbol": symbol, "name": symbol.replace("-EQ", ""), "exch_seg": exch}


def master(exchange="NSE", symbol="RELIANCE", isin=REL):
    return normalize_equity(exchange, symbol, isin)


def run(catalog, m, profile, **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("minimum", DOCS)
    return compare_instrument(m, catalog.view_for(m.instrument) if m.instrument else BrokerView(catalog.broker_id), profile, **kw)


# ── Groww ────────────────────────────────────────────────────────────────────────────────

def test_groww_rows_map_only_stored_fields():
    cat = GrowwCatalog(groww_store(groww_row(lot="1"), groww_row(symbol="INFY", isin=INFY)))
    view = cat.view_for(master().instrument)
    assert view.broker_id == "groww" and view.loaded_at == LOADED
    assert view.candidates == (BrokerInstrument("groww", "NSE", "RELIANCE", REL, None, 1, None),)


def test_a_lot_size_the_csv_never_stated_is_not_reported_as_one():
    store = groww_store(groww_row(lot=""))
    assert store.resolve("NSE", "RELIANCE").lot_size == 1                    # existing coercion is unchanged
    (cand,) = GrowwCatalog(store).view_for(master().instrument).candidates
    assert cand.lot_size is None and cand.tick_size is None


def test_a_loaded_groww_catalog_matches_by_isin_and_reports_lot_difference():
    cat = GrowwCatalog(groww_store(groww_row(lot="5")))
    r = run(cat, master(), GROWW, master_details=BrokerInstrument("alicebluenew", "NSE", "RELIANCE", lot_size=1))
    assert r.verdict is Verdict.ATTRIBUTE_DIFF and [(d.attribute, d.master, d.broker) for d in r.diffs] == [("lot_size", 1, 5)]
    assert r.unverified_attributes == ("tick_size",)
    assert run(GrowwCatalog(groww_store(groww_row())), master(), GROWW).verdict is Verdict.MATCH


def test_an_empty_groww_store_is_not_loaded_not_missing():
    cat = GrowwCatalog(GrowwInstrumentStore(clock=clock))
    view = cat.view_for(master().instrument)
    assert view.candidates is None and view.loaded_at is None
    assert run(cat, master(), GROWW).verdict is Verdict.CATALOG_NOT_LOADED


def test_a_loaded_groww_store_without_the_instrument_reports_not_found():
    r = run(GrowwCatalog(groww_store(groww_row(symbol="INFY", isin=INFY))), master(), GROWW)
    assert r.verdict is Verdict.UNMAPPED and r.reason == "NOT_FOUND"


def test_duplicate_groww_rows_stay_ambiguous():
    store = groww_store(groww_row(), groww_row(isin=REL))
    with pytest.raises(Exception):
        store.resolve("NSE", "RELIANCE")                                      # the store itself refuses
    view = GrowwCatalog(store).view_for(master().instrument)
    assert len(view.candidates) == 2
    assert run(GrowwCatalog(store), master(), GROWW).verdict is Verdict.AMBIGUOUS
    other_isin = groww_store(groww_row(), groww_row(isin=INFY))
    assert run(GrowwCatalog(other_isin), master(), GROWW).verdict is Verdict.AMBIGUOUS


def test_groww_rows_with_and_without_an_isin_for_one_symbol_are_not_definitive():
    cat = GrowwCatalog(groww_store(groww_row(isin=REL), groww_row(isin="")))
    r = run(cat, master(), GROWW)
    assert r.verdict is Verdict.UNMAPPED and r.reason == "CANDIDATE_LACKS_ISIN"
    assert run(GrowwCatalog(groww_store(groww_row(isin=REL))), master(), GROWW).verdict is Verdict.MATCH
    assert run(GrowwCatalog(groww_store(groww_row(isin=REL), groww_row(isin=INFY))), master(), GROWW).verdict is Verdict.AMBIGUOUS
    assert run(GrowwCatalog(groww_store(groww_row(isin=INFY), groww_row(isin=""))), master(), GROWW).reason == "ISIN_CONFLICT"


def test_the_same_symbol_on_nse_and_bse_is_never_conflated():
    store = groww_store(groww_row("NSE", "RELIANCE", REL), groww_row("BSE", "RELIANCE", INFY))
    cat = GrowwCatalog(store)
    nse = run(cat, master("NSE"), GROWW)
    assert nse.verdict is Verdict.MATCH and nse.matched.exchange == "NSE"
    bse = run(cat, master("BSE", isin=INFY), GROWW)
    assert bse.verdict is Verdict.MATCH and bse.matched.exchange == "BSE"
    assert run(cat, master("BSE", isin=REL), GROWW).verdict is Verdict.UNMAPPED       # NSE's ISIN is not BSE's row


def test_a_symbol_only_master_is_never_confirmed_by_groww_either():
    r = run(GrowwCatalog(groww_store(groww_row())), master(isin=None), GROWW)
    assert r.verdict is Verdict.UNMAPPED and r.reason == "SYMBOL_ONLY_NOT_DEFINITIVE" and r.confidence is Confidence.SYMBOL_ONLY


def test_derivative_rows_are_not_loaded_and_derivative_masters_stay_unmapped():
    store = groww_store(groww_row(), groww_row(symbol="NIFTY24500CE", segment="FNO", itype="CE"), groww_row(symbol="X", itype="FUT"))
    assert store.size == 1
    r = run(GrowwCatalog(store), normalize_equity("NFO", "NIFTY 24500 CE"), GROWW)
    assert r.verdict is Verdict.UNMAPPED and r.reason == "DERIVATIVES_NOT_SUPPORTED_YET"


# ── freshness ────────────────────────────────────────────────────────────────────────────

def test_one_complete_csv_load_is_fresh_and_ages_out():
    store = groww_store(groww_row())
    assert store.snapshot_loaded_at == LOADED
    assert run(GrowwCatalog(store), master(), GROWW).verdict is Verdict.MATCH
    assert run(GrowwCatalog(store), master(), GROWW, now=LOADED + timedelta(hours=25)).verdict is Verdict.MASTER_DATA_STALE


@pytest.mark.parametrize("how", ["direct-rows", "second-load", "empty-load", "partial-then-csv"])
def test_freshness_is_unknown_when_completeness_cannot_be_shown(how):
    if how == "direct-rows":
        store = groww_store(groww_row(), loader="rows")
    elif how == "second-load":
        store = groww_store(groww_row())
        store.load_csv_text(groww_csv(groww_row(symbol="INFY", isin=INFY)))
    elif how == "empty-load":
        store = GrowwInstrumentStore(clock=clock)
        store.load_csv_text(groww_csv())
    else:
        store = GrowwInstrumentStore(clock=clock)
        store.load_rows([groww_row()])
        store.load_csv_text(groww_csv(groww_row(symbol="INFY", isin=INFY)))
    assert store.snapshot_loaded_at is None
    view = GrowwCatalog(store).view_for(master().instrument)
    assert view.loaded_at is None
    if store.size:
        r = run(GrowwCatalog(store), master(), GROWW)
        assert r.verdict is Verdict.MASTER_DATA_STALE and r.reason == "LOAD_TIME_UNKNOWN"


def test_a_failed_load_never_yields_a_fresh_time():
    store = GrowwInstrumentStore(clock=clock)

    def boom():
        raise RuntimeError("disk")
        yield

    with pytest.raises(RuntimeError):
        store.load_rows(boom())
    assert store.snapshot_loaded_at is None and store.size == 0


def test_the_clock_is_only_used_by_the_store_when_it_loads():
    calls = []
    store = GrowwInstrumentStore(clock=lambda: calls.append(1) or LOADED)
    cat = GrowwCatalog(store)
    cat.view_for(master().instrument)
    assert calls == []
    store.load_csv_text(groww_csv(groww_row()))
    assert calls == [1]
    cat.view_for(master().instrument)
    cat.view_for(master().instrument)
    assert calls == [1]


# ── Angel One ────────────────────────────────────────────────────────────────────────────

def test_angel_maps_token_symbol_and_exchange_only(tmp_path):
    cat = AngelCatalog(angel_store(tmp_path, acct("3045", "SBIN-EQ"), acct("11536", "TCS-EQ")))
    view = cat.view_for(master("NSE", "SBIN", None).instrument)
    assert view.loaded_at == LOADED
    assert view.candidates == (BrokerInstrument("angelone", "NSE", "SBIN-EQ", None, "3045", None, None),)


def test_an_empty_angel_store_is_not_loaded_and_is_not_evidence_of_absence():
    cat = AngelCatalog(AngelInstrumentStore(clock=clock))
    r = run(cat, master(), ANGEL, minimum=ANY)
    assert r.verdict is Verdict.CATALOG_NOT_LOADED and r.reason == "CATALOG_NOT_LOADED"


def test_an_isin_master_against_angel_is_unmapped_because_the_candidate_lacks_an_isin(tmp_path):
    r = run(AngelCatalog(angel_store(tmp_path, acct("2885", "RELIANCE-EQ"))), master(), ANGEL, minimum=ANY)
    assert r.verdict is Verdict.UNMAPPED and r.reason == "CANDIDATE_LACKS_ISIN" and r.matched is None


def test_a_symbol_only_master_is_never_confirmed_by_angel(tmp_path):
    r = run(AngelCatalog(angel_store(tmp_path, acct("2885", "RELIANCE-EQ"))), master(isin=None), ANGEL, minimum=ANY)
    assert r.verdict is Verdict.UNMAPPED and r.reason == "SYMBOL_ONLY_NOT_DEFINITIVE"


def test_multiple_angel_tokens_for_one_symbol_stay_ambiguous(tmp_path):
    cat = AngelCatalog(angel_store(tmp_path, acct("2885", "RELIANCE-EQ"), acct("777", "RELIANCE-EQ")))
    r = run(cat, master(isin=None), ANGEL, minimum=ANY)
    assert r.verdict is Verdict.AMBIGUOUS and r.reason == "MULTIPLE_CANDIDATES_FOR_SYMBOL"
    assert len(cat.view_for(master().instrument).candidates) == 2


def test_angel_keeps_nse_and_bse_apart(tmp_path):
    cat = AngelCatalog(angel_store(tmp_path, acct("1", "SBIN-EQ", "NSE"), acct("500112", "SBIN", "BSE")))
    bse = cat.view_for(master("BSE", "SBIN", None).instrument)
    assert [c.exchange for c in bse.candidates] == ["BSE"]
    nse = cat.view_for(master("NSE", "SBIN", None).instrument)
    assert [c.exchange for c in nse.candidates] == ["NSE"]


def test_angel_freshness_follows_the_same_complete_snapshot_rule(tmp_path):
    store = angel_store(tmp_path, acct("1", "SBIN-EQ"))
    assert store.snapshot_loaded_at == LOADED
    store.load_records([acct("2", "TCS-EQ")])
    assert store.snapshot_loaded_at is None
    direct = AngelInstrumentStore(clock=clock)
    direct.load_records([acct("1", "SBIN-EQ")])
    assert direct.snapshot_loaded_at is None
    r = run(AngelCatalog(direct), master("NSE", "SBIN", None), ANGEL, minimum=ANY)
    assert r.verdict is Verdict.MASTER_DATA_STALE and r.reason == "LOAD_TIME_UNKNOWN"


def test_a_non_list_angel_master_is_refused_and_leaves_no_freshness(tmp_path):
    s = AngelInstrumentStore(clock=clock)
    path = tmp_path / "bad.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(Exception):
        s.load_file(str(path))
    assert s.size == 0 and s.snapshot_loaded_at is None


# ── Alice Blue ───────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("m", [master(), master(isin=None), master("BSE"), master("NSE", "SBIN", None),
                               normalize_equity("NFO", "NIFTY 24500 CE"), normalize_equity(None, "X")])
def test_alice_is_always_not_loaded_whatever_the_master(m):
    r = run(AliceCatalog(), m, ALICE, minimum=ANY)
    if m.instrument is None:
        assert r.verdict is Verdict.UNMAPPED
    else:
        assert r.verdict is Verdict.CATALOG_NOT_LOADED and r.reason == "CATALOG_NOT_LOADED"
        assert r.reason != "CANDIDATE_LACKS_ISIN"


def test_alice_ignores_the_mixed_provenance_in_process_table():
    assert ("NSE", "RELIANCE") in alice_instruments.KNOWN_INSTRUMENTS          # present, and deliberately not used
    view = AliceCatalog().view_for(master().instrument)
    assert view == BrokerView("alicebluenew", None, None)
    assert AliceCatalog().view_for(normalize_equity("NSE", "RELIANCE", None).instrument).candidates is None


# ── view_for == the full catalog ─────────────────────────────────────────────────────────

def _full_view_groww(store):
    rows = tuple(BrokerInstrument("groww", i.exchange, i.trading_symbol, i.isin or None, None,
                                  i.lot_size if i.lot_size_stated else None, None) for i in store.iter_instruments())
    return BrokerView("groww", rows, store.snapshot_loaded_at)


def _full_view_angel(store):
    rows = tuple(BrokerInstrument("angelone", e, s, None, t) for e, s, t in store.iter_records())
    return BrokerView("angelone", rows, store.snapshot_loaded_at)


MASTERS = [master(), master(isin=None), master("BSE", "RELIANCE", INFY), master("NSE", "INFY", INFY), master("NSE", "ZZZ", HDFC),
           master("NSE", "TCS", None), normalize_equity("NSE", "RELIANCE", "bad"), normalize_equity("NFO", "X")]


@pytest.mark.parametrize("m", MASTERS)
def test_groww_view_for_gives_the_same_comparison_as_the_full_catalog(m):
    store = groww_store(groww_row(), groww_row(), groww_row("BSE", "RELIANCE", INFY), groww_row(symbol="INFY", isin=INFY),
                        groww_row(symbol="OLD", isin=HDFC), groww_row(symbol="TCS", isin=""), groww_row(symbol="TCS", isin="BAD"),
                        *[groww_row(symbol=f"S{i}", isin=INFY if i == 3 else "") for i in range(30)])
    cat = GrowwCatalog(store)
    for profile, minimum in ((GROWW, DOCS), (GROWW, Verification.VERIFIED_LIVE)):
        a = compare_instrument(m, cat.view_for(m.instrument) if m.instrument else BrokerView("groww"), profile, now=NOW, minimum=minimum)
        b = compare_instrument(m, _full_view_groww(store), profile, now=NOW, minimum=minimum)
        assert a == b


@pytest.mark.parametrize("m", MASTERS)
def test_angel_view_for_gives_the_same_comparison_as_the_full_catalog(m, tmp_path):
    store = angel_store(tmp_path, acct("1", "RELIANCE-EQ"), acct("2", "RELIANCE-EQ"), acct("3", "INFY-EQ"), acct("4", "TCS-EQ"),
                        acct("500325", "RELIANCE", "BSE"), *[acct(str(100 + i), f"S{i}-EQ") for i in range(30)])
    cat = AngelCatalog(store)
    a = compare_instrument(m, cat.view_for(m.instrument) if m.instrument else BrokerView("angelone"), ANGEL, now=NOW, minimum=ANY)
    b = compare_instrument(m, _full_view_angel(store), ANGEL, now=NOW, minimum=ANY)
    assert a == b


# ── stores keep their behaviour; determinism; purity ─────────────────────────────────────

def test_store_resolution_is_unchanged_by_the_additions(tmp_path):
    g = groww_store(groww_row("NSE", "SBIN", INFY), groww_row("BSE", "SBIN", HDFC))
    assert g.resolve("nse", "sbin-eq").trading_symbol == "SBIN" and g.resolve("BSE", "SBIN").exchange == "BSE"
    assert g.resolve("NSE", "SBIN").isin == INFY and g.size == 2
    a = angel_store(tmp_path, acct("3045", "SBIN-EQ"))
    assert a.resolve("nse", "sbin") == ("3045", "SBIN-EQ") and a.size == 1
    for exchange, symbol in (("NFO", "NIFTY"), ("NSE", "")):
        with pytest.raises(Exception):
            g.resolve(exchange, symbol)
        with pytest.raises(Exception):
            a.resolve(exchange, symbol)


def test_view_for_is_deterministic_and_insertion_order_independent(tmp_path):
    rows = [groww_row(symbol=f"S{i}", isin="") for i in range(5)] + [groww_row(), groww_row(isin=REL)]
    a, b = groww_store(*rows), groww_store(*reversed(rows))
    m = master().instrument
    assert GrowwCatalog(a).view_for(m) == GrowwCatalog(a).view_for(m) == GrowwCatalog(b).view_for(m)
    recs = [acct(str(i), "RELIANCE-EQ") for i in range(4)]
    x = angel_store(tmp_path, *recs)
    y = angel_store(tmp_path, *reversed(recs))
    assert AngelCatalog(x).view_for(m) == AngelCatalog(y).view_for(m)


def test_the_default_catalogs_use_the_live_process_wide_stores(monkeypatch):
    live = groww_store(groww_row())
    monkeypatch.setattr(groww_instruments, "store", live)
    by_id = {c.broker_id: c for c in default_catalogs()}
    assert list(by_id) == ["alicebluenew", "angelone", "groww"] and [c.broker_id for c in default_catalogs()] == sorted(by_id)
    assert by_id["groww"].view_for(master().instrument).candidates is not None
    assert by_id["alicebluenew"].view_for(master().instrument).candidates is None


def test_catalogs_import_no_client_redis_http_or_database_code():
    tree = ast.parse(open(catalogs_module.__file__, encoding="utf-8").read())
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
            names += [f"{node.module}.{a.name}" for a in node.names]
    joined = " ".join(names).lower()
    for banned in ("redis", "httpx", "requests", "sqlalchemy", "client", "alice_blue", "socket", "datetime"):
        assert banned not in joined, banned
    calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not calls & {"place_order", "modify_order", "cancel_order", "get_order_book", "get", "post", "now", "utcnow"}


def test_the_tick_size_is_never_invented():
    (cand,) = GrowwCatalog(groww_store(groww_row())).view_for(master().instrument).candidates
    assert cand.tick_size is None
