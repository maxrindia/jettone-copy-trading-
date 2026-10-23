"""
Shadow-only NSE crosswalk evidence (R4i).

Connects the offline NSE token -> ISIN crosswalk (R4h) to the read-only instrument shadow report. For each persisted Master
(exchange, symbol, instrument_token) it records what the operator-supplied security-master snapshot says about that token.
It influences nothing: no broker call, no network, no Redis, no order path, no migration, and the Child-order planner and
the copy pipeline never import this module.

Honesty rules
-------------
* A candidate ISIN is ALWAYS tagged MASTER_TOKEN_LINK_UNVERIFIED. Nothing yet proves that the `instrumentId` Alice reports on a
  Master order equals the contract-master `Token` (R4g), so the ISIN is a lead for an operator, never an identity. It is never
  passed to the comparator, so it can never produce a MATCH.
* The lookup key is the persisted token (with the exchange). A symbol, a company name or a lot size is never used to find or
  to confirm a record; the symbol is compared only to REJECT a candidate whose symbol disagrees.
* Only NSE cash-equity series in SUPPORTED_SERIES can yield a candidate. BSE and every derivative exchange stay unmapped.
* Fail closed: no file, an unusable file, a stale snapshot, a conflict, a deleted or invalid row all yield a state with no ISIN.
* The file is read only from the explicitly configured local path; nothing is downloaded.
"""
import os
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.config import settings
from app.services.instruments.nse_crosswalk import (
    CrosswalkFileError, CrosswalkStatus, NseCrosswalk, load_nse_security_master,
)
from app.services.instruments.normalizer import normalize_exchange, normalize_symbol

EVENT_TYPE = "INSTRUMENT_SHADOW_CROSSWALK"
LINK_STATUS = "MASTER_TOKEN_LINK_UNVERIFIED"
SUPPORTED_SERIES = frozenset({"EQ", "BE", "BZ", "SM", "ST"})      # NSE cash-equity series; every other series is unsupported here
REPORT_ROW_CAP = 100

LINK_NOTE = ("A candidate ISIN comes from an operator-supplied NSE security-master file keyed by the Master's persisted "
             "instrument token. It stays MASTER_TOKEN_LINK_UNVERIFIED until authorised evidence shows that Alice's order-book "
             "instrumentId equals the contract-master Token. It is never a definitive match and is not used by the planner.")


class State(str, Enum):
    CANDIDATE = "CANDIDATE"
    CROSSWALK_UNAVAILABLE = "CROSSWALK_UNAVAILABLE"
    EXCHANGE_NOT_COVERED = "EXCHANGE_NOT_COVERED"
    NO_MASTER_TOKEN = "NO_MASTER_TOKEN"
    MASTER_SYMBOL_UNUSABLE = "MASTER_SYMBOL_UNUSABLE"
    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    CONFLICT = "CONFLICT"
    DELETED = "DELETED"
    INVALID_ISIN = "INVALID_ISIN"
    UNMAPPED = "UNMAPPED"
    UNSUPPORTED_SERIES = "UNSUPPORTED_SERIES"
    SYMBOL_DISAGREES = "SYMBOL_DISAGREES"


_STATE_OF_STATUS = {
    CrosswalkStatus.STALE_SNAPSHOT: State.STALE_SNAPSHOT,
    CrosswalkStatus.CONFLICT: State.CONFLICT,
    CrosswalkStatus.DELETED: State.DELETED,
    CrosswalkStatus.INVALID_ISIN: State.INVALID_ISIN,
    CrosswalkStatus.UNMAPPED: State.UNMAPPED,
}

# ── configuration and cached loading ─────────────────────────────────────────────────────────────

NOT_CONFIGURED = "NOT_CONFIGURED"
_cache_lock = threading.Lock()
_cache: Dict[Tuple[str, int, int], Tuple[Optional[NseCrosswalk], Optional[str]]] = {}

# Directory mode (R7): the configured path may be a DIRECTORY the operator drops official-named snapshot files into. The process
# notices a newer one without a restart. Constants, not settings: they are safety margins, not tuning knobs.
RESCAN_SECONDS = 60          # the directory is listed at most this often (a parse happens only for a NEW candidate file)
SETTLE_SECONDS = 60          # a candidate must have been unmodified this long: a half-copied CSV can still parse
MIN_ROW_RATIO = 0.8          # a replacement indexing fewer than this share of the current rows is refused as truncated
_OFFICIAL_NAME = re.compile(r"^NSE_CM_security_(\d{2})(\d{2})(\d{4})\.csv(\.gz)?$", re.I)
_clock = time.monotonic      # scan throttle; replaceable in tests
_wall = time.time            # file age; replaceable in tests

