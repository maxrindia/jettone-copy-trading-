"""
Validation of a cross-broker F&O Child order: a Chain of Responsibility.
========================================================================
After the Child's quantity has been sized (the existing Copy Quantity settings, FIXED or MULTIPLIER, turned into whole lots by the
calculator), an F&O order that crosses brokers passes through an ordered chain of independent checks. Each check looks at ONE thing and either
passes or refuses with a code; the first refusal stops the chain and the Child is SKIPPED with that code. Nothing is rounded, split or
raised to make an order pass.

  LotMultiple     the Child's own lot size must be stated, and the quantity must be a whole number of lots
  FreezeQuantity  not above the exchange freeze quantity the Child's catalog states (an unstated freeze quantity is not assumed)
  TickSize        a stated limit price must sit on the Child's tick size (a MARKET order has no price yet: not evaluated)

Margin and exposure are the existing RiskEngine's job, which runs on every order. A price BAND is not stated by any broker's catalog, so it
is not evaluated here (the exchange rejects an order outside it). Adding a check = writing a class with `check` and putting it in the chain;
no broker and no pipeline code changes.

The chain only applies to an order that crosses brokers. Same-broker orders (Alice -> Alice, the production path) and cash equity never enter.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional, Protocol, Sequence, Tuple

from app.services.copy_engine.derivative_routing import ChildTerms


@dataclass(frozen=True)
class FnoOrderFacts:
    child_broker: Optional[str]
    quantity: int
    terms: Optional[ChildTerms]
    price: Optional[Decimal] = None          # the Child's LIMIT / SL price when the order has one


class FnoCheck(Protocol):
    name: str

    def check(self, facts: FnoOrderFacts) -> Optional[str]: ...


class LotMultiple:
    name = "lot_multiple"

    def check(self, facts: FnoOrderFacts) -> Optional[str]:
        lot = facts.terms.lot_size if facts.terms is not None else None
        if not lot or lot <= 0:
            return "FNO_LOT_SIZE_UNKNOWN"                       # a lot count cannot be established, so the quantity cannot be validated
        if facts.quantity <= 0 or facts.quantity % lot:
            return "FNO_QUANTITY_NOT_LOT_MULTIPLE"
        return None


class FreezeQuantity:
    name = "freeze_quantity"

    def check(self, facts: FnoOrderFacts) -> Optional[str]:
        freeze = facts.terms.freeze_quantity if facts.terms is not None else None
        return "FNO_FREEZE_QUANTITY_EXCEEDED" if freeze and facts.quantity > freeze else None


class TickSize:
    name = "tick_size"

    def check(self, facts: FnoOrderFacts) -> Optional[str]:
        tick = facts.terms.tick_size if facts.terms is not None else None
        if facts.price is None or facts.price <= 0 or not tick or tick <= 0:
            return None
        try:
            return "FNO_PRICE_NOT_ON_TICK" if (Decimal(str(facts.price)) % Decimal(str(tick))) != 0 else None
        except (InvalidOperation, ArithmeticError):
            return "FNO_PRICE_NOT_ON_TICK"


DEFAULT_CHAIN: Tuple[FnoCheck, ...] = (LotMultiple(), FreezeQuantity(), TickSize())


def run_chain(facts: FnoOrderFacts, chain: Sequence[FnoCheck] = DEFAULT_CHAIN) -> Optional[str]:
    """The first refusal code, or None when every check passes. An error inside a check is a refusal, never a pass."""
    for step in chain:
        try:
            code = step.check(facts)
        except Exception:
            code = "FNO_CHECK_ERROR"
        if code:
            return code
    return None
