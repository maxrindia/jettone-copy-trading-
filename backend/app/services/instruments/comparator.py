"""
Pure cross-broker comparison for one equity instrument (R4a). Observe-only: it returns a verdict and never
influences an order. No I/O; `now` is passed in so results are deterministic.

Verdict order (the first that applies wins, every step fails closed):
  1. the Master instrument could not be normalised           -> UNMAPPED
  2. the broker's capability profile does not support it     -> SEGMENT_NOT_SUPPORTED   (existing SegmentCaps /
     supports_order API; no parallel capability model)
  3. the broker's catalog is not loaded                      -> CATALOG_NOT_LOADED
  4. the catalog is older than `max_age` or has no/invalid time -> MASTER_DATA_STALE
  5. candidate selection, ALWAYS inside the Master's exchange:
       Master has an ISIN   -> candidates with that ISIN. None -> UNMAPPED (ISIN_CONFLICT if the same symbol
                               carries a different ISIN); several -> AMBIGUOUS.
       Master is symbol-only -> candidates with that symbol. None -> UNMAPPED; several -> AMBIGUOUS; exactly one ->
                               UNMAPPED "SYMBOL_ONLY_NOT_DEFINITIVE" (a name is not an identity); no candidate
                               is reported as matched.
  6. a single ISIN match -> MATCH, or ATTRIBUTE_DIFF when lot size / tick size are both known and differ. An
     attribute that either side does not provide is listed in `unverified_attributes`, never assumed.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Mapping, Optional, Sequence, Tuple

from app.services.brokers.capabilities import CapabilityProfile, Verification
from app.services.instruments.model import (
    AttributeDiff,
    BrokerInstrument,
    BrokerView,
    Comparison,
    Confidence,
    Normalization,
    Verdict,
)
from app.services.instruments.normalizer import normalize_exchange, normalize_isin, normalize_symbol

DEFAULT_MAX_AGE = timedelta(hours=24)


def _result(view: BrokerView, master: Normalization, verdict: Verdict, reason: str, **extra) -> Comparison:
    return Comparison(
        broker_id=view.broker_id, verdict=verdict, reason=reason,
        confidence=master.confidence, master_key=master.key, **extra,
    )


def _supported(profile: CapabilityProfile, exchange: str, product: object, order_type: object,
               minimum: Verification) -> bool:
    caps = profile.segment(exchange, "CASH")
    if caps is None or caps.verification is Verification.NOT_SUPPORTED or not caps.verification.at_least(minimum):
        return False
    if product is None:
        return True
    return profile.supports_order(exchange, "CASH", product, order_type, minimum=minimum)


def _is_fresh(loaded_at: Optional[datetime], now: datetime, max_age: timedelta) -> Tuple[bool, str]:
    if loaded_at is None:
        return False, "LOAD_TIME_UNKNOWN"
    if loaded_at.tzinfo is None or now.tzinfo is None:
        return False, "NAIVE_TIMESTAMP"
    if loaded_at > now:
        return False, "LOAD_TIME_IN_FUTURE"
    if now - loaded_at > max_age:
        return False, "CATALOG_OLDER_THAN_LIMIT"
    return True, ""


def _diffs(master: Optional[BrokerInstrument], broker: BrokerInstrument) -> Tuple[List[AttributeDiff], List[str]]:
    diffs: List[AttributeDiff] = []
    unverified: List[str] = []
    for attr, valid in (("lot_size", lambda v: isinstance(v, int) and not isinstance(v, bool) and v > 0),
                        ("tick_size", lambda v: isinstance(v, Decimal) and v > 0)):
        m = getattr(master, attr, None) if master is not None else None
        b = getattr(broker, attr, None)
        if not valid(m) or not valid(b):
            unverified.append(attr)
        elif m != b:
            diffs.append(AttributeDiff(attr, m, b))
    return diffs, unverified


def compare_instrument(
    master: Normalization,
    view: BrokerView,
    profile: Optional[CapabilityProfile],
    *,
    now: datetime,
    master_details: Optional[BrokerInstrument] = None,
    product: object = None,
    order_type: object = None,
    minimum: Verification = Verification.VERIFIED_LIVE,
    max_age: timedelta = DEFAULT_MAX_AGE,
) -> Comparison:
    if (product is None) != (order_type is None):
        raise ValueError("product and order_type must be given together or not at all")
    inst = master.instrument
    if inst is None or master.confidence is Confidence.UNMAPPED:
        return _result(view, master, Verdict.UNMAPPED, master.reason or "MASTER_UNMAPPED")

    if profile is None:
        return _result(view, master, Verdict.SEGMENT_NOT_SUPPORTED, "NO_CAPABILITY_PROFILE")
    if not _supported(profile, inst.exchange, product, order_type, minimum):
        return _result(view, master, Verdict.SEGMENT_NOT_SUPPORTED, f"{inst.exchange}_CASH_NOT_SUPPORTED_AT_{minimum.value}")

    if view.candidates is None:
        return _result(view, master, Verdict.CATALOG_NOT_LOADED, "CATALOG_NOT_LOADED")
    fresh, why = _is_fresh(view.loaded_at, now, max_age)
    if not fresh:
        return _result(view, master, Verdict.MASTER_DATA_STALE, why)

    # Candidates of the same exchange only; an NSE listing never stands in for a BSE one.
    rows: List[Tuple[BrokerInstrument, Optional[str], Optional[str], bool]] = []   # (row, symbol, isin, isin_ok)
    for cand in view.candidates:
        if normalize_exchange(cand.exchange) != inst.exchange:
            continue
        c_sym, _ = normalize_symbol(cand.trading_symbol)
        c_isin, isin_reason = normalize_isin(cand.isin)
        rows.append((cand, c_sym, c_isin, not isin_reason))

    same_symbol = [r for r in rows if r[1] == inst.symbol]
    if any(not r[3] for r in same_symbol):
        return _result(view, master, Verdict.UNMAPPED, "CANDIDATE_ISIN_MALFORMED")

    if inst.isin:
        hits = [r for r in rows if r[2] == inst.isin]
        if not hits:
            conflict = any(r[2] and r[2] != inst.isin for r in same_symbol)
            return _result(view, master, Verdict.UNMAPPED, "ISIN_CONFLICT" if conflict else "NOT_FOUND")
        if len(hits) > 1:
            return _result(view, master, Verdict.AMBIGUOUS, "MULTIPLE_CANDIDATES_FOR_ISIN")
        if any(r[2] and r[2] != inst.isin for r in same_symbol):
            return _result(view, master, Verdict.AMBIGUOUS, "SYMBOL_SHARED_WITH_OTHER_ISIN")
        row = hits[0][0]
        diffs, unverified = _diffs(master_details, row)
        verdict = Verdict.ATTRIBUTE_DIFF if diffs else Verdict.MATCH
        return _result(view, master, verdict, "ATTRIBUTE_DIFF" if diffs else "ISIN_MATCH", matched=row,
                       diffs=tuple(diffs), unverified_attributes=tuple(sorted(unverified)))

    if not same_symbol:
        return _result(view, master, Verdict.UNMAPPED, "NOT_FOUND")
    if len(same_symbol) > 1:
        return _result(view, master, Verdict.AMBIGUOUS, "MULTIPLE_CANDIDATES_FOR_SYMBOL")
    return _result(view, master, Verdict.UNMAPPED, "SYMBOL_ONLY_NOT_DEFINITIVE", matched=None)


def compare_all(
    master: Normalization,
    views: Sequence[BrokerView],
    profiles: Mapping[str, Optional[CapabilityProfile]],
    **kwargs,
) -> Tuple[Comparison, ...]:
    """One comparison per broker, ordered by broker id (deterministic)."""
    return tuple(
        compare_instrument(master, view, profiles.get(view.broker_id), **kwargs)
        for view in sorted(views, key=lambda v: v.broker_id)
    )
