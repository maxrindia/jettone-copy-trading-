"""
Zerodha (Kite Connect v3) response contract
===========================================
Everything Jettone relies on from Zerodha's Kite Connect API, with its documentation source. Values come ONLY from Zerodha's official
Kite Connect v3 documentation (kite.trade/docs/connect/v3/: introduction, user, orders, portfolio, market-quotes (instruments),
postbacks, websocket and exceptions pages), read on 2026-10-27. Nothing here was observed against a real account.

Documented and used:
  - base URL https://api.kite.trade; headers `X-Kite-Version: 3` and `Authorization: token api_key:access_token`;
    "All inputs are form-encoded parameters and responses are JSON" (introduction page)
  - login: https://kite.zerodha.com/connect/login?v=3&api_key=...; the redirect carries a short-lived `request_token`, exchanged at
    POST /session/token with checksum = SHA-256(api_key + request_token + api_secret); the `access_token` expires at 6 AM the next
    day unless invalidated earlier (DELETE /session/token, or a master logout from the Kite web terminal)
  - orders: POST /orders/:variety, PUT /orders/:variety/:order_id, DELETE /orders/:variety/:order_id, GET /orders,
    GET /orders/:order_id (history: a list of states), GET /trades; place / modify / cancel return only `data.order_id`
  - vocabularies: variety {regular, amo, co, iceberg, auction}; order_type {MARKET, LIMIT, SL, SL-M}; product {CNC, NRML, MIS, MTF};
    validity {DAY, IOC, TTL}; exchange {NSE, BSE, NFO, CDS, BCD, MCX}; transaction_type {BUY, SELL}
  - `tag`: "alphanumeric, max 20 chars", returned in GET /orders in the `tag` field
  - statuses: OPEN, COMPLETE, CANCELLED, REJECTED plus the interim PUT ORDER REQ RECEIVED, VALIDATION PENDING, OPEN PENDING,
    MODIFY VALIDATION PENDING, MODIFY PENDING, TRIGGER PENDING, CANCEL PENDING, AMO REQ RECEIVED ("other values may also occur")
  - order object fields (order_id, exchange_order_id, parent_order_id, status, status_message, order_timestamp, exchange_timestamp,
    filled_quantity, pending_quantity, cancelled_quantity, quantity, disclosed_quantity, average_price, price, trigger_price, tag,
    guid, placed_by, variety, tradingsymbol, exchange, instrument_token, transaction_type, order_type, product, validity, meta)
  - positions: GET /portfolio/positions -> data.net / data.day; funds: GET /user/margins/equity (available.live_balance,
    available.collateral, utilised.debits, net)
  - errors: {"status": "error", "message", "error_type"} with TokenException (403), UserException, OrderException, InputException,
    MarginException, HoldingException, NetworkException, DataException, GeneralException; HTTP 400/403/404/405/410/429/500/502/503/504
  - rate limits: order placement 10 req/s, other endpoints 10 req/s, quote 1 req/s; 400 orders/min; at most 5000 orders/day per
    API key; at most 25 modifications per order
  - instruments: GET /instruments/:exchange is a gzipped CSV regenerated once a day with columns instrument_token, exchange_token,
    tradingsymbol, name, last_price, expiry, strike, tick_size, lot_size, instrument_type (EQ, FUT, CE, PE), segment, exchange.
    Zerodha advises (exchange, tradingsymbol) - NOT the numeric token - as the unique key, because "exchanges may reuse
    instrument tokens for different derivative instruments after each expiry"
  - order updates: WebSocket (wss://ws.kite.trade?api_key&access_token) text messages {"type": "order", "data": {...}} and HTTP
    postbacks, authenticated by checksum = SHA-256(order_id + order_timestamp + api_secret)

Documented as unclear or absent - Jettone fails closed on each (see client.py):
  - whether GET /orders (and the WebSocket / postbacks) include orders placed OUTSIDE the API key (Kite web / mobile): the official
    pages are silent; only forum posts say the WebSocket covers all orders and postbacks only API-placed ones. MASTER DETECTION is
    therefore NOT verified: `master_verified` stays False until a supervised capture shows an externally placed order in the book
  - a partial-fill status: none is defined (fill progress is `filled_quantity` / `pending_quantity`), so PARTIAL is derived and a row
    that cannot be read consistently is refused
  - whether `quantity` on a modify is the total or the remaining size, and modification of a part-filled order: "not stated", so a
    part-filled order is never modified (same rule as Groww)
  - `average_price`: "only for COMPLETE orders", so it is not required for a partial fill and a missing one reads as zero
  - the timezone of timestamps (no zone on the page): order_timestamp is read as IST, the exchange's own clock
  - whether a static IP must be whitelisted for the order endpoints: the pages read do not say, so none is invented here
  - any guidance for a timeout or network error on order placement: none, so such an outcome is AMBIGUOUS and is never resent
  - how a placement made by TTL / IOC / iceberg / auction / AMO / cover varieties behaves for copying: only `regular` + `DAY` is
    placed, and a Master order of any other variety or validity is not copied (it is surfaced as an unsupported status)
  - the per-segment product rules (e.g. NRML for equity cash): not listed on the pages read, so an unsupported combination is refused
    here instead of being sent

`live_verified` and `master_verified` stay False until a supervised capture has been reviewed against a real account.
"""
from dataclasses import dataclass
from typing import Dict, FrozenSet, Tuple

