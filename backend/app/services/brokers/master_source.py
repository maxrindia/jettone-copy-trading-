"""
Broker-neutral Master order source: how each broker's Master orders are detected, and what one detected order looks like.
=======================================================================================================================
`MasterOrderPoller` already reads any client's order book through `order_book_read.read_order_book`. This module adds the pieces that were
implicit: WHICH detection mechanism a broker offers and how far it is evidenced (`MasterDetectionInfo`), one normalized description of a
detected order (`NormalizedMasterOrder`, broker-aware id included), and a fail-closed adapter (`MasterSourceAdapter`) that refuses to read
for a broker/role the registry has not opened.

Nothing here decides eligibility - the registry gates (`can_be_master`) and the capability profile do. The adapter only refuses to read when
the gate is closed, so a Master source can never be driven for a broker that may not be a Master. The recorded `blocker` of each broker is
the exact unverified fact (see docs/architecture/multi-broker-copy-trading/09).

Detection is polling-first for every broker. A stream (Alice's NorenWS, Zerodha's order WebSocket / postbacks) is only ever a hint to
read the order book again: the order book is the source of truth, because none of the streams is documented to carry orders placed in the
broker's own app.
"""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Optional, Tuple

from app.services.alice_blue.types import OrderRecord
from app.services.brokers.identity import master_scope
from app.services.brokers.order_book_read import NO_CLIENT, OrderBookRead, OrderBookState, read_order_book
from app.services.brokers.registry import can_be_master, normalize_broker_id

# The statuses the Master poller dispatches (its BROKER_STATUS_MAP keys). A pinned test keeps the two in step.
DISPATCHABLE_STATUSES = frozenset({
    "COMPLETE", "COMPLETED", "OPEN", "PENDING", "TRIGGER_PENDING", "PARTIAL", "PARTIALLY_FILLED", "CANCELLED", "REJECTED",
})
MASTER_NOT_OPEN = "MASTER_NOT_OPEN"
NORMALIZE_FAILED = "NORMALIZE_FAILED"


class DetectionMode(str, Enum):
    POLL_ORDER_BOOK = "POLL_ORDER_BOOK"
    POLL_WITH_STREAM_HINT = "POLL_WITH_STREAM_HINT"


@dataclass(frozen=True)
class MasterDetectionInfo:
    broker_id: str
    mode: DetectionMode
    external_orders_verified: bool       # evidence that orders placed in the broker's own app/web appear in the order book
    blocker: Optional[str]               # the exact unverified fact, when not verified
    notes: str = ""


MASTER_DETECTION: Mapping[str, MasterDetectionInfo] = {
    "alicebluenew": MasterDetectionInfo(
        "alicebluenew", DetectionMode.POLL_WITH_STREAM_HINT, True, None,
        "REST order-book polling is proven to observe Mobile/Web orders in production; the NorenWS order stream is silent.",
    ),
    "groww": MasterDetectionInfo(
        "groww", DetectionMode.POLL_ORDER_BOOK, False,
        "Groww's documentation does not say whether orders placed in the Groww app appear in the Trading API order list; no capture exists.",
    ),
    "angelone": MasterDetectionInfo(
        "angelone", DetectionMode.POLL_ORDER_BOOK, False,
        "SmartAPI order-book coverage of orders placed outside the API key is unverified; no capture exists.",
    ),
    "zerodha": MasterDetectionInfo(
        "zerodha", DetectionMode.POLL_WITH_STREAM_HINT, False,
        "Kite's documentation is silent on whether GET /orders, postbacks and the order WebSocket include orders placed outside the API "
        "key (Kite web / mobile); only unofficial forum posts address it.",
    ),
}


def detection_info(broker_id: Optional[str]) -> Optional[MasterDetectionInfo]:
    """Detection facts for a stored broker value, or None when the broker has no Master source (unknown or catalog-only)."""
    canonical = normalize_broker_id(broker_id)
    return MASTER_DETECTION.get(canonical) if canonical else None


@dataclass(frozen=True)
class NormalizedMasterOrder:
    """One detected Master order, independent of the broker that produced it."""
    broker_id: str
    master_account_id: str
    native_order_id: str              # the broker's own id, verbatim - unique only within `broker_id`
    event_scope: str                  # identity.master_scope(): "" for Alice Blue, "<broker>:<account>:" otherwise
    client_reference: Optional[str]
    symbol: str
    exchange: str
    side: str
    order_type: str
    product_type: str
    quantity: int
    filled_quantity: int
    status: str                       # the broker status normalized to the engine vocabulary (unmapped values pass through upper-cased)
    dispatchable: bool                # False => the poller will not copy it (interim or unsupported status)
    order_timestamp: datetime
    price: str
    trigger_price: str
    instrument_id: Optional[str]      # the Master broker's own instrument id; meaningful only within `broker_id`

    @property
    def dedupe_key(self) -> str:
        """The first-line duplicate key for this order's fill, as the validator builds it."""
        return f"{self.event_scope}{self.native_order_id}"


def normalize_order(broker_id: str, master_account_id: Any, record: OrderRecord) -> NormalizedMasterOrder:
    canonical = normalize_broker_id(broker_id) or str(broker_id)
    status = str(record.status or "").strip().upper()
    side = getattr(record.side, "value", record.side)
    return NormalizedMasterOrder(
        broker_id=canonical,
        master_account_id=str(master_account_id),
        native_order_id=str(record.broker_order_id),
        event_scope=master_scope(canonical, master_account_id),
        client_reference=record.client_order_id or None,
        symbol=str(record.symbol),
        exchange=str(record.exchange),
        side=str(side),
        order_type=str(record.order_type or ""),
        product_type=str(record.product_type or ""),
        quantity=int(record.quantity),
        filled_quantity=int(record.filled_quantity),
        status=status,
        dispatchable=status in DISPATCHABLE_STATUSES,
        order_timestamp=record.order_timestamp,
        price=str(record.price),
        trigger_price=str(record.trigger_price),
        instrument_id=record.instrument_id,
    )


class MasterSourceAdapter:
    """Reads one Master account's order book and normalizes it. Never raises; refuses when the broker may not be a Master."""

    def __init__(self, broker_id: Optional[str], master_account_id: Any, client: Optional[Any]):
        self.broker_id = normalize_broker_id(broker_id)
        self.master_account_id = str(master_account_id)
        self._client = client

    @property
    def info(self) -> Optional[MasterDetectionInfo]:
        return detection_info(self.broker_id)

    @property
    def open(self) -> bool:
        return bool(self.broker_id and can_be_master(self.broker_id) and self.info is not None)

    async def read(self, *, checked: bool = True) -> OrderBookRead:
        if not self.open:
            return OrderBookRead(OrderBookState.UNKNOWN, (), MASTER_NOT_OPEN, "", checked)
        if self._client is None:
            return OrderBookRead(OrderBookState.UNKNOWN, (), NO_CLIENT, "", checked)
        return await read_order_book(self._client, checked=checked)

    async def read_normalized(self, *, checked: bool = True) -> Tuple[OrderBookRead, Tuple[NormalizedMasterOrder, ...]]:
        result = await self.read(checked=checked)
        try:
            orders = tuple(normalize_order(self.broker_id or "", self.master_account_id, r) for r in result.records)
        except Exception as exc:                                    # a record that cannot be normalized makes the whole read unusable
            return OrderBookRead(OrderBookState.FAILED, (), NORMALIZE_FAILED, type(exc).__name__, result.checked), ()
        return result, orders
