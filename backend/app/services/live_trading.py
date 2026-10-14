"""
Live Trading ON/OFF - a runtime switch the owner controls from the UI.

State resolution (fail closed):
  * a stored value (set from the UI) is authoritative;
  * with nothing stored, ENGINE_LIVE_TRADING_ENABLED from the environment is the
    default (so existing installs behave exactly as before);
  * if the state cannot be read for any reason the answer is OFF.

OFF means: no NEW broker order is submitted by the engine or the manual-order
endpoint. Master order CANCELLATIONS are still propagated to already-placed
Child orders (they only reduce exposure) through the existing cancel logic,
and Child fills are still synced. Modifications of existing Child orders are
treated like new exposure and are not propagated while OFF (the same stance the
global emergency stop takes).
"""
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.platform_settings import PlatformSetting
from app.services.monitoring.notifier import emit_system_event

logger = logging.getLogger(__name__)

LIVE_TRADING_KEY = "live_trading_enabled"

# Master events that may still be acted on while Live Trading is OFF: a
# cancellation can only reduce what is already working at the broker.
EXPOSURE_REDUCING_EVENTS = frozenset({"ORDER_CANCELLED"})


@dataclass(frozen=True)
class LiveTradingState:
    enabled: bool
    source: str  # "runtime" (stored from the UI) | "env_default" | "error"
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None


def event_allowed_when_live_off(event_type: str) -> bool:
    return str(event_type or "").upper() in EXPOSURE_REDUCING_EVENTS


async def get_live_trading_state(db: AsyncSession) -> LiveTradingState:
    try:
        row = (
            await db.execute(select(PlatformSetting).where(PlatformSetting.key == LIVE_TRADING_KEY))
        ).scalar_one_or_none()
    except Exception as exc:  # unreadable state is never "ON"
        logger.error(f"[LiveTrading] Could not read the Live Trading state; treating as OFF: {exc}")
        return LiveTradingState(enabled=False, source="error")
    if row is None:
        return LiveTradingState(enabled=bool(settings.ENGINE_LIVE_TRADING_ENABLED), source="env_default")
    return LiveTradingState(
        enabled=str(row.value).strip().lower() == "true",
        source="runtime",
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


async def is_live_trading_enabled() -> bool:
    """Per-event check used by the engine loop (own short-lived session)."""
    try:
        async with AsyncSessionLocal() as db:
            return (await get_live_trading_state(db)).enabled
    except Exception as exc:
        logger.error(f"[LiveTrading] Live Trading check failed; treating as OFF: {exc}")
        return False


async def set_live_trading(
    db: AsyncSession, enabled: bool, *, actor: str = "ui", reason: str = "Toggled from the app"
) -> LiveTradingState:
    row = (
        await db.execute(select(PlatformSetting).where(PlatformSetting.key == LIVE_TRADING_KEY))
    ).scalar_one_or_none()
    previous = None if row is None else (str(row.value).strip().lower() == "true")
    value = "true" if enabled else "false"
    if row is None:
        db.add(PlatformSetting(key=LIVE_TRADING_KEY, value=value, updated_by=actor))
    else:
        row.value = value
        row.updated_by = actor
    await db.commit()

    await emit_system_event(
        db,
        event_type="LIVE_TRADING_TOGGLED",
        severity="WARNING" if enabled else "INFO",
        message=f"Live Trading turned {'ON' if enabled else 'OFF'} by {actor}: {reason}",
        metadata_payload={"enabled": enabled, "previous": previous, "actor": actor, "reason": reason},
    )
    return await get_live_trading_state(db)
