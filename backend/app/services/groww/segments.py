"""
Groww order-book SEGMENTS as interchangeable policies (Strategy pattern).
=========================================================================
Groww keeps one order list per segment (CASH, FNO). What a row MEANS - which instrument it names, what its quantity counts, which exchange
it is on - differs by segment, so each segment is a small policy object that turns a raw row into the broker-neutral `OrderRecord` the Master
poller already understands. `GrowwClient.get_order_book` asks the registry of enabled policies and merges their records: it knows no segment
by name, so another segment is one more policy, not an edit to the client or to the common engine.

  CashSegment         unchanged: the strict parser the Child path has always used. Always enabled.
  DerivativesSegment  F&O (phase A, DETECTION only). Enabled by GROWW_FNO_MASTER_ENABLED (default off). A row becomes a record carrying a
                      canonical contract key (underlying + expiry + strike + CE/PE/FUT, instruments/contracts.py) and exchange NFO, with the
                      quantity in UNITS. A row it cannot prove it understands is returned with an UNSUPPORTED_FNO_<reason> status, which the
                      Master poller never dispatches (BROKER_STATUS_MAP has no such status), so it is surfaced and never copied.

Nothing here places an order. Everything Groww's documentation does not state is a closed gate, not a guess:
  * quantity unit (units vs lots)        -> GROWW_FNO_QUANTITY_UNIT, empty by default: every F&O row is UNSUPPORTED until it is set;
  * the F&O product                      -> only MIS / NRML (the documented products minus CNC, which is delivery);
  * the F&O exchange                     -> only NSE rows are read, as NFO; anything else is UNSUPPORTED;
  * a future's instrument_type           -> not stated; a row that is not CE / PE / FUT* is not loaded, so it cannot resolve.
"""
import dataclasses
import logging
from typing import Any, Dict, Optional, Protocol, Set, Tuple

from app.core.config import settings
from app.services.alice_blue.instruments import InstrumentResolutionError
from app.services.alice_blue.types import OrderRecord
from app.services.instruments.contracts import make_contract

logger = logging.getLogger("groww_segments")

FNO_PRODUCTS = frozenset({"MIS", "NRML"})
CANONICAL_FNO_EXCHANGE = "NFO"
UNSUPPORTED_PREFIX = "UNSUPPORTED_FNO_"


class SegmentPolicy(Protocol):
    name: str

    def enabled(self) -> bool: ...

    def to_record(self, client: Any, row: Dict[str, Any]) -> OrderRecord: ...


class CashSegment:
    name = "CASH"

    def enabled(self) -> bool:
        return True

    def to_record(self, client: Any, row: Dict[str, Any]) -> OrderRecord:
        return client.parse_order_row(row)


class DerivativesSegment:
    name = "FNO"

    def __init__(self) -> None:
        self._reported: Set[Tuple[str, str]] = set()

    def enabled(self) -> bool:
        return bool(settings.GROWW_FNO_MASTER_ENABLED)

    def to_record(self, client: Any, row: Dict[str, Any]) -> OrderRecord:
        record = client.parse_order_row(row)                    # strict base fields: a malformed row refuses the whole read (fail closed)
        reason, normalized = self._normalize(client, row, record)
        if reason is not None:
            self._report_once(record.broker_order_id, reason)
            return dataclasses.replace(record, status=UNSUPPORTED_PREFIX + reason)
        return normalized

    def _normalize(self, client: Any, row: Dict[str, Any], record: OrderRecord) -> Tuple[Optional[str], Optional[OrderRecord]]:
        unit = str(settings.GROWW_FNO_QUANTITY_UNIT or "").strip().upper()
        if unit not in ("UNITS", "LOTS"):
            return "QUANTITY_UNIT_UNSET", None
        if str(row.get("segment") or "").strip().upper() != self.name:
            return "SEGMENT_MISMATCH", None
        if str(row.get("exchange") or "").strip().upper() != "NSE":
            return "EXCHANGE_NOT_NSE", None
        product = str(record.product_type or "").strip().upper()
        if product not in FNO_PRODUCTS:
            return "PRODUCT_NOT_FNO", None
        try:
            inst = client._instruments.derivative("NSE", record.symbol)
        except InstrumentResolutionError:
            return "INSTRUMENT_UNKNOWN", None
        contract = make_contract(inst.kind, CANONICAL_FNO_EXCHANGE, inst.underlying, inst.expiry, inst.strike)
        if contract is None:
            return "CONTRACT_INVALID", None
        quantity, filled = record.quantity, record.filled_quantity
        if unit == "LOTS":
            quantity, filled = quantity * inst.lot_size, filled * inst.lot_size
        elif quantity % inst.lot_size or filled % inst.lot_size:
            return "QUANTITY_NOT_LOT_MULTIPLE", None
        return None, dataclasses.replace(
            record, exchange=CANONICAL_FNO_EXCHANGE, quantity=quantity, filled_quantity=filled, product_type=product, contract_key=contract.key,
            instrument_id=None,                                  # Groww's own id means nothing at another broker; the contract key is the identity
        )

    def _report_once(self, order_id: str, reason: str) -> None:
        key = (order_id, reason)
        if key in self._reported:
            return
        if len(self._reported) > 2000:
            self._reported.clear()
        self._reported.add(key)
        logger.warning(f"[Groww F&O] order ...{str(order_id)[-5:]} is not copyable: {reason}. It is shown as {UNSUPPORTED_PREFIX}{reason} and never dispatched.")


_POLICIES: Tuple[SegmentPolicy, ...] = (CashSegment(), DerivativesSegment())


def enabled_policies() -> Tuple[SegmentPolicy, ...]:
    """The segments the order book reads right now, cash first. Read from settings on every call."""
    return tuple(p for p in _POLICIES if p.enabled())
