import logging
from typing import Optional
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import EventSeverity
from app.services.monitoring.notifier import emit_system_event

logger = logging.getLogger(__name__)

GLOBAL_STOP_KEY = "emergency_stop:global"
FOLLOWER_PAUSE_PREFIX = "emergency_stop:follower:"
MASTER_TRADING_KEY = "master:trading_enabled"


def _as_bool(val) -> bool:
    if isinstance(val, bytes):
        return val.decode("utf-8") == "true"
    return str(val) == "true"


async def is_master_trading_enabled(redis: aioredis.Redis) -> bool:
    """Absent key means enabled; the global emergency stop is the kill switch."""
    val = await redis.get(MASTER_TRADING_KEY)
    if val is None:
        return True
    return _as_bool(val)


async def set_master_trading(redis: aioredis.Redis, enabled: bool) -> None:
    if enabled:
        await redis.delete(MASTER_TRADING_KEY)
    else:
        await redis.set(MASTER_TRADING_KEY, "false")


class EmergencyStopService:
    def __init__(self, redis_client: aioredis.Redis):
        self.redis = redis_client

    async def is_global_active(self) -> bool:
        val = await self.redis.get(GLOBAL_STOP_KEY)
        if val is None:
            return False
        return _as_bool(val)

    async def activate_global(self, db: AsyncSession, reason: str = "Manual activation") -> None:
        await self.redis.set(GLOBAL_STOP_KEY, "true")
        logger.critical(f"GLOBAL EMERGENCY STOP ACTIVATED: {reason}")
        
        await emit_system_event(
            db,
            event_type="EMERGENCY_STOP_ACTIVATED",
            severity=EventSeverity.CRITICAL,
            message=f"Global emergency stop activated: {reason}",
            metadata_payload={"scope": "global", "reason": reason},
        )

    async def deactivate_global(self, db: AsyncSession, reason: str = "Manual deactivation") -> None:
        await self.redis.delete(GLOBAL_STOP_KEY)
        logger.info(f"Global emergency stop deactivated: {reason}")

        await emit_system_event(
            db,
            event_type="EMERGENCY_STOP_DEACTIVATED",
            severity=EventSeverity.INFO,
            message=f"Global emergency stop deactivated: {reason}",
            metadata_payload={"scope": "global", "reason": reason},
        )

    async def is_follower_paused(self, account_id: str) -> bool:
        key = f"{FOLLOWER_PAUSE_PREFIX}{account_id}"
        val = await self.redis.get(key)
        if val is None:
            return False
        return _as_bool(val)

    async def pause_follower(self, account_id: str, db: AsyncSession, reason: str = "Manual pause") -> None:
        key = f"{FOLLOWER_PAUSE_PREFIX}{account_id}"
        await self.redis.set(key, "true")
        logger.warning(f"Follower {account_id} PAUSED: {reason}")
        
        await emit_system_event(
            db,
            event_type="FOLLOWER_PAUSED",
            severity=EventSeverity.WARNING,
            message=f"Follower {account_id} paused: {reason}",
            metadata_payload={"account_id": account_id, "reason": reason},
        )

    async def resume_follower(self, account_id: str, db: AsyncSession, reason: str = "Manual resume") -> None:
        key = f"{FOLLOWER_PAUSE_PREFIX}{account_id}"
        await self.redis.delete(key)
        logger.info(f"Follower {account_id} RESUMED: {reason}")
        
        await emit_system_event(
            db,
            event_type="FOLLOWER_RESUMED",
            severity=EventSeverity.INFO,
            message=f"Follower {account_id} resumed: {reason}",
            metadata_payload={"account_id": account_id, "reason": reason},
        )
