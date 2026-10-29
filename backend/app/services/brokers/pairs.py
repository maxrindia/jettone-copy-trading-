"""
Explicit Master -> Child broker-pair support gate (R5).

A pair is supported ONLY when it is on the allowlist below AND the live registry gates and capability evidence still back
it. Everything else - an unknown broker, a missing Master, a pair nobody has listed, a role gate that is closed, a feature
whose evidence is below VERIFIED_LIVE, any error while checking - is unsupported and fails closed with
BROKER_PAIR_NOT_SUPPORTED. Nothing here reads a catalog, an instrument master or a flag that enables something; it calls no
broker, Redis or database and creates no order.

Why an allowlist and not a derivation
-------------------------------------
Whether two brokers can be paired is not a property of either alone, and "an adapter exists" is not evidence that a pair
works end to end. A catalog adapter (read-only instrument data), a capability profile (declared evidence), an execution
adapter (place / modify / cancel written against documentation) and a pair verified through placement, lifecycle,
reconciliation and instrument identity are four different things. The allowlist is the one place a human states the fourth,
together with the evidence for it. The registry and profile checks below can only make a listed pair STOP being supported
(a gate closed, evidence lowered); they can never make an unlisted pair supported.

Adding a pair
-------------
Add it to SUPPORTED_PAIRS with its evidence in the same change, after the acceptance criteria in
docs/architecture/multi-broker-copy-trading/README.md are met (both roles VERIFIED_LIVE, instrument mapping for the
segments the pair claims, SELL guard on the Child broker, funds, pricing, idempotency, reconciliation, a supervised live
smoke order and recorded owner approval). Alice Blue -> Groww and Alice Blue -> Angel One are deliberately absent.

Scope
-----
This gates the creation of NEW Child orders. It does not gate modify, cancel, SL-trigger repricing or an operator square-off,
which act on orders that already exist; blocking them would leave a live Child order unmanageable.
"""
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Tuple

from app.services.brokers.capabilities import Feature, Verification
from app.services.brokers.profiles import build_profile
from app.services.brokers.registry import can_be_child, can_be_master, get_broker_spec, normalize_broker_id

BROKER_PAIR_NOT_SUPPORTED = "BROKER_PAIR_NOT_SUPPORTED"

ALICE = "alicebluenew"

# (master broker, child broker) -> the evidence that justifies it. Keys are canonical registry ids.
SUPPORTED_PAIRS: Mapping[Tuple[str, str], str] = {
    (ALICE, ALICE): (
        "Alice Blue -> Alice Blue: the production path. Master detection and every Child feature (placement, modify, cancel, "
        "order status, positions, funds) are VERIFIED_LIVE in the capability profile; the Master's token is native to the "
        "Child's broker; duplicate-send protection, ambiguous-send resolution, per-Child outcomes, restart recovery and "
        "position reconciliation are implemented and tested for it."
    ),
}

# The live evidence a listed pair must still have. A profile that lowers any of these ends the pair's support.
REQUIRED_MASTER_FEATURES = (Feature.MASTER_DETECTION, Feature.ORDER_STATUS)
REQUIRED_CHILD_FEATURES = (
    Feature.CHILD_PLACEMENT, Feature.MODIFY, Feature.CANCEL, Feature.ORDER_STATUS, Feature.POSITIONS, Feature.FUNDS,
)


@dataclass(frozen=True)
class PairDecision:
    supported: bool
    code: Optional[str]            # BROKER_PAIR_NOT_SUPPORTED when not supported, else None
    detail: str                    # short, non-sensitive: broker ids and a reason only


def _no(detail: str) -> PairDecision:
    return PairDecision(False, BROKER_PAIR_NOT_SUPPORTED, detail[:200])


def _feature_gap(broker_id: str, features: Tuple[Feature, ...]) -> Optional[str]:
    profile = build_profile(broker_id)
    if profile is None:
        return f"NO_PROFILE:{broker_id}"
    for feature in features:
        level = profile.features.get(feature, Verification.NOT_SUPPORTED)
        if level is not Verification.VERIFIED_LIVE:
            return f"{broker_id}:{feature.value}={level.value}"
    return None


