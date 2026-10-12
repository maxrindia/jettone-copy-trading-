"""Alice Blue A3 books captured read-only from production on 2026-09-11
(CANBK intraday live test) - the regression fixture for gross P&L.

Values are verbatim from the broker responses; fields that play no part in
P&L are omitted. The broker's own figures for the day: master realizedPnl
-1.52, child realizedPnl -1.48, both accounts flat (netQuantity 0). The old
Demat card showed +4200.16 for both - that was turnover, not P&L.
"""
import copy
from datetime import date
from typing import Any, Dict, List, Optional

TRADING_DAY = date(2026, 9, 11)


def _order(broker_order_id, side, quantity, filled, average_price, status, exchange_time, order_type, price, cancelled=0):
    return {
        "brokerOrderId": broker_order_id,
        "transactionType": side,
        "quantity": quantity,
        "filledQuantity": filled,
        "pendingQuantity": 0,
        "cancelledQuantity": cancelled,
        "averageTradedPrice": average_price,
        "orderStatus": status,
        "exchangeTimestamp": exchange_time,
        "orderType": order_type,
        "price": price,
        "exchange": "NSE",
        "instrumentId": "10794",
        "tradingSymbol": "CANBK",
        "formattedInstrumentName": "CANBK-EQ",
        "product": "INTRADAY",
        "lotsize": 1,
        "validity": "DAY",
    }


# Master (Dhinesh) - broker order, newest first, as Alice Blue returned it.
MASTER_ORDER_BOOK: List[Dict[str, Any]] = [
    _order("26091100067084", "SELL", 10, 10, 123.45, "COMPLETE", "2026-09-11 11:27:30", "MARKET", 0.0),
    _order("26091100066640", "BUY", 5, 5, 123.5, "COMPLETE", "2026-09-11 11:26:09", "LIMIT", 123.5),
    _order("26091100066406", "SELL", 5, 5, 123.56, "COMPLETE", "2026-09-11 11:25:19", "MARKET", 0.0),
    _order("26091100066089", "BUY", 10, 10, 123.67, "COMPLETE", "2026-09-11 11:24:30", "MARKET", 0.0),
    _order("26091100064974", "SELL", 1, 1, 123.52, "COMPLETE", "2026-09-11 11:21:21", "MARKET", 0.0),
    _order("26091100064718", "BUY", 1, 1, 123.54, "COMPLETE", "2026-09-11 11:20:42", "MARKET", 0.0),
    _order("26091100064648", "SELL", 1, 1, 123.5, "COMPLETE", "2026-09-11 11:20:32", "MARKET", 0.0),
    _order("26091100020785", "BUY", 1, 0, 0, "CANCELED", "2026-09-11 09:42:38", "LIMIT", 121.12, cancelled=1),
    _order("26091100020254", "BUY", 1, 1, 123.1, "COMPLETE", "2026-09-11 09:41:10", "MARKET", 0.0),
]

# Child (vikash) - its own fills. Two differ from the master's prices:
# 26091100064765 bought at 123.53 (master 123.54) and 26091100065004 sold at
# 123.55 (master 123.52).
CHILD_ORDER_BOOK: List[Dict[str, Any]] = [
    _order("26091100067096", "SELL", 10, 10, 123.45, "COMPLETE", "2026-09-11 11:27:34", "LIMIT", 123.45),
    _order("26091100066712", "BUY", 5, 5, 123.5, "COMPLETE", "2026-09-11 11:26:09", "LIMIT", 123.5),
    _order("26091100066413", "SELL", 5, 5, 123.56, "COMPLETE", "2026-09-11 11:25:25", "LIMIT", 123.56),
    _order("26091100066143", "BUY", 10, 10, 123.67, "COMPLETE", "2026-09-11 11:24:36", "LIMIT", 123.67),
    _order("26091100065004", "SELL", 1, 1, 123.55, "COMPLETE", "2026-09-11 11:21:25", "LIMIT", 123.52),
    _order("26091100064765", "BUY", 1, 1, 123.53, "COMPLETE", "2026-09-11 11:20:48", "LIMIT", 123.54),
    _order("26091100064679", "SELL", 1, 1, 123.5, "COMPLETE", "2026-09-11 11:20:38", "LIMIT", 123.5),
    _order("26091100020862", "BUY", 1, 0, 0, "CANCELED", "2026-09-11 09:42:39", "LIMIT", 121.12, cancelled=1),
    _order("26091100020355", "BUY", 1, 1, 123.1, "COMPLETE", "2026-09-11 09:41:15", "LIMIT", 123.1),
]


def _position(realized_pnl, mtm, day_buy_value, day_sell_value):
    return {
        "MtoM": mtm,
        "Netamt": -1.53,
        "buyPrice": 123.58,
        "buyQuantity": 17,
        "dayBuyPrice": 123.58,
        "dayBuyQuantity": 17,
        "dayBuyValue": day_buy_value,
        "daySellPrice": 123.49,
        "daySellQuantity": 17,
        "daySellValue": day_sell_value,
        "exchange": "NSE",
        "formattedInstrumentName": "CANBK-EQ",
        "instrumentId": "10794",
        "lotsize": 1,
        "ltp": 123.18,
        "multiplier": 1,
        "netAveragePrice": 0.0,
        "netQuantity": 0,
        "overnightPrice": 0.0,
        "overnightQuantity": 0,
        "previousDayClose": 124.6,
        "product": "MIS",
        "realizedPnl": realized_pnl,
        "sellPrice": 123.49,
        "sellQuantity": 17,
        "tradingSymbol": "CANBK-EQ",
        "unrealizedPnl": 0.0,
    }


MASTER_POSITIONS: List[Dict[str, Any]] = [
    _position(-1.5199999809265137, -1.52, 2100.840087890625, 2099.320068359375)
]
CHILD_POSITIONS: List[Dict[str, Any]] = [
    _position(-1.4800000190734863, -1.48, 2100.830078125, 2099.35009765625)
]


def on_day(rows: List[Dict[str, Any]], day: date) -> List[Dict[str, Any]]:
    """The same rows with their exchange timestamps moved onto `day` (for
    tests that run against the real clock)."""
    moved = copy.deepcopy(rows)
    for row in moved:
        if row.get("exchangeTimestamp"):
            row["exchangeTimestamp"] = day.isoformat() + row["exchangeTimestamp"][10:]
    return moved


class FakeBrokerClient:
    """Stands in for AliceBlueClient's two read-only raw-book methods.
    A book set to an Exception raises it; None means "could not be read"."""

    def __init__(self, orders: Any, positions: Optional[Any]):
        self.orders = orders
        self.positions = positions
        self.order_book_calls = 0
        self.position_calls = 0

    async def get_order_book_raw(self):
        self.order_book_calls += 1
        if isinstance(self.orders, Exception):
            raise self.orders
        return copy.deepcopy(self.orders)

    async def get_positions_raw(self):
        self.position_calls += 1
        if isinstance(self.positions, Exception):
            raise self.positions
        return copy.deepcopy(self.positions)
