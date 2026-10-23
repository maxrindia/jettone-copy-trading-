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
from collections import Counter
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional, Sequence, Tuple

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


def configured_path() -> str:
    return str(getattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", "") or "").strip()


def is_configured() -> bool:
    return bool(configured_path())


def _error_code(exc: Exception) -> str:
    """A stable code only - never the message, which can quote a path or file content."""
    m = re.match(r"^([A-Z][A-Z_]{4,})\b", str(exc))
    return m.group(1) if m else "FILE_UNUSABLE"


def get_crosswalk(path: Optional[str] = None) -> Tuple[Optional[NseCrosswalk], Optional[str]]:
    """(crosswalk, None) or (None, error_code). Reads the configured LOCAL file once per file version (path, mtime, size);
    an unusable file is remembered too, so it is not re-parsed every cycle. Never raises, never touches the network."""
    path = configured_path() if path is None else str(path).strip()
    if not path:
        return None, NOT_CONFIGURED
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


def snapshot_summary(crosswalk: Optional[NseCrosswalk], now: datetime) -> Optional[Dict[str, Any]]:
    if crosswalk is None:
        return None
    p = crosswalk.provenance
    return {
        "source_filename": p.source_filename, "snapshot_date": p.snapshot_date.isoformat(), "sha256": p.sha256,
        "content_sha256": p.content_sha256, "age_trading_days": crosswalk.age_trading_days(now),
        "stale": crosswalk.is_stale(now), "rows_indexed": p.rows_indexed, "conflicting_tokens": p.conflicting_tokens,
        "deleted_rows": p.deleted_rows, "invalid_isin_rows": p.invalid_isin_rows,
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

def build_section(events: Sequence[Tuple[Optional[datetime], Dict[str, Any]]], now: datetime) -> Dict[str, Any]:
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
        "load_error": None if crosswalk is not None or not configured else error,
        "instrument_count": len(latest),
        "candidates": by_state.get(State.CANDIDATE.value, 0),
        "by_state": dict(sorted(by_state.items())),
        "rows": rows[:REPORT_ROW_CAP],
        "rows_truncated": len(rows) > REPORT_ROW_CAP,
    }
