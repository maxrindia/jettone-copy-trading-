"""
Instrument terms (lot size, freeze quantity, contract identity) and a pure quantity validator - OBSERVE foundation.

What this is
------------
A broker-neutral record of the TRADING TERMS the engine can establish for one instrument, and a pure function that asks "is this
quantity consistent with those terms?". It makes no network call, reads no database and imports no broker client. In this phase it is
used only to OBSERVE (log and count) at the pipeline's final-quantity points; it changes no allow / skip / send decision. Enforcement
is a separate, later decision.

Honesty rules
-------------
* Identity is `(broker, exchange, token)`: the broker's own token on one exchange. Never a symbol, never a substring.
* Every fact has an explicit state: KNOWN, UNKNOWN (the source does not say) or INVALID (the source says something unusable).
  UNKNOWN is never treated as 1, as zero, or as unlimited. In particular an unknown freeze quantity is UNKNOWN, not "no limit".
* The validator returns ALLOW only when every dimension is positively established: fresh, valid, known lot and known freeze, and the
  quantity satisfies both. With a freeze quantity unknown (the case for every source available today) the overall answer is UNKNOWN.
* Missing, stale, malformed and conflicting metadata are distinct codes. Nothing is guessed, defaulted or inferred from a symbol.

Sources (verified here)
-----------------------
* Alice Blue contract master CSV (NSE, BSE, NFO): columns Exch, Exchange Segment, Symbol, Token, Instrument Type, Trading Symbol,
  Lot Size, Tick Size, and for NFO Option Type, Strike Price, Expiry Date. Verified against the committed local snapshots of the
  files (public data). It has NO freeze column, so freeze is UNKNOWN for every Alice row.
* Angel One scrip master: only token, symbol, name and exch_seg are used by this repository. Other field names (lotsize, expiry,
  strike, instrumenttype, tick_size) appear in community posts but there is no verified local fixture or official reference, so they
  are NOT mapped: lot, freeze, expiry and strike stay UNKNOWN for Angel One.
"""
import csv
import io
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Tuple

ALICE = "alicebluenew"
ANGEL = "angelone"
MAX_AGE = timedelta(hours=24)            # terms older than this are STALE (the Alice master is loaded once, at engine start)
MAX_LOT = 10_000_000                     # a sanity bound: a larger lot is treated as malformed, not trusted
_TOKEN_RE = re.compile(r"^[0-9]{1,12}$")
_INT_RE = re.compile(r"^[0-9]+$")


class Knowledge(str, Enum):
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"          # the source does not state it
    INVALID = "INVALID"          # the source states something unusable (malformed, non-positive, out of range)


class TermsStatus(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"          # at least one stated field is malformed
    CONFLICT = "CONFLICT"        # the source holds differing rows for the same identity


@dataclass(frozen=True)
class InstrumentTerms:
    broker: str
    exchange: str
    token: str
    lot_size: Optional[int] = None
    lot_state: Knowledge = Knowledge.UNKNOWN
    freeze_quantity: Optional[int] = None
    freeze_state: Knowledge = Knowledge.UNKNOWN
    instrument_type: Optional[str] = None
    option_type: Optional[str] = None
    strike: Optional[Decimal] = None
    expiry: Optional[date] = None
    trading_symbol: Optional[str] = None
    source: str = ""
    source_date: Optional[date] = None
    loaded_at: Optional[datetime] = None
    status: TermsStatus = TermsStatus.VALID
    issues: Tuple[str, ...] = ()

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.broker, self.exchange, self.token)

    def age(self, now: datetime) -> Optional[timedelta]:
        return None if self.loaded_at is None else now - self.loaded_at

    def freshness(self, now: datetime, max_age: timedelta = MAX_AGE) -> str:
        """FRESH, STALE or UNDATED. A load time in the future is not trustworthy and counts as STALE."""
        if self.loaded_at is None:
            return "UNDATED"
        age = now - self.loaded_at
        return "FRESH" if timedelta(minutes=-5) <= age <= max_age else "STALE"


# ── Alice Blue contract master parsing ────────────────────────────────────────────────────────────

@dataclass
class ParseReport:
    exchange: str
    rows: int = 0
    stored: int = 0
    rejected: Counter = field(default_factory=Counter)      # reason -> rows not stored
    invalid: int = 0
    conflicts: int = 0
    duplicates_collapsed: int = 0
    missing_columns: Tuple[str, ...] = ()