BASE_URL = "https://api.kite.trade"
LOGIN_URL = "https://kite.zerodha.com/connect/login"
API_VERSION = "3"
WEBSOCKET_URL = "wss://ws.kite.trade"

ROUTES = {
    "session": "/session/token",
    "profile": "/user/profile",
    "margins": "/user/margins/equity",
    "orders": "/orders",
    "order_place": "/orders/{variety}",
    "order_modify": "/orders/{variety}/{order_id}",
    "order_cancel": "/orders/{variety}/{order_id}",
    "order_history": "/orders/{order_id}",
    "positions": "/portfolio/positions",
    "instruments": "/instruments/{exchange}",
}

VARIETIES: FrozenSet[str] = frozenset({"regular", "amo", "co", "iceberg", "auction"})
PLACED_VARIETY = "regular"                          # the only variety Jettone places
ORDER_TYPES: FrozenSet[str] = frozenset({"MARKET", "LIMIT", "SL", "SL-M"})
PRODUCTS: FrozenSet[str] = frozenset({"CNC", "NRML", "MIS", "MTF"})
VALIDITIES: FrozenSet[str] = frozenset({"DAY", "IOC", "TTL"})
PLACED_VALIDITY = "DAY"                             # the only validity Jettone places
EXCHANGES: FrozenSet[str] = frozenset({"NSE", "BSE", "NFO", "CDS", "BCD", "MCX"})
SUPPORTED_EXCHANGES: FrozenSet[str] = frozenset({"NSE", "BSE", "NFO"})      # what Jettone trades (equity cash and equity F&O)
EQUITY_EXCHANGES: FrozenSet[str] = frozenset({"NSE", "BSE"})
DERIVATIVE_EXCHANGES: FrozenSet[str] = frozenset({"NFO"})
TRANSACTION_TYPES: FrozenSet[str] = frozenset({"BUY", "SELL"})
MARKET_PROTECTION_AUTOMATIC = "-1"                  # documented: "greater than 0 and up to 100, or -1" (automatic)
TAG_MAX_LENGTH = 20                                 # documented: "alphanumeric, max 20 chars"

# Status vocabulary. COMPLETE / OPEN / CANCELLED / REJECTED are the documented terminal and working states.
TERMINAL_STATUSES: FrozenSet[str] = frozenset({"COMPLETE", "CANCELLED", "REJECTED"})
INTERIM_STATUSES: FrozenSet[str] = frozenset({
    "PUT ORDER REQ RECEIVED", "VALIDATION PENDING", "OPEN PENDING", "MODIFY VALIDATION PENDING", "MODIFY PENDING",
    "CANCEL PENDING", "AMO REQ RECEIVED", "MODIFIED",
})
MODIFIABLE_STATUSES: FrozenSet[str] = frozenset({"OPEN", "TRIGGER PENDING"})

# Rate limits as (requests, seconds): documented figures only.
LIMIT_ORDERS: Tuple[Tuple[int, float], ...] = ((10, 1.0), (400, 60.0))
LIMIT_READS: Tuple[Tuple[int, float], ...] = ((10, 1.0),)
LIMIT_SESSION: Tuple[Tuple[int, float], ...] = ((10, 1.0),)
MAX_ORDERS_PER_DAY = 5000
MAX_MODIFICATIONS_PER_ORDER = 25

# error_type -> what it proves about an order write. Only a 4xx proves the OMS did not take the order.
DEFINITIVE_REJECTION_TYPES: FrozenSet[str] = frozenset({
    "InputException", "OrderException", "MarginException", "HoldingException", "UserException",
})
SESSION_ERROR_TYPES: FrozenSet[str] = frozenset({"TokenException"})
AMBIGUOUS_ERROR_TYPES: FrozenSet[str] = frozenset({"NetworkException", "DataException", "GeneralException"})


@dataclass(frozen=True)
class ZerodhaContract:
    docs_verified: bool = True            # implemented against the official pages listed above
    live_verified: bool = False           # nothing has been observed against a real account
    master_verified: bool = False         # whether externally placed orders appear in the order book is NOT documented
    status_map: Dict[str, str] = None      # type: ignore[assignment]

    def __post_init__(self):
        if self.status_map is None:
            object.__setattr__(self, "status_map", dict(STATUS_MAP))


# Documented status -> the engine's common vocabulary (client.normalize_status adds PARTIAL and TRIGGER_PENDING).
STATUS_MAP: Dict[str, str] = {
    "OPEN": "OPEN",
    "COMPLETE": "COMPLETE",
    "CANCELLED": "CANCELLED",
    "REJECTED": "REJECTED",
    "TRIGGER PENDING": "TRIGGER_PENDING",
}

ZERODHA_CONTRACT = ZerodhaContract()


def contract_is_usable(contract: ZerodhaContract) -> bool:
    """A read adapter may be used only when the contract was built from the documentation and its status map is non-empty."""
    return bool(contract.docs_verified and contract.status_map)
