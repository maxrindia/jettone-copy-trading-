from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal, Optional, Dict, Any


class OrderResultCategory(str, Enum):
    SUCCESS = "SUCCESS"
    REJECTED = "REJECTED"
    TIMEOUT = "TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    UNKNOWN = "UNKNOWN"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL-M"


class ProductType(str, Enum):
    MIS = "MIS"
    NRML = "NRML"
    CNC = "CNC"


@dataclass
class FundsData:
    account_id: str
    available_margin: Decimal
    collateral: Decimal
    used_margin: Decimal
    updated_at: datetime


@dataclass
class Position:
    symbol: str
    quantity: int  # positive = LONG, negative = SHORT
    side: Literal["BUY", "SELL"]
    average_price: Decimal
    account_id: str
    exchange: str = "NSE"
    product_type: str = "MIS"
    # Alice Blue's own numeric instrument identity for this position, when
    # the position-book response carries one. Confirmed live: for the same
    # NFO option contract, the order book reports tradingSymbol "NIFTY"
    # while the position book reports "NIFTY29SEP26C23900" - different
    # strings for the same instrument - but instrumentId ("74056" in that
    # case) is identical and stable across both endpoints. This is the only
    # confirmed-safe identity for matching a position row back to a master
    # event's instrument when the symbol strings cannot be trusted to agree.
    instrument_id: Optional[str] = None


@dataclass
class OrderRequest:
    symbol: str
    exchange: str
    side: OrderSide
    quantity: int
    product_type: ProductType
    order_type: OrderType
    price: Decimal = Decimal("0")
    trigger_price: Decimal = Decimal("0")
    client_order_id: Optional[str] = None
    instrument_token: Optional[str] = None


@dataclass
class OrderResult:
    category: OrderResultCategory
    client_order_id: Optional[str]
    broker_order_id: Optional[str]
    message: str
    raw_response: Dict[str, Any]
    timestamp: datetime


@dataclass
class OrderRecord:
    broker_order_id: str
    client_order_id: Optional[str]
    symbol: str
    exchange: str
    side: OrderSide
    quantity: int
    filled_quantity: int
    average_price: Decimal
    status: str  # OPEN, COMPLETE, REJECTED, CANCELLED, PARTIAL
    order_timestamp: datetime
    order_type: str = "MARKET"
    product_type: str = "MIS"
    price: Decimal = Decimal("0")
    trigger_price: Decimal = Decimal("0")
    rejection_reason: Optional[str] = None
    # Alice Blue's own numeric instrument identity for this order, when the
    # order-book response carries one (e.g. "74056" for an NFO option) - see
    # get_order_book()'s parsing. Bare option/future symbols like "NIFTY" are
    # ambiguous (many strikes/expiries share one underlying name), so this,
    # when present, is the ONLY reliable way to identify exactly which
    # contract a master order actually traded.
    instrument_id: Optional[str] = None


@dataclass
class OrderUpdateEvent:
    broker_order_id: str
    client_order_id: Optional[str]
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int
    filled_quantity: int
    fill_price: Decimal
    status: Literal["OPEN", "COMPLETE", "REJECTED", "CANCELLED", "PARTIAL"]
    timestamp: datetime
    account_id: str
    product_type: str = "MIS"
    order_type: str = "MARKET"
    price: Decimal = Decimal("0")
    trigger_price: Decimal = Decimal("0")
    rejection_reason: Optional[str] = None
    exchange: str = "NSE"
    # Threaded straight from OrderRecord.instrument_id - see its docstring.
    instrument_id: Optional[str] = None
