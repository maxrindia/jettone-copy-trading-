"""
How an F&O order's `quantity` is read: UNITS (a multiple of the lot size) or LOTS (a count of lots). Broker-neutral, shared by every Master
adapter that reads derivative orders (Groww today, Zerodha next), so the rule is written - and tested - once.

A broker's documentation often does not say which one its `quantity` is, so nothing here assumes it: the unit is either configured
explicitly (UNITS / LOTS) or INFERRED per order (AUTO) from documented instrument fields, and an order whose unit is not unambiguous is
refused by the caller, never guessed.
"""
from typing import Optional, Tuple


def infer_quantity_unit(quantity: int, lot_size: int, freeze_quantity: int) -> Optional[str]:
    """UNITS or LOTS for ONE order from documented instrument fields only, or None when it is not unambiguous.

    Units must be a multiple of the lot size, so a quantity BELOW one lot can only be a count of lots (and only if it does not exceed the most
    lots one order may hold, freeze_quantity // lot_size). A quantity of at least one lot that is a multiple of it is units - unless it could
    also be a legal count of lots. With no freeze quantity that bound is unknown and the order is ambiguous. Index contracts always resolve
    (NIFTY: 65 per lot, at most 27 lots per order, so 65 is units and 1 is lots); an instrument where both readings fit is refused."""
    if quantity <= 0 or lot_size <= 0 or freeze_quantity <= 0:
        return None                                      # the freeze quantity is what bounds a legal count of lots; without it nothing is provable
    max_lots = freeze_quantity // lot_size
    if quantity < lot_size:
        return "LOTS" if quantity <= max_lots else None  # below one lot can only be lots - and only a legal number of them
    if quantity % lot_size or quantity <= max_lots:
        return None                                      # not a whole number of lots as units, or it fits as lots too: ambiguous
    return "UNITS"


def to_units(unit: str, quantity: int, filled: int, lot_size: int) -> Optional[Tuple[int, int]]:
    """(quantity, filled) in UNITS for a configured unit, or None when they are not whole lots as units. LOTS is multiplied out."""
    if unit == "LOTS":
        return quantity * lot_size, filled * lot_size
    if unit == "UNITS" and not (quantity % lot_size or filled % lot_size):
        return quantity, filled
    return None
