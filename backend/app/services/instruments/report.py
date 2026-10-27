"""
Read-only shadow report and single-instrument comparison (R4c).

Everything here reads: in-memory catalogs and persisted INSTRUMENT_SHADOW_COMPARE events. Nothing writes, and nothing
calls a broker. Verdict grouping happens here, over the EXISTING comparator verdicts; no new verdict is introduced.
"""
from collections import Counter
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.config import settings
from app.services.brokers.profiles import build_profile
from app.services.instruments import crosswalk_evidence
from app.services.instruments.catalogs import ALICE_ID, ANGEL_ID, GROWW_ID, ZERODHA_ID, InstrumentCatalog, default_catalogs
from app.services.instruments.comparator import compare_instrument
from app.services.instruments.model import BrokerView, CanonicalInstrument
from app.services.instruments.normalizer import normalize_equity
from app.services.instruments.shadow import MIN_EVIDENCE, comparison_payload

REPORT_EVENT_CAP = 5000
MAX_HOURS, MAX_LIMIT, MAX_OFFSET = 168, 500, 100000

COVERAGE_NOTES = {
    ALICE_ID: "Alice Blue: catalog not loaded. Its in-process table mixes hand-written placeholders and has no ISIN, "
              "so Alice is never compared.",
    ANGEL_ID: "Angel One: no production loader populates its catalog, and its data has no ISIN, lot or tick size.",
    GROWW_ID: "Groww: equity (CASH/EQ) rows on NSE and BSE only; an instrument that is missing may simply have been "
              "filtered out when the master was loaded.",
    ZERODHA_ID: "Zerodha: equity rows (NSE/BSE) only, loaded from Kite's instruments dump when enabled; the dump has no ISIN, so a "
                "Zerodha row can only be matched by symbol and is never a definitive MATCH.",
}
MASTER_NOTE = ("Persisted Master orders carry no ISIN, so their identity is symbol-only and can never be a definitive "
               "MATCH. Candidate counts show catalog presence, not parity.")

_PROBE = CanonicalInstrument("NSE", "SHADOW_PROBE", None)


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def group_of(verdict: str, reason: str) -> str:
    """Existing verdicts grouped for the report."""
    if verdict == "MATCH":
        return "matched"
    if verdict == "ATTRIBUTE_DIFF":
        return "attribute_mismatch"
    if verdict == "AMBIGUOUS":
        return "ambiguous"
    if verdict == "CATALOG_NOT_LOADED":
        return "catalog_not_loaded"
    if verdict == "MASTER_DATA_STALE":
        return "catalog_stale"
    if verdict == "SEGMENT_NOT_SUPPORTED":
        return "segment_not_supported"
    if verdict == "UNMAPPED":
        if reason == "ISIN_CONFLICT":
            return "identity_conflict"
        if reason in ("SYMBOL_ONLY_NOT_DEFINITIVE", "CANDIDATE_LACKS_ISIN"):
            return "identity_unconfirmed"
        return "unmapped"
    return "unmapped"


def catalog_states(catalogs: Optional[Sequence[InstrumentCatalog]] = None) -> List[Dict[str, Any]]:
    """Live, in-memory state of each catalog with the honest coverage note."""
    out = []
    for catalog in sorted(catalogs if catalogs is not None else default_catalogs(), key=lambda c: c.broker_id):
        view = catalog.view_for(_PROBE)
        out.append({
            "broker_id": catalog.broker_id,
            "loaded": view.candidates is not None,
            "snapshot_loaded_at": view.loaded_at.isoformat() if view.loaded_at else None,
            "note": COVERAGE_NOTES.get(catalog.broker_id, "No coverage note."),
        })
    return out


def compare_one(exchange: str, symbol: str, isin: Optional[str], now: datetime,
                catalogs: Optional[Sequence[InstrumentCatalog]] = None) -> Dict[str, Any]:
    """Compare one explicitly identified instrument (a caller-supplied ISIN is allowed). Writes nothing."""
    master = normalize_equity(exchange, symbol, isin)
    results = []
    for catalog in sorted(catalogs if catalogs is not None else default_catalogs(), key=lambda c: c.broker_id):
        view = catalog.view_for(master.instrument) if master.instrument else BrokerView(catalog.broker_id)
        result = compare_instrument(master, view, build_profile(catalog.broker_id), now=now, minimum=MIN_EVIDENCE)
        payload = comparison_payload(exchange, symbol, master, view, result)
        payload["group"] = group_of(payload["verdict"], payload["reason"])
        if result.matched is not None:
            m = result.matched
            payload["matched"] = {"exchange": m.exchange, "trading_symbol": m.trading_symbol, "isin": m.isin,
                                  "lot_size": m.lot_size, "tick_size": str(m.tick_size) if m.tick_size is not None else None}
        results.append(payload)
    return {
        "input": {"exchange": str(exchange).strip().upper(), "symbol": str(symbol).strip().upper(),
                  "isin": (str(isin).strip().upper() if isin else None)},
        "normalization": {"key": master.key, "confidence": master.confidence.value, "reason": master.reason},
        "min_evidence": MIN_EVIDENCE.value,
        "results": results,
    }


def _row(payload: Dict[str, Any], created_at: Optional[datetime]) -> Dict[str, Any]:
    return {
        "created_at": created_at.isoformat() if created_at else None,
        "broker_id": payload.get("broker_id"), "exchange": payload.get("exchange"), "symbol": payload.get("symbol"),
        "master_key": payload.get("master_key"), "confidence": payload.get("confidence"),
        "verdict": payload.get("verdict"), "reason": payload.get("reason"),
        "group": group_of(str(payload.get("verdict")), str(payload.get("reason"))),
        "candidate_count": payload.get("candidate_count"), "catalog_loaded": payload.get("catalog_loaded"),
        "catalog_loaded_at": payload.get("catalog_loaded_at"), "diffs": payload.get("diffs") or [],
        "unverified_attributes": payload.get("unverified_attributes") or [],
    }


