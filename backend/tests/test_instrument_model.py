"""
R4a: canonical equity identity, normaliser and comparator. Pure fixtures only - no broker, Redis, database or
network. Derivatives are explicitly unmapped; no F&O token or placeholder is used as evidence.
"""
import dataclasses
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.services.brokers.capabilities import Verification
from app.services.brokers.profiles import build_profile
from app.services.instruments.comparator import compare_all, compare_instrument
from app.services.instruments.model import (
    BrokerInstrument,
    BrokerView,
    CanonicalInstrument,
    Confidence,
    Verdict,
)
from app.services.instruments.normalizer import is_valid_isin, normalize_equity

NOW = datetime(2026, 10, 22, 6, 0, tzinfo=timezone.utc)
FRESH = NOW - timedelta(hours=2)
REL, INFY, HDFC = "INE002A01018", "INE009A01021", "INE040A01034"     # real ISINs (check digits verified in-test)

ALICE = build_profile("alicebluenew")
GROWW = build_profile("groww")


def bi(broker="alicebluenew", exchange="NSE", sym="RELIANCE", isin=REL, lot=None, tick=None, token=None):
    return BrokerInstrument(broker, exchange, sym, isin, token, lot, tick)


def view(*cands, broker="alicebluenew", loaded=FRESH):
    return BrokerView(broker, tuple(cands), loaded)


def cmp(master, v, profile=ALICE, **kw):
    kw.setdefault("now", NOW)
    return compare_instrument(master, v, profile, **kw)


# ── ISIN ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("isin,ok", [(REL, True), (INFY, True), (HDFC, True), ("INE002A01019", False),
                                     ("INE002A0101", False), ("ine002a01018", True), ("", False), (None, False),
                                     ("INE002A0101X", False)])
def test_isin_validation_checks_format_and_check_digit(isin, ok):
    assert is_valid_isin(isin) is ok


# ── normaliser ───────────────────────────────────────────────────────────────────────────

def test_a_valid_isin_gives_an_isin_confidence_identity_with_a_deterministic_key():
    n = normalize_equity("nse", " reliance-eq ", REL.lower())
    assert n.confidence is Confidence.ISIN and n.instrument == CanonicalInstrument("NSE", "RELIANCE", REL)
    assert n.key == f"EQ|NSE|ISIN|{REL}" == normalize_equity("NSE", "RELIANCE", REL).key


def test_a_symbol_without_isin_is_low_confidence_and_its_key_cannot_equal_an_isin_key():
    n = normalize_equity("NSE", "RELIANCE")
    assert n.confidence is Confidence.SYMBOL_ONLY and n.key == "EQ|NSE|SYM|RELIANCE"
    assert n.key != normalize_equity("NSE", "RELIANCE", REL).key


@pytest.mark.parametrize("raw", ["RELIANCE", "RELIANCE-EQ", "reliance-eq", "  RELIANCE-EQ "])
def test_eq_suffix_variants_normalise_to_one_identity(raw):
    assert normalize_equity("NSE", raw, REL).key == f"EQ|NSE|ISIN|{REL}"


def test_only_one_trailing_eq_is_stripped_and_symbols_with_punctuation_are_kept():
    assert normalize_equity("NSE", "M&M").instrument.symbol == "M&M"
    assert normalize_equity("NSE", "BAJAJ-AUTO").instrument.symbol == "BAJAJ-AUTO"
    assert normalize_equity("NSE", "X-EQ-EQ").instrument.symbol == "X-EQ"


def test_nse_and_bse_are_never_equated_even_with_the_same_isin():
    nse, bse = normalize_equity("NSE", "RELIANCE", REL), normalize_equity("BSE", "RELIANCE", REL)
    assert nse.key != bse.key and nse.instrument != bse.instrument


