"""
Instrument terms model, Alice contract-master parsing and the pure quantity validator (OBSERVE foundation). Offline and pure: no
broker, Redis, database or network. The Alice parser is exercised on real rows copied from the committed-format contract master
(columns verified against the NSE / BSE / NFO snapshots) and, when the full snapshots are present locally, on those too.
"""
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.services.instruments import terms as T
from app.services.instruments.terms import (
    ALICE, ANGEL, InstrumentTerms, Knowledge, Outcome, TermsStatus, TermsStore, angel_identity_only, observe_quantity,
    parse_alice_contract_master, validate_quantity,
)

NOW = datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc)
NFO_HEADER = "Exch,Exchange Segment,Symbol,Token,Instrument Type,Option Type,Strike Price,Instrument Name,Formatted Ins Name,Trading Symbol,Expiry Date,Lot Size,Tick Size"
NSE_HEADER = "Exch,Exchange Segment,Group Name,Symbol,Token,Instrument Type,Instrument Name,Formatted Ins Name,Trading Symbol,Lot Size,Tick Size"
NFO_ROWS = [
    "NFO,nse_fo,MIDCPNIFTY,35066,OPTIDX,CE,12725,MIDCPNIFTY,MIDCPNIFTY NOV 12725 CE,MIDCPNIFTY23NOV26C12725,2026-11-23,120,0.05",
    "NFO,nse_fo,NIFTYFPI,35956,FUTIDX,XX,-1,NIFTYFPI,NIFTYFPI OCT FUT,NIFTYFPI27OCT26F,2026-10-27,1100,0.05",
]
NSE_ROWS = [
    "NSE,nse_cm,EQ,011NSETEST,14747,0,011NSETEST,011NSETEST-EQ,011NSETEST-EQ,1,0.05",
    "NSE,nse_cm,SM,GRAPHISAD,20454,0,GRAPHISADS LIMITED,GRAPHISAD-SM,GRAPHISAD-SM,1200,0.05",
]
SNAPSHOTS = Path(os.environ.get("ALICE_CM_SNAPSHOT_DIR", "")) if os.environ.get("ALICE_CM_SNAPSHOT_DIR") else None


def csv_text(header, rows):
    return "\n".join([header] + rows) + "\n"


def parse_nfo(rows, **kw):
    return parse_alice_contract_master("NFO", csv_text(NFO_HEADER, rows), loaded_at=NOW, **kw)


def terms(**kw):
    base = dict(broker=ALICE, exchange="NFO", token="1", lot_size=65, lot_state=Knowledge.KNOWN, freeze_quantity=1800,
                freeze_state=Knowledge.KNOWN, source="test", loaded_at=NOW)
    base.update(kw)
    return InstrumentTerms(**base)


# ── Alice parser ─────────────────────────────────────────────────────────────────────────────────

def test_parser_retains_lot_expiry_strike_and_option_type():
    out, rep = parse_nfo(NFO_ROWS[:1])
    t = out[(ALICE, "NFO", "35066")]
    assert (t.lot_size, t.lot_state) == (120, Knowledge.KNOWN)
    assert (t.expiry, t.strike, t.option_type, t.instrument_type) == (date(2026, 11, 23), Decimal("12725"), "CE", "OPTIDX")
    assert t.trading_symbol == "MIDCPNIFTY23NOV26C12725" and t.status is TermsStatus.VALID
    assert rep.rows == 1 and rep.stored == 1 and rep.invalid == 0


def test_alice_master_has_no_freeze_column_so_freeze_is_unknown_never_zero_or_unlimited():
    out, _ = parse_nfo(NFO_ROWS)
    for t in out.values():
        assert t.freeze_state is Knowledge.UNKNOWN and t.freeze_quantity is None


def test_future_sentinel_xx_and_minus_one_is_not_applicable_not_malformed():
    out, rep = parse_nfo(NFO_ROWS[1:])
    t = out[(ALICE, "NFO", "35956")]
    assert t.status is TermsStatus.VALID and t.option_type is None and t.strike is None and rep.invalid == 0


@pytest.mark.parametrize("strike", ["abc", "-2", "nan"])
def test_only_the_exact_minus_one_sentinel_is_forgiven_on_a_future(strike):
    out, rep = parse_nfo([NFO_ROWS[1].replace(",-1,", f",{strike},")])
    assert out[(ALICE, "NFO", "35956")].status is TermsStatus.INVALID and rep.invalid == 1


