"""
R4h: the offline NSE token -> ISIN crosswalk prototype (app/services/instruments/nse_crosswalk.py).

It reads one operator-supplied official NSE security-master file, never fetches anything, and is imported by nothing in the engine.
The fixture `tests/fixtures/synthetic_nse_cm_security.csv` is ENTIRELY SYNTHETIC: the official 120 column names (schema only) and 14
invented rows - tokens from 9,000,001, symbols `SYN...`, names `SYNTHETIC ...`, and ISINs built with the non-issuer prefix `ZZ` and a
computed check digit, so none can equal a real instrument. It holds ordinary securities, a placeholder-ISIN row, a deleted security,
and two symbols that each exist in two series (to prove tokens never collapse on a symbol). Variants for duplicates, conflicts and bad
rows are derived from it in the tests. The clock is always injected. No real NSE data belongs in this repository: the exchange's
data-use terms have not been cleared for redistribution.
"""
import ast
import csv
import dataclasses
import gzip
import hashlib
import io
import json
import socket
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.services.instruments import normalizer
from app.services.instruments import nse_crosswalk as cwm
from app.services.instruments.nse_crosswalk import (
    CrosswalkFileError,
    CrosswalkStatus as S,
    load_nse_security_master,
    weekdays_since,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "synthetic_nse_cm_security.csv"
IST = timezone(timedelta(hours=5, minutes=30))
SNAP = date(2026, 10, 22)                                   # a Thursday
LOADED = datetime(2026, 10, 23, 6, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=IST)


def ist(y, m, d, h=12, mi=0, s=0):
    return datetime(y, m, d, h, mi, s, tzinfo=IST)


def table():
    """(header, rows as dicts) of the fixture."""
    reader = csv.DictReader(io.StringIO(FIXTURE.read_text(encoding="utf-8"), newline=""))
    return list(reader.fieldnames), list(reader)


def dump(header, rows):
    out = io.StringIO(newline="")
    w = csv.DictWriter(out, fieldnames=header, lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return out.getvalue()


def put(tmp_path, text, name="NSE_CM_security_22102026.csv", gz=False):
    p = tmp_path / (name + (".gz" if gz and not name.endswith(".gz") else ""))
    data = text.encode("utf-8")
    p.write_bytes(gzip.compress(data, mtime=0) if gz else data)
    return p


def load(path, **kw):
    kw.setdefault("loaded_at", LOADED)
    return load_nse_security_master(path, **kw)


@pytest.fixture
def cw():
    return load(FIXTURE, snapshot_date=SNAP)


def row_for(rows, token):
    return next(r for r in rows if r["FinInstrmId"] == token)


# ══ loading: plain CSV and gzip, provenance ══════════════════════════════════════════════

def test_the_fixture_has_the_official_column_structure():
    header, rows = table()
    assert len(header) == 120 and header[:5] == ["FinInstrmId", "TckrSymb", "SctySrs", "FinInstrmNm", "ISIN"]
    assert "DelFlg" in header and "NewBrdLotQty" in header and "UpdDt" in header and len(rows) == 14


def test_the_fixture_is_entirely_synthetic():
    """Guards against real exchange rows being put back: redistribution of NSE data has not been cleared."""
    header, rows = table()
    used = {"FinInstrmId", "TckrSymb", "SctySrs", "FinInstrmNm", "ISIN", "NewBrdLotQty", "DelFlg", "UpdDt"}
    assert len({r["FinInstrmId"] for r in rows}) == 14
    for r in rows:
        assert int(r["FinInstrmId"]) >= 9_000_000                 # the official file's tokens have at most 6 digits; this range starts above
        assert r["TckrSymb"].startswith("SYN") and r["FinInstrmNm"].startswith("SYNTHETIC")
        assert r["ISIN"].startswith(("ZZ", "SYN"))                # ZZ is not an issuer country code; SYN... is a deliberate placeholder
        assert all(not r[c] for c in header if c not in used)     # no row-specific value (capital, dates, price bands, ...) is carried
    assert not any(r["ISIN"].startswith(("IN", "XS")) for r in rows)


def test_a_plain_csv_and_the_same_content_gzipped_load_identically(tmp_path):
    plain = put(tmp_path, FIXTURE.read_text(encoding="utf-8"), "NSE_CM_security_22102026.csv")
    zipped = put(tmp_path, FIXTURE.read_text(encoding="utf-8"), "NSE_CM_security_22102026.csv", gz=True)
    a, b = load(plain), load(zipped)
    assert (a.provenance.compressed, b.provenance.compressed) == (False, True)
    assert a.provenance.content_sha256 == b.provenance.content_sha256
    assert a.provenance.sha256 != b.provenance.sha256                       # the supplied bytes differ, the content does not
    assert a._records == b._records and len(a) == len(b) == 14


def test_provenance_records_the_file_the_dates_and_the_counts(tmp_path):
    raw = FIXTURE.read_bytes()
    cw = load(FIXTURE, snapshot_date=SNAP, source_url="https://example.invalid/operator-declared")
    p = cw.provenance
    assert p.source_filename == FIXTURE.name and p.source_url == "https://example.invalid/operator-declared"
    assert p.sha256 == p.content_sha256 == hashlib.sha256(raw).hexdigest()
    assert p.snapshot_date == SNAP and p.loaded_at == LOADED and p.observed_update_date == SNAP
    assert (p.rows_total, p.rows_indexed, p.rows_rejected) == (14, 14, 0)
    assert (p.identical_duplicates, p.conflicting_tokens, p.invalid_isin_rows, p.deleted_rows, p.symbol_series_collisions) == (0, 0, 1, 1, 0)
    assert load(FIXTURE, snapshot_date=SNAP).provenance.source_url is None            # nothing is invented or fetched


def test_the_snapshot_date_comes_from_the_official_file_name(tmp_path):
    p = put(tmp_path, FIXTURE.read_text(encoding="utf-8"), "NSE_CM_security_22102026.csv")
    assert load(p).provenance.snapshot_date == SNAP
    assert load(put(tmp_path, FIXTURE.read_text(encoding="utf-8"), "nse_cm_security_22102026.csv.gz", gz=True)).provenance.snapshot_date == SNAP


def test_loading_twice_is_deterministic():
    a, b = load(FIXTURE, snapshot_date=SNAP), load(FIXTURE, snapshot_date=SNAP)
    assert a.provenance == b.provenance and a._records == b._records
    assert a.lookup("9000003", now=NOW) == b.lookup("9000003", now=NOW)


# ══ lookups keyed by (exchange, token) ═══════════════════════════════════════════════════

@pytest.mark.parametrize("token,symbol,series,isin,lot", [
    ("9000003", "SYNALPHA", "EQ", "ZZSYN001A014", 1), ("9000004", "SYNBETA", "EQ", "ZZSYN002A012", 1), ("9000005", "SYNGAMMA", "EQ", "ZZSYN003A010", 1),
    ("9000006", "SYNDELTA", "EQ", "ZZSYN004A018", 1), ("9000007", "SYNEPSILON", "EQ", "ZZSYN005A015", 1),
    ("9000008", "SYNSME", "SM", "ZZSYN006A013", 600), ("9000009", "SYNBOND", "GB", "ZZSYN007A011", 1),
])
def test_a_token_resolves_to_its_record_and_isin(cw, token, symbol, series, isin, lot):
    r = cw.lookup(token, now=NOW)
    assert r.status is S.MATCH and r.usable and r.reason == "OK" and r.isin == isin
    assert (r.record.exchange, r.record.token, r.record.symbol, r.record.series, r.record.lot_size) == ("NSE", token, symbol, series, lot)


def test_the_same_symbol_in_two_series_stays_two_distinct_securities(cw):
    """SYNCOLA (EQ and D1) and SYNCOLB (EQ and W1) share a symbol but not a token, a series or an ISIN."""
    pairs = {"SYNCOLA": (("9000010", "EQ", "ZZSYN008A019"), ("9000011", "D1", "ZZSYN009A017")),
             "SYNCOLB": (("9000012", "EQ", "ZZSYN010A015"), ("9000013", "W1", "ZZSYN011A013"))}
    for symbol, ((t1, s1, i1), (t2, s2, i2)) in pairs.items():
        a, b = cw.lookup(t1, now=NOW), cw.lookup(t2, now=NOW)
        assert (a.record.symbol, a.record.series, a.isin) == (symbol, s1, i1)
        assert (b.record.symbol, b.record.series, b.isin) == (symbol, s2, i2)
        assert a.isin != b.isin and a.record != b.record
    assert cw.provenance.symbol_series_collisions == 0
    assert len(cw) == 14                                                      # nothing collapsed on the symbol


def test_there_is_no_symbol_keyed_lookup():
    public = [n for n in dir(cwm.NseCrosswalk) if not n.startswith("_")]
    assert sorted(public) == ["age_trading_days", "is_stale", "lookup"]
    import inspect
    assert list(inspect.signature(cwm.NseCrosswalk.lookup).parameters) == ["self", "token", "now", "exchange"]


def test_the_exchange_is_part_of_the_key(cw):
    assert cw.lookup("9000003", now=NOW, exchange="nse").status is S.MATCH
    for other in ("BSE", "NFO", "", None):
        r = cw.lookup("9000003", now=NOW, exchange=other)
        assert (r.status, r.reason, r.isin) == (S.UNMAPPED, "EXCHANGE_NOT_COVERED", None)


def test_a_missing_token_is_unmapped(cw):
    r = cw.lookup("9999999", now=NOW)
    assert (r.status, r.reason, r.isin, r.record) == (S.UNMAPPED, "TOKEN_NOT_IN_SNAPSHOT", None, None)


@pytest.mark.parametrize("token", ["", " 9000003", "9000003 ", "09000003", "0", "-5", "1.5", "abc", "1e3", "12345678901", None, 9000003, True, b"9000003"])
def test_a_malformed_lookup_token_is_unmapped_never_coerced(cw, token):
    r = cw.lookup(token, now=NOW)
    assert (r.status, r.reason, r.isin) == (S.UNMAPPED, "MALFORMED_TOKEN", None)


# ══ ISIN validation, placeholders, deleted rows ══════════════════════════════════════════

def test_the_isin_check_uses_the_existing_project_validator():
    assert cwm.is_valid_isin is normalizer.is_valid_isin


def test_a_placeholder_isin_is_invalid_and_carries_no_isin(cw):
    r = cw.lookup("9000001", now=NOW)                                           # NSE test symbol, ISIN "SYNDUMMY001"
    assert (r.status, r.reason, r.isin) == (S.INVALID_ISIN, "PLACEHOLDER_OR_MALFORMED_ISIN", None)
    assert not r.usable and r.record.symbol == "SYNTEST1"


@pytest.mark.parametrize("isin,reason", [("", "BLANK_ISIN"), ("   ", "BLANK_ISIN"), ("ZZSYN001A015", "BAD_CHECK_DIGIT"),
                                         ("ZZSYN001A01", "PLACEHOLDER_OR_MALFORMED_ISIN"), ("1ZSYN001A014", "PLACEHOLDER_OR_MALFORMED_ISIN"),
                                         ("ZZSYN001A0144", "PLACEHOLDER_OR_MALFORMED_ISIN")])
def test_each_kind_of_bad_isin_is_reported_with_its_reason(tmp_path, isin, reason):
    header, rows = table()
    row_for(rows, "9000003")["ISIN"] = isin
    r = load(put(tmp_path, dump(header, rows))).lookup("9000003", now=NOW)
    assert (r.status, r.reason, r.isin) == (S.INVALID_ISIN, reason, None)


def test_isin_case_and_padding_are_normalised_but_nothing_else(tmp_path):
    header, rows = table()
    row_for(rows, "9000003")["ISIN"] = "  zzsyn001a014 "
    assert load(put(tmp_path, dump(header, rows))).lookup("9000003", now=NOW).isin == "ZZSYN001A014"


def test_a_deleted_security_is_unsupported_even_with_a_valid_isin(cw):
    r = cw.lookup("9000002", now=NOW)
    assert (r.status, r.reason, r.isin) == (S.DELETED, "DELFLG_Y", None) and r.record.isin == "ZZSYN000D010"


def test_only_y_and_n_are_accepted_as_the_deletion_flag(tmp_path):
    header, rows = table()
    row_for(rows, "9000003")["DelFlg"] = ""
    row_for(rows, "9000004")["DelFlg"] = "X"
    cw = load(put(tmp_path, dump(header, rows)))
    assert dict(cw.provenance.rejected_by_reason) == {"UNKNOWN_DELFLG": 2} and len(cw) == 12
    assert cw.lookup("9000003", now=NOW).reason == "ROW_REJECTED:UNKNOWN_DELFLG" and cw.lookup("9000003", now=NOW).status is S.UNMAPPED


# ══ duplicates and conflicts ═════════════════════════════════════════════════════════════

def test_an_identical_duplicate_row_collapses_and_is_counted(tmp_path):
    header, rows = table()
    rows.append(dict(row_for(rows, "9000003")))
    cw = load(put(tmp_path, dump(header, rows)))
    assert (cw.provenance.rows_total, cw.provenance.rows_indexed, cw.provenance.identical_duplicates) == (15, 14, 1)
    assert cw.lookup("9000003", now=NOW).status is S.MATCH


@pytest.mark.parametrize("field,value", [("ISIN", "ZZSYN002A012"), ("TckrSymb", "SYNALPHAX"), ("SctySrs", "BE"), ("DelFlg", "Y"), ("NewBrdLotQty", "5")])
def test_a_differing_duplicate_is_a_conflict_with_no_winner(tmp_path, field, value):
    header, rows = table()
    clash = dict(row_for(rows, "9000003"))
    clash[field] = value
    rows.append(clash)
    cw = load(put(tmp_path, dump(header, rows)))
    r = cw.lookup("9000003", now=NOW)
    assert (r.status, r.reason, r.isin, r.record) == (S.CONFLICT, "DIFFERING_ROWS_FOR_TOKEN", None, None)
    assert len(r.conflicting) == 2 and {c.isin for c in r.conflicting} | {c.symbol for c in r.conflicting} | {c.series for c in r.conflicting}
    assert cw.provenance.conflicting_tokens == 1 and len(cw) == 13 and cw.lookup("9000004", now=NOW).status is S.MATCH   # others unaffected


def test_the_order_of_conflicting_rows_does_not_pick_a_winner(tmp_path):
    header, rows = table()
    clash = dict(row_for(rows, "9000003")); clash["ISIN"] = "ZZSYN002A012"
    a = load(put(tmp_path, dump(header, rows + [clash]), "NSE_CM_security_22102026.csv"))
    b = load(put(tmp_path, dump(header, [clash] + rows), "NSE_CM_security_22102026.csv"))
    assert a.lookup("9000003", now=NOW).status is b.lookup("9000003", now=NOW).status is S.CONFLICT
    assert {c.isin for c in a.lookup("9000003", now=NOW).conflicting} == {c.isin for c in b.lookup("9000003", now=NOW).conflicting}


def test_two_tokens_sharing_a_symbol_and_series_are_counted_but_never_merged(tmp_path):
    header, rows = table()
    twin = dict(row_for(rows, "9000003")); twin["FinInstrmId"] = "9000099"; twin["ISIN"] = "ZZSYN002A012"
    cw = load(put(tmp_path, dump(header, rows + [twin])))
    assert cw.provenance.symbol_series_collisions == 1
    assert cw.lookup("9000003", now=NOW).isin == "ZZSYN001A014" and cw.lookup("9000099", now=NOW).isin == "ZZSYN002A012"


# ══ rejected rows and unusable files ═════════════════════════════════════════════════════

@pytest.mark.parametrize("token", ["", "0", "abc", "-1", "09000003", "1.5", " 5", "12345678901"])
def test_a_row_with_a_malformed_identifier_is_rejected_not_indexed(tmp_path, token):
    header, rows = table()
    bad = dict(row_for(rows, "9000003")); bad["FinInstrmId"] = token
    cw = load(put(tmp_path, dump(header, rows + [bad])))
    assert dict(cw.provenance.rejected_by_reason) == {"MALFORMED_TOKEN": 1} and cw.provenance.rows_indexed == 14 and len(cw) == 14


def test_a_row_without_a_symbol_or_series_is_rejected_and_its_token_reports_why(tmp_path):
    header, rows = table()
    row_for(rows, "9000003")["TckrSymb"] = ""
    row_for(rows, "9000004")["SctySrs"] = " "
    cw = load(put(tmp_path, dump(header, rows)))
    assert dict(cw.provenance.rejected_by_reason) == {"MISSING_SERIES": 1, "MISSING_SYMBOL": 1}
    assert cw.lookup("9000003", now=NOW).reason == "ROW_REJECTED:MISSING_SYMBOL"
    assert cw.lookup("9000004", now=NOW).reason == "ROW_REJECTED:MISSING_SERIES"


def test_a_short_or_overlong_row_is_rejected(tmp_path):
    text = FIXTURE.read_text(encoding="utf-8").rstrip("\n") + "\n9000003,SYNALPHA\n" + "5,X,EQ,N,ZZSYN001A014," + ",".join(["x"] * 200) + "\n"
    assert dict(load(put(tmp_path, text)).provenance.rejected_by_reason) == {"MALFORMED_ROW": 2}


@pytest.mark.parametrize("drop", ["FinInstrmId", "TckrSymb", "SctySrs", "ISIN", "DelFlg"])
def test_a_missing_required_column_makes_the_file_unusable(tmp_path, drop):
    header, rows = table()
    keep = [c for c in header if c != drop]
    text = dump(keep, [{k: v for k, v in r.items() if k != drop} for r in rows])
    with pytest.raises(CrosswalkFileError, match="MISSING_REQUIRED_COLUMNS"):
        load(put(tmp_path, text))


def test_optional_columns_may_be_absent(tmp_path):
    header, rows = table()
    keep = [c for c in header if c not in ("FinInstrmNm", "NewBrdLotQty", "UpdDt")]
    cw = load(put(tmp_path, dump(keep, [{k: r[k] for k in keep} for r in rows])))
    r = cw.lookup("9000003", now=NOW)
    assert r.status is S.MATCH and r.record.name == "" and r.record.lot_size is None and cw.provenance.observed_update_date is None


def test_a_duplicate_header_empty_data_and_bad_encoding_are_unusable(tmp_path):
    with pytest.raises(CrosswalkFileError, match="DUPLICATE_HEADER"):
        load(put(tmp_path, "FinInstrmId,TckrSymb,SctySrs,ISIN,DelFlg,ISIN\n1,A,EQ,X,N,Y\n"))
    header, _ = table()
    with pytest.raises(CrosswalkFileError, match="no data rows"):
        load(put(tmp_path, ",".join(header) + "\n"))
    bad = tmp_path / "NSE_CM_security_22102026.csv"
    bad.write_bytes(b"FinInstrmId,TckrSymb\n\xff\xfe\x00bad")
    with pytest.raises(CrosswalkFileError, match="UTF-8"):
        load(bad)


def test_a_gz_name_without_gzip_content_and_the_reverse_are_unusable(tmp_path):
    text = FIXTURE.read_text(encoding="utf-8")
    fake = tmp_path / "NSE_CM_security_22102026.csv.gz"
    fake.write_bytes(text.encode())
    with pytest.raises(CrosswalkFileError, match="FILENAME_CONTENT_MISMATCH"):
        load(fake)
    real_gz = tmp_path / "NSE_CM_security_22102026.csv"
    real_gz.write_bytes(gzip.compress(text.encode()))
    with pytest.raises(CrosswalkFileError, match="FILENAME_CONTENT_MISMATCH"):
        load(real_gz)
    corrupt = tmp_path / "NSE_CM_security_23102026.csv.gz"
    corrupt.write_bytes(b"\x1f\x8b" + b"not really gzip")
    with pytest.raises(CrosswalkFileError, match="gzip"):
        load(corrupt)


def test_a_decompression_bomb_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(cwm, "MAX_DECOMPRESSED_BYTES", 1000)
    with pytest.raises(CrosswalkFileError, match="size limit"):
        load(put(tmp_path, FIXTURE.read_text(encoding="utf-8"), gz=True))


def test_a_missing_file_a_directory_and_a_url_are_refused(tmp_path):
    for target in (tmp_path / "nope.csv", tmp_path):
        with pytest.raises(CrosswalkFileError, match="not a file"):
            load(target)
    for url in ("https://nsearchives.nseindia.com/content/cm/NSE_CM_security_22102026.csv.gz", "http://example.invalid/x.csv", "ftp://x/y"):
        with pytest.raises(CrosswalkFileError, match="never fetches"):
            load(url)


# ══ inconsistent source metadata ═════════════════════════════════════════════════════════

def test_the_snapshot_date_must_be_determinable_and_consistent(tmp_path):
    text = FIXTURE.read_text(encoding="utf-8")
    with pytest.raises(CrosswalkFileError, match="snapshot date is unknown"):
        load(put(tmp_path, text, "security.csv"))
    assert load(put(tmp_path, text, "security.csv"), snapshot_date=SNAP).provenance.snapshot_date == SNAP
    with pytest.raises(CrosswalkFileError, match="INCONSISTENT_SNAPSHOT_DATE"):
        load(put(tmp_path, text, "NSE_CM_security_22102026.csv"), snapshot_date=date(2026, 10, 21))
    with pytest.raises(CrosswalkFileError, match="not a real date"):
        load(put(tmp_path, text, "NSE_CM_security_31022026.csv"))


def test_update_timestamps_far_from_the_snapshot_date_are_inconsistent(tmp_path):
    header, rows = table()
    for r in rows:
        r["UpdDt"] = str(int(r["UpdDt"]) + 40 * 86400)
    text = dump(header, rows)
    with pytest.raises(CrosswalkFileError, match="UPDATE_TIMESTAMPS_INCONSISTENT"):
        load(put(tmp_path, text))
    assert len(load(put(tmp_path, text), verify_update_timestamps=False)) == 14        # an explicit, deliberate override


def test_update_timestamps_within_a_day_are_accepted(tmp_path):
    header, rows = table()
    for r in rows:
        r["UpdDt"] = str(int(r["UpdDt"]) + 86400)
    assert len(load(put(tmp_path, dump(header, rows)))) == 14


# ══ freshness (injected clock, IST weekdays) ═════════════════════════════════════════════

def test_the_reference_dates_are_what_the_tests_assume():
    assert SNAP.weekday() == 3 and date(2026, 10, 26).weekday() == 0


@pytest.mark.parametrize("now,age,stale", [
    (ist(2026, 10, 22, 0, 0, 0), 0, False),
    (ist(2026, 10, 22, 23, 59, 59), 0, False),
    (ist(2026, 10, 23, 0, 0, 0), 1, False),             # Friday
    (ist(2026, 10, 23, 23, 59, 59), 1, False),
    (ist(2026, 10, 24, 12), 1, False),                  # Saturday: still only Friday has passed
    (ist(2026, 10, 25, 23, 59, 59), 1, False),          # Sunday
    (ist(2026, 10, 26, 0, 0, 0), 2, True),              # Monday: Friday and Monday have passed
    (ist(2026, 10, 27, 12), 3, True),
])
def test_freshness_boundaries_in_ist(cw, now, age, stale):
    assert cw.age_trading_days(now) == age and cw.is_stale(now) is stale
    r = cw.lookup("9000003", now=now)
    assert (r.status is S.STALE_SNAPSHOT) is stale and (r.isin is None) is stale
    if stale:
        assert r.reason == f"AGE_{age}_TRADING_DAYS"


def test_the_boundary_is_ist_midnight_not_utc_midnight(cw):
    sunday_late = datetime(2026, 10, 25, 18, 29, 59, tzinfo=timezone.utc)                # 23:59:59 IST Sunday
    monday_start = datetime(2026, 10, 25, 18, 30, 0, tzinfo=timezone.utc)                # 00:00:00 IST Monday
    assert cw.lookup("9000003", now=sunday_late).status is S.MATCH
    assert cw.lookup("9000003", now=monday_start).status is S.STALE_SNAPSHOT


def test_a_fridays_file_is_fresh_on_monday_and_stale_on_tuesday(tmp_path):
    friday = load(put(tmp_path, FIXTURE.read_text(encoding="utf-8"), "NSE_CM_security_23102026.csv"), verify_update_timestamps=False)
    assert friday.provenance.snapshot_date == date(2026, 10, 23)
    assert friday.lookup("9000003", now=ist(2026, 10, 26, 9)).status is S.MATCH
    assert friday.lookup("9000003", now=ist(2026, 10, 27, 9)).status is S.STALE_SNAPSHOT


def test_a_snapshot_dated_in_the_future_is_not_usable(tmp_path):
    future = load(put(tmp_path, FIXTURE.read_text(encoding="utf-8"), "NSE_CM_security_23102026.csv"), verify_update_timestamps=False)
    r = future.lookup("9000003", now=NOW)
    assert (r.status, r.reason, r.isin) == (S.STALE_SNAPSHOT, "FUTURE_SNAPSHOT", None)


def test_staleness_outranks_every_other_result(cw):
    late = ist(2026, 11, 2)
    for token in ("9000003", "9000001", "9000002", "9999999"):
        assert cw.lookup(token, now=late).status is S.STALE_SNAPSHOT
    assert cw.lookup("abc", now=late).reason == "MALFORMED_TOKEN"                        # a malformed key is rejected before anything else
    assert cw.lookup("9000003", now=late, exchange="BSE").reason == "EXCHANGE_NOT_COVERED"


def test_the_clock_must_be_timezone_aware(cw):
    for naive in (datetime(2026, 10, 22, 12, 0), None, "2026-10-22", date(2026, 10, 22)):
        with pytest.raises(ValueError):
            cw.lookup("9000003", now=naive)


def test_weekday_counting():
    assert weekdays_since(date(2026, 10, 22), date(2026, 10, 22)) == 0
    assert weekdays_since(date(2026, 10, 23), date(2026, 10, 26)) == 1
    assert weekdays_since(date(2026, 10, 22), date(2026, 11, 2)) == 7
    assert weekdays_since(date(2026, 10, 23), date(2026, 10, 22)) == -1


# ══ immutability ═════════════════════════════════════════════════════════════════════════

def test_results_records_and_provenance_are_immutable(cw):
    r = cw.lookup("9000003", now=NOW)
    for obj, attr in ((r, "isin"), (r.record, "isin"), (cw.provenance, "sha256")):
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(obj, attr, "x")
    with pytest.raises(TypeError):
        cw._records[("NSE", "1")] = r.record
    with pytest.raises(TypeError):
        cw._conflicts[("NSE", "1")] = ()


# ══ offline and isolated ═════════════════════════════════════════════════════════════════

def test_nothing_touches_the_network(tmp_path, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    cw = load(put(tmp_path, FIXTURE.read_text(encoding="utf-8"), gz=True))
    assert cw.lookup("9000003", now=NOW).status is S.MATCH
    assert cwm.main([str(FIXTURE), "--snapshot-date", "2026-10-22", "--lookup", "9000003"], now=NOW) == 0


def test_the_module_imports_only_the_standard_library_and_the_isin_validator():
    tree = ast.parse(Path(cwm.__file__).read_text(encoding="utf-8"))
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add((n.module or "").split(".")[0] if n.level == 0 else "")
    assert mods <= {"argparse", "csv", "gzip", "hashlib", "io", "json", "re", "sys", "dataclasses", "datetime", "enum", "pathlib",
                    "types", "typing", "app"}
    from_app = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("app")]
    assert from_app == ["app.services.instruments.normalizer"]
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not called & {"urlopen", "request", "connect", "create_connection", "getaddrinfo", "place_order", "modify_order", "cancel_order"}


def test_nothing_in_the_engine_imports_the_crosswalk():
    app = Path(__file__).resolve().parent.parent / "app"
    for path in app.rglob("*.py"):
        if path.name != "nse_crosswalk.py":
            assert "nse_crosswalk" not in path.read_text(encoding="utf-8"), path


# ══ the operator command ═════════════════════════════════════════════════════════════════

def test_the_operator_command_prints_provenance_and_lookups(capsys):
    assert cwm.main([str(FIXTURE), "--snapshot-date", "2026-10-22", "--lookup", "9000003", "--lookup", "9000001", "--lookup", "9999999",
                     "--source-url", "operator-declared"], now=NOW) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["stale"] is False and out["provenance"]["snapshot_date"] == "2026-10-22" and out["provenance"]["source_url"] == "operator-declared"
    assert out["provenance"]["sha256"] == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
    assert [(r["status"], r["isin"]) for r in out["lookups"]] == [("MATCH", "ZZSYN001A014"), ("INVALID_ISIN", None), ("UNMAPPED", None)]


def test_the_operator_command_reports_an_unusable_file_and_a_stale_snapshot(tmp_path, capsys):
    assert cwm.main([str(tmp_path / "missing.csv")], now=NOW) == 2
    assert "UNUSABLE" in capsys.readouterr().err
    assert cwm.main([str(FIXTURE), "--snapshot-date", "not-a-date"], now=NOW) == 2
    capsys.readouterr()
    assert cwm.main([str(FIXTURE), "--snapshot-date", "2026-10-22", "--lookup", "9000003"], now=ist(2026, 11, 5)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["stale"] is True and out["lookups"][0]["status"] == "STALE_SNAPSHOT" and out["lookups"][0]["isin"] is None
