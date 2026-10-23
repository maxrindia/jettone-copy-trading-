"""
R7: rotating the locally supplied NSE snapshot without restarting the engine. Local files and the synthetic fixture only - no
network, broker, Redis or production access. A failed refresh must leave the last good snapshot in place, a partially built
snapshot must never be visible, and an old snapshot must never be presented as fresh.
"""
import hashlib
import json
import socket
import threading
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from app.core.config import settings
from app.services.instruments import crosswalk_evidence as ce
from app.services.instruments import nse_crosswalk as cwm
from app.services.instruments import shadow
from test_crosswalk_evidence import cats, events, make_file, master_order, on  # noqa: F401  (on is a fixture)

D = date(2026, 10, 22)                                                   # a Thursday
NOW = datetime(2026, 10, 22, 6, 0, tzinfo=timezone.utc)
ALPHA = "9000003"


class Ticker:
    """A controllable monotonic clock for the scan throttle."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds=ce.RESCAN_SECONDS + 1):
        self.t += seconds


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    ce.clear_cache()
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", "")
    local = {"127.0.0.1", "::1", "localhost", None, ""}
    real_connect, real_getaddrinfo = socket.socket.connect, socket.getaddrinfo

    def connect(self, address, *a, **k):
        if (address[0] if isinstance(address, tuple) else address) not in local:
            raise AssertionError("network access attempted")
        return real_connect(self, address, *a, **k)

    def getaddrinfo(host, *a, **k):
        if host not in local:
            raise AssertionError("network access attempted")
        return real_getaddrinfo(host, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield
    ce.clear_cache()


@pytest.fixture
def tick(monkeypatch):
    ticker = Ticker()
    monkeypatch.setattr(ce, "_clock", ticker)
    monkeypatch.setattr(ce, "_wall", lambda: time.time() + 10_000)      # every file counts as long settled unless a test says otherwise
    return ticker


def put(folder, snapshot, **kw):
    return make_file(folder, snapshot=snapshot, **kw)


def drop_rows(keep):
    return lambda rows: rows[:keep]


def current(folder):
    return ce.get_crosswalk(str(folder))


def date_of(cw):
    return cw.provenance.snapshot_date


# ── loading from a directory ─────────────────────────────────────────────────────────────

def test_a_file_path_behaves_exactly_as_before(tmp_path, tick):
    path = put(tmp_path, D)
    cw, err = ce.get_crosswalk(str(path))
    assert err is None and date_of(cw) == D
    assert ce.crosswalk_status(str(path)) == {"mode": "FILE", "outcome": "LOADED"}


def test_a_directory_loads_the_newest_valid_file(tmp_path, tick):
    put(tmp_path, D - timedelta(days=2))
    put(tmp_path, D)
    cw, err = current(tmp_path)
    assert err is None and date_of(cw) == D
    status = ce.crosswalk_status(str(tmp_path))
    assert status["mode"] == "DIRECTORY" and status["outcome"] == "LOADED" and status["rotations"] == 0


def test_an_empty_or_unrelated_directory_is_unavailable_with_a_code(tmp_path, tick):
    assert current(tmp_path) == (None, "FILE_UNUSABLE")
    assert ce.crosswalk_status(str(tmp_path))["outcome"] == ce.NO_CANDIDATE_FILES
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "NSE_CM_security_22102026.csv.tmp").write_text("x")      # an upload in progress is not a candidate
    (tmp_path / "NSE_CM_security_99999999.csv").write_text("x")          # not a real date
    tick.advance()
    assert current(tmp_path) == (None, "FILE_UNUSABLE")
    assert ce.crosswalk_status(str(tmp_path))["outcome"] == ce.NO_CANDIDATE_FILES


def test_at_start_an_invalid_newest_file_falls_back_to_an_older_valid_one_which_is_then_stale(tmp_path, tick):
    put(tmp_path, D - timedelta(days=4))
    bad = tmp_path / "NSE_CM_security_22102026.csv"
    bad.write_text("FinInstrmId,TckrSymb\n1,A\n")
    cw, err = current(tmp_path)
    assert err is None and date_of(cw) == D - timedelta(days=4)
    assert cw.is_stale(NOW) is True                                         # honestly old, not presented as fresh
    status = ce.crosswalk_status(str(tmp_path))
    assert status["outcome"] == ce.LOADED and status["refused_outcome"] == ce.CANDIDATE_INVALID      # the refusal stays visible
    assert status["refused_candidate"] == "NSE_CM_security_22102026.csv"


# ── rotation ─────────────────────────────────────────────────────────────────────────────

def test_a_newer_file_is_picked_up_without_a_restart_and_the_scan_is_throttled(tmp_path, tick, monkeypatch):
    put(tmp_path, D - timedelta(days=1))
    first, _ = current(tmp_path)
    put(tmp_path, D)
    again, _ = current(tmp_path)
    assert again is first                                                   # inside the rescan interval: no listing, no parse
    tick.advance()
    rotated, err = current(tmp_path)
    assert err is None and rotated is not first and date_of(rotated) == D
    status = ce.crosswalk_status(str(tmp_path))
    assert status["outcome"] == ce.ROTATED and status["rotations"] == 1 and status["candidate_filename"] == "NSE_CM_security_22102026.csv"
    assert date_of(first) == D - timedelta(days=1)                          # the old snapshot object is untouched (immutable)


def test_a_candidate_still_being_written_is_not_adopted_until_it_settles(tmp_path, monkeypatch):
    ticker = Ticker()
    monkeypatch.setattr(ce, "_clock", ticker)
    put(tmp_path, D - timedelta(days=1))
    monkeypatch.setattr(ce, "_wall", lambda: time.time() + 10_000)
    first, _ = current(tmp_path)
    put(tmp_path, D)
    monkeypatch.setattr(ce, "_wall", time.time)                              # the new file was modified just now
    ticker.advance()
    kept, _ = current(tmp_path)
    assert kept is first and ce.crosswalk_status(str(tmp_path))["outcome"] == ce.CANDIDATE_NOT_SETTLED
    monkeypatch.setattr(ce, "_wall", lambda: time.time() + ce.SETTLE_SECONDS + 5)
    ticker.advance()
    assert date_of(current(tmp_path)[0]) == D                                # retried on a later scan and adopted


def test_an_invalid_replacement_leaves_the_last_snapshot_and_is_not_reparsed(tmp_path, tick, monkeypatch):
    put(tmp_path, D - timedelta(days=1))
    first, _ = current(tmp_path)
    (tmp_path / "NSE_CM_security_22102026.csv").write_text("FinInstrmId,TckrSymb\n1,A\n")
    calls = []
    real = ce.load_nse_security_master
    monkeypatch.setattr(ce, "load_nse_security_master", lambda p: calls.append(p) or real(p))
    tick.advance()
    assert current(tmp_path)[0] is first and ce.crosswalk_status(str(tmp_path))["outcome"] == ce.CANDIDATE_INVALID
    tick.advance()
    assert current(tmp_path)[0] is first
    assert len(calls) == 1                                                  # the refused file is remembered, not re-parsed every scan
    put(tmp_path, D + timedelta(days=1))                                     # a later valid file is still adopted
    tick.advance()
    assert date_of(current(tmp_path)[0]) == D + timedelta(days=1)


def test_a_truncated_replacement_is_refused_even_though_it_parses(tmp_path, tick):
    put(tmp_path, D - timedelta(days=1))
    first, _ = current(tmp_path)
    put(tmp_path, D, rows=drop_rows(10))                                    # 10 of 14 rows: below the 80% floor
    tick.advance()
    assert current(tmp_path)[0] is first and ce.crosswalk_status(str(tmp_path))["outcome"] == ce.ROW_COUNT_DROP
    put(tmp_path, D + timedelta(days=1), rows=drop_rows(12))                # 12 of 14 rows: kept
    tick.advance()
    assert date_of(current(tmp_path)[0]) == D + timedelta(days=1)


@pytest.mark.parametrize("shape", ["older", "same_date_changed", "ambiguous", "unreadable"])
def test_nothing_newer_or_ambiguous_keeps_the_current_snapshot(tmp_path, tick, shape):
    path = put(tmp_path, D)
    first, _ = current(tmp_path)
    if shape == "older":
        put(tmp_path, D - timedelta(days=3))
        expected = ce.NO_NEWER_SNAPSHOT
    elif shape == "same_date_changed":
        path.write_bytes(path.read_bytes() + b"\n")                           # the loaded file was edited in place
        expected = ce.SAME_DATE_FILE_CHANGED
    elif shape == "ambiguous":
        put(tmp_path, D + timedelta(days=1))
        put(tmp_path, D + timedelta(days=1), name="NSE_CM_security_23102026.csv.gz")      # a second file for the same date
        expected = ce.AMBIGUOUS_SAME_DATE
    else:
        import shutil
        shutil.rmtree(tmp_path)
        expected = ce.DIRECTORY_UNREADABLE
    tick.advance()
    cw, err = current(tmp_path)
    assert cw is first and err is None                                        # never replaced, never blanked
    assert ce.crosswalk_status(str(tmp_path))["outcome"] == expected


def test_an_ambiguous_date_is_skipped_but_an_unambiguous_newer_one_still_wins(tmp_path, tick):
    put(tmp_path, D)
    current(tmp_path)
    put(tmp_path, D + timedelta(days=1))
    put(tmp_path, D + timedelta(days=1), name="NSE_CM_security_23102026.csv.gz")
    put(tmp_path, D + timedelta(days=4))
    tick.advance()
    assert date_of(current(tmp_path)[0]) == D + timedelta(days=4)


# ── freshness, provenance ────────────────────────────────────────────────────────────────

def test_a_snapshot_that_cannot_be_replaced_goes_stale_by_the_existing_rule(tmp_path, tick):
    put(tmp_path, D)
    cw, _ = current(tmp_path)
    friday = datetime(2026, 10, 23, 6, 0, tzinfo=timezone.utc)
    monday = datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc)
    assert cw.is_stale(friday) is False and cw.is_stale(monday) is True
    tick.advance()                                                           # a refresh with nothing new does not freshen it
    assert ce.get_crosswalk(str(tmp_path))[0].is_stale(monday) is True
    assert ce.evaluate("NSE", "SYNALPHA", ALPHA, cw, None, monday)["state"] == "STALE_SNAPSHOT"


def test_provenance_is_complete_and_matches_the_file_on_disk(tmp_path, tick):
    put(tmp_path, D - timedelta(days=1))
    current(tmp_path)
    new = put(tmp_path, D)
    tick.advance()
    cw, _ = current(tmp_path)
    summary = ce.snapshot_summary(cw, NOW)
    assert summary["source_filename"] == "NSE_CM_security_22102026.csv" and summary["snapshot_date"] == "2026-10-22"
    assert summary["sha256"] == hashlib.sha256(new.read_bytes()).hexdigest() == summary["content_sha256"]
    assert summary["rows_indexed"] == 14 and summary["rows_total"] == 14 and summary["rows_rejected"] == 0
    assert summary["stale"] is False and summary["age_trading_days"] == 0 and summary["loaded_at"]


def test_r4h_validation_is_unchanged_in_directory_mode(tmp_path, tick):
    def break_rows(rows):
        twin = dict(next(r for r in rows if r["FinInstrmId"] == ALPHA))
        twin["ISIN"] = "ZZSYN002A012"
        return rows + [twin]
    put(tmp_path, D, rows=break_rows)
    cw, _ = current(tmp_path)
    assert ce.evaluate("NSE", "SYNALPHA", ALPHA, cw, None, NOW)["state"] == "CONFLICT"
    assert ce.evaluate("NSE", "SYNDELETED", "9000002", cw, None, NOW)["state"] == "DELETED"
    assert ce.evaluate("NSE", "SYNDUMMY", "9000001", cw, None, NOW)["state"] == "INVALID_ISIN"


def test_the_directory_path_never_appears_in_any_status(tmp_path, tick):
    put(tmp_path, D)
    current(tmp_path)
    settings.INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH = str(tmp_path)
    try:
        section = ce.build_section([], NOW)
        coverage = ce.coverage_summary([("NSE", "SYNALPHA", ALPHA)], NOW)
    finally:
        settings.INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH = ""
    text = json.dumps([section, coverage, ce.crosswalk_status(str(tmp_path))])
    assert str(tmp_path) not in text and tmp_path.name not in text
    assert section["refresh"]["mode"] == "DIRECTORY" and coverage["refresh"]["outcome"] in (ce.LOADED, ce.ROTATED)


# ── concurrency ──────────────────────────────────────────────────────────────────────────

def test_readers_only_ever_see_a_complete_snapshot_while_it_rotates(tmp_path, tick):
    base = D - timedelta(days=5)
    put(tmp_path, base)
    first, _ = current(tmp_path)
    expected = {first.provenance.sha256: (base, first.provenance.rows_indexed)}
    seen, errors, stop = [], [], threading.Event()

    def reader():
        while not stop.is_set():
            try:
                cw, err = ce.get_crosswalk(str(tmp_path))
                assert err is None and cw is not None
                seen.append((cw.provenance.sha256, cw.provenance.snapshot_date, cw.provenance.rows_indexed, len(cw)))
            except Exception as exc:                                         # pragma: no cover - would be the failure
                errors.append(exc)
                return

    threads = [threading.Thread(target=reader) for _ in range(6)]
    for t in threads:
        t.start()
    for offset in range(1, 5):                                              # four rotations, each followed by a new scan window
        snapshot = base + timedelta(days=offset)
        f = put(tmp_path, snapshot)
        tick.advance()
        cw, _ = ce.get_crosswalk(str(tmp_path))
        expected[cw.provenance.sha256] = (snapshot, cw.provenance.rows_indexed)
    stop.set()
    for t in threads:
        t.join()
    assert not errors and seen
    for sha, snapshot, rows, size in seen:
        assert sha in expected and expected[sha] == (snapshot, rows) and size == rows       # never a mixture of two snapshots


def test_concurrent_scans_parse_a_new_file_only_once(tmp_path, tick, monkeypatch):
    put(tmp_path, D - timedelta(days=1))
    current(tmp_path)
    put(tmp_path, D)
    tick.advance()
    calls = []
    real = ce.load_nse_security_master
    monkeypatch.setattr(ce, "load_nse_security_master", lambda p: calls.append(p) or (time.sleep(0.05), real(p))[1])
    results = []
    threads = [threading.Thread(target=lambda: results.append(ce.get_crosswalk(str(tmp_path)))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1 and {date_of(cw) for cw, _ in results} <= {D - timedelta(days=1), D}


# ── the job uses it without a restart ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_shadow_job_follows_a_rotation_without_a_restart(db_session, sample_accounts, sample_strategy, on, tmp_path, tick, monkeypatch):
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", str(tmp_path))
    put(tmp_path, D - timedelta(days=1))
    await master_order(db_session, sample_accounts["master"], "SYNALPHA", ALPHA)
    first = await shadow.run_once(NOW, catalogs=cats())
    assert first.crosswalk_written == 1
    newer = put(tmp_path, D)
    tick.advance()
    second = await shadow.run_once(NOW + timedelta(minutes=5), catalogs=cats())
    assert second.crosswalk_written == 1                                    # new provenance (SHA-256) -> a new event, no restart
    a, b = await events(ce.EVENT_TYPE)
    assert a["snapshot"]["snapshot_date"] == "2026-10-21" and b["snapshot"]["snapshot_date"] == "2026-10-22"
    assert b["snapshot"]["sha256"] == hashlib.sha256(newer.read_bytes()).hexdigest()
    assert a["link_status"] == b["link_status"] == "MASTER_TOKEN_LINK_UNVERIFIED" and b["state"] == "CANDIDATE"


def test_no_new_setting_is_needed_and_nothing_is_enabled_by_default():
    from app.core.config import Settings
    s = Settings(_env_file=None)
    assert s.INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH == "" and s.INSTRUMENT_SHADOW_ENABLED is False
    assert s.CANONICAL_MAPPING_ENFORCED is False