@pytest.mark.parametrize("exchange,symbol,isin,reason", [
    (None, "RELIANCE", None, "MISSING_EXCHANGE"),
    ("", "RELIANCE", REL, "MISSING_EXCHANGE"),
    ("NSE", None, REL, "MISSING_SYMBOL"),
    ("NSE", "  ", None, "MISSING_SYMBOL"),
    ("NSE", "-EQ", None, "MISSING_SYMBOL"),
    ("NSE", "RELI ANCE", None, "MALFORMED_SYMBOL"),
    ("NSE", "RELIANCE (NSE)", None, "MALFORMED_SYMBOL"),
    ("NSE", "RELIANCE", "INE002A01019", "MALFORMED_ISIN"),     # present but invalid: NOT downgraded to symbol-only
    ("NSE", "RELIANCE", "garbage", "MALFORMED_ISIN"),
    ("NSE", "NIFTY", None, "NOT_AN_EQUITY_INDEX"),
    ("NSE", "BANKNIFTY", REL, "NOT_AN_EQUITY_INDEX"),
    ("NSE_CM", "RELIANCE", REL, "UNSUPPORTED_EXCHANGE"),
    ("MCX", "GOLD", None, "DERIVATIVES_NOT_SUPPORTED_YET"),
])
def test_missing_malformed_or_unsupported_metadata_is_unmapped_with_a_reason(exchange, symbol, isin, reason):
    n = normalize_equity(exchange, symbol, isin)
    assert n.instrument is None and n.key is None and n.confidence is Confidence.UNMAPPED and n.reason == reason


@pytest.mark.parametrize("exchange,symbol", [("NFO", "NIFTY 24500 CE"), ("NFO", "NIFTY-FUT"), ("BFO", "SENSEX"),
                                              ("NSE", "NIFTY-FUT"), ("NSE", "RELIANCE-FUT")])
def test_derivatives_are_never_mapped(exchange, symbol):
    n = normalize_equity(exchange, symbol, REL)
    assert n.instrument is None and n.reason == "DERIVATIVES_NOT_SUPPORTED_YET"


def test_a_derivative_shaped_name_with_spaces_on_a_cash_exchange_is_malformed_not_mapped():
    assert normalize_equity("NSE", "NIFTY 24500 CE").reason == "MALFORMED_SYMBOL"


@pytest.mark.parametrize("absent", [None, "", "  ", "nan", "NaN", "None", "-"])
def test_an_absent_isin_marker_means_symbol_only(absent):
    assert normalize_equity("NSE", "RELIANCE", absent).confidence is Confidence.SYMBOL_ONLY


def test_the_model_is_immutable():
    n = normalize_equity("NSE", "RELIANCE", REL)
    with pytest.raises(dataclasses.FrozenInstanceError):
        n.instrument.isin = "X"
    with pytest.raises(dataclasses.FrozenInstanceError):
        n.reason = "x"


# ── comparator ───────────────────────────────────────────────────────────────────────────

def test_an_exact_isin_match_is_a_definitive_match():
    master = normalize_equity("NSE", "RELIANCE-EQ", REL)
    r = cmp(master, view(bi(sym="RELIANCE-EQ"), bi(sym="INFY", isin=INFY)))
    assert r.verdict is Verdict.MATCH and r.reason == "ISIN_MATCH" and r.confidence is Confidence.ISIN
    assert r.matched.isin == REL and r.master_key == f"EQ|NSE|ISIN|{REL}"


def test_an_isin_matches_even_when_the_brokers_symbol_differs():
    r = cmp(normalize_equity("NSE", "OLDNAME", REL), view(bi(sym="NEWNAME")))
    assert r.verdict is Verdict.MATCH


def test_a_symbol_only_master_never_matches_by_name_alone():
    r = cmp(normalize_equity("NSE", "RELIANCE"), view(bi()))
    assert r.verdict is Verdict.UNMAPPED and r.reason == "SYMBOL_ONLY_NOT_DEFINITIVE"
    assert r.confidence is Confidence.SYMBOL_ONLY and r.matched is None