def test_minus_one_strike_on_a_real_option_row_is_still_malformed():
    bad = NFO_ROWS[0].replace(",12725,", ",-1,")
    out, rep = parse_nfo([bad])
    assert out[(ALICE, "NFO", "35066")].status is TermsStatus.INVALID and "STRIKE_MALFORMED" in out[(ALICE, "NFO", "35066")].issues
    assert rep.invalid == 1


def test_equity_rows_parse_with_their_lot():
    out, _ = parse_alice_contract_master("NSE", csv_text(NSE_HEADER, NSE_ROWS), loaded_at=NOW)
    assert out[(ALICE, "NSE", "20454")].lot_size == 1200 and out[(ALICE, "NSE", "14747")].lot_size == 1


@pytest.mark.parametrize("lot,state,issue", [("0", Knowledge.INVALID, "LOT_NON_POSITIVE"), ("-5", Knowledge.INVALID, "LOT_MALFORMED"),
                                             ("abc", Knowledge.INVALID, "LOT_MALFORMED"), ("1.5", Knowledge.INVALID, "LOT_MALFORMED"),
                                             ("99999999999", Knowledge.INVALID, "LOT_OUT_OF_RANGE"), ("", Knowledge.UNKNOWN, None)])
def test_malformed_or_non_positive_lots_are_never_trusted(lot, state, issue):
    row = NFO_ROWS[0].replace(",120,0.05", f",{lot},0.05")
    out, _ = parse_nfo([row])
    t = out[(ALICE, "NFO", "35066")]
    assert t.lot_state is state and t.lot_size is None
    assert (issue in t.issues) if issue else not t.issues


def test_malformed_expiry_is_flagged():
    out, _ = parse_nfo([NFO_ROWS[0].replace("2026-11-23", "23-NOV-2026")])
    assert "EXPIRY_MALFORMED" in out[(ALICE, "NFO", "35066")].issues


@pytest.mark.parametrize("token", ["", "abc", "12 34", "-3"])
def test_rows_with_a_malformed_token_are_dropped_and_counted(token):
    out, rep = parse_nfo([NFO_ROWS[0].replace(",35066,", f",{token},")])
    assert out == {} and rep.rejected["TOKEN_MALFORMED"] == 1


def test_identical_duplicate_rows_collapse():
    out, rep = parse_nfo([NFO_ROWS[0], NFO_ROWS[0]])
    assert len(out) == 1 and rep.duplicates_collapsed == 1 and rep.conflicts == 0
    assert out[(ALICE, "NFO", "35066")].lot_size == 120


def test_differing_duplicate_rows_are_a_conflict_and_carry_no_lot():
    out, rep = parse_nfo([NFO_ROWS[0], NFO_ROWS[0].replace(",120,0.05", ",75,0.05")])
    t = out[(ALICE, "NFO", "35066")]
    assert t.status is TermsStatus.CONFLICT and t.lot_size is None and rep.conflicts == 1
    assert T.validate_quantity(t, 120, now=NOW).codes == (T.TERMS_CONFLICT,)


@pytest.mark.parametrize("header", [NFO_HEADER.replace("Lot Size,", ""), NFO_HEADER.replace("Token,", "")])
def test_a_file_missing_a_required_column_yields_nothing(header):
    out, rep = parse_alice_contract_master("NFO", csv_text(header, []), loaded_at=NOW)
    assert out == {} and rep.missing_columns


def test_the_same_token_on_different_exchanges_are_different_identities():
    nse, _ = parse_alice_contract_master("NSE", csv_text(NSE_HEADER, NSE_ROWS), loaded_at=NOW)
    bse_row = NSE_ROWS[1].replace("NSE,nse_cm", "BSE,bse_cm").replace("1200", "50")
    bse, _ = parse_alice_contract_master("BSE", csv_text(NSE_HEADER, [bse_row]), loaded_at=NOW)
    assert (ALICE, "NSE", "20454") in nse and (ALICE, "BSE", "20454") in bse
    assert nse[(ALICE, "NSE", "20454")].lot_size != bse[(ALICE, "BSE", "20454")].lot_size


