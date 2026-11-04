"""
Angel One F&O rows as the broker-neutral records the copy engine reads.
=====================================================================
Nothing is placed or changed here. `quantity_unit()` is the one place the configured reading of Angel One's `quantity` on an NFO row
(ANGEL_ONE_FNO_QUANTITY_UNIT: UNITS or LOTS, no default because the API documentation does not state it) is interpreted, and `fno_record` /
`fno_position` turn a parsed order / position row into units. Everything fails closed: an unconfigured unit, an instrument the scrip master
does not hold exactly, or a quantity that is not a whole number of lots never produces a usable F&O record.
"""
import dataclasses
from typing import Any, Optional

from app.core.config import settings
from app.services.alice_blue.instruments import InstrumentResolutionError
from app.services.alice_blue.types import OrderRecord, Position
from app.services.brokers.fno_units import to_units
from app.services.instruments.contracts import make_contract

FNO_EXCHANGE = "NFO"
UNSUPPORTED_PREFIX = "UNSUPPORTED_FNO_"


def quantity_unit() -> Optional[str]:
    unit = str(settings.ANGEL_ONE_FNO_QUANTITY_UNIT or "").strip().upper()
    return unit if unit in ("UNITS", "LOTS") else None


def fno_read_enabled() -> bool:
    return bool(settings.ANGEL_ONE_FNO_CHILD_ENABLED or settings.ANGEL_ONE_FNO_MASTER_ENABLED)


def fno_record(record: OrderRecord, store: Any) -> OrderRecord:
    """An NFO order-book row in UNITS with its canonical contract key (Master role only). Unchanged when no F&O gate is open or the row is
    not NFO; a row that cannot be read with confidence keeps its raw numbers and an UNSUPPORTED_FNO_* status (never copied or synced)."""
    if str(record.exchange or "").upper() != FNO_EXCHANGE or not fno_read_enabled() or str(record.status or "").startswith(UNSUPPORTED_PREFIX):
        return record
    try:
        d = store.derivative_by_token(record.instrument_id) if record.instrument_id else store.derivative(record.symbol)
    except InstrumentResolutionError:
        return dataclasses.replace(record, status=UNSUPPORTED_PREFIX + "INSTRUMENT")
    unit = quantity_unit()
    if unit is None:
        return dataclasses.replace(record, status=UNSUPPORTED_PREFIX + "UNIT")
    converted = to_units(unit, record.quantity, record.filled_quantity, d.lot_size)
    if converted is None:
        return dataclasses.replace(record, status=UNSUPPORTED_PREFIX + "QUANTITY")
    key = None
    if settings.ANGEL_ONE_FNO_MASTER_ENABLED:
        contract = make_contract(d.kind, FNO_EXCHANGE, d.underlying, d.expiry, d.strike)
        key = contract.key if contract is not None else None
    return dataclasses.replace(record, symbol=d.tradingsymbol, quantity=converted[0], filled_quantity=converted[1], contract_key=key)


def fno_position(position: Position, store: Any) -> Optional[Position]:
    """An open NFO position in UNITS, or the position unchanged when it is not NFO / the Child F&O gate is closed / it is flat. Raises
    InstrumentResolutionError when an open F&O position cannot be read with confidence: the caller makes the WHOLE book unknown."""
    if str(position.exchange or "").upper() != FNO_EXCHANGE or not settings.ANGEL_ONE_FNO_CHILD_ENABLED or position.quantity == 0:
        return position
    unit = quantity_unit()
    if unit is None:
        raise InstrumentResolutionError("the F&O quantity unit is not configured, so a position quantity cannot be trusted")
    d = store.derivative_by_token(position.instrument_id) if position.instrument_id else store.derivative(position.symbol)
    quantity = position.quantity * d.lot_size if unit == "LOTS" else position.quantity
    if quantity % d.lot_size:
        raise InstrumentResolutionError("an F&O position quantity is not a whole number of lots")
    return dataclasses.replace(position, symbol=d.tradingsymbol, quantity=quantity)
