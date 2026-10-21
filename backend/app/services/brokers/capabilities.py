"""
Broker capability profiles (R1 of docs/architecture/multi-broker-copy-trading).

A profile records, per broker, how well each capability is EVIDENCED. It is descriptive only: nothing in the
engine reads it yet, and it never switches anything on. The enforcing gates stay where they are
(`brokers/registry.py` flags and contracts).

Two ideas are kept apart on purpose:

* `Verification`  - the level of evidence behind a capability (live, docs only, unverified, not supported).
* `gates`         - what the registry currently switches on (login / Master / Child), from the same flags the
                    engine already obeys. A capability can be evidenced but gated off, or gated on with only
                    documentation behind it (Groww Child, today).

Fail-closed defaults: an unlisted feature is NOT_SUPPORTED, an unlisted segment is unsupported, and
`supports()` defaults to requiring live evidence.

Vocabulary: `supports()` compares CANONICAL values (products MIS / CNC / NRML, order types MARKET / LIMIT / SL /
SLM). Real values differ (`LONGTERM`, `INTRADAY`, `OrderType.SL_M == "SL-M"`), so anything holding real values
must use `supports_order()`, which normalises first.
"""
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import FrozenSet, List, Mapping, Optional, Tuple


class Verification(str, Enum):
    VERIFIED_LIVE = "VERIFIED_LIVE"    # implemented, tested offline, and observed against a real account
    VERIFIED_DOCS = "VERIFIED_DOCS"    # implemented against the official documentation; never seen live
    UNVERIFIED = "UNVERIFIED"          # implemented or partly implemented; contract/behaviour not confirmed
    NOT_SUPPORTED = "NOT_SUPPORTED"    # not implemented in Jettone (says nothing about the broker's own API)

    @property
    def rank(self) -> int:
        return _RANK[self]

    @property
    def is_live(self) -> bool:
        return self is Verification.VERIFIED_LIVE

    def at_least(self, minimum: "Verification") -> bool:
        return self.rank >= minimum.rank

    @staticmethod
    def lowest(a: "Verification", b: "Verification") -> "Verification":
        return a if a.rank <= b.rank else b


_RANK = {
    Verification.NOT_SUPPORTED: 0,
    Verification.UNVERIFIED: 1,
    Verification.VERIFIED_DOCS: 2,
    Verification.VERIFIED_LIVE: 3,
}


class Feature(str, Enum):
    LOGIN = "LOGIN"
    MASTER_DETECTION = "MASTER_DETECTION"
    CHILD_PLACEMENT = "CHILD_PLACEMENT"
    MODIFY = "MODIFY"
    CANCEL = "CANCEL"
    # Reading the broker's ORDER BOOK (rows with status and fills). It is NOT a lookup of one specific order.
    ORDER_STATUS = "ORDER_STATUS"
    # Querying one specific order by broker id or client reference, exposed as a public client method. No
    # client exposes one today (Groww has a private helper only), so it is NOT_SUPPORTED for every broker.
    ORDER_LOOKUP = "ORDER_LOOKUP"
    POSITIONS = "POSITIONS"
    HOLDINGS = "HOLDINGS"
    FUNDS = "FUNDS"
    CLIENT_REFERENCE = "CLIENT_REFERENCE"  # a caller-chosen order tag the broker accepts on placement


class Role(str, Enum):
    MASTER = "MASTER"
    CHILD = "CHILD"


ROLE_FEATURE = {Role.MASTER: Feature.MASTER_DETECTION, Role.CHILD: Feature.CHILD_PLACEMENT}

# Everything a role needs to be VERIFIED_LIVE before it meets the design bar (README acceptance criteria 1-2).
ROLE_REQUIREMENTS = {
    Role.MASTER: (Feature.MASTER_DETECTION, Feature.ORDER_STATUS),
    Role.CHILD: (
        Feature.CHILD_PLACEMENT, Feature.MODIFY, Feature.CANCEL,
        Feature.ORDER_STATUS, Feature.POSITIONS, Feature.FUNDS,
    ),
}


def normalize_order_type(raw: object) -> Optional[str]:
    """Canonical order type (MARKET / LIMIT / SL / SLM) for any spelling in use, or None when unrecognised.
    `OrderType.SL_M` has the value "SL-M"; Master events and broker mappings say "SLM"."""
    key = str(getattr(raw, "value", raw) or "").upper().strip()
    return {
        "MARKET": "MARKET", "MKT": "MARKET",
        "LIMIT": "LIMIT", "L": "LIMIT",
        "SL": "SL",
        "SL-M": "SLM", "SLM": "SLM", "SL_M": "SLM",
    }.get(key)