# Refresh outcome codes: stable, no path or file content.
ROTATED, LOADED = "ROTATED", "LOADED"
NO_NEWER_SNAPSHOT, NO_CANDIDATE_FILES = "NO_NEWER_SNAPSHOT", "NO_CANDIDATE_FILES"
CANDIDATE_NOT_SETTLED, CANDIDATE_INVALID = "CANDIDATE_NOT_SETTLED", "CANDIDATE_INVALID"
ROW_COUNT_DROP, AMBIGUOUS_SAME_DATE = "ROW_COUNT_DROP", "AMBIGUOUS_SAME_DATE"
SAME_DATE_FILE_CHANGED, DIRECTORY_UNREADABLE = "SAME_DATE_FILE_CHANGED", "DIRECTORY_UNREADABLE"


def configured_path() -> str:
    return str(getattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", "") or "").strip()


def is_configured() -> bool:
    return bool(configured_path())


def _error_code(exc: Exception) -> str:
    """A stable code only - never the message, which can quote a path or file content."""
    m = re.match(r"^([A-Z][A-Z_]{4,})\b", str(exc))
    return m.group(1) if m else "FILE_UNUSABLE"


@dataclass(frozen=True)
class _DirState:
    """One immutable view of a snapshot directory. Replaced as a whole, so a reader holds either the old view or the new one."""
    crosswalk: Optional[NseCrosswalk]
    error: Optional[str]
    refresh: Dict[str, Any]
    scanned_at: float                                   # monotonic
    current_key: Optional[Tuple[str, int, int]]         # (file name, size, mtime_ns) of the loaded snapshot
    rejected: frozenset                                 # (name, size, mtime_ns) of candidates already refused: not re-parsed
    rotations: int


_dir_lock = threading.Lock()                            # one refresh at a time
_dir_states: Dict[str, _DirState] = {}


def get_crosswalk(path: Optional[str] = None) -> Tuple[Optional[NseCrosswalk], Optional[str]]:
    """(crosswalk, None) or (None, error_code). Reads the configured LOCAL file - or, when the path is a directory, its newest valid
    official-named file - once per file version. An unusable file is remembered too, so it is not re-parsed every cycle. Never
    raises, never touches the network."""
    path = configured_path() if path is None else str(path).strip()
    if not path:
        return None, NOT_CONFIGURED
    if os.path.isdir(path) or path in _dir_states:      # a directory that vanished keeps serving its last good snapshot
        state = _directory_state(path)
        return state.crosswalk, state.error
    try:
        st = os.stat(path)
    except OSError:
        return None, "FILE_UNUSABLE"
    key = (path, st.st_mtime_ns, st.st_size)
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    try:
        outcome: Tuple[Optional[NseCrosswalk], Optional[str]] = (load_nse_security_master(path), None)
    except CrosswalkFileError as exc:
        outcome = (None, _error_code(exc))
    except Exception:                                   # any parser surprise is still "unusable", never a crash
        outcome = (None, "FILE_UNUSABLE")
    with _cache_lock:
        _cache.clear()                                  # one file is configured: keep only its latest version
        _cache[key] = outcome
    return outcome


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
    with _dir_lock:
        _dir_states.clear()


def _directory_state(path: str) -> _DirState:
    state = _dir_states.get(path)
    if state is not None and _clock() - state.scanned_at < RESCAN_SECONDS:
        return state
    with _dir_lock:
        state = _dir_states.get(path)
        if state is not None and _clock() - state.scanned_at < RESCAN_SECONDS:
            return state                                # another thread refreshed while this one waited
        fresh = _rescan(path, state)
        _dir_states[path] = fresh                       # one reference assignment: readers never see a half-built state
        return fresh


def _candidates(path: str) -> Optional[List[Tuple[date, str, Tuple[str, int, int], float]]]:
    """Official-named regular files as (snapshot date, name, key, mtime), or None when the directory cannot be listed."""
    try:
        entries = list(os.scandir(path))
    except OSError:
        return None
    out = []
    for entry in entries:
        m = _OFFICIAL_NAME.match(entry.name)
        if not m:
            continue                                    # temp files, notes, anything else: ignored
        try:
            if not entry.is_file():
                continue
            snapshot = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
            st = entry.stat()
        except (OSError, ValueError):
            continue
        out.append((snapshot, entry.name, (entry.name, st.st_size, st.st_mtime_ns), st.st_mtime))
    return out


def _rescan(path: str, prev: Optional[_DirState]) -> _DirState:
    """Look for a strictly newer valid snapshot. Any failure leaves the last good one in place."""
    current = prev.crosswalk if prev is not None else None
    rejected = set(prev.rejected) if prev is not None else set()
    rotations = prev.rotations if prev is not None else 0
    now_wall = datetime.now(timezone.utc).isoformat()
    first_problem: Optional[Tuple[str, Optional[str]]] = None

    def state(cw, err, outcome, candidate=None, key=None, rotated=0, extra=None):
        refresh = {
            "mode": "DIRECTORY", "last_scan_at": now_wall, "outcome": outcome, "candidate_filename": candidate,
            "rotations": rotations + rotated, "candidates_refused": len(rejected), **(extra or {}),
        }
        return _DirState(cw, err, refresh, _clock(), key if key is not None else (prev.current_key if prev else None),
                         frozenset(rejected), rotations + rotated)

    listed = _candidates(path)
    keep_error = (prev.error if prev is not None and current is None else None) or ("FILE_UNUSABLE" if current is None else None)
    if listed is None:
        return state(current, keep_error, DIRECTORY_UNREADABLE)
    if not listed:
        return state(current, keep_error, NO_CANDIDATE_FILES)

    current_date = current.provenance.snapshot_date if current is not None else None
    by_date: Dict[date, list] = {}
    for item in listed:
        by_date.setdefault(item[0], []).append(item)

    def note(outcome: str, name: Optional[str]) -> None:
        nonlocal first_problem
        if first_problem is None:
            first_problem = (outcome, name)

    for snapshot in sorted(by_date, reverse=True):
        if current_date is not None and snapshot <= current_date:
            if snapshot == current_date and prev is not None and prev.current_key is not None:
                if any(item[2] != prev.current_key for item in by_date[snapshot]):
                    note(SAME_DATE_FILE_CHANGED, by_date[snapshot][0][1])      # never replace a snapshot with a same-date file
            break
        group = by_date[snapshot]
        if len(group) > 1:
            note(AMBIGUOUS_SAME_DATE, group[0][1])                              # two files, one date: no winner is chosen
            continue
        _, name, key, mtime = group[0]
        if key in rejected:
            note(CANDIDATE_INVALID, name)
            continue
        if _wall() - mtime < SETTLE_SECONDS:
            note(CANDIDATE_NOT_SETTLED, name)                                   # retried on a later scan
            continue
        try:
            loaded = load_nse_security_master(os.path.join(path, name))
        except CrosswalkFileError as exc:
            rejected.add(key)
            note(CANDIDATE_INVALID, name)
            continue
        except Exception:
            rejected.add(key)
            note(CANDIDATE_INVALID, name)
            continue
        if current is not None and loaded.provenance.rows_indexed < MIN_ROW_RATIO * current.provenance.rows_indexed:
            rejected.add(key)
            note(ROW_COUNT_DROP, name)
            continue
        outcome = ROTATED if current is not None else LOADED
        refused = {} if first_problem is None else {"refused_outcome": first_problem[0], "refused_candidate": first_problem[1]}
        return state(loaded, None, outcome, candidate=name, key=key, rotated=1 if current is not None else 0, extra=refused)

    outcome, name = first_problem if first_problem is not None else (NO_NEWER_SNAPSHOT, None)
    return state(current, keep_error, outcome, candidate=name)


def crosswalk_status(path: Optional[str] = None) -> Dict[str, Any]:
    """How the snapshot was obtained and how the last refresh went. Codes and file names only - never the configured path."""
    path = configured_path() if path is None else str(path).strip()
    if not path:
        return {"mode": "NOT_CONFIGURED", "outcome": NOT_CONFIGURED}
    if os.path.isdir(path) or path in _dir_states:
        get_crosswalk(path)
        state = _dir_states.get(path)
        return dict(state.refresh) if state is not None else {"mode": "DIRECTORY", "outcome": "UNKNOWN"}
    crosswalk, error = get_crosswalk(path)
    return {"mode": "FILE", "outcome": LOADED if crosswalk is not None else (error or "FILE_UNUSABLE")}


def snapshot_summary(crosswalk: Optional[NseCrosswalk], now: datetime) -> Optional[Dict[str, Any]]:
    if crosswalk is None:
        return None
    p = crosswalk.provenance
    return {
        "source_filename": p.source_filename, "snapshot_date": p.snapshot_date.isoformat(), "sha256": p.sha256,
        "content_sha256": p.content_sha256, "loaded_at": p.loaded_at.isoformat(), "age_trading_days": crosswalk.age_trading_days(now),
        "stale": crosswalk.is_stale(now), "rows_indexed": p.rows_indexed, "rows_total": p.rows_total, "rows_rejected": p.rows_rejected,
        "conflicting_tokens": p.conflicting_tokens, "deleted_rows": p.deleted_rows, "invalid_isin_rows": p.invalid_isin_rows,
    }


# ── evaluation ───────────────────────────────────────────────────────────────────────────────────

def _payload(exchange: str, symbol: str, token: Optional[str], state: State, reason: str, *,
             crosswalk: Optional[NseCrosswalk], now: datetime, isin: Optional[str] = None,
             series: Optional[str] = None, crosswalk_symbol: Optional[str] = None, conflicting: int = 0) -> Dict[str, Any]:
    return {
        "exchange": normalize_exchange(exchange)[:20],
        "symbol": str(symbol or "").strip().upper()[:60],
        "master_token": token,
        "state": state.value,
        "reason": reason,
        "link_status": LINK_STATUS,                                    # constant: no code path produces a verified link
        "candidate_isin": isin if state is State.CANDIDATE else None,
        "crosswalk_series": series,
        "crosswalk_symbol": crosswalk_symbol,
        "conflicting_rows": conflicting,
        "snapshot": snapshot_summary(crosswalk, now),
    }


def evaluate(exchange: Any, symbol: Any, token: Any, crosswalk: Optional[NseCrosswalk], error: Optional[str],
             now: datetime) -> Dict[str, Any]:
    """The sanitised evidence for one Master (exchange, symbol, token). Pure; writes nothing."""
    exch = normalize_exchange(exchange)
    tok = str(token).strip() if token not in (None, "") else None
    if exch != "NSE":
        return _payload(exch, symbol, tok, State.EXCHANGE_NOT_COVERED, "ONLY_NSE_CASH_EQUITY", crosswalk=crosswalk, now=now)
    if tok is None:
        return _payload(exch, symbol, None, State.NO_MASTER_TOKEN, "ORDER_HAS_NO_INSTRUMENT_TOKEN", crosswalk=crosswalk, now=now)
    if crosswalk is None:
        return _payload(exch, symbol, tok, State.CROSSWALK_UNAVAILABLE, error or "FILE_UNUSABLE", crosswalk=None, now=now)
    sym, sym_reason = normalize_symbol(symbol)
    if sym is None:
        return _payload(exch, symbol, tok, State.MASTER_SYMBOL_UNUSABLE, sym_reason, crosswalk=crosswalk, now=now)

    result = crosswalk.lookup(tok, now=now, exchange=exch)
    if result.status is not CrosswalkStatus.MATCH:
        rec = result.record
        return _payload(exch, symbol, tok, _STATE_OF_STATUS[result.status], result.reason, crosswalk=crosswalk, now=now,
                        series=rec.series if rec else None, crosswalk_symbol=rec.symbol if rec else None,
                        conflicting=len(result.conflicting))
    rec = result.record
    if rec.series not in SUPPORTED_SERIES:
        return _payload(exch, symbol, tok, State.UNSUPPORTED_SERIES, f"SERIES_{rec.series}", crosswalk=crosswalk, now=now,
                        series=rec.series, crosswalk_symbol=rec.symbol)
    if rec.symbol.upper() != sym:                        # the symbol can only veto a candidate, never create or confirm one
        return _payload(exch, symbol, tok, State.SYMBOL_DISAGREES, "ORDER_SYMBOL_DIFFERS_FROM_SNAPSHOT", crosswalk=crosswalk,
                        now=now, series=rec.series, crosswalk_symbol=rec.symbol)
    return _payload(exch, symbol, tok, State.CANDIDATE, "TOKEN_LINK_UNVERIFIED", crosswalk=crosswalk, now=now,
                    isin=result.isin, series=rec.series, crosswalk_symbol=rec.symbol)


def identity_of(payload: Dict[str, Any]) -> Tuple[str, str, str]:
    return (payload["exchange"], payload["symbol"], payload["master_token"] or "-")


def dedupe_key(payload: Dict[str, Any], day: str) -> str:
    snap = payload.get("snapshot") or {}
    return "|".join([
        "CW", f"{payload['exchange']}:{payload['symbol']}:{payload['master_token'] or '-'}", payload["state"], payload["reason"],
        payload["candidate_isin"] or "-", payload["crosswalk_series"] or "-", snap.get("sha256") or "-",
        snap.get("snapshot_date") or "-", str(snap.get("stale", "-")), payload["link_status"], day,
    ])


# ── report section ───────────────────────────────────────────────────────────────────────────────

def build_section(
    events: Sequence[Tuple[Optional[datetime], Dict[str, Any]]], now: datetime, *, events_truncated: bool = False,
) -> Dict[str, Any]:
    """`events` are (created_at, metadata) newest first. Counts describe the CURRENT state: the latest event per
    (exchange, symbol, token). The live file state is read from the configured local file (cached), never from a network."""
    configured = is_configured()
    crosswalk, error = get_crosswalk() if configured else (None, NOT_CONFIGURED)
    latest: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for created, payload in events:
        if not isinstance(payload, dict) or "state" not in payload:
            continue
        key = (str(payload.get("exchange")), str(payload.get("symbol")), str(payload.get("master_token") or "-"))
        latest.setdefault(key, {**{k: payload.get(k) for k in (
            "exchange", "symbol", "master_token", "state", "reason", "candidate_isin", "crosswalk_series", "crosswalk_symbol",
            "conflicting_rows", "snapshot")}, "link_status": LINK_STATUS,                # never trust a stored value for this
            "created_at": created.isoformat() if created else None})
    by_state = Counter(r["state"] for r in latest.values())
    rows = sorted(latest.values(), key=lambda r: (r["state"], r["exchange"], r["symbol"], r["master_token"] or ""))
    return {
        "configured": configured,
        "link_status": LINK_STATUS,
        "note": LINK_NOTE,
        "live_snapshot": snapshot_summary(crosswalk, now),
        "refresh": crosswalk_status() if configured else {"mode": "NOT_CONFIGURED", "outcome": NOT_CONFIGURED},
        "load_error": None if crosswalk is not None or not configured else error,
        "events_read": len(events),
        "events_truncated": bool(events_truncated),
        "instrument_count": len(latest),
        "candidates": by_state.get(State.CANDIDATE.value, 0),
        "by_state": dict(sorted(by_state.items())),
        "rows": rows[:REPORT_ROW_CAP],
        "rows_truncated": len(rows) > REPORT_ROW_CAP,
    }


# ── coverage of a set of Master instruments (R6; read-only, no shadow job needed) ─────────────────

# How each state is grouped for an operator. A CANDIDATE is never "verified": identity stays unverified in every group.
COVERAGE_GROUPS = {
    "identity_unverified": (State.CANDIDATE,),
    "no_mapping_found": (State.UNMAPPED,),
    "source_unavailable": (State.CROSSWALK_UNAVAILABLE,),
    "source_stale": (State.STALE_SNAPSHOT,),
    "no_master_token": (State.NO_MASTER_TOKEN,),
    "exchange_not_covered": (State.EXCHANGE_NOT_COVERED,),
    "needs_review": (State.CONFLICT, State.DELETED, State.INVALID_ISIN, State.UNSUPPORTED_SERIES,
                     State.SYMBOL_DISAGREES, State.MASTER_SYMBOL_UNUSABLE),
}


def coverage_summary(subjects: Sequence[Tuple[Any, Any, Any]], now: datetime) -> Dict[str, Any]:
    """Counts of what the configured local NSE snapshot says about distinct Master (exchange, symbol, token) subjects.

    `verified_identities` is ALWAYS 0: no authorised evidence yet shows that Alice's `instrumentId` equals the contract-master
    `Token`, so even a CANDIDATE is only MASTER_TOKEN_LINK_UNVERIFIED. `source` says whether the snapshot could be used at all, so
    an absent or unusable file is never read as "nothing to map"."""
    configured = is_configured()
    crosswalk, error = get_crosswalk() if configured else (None, NOT_CONFIGURED)
    source = "NOT_CONFIGURED" if not configured else ("LOADED" if crosswalk is not None else "FILE_UNUSABLE")
    seen = set()
    states: Counter = Counter()
    for exchange, symbol, token in subjects:
        key = (str(exchange or "").strip().upper(), str(symbol or "").strip().upper(),
               str(token).strip() if token not in (None, "") else None)
        if key in seen:
            continue
        seen.add(key)
        states[evaluate(key[0], key[1], key[2], crosswalk, error, now)["state"]] += 1
    snapshot = snapshot_summary(crosswalk, now)
    return {
        "source": source,
        "source_error": None if source != "FILE_UNUSABLE" else error,
        "snapshot": snapshot,
        "refresh": crosswalk_status() if configured else {"mode": "NOT_CONFIGURED", "outcome": NOT_CONFIGURED},
        "total_instruments": len(seen),
        "verified_identities": 0,
        "link_status": LINK_STATUS,
        "candidates": states.get(State.CANDIDATE.value, 0),
        "by_state": dict(sorted(states.items())),
        "groups": {g: sum(states.get(s.value, 0) for s in members) for g, members in COVERAGE_GROUPS.items()},
    }
