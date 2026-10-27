"""
Normalized Master order-book read: the explicit result contract on top of the existing client capability.

The R1 `MasterOrderSource` Protocol (brokers/interfaces.py) only says a client has `get_order_book()`. It cannot say whether an
empty list is a genuinely empty book or a read that failed, and the clients disagree on that:

* Alice Blue: `get_order_book()` returns `[]` on a network error or a non-200, so `[]` proves nothing. `get_order_book_raw()`
  returns None on any failure, so it is the only Alice read that can prove the book was read.
* Groww and Angel One: `get_order_book()` RAISES on a failed or untrustworthy read (no order_list array, a truncated page, an
  unrecognised status, a failed status flag), so `[]` from them is a successful read of an empty book.

`read_order_book` turns either convention into one explicit result. It is the same algorithm `MasterOrderPoller` used
inline before (its seed-pass "checked" read and its plain later reads), moved here unchanged so that other callers can share it;
it makes exactly the calls the poller made, in the same order, and nothing else. It decides nothing about eligibility: whether a
broker may be a Master is the registry's and the capability profile's decision (brokers/registry.py, brokers/profiles.py).

States
------
ORDERS   the book was read and has at least one row.
EMPTY    the book was read and is genuinely empty (only ever produced when that can be shown).
UNKNOWN  the state of the book cannot be established: no client, a checked read that did not succeed, a raw/parsed
         disagreement, or an Alice-convention plain read that returned [] (which a failure also returns).
FAILED   the read raised. `detail` names only the exception type: broker messages are never copied into it.

UNKNOWN and FAILED are never "no orders". A caller must treat both as "this read tells me nothing".

Three readers, one result type
------------------------------
`read_order_book(client, checked=...)`   the poller's read; parsed OrderRecords, up to two broker calls when `checked`.
`read_order_book_rows(client)`           the RAW rows of an Alice-convention client, verbatim, from ONE call
                                         (`get_order_book_raw`). The Master-switch preflight and crash recovery read this way:
                                         they need the broker's own order ids and statuses, not parsed records.
`read_order_book_single(client)`         the checked read from ONE response: the raw rows are fetched once and the same rows are
                                         parsed with the client's own `parse_order_book_rows`, so `rows` and `records` always describe
                                         the same response (the two-call checked read can see two different books). A client without
                                         the raw read or the parser, or a mock whose raw probe is not awaitable, takes the two-call path
                                         unchanged. The stale-order sweeper reads this way.
"""
import inspect
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Tuple

from app.services.alice_blue.types import OrderRecord


class OrderBookState(str, Enum):
    ORDERS = "ORDERS"
    EMPTY = "EMPTY"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


# reason codes (stable, machine-readable)
READ_OK = "READ_OK"
NO_CLIENT = "NO_CLIENT"
RAW_READ_UNAVAILABLE = "RAW_READ_UNAVAILABLE"                  # the checked raw read returned None: not read
RAW_AND_PARSED_DISAGREE = "RAW_AND_PARSED_DISAGREE"            # raw rows were seen but the parsed read found none
RAW_READ_NOT_SUPPORTED = "RAW_READ_NOT_SUPPORTED"              # the client has no raw read, so the book cannot be proven read
RAW_NOT_A_LIST = "RAW_NOT_A_LIST"                              # the raw read returned something that is not a list of rows
EMPTY_NOT_PROVEN = "EMPTY_NOT_PROVEN"                          # [] from a client whose plain read also returns [] on failure
READ_RAISED = "READ_RAISED"


@dataclass(frozen=True)
class OrderBookRead:
    state: OrderBookState
    records: Tuple[OrderRecord, ...] = ()
    reason: str = READ_OK
    detail: str = ""                                             # exception type name only; never a broker message
    checked: bool = False
    rows: Tuple[Any, ...] = ()                                   # the raw broker rows, verbatim, when the reader returns them

    @property
    def is_known(self) -> bool:
        """True only when the book was read: ORDERS or a proven EMPTY."""
        return self.state in (OrderBookState.ORDERS, OrderBookState.EMPTY)


def _known(records, checked: bool) -> OrderBookRead:
    records = tuple(records)
    return OrderBookRead(OrderBookState.ORDERS if records else OrderBookState.EMPTY, records, READ_OK, "", checked)


def _unknown(reason: str, checked: bool) -> OrderBookRead:
    return OrderBookRead(OrderBookState.UNKNOWN, (), reason, "", checked)


def hides_read_failures(client: Any) -> bool:
    """The Alice Blue convention: a client that offers `get_order_book_raw()` is one whose plain `get_order_book()` returns []
    on failure. This is the same capability test MasterOrderPoller has always used to decide whether to probe."""
    return callable(getattr(client, "get_order_book_raw", None))


