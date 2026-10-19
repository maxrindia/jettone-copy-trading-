"""
Transport-error classification for order creation
=================================================
The copy pipeline retries ONE result category, NETWORK_ERROR, and also retries any
exception raised by place_order (reliability/retry.py). Re-sending a create is only
safe when the failure PROVES the request never left this process. Everything else -
a read or write failure, a protocol error, a reset connection, a 5xx, a response that
cannot be understood - may have followed a request the broker already accepted, so
it is an ambiguous outcome that must never be retried (the adapters report it as
TIMEOUT, which the stale-order sweeper reconciles against the broker's order book).

The allow-list is intentionally short: anything not named here is treated as ambiguous.
"""

import httpx

# Raised before any bytes of the request are written.
_PROVABLY_NOT_SENT = (
    httpx.ConnectError,         # TCP connect or TLS handshake failed
    httpx.ConnectTimeout,       # timed out while connecting
    httpx.PoolTimeout,          # no connection became available
    httpx.ProxyError,           # the proxy refused the CONNECT tunnel
    httpx.UnsupportedProtocol,  # bad scheme: nothing was attempted
    httpx.LocalProtocolError,   # this side built an invalid request: not transmitted
)


def is_provably_not_sent(exc: BaseException) -> bool:
    """True only for errors that prove the request was never transmitted."""
    return isinstance(exc, _PROVABLY_NOT_SENT)


class SendState:
    """Set by an adapter's transport call immediately before the request is issued, so a
    failure can be classed as 'before send' (safe to reject) or 'after send' (ambiguous)."""

    __slots__ = ("sent",)

    def __init__(self) -> None:
        self.sent = False
