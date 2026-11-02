"""
Zerodha F&O orders as a Master's: attach the broker-neutral canonical contract to an NFO order read from the order book.
=========================================================================================================================
Nothing is placed or changed here. For an order on NFO whose instrument is in Zerodha's own dump (exactly one row), the record gets
`contract_key` = CanonicalContract.key (underlying + expiry + strike + CE/PE/FUT), the identity another broker's Child resolves in ITS OWN
catalog (copy_engine/derivative_routing.py). The record's status, quantity and everything else are untouched, so the Child fill sync, which
reads the same order book, sees exactly what it always saw.

Closed unless configured: ZERODHA_FNO_QUANTITY_UNIT must be "UNITS" (Kite's `quantity` is then taken to already be units and a lot multiple).
Without it, or with an unknown / ambiguous instrument or a quantity that is not a whole number of lots, NO key is attached and the order
cannot be routed (fail closed). Kite's documentation does not say the `name` column holds a derivative's underlying, so the key is
best-effort evidence and any Child mapping stays UNVERIFIED (instruments/contracts.py). Zerodha as a Master is a separate gate.
"""
import dataclasses
import logging
from typing import Any

from app.core.config import settings
from app.services.alice_blue.instruments import InstrumentResolutionError
from app.services.alice_blue.types import OrderRecord
from app.services.instruments.contracts import make_contract

logger = logging.getLogger("zerodha_derivatives")


def with_contract_key(record: OrderRecord, store: Any) -> OrderRecord:
    if record.exchange != "NFO" or str(record.status or "").startswith("UNSUPPORTED"):
        return record
    if str(settings.ZERODHA_FNO_QUANTITY_UNIT or "").strip().upper() != "UNITS":
        return record
    try:
        inst = store.resolve("NFO", record.symbol)
    except InstrumentResolutionError:
        return record
    if inst.lot_size <= 0 or record.quantity % inst.lot_size or record.filled_quantity % inst.lot_size:
        return record
    contract = make_contract(inst.instrument_type, "NFO", inst.name, inst.expiry, inst.strike)
    return dataclasses.replace(record, contract_key=contract.key) if contract is not None else record
