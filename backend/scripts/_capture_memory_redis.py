"""
Throw-away, in-process stand-in for Redis, used ONLY by the supervised capture scripts
(groww_capture.py, angel_capture.py). Nothing is persisted and nothing is shared with the
production Redis or its login protection - the real login guard (brokers/login_guard.py) runs
unchanged on top of this store, so a capture login gets the same lock / spacing / cap rules.

It implements exactly what the guard and the clients use: get, set (ex / px / nx / xx), delete,
exists, incr, expire, ttl - with real expiry and Redis' return values (SET NX -> True or None,
TTL -> -2 missing / -1 no expiry / seconds left).

Atomicity: every method runs to completion without awaiting, so on a single asyncio loop two
concurrent callers can never interleave inside one operation (SET NX has exactly one winner).
"""
import math
import time
from typing import Any, Callable, Dict, Optional


class MemoryRedis:
    def __init__(self, monotonic: Callable[[], float] = time.monotonic):
        self._now = monotonic
        self._values: Dict[str, Any] = {}
        self._expires_at: Dict[str, float] = {}

    # ── internals ──────────────────────────────────────────────────────
    def _alive(self, key: str) -> bool:
        deadline = self._expires_at.get(key)
        if deadline is not None and self._now() >= deadline:
            self._values.pop(key, None)
            self._expires_at.pop(key, None)
            return False
        return key in self._values

    # ── commands ───────────────────────────────────────────────────────
    async def get(self, key: str) -> Any:
        return self._values[key] if self._alive(key) else None

    async def set(self, key: str, value: Any, ex: Optional[float] = None, px: Optional[float] = None,
                  nx: bool = False, xx: bool = False) -> Optional[bool]:
        exists = self._alive(key)
        if (nx and exists) or (xx and not exists):
            return None
        self._values[key] = value
        self._expires_at.pop(key, None)                       # SET clears any previous expiry
        seconds = ex if ex is not None else (px / 1000.0 if px is not None else None)
        if seconds is not None:
            if seconds <= 0:
                raise ValueError("invalid expire time in 'set' command")
            self._expires_at[key] = self._now() + seconds
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self._alive(key):
                removed += 1
            self._values.pop(key, None)
            self._expires_at.pop(key, None)
        return removed

    async def exists(self, *keys: str) -> int:
        return sum(1 for key in keys if self._alive(key))

    async def incr(self, key: str) -> int:
        current = 0
        if self._alive(key):
            try:
                current = int(self._values[key])
            except (TypeError, ValueError):
                raise ValueError("value is not an integer or out of range") from None
        current += 1
        self._values[key] = str(current)                      # keeps any existing expiry, like Redis
        return current

    async def expire(self, key: str, seconds: float) -> bool:
        if not self._alive(key):
            return False
        if seconds <= 0:
            await self.delete(key)
            return True
        self._expires_at[key] = self._now() + seconds
        return True

    async def ttl(self, key: str) -> int:
        if not self._alive(key):
            return -2
        deadline = self._expires_at.get(key)
        if deadline is None:
            return -1
        return max(0, math.ceil(deadline - self._now()))