@dataclass(frozen=True)
class SegmentCaps:
    exchange: str
    segment: str                    # CASH | FUT | OPT | COM | CUR
    products: FrozenSet[str]        # canonical Jettone products: MIS, CNC, NRML
    order_types: FrozenSet[str]     # canonical: MARKET, LIMIT, SL, SLM
    verification: Verification


@dataclass(frozen=True)
class CapabilityProfile:
    broker_id: str
    features: Mapping[Feature, Verification] = field(default_factory=dict)
    segments: Tuple[SegmentCaps, ...] = ()
    # What the registry has switched on right now: {"login": bool, "as_master": bool, "as_child": bool}.
    gates: Mapping[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Read-only views: a held profile cannot be edited through its mappings.
        object.__setattr__(self, "features", MappingProxyType(dict(self.features)))
        object.__setattr__(self, "gates", MappingProxyType(dict(self.gates)))
        object.__setattr__(self, "segments", tuple(self.segments))

    def __hash__(self) -> int:
        return hash((
            self.broker_id,
            tuple(sorted((f.value, v.value) for f, v in self.features.items())),
            self.segments,
            tuple(sorted(self.gates.items())),
        ))

    # ── evidence ───────────────────────────────────────────────────────────────────────────
    def verification(self, feature: Feature) -> Verification:
        return self.features.get(feature, Verification.NOT_SUPPORTED)

    def role_verification(self, role: Role) -> Verification:
        """Evidence for the role's headline feature only (placement / detection). The design bar uses the full
        requirement set; see `missing_for_design_bar`."""
        return self.verification(ROLE_FEATURE[role])

    # ── gates ──────────────────────────────────────────────────────────────────────────────
    def gate_open(self, role: Role) -> bool:
        """The registry switch for this role (mirrors BrokerCapabilities.as_master / as_child)."""
        return bool(self.gates.get("as_master" if role is Role.MASTER else "as_child", False))

    # ── design bar ─────────────────────────────────────────────────────────────────────────
    def missing_for_design_bar(self, role: Role) -> List[Feature]:
        """Required features of the role that are not VERIFIED_LIVE (empty when the evidence is complete)."""
        return [f for f in ROLE_REQUIREMENTS[role] if not self.verification(f).is_live]

    def meets_design_bar(self, role: Role) -> bool:
        """The architecture's bar for enabling a pair: EVERY feature the role needs is VERIFIED_LIVE and the
        registry gate for the role is open. Gates and evidence stay independent inputs. This is stricter than
        the registry today (Groww Child opens on documentation alone) and is informational only in R1."""
        return self.gate_open(role) and not self.missing_for_design_bar(role)

    # ── segments ───────────────────────────────────────────────────────────────────────────
    def segment(self, exchange: str, segment: str) -> Optional[SegmentCaps]:
        ex, seg = str(exchange or "").upper().strip(), str(segment or "").upper().strip()
        for caps in self.segments:
            if caps.exchange == ex and caps.segment == seg:
                return caps
        return None

    def supports(self, exchange: str, segment: str, product: str, order_type: str, *,
                 minimum: Verification = Verification.VERIFIED_LIVE) -> bool:
        """True only when the segment is listed with evidence of at least `minimum` and lists both the product
        and the order type. Takes CANONICAL product / order-type values (MIS, CNC, NRML; MARKET, LIMIT, SL,
        SLM); for real-world values use `supports_order`.

        The default is the strictest level, so an unverified or documentation-only segment is NOT supported
        unless the caller states the weaker level it accepts (`minimum=Verification.VERIFIED_DOCS` or
        `Verification.UNVERIFIED`). A NOT_SUPPORTED segment is never supported."""
        caps = self.segment(exchange, segment)
        if caps is None or caps.verification is Verification.NOT_SUPPORTED:
            return False
        if not caps.verification.at_least(minimum):
            return False
        return (
            str(product or "").upper().strip() in caps.products
            and str(order_type or "").upper().strip() in caps.order_types
        )

    def supports_order(self, exchange: str, segment: str, product: object, order_type: object, *,
                       minimum: Verification = Verification.VERIFIED_LIVE) -> bool:
        """`supports()` for real-world values: `LONGTERM` / `INTRADAY` / `ProductType.CNC`, `SL-M` /
        `OrderType.SL_M`. Products go through the existing `normalize_product_for_exchange` (so CNC folds into
        NRML on derivative exchanges); an unknown product or order type is unsupported (fail closed)."""
        from app.services.copy_engine.risk_inputs import UnknownProductTypeError, normalize_product_for_exchange

        canonical_type = normalize_order_type(order_type)
        if canonical_type is None:
            return False
        try:
            canonical_product = normalize_product_for_exchange(getattr(product, "value", product), exchange)
        except UnknownProductTypeError:
            return False
        return self.supports(exchange, segment, canonical_product, canonical_type, minimum=minimum)