def test_a_row_for_another_exchange_is_flagged_not_silently_stored():
    out, _ = parse_alice_contract_master("NSE", csv_text(NSE_HEADER, [NSE_ROWS[0].replace("NSE,nse_cm", "BSE,bse_cm")]), loaded_at=NOW)
    assert "EXCHANGE_MISMATCH" in out[(ALICE, "NSE", "14747")].issues


# ── store ────────────────────────────────────────────────────────────────────────────────────────

def test_store_lookup_is_by_broker_exchange_token_only():
    s = TermsStore()
    s.load_alice("NFO", csv_text(NFO_HEADER, NFO_ROWS), loaded_at=NOW)
    assert s.get(ALICE, "NFO", "35066").lot_size == 120
    assert s.get(ALICE, "nfo", " 35066 ").lot_size == 120                     # exchange / token are normalised, nothing else
    assert s.get(ALICE, "NSE", "35066") is None                               # other exchange
    assert s.get(ANGEL, "NFO", "35066") is None                               # other broker
    assert s.get(ALICE, "NFO", "3506") is None and s.get(ALICE, "NFO", "350666") is None   # no substring identity
    assert s.get(ALICE, "NFO", "MIDCPNIFTY") is None                          # no symbol identity


def test_a_failed_or_empty_reload_keeps_the_previous_terms():
    s = TermsStore()
    s.load_alice("NFO", csv_text(NFO_HEADER, NFO_ROWS), loaded_at=NOW)
    s.load_alice("NFO", "garbage", loaded_at=NOW)
    s.load_alice("NFO", csv_text(NFO_HEADER, []), loaded_at=NOW)
    assert s.get(ALICE, "NFO", "35066").lot_size == 120


def test_a_good_reload_replaces_the_exchange_atomically():
    s = TermsStore()
    s.load_alice("NFO", csv_text(NFO_HEADER, NFO_ROWS), loaded_at=NOW)
    s.load_alice("NFO", csv_text(NFO_HEADER, [NFO_ROWS[0].replace(",120,0.05", ",150,0.05")]), loaded_at=NOW)
    assert s.get(ALICE, "NFO", "35066").lot_size == 150 and s.get(ALICE, "NFO", "35956") is None


# ── validator matrix ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("qty", [0, -1, 1.5, "65", None, True, False])
def test_a_non_positive_or_non_integer_quantity_is_rejected(qty):
    v = validate_quantity(terms(), qty, now=NOW)
    assert v.outcome is Outcome.REJECT and v.codes == (T.QUANTITY_INVALID,) and not v.verified_safe


def test_missing_terms_are_unknown_not_allowed():
    v = validate_quantity(None, 65, now=NOW)
    assert v.outcome is Outcome.UNKNOWN and v.codes == (T.TERMS_MISSING,) and not v.verified_safe


@pytest.mark.parametrize("qty,outcome,codes", [
    (65, Outcome.ALLOW, (T.LOT_OK, T.FREEZE_OK)),
    (1800, Outcome.REJECT, (T.NOT_LOT_MULTIPLE, T.FREEZE_OK)),            # 1800 % 65 != 0, within freeze
    (1755, Outcome.ALLOW, (T.LOT_OK, T.FREEZE_OK)),                       # 27 lots == 1755, at the freeze boundary below 1800
    (1820, Outcome.REJECT, (T.LOT_OK, T.EXCEEDS_FREEZE)),                 # 28 lots, over freeze
    (66, Outcome.REJECT, (T.NOT_LOT_MULTIPLE, T.FREEZE_OK)),
    (1, Outcome.REJECT, (T.NOT_LOT_MULTIPLE, T.FREEZE_OK)),
])
def test_known_lot_and_freeze_matrix(qty, outcome, codes):
    v = validate_quantity(terms(), qty, now=NOW)
    assert (v.outcome, v.codes) == (outcome, codes)
    assert v.verified_safe is (outcome is Outcome.ALLOW)


def test_freeze_boundary_is_inclusive():
    t = terms(lot_size=1, freeze_quantity=1800)
    assert validate_quantity(t, 1800, now=NOW).outcome is Outcome.ALLOW
    assert validate_quantity(t, 1801, now=NOW).codes == (T.LOT_OK, T.EXCEEDS_FREEZE)