def test_a_symbol_only_master_with_no_or_several_candidates():
    master = normalize_equity("NSE", "RELIANCE")
    assert cmp(master, view(bi(sym="INFY", isin=INFY))).reason == "NOT_FOUND"
    two = cmp(master, view(bi(isin=REL), bi(isin=HDFC)))
    assert two.verdict is Verdict.AMBIGUOUS and two.reason == "MULTIPLE_CANDIDATES_FOR_SYMBOL"


def test_nse_and_bse_candidates_are_kept_apart():
    master = normalize_equity("NSE", "RELIANCE", REL)
    assert cmp(master, view(bi(exchange="BSE"))).reason == "NOT_FOUND"
    both = cmp(master, view(bi(exchange="BSE"), bi(exchange="NSE")))
    assert both.verdict is Verdict.MATCH and both.matched.exchange == "NSE"


def test_a_symbol_carrying_a_different_isin_is_a_conflict_not_a_match():
    r = cmp(normalize_equity("NSE", "RELIANCE", REL), view(bi(isin=INFY)))
    assert r.verdict is Verdict.UNMAPPED and r.reason == "ISIN_CONFLICT"


def test_a_candidate_without_an_isin_cannot_confirm_an_isin_master():
    r = cmp(normalize_equity("NSE", "RELIANCE", REL), view(bi(isin=None)))
    assert r.verdict is Verdict.UNMAPPED and r.reason == "CANDIDATE_LACKS_ISIN" and r.matched is None
    assert cmp(normalize_equity("NSE", "RELIANCE", REL), view(bi(sym="INFY", isin=None))).reason == "NOT_FOUND"
    assert cmp(normalize_equity("NSE", "RELIANCE", REL), view(bi(isin=None), bi(isin=INFY))).reason == "ISIN_CONFLICT"


def test_a_matching_isin_is_not_definitive_while_another_same_symbol_candidate_lacks_one():
    master = normalize_equity("NSE", "RELIANCE", REL)
    assert cmp(master, view(bi(isin=REL))).verdict is Verdict.MATCH                      # no competing candidate
    for cands in ((bi(isin=REL), bi(isin=None, token="2")), (bi(isin=None, token="2"), bi(isin=REL))):
        r = cmp(master, view(*cands))                                                    # order never matters
        assert r.verdict is Verdict.UNMAPPED and r.reason == "CANDIDATE_LACKS_ISIN" and r.matched is None
    # a lot-size difference cannot turn it into a definitive ATTRIBUTE_DIFF either
    r = cmp(master, view(bi(isin=REL, lot=5), bi(isin=None, token="2")), master_details=bi(lot=1))
    assert r.verdict is Verdict.UNMAPPED and r.diffs == ()
    # precedence is preserved: a conflicting ISIN on the same symbol is still reported first
    assert cmp(master, view(bi(isin=INFY))).reason == "ISIN_CONFLICT"
    assert cmp(master, view(bi(isin=None), bi(isin=INFY))).reason == "ISIN_CONFLICT"
    assert cmp(master, view(bi(isin=REL), bi(isin=INFY), bi(isin=None))).verdict is Verdict.AMBIGUOUS
    # no matching ISIN and no competing symbol: the established behaviour
    assert cmp(master, view(bi(sym="INFY", isin=INFY))).reason == "NOT_FOUND"
    # a candidate without an ISIN on a DIFFERENT symbol or exchange is irrelevant
    assert cmp(master, view(bi(isin=REL), bi(sym="INFY", isin=None), bi(exchange="BSE", isin=None))).verdict is Verdict.MATCH


def test_the_same_isin_twice_or_a_shared_symbol_is_ambiguous():
    master = normalize_equity("NSE", "RELIANCE", REL)
    assert cmp(master, view(bi(token="1"), bi(token="2"))).verdict is Verdict.AMBIGUOUS
    shared = cmp(master, view(bi(), bi(isin=INFY)))
    assert shared.verdict is Verdict.AMBIGUOUS and shared.reason == "SYMBOL_SHARED_WITH_OTHER_ISIN"