async def read_order_book(client: Optional[Any], *, checked: bool) -> OrderBookRead:
    """
    One read of a Master's order book, as an explicit OrderBookRead. Never raises.

    `checked` is the poller's seed-pass read: it must be PROVEN, so a client offering `get_order_book_raw()` is asked that first
    (None -> UNKNOWN), and raw rows with an empty parsed book are a disagreement (UNKNOWN). A raw probe that is not awaitable is
    ignored, exactly as before. With `checked` False only `get_order_book()` is called; for an Alice-convention client a [] result
    is UNKNOWN (EMPTY_NOT_PROVEN) because a failed read returns [] too.
    """
    if client is None:
        return _unknown(NO_CLIENT, checked)
    try:
        raw: Any = None
        if checked:
            probe_fn = getattr(client, "get_order_book_raw", None)
            if callable(probe_fn):
                probe = probe_fn()
                if inspect.isawaitable(probe):
                    raw = await probe
                    if raw is None:
                        return _unknown(RAW_READ_UNAVAILABLE, checked)
        book = await client.get_order_book()
    except Exception as exc:
        return OrderBookRead(OrderBookState.FAILED, (), READ_RAISED, type(exc).__name__, checked)
    book = list(book or [])
    if checked and isinstance(raw, list) and raw and not book:
        return _unknown(RAW_AND_PARSED_DISAGREE, checked)
    if not book and not checked and hides_read_failures(client):
        return _unknown(EMPTY_NOT_PROVEN, checked)
    return _known(book, checked)


def _unknown_rows(reason: str) -> OrderBookRead:
    return OrderBookRead(OrderBookState.UNKNOWN, (), reason, "", True)


async def read_order_book_rows(client: Optional[Any]) -> OrderBookRead:
    """
    The raw order-book rows of an Alice-convention client from ONE `get_order_book_raw()` call. Never raises.

    ORDERS / EMPTY  the call returned a list (an empty list is the broker's proven empty book: the raw read returns None, not [],
                    for any failure). `rows` holds the list verbatim - rows are not filtered or validated here.
    UNKNOWN         NO_CLIENT; RAW_READ_NOT_SUPPORTED (no callable `get_order_book_raw`); RAW_READ_UNAVAILABLE (it returned None, or
                    its result is not awaitable); RAW_NOT_A_LIST (it returned something else).
    FAILED          the call raised (`detail` = exception type).
    """
    if client is None:
        return _unknown_rows(NO_CLIENT)
    fn = getattr(client, "get_order_book_raw", None)
    if not callable(fn):
        return _unknown_rows(RAW_READ_NOT_SUPPORTED)
    try:
        probe = fn()
        if not inspect.isawaitable(probe):
            return _unknown_rows(RAW_READ_UNAVAILABLE)
        raw = await probe
    except Exception as exc:
        return OrderBookRead(OrderBookState.FAILED, (), READ_RAISED, type(exc).__name__, True)
    if raw is None:
        return _unknown_rows(RAW_READ_UNAVAILABLE)
    if not isinstance(raw, list):
        return _unknown_rows(RAW_NOT_A_LIST)
    return OrderBookRead(OrderBookState.ORDERS if raw else OrderBookState.EMPTY, (), READ_OK, "", True, tuple(raw))


async def read_order_book_single(client: Optional[Any]) -> OrderBookRead:
    """
    The checked read from a single response (see the module docstring). Never raises.

    For a client with a callable `get_order_book_raw` AND `parse_order_book_rows`, one raw call is made and its rows are parsed (a
    parse error is FAILED, never a shortened book). Anything else - no client, no raw read, no parser, a raw probe that is not
    awaitable - is read exactly as `read_order_book(client, checked=True)` reads it.
    """
    if client is None:
        return _unknown(NO_CLIENT, True)
    if not (callable(getattr(client, "get_order_book_raw", None)) and callable(getattr(client, "parse_order_book_rows", None))):
        return await read_order_book(client, checked=True)
    fn = client.get_order_book_raw
    try:
        probe = fn()
    except Exception as exc:
        return OrderBookRead(OrderBookState.FAILED, (), READ_RAISED, type(exc).__name__, True)
    if not inspect.isawaitable(probe):
        return await read_order_book(client, checked=True)
    try:
        raw = await probe
    except Exception as exc:
        return OrderBookRead(OrderBookState.FAILED, (), READ_RAISED, type(exc).__name__, True)
    if raw is None:
        return _unknown_rows(RAW_READ_UNAVAILABLE)
    if not isinstance(raw, list):
        return _unknown_rows(RAW_NOT_A_LIST)
    if not raw:
        return OrderBookRead(OrderBookState.EMPTY, (), READ_OK, "", True, ())
    try:
        records = tuple(client.parse_order_book_rows(raw))
    except Exception as exc:
        return OrderBookRead(OrderBookState.FAILED, (), READ_RAISED, type(exc).__name__, True)
    return OrderBookRead(OrderBookState.ORDERS, records, READ_OK, "", True, tuple(raw))
