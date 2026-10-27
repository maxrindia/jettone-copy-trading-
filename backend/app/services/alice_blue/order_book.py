"""
Alice Blue order-book row parsing, as pure functions (no I/O, no client state).

Moved verbatim out of `AliceBlueClient.get_order_book` so that every read of an Alice Blue order book - the parsed one, the
raw-rows one used by the Master-switch preflight and crash recovery, and the single-response checked read - turns the same JSON
rows into the same `OrderRecord`s. Nothing about the parsing changed: the loop body below is the old loop body, de-indented, and the
oracle test (tests/test_alice_order_book_parser.py) pins it against a verbatim copy of the old code.

Two behaviours are deliberately preserved and must not be "tidied", because callers depend on them:
* a row that is not a dict, or one whose quantity / price is not a number, RAISES (the poller's checked read treats a raise as a failed
  read; the sweeper and recovery treat it as unknown). A malformed book is never silently shortened to the rows that parsed;
* an unrecognised status is passed through upper-cased, not mapped, so the poller can refuse it (it fails closed on unmapped statuses).
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Iterable, List

from app.services.alice_blue.types import OrderRecord, OrderSide


def order_book_row_id(item: Dict[str, Any]) -> str:
    """The broker order id of one raw /orders/book row - the single
    definition shared by get_order_book() and the Master-switch preflight
    snapshot, so both always identify the same order the same way."""
    return str(item.get("brokerOrderId", item.get("Nstordno", item.get("orderId", ""))))


def parse_order_book_rows(raw_list: Iterable[Dict[str, Any]]) -> List[OrderRecord]:
    """Alice Blue /orders/book rows -> OrderRecords. Pure; see the module docstring for what it does NOT forgive."""
    orders: List[OrderRecord] = []
    for item in raw_list:
        # Parse order timestamp if present (e.g. "12:53:26 11-07-2026")
        order_time_raw = item.get("orderTime")
        ts = datetime.now(timezone.utc)
        if order_time_raw:
            try:
                ts = datetime.strptime(str(order_time_raw).strip(), "%H:%M:%S %d-%m-%Y").replace(tzinfo=timezone.utc)
            except Exception:
                pass

        # Map transactionType / Trantype
        trans_type = str(item.get("transactionType", item.get("Trantype", ""))).upper()
        side = OrderSide.BUY if trans_type in ("BUY", "B") else OrderSide.SELL

        # Normalize orderStatus / Status
        raw_status = str(item.get("orderStatus", item.get("Status", "UNKNOWN"))).upper()
        status_map = {
            "COMPLETED": "COMPLETE",
            "COMPLETE": "COMPLETE",
            "OPEN": "OPEN",
            "PENDING": "OPEN",
            "REJECTED": "REJECTED",
            "CANCELLED": "CANCELLED",
            "CANCELED": "CANCELLED",
            "PARTIALLY_FILLED": "PARTIAL",
            "PARTIAL": "PARTIAL",
        }
        norm_status = status_map.get(raw_status, raw_status)

        order_type_raw = str(item.get("orderType", item.get("prctyp", "MARKET"))).upper()
        if order_type_raw in ("L", "LIMIT"):
            norm_otype = "LIMIT"
        elif order_type_raw in ("SL", "STOPLOSS"):
            norm_otype = "SL"
        elif order_type_raw in ("SL-M", "SLM"):
            norm_otype = "SLM"
        else:
            norm_otype = "MARKET"

        price_raw = Decimal(str(item.get("price", item.get("prc", "0")) or "0"))
        # Confirmed in production, 2026-09-29: a native SL order's order
        # book row reports its trigger price under "slTriggerPrice", not
        # "triggerPrice"/"trgprc" - those two keys are kept as fallbacks
        # for whichever (if any) response shape actually uses them.
        trigger_price_raw = Decimal(str(
            item.get("slTriggerPrice", item.get("triggerPrice", item.get("trgprc", "0"))) or "0"
        ))
        rej_reason = item.get("rejectionReason") or item.get("rejreason") or None

        # Alice Blue's order book carries its own numeric instrument
        # identity directly (e.g. "74056") even for NFO options whose
        # tradingSymbol is reported as just the bare underlying name
        # (e.g. "NIFTY" - confirmed live: formattedInstrumentName
        # "NIFTY 29th SEP 23900 CE" alongside tradingSymbol "NIFTY").
        # Capturing it here lets callers skip the ambiguous
        # symbol-based NFO resolution entirely instead of re-guessing
        # which of many same-named contracts was actually traded.
        instrument_id_raw = item.get("instrumentId", item.get("Token", item.get("token")))
        instrument_id = str(instrument_id_raw).strip() if instrument_id_raw not in (None, "") else None
        # The exchange only when the row actually reported one: `exchange` below falls back to "NSE", which must
        # not be paired with a token as if it were evidence.
        instrument_exchange_raw = item.get("exchange", item.get("Exchange"))
        instrument_exchange = (
            str(instrument_exchange_raw).strip() if instrument_exchange_raw not in (None, "") else None
        ) or None

        orders.append(
            OrderRecord(
                broker_order_id=order_book_row_id(item),
                client_order_id=item.get("orderTag") or None,
                symbol=str(item.get("tradingSymbol", item.get("Trsym", item.get("symbol", "")))),
                exchange=str(item.get("exchange", item.get("Exchange", "NSE"))),
                side=side,
                quantity=int(item.get("quantity", item.get("Qty", 0))),
                filled_quantity=int(item.get("filledQuantity", item.get("Fillshares", 0))),
                average_price=Decimal(str(item.get("averageTradedPrice", item.get("Avgprc", "0")))),
                status=norm_status,
                order_timestamp=ts,
                order_type=norm_otype,
                product_type=str(item.get("product", item.get("pCode", "MIS"))),
                price=price_raw,
                trigger_price=trigger_price_raw,
                rejection_reason=rej_reason,
                instrument_id=instrument_id,
                instrument_exchange=instrument_exchange,
            )
        )

    return orders