def test_a_malformed_candidate_isin_for_the_same_symbol_fails_closed():
    r = cmp(normalize_equity("NSE", "RELIANCE", REL), view(bi(isin="INE002A01019")))
    assert r.verdict is Verdict.UNMAPPED and r.reason == "CANDIDATE_ISIN_MALFORMED"


@pytest.mark.parametrize("master", [normalize_equity(None, "X"), normalize_equity("NSE", "RELIANCE", "bad"),
                                    normalize_equity("NFO", "NIFTY 24500 CE"), normalize_equity("NSE", "NIFTY")])
def test_an_unmapped_master_is_unmapped_for_every_broker(master):
    r = cmp(master, view(bi()))
    assert r.verdict is Verdict.UNMAPPED and r.confidence is Confidence.UNMAPPED and r.master_key is None


# ── capability (existing SegmentCaps / supports_order) ───────────────────────────────────

def test_capability_uses_the_existing_profiles_and_their_evidence_levels():
    nse = normalize_equity("NSE", "RELIANCE", REL)
    assert cmp(nse, view(bi())).verdict is Verdict.MATCH                              # Alice NSE CASH is VERIFIED_LIVE
    bse = normalize_equity("BSE", "RELIANCE", REL)
    r = cmp(bse, view(bi(exchange="BSE")))                                            # Alice BSE CASH is only UNVERIFIED
    assert r.verdict is Verdict.SEGMENT_NOT_SUPPORTED and r.reason == "BSE_CASH_NOT_SUPPORTED_AT_VERIFIED_LIVE"
    assert cmp(bse, view(bi(exchange="BSE")), minimum=Verification.UNVERIFIED).verdict is Verdict.MATCH
    g = cmp(nse, view(bi("groww"), broker="groww"), GROWW)                            # Groww is docs-only
    assert g.verdict is Verdict.SEGMENT_NOT_SUPPORTED
    assert cmp(nse, view(bi("groww"), broker="groww"), GROWW, minimum=Verification.VERIFIED_DOCS).verdict is Verdict.MATCH


def test_product_and_order_type_go_through_supports_order():
    nse = normalize_equity("NSE", "RELIANCE", REL)
    assert cmp(nse, view(bi()), product="CNC", order_type="SL-M").verdict is Verdict.MATCH
    assert cmp(nse, view(bi()), product="WHATEVER", order_type="LIMIT").verdict is Verdict.SEGMENT_NOT_SUPPORTED
    assert cmp(nse, view(bi()), product="MIS", order_type="ICEBERG").verdict is Verdict.SEGMENT_NOT_SUPPORTED
    with pytest.raises(ValueError):
        cmp(nse, view(bi()), product="MIS")


def test_no_profile_or_an_unsupported_broker_is_not_supported():
    nse = normalize_equity("NSE", "RELIANCE", REL)
    assert cmp(nse, view(bi()), None).reason == "NO_CAPABILITY_PROFILE"
    zerodha = build_profile("zerodha")
    assert cmp(nse, view(bi("zerodha"), broker="zerodha"), zerodha).verdict is Verdict.SEGMENT_NOT_SUPPORTED


# ── catalog presence and freshness ───────────────────────────────────────────────────────

def test_a_missing_catalog_is_distinct_from_an_empty_one():
    master = normalize_equity("NSE", "RELIANCE", REL)
    assert cmp(master, BrokerView("alicebluenew", None, FRESH)).verdict is Verdict.CATALOG_NOT_LOADED
    assert cmp(master, BrokerView("alicebluenew", (), FRESH)).reason == "NOT_FOUND"


@pytest.mark.parametrize("loaded,reason", [
    (None, "LOAD_TIME_UNKNOWN"),
    (NOW - timedelta(hours=24, seconds=1), "CATALOG_OLDER_THAN_LIMIT"),
    (NOW + timedelta(seconds=1), "LOAD_TIME_IN_FUTURE"),
    (datetime(2026, 10, 22, 5, 0), "NAIVE_TIMESTAMP"),
])
def test_stale_unknown_or_unusable_load_times_fail_closed(loaded, reason):
    r = cmp(normalize_equity("NSE", "RELIANCE", REL), view(bi(), loaded=loaded))
    assert r.verdict is Verdict.MASTER_DATA_STALE and r.reason == reason


