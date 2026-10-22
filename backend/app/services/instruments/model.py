"""
Immutable types for R4a. Equity/CASH only.

Identity rules
--------------
* The exchange is part of the identity. NSE and BSE listings of one company are different tradable instruments and
  are never equated automatically, even when they share an ISIN.
* An ISIN (syntactically valid, check digit correct) is the only strong identity. A symbol alone is a name, not an
  identity: it yields SYMBOL_ONLY confidence and can never produce a definitive MATCH.
* Keys live in disjoint namespaces, so a symbol-only key can never compare equal to an ISIN key.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional, Tuple


class Segment(str, Enum):
    CASH = "CASH"


class Confidence(str, Enum):
    ISIN = "ISIN"                # exchange + a valid ISIN
    SYMBOL_ONLY = "SYMBOL_ONLY"  # exchange + symbol, no usable ISIN: low confidence, never definitive
    UNMAPPED = "UNMAPPED"        # no identity could be established


class Verdict(str, Enum):
    MATCH = "MATCH"
    ATTRIBUTE_DIFF = "ATTRIBUTE_DIFF"
    UNMAPPED = "UNMAPPED"
    AMBIGUOUS = "AMBIGUOUS"
    SEGMENT_NOT_SUPPORTED = "SEGMENT_NOT_SUPPORTED"
    MASTER_DATA_STALE = "MASTER_DATA_STALE"
    CATALOG_NOT_LOADED = "CATALOG_NOT_LOADED"


@dataclass(frozen=True)
class CanonicalInstrument:
    exchange: str                 # NSE | BSE
    symbol: str                   # normalised: upper-case, no "-EQ"
    isin: Optional[str]           # valid ISIN, or None
    segment: Segment = Segment.CASH

    @property
    def confidence(self) -> Confidence:
        return Confidence.ISIN if self.isin else Confidence.SYMBOL_ONLY

    @property
    def key(self) -> str:
        if self.isin:
            return f"EQ|{self.exchange}|ISIN|{self.isin}"
        return f"EQ|{self.exchange}|SYM|{self.symbol}"


@dataclass(frozen=True)
class Normalization:
    """Outcome of normalising raw metadata. `instrument` is None exactly when confidence is UNMAPPED."""
    instrument: Optional[CanonicalInstrument]
    confidence: Confidence
    reason: str = ""

    @property
    def key(self) -> Optional[str]:
        return self.instrument.key if self.instrument else None


@dataclass(frozen=True)
class BrokerInstrument:
    """One row of a broker's own instrument master, as supplied by a future catalog. `lot_size` and `tick_size`
    are None when the broker's data does not provide them; they are never defaulted."""
    broker_id: str
    exchange: str
    trading_symbol: str
    isin: Optional[str] = None
    token: Optional[str] = None
    lot_size: Optional[int] = None
    tick_size: Optional[Decimal] = None


@dataclass(frozen=True)
class BrokerView:
    """What the comparator is told about one broker. `candidates=None` means its catalog is not loaded (an empty
    tuple is a loaded catalog that has nothing)."""
    broker_id: str
    candidates: Optional[Tuple[BrokerInstrument, ...]] = None
    loaded_at: Optional[datetime] = None


@dataclass(frozen=True)
class AttributeDiff:
    attribute: str                # lot_size | tick_size
    master: object
    broker: object


@dataclass(frozen=True)
class Comparison:
    broker_id: str
    verdict: Verdict
    reason: str
    confidence: Confidence
    master_key: Optional[str]
    matched: Optional[BrokerInstrument] = None
    diffs: Tuple[AttributeDiff, ...] = ()
    unverified_attributes: Tuple[str, ...] = ()   # attributes that could not be compared (a side has no data)