_ALICE_REQUIRED = ("Token", "Lot Size")


def _parse_lot(raw: Optional[str]) -> Tuple[Optional[int], Knowledge, Optional[str]]:
    text = (raw or "").strip()
    if not text:
        return None, Knowledge.UNKNOWN, None
    if not _INT_RE.match(text):
        return None, Knowledge.INVALID, "LOT_MALFORMED"
    value = int(text)
    if value <= 0:
        return None, Knowledge.INVALID, "LOT_NON_POSITIVE"
    if value > MAX_LOT:
        return None, Knowledge.INVALID, "LOT_OUT_OF_RANGE"
    return value, Knowledge.KNOWN, None


def _parse_date(raw: Optional[str]) -> Tuple[Optional[date], Optional[str]]:
    text = (raw or "").strip()
    if not text:
        return None, None
    try:
        return date.fromisoformat(text), None
    except ValueError:
        return None, "EXPIRY_MALFORMED"


def _parse_strike(raw: Optional[str]) -> Tuple[Optional[Decimal], Optional[str]]:
    text = (raw or "").strip()
    if not text:
        return None, None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None, "STRIKE_MALFORMED"
    return (value, None) if value.is_finite() and value >= 0 else (None, "STRIKE_MALFORMED")


def parse_alice_contract_master(exchange: str, text: str, *, loaded_at: datetime, source_date: Optional[date] = None
                                ) -> Tuple[Dict[Tuple[str, str, str], InstrumentTerms], ParseReport]:
    """Alice Blue contract-master CSV text -> terms keyed by (broker, exchange, token). Pure. A file missing the Token or Lot Size
    column yields NO terms (fail closed). Rows with a malformed token are dropped and counted. A stated-but-unusable lot (malformed,
    zero, negative, absurd) is kept as INVALID so the validator can say so; a blank lot is UNKNOWN. Differing rows for one token are
    a CONFLICT and carry no lot."""
    exch = str(exchange or "").strip().upper()
    report = ParseReport(exchange=exch)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    header = reader.fieldnames or []
    missing = tuple(c for c in _ALICE_REQUIRED if c not in header)
    if missing:
        report.missing_columns = missing
        return {}, report

    by_key: Dict[Tuple[str, str, str], List[InstrumentTerms]] = {}
    for row in reader:
        report.rows += 1
        token = (row.get("Token") or "").strip()
        if not _TOKEN_RE.match(token):
            report.rejected["TOKEN_MALFORMED"] += 1
            continue
        row_exch = (row.get("Exch") or "").strip().upper()
        lot, lot_state, lot_issue = _parse_lot(row.get("Lot Size"))
        expiry, expiry_issue = _parse_date(row.get("Expiry Date"))
        strike, strike_issue = _parse_strike(row.get("Strike Price"))
        option_type = (row.get("Option Type") or "").strip().upper()
        if option_type == "XX":
            # Verified in the NFO snapshot: futures carry Option Type XX and Strike -1 meaning "not applicable". Accept the
            # sentinel only in that pairing; a -1 strike on a CE/PE row stays malformed.
            option_type = ""
            if (row.get("Strike Price") or "").strip() in ("-1", "-1.0", "-1.00"):
                strike, strike_issue = None, None
        issues = [i for i in (lot_issue, expiry_issue, strike_issue) if i]
        if row_exch and row_exch != exch:
            issues.append("EXCHANGE_MISMATCH")
        terms = InstrumentTerms(
            broker=ALICE, exchange=exch, token=token, lot_size=lot, lot_state=lot_state,
            freeze_quantity=None, freeze_state=Knowledge.UNKNOWN,               # the Alice master has no freeze column
            instrument_type=(row.get("Instrument Type") or "").strip() or None,
            option_type=option_type or None,
            strike=strike, expiry=expiry, trading_symbol=(row.get("Trading Symbol") or "").strip() or None,
            source="alice_contract_master", source_date=source_date, loaded_at=loaded_at,
            status=TermsStatus.INVALID if issues else TermsStatus.VALID, issues=tuple(issues),
        )
        by_key.setdefault(terms.key, []).append(terms)

    out: Dict[Tuple[str, str, str], InstrumentTerms] = {}
    for key, rows in by_key.items():
        first = rows[0]
        if len(rows) > 1:
            if all(r == first for r in rows):
                report.duplicates_collapsed += len(rows) - 1
            else:
                report.conflicts += 1
                out[key] = InstrumentTerms(
                    broker=ALICE, exchange=exch, token=key[2], source="alice_contract_master", source_date=source_date,
                    loaded_at=loaded_at, status=TermsStatus.CONFLICT, issues=("DUPLICATE_TOKEN_DIFFERING_ROWS",),
                )
                continue
        if first.status is TermsStatus.INVALID:
            report.invalid += 1
        out[key] = first
    report.stored = len(out)
    return out, report


