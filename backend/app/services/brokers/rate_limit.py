"""
Client-side sliding-window rate limiting for broker adapters.

No limit is assumed here. The windows come from configuration (e.g. ANGEL_ONE_ORDER_RATE_LIMITS="10/1,200/60"),
which the owner sets only from the broker's documented figures. Empty configuration means "no limiting".
A malformed value is an error the caller must treat as fail-closed - it is never silently ignored.

The limiter only ever delays or refuses a request BEFORE it is sent. It has no knowledge of requests already on
the wire, so it can never cause (or hide) an ambiguous order outcome and must never be used to retry one.
"""
import asyncio
import time
from typing import Awaitable, Callable, List, Optional, Sequence, Tuple

Window = Tuple[int, float]      # (max requests, per seconds)


class RateLimitConfigError(ValueError):
    pass


class RateLimitExceeded(Exception):
    """The request was NOT sent: no slot became available within the allowed wait."""


def parse_rate_limits(text: Optional[str]) -> Tuple[Window, ...]:
    """"10/1,200/60" -> ((10, 1.0), (200, 60.0)). Empty/blank -> (). Anything malformed raises."""
    raw = (text or "").strip()
    if not raw:
        return ()
    windows: List[Window] = []
    for part in raw.split(","):
        pieces = part.strip().split("/")
        if len(pieces) != 2:
            raise RateLimitConfigError("each rate limit must look like requests/seconds")
        try:
            limit, seconds = int(pieces[0]), float(pieces[1])
        except ValueError:
            raise RateLimitConfigError("rate limit numbers must be numeric") from None
        if limit <= 0 or not (0 < seconds < 86400) or seconds != seconds:
            raise RateLimitConfigError("rate limit values must be positive")
        windows.append((limit, seconds))
    return tuple(windows)


class SlidingWindowLimiter:
    """Waits up to max_wait seconds for a slot, otherwise raises RateLimitExceeded (the caller fails closed)."""

    def __init__(
        self,
        windows: Sequence[Window],
        max_wait: float = 2.0,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self._windows = tuple(windows)
        self._max_wait = max_wait
        self._now = now
        self._sleep = sleep
        self._stamps: List[float] = []

    @property
    def enabled(self) -> bool:
        return bool(self._windows)

    async def acquire(self) -> None:
        if not self._windows:
            return
        waited = 0.0
        while True:
            now = self._now()
            horizon = max(w for _, w in self._windows)
            self._stamps = [t for t in self._stamps if now - t < horizon]
            wait = 0.0
            for limit, window in self._windows:
                recent = [t for t in self._stamps if now - t < window]
                if len(recent) >= limit:
                    wait = max(wait, window - (now - recent[0]))
            if wait <= 0:
                self._stamps.append(now)
                return
            if waited + wait > self._max_wait:
                raise RateLimitExceeded("client-side rate limit reached; request not sent")
            await self._sleep(wait)
            waited += wait
