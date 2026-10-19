"""
Strict numeric parsing for broker responses
===========================================
A number that feeds a safety decision (a position's quantity, an order's filled quantity, available
margin, a price used for exposure) must either be a real, finite number meeting the field's rules, or
the response is refused. Nothing is coerced: null, blank strings, booleans, NaN, +/-Infinity, absurd
magnitudes and non-numeric text never become zero.

Errors carry the field name and a fixed reason, never the offending value, so they are safe to log and
to show.
"""

import math
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional

# Far beyond any real quantity or price, so "1e999999" style values are refused rather than computed with.
# Checked through the decimal exponent (adjusted() < MAX_EXPONENT, i.e. |value| < 1e15) because arithmetic such as
# abs() on a huge-exponent Decimal raises decimal.Overflow instead of returning.
MAX_EXPONENT = 15


class StrictNumberError(ValueError):
    def __init__(self, field: str, reason: str):
        super().__init__(f"{field} {reason}")
        self.field = field
        self.reason = reason


def need(row: Any, key: str) -> Any:
    """row[key], or a StrictNumberError if the row is not an object or the key is absent."""
    if not isinstance(row, Mapping) or key not in row:
        raise StrictNumberError(key, "is missing")
    return row[key]


def strict_decimal(
    value: Any,
    field: str,
    *,
    positive: bool = False,
    allow_negative: bool = True,
    minimum: Optional[Decimal] = None,
    maximum: Optional[Decimal] = None,
) -> Decimal:
    if value is None:
        raise StrictNumberError(field, "is null")
    if isinstance(value, bool):
        raise StrictNumberError(field, "is a boolean, not a number")
    if isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise StrictNumberError(field, "is not finite")
        number = Decimal(repr(value))
    elif isinstance(value, Decimal):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise StrictNumberError(field, "is blank")
        try:
            number = Decimal(text)
        except InvalidOperation:
            raise StrictNumberError(field, "is not numeric") from None
    else:
        raise StrictNumberError(field, "has an unsupported type")
    if not number.is_finite():
        raise StrictNumberError(field, "is not finite")
    if number.adjusted() >= MAX_EXPONENT:
        raise StrictNumberError(field, "is out of range")
    if positive and number <= 0:
        raise StrictNumberError(field, "must be greater than zero")
    if not allow_negative and number < 0:
        raise StrictNumberError(field, "must not be negative")
    if minimum is not None and number < minimum:
        raise StrictNumberError(field, "is below its minimum")
    if maximum is not None and number > maximum:
        raise StrictNumberError(field, "is above its maximum")
    return number


def strict_int(
    value: Any,
    field: str,
    *,
    positive: bool = False,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    number = strict_decimal(
        value, field, positive=positive,
        minimum=None if minimum is None else Decimal(minimum),
        maximum=None if maximum is None else Decimal(maximum),
    )
    if number != number.to_integral_value():
        raise StrictNumberError(field, "is not a whole number")
    return int(number)


def optional_decimal(row: Any, key: str, *, applicable: bool = False, positive_when_applicable: bool = True) -> Decimal:
    """A field that only matters for some order types (a limit price, a trigger, an average fill price).
    When it applies it is required and strict. When it does not apply, an absent key or an explicit null
    means 'not applicable' (0); anything else that IS present must still be a valid non-negative number -
    a blank string, NaN or text is never quietly read as zero."""
    if applicable:
        return strict_decimal(need(row, key), key, positive=positive_when_applicable, allow_negative=False)
    if not isinstance(row, Mapping) or key not in row or row[key] is None:
        return Decimal(0)
    return strict_decimal(row[key], key, allow_negative=False)