def angel_identity_only(exchange: str, token: str, *, loaded_at: datetime, source_date: Optional[date] = None) -> InstrumentTerms:
    """What is verified for an Angel One scrip-master record: its identity. Lot, freeze, expiry and strike are UNKNOWN because their
    field names are not verified from a local fixture or official documentation (see the module docstring); nothing is mapped."""
    return InstrumentTerms(
        broker=ANGEL, exchange=str(exchange).strip().upper(), token=str(token).strip(), source="angel_scrip_master_identity_only",
        source_date=source_date, loaded_at=loaded_at,
    )


# ── the store ─────────────────────────────────────────────────────────────────────────────────────

class TermsStore:
    """In-memory terms by (broker, exchange, token). An exchange's terms are replaced as a whole, atomically: a failed or empty
    parse never wipes the previous load."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_exchange: Dict[Tuple[str, str], Dict[Tuple[str, str, str], InstrumentTerms]] = {}
        self.reports: Dict[Tuple[str, str], ParseReport] = {}

    def replace(self, broker: str, exchange: str, terms: Dict[Tuple[str, str, str], InstrumentTerms], report: ParseReport) -> bool:
        key = (broker, str(exchange).strip().upper())
        self.reports[key] = report
        if not terms:
            return False                       # keep whatever was loaded before
        with self._lock:
            self._by_exchange[key] = dict(terms)
        return True

    def load_alice(self, exchange: str, text: str, *, loaded_at: Optional[datetime] = None, source_date: Optional[date] = None) -> ParseReport:
        terms, report = parse_alice_contract_master(exchange, text, loaded_at=loaded_at or datetime.now(timezone.utc),
                                                    source_date=source_date)
        self.replace(ALICE, exchange, terms, report)
        return report

    def get(self, broker: str, exchange: str, token: str) -> Optional[InstrumentTerms]:
        bucket = self._by_exchange.get((str(broker), str(exchange).strip().upper()))
        return None if bucket is None else bucket.get((str(broker), str(exchange).strip().upper(), str(token).strip()))

    def size(self) -> int:
        return sum(len(b) for b in self._by_exchange.values())

    def clear(self) -> None:
        with self._lock:
            self._by_exchange.clear()
            self.reports.clear()


store = TermsStore()


# ── the pure validator ────────────────────────────────────────────────────────────────────────────

class Outcome(str, Enum):
    ALLOW = "ALLOW"
    REJECT = "REJECT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Dimension:
    outcome: Outcome
    code: str


@dataclass(frozen=True)
class QuantityVerdict:
    outcome: Outcome
    lot: Dimension
    freeze: Dimension
    codes: Tuple[str, ...]
    freshness: str
    detail: str = ""

    @property
    def verified_safe(self) -> bool:
        """True only for ALLOW: every dimension positively established. UNKNOWN is never 'safe'."""
        return self.outcome is Outcome.ALLOW


# reason codes
QUANTITY_INVALID = "QUANTITY_NOT_A_POSITIVE_INTEGER"
TERMS_MISSING = "TERMS_MISSING"
TERMS_CONFLICT = "TERMS_CONFLICT"
TERMS_INVALID = "TERMS_INVALID"
TERMS_STALE = "TERMS_STALE"
TERMS_UNDATED = "TERMS_UNDATED"
LOT_UNKNOWN = "LOT_UNKNOWN"
LOT_INVALID = "LOT_INVALID"
LOT_OK = "LOT_OK"
NOT_LOT_MULTIPLE = "QUANTITY_NOT_LOT_MULTIPLE"
FREEZE_UNKNOWN = "FREEZE_UNKNOWN"
FREEZE_INVALID = "FREEZE_INVALID"
FREEZE_OK = "FREEZE_OK"
EXCEEDS_FREEZE = "QUANTITY_EXCEEDS_FREEZE"
BROKER_UNKNOWN = "BROKER_UNKNOWN"
CROSS_BROKER_TOKEN = "CROSS_BROKER_TOKEN_NOT_NATIVE"
TOKEN_MISSING = "TOKEN_MISSING"


# (decision point or "ALL", outcome / reason code) -> times seen. Process-local, observation only.
observation_counts: Counter = Counter()


def _verdict(outcome: Outcome, lot: Dimension, freeze: Dimension, codes: Iterable[str], freshness: str, detail: str = "") -> QuantityVerdict:
    return QuantityVerdict(outcome, lot, freeze, tuple(codes), freshness, detail)


def _both(outcome: Outcome, code: str, freshness: str, detail: str = "") -> QuantityVerdict:
    d = Dimension(outcome, code)
    return _verdict(outcome, d, d, (code,), freshness, detail)


def validate_quantity(terms: Optional[InstrumentTerms], quantity: Any, *, now: datetime, max_age: timedelta = MAX_AGE) -> QuantityVerdict:
    """Is `quantity` consistent with `terms`? Pure; never raises. See the module docstring for the rules."""
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
        return _both(Outcome.REJECT, QUANTITY_INVALID, "N/A", f"quantity={quantity!r}")
    if terms is None:
        return _both(Outcome.UNKNOWN, TERMS_MISSING, "N/A")
    freshness = terms.freshness(now, max_age)
    if terms.status is TermsStatus.CONFLICT:
        return _both(Outcome.REJECT, TERMS_CONFLICT, freshness, ",".join(terms.issues))
    if terms.status is TermsStatus.INVALID:
        return _both(Outcome.REJECT, TERMS_INVALID, freshness, ",".join(terms.issues))
    if freshness != "FRESH":
        return _both(Outcome.UNKNOWN, TERMS_STALE if freshness == "STALE" else TERMS_UNDATED, freshness)

    # lot
    if terms.lot_state is Knowledge.KNOWN and terms.lot_size:
        lot = Dimension(Outcome.ALLOW, LOT_OK) if quantity % terms.lot_size == 0 else Dimension(Outcome.REJECT, NOT_LOT_MULTIPLE)
    elif terms.lot_state is Knowledge.INVALID:
        lot = Dimension(Outcome.REJECT, LOT_INVALID)
    else:
        lot = Dimension(Outcome.UNKNOWN, LOT_UNKNOWN)

    # freeze: unknown is UNKNOWN - never unlimited
    if terms.freeze_state is Knowledge.KNOWN and terms.freeze_quantity:
        freeze = Dimension(Outcome.ALLOW, FREEZE_OK) if quantity <= terms.freeze_quantity else Dimension(Outcome.REJECT, EXCEEDS_FREEZE)
    elif terms.freeze_state is Knowledge.INVALID:
        freeze = Dimension(Outcome.REJECT, FREEZE_INVALID)
    else:
        freeze = Dimension(Outcome.UNKNOWN, FREEZE_UNKNOWN)

    dims = (lot, freeze)
    if any(d.outcome is Outcome.REJECT for d in dims):
        overall = Outcome.REJECT
    elif any(d.outcome is Outcome.UNKNOWN for d in dims):
        overall = Outcome.UNKNOWN
    else:
        overall = Outcome.ALLOW
    return _verdict(overall, lot, freeze, (d.code for d in dims), freshness)


def observe_quantity(*, child_broker: Optional[str], master_broker: Optional[str], exchange: Any, token: Any, quantity: Any,
                     now: datetime, terms_store: Optional[TermsStore] = None, max_age: timedelta = MAX_AGE) -> QuantityVerdict:
    """The lookup + validation used by the pipeline's observation points. The Master's token is only meaningful on the SAME broker, so
    a cross-broker Child (whose own token is resolved inside its client) is UNKNOWN, never matched by symbol."""
    if not child_broker or not master_broker:
        return _both(Outcome.UNKNOWN, BROKER_UNKNOWN, "N/A")
    if child_broker != master_broker:
        return _both(Outcome.UNKNOWN, CROSS_BROKER_TOKEN, "N/A")
    tok = str(token).strip() if token not in (None, "") else ""
    if not tok:
        return _both(Outcome.UNKNOWN, TOKEN_MISSING, "N/A")
    found = (terms_store or store).get(child_broker, str(exchange or ""), tok)
    return validate_quantity(found, quantity, now=now, max_age=max_age)
