import logging
from typing import Optional, Set
import redis.asyncio as aioredis

from app.services.copy_engine.events import MasterTradeEvent

logger = logging.getLogger("event_validator")


class ValidationResult:
    def __init__(self, is_valid: bool, reason_code: Optional[str] = None):
        self.is_valid = is_valid
        self.reason_code = reason_code


class EventValidator:
    """
    Validates incoming MasterTradeEvents before copy calculation:
    - Symbol allowed?
    - Event seen before in Redis (first-line idempotency check)?
    - Master session / state healthy?
    - Global emergency stop disarmed?
    """

    DEDUP_TTL_SECONDS = 24 * 3600  # 24 hours

    def __init__(self, redis: aioredis.Redis, allowed_symbols: Optional[Set[str]] = None):
        self.redis = redis
        self.allowed_symbols = allowed_symbols  # None means all symbols allowed

    async def validate(
        self,
        event: MasterTradeEvent,
        is_emergency_stopped: bool = False,
        is_master_healthy: bool = True,
        check_duplicate: bool = True,
    ) -> ValidationResult:
        # Check 1: Global Emergency Stop
        if is_emergency_stopped:
            logger.warning(f"[Validator] Rejected event {event.master_order_id + '#' + str(event.fill_sequence)}: EMERGENCY_STOP_ACTIVE")
            return ValidationResult(False, "EMERGENCY_STOP_ACTIVE")

        # Check 2: Master Account Health
        if not is_master_healthy:
            logger.warning(f"[Validator] Rejected event {event.master_order_id + '#' + str(event.fill_sequence)}: MASTER_UNHEALTHY")
            return ValidationResult(False, "MASTER_UNHEALTHY")

        # Check 3: Symbol Allowlist
        if self.allowed_symbols is not None and event.symbol not in self.allowed_symbols:
            logger.warning(f"[Validator] Rejected event {event.master_order_id + '#' + str(event.fill_sequence)}: SYMBOL_NOT_ALLOWED ({event.symbol})")
            return ValidationResult(False, "SYMBOL_NOT_ALLOWED")

        # Check 4: Redis First-Line Deduplication Check. `check_duplicate=False` is used ONLY by a deliberate restart
        # recovery (R3b), whose Children are still stopped by the database claim; every other check above still ran.
        if not check_duplicate:
            return ValidationResult(True)
        # The scope is "" for an Alice Blue Master (key unchanged); other brokers' native ids are only unique per broker/account.
        seen_key = f"copy:seen:{event.dedupe_scope}{event.master_order_id}:{event.fill_sequence}"
        was_set = await self.redis.set(seen_key, "1", ex=self.DEDUP_TTL_SECONDS, nx=True)
        if not was_set:
            logger.warning(f"[Validator] Duplicate event detected for {event.master_order_id} (seq {event.fill_sequence})")
            return ValidationResult(False, "DUPLICATE_EVENT")

        return ValidationResult(True)
