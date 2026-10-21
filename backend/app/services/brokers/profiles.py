"""
Capability profiles derived from the broker registry and the contract flags that already exist
(R1 of docs/architecture/multi-broker-copy-trading, 05 §5.2).

Nothing here reads a new flag or enables anything. Evidence comes from facts in the repository, and every
non-Alice feature has an explicit EVIDENCE CEILING below. A contract flag can lower a feature under its ceiling
but never raise it above: flipping `GrowwContract.live_verified` or `master_verified` therefore cannot, by
itself, make placement, modify, cancel, positions, funds, a segment or Master detection VERIFIED_LIVE. A ceiling
is raised only by editing the table below together with the evidence for THAT feature (a reviewed live capture),
and the tests pin the table.

* Alice Blue    - the registry marks it SUPPORTED; segments are those observed in production (NSE cash; NFO
                  options confirmed live 2026-10-14). Everything else is UNVERIFIED or absent.
* Groww         - adapter built from the official documentation (`docs_verified`). Login (the token call only)
                  was observed live on 2026-10-19 (docs/groww-verification.md). Nothing else has been seen live.
* Angel One     - adapter exists, but `ANGEL_CONTRACT.verified` only records that the response keys are filled
                  in, not whether they came from documentation or a capture, so nothing is rated above
                  UNVERIFIED.
* Everything else in the catalog (Zerodha, 5paisa, ...) has no adapter: every feature NOT_SUPPORTED, no segments.

A profile's `gates` always mirror the CURRENT registry capabilities, so flipping `GROWW_CHILD_TRADING_ENABLED`
changes `gate_open` but never the evidence.
"""
from typing import Dict, Mapping, Optional, Tuple

from app.services.brokers import registry
from app.services.brokers.capabilities import (
    CapabilityProfile,
    Feature,
    SegmentCaps,
    Verification,
)
from app.services.brokers.registry import BrokerSpec, all_broker_specs, get_broker_spec

V = Verification
F = Feature
_ALL_PRODUCTS = frozenset({"MIS", "CNC", "NRML"})
_ALL_ORDER_TYPES = frozenset({"MARKET", "LIMIT", "SL", "SLM"})


def _gates(spec: BrokerSpec) -> Dict[str, bool]:
    c = spec.capabilities
    return {"login": c.login, "as_master": c.as_master, "as_child": c.as_child}


def _capped(derived: Mapping[Feature, Verification], ceiling: Mapping[Feature, Verification]) -> Dict[Feature, Verification]:
    """Each feature's evidence is the LOWER of what the contract flags suggest and its recorded ceiling.
    A feature with no ceiling entry is NOT_SUPPORTED."""
    return {
        feature: V.lowest(derived.get(feature, V.NOT_SUPPORTED), ceiling.get(feature, V.NOT_SUPPORTED))
        for feature in set(derived) | set(ceiling)
    }


# ── Alice Blue ─────────────────────────────────────────────────────────────────────────────

def _alice_profile(spec: BrokerSpec) -> CapabilityProfile:
    return CapabilityProfile(
        broker_id=spec.id,
        features={
            F.LOGIN: V.VERIFIED_LIVE,
            F.MASTER_DETECTION: V.VERIFIED_LIVE,
            F.CHILD_PLACEMENT: V.VERIFIED_LIVE,
            F.MODIFY: V.VERIFIED_LIVE,
            F.CANCEL: V.VERIFIED_LIVE,
            F.ORDER_STATUS: V.VERIFIED_LIVE,
            F.ORDER_LOOKUP: V.NOT_SUPPORTED,
            F.POSITIONS: V.VERIFIED_LIVE,
            F.FUNDS: V.VERIFIED_LIVE,
            F.HOLDINGS: V.NOT_SUPPORTED,
            F.CLIENT_REFERENCE: V.NOT_SUPPORTED,   # no order tag is sent (pinned by test_alice_outgoing_order_semantics)
        },
        segments=(
            SegmentCaps("NSE", "CASH", _ALL_PRODUCTS, _ALL_ORDER_TYPES, V.VERIFIED_LIVE),
            SegmentCaps("NFO", "OPT", _ALL_PRODUCTS, _ALL_ORDER_TYPES, V.VERIFIED_LIVE),
            SegmentCaps("NFO", "FUT", _ALL_PRODUCTS, _ALL_ORDER_TYPES, V.UNVERIFIED),
            SegmentCaps("BSE", "CASH", _ALL_PRODUCTS, _ALL_ORDER_TYPES, V.UNVERIFIED),
        ),
        gates=_gates(spec),
    )


# ── Groww ──────────────────────────────────────────────────────────────────────────────────