def check_pair(master_broker: Optional[str], child_broker: Optional[str]) -> PairDecision:
    """Whether copying a Master-broker order to a Child-broker account is supported. Never raises.

    A NULL stored broker means Alice Blue (the column's historical default, as everywhere else in the registry). A `master_broker`
    of None here means the caller could NOT establish the Master's broker, which is unsupported."""
    try:
        if master_broker is None:
            return _no("MASTER_BROKER_UNKNOWN")
        master = normalize_broker_id(master_broker)
        child = normalize_broker_id(child_broker)
        if master is None:
            return _no(f"MASTER_BROKER_UNKNOWN:{str(master_broker)[:40]}")
        if child is None:
            return _no(f"CHILD_BROKER_UNKNOWN:{str(child_broker)[:40]}")
        if (master, child) not in SUPPORTED_PAIRS:
            return _no(f"PAIR_NOT_ALLOWLISTED:{master}->{child}")
        if not can_be_master(master):
            return _no(f"MASTER_ROLE_NOT_OPEN:{master}")
        if not can_be_child(child):
            return _no(f"CHILD_ROLE_NOT_OPEN:{child}")
        gap = _feature_gap(master, REQUIRED_MASTER_FEATURES) or _feature_gap(child, REQUIRED_CHILD_FEATURES)
        if gap:
            return _no(f"EVIDENCE_BELOW_LIVE:{gap}")
        return PairDecision(True, None, f"{master}->{child}")
    except Exception as exc:                                    # an error is never read as support
        return _no(f"PAIR_CHECK_ERROR:{type(exc).__name__}")


def pair_requirements(master_broker: Optional[str], child_broker: Optional[str]) -> Tuple[Dict[str, str], ...]:
    """EVERY requirement a pair still lacks, not only the first (check_pair stops at the first). Read-only and never raises; an
    empty tuple means nothing is missing. It describes, it never grants: a pair is supported only when check_pair says so.

    The list is about REAL-BROKER readiness (allowlist, role gates, live-verified evidence). It is not a statement about the code:
    the engine's fan-out is exercised for every pair with fakes (tests/test_multi_broker_fanout.py), and a fake is not evidence."""
    try:
        master = normalize_broker_id(master_broker) if master_broker is not None else None
        child = normalize_broker_id(child_broker)
        if master is None or child is None:
            return ({"requirement": "BROKER_UNKNOWN", "detail": "A broker of this pair is not recognised."},)
        missing = []
        if (master, child) not in SUPPORTED_PAIRS:
            missing.append({"requirement": "PAIR_NOT_ALLOWLISTED",
                            "detail": f"{master}->{child} is not on the allowlist; it is added only after a supervised live smoke order and recorded owner approval."})
        for role, broker, open_ in (("MASTER", master, can_be_master(master)), ("CHILD", child, can_be_child(child))):
            if not open_:
                spec = get_broker_spec(broker)
                missing.append({"requirement": f"{role}_ROLE_NOT_OPEN", "detail": f"{broker}: {spec.reason if spec else 'unknown broker'}"})
        for broker, features in ((master, REQUIRED_MASTER_FEATURES), (child, REQUIRED_CHILD_FEATURES)):
            profile = build_profile(broker)
            for feature in features:
                level = profile.features.get(feature, Verification.NOT_SUPPORTED) if profile else Verification.NOT_SUPPORTED
                if level is not Verification.VERIFIED_LIVE:
                    missing.append({"requirement": "EVIDENCE_BELOW_LIVE", "detail": f"{broker}: {feature.value} is {level.value}, needs VERIFIED_LIVE"})
        return tuple(missing)
    except Exception as exc:                                    # an error is never read as "nothing missing"
        return ({"requirement": "PAIR_CHECK_ERROR", "detail": type(exc).__name__},)