@pytest.mark.parametrize("qty", [65, 130, 65 * 1000, 66])
def test_unknown_freeze_is_never_unlimited_so_a_lot_multiple_is_only_unknown(qty):
    t = terms(freeze_quantity=None, freeze_state=Knowledge.UNKNOWN)
    v = validate_quantity(t, qty, now=NOW)
    if qty % 65 == 0:
        assert v.outcome is Outcome.UNKNOWN and v.codes == (T.LOT_OK, T.FREEZE_UNKNOWN) and not v.verified_safe
    else:
        assert v.outcome is Outcome.REJECT                                   # a known lot violation is still a rejection


def test_known_freeze_violation_is_rejected_even_when_the_lot_is_unknown():
    t = terms(lot_size=None, lot_state=Knowledge.UNKNOWN)
    v = validate_quantity(t, 5000, now=NOW)
    assert v.outcome is Outcome.REJECT and v.codes == (T.LOT_UNKNOWN, T.EXCEEDS_FREEZE)


def test_unknown_lot_with_a_quantity_inside_the_freeze_is_unknown_not_allowed():
    t = terms(lot_size=None, lot_state=Knowledge.UNKNOWN)
    v = validate_quantity(t, 100, now=NOW)
    assert v.outcome is Outcome.UNKNOWN and v.codes == (T.LOT_UNKNOWN, T.FREEZE_OK)


def test_both_unknown_is_unknown():
    t = terms(lot_size=None, lot_state=Knowledge.UNKNOWN, freeze_quantity=None, freeze_state=Knowledge.UNKNOWN)
    v = validate_quantity(t, 100, now=NOW)
    assert v.outcome is Outcome.UNKNOWN and v.codes == (T.LOT_UNKNOWN, T.FREEZE_UNKNOWN)


def test_invalid_lot_or_freeze_states_reject():
    assert validate_quantity(terms(lot_size=None, lot_state=Knowledge.INVALID), 65, now=NOW).codes[0] == T.LOT_INVALID
    assert validate_quantity(terms(freeze_quantity=None, freeze_state=Knowledge.INVALID), 65, now=NOW).codes[1] == T.FREEZE_INVALID


def test_a_known_state_with_a_missing_or_zero_value_is_not_trusted():
    # state KNOWN but no usable number: must not be read as "multiple of 0" or "limit 0 / unlimited"
    assert validate_quantity(terms(lot_size=0), 65, now=NOW).lot.outcome is Outcome.UNKNOWN
    assert validate_quantity(terms(freeze_quantity=0), 65, now=NOW).freeze.outcome is Outcome.UNKNOWN
    assert validate_quantity(terms(freeze_quantity=None), 65, now=NOW).freeze.outcome is Outcome.UNKNOWN


@pytest.mark.parametrize("status,code", [(TermsStatus.CONFLICT, T.TERMS_CONFLICT), (TermsStatus.INVALID, T.TERMS_INVALID)])
def test_conflicting_or_invalid_terms_reject_whatever_the_quantity(status, code):
    v = validate_quantity(terms(status=status, issues=("X",)), 65, now=NOW)
    assert v.outcome is Outcome.REJECT and v.codes == (code,)


def test_stale_terms_are_unknown_even_when_everything_else_matches():
    old = terms(loaded_at=NOW - timedelta(hours=25))
    v = validate_quantity(old, 65, now=NOW)
    assert v.outcome is Outcome.UNKNOWN and v.codes == (T.TERMS_STALE,) and v.freshness == "STALE"


def test_undated_and_future_dated_terms_are_not_fresh():
    assert validate_quantity(terms(loaded_at=None), 65, now=NOW).codes == (T.TERMS_UNDATED,)
    assert validate_quantity(terms(loaded_at=NOW + timedelta(hours=2)), 65, now=NOW).codes == (T.TERMS_STALE,)
    assert validate_quantity(terms(loaded_at=NOW - timedelta(hours=23)), 65, now=NOW).outcome is Outcome.ALLOW


def test_validator_is_pure_and_never_raises_on_odd_input():
    t = terms()
    for q in (object(), [], {}, float("nan"), 10 ** 30):
        validate_quantity(t, q, now=NOW)
    assert t == terms()


# ── the lookup used by the pipeline ───────────────────────────────────────────────────────────────

