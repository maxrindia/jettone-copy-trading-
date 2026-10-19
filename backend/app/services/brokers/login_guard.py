"""
Cross-instance login protection
===============================
An explicit broker login contacts the broker (Groww's token endpoint allows 30/min and 150/day; a repeated
Angel One TOTP can lock the account). POST /accounts/{id}/broker-login builds a NEW client for every call, so
per-instance locks cannot stop two requests - or two browser clicks - from each sending a token request.
This guard lives in Redis, keyed by broker + account, so it holds across client instances, concurrent API
requests, and process restarts:

  lock    one login in flight per account (SET NX EX 30). A concurrent caller waits for it and shares its
          result; it never sends a second request.
  spacing a login attempt - successful or not - blocks another attempt for 10 s. A caller arriving within
          that window right after a SUCCESSFUL login (session still present) is simply told it is done.
  daily   an optional per-account cap on attempts per IST day (Groww: 120, under its documented 150).

It fails closed: if Redis cannot be reached nothing is sent. Messages are fixed text; no secret, key name
or exception detail appears in them.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("login_guard")

LOCK_TTL_SECONDS = 30
MIN_SPACING_SECONDS = 10
JOIN_WAIT_SECONDS = 15.0
POLL_INTERVAL_SECONDS = 0.05
COUNT_TTL_SECONDS = 36 * 3600
GENERATION_TTL_SECONDS = 24 * 3600
IST = timezone(timedelta(hours=5, minutes=30))

IN_PROGRESS = "LOGIN_IN_PROGRESS"
TOO_SOON = "LOGIN_TOO_SOON"
DAILY_CAP = "LOGIN_DAILY_CAP"
GUARD_UNAVAILABLE = "LOGIN_GUARD_UNAVAILABLE"


class LoginThrottled(Exception):
    """The login was not attempted (or not repeated). `retry_after` is in seconds."""

    def __init__(self, code: str, retry_after: int, message: str):
        super().__init__(message)
        self.code = code
        self.retry_after = max(1, int(retry_after))
        self.message = message


def _keys(broker: str, account_id: str, ist_date: str) -> dict:
    base = f"login:{{}}:{broker}:{account_id}"
    return {
        "lock": base.format("lock"), "last": base.format("last"), "recent": base.format("recent"),
        "gen": base.format("gen"), "count": f"login:count:{broker}:{account_id}:{ist_date}",
    }


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


def _seconds_to_ist_midnight(now: datetime) -> int:
    local = now.astimezone(IST)
    midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((midnight - local).total_seconds())


async def _retry_after(redis: Any, key: str, default: int) -> int:
    try:
        ttl = await redis.ttl(key)
        return int(ttl) if isinstance(ttl, (int, float)) and ttl > 0 else default
    except Exception:
        return default


async def run_guarded_login(
    *,
    redis: Any,
    broker: str,
    account_id: str,
    clock: Callable[[], datetime],
    do_login: Callable[[], Awaitable[None]],
    session_key: str,
    daily_cap: Optional[int] = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Run `do_login` at most once per spacing window per account across the whole system, or raise
    LoginThrottled. Exceptions from do_login propagate unchanged (after the lock is released)."""
    if redis is None:
        raise LoginThrottled(GUARD_UNAVAILABLE, 5, "Login protection is unavailable; no login was attempted.")
    now = clock()
    k = _keys(broker, str(account_id), now.astimezone(IST).date().isoformat())
    token = uuid.uuid4().hex
    try:
        generation_before = int(_text(await redis.get(k["gen"])) or 0)
        acquired = await redis.set(k["lock"], token, ex=LOCK_TTL_SECONDS, nx=True)
    except Exception:
        raise LoginThrottled(GUARD_UNAVAILABLE, 5, "Login protection is unavailable; no login was attempted.") from None

    if not acquired:
        await _join_in_flight(redis, k, generation_before, session_key, sleep)
        return

    try:
        try:
            # A login that just succeeded (and whose session is still there) already satisfies this request.
            if await redis.get(k["recent"]) and await redis.exists(session_key):
                return
            if await redis.get(k["last"]):
                raise LoginThrottled(
                    TOO_SOON, await _retry_after(redis, k["last"], MIN_SPACING_SECONDS),
                    "A login was attempted for this account moments ago. Try again shortly.",
                )
            if daily_cap is not None:
                attempts = await redis.incr(k["count"])
                if attempts == 1:
                    await redis.expire(k["count"], COUNT_TTL_SECONDS)
                if attempts > daily_cap:
                    raise LoginThrottled(
                        DAILY_CAP, _seconds_to_ist_midnight(now),
                        "The daily login limit for this account has been reached. Try again tomorrow.",
                    )
            # Claim the spacing window BEFORE contacting the broker, so a crash or a timeout still counts.
            await redis.set(k["last"], "1", ex=MIN_SPACING_SECONDS)
        except LoginThrottled:
            raise
        except Exception:
            raise LoginThrottled(GUARD_UNAVAILABLE, 5, "Login protection is unavailable; no login was attempted.") from None

        await do_login()

        try:   # best effort: the login itself already succeeded
            await redis.incr(k["gen"])
            await redis.expire(k["gen"], GENERATION_TTL_SECONDS)
            await redis.set(k["recent"], "1", ex=MIN_SPACING_SECONDS)
        except Exception:
            logger.warning(f"[{account_id}] login succeeded but its completion marker could not be written")
    finally:
        await _release(redis, k["lock"], token)


async def _join_in_flight(redis: Any, k: dict, generation_before: int, session_key: str, sleep) -> None:
    """Another request holds the lock: wait for it and share its result. Never sends a login of our own."""
    waited = 0.0
    while waited < JOIN_WAIT_SECONDS:
        await sleep(POLL_INTERVAL_SECONDS)
        waited += POLL_INTERVAL_SECONDS
        try:
            generation = int(_text(await redis.get(k["gen"])) or 0)
            if generation > generation_before and await redis.exists(session_key):
                return                                    # the in-flight login succeeded: its session is ours too
            if not await redis.get(k["lock"]):
                break                                     # the holder finished (or failed) without a new session
        except Exception:
            raise LoginThrottled(GUARD_UNAVAILABLE, 5, "Login protection is unavailable; no login was attempted.") from None
    else:
        raise LoginThrottled(IN_PROGRESS, LOCK_TTL_SECONDS, "A login for this account is already in progress.")
    try:
        generation = int(_text(await redis.get(k["gen"])) or 0)
        if generation > generation_before and await redis.exists(session_key):
            return
    except Exception:
        raise LoginThrottled(GUARD_UNAVAILABLE, 5, "Login protection is unavailable; no login was attempted.") from None
    raise LoginThrottled(
        TOO_SOON, await _retry_after(redis, k["last"], MIN_SPACING_SECONDS),
        "The login that was in progress did not succeed. Try again shortly.",
    )


async def _release(redis: Any, lock_key: str, token: str) -> None:
    """Delete the lock only if it is still ours (an expired lock may now belong to someone else)."""
    try:
        if _text(await redis.get(lock_key)) == token:
            await redis.delete(lock_key)
    except Exception:
        pass
