"""
Zerodha order-update messages (WebSocket text frames and HTTP postbacks) - pure parsing, no sockets.
====================================================================================================
Documented (kite.trade/docs/connect/v3/websocket and /postbacks): an order update is the JSON object {"type": "order", "data": {...}},
where `data` has the same fields as a row of GET /orders; a postback is that `data` object POSTed as JSON and authenticated by
`checksum` = SHA-256(order_id + order_timestamp + api_secret).

Not documented, so not assumed: whether updates for orders placed OUTSIDE the API key (Kite web / mobile) are delivered - see
contract.py. Nothing here opens a connection; the polling order-book path stays the source of truth, and an update is only ever a
hint to re-read it.
"""
import hashlib
import hmac
from typing import Any, Dict, Optional

from app.services.alice_blue.types import OrderRecord


class ZerodhaUpdateError(ValueError):
    """The message is malformed or fails authentication. Messages never contain the secret."""


def postback_checksum(order_id: str, order_timestamp: str, api_secret: str) -> str:
    return hashlib.sha256(f"{order_id}{order_timestamp}{api_secret}".encode("utf-8")).hexdigest()


def verify_postback(payload: Any, api_secret: str) -> Dict[str, Any]:
    """The order object of an authentic postback. Constant-time comparison; a missing field or a mismatch raises."""
    if not isinstance(payload, dict):
        raise ZerodhaUpdateError("postback is not an object")
    order_id, stamp, checksum = payload.get("order_id"), payload.get("order_timestamp"), payload.get("checksum")
    if not all(isinstance(v, str) and v for v in (order_id, stamp, checksum)):
        raise ZerodhaUpdateError("postback is missing order_id, order_timestamp or checksum")
    if not hmac.compare_digest(postback_checksum(order_id, stamp, api_secret), checksum):
        raise ZerodhaUpdateError("postback checksum mismatch")
    return {k: v for k, v in payload.items() if k != "checksum"}


def order_from_stream_message(message: Any) -> Optional[Dict[str, Any]]:
    """The `data` object of a {"type": "order"} frame, or None for any other frame type (binary ticks, heartbeats, errors)."""
    if not isinstance(message, dict) or message.get("type") != "order":
        return None
    data = message.get("data")
    if not isinstance(data, dict):
        raise ZerodhaUpdateError("order frame has no data object")
    return data


def record_from_update(client: Any, data: Dict[str, Any]) -> OrderRecord:
    """Parse an update with the same strict row parser as the order book, so a stream hint can never be looser than a poll."""
    return client.parse_order_row(data)