def loaded_store():
    s = TermsStore()
    s.load_alice("NFO", csv_text(NFO_HEADER, NFO_ROWS), loaded_at=NOW)
    return s


def test_observe_same_broker_with_token_finds_the_terms_but_alice_has_no_freeze():
    v = observe_quantity(child_broker=ALICE, master_broker=ALICE, exchange="NFO", token="35066", quantity=120, now=NOW, terms_store=loaded_store())
    assert v.outcome is Outcome.UNKNOWN and v.codes == (T.LOT_OK, T.FREEZE_UNKNOWN) and not v.verified_safe


def test_observe_flags_a_real_lot_violation_from_the_master_data():
    v = observe_quantity(child_broker=ALICE, master_broker=ALICE, exchange="NFO", token="35066", quantity=1, now=NOW, terms_store=loaded_store())
    assert v.outcome is Outcome.REJECT and T.NOT_LOT_MULTIPLE in v.codes


@pytest.mark.parametrize("kw,code", [
    (dict(child_broker=ANGEL, master_broker=ALICE), T.CROSS_BROKER_TOKEN),
    (dict(child_broker=None, master_broker=ALICE), T.BROKER_UNKNOWN),
    (dict(child_broker=ALICE, master_broker=None), T.BROKER_UNKNOWN),
])
def test_observe_never_matches_a_master_token_to_another_brokers_child(kw, code):
    v = observe_quantity(exchange="NFO", token="35066", quantity=120, now=NOW, terms_store=loaded_store(), **kw)
    assert v.outcome is Outcome.UNKNOWN and v.codes == (code,)


@pytest.mark.parametrize("token", [None, "", "  "])
def test_observe_without_a_token_is_unknown_and_never_guesses_from_the_symbol(token):
    v = observe_quantity(child_broker=ALICE, master_broker=ALICE, exchange="NFO", token=token, quantity=120, now=NOW, terms_store=loaded_store())
    assert v.outcome is Outcome.UNKNOWN and v.codes == (T.TOKEN_MISSING,)


def test_observe_unknown_token_is_unknown():
    v = observe_quantity(child_broker=ALICE, master_broker=ALICE, exchange="NFO", token="999", quantity=120, now=NOW, terms_store=loaded_store())
    assert v.codes == (T.TERMS_MISSING,)


# ── Angel One: identity only ──────────────────────────────────────────────────────────────────────

def test_angel_terms_carry_identity_only_and_everything_else_unknown():
    t = angel_identity_only("nfo", "12345", loaded_at=NOW)
    assert t.key == (ANGEL, "NFO", "12345")
    assert (t.lot_state, t.freeze_state) == (Knowledge.UNKNOWN, Knowledge.UNKNOWN)
    assert t.lot_size is None and t.freeze_quantity is None and t.expiry is None and t.strike is None
    v = validate_quantity(t, 75, now=NOW)
    assert v.outcome is Outcome.UNKNOWN and v.codes == (T.LOT_UNKNOWN, T.FREEZE_UNKNOWN)


# ── full snapshots (only when present locally) ───────────────────────────────────────────────────

@pytest.mark.skipif(SNAPSHOTS is None or not (SNAPSHOTS / "NFO.csv").exists(), reason="set ALICE_CM_SNAPSHOT_DIR to the local contract-master snapshots")
@pytest.mark.parametrize("exch,min_rows", [("NSE", 9000), ("BSE", 6000), ("NFO", 39000)])
def test_full_snapshots_parse_cleanly(exch, min_rows):
    out, rep = parse_alice_contract_master(exch, (SNAPSHOTS / f"{exch}.csv").read_text(encoding="utf-8"), loaded_at=NOW)
    assert rep.stored >= min_rows and rep.rows == rep.stored and rep.invalid == 0 and rep.conflicts == 0 and not rep.rejected
    assert all(t.freeze_state is Knowledge.UNKNOWN for t in out.values())
    # Index rows (e.g. NIFTY 50) are not tradable and state no lot: UNKNOWN, never INVALID, never defaulted to 1.
    not_known = [t for t in out.values() if t.lot_state is not Knowledge.KNOWN]
    assert all(t.lot_state is Knowledge.UNKNOWN and t.instrument_type == "INDEX" for t in not_known)
