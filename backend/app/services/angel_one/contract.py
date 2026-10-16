"""
Angel One response contract
===========================
Everything about Angel One's READ responses that Jettone depends on for
position safety, margin checks and order lifecycle tracking, in one place.

Angel One's public documentation and official SDKs do not state the order
status values, the position-book keys or the RMS (funds) keys, so none of it
is assumed. `ANGEL_CONTRACT` stays `verified=False` (every parser refuses)
until it is filled in from a supervised capture of the authorized account's
own responses (scripts/angel_capture.py) and reviewed. Tests use a clearly
synthetic contract; the production default never does.

A contract is verified only when ALL of its sections are filled in; a partial
contract still refuses, so one confirmed section can never switch on trading.
"""

from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass(frozen=True)
class PositionKeys:
    symbol: str          # trading symbol key
    token: str           # instrument token key
    exchange: str
    product: str
    net_qty: str         # signed net quantity (positive long, negative short)
    avg_price: str


@dataclass(frozen=True)
class RmsKeys:
    available_margin: str
    used_margin: str
    collateral: str


@dataclass(frozen=True)
class AngelContract:
    # lowercase raw order-book status -> OPEN / PARTIAL / COMPLETE / REJECTED / CANCELLED
    status_map: Dict[str, str] = field(default_factory=dict)
    positions: Optional[PositionKeys] = None
    rms: Optional[RmsKeys] = None

    @property
    def verified(self) -> bool:
        return bool(self.status_map) and self.positions is not None and self.rms is not None


# Production default: nothing confirmed. Do not fill this in from memory,
# forum posts or third-party SDKs.
ANGEL_CONTRACT = AngelContract()

LIFECYCLE_STATES = frozenset({"OPEN", "PARTIAL", "COMPLETE", "REJECTED", "CANCELLED"})


def contract_is_usable(contract: AngelContract) -> bool:
    """verified, and every mapped status is a Jettone lifecycle state."""
    return contract.verified and set(contract.status_map.values()) <= LIFECYCLE_STATES