# Evidence ceilings. Only LOGIN is live: the token call was observed on 2026-10-19. Placement, modify, cancel,
# order book, positions, funds and the client reference are implemented against the documentation and have not
# been exercised on a real account, whatever `GrowwContract.live_verified` says about the RESPONSE shapes.
# Master detection stays UNVERIFIED even if `master_verified` is set: that flag records a reviewed capture, not
# a rating this table has been updated to.
GROWW_FEATURE_CEILING: Mapping[Feature, Verification] = {
    F.LOGIN: V.VERIFIED_LIVE,
    F.MASTER_DETECTION: V.UNVERIFIED,
    F.CHILD_PLACEMENT: V.VERIFIED_DOCS,
    F.MODIFY: V.VERIFIED_DOCS,
    F.CANCEL: V.VERIFIED_DOCS,
    F.ORDER_STATUS: V.VERIFIED_DOCS,
    F.ORDER_LOOKUP: V.NOT_SUPPORTED,        # `_lookup_by_reference` is private; not a public capability
    F.POSITIONS: V.VERIFIED_DOCS,
    F.FUNDS: V.VERIFIED_DOCS,
    F.HOLDINGS: V.NOT_SUPPORTED,
    F.CLIENT_REFERENCE: V.VERIFIED_DOCS,    # order_reference_id
}
GROWW_SEGMENT_CEILING: Mapping[Tuple[str, str], Verification] = {
    ("NSE", "CASH"): V.VERIFIED_DOCS,
    ("BSE", "CASH"): V.VERIFIED_DOCS,
}


def _groww_profile(spec: BrokerSpec) -> CapabilityProfile:
    contract = registry.GROWW_CONTRACT
    base = V.VERIFIED_LIVE if contract.live_verified else (V.VERIFIED_DOCS if contract.docs_verified else V.UNVERIFIED)
    derived = {feature: base for feature in GROWW_FEATURE_CEILING}
    derived[F.LOGIN] = V.VERIFIED_LIVE                               # independent of the response contract
    derived[F.MASTER_DETECTION] = base if contract.master_verified else V.UNVERIFIED
    derived[F.ORDER_LOOKUP] = V.NOT_SUPPORTED
    derived[F.HOLDINGS] = V.NOT_SUPPORTED
    return CapabilityProfile(
        broker_id=spec.id,
        features=_capped(derived, GROWW_FEATURE_CEILING),
        segments=tuple(
            SegmentCaps(ex, seg, _ALL_PRODUCTS, _ALL_ORDER_TYPES, V.lowest(base, ceiling))
            for (ex, seg), ceiling in GROWW_SEGMENT_CEILING.items()
        ),
        gates=_gates(spec),
    )


# ── Angel One ──────────────────────────────────────────────────────────────────────────────

def _angel_profile(spec: BrokerSpec) -> CapabilityProfile:
    """Nothing is rated above UNVERIFIED: `ANGEL_CONTRACT.verified` / `master_verified` say the keys are filled
    in, not where the evidence came from, so they cannot raise a rating by themselves."""
    return CapabilityProfile(
        broker_id=spec.id,
        features={
            F.LOGIN: V.UNVERIFIED,                 # never run against a real account
            F.MASTER_DETECTION: V.UNVERIFIED,
            F.CHILD_PLACEMENT: V.UNVERIFIED,
            F.MODIFY: V.UNVERIFIED,
            F.CANCEL: V.UNVERIFIED,
            F.ORDER_STATUS: V.UNVERIFIED,
            F.ORDER_LOOKUP: V.NOT_SUPPORTED,
            F.POSITIONS: V.UNVERIFIED,
            F.FUNDS: V.UNVERIFIED,
            F.HOLDINGS: V.NOT_SUPPORTED,
            F.CLIENT_REFERENCE: V.NOT_SUPPORTED,   # placeOrder offers none
        },
        segments=(
            SegmentCaps("NSE", "CASH", _ALL_PRODUCTS, _ALL_ORDER_TYPES, V.UNVERIFIED),
            SegmentCaps("BSE", "CASH", _ALL_PRODUCTS, _ALL_ORDER_TYPES, V.UNVERIFIED),
        ),
        gates=_gates(spec),
    )


def _empty_profile(spec: BrokerSpec) -> CapabilityProfile:
    """No adapter: nothing is evidenced and nothing is supported."""
    return CapabilityProfile(broker_id=spec.id, features={}, segments=(), gates=_gates(spec))


_BUILDERS = {"alicebluenew": _alice_profile, "groww": _groww_profile, "angelone": _angel_profile}


def build_profile(broker_id: Optional[str]) -> Optional[CapabilityProfile]:
    """The capability profile for a stored broker value, or None when the broker id is unknown.
    Brokers without an adapter get an empty profile (everything NOT_SUPPORTED)."""
    spec = get_broker_spec(broker_id)
    if spec is None:
        return None
    builder = _BUILDERS.get(spec.id, _empty_profile)
    return builder(spec)


def all_profiles() -> Dict[str, CapabilityProfile]:
    return {broker_id: build_profile(broker_id) for broker_id in all_broker_specs()}  # type: ignore[misc]
