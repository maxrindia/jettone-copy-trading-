"""
Groww response contract
=======================
Everything Jettone relies on from Groww's API, with its documentation source.
Values come ONLY from Groww's official Trading API documentation
(groww.in/trade-api/docs: cURL orders / portfolio / margin / annexures /
instruments pages and the Python SDK pages), read on 2026-10-16.

Documented and used:
  - base URL https://api.groww.in/v1 and the headers Accept, Authorization:
    Bearer <token>, X-API-VERSION: 1.0
  - order create / modify / cancel / status / list / detail paths and fields
  - positions (GET /v1/positions/user) and margin (GET /v1/margins/detail/user)
    paths and fields
  - the order-status, exchange, segment, product, order-type, transaction-type
    and validity vocabularies (Annexures page)
  - token endpoint POST /v1/token/api/access, key_type "approval", checksum =
    SHA-256(secret + timestamp), response fields token/expiry/isActive
  - rate limits: auth 5/s 30/min (+150/day), orders 10/s 250/min,
    non-trading reads 20/s 500/min
  - instrument master CSV columns

LIVE-VERIFIED (read-only capture against a real account through the production proxy, 2026-10-29; shapes only, no
values recorded; see LIVE_VERIFIED_SHAPES and docs/groww-verification.md):
  - GET /v1/order/list?segment=CASH&page_size=100 -> HTTP 200, {"status": "SUCCESS", "payload": {"order_list": [...]}};
    an account with no orders answers order_list = []
  - GET /v1/positions/user?segment=CASH -> HTTP 200, {"status": "SUCCESS", "payload": {"positions": [...]}}; a flat account
    answers positions = []
  - GET /v1/margins/detail/user -> HTTP 200, status SUCCESS; payload carries the JSON numbers in MARGIN_FUNDS_FIELDS and
    MARGIN_OTHER_NUMERIC_FIELDS and the objects in MARGIN_OBJECT_FIELDS
  Not seen (the account was empty): any order or position ROW, the order_status vocabulary, a full page.

Documented as unclear or absent - Jettone fails closed on each (see client.py):
  - the FAILURE response body and error codes (only status "SUCCESS"/"FAILURE")
  - the TOTP-flow token response (so only the approval flow is implemented)
  - any response shape other than the live-verified ones above (an unexpected shape is "unknown", never "flat")
  - the page base of order-list paging (a full first page is treated as truncated)
  - the quantity semantics of a modify on a part-filled order (refused)
  - whether static IP is enforced on reads (Groww's own blog says order placement
    needs a whitelisted static IP; Jettone requires a recorded, unexpired one
    before every create / modify / cancel)
  - the exact meaning of EXECUTED / COMPLETED / DELIVERY_AWAITED (names only):
    mapped to a fill state only when filled_quantity agrees; otherwise refused
  - the timezone of timestamps and token expiry (the earlier reading is used)

`live_verified` stays False: the 2026-10-29 capture confirmed the empty-account shapes only, not order or position rows,
statuses, paging, error bodies or any order operation.
"""

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Tuple

BASE_URL = "https://api.groww.in"
API_VERSION = "1.0"

ROUTES = {
    "token": "/v1/token/api/access",
    "order_create": "/v1/order/create",
    "order_modify": "/v1/order/modify",
    "order_cancel": "/v1/order/cancel",
    "order_list": "/v1/order/list",
    "order_detail": "/v1/order/detail/{groww_order_id}",
    "order_status_ref": "/v1/order/status/reference/{order_reference_id}",
    "positions": "/v1/positions/user",
    "margin": "/v1/margins/detail/user",
}

# Annexures: Order Status. Interpretation notes are in client.normalize_status.
ORDER_STATUSES: FrozenSet[str] = frozenset({
    "NEW", "ACKED", "TRIGGER_PENDING", "APPROVED", "REJECTED", "FAILED", "EXECUTED",
    "DELIVERY_AWAITED", "CANCELLED", "CANCELLATION_REQUESTED", "MODIFICATION_REQUESTED", "COMPLETED",
})
# Orders page example value for a freshly placed order; not in the Annexures list.
EXTRA_DOCUMENTED_STATUSES: FrozenSet[str] = frozenset({"OPEN"})

EXCHANGES: FrozenSet[str] = frozenset({"NSE", "BSE"})
SEGMENTS: FrozenSet[str] = frozenset({"CASH", "FNO"})
PRODUCTS: FrozenSet[str] = frozenset({"CNC", "MIS", "NRML"})
ORDER_TYPES: FrozenSet[str] = frozenset({"LIMIT", "MARKET", "SL", "SL_M"})
TRANSACTION_TYPES: FrozenSet[str] = frozenset({"BUY", "SELL"})
VALIDITY_DAY = "DAY"
SUPPORTED_SEGMENT = "CASH"          # equity only; F&O needs lot-size validation that is not built

# Documented rate limits (requests, window seconds).
LIMIT_ORDERS = ((10, 1), (250, 60))
LIMIT_READS = ((20, 1), (500, 60))
LIMIT_TOKEN = ((5, 1), (30, 60))
ORDER_LIST_PAGE_SIZE = 100          # documented maximum

ORDER_REFERENCE_MIN, ORDER_REFERENCE_MAX = 8, 20   # alphanumeric, at most two hyphens

# Live capture, 2026-10-29 (read-only, empty account). Field NAMES and JSON types only - never a value.
LIVE_CAPTURE_DATE = "2026-10-29"
MARGIN_FUNDS_FIELDS: Tuple[str, ...] = ("clear_cash", "collateral_available", "net_margin_used")   # read by get_funds
MARGIN_OTHER_NUMERIC_FIELDS: Tuple[str, ...] = ("adhoc_margin", "brokerage_and_charges", "collateral_used")
MARGIN_OBJECT_FIELDS: Tuple[str, ...] = ("commodity_margin_details", "equity_margin_details", "fno_margin_details")
LIVE_VERIFIED_SHAPES: Dict[str, str] = {
    "order_list": "HTTP 200, status SUCCESS, payload.order_list is a list; [] when the account has no orders",
    "positions": "HTTP 200, status SUCCESS, payload.positions is a list; [] when the account is flat",
    "margin": "HTTP 200, status SUCCESS, payload holds MARGIN_FUNDS_FIELDS and MARGIN_OTHER_NUMERIC_FIELDS as JSON numbers "
              "and MARGIN_OBJECT_FIELDS as objects",
}


@dataclass(frozen=True)
class GrowwContract:
    docs_verified: bool = True        # every field above is from the official documentation
    live_verified: bool = False       # confirmed against a real account's responses
    # Master order detection needs facts nobody has confirmed: the order-list page base, stable
    # groww_order_id across polls and the order_status values of a part-filled order. Set True only
    # after the owner's capture has been reviewed.
    master_verified: bool = False
    notes: Dict[str, str] = field(default_factory=dict)


GROWW_CONTRACT = GrowwContract()


def contract_is_usable(contract: GrowwContract) -> bool:
    """Child-ready: the documented contract is complete. (Live verification is a
    separate, supervised step reported to the owner; it does not silently gate.)"""
    return contract.docs_verified
