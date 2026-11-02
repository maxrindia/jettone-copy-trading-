"""
Groww order-book SEGMENTS as interchangeable policies (Strategy pattern).
=========================================================================
Groww keeps one order list per segment (CASH, FNO). What a row MEANS - which instrument it names, what its quantity counts, which exchange
it is on - differs by segment, so each segment is a small policy object that turns a raw row into the broker-neutral `OrderRecord` the Master
poller already understands. `GrowwClient.get_order_book` asks the registry of enabled policies and merges their records: it knows no segment
by name, so another segment is one more policy, not an edit to the client or to the common engine.

  CashSegment         unchanged: the strict parser the Child path has always used. Always enabled. A bad row refuses the whole read.
  DerivativesSegment  F&O (phase A, DETECTION only). Enabled by GROWW_FNO_MASTER_ENABLED (default off). A row becomes a record carrying a
                      canonical contract key (underlying + expiry + strike + CE/PE/FUT, instruments/contracts.py) and exchange NFO, with the
                      quantity in UNITS. A row it cannot prove it understands is returned with an UNSUPPORTED_FNO_<reason> status, which the
                      Master poller never dispatches (BROKER_STATUS_MAP has no such status), so it is surfaced and never copied. A row so
                      malformed that the strict parser refuses it is skipped and logged WITH ITS FIELDS (`isolate_row_errors`): one odd F&O
                      row must not blind the cash book of the same Master.

Nothing here places an order. Everything Groww's documentation does not state is a closed gate, not a guess:
  * quantity unit (units vs lots)        -> GROWW_FNO_QUANTITY_UNIT, empty by default: every F&O row is UNSUPPORTED until it is set. Besides
                                            UNITS and LOTS there is AUTO: the unit is INFERRED PER ORDER from documented fields only (lot_size and
                                            freeze_quantity) and an order whose unit is not unambiguous is UNSUPPORTED, never guessed;
  * the F&O product                      -> only MIS / NRML (the documented products minus CNC, which is delivery);
  * the F&O exchange                     -> an NSE or NFO row is read, as NFO (the instrument is then looked up by trading_symbol in the
                                            F&O catalog, which must hold exactly one row); anything else is UNSUPPORTED;
  * a future's instrument_type           -> not stated; a row that is not CE / PE / FUT* is not loaded, so it cannot resolve.
Every refusal is logged once per order with the raw values that caused it (segment, exchange, product, type, status, quantity), so a live
test shows exactly which assumption was wrong.
"""
import dataclasses
import logging
from typing import Any, Dict, Optional, Protocol, Set, Tuple

from app.core.config import settings
from app.services.alice_blue.instruments import InstrumentResolutionError
from app.services.alice_blue.types import OrderRecord
from app.services.brokers.fno_units import infer_quantity_unit  # noqa: F401  (re-exported: the rule is shared by every Master adapter)
from app.services.instruments.contracts import make_contract

logger = logging.getLogger("groww_segments")

FNO_PRODUCTS = frozenset({"MIS", "NRML"})
FNO_EXCHANGES = frozenset({"NSE", "NFO"})
CANONICAL_FNO_EXCHANGE = "NFO"
UNSUPPORTED_PREFIX = "UNSUPPORTED_FNO_"


class SegmentPolicy(Protocol):
    name: str
    isolate_row_errors: bool

    def enabled(self) -> bool: ...

    def to_record(self, client: Any, row: Dict[str, Any]) -> OrderRecord: ...

    def describe_row(self, row: Dict[str, Any]) -> Dict[str, Any]: ...


class CashSegment:
    name = "CASH"
    isolate_row_errors = False

    def enabled(self) -> bool:
        return True

    def to_record(self, client: Any, row: Dict[str, Any]) -> OrderRecord:
        return client.parse_order_row(row)

    def describe_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        return {}


class DerivativesSegment:
    name = "FNO"
    isolate_row_errors = True

    def __init__(self) -> None:
        self._reported: Set[Tuple[str, str]] = set()

    def enabled(self) -> bool:
        return bool(settings.GROWW_FNO_MASTER_ENABLED)

    def describe_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        keys = ("segment", "exchange", "product", "order_type", "order_status", "transaction_type", "quantity", "filled_quantity", "trading_symbol")
        return {k: row.get(k) for k in keys if k in row}

    def to_record(self, client: Any, row: Dict[str, Any]) -> OrderRecord:
        record = client.parse_order_row(row)                    # strict base fields; a malformed row is isolated by the caller, with its fields logged
        reason, normalized = self._normalize(client, row, record)
        if reason is not None:
            self._report_once(record.broker_order_id, reason, row)
            return dataclasses.replace(record, status=UNSUPPORTED_PREFIX + reason)
        return normalized

    def _normalize(self, client: Any, row: Dict[str, Any], record: OrderRecord) -> Tuple[Optional[str], Optional[OrderRecord]]:
        unit = str(settings.GROWW_FNO_QUANTITY_UNIT or "").strip().upper()
        if unit not in ("UNITS", "LOTS", "AUTO"):
            return "QUANTITY_UNIT_UNSET", None
        if str(row.get("segment") or "").strip().upper() != self.name:
            return "SEGMENT_MISMATCH", None
        if str(row.get("exchange") or "").strip().upper() not in FNO_EXCHANGES:
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
        if unit == "AUTO":
            unit = infer_quantity_unit(quantity, inst.lot_size, inst.freeze_quantity) or ""
            if not unit:
                return "QUANTITY_UNIT_AMBIGUOUS", None
            logger.info(f"[Groww F&O] order ...{str(record.broker_order_id)[-5:]} {record.symbol}: quantity {quantity} read as {unit} "
                        f"(lot {inst.lot_size}, freeze {inst.freeze_quantity}).")
        if unit == "LOTS":
            quantity, filled = quantity * inst.lot_size, filled * inst.lot_size
        elif quantity % inst.lot_size or filled % inst.lot_size:
            return "QUANTITY_NOT_LOT_MULTIPLE", None
        return None, dataclasses.replace(
            record, exchange=CANONICAL_FNO_EXCHANGE, quantity=quantity, filled_quantity=filled, product_type=product, contract_key=contract.key,
            instrument_id=None,                                  # Groww's own id means nothing at another broker; the contract key is the identity
        )

    def _report_once(self, order_id: str, reason: str, row: Dict[str, Any]) -> None:
        key = (order_id, reason)
        if key in self._reported:
            return
        if len(self._reported) > 2000:
            self._reported.clear()
        self._reported.add(key)
        logger.warning(f"[Groww F&O] order ...{str(order_id)[-5:]} is not copyable: {reason}. Fields: {self.describe_row(row)}. "
                       f"It is shown as {UNSUPPORTED_PREFIX}{reason} and never dispatched.")


_POLICIES: Tuple[SegmentPolicy, ...] = (CashSegment(), DerivativesSegment())


def enabled_policies() -> Tuple[SegmentPolicy, ...]:
    """The segments the order book reads right now, cash first. Read from settings on every call."""
    return tuple(p for p in _POLICIES if p.enabled())