def test_the_age_limit_is_inclusive_and_configurable():
    master = normalize_equity("NSE", "RELIANCE", REL)
    assert cmp(master, view(bi(), loaded=NOW - timedelta(hours=24))).verdict is Verdict.MATCH
    assert cmp(master, view(bi(), loaded=NOW - timedelta(hours=2)), max_age=timedelta(hours=1)).verdict is Verdict.MASTER_DATA_STALE


# ── lot / tick ───────────────────────────────────────────────────────────────────────────

MASTER = normalize_equity("NSE", "RELIANCE", REL)


def test_matching_verified_lot_and_tick_is_a_plain_match():
    r = cmp(MASTER, view(bi("groww", lot=1, tick=Decimal("0.05"))), master_details=bi(lot=1, tick=Decimal("0.050")))
    assert r.verdict is Verdict.MATCH and r.diffs == () and r.unverified_attributes == ()


def test_differing_lot_or_tick_is_an_attribute_diff_listing_each():
    r = cmp(MASTER, view(bi("groww", lot=5, tick=Decimal("0.10"))), master_details=bi(lot=1, tick=Decimal("0.05")))
    assert r.verdict is Verdict.ATTRIBUTE_DIFF
    assert [(d.attribute, d.master, d.broker) for d in r.diffs] == [("lot_size", 1, 5), ("tick_size", Decimal("0.05"), Decimal("0.10"))]
    assert r.matched is not None


def test_missing_or_invalid_attribute_data_is_never_invented_or_compared():
    r = cmp(MASTER, view(bi(lot=5)), master_details=None)
    assert r.verdict is Verdict.MATCH and r.unverified_attributes == ("lot_size", "tick_size")
    zero = cmp(MASTER, view(bi(lot=0, tick=Decimal("0"))), master_details=bi(lot=1, tick=Decimal("0.05")))
    assert zero.verdict is Verdict.MATCH and zero.unverified_attributes == ("lot_size", "tick_size")
    half = cmp(MASTER, view(bi(lot=2)), master_details=bi(lot=1, tick=Decimal("0.05")))
    assert half.verdict is Verdict.ATTRIBUTE_DIFF and half.unverified_attributes == ("tick_size",)


def test_a_boolean_or_non_integer_lot_size_is_not_verified_data():
    for bad in (True, 1.0, "1"):
        r = cmp(MASTER, view(bi(lot=bad, tick=Decimal("0.05"))), master_details=bi(lot=1, tick=Decimal("0.05")))
        assert r.verdict is Verdict.MATCH and r.unverified_attributes == ("lot_size",)


# ── determinism ──────────────────────────────────────────────────────────────────────────

def test_results_are_deterministic_ordered_and_input_order_independent():
    a, g = view(bi(), broker="alicebluenew"), view(bi("groww"), broker="groww")
    profiles = {"alicebluenew": ALICE, "groww": GROWW}
    one = compare_all(MASTER, [g, a], profiles, now=NOW)
    two = compare_all(MASTER, [a, g], profiles, now=NOW)
    assert one == two and [c.broker_id for c in one] == ["alicebluenew", "groww"]
    assert compare_all(MASTER, [a, g], profiles, now=NOW) == one


def test_candidate_order_does_not_change_the_result():
    cands = [bi(token=str(i), isin=REL if i == 2 else INFY, sym="RELIANCE" if i == 2 else "INFY") for i in range(5)]
    assert cmp(MASTER, view(*cands)) == cmp(MASTER, view(*reversed(cands)))


def test_the_package_does_no_io():
    import app.services.instruments.comparator as c
    import app.services.instruments.normalizer as n
    for mod in (c, n):
        src = open(mod.__file__, encoding="utf-8").read()
        for banned in ("import redis", "httpx", "requests", "AsyncSession", "sqlalchemy", "socket"):
            assert banned not in src, (mod.__name__, banned)
