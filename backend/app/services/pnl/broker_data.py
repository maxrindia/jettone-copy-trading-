"""Parsing of raw Alice Blue A3 order-book / position-book rows for P&L
reporting. Pure functions - no I/O, no broker calls.

Field names are the ones observed in live production responses on
2026-09-11, not assumed from documentation:

  /open-api/od/v1/orders/book
      brokerOrderId, transactionType ("BUY"/"SELL"), filledQuantity,
      averageTradedPrice, exchangeTimestamp ("2026-09-11 11:27:34",
      exchange-local time), exchange, instrumentId, tradingSymbol ("CANBK"),
      product ("INTRADAY")
  /open-api/od/v1/positions
      exchange, instrumentId, tradingSymbol ("CANBK-EQ"), product ("MIS"),
      netQuantity, overnightQuantity, ltp, multiplier, realizedPnl,
      unrealizedPnl, MtoM

The two books name the same instrument and product differently (CANBK vs
CANBK-EQ, INTRADAY vs MIS), so rows are joined on (exchange, instrumentId,
normalized product) - never on the trading symbol.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple

from app.core.trading_day import trading_tz


class InstrumentKey(NamedTuple):
    exchange: str
    instrument_id: str
    product: str


# Same product aliases this codebase already applies elsewhere (client.py
# get_positions_detail, the pipeline's product mapping).
_PRODUCT_ALIASES = {
    "INTRADAY": "MIS",
    "MIS": "MIS",
    "CNC": "CNC",
    "DELIVERY": "CNC",
    "LONGTERM": "CNC",
    "NRML": "NRML",
    "CARRYFORWARD": "NRML",
    "MARGIN": "NRML",
}

# The first format is the one observed live for exchangeTimestamp; the second
# is the format Alice Blue uses for its other timestamps (requestTime).
_EXCHANGE_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%H:%M:%S %d-%m-%Y")


def normalize_product(raw: Any) -> str:
    value = str(raw or "").strip().upper()
    return _PRODUCT_ALIASES.get(value, value)


def instrument_key(row: Dict[str, Any]) -> InstrumentKey:
    exchange = str(row.get("exchange") or "").strip().upper()
    instrument_id = str(row.get("instrumentId") or "").strip()
    if not instrument_id:
        # Nothing to join on: key by symbol so the row can never silently
        # merge with another instrument. The two books spell symbols
        # differently, so such a row fails the position cross-check and is
        # reported as unverified rather than mis-costed.
        instrument_id = "SYMBOL:" + str(row.get("tradingSymbol") or "").strip().upper()
    return InstrumentKey(exchange, instrument_id, normalize_product(row.get("product")))


def to_decimal(value: Any) -> Optional[Decimal]:
    """Exact Decimal from a broker value. Floats go through str(), so the
    broker's float32 noise is kept as-is and removed only by rounding."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def to_int(value: Any) -> Optional[int]:
    number = to_decimal(value)
    if number is None or number != number.to_integral_value():
        return None
    return int(number)


def parse_exchange_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in _EXCHANGE_TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=trading_tz())
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class BrokerFill:
    broker_order_id: str
    key: InstrumentKey
    symbol: str
    side: Optional[str]  # "BUY" / "SELL"; None when the broker value is unrecognised
    filled_quantity: int
    average_price: Optional[Decimal]
    exchange_time: Optional[datetime]


@dataclass(frozen=True)
class BrokerPosition:
    key: InstrumentKey
    symbol: str
    net_quantity: Optional[int]
    overnight_quantity: Optional[int]
    ltp: Optional[Decimal]
    multiplier: Optional[Decimal]
    realized_pnl: Optional[Decimal]
    unrealized_pnl: Optional[Decimal]
    mtm: Optional[Decimal]


def _side(value: Any) -> Optional[str]:
    text = str(value or "").strip().upper()
    if text in ("BUY", "B"):
        return "BUY"
    if text in ("SELL", "S"):
        return "SELL"
    return None


def parse_order_book_fills(rows: Iterable[Dict[str, Any]]) -> Tuple[List[BrokerFill], List[str]]:
    """
    Executed fills from one account's order book.

    Inclusion is by filledQuantity > 0 alone, whatever the order status: a
    CANCELLED order that filled 4 before cancellation contributes those 4; a
    REJECTED or zero-fill CANCELLED order contributes nothing.

    The order book reports each order's CUMULATIVE fill, so exactly one entry
    per brokerOrderId is kept - the one with the largest filledQuantity, i.e.
    its latest state (a PARTIAL row superseded by its COMPLETE row). A
    repeated row can therefore never be counted twice.

    Returns (fills, issues). An issue means a row could not be interpreted
    safely, and the caller must not report realized P&L as verified.
    """
    by_order_id: Dict[str, BrokerFill] = {}
    issues: List[str] = []
    for row in rows:
        filled = to_int(row.get("filledQuantity"))
        if filled is None:
            issues.append(f"UNPARSEABLE_FILLED_QUANTITY:{row.get('brokerOrderId') or '?'}")
            continue
        if filled <= 0:
            continue
        broker_order_id = str(row.get("brokerOrderId") or "").strip()
        if not broker_order_id:
            issues.append("FILL_WITHOUT_BROKER_ORDER_ID")
            continue
        fill = BrokerFill(
            broker_order_id=broker_order_id,
            key=instrument_key(row),
            symbol=str(row.get("tradingSymbol") or "").strip(),
            side=_side(row.get("transactionType")),
            filled_quantity=filled,
            average_price=to_decimal(row.get("averageTradedPrice")),
            exchange_time=parse_exchange_time(row.get("exchangeTimestamp")),
        )
        previous = by_order_id.get(broker_order_id)
        if previous is None or fill.filled_quantity >= previous.filled_quantity:
            by_order_id[broker_order_id] = fill
    return list(by_order_id.values()), issues


def parse_positions(rows: Iterable[Dict[str, Any]]) -> List[BrokerPosition]:
    """Every position row, closed ones (netQuantity 0) included - the broker's
    realizedPnl for the day lives on those rows too."""
    return [
        BrokerPosition(
            key=instrument_key(row),
            symbol=str(row.get("tradingSymbol") or "").strip(),
            net_quantity=to_int(row.get("netQuantity")),
            overnight_quantity=to_int(row.get("overnightQuantity")),
            ltp=to_decimal(row.get("ltp")),
            multiplier=to_decimal(row.get("multiplier")),
            realized_pnl=to_decimal(row.get("realizedPnl")),
            unrealized_pnl=to_decimal(row.get("unrealizedPnl")),
            mtm=to_decimal(row.get("MtoM")),
        )
        for row in rows
    ]
