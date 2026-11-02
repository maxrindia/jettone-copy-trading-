"""
F&O capabilities of each broker AS A CHILD, and the gate that has to be open before a cross-broker derivative order is sent to it.
===============================================================================================================================
One table, read by the route policy and by the tests, so "what may this broker do with an F&O order" has one answer.

`verification` says how far a capability is evidenced, in the same spirit as brokers/capabilities.py:
  VERIFIED_LIVE  seen working against a real account in production;
  CODE_ONLY      built and tested offline against documentation-shaped fakes; NEVER observed live (so it is gated, off by default);
  NOT_BUILT      no code path exists.
Nothing is claimed beyond that. A capability that is CODE_ONLY or NOT_BUILT can only be reached through its own gate.

Adding a Child broker = one entry here + its resolver / lot provider in copy_engine/derivative_routing.py. The copy pipeline is not edited.
"""
from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet, Optional

from app.core.config import settings
from app.services.brokers.registry import normalize_broker_id


@dataclass(frozen=True)
class FnoChildCapability:
    broker: str
    place: str                                  # verification level of placing an F&O order
    modify: str
    cancel: str
    reconcile: str                              # order status / fill sync of an F&O order
    order_types: FrozenSet[str]
    products: FrozenSet[str]
    gate: Callable[[], bool]                    # True only when the owner has switched this broker's F&O Child role on
    notes: str = ""


_ALL_TYPES = frozenset({"MARKET", "LIMIT", "SL", "SLM"})

FNO_CHILD_CAPABILITIES: Dict[str, FnoChildCapability] = {
    "alicebluenew": FnoChildCapability(
        "alicebluenew", "VERIFIED_LIVE", "VERIFIED_LIVE", "VERIFIED_LIVE", "VERIFIED_LIVE", _ALL_TYPES, frozenset({"MIS", "NRML"}),
        gate=lambda: True, notes="Alice -> Alice F&O is the production path. A cross-broker order to Alice is CODE_ONLY until seen live.",
    ),
    "groww": FnoChildCapability(
        "groww", "CODE_ONLY", "CODE_ONLY", "CODE_ONLY", "CODE_ONLY", _ALL_TYPES, frozenset({"MIS", "NRML"}),
        gate=lambda: bool(settings.GROWW_FNO_CHILD_ENABLED),
        notes="The create-order quantity unit for FNO is not documented (the order LIST shows units); a part-filled order is not modified.",
    ),
    "zerodha": FnoChildCapability(
        "zerodha", "CODE_ONLY", "CODE_ONLY", "CODE_ONLY", "CODE_ONLY", _ALL_TYPES, frozenset({"MIS", "NRML"}),
        gate=lambda: bool(settings.ZERODHA_FNO_CHILD_ENABLED),
        notes="Built from the documentation; the dump has no freeze quantity and `name` is undocumented for derivatives.",
    ),
}


def capability_for(broker_id: Optional[str]) -> Optional[FnoChildCapability]:
    canonical = normalize_broker_id(broker_id)
    return FNO_CHILD_CAPABILITIES.get(canonical) if canonical else None


def child_gate_open(broker_id: Optional[str]) -> bool:
    """True when `broker_id` may receive a cross-broker F&O order. An unknown broker is never open."""
    cap = capability_for(broker_id)
    try:
        return bool(cap and cap.gate())
    except Exception:
        return False