def build_limits(
    *, hours: int, limit: int, offset: int, requested: Optional[Dict[str, int]], events_read: int, truncated: bool,
    crosswalk_read: int, crosswalk_truncated: bool, crosswalk_section: Dict[str, Any], returned: int, total: int,
) -> Tuple[Dict[str, Any], bool, List[str]]:
    """Every cap and clamp that can make a report cover less than it appears to, stated explicitly. `complete` is False when the
    aggregates may not describe the whole window (an event cap was reached, or the window asked for was shortened). Paging is
    separate: `has_more` only says more rows exist beyond this page; the aggregates already cover them."""
    req = requested or {"hours": hours, "limit": limit, "offset": offset}
    window_clamped = req["hours"] != hours
    reasons: List[str] = []
    if truncated:
        reasons.append("COMPARISON_EVENT_CAP_REACHED")
    if crosswalk_truncated:
        reasons.append("CROSSWALK_EVENT_CAP_REACHED")
    if window_clamped:
        reasons.append("WINDOW_CLAMPED")
    limits = {
        "comparison_events": {"read": events_read, "cap": REPORT_EVENT_CAP, "truncated": truncated},
        "crosswalk_events": {"read": crosswalk_read, "cap": REPORT_EVENT_CAP, "truncated": crosswalk_truncated},
        "crosswalk_rows": {"cap": crosswalk_evidence.REPORT_ROW_CAP, "total": crosswalk_section.get("instrument_count", 0),
                           "truncated": bool(crosswalk_section.get("rows_truncated"))},
        "window": {"requested_hours": req["hours"], "applied_hours": hours, "max_hours": MAX_HOURS, "clamped": window_clamped},
        "page": {
            "limit_requested": req["limit"], "limit_applied": limit, "max_limit": MAX_LIMIT, "limit_clamped": req["limit"] != limit,
            "offset_requested": req["offset"], "offset_applied": offset, "max_offset": MAX_OFFSET,
            "offset_clamped": req["offset"] != offset,
            "returned": returned, "total": total, "has_more": offset + returned < total,
        },
    }
    return limits, not reasons, reasons


def build_report(
    events: Sequence[Tuple[Optional[datetime], Dict[str, Any]]],
    *,
    hours: int,
    limit: int,
    offset: int,
    truncated: bool,
    now: datetime,
    catalogs: Optional[Sequence[InstrumentCatalog]] = None,
    crosswalk_events: Sequence[Tuple[Optional[datetime], Dict[str, Any]]] = (),
    crosswalk_truncated: bool = False,
    requested: Optional[Dict[str, int]] = None,
    liveness: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """`events` are (created_at, metadata) newest first. Aggregates describe the CURRENT state: the latest event per
    (broker, exchange, symbol). Rows list the events themselves, paged. `crosswalk_events` (R4i) feed a separate
    `crosswalk` section; they never change a group, verdict or count above."""
    rows = [_row(p, created) for created, p in events if isinstance(p, dict)]
    latest: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for row in rows:
        latest.setdefault((row["broker_id"], row["exchange"], row["symbol"]), row)

    by_group, by_verdict, by_reason = Counter(), Counter(), Counter()
    per_broker: Dict[str, Dict[str, Any]] = {}
    for row in latest.values():
        by_group[row["group"]] += 1
        by_verdict[row["verdict"]] += 1
        by_reason[f"{row['verdict']}:{row['reason']}"] += 1
        b = per_broker.setdefault(row["broker_id"], {"instruments": 0, "groups": Counter(), "with_candidates": 0})
        b["instruments"] += 1
        b["groups"][row["group"]] += 1
        if (row.get("candidate_count") or 0) > 0:
            b["with_candidates"] += 1
    states = catalog_states(catalogs)
    for state in states:                                   # every broker is listed, compared or not
        per_broker.setdefault(state["broker_id"], {"instruments": 0, "groups": Counter(), "with_candidates": 0})
    crosswalk_section = crosswalk_evidence.build_section(crosswalk_events, now, events_truncated=crosswalk_truncated)
    limits, complete, reasons = build_limits(
        hours=hours, limit=limit, offset=offset, requested=requested, events_read=len(rows), truncated=truncated,
        crosswalk_read=len(crosswalk_events), crosswalk_truncated=crosswalk_truncated, crosswalk_section=crosswalk_section,
        returned=len(rows[offset:offset + limit]), total=len(rows),
    )
    return {
        "enabled": bool(settings.INSTRUMENT_SHADOW_ENABLED),
        "generated_at": now.isoformat(),
        "window_hours": hours,
        "min_evidence": MIN_EVIDENCE.value,
        "event_count": len(rows),
        "instrument_count": len({(r["exchange"], r["symbol"]) for r in latest.values()}),
        "truncated": truncated,
        "groups": dict(sorted(by_group.items())),
        "by_verdict": dict(sorted(by_verdict.items())),
        "by_reason": dict(sorted(by_reason.items())),
        "by_broker": {k: {**v, "groups": dict(sorted(v["groups"].items()))} for k, v in sorted(per_broker.items())},
        "catalogs": states,
        "notes": [MASTER_NOTE] + [s["note"] for s in states],
        "crosswalk": crosswalk_section,
        "limits": limits,
        "complete": complete,
        "incomplete_reasons": reasons,
        "liveness": liveness,
        "pagination": {"limit": limit, "offset": offset, "returned": len(rows[offset:offset + limit]), "total": len(rows)},
        "rows": rows[offset:offset + limit],
    }
