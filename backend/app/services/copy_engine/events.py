import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

COPY_EVENT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "jettone:copy-engine:copy-event")


def derive_copy_event_id(
    master_order_id: str,
    follower_account_id: str,
    fill_sequence: int = 1,
) -> uuid.UUID:
    """
    Deterministic idempotency key. The same broker event redelivered any number
    of times (WebSocket replay, restart, Redis flush) always maps to the same
    UUID, so the DB unique constraint (master_order_id, follower_account_id,
    copy_event_id) genuinely blocks duplicate copy trades.
    """
    return uuid.uuid5(
        COPY_EVENT_NAMESPACE,
        f"{master_order_id}:{follower_account_id}:{fill_sequence}",
    )


@dataclass
class MasterTradeEvent:
    """
    Internal normalized trade / order event produced when the Master account creates,
    fills, cancels, or modifies an order.
    """
    master_order_id: str
    symbol: str
    exchange: str
    side: Literal["BUY", "SELL"]
    quantity: int                 # Incremental or total quantity
    price: Decimal                # Fill price or limit price
    product_type: str             # MIS, NRML, CNC
    timestamp: datetime
    fill_sequence: int = 1        # Dedup differentiator for fill events on the same master order.
                                   # For detector-produced fill events this is the order's cumulative
                                   # filled_quantity at the time of this fill (durable, restart-safe -
                                   # see MasterTradeDetector.process_order_update), not an incrementing
                                   # counter. Fixed at 1 for LIMIT_CREATED/ORDER_MODIFIED, which only
                                   # ever fire once per broker_order_id.
    account_id: str = ""
    order_type: str = "MARKET"    # MARKET, LIMIT, SL, SLM
    limit_price: Decimal = Decimal("0")
    trigger_price: Decimal = Decimal("0")
    event_type: str = "MARKET_ACCEPTED"  # LIMIT_CREATED, MARKET_ACCEPTED, ORDER_FILLED, ORDER_CANCELLED, ORDER_MODIFIED, ORDER_REJECTED
    source: str = "INTERNAL_UI"   # INTERNAL_UI, BROKER_EXTERNAL, SYSTEM
    total_quantity: int = 0
    filled_quantity: int = 0
    pending_quantity: int = 0
    status: str = "OPEN"
    broker_status: Optional[str] = None
    rejection_reason: Optional[str] = None
    # Alice Blue's own numeric instrument identity for the master's order,
    # when known (see OrderRecord.instrument_id). Takes precedence over
    # symbol-based NFO resolution at follower placement/position-check time,
    # since a bare option/future symbol like "NIFTY" cannot by itself
    # identify which of many same-named contracts was actually traded.
    instrument_id: Optional[str] = None
