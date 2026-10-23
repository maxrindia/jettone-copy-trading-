"""
Per-order planner checks (R4f, rules 4 and 5 of docs/architecture/multi-broker-copy-trading/02 section 2.3).

For one Master order and one Child, decide whether that Child's broker can take the order at all:

  1. SEGMENT_NOT_SUPPORTED   the exchange maps to a segment the Child's broker lists in its capability profile
  2. INSTRUMENT_UNMAPPED     the instrument has a trustworthy identity for the Child's broker (below)
  3. PRODUCT_NOT_SUPPORTED   the product is a known one that the listed segment carries
  4. ORDER_TYPE_NOT_SUPPORTED  the order type the Child will actually receive is a known one the segment carries

The first failure wins, so one Child gets one reason; a skip applies to THAT Child only (decision D1). Nothing here
calls a broker, Redis or the database, and nothing is substituted (an SL-M is never turned into a MARKET).

Instrument identity
-------------------
* Same broker (the Child's broker is the Master's broker, e.g. Alice -> Alice): the Master's token is native to the Child's
  broker. The instrument is mapped when the order carries that token, or when it is a plain NSE/BSE equity symbol (resolved
  by symbol exactly as before). A derivative without a token is ambiguous ("NIFTY" is every strike and expiry) and fails.
  No catalog is consulted: it is the same instrument master, and the Alice catalog adapter is deliberately not loaded.
* Different broker (e.g. Alice -> Groww): a Master token means nothing there, so an ISIN-level identity is required. None
  exists yet (persisted Master orders carry only the token and exchange; no token-to-ISIN resolver exists), so with no
  `master_isin` the instrument is unmapped (fail closed). When an ISIN is supplied, the R4b catalog and the R4a comparator
  decide: only MATCH / ATTRIBUTE_DIFF map; unmapped, ambiguous, not-loaded and stale all fail.

Evidence level
--------------
Capability is checked at `Verification.UNVERIFIED`: "does this broker list the segment, product and order type at all".
How well a broker's capability is evidenced is the job of the registry's role gates and the future pair gate (R5), not of
this per-order check; using a stricter level here would skip Alice NFO orders that Alice -> Alice copies today.

The caller applies this only when CANONICAL_MAPPING_ENFORCED is on (default off).
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.services.brokers.capabilities import SegmentCaps, Verification, normalize_order_type
from app.services.brokers.profiles import build_profile
from app.services.brokers.registry import get_broker_spec, normalize_broker_id
from app.services.copy_engine.risk_inputs import UnknownProductTypeError, normalize_product_for_exchange
from app.services.instruments.catalogs import InstrumentCatalog, default_catalogs
from app.services.instruments.comparator import compare_instrument
from app.services.instruments.model import Verdict
from app.services.instruments.normalizer import normalize_equity

INSTRUMENT_UNMAPPED = "INSTRUMENT_UNMAPPED"
SEGMENT_NOT_SUPPORTED = "SEGMENT_NOT_SUPPORTED"
PRODUCT_NOT_SUPPORTED = "PRODUCT_NOT_SUPPORTED"
ORDER_TYPE_NOT_SUPPORTED = "ORDER_TYPE_NOT_SUPPORTED"

PLANNER_MIN_EVIDENCE = Verification.UNVERIFIED

# The capability-profile segments an exchange can carry. A derivative exchange lists both futures and options; the order
# does not say which, so either listed segment satisfies the check.
EXCHANGE_SEGMENTS: Dict[str, Tuple[str, ...]] = {
    "NSE": ("CASH",), "BSE": ("CASH",),
    "NFO": ("OPT", "FUT"), "BFO": ("OPT", "FUT"),
    "MCX": ("COM",), "CDS": ("CUR",), "BCD": ("CUR",),
}


@dataclass(frozen=True)
class PlannerSkip:
    code: str
    detail: str


def _skip(code: str, detail: str) -> PlannerSkip:
    return PlannerSkip(code, detail[:200])


def check_child_order(
    *,
    master_broker: Optional[str],
    child_broker: Optional[str],
    exchange: Any,
    symbol: Any,
    product_type: Any,
    order_type: Any,
    instrument_token: Optional[str] = None,
    master_isin: Optional[str] = None,
    catalogs: Optional[Sequence[InstrumentCatalog]] = None,
    now: Optional[datetime] = None,
) -> Optional[PlannerSkip]:
    """None when the Child's broker can take the order, else the first failing PlannerSkip. `order_type` is the type the
    Child will RECEIVE (a Master MARKET becomes a LIMIT in MARKETABLE_LIMIT mode)."""
    spec = get_broker_spec(child_broker)
    profile = build_profile(spec.id) if spec is not None else None
    if spec is None or profile is None:
        return _skip(SEGMENT_NOT_SUPPORTED, f"UNKNOWN_CHILD_BROKER:{child_broker}")

    # 1. segment
    ex = str(exchange or "").strip().upper()
    if not ex:
        return _skip(SEGMENT_NOT_SUPPORTED, "MISSING_EXCHANGE")
    segments = EXCHANGE_SEGMENTS.get(ex)
    if segments is None:
        return _skip(SEGMENT_NOT_SUPPORTED, f"UNKNOWN_EXCHANGE:{ex}")
    listed: List[SegmentCaps] = []
    for seg in segments:
        caps = profile.segment(ex, seg)
        if caps is not None and caps.verification is not Verification.NOT_SUPPORTED \
                and caps.verification.at_least(PLANNER_MIN_EVIDENCE):
            listed.append(caps)
    if not listed:
        return _skip(SEGMENT_NOT_SUPPORTED, f"{ex}_NOT_LISTED_FOR_{spec.id}")

    # 2. instrument identity
    unmapped = _instrument_problem(
        spec_id=spec.id, profile=profile, master_broker=master_broker, ex=ex, segments=segments, symbol=symbol,
        instrument_token=instrument_token, master_isin=master_isin, catalogs=catalogs, now=now,
    )
    if unmapped is not None:
        return _skip(INSTRUMENT_UNMAPPED, unmapped)

    # 3. product
    try:
        product = normalize_product_for_exchange(product_type, ex)
    except UnknownProductTypeError:
        return _skip(PRODUCT_NOT_SUPPORTED, f"UNKNOWN_PRODUCT:{product_type}")
    carrying = [c for c in listed if product in c.products]
    if not carrying:
        return _skip(PRODUCT_NOT_SUPPORTED, f"{product}_NOT_LISTED_FOR_{spec.id}_{ex}")

    # 4. order type
    otype = normalize_order_type(order_type)
    if otype is None:
        return _skip(ORDER_TYPE_NOT_SUPPORTED, f"UNKNOWN_ORDER_TYPE:{order_type}")
    if not any(otype in c.order_types for c in carrying):
        return _skip(ORDER_TYPE_NOT_SUPPORTED, f"{otype}_NOT_LISTED_FOR_{spec.id}_{ex}_{product}")
    return None


def _instrument_problem(
    *, spec_id: str, profile: Any, master_broker: Optional[str], ex: str, segments: Tuple[str, ...], symbol: Any,
    instrument_token: Optional[str], master_isin: Optional[str], catalogs: Optional[Sequence[InstrumentCatalog]],
    now: Optional[datetime],
) -> Optional[str]:
    """None when the instrument is mapped for this Child's broker, else a short reason."""
    master_id = normalize_broker_id(master_broker) if master_broker else None

    if master_id is not None and master_id == spec_id:
        token = instrument_token.strip() if isinstance(instrument_token, str) else ""
        if token:
            return None
        if segments == ("CASH",):
            norm = normalize_equity(ex, symbol, None)
            return None if norm.instrument is not None else f"SYMBOL_NOT_RESOLVABLE:{norm.reason}"
        return "NO_INSTRUMENT_TOKEN_FOR_DERIVATIVE"

    if master_id is None:
        return "MASTER_BROKER_UNKNOWN"
    if not master_isin:
        return "MASTER_ISIN_UNAVAILABLE"
    norm = normalize_equity(ex, symbol, master_isin)
    if norm.instrument is None:
        return f"MASTER_IDENTITY_UNMAPPED:{norm.reason}"
    pool = list(catalogs) if catalogs is not None else list(default_catalogs())
    catalog = next((c for c in pool if c.broker_id == spec_id), None)
    if catalog is None:
        return f"NO_CATALOG_FOR_{spec_id}"
    view = catalog.view_for(norm.instrument)
    result = compare_instrument(norm, view, profile, now=now or datetime.now(timezone.utc), minimum=PLANNER_MIN_EVIDENCE)
    if result.verdict in (Verdict.MATCH, Verdict.ATTRIBUTE_DIFF):
        return None
    return f"{result.verdict.value}:{result.reason}"
