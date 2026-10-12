"""Central SystemEvent emitter: persists the event and dispatches a Telegram
alert for WARNING severity and above. Telegram failures never propagate into
the trading path."""
import logging
from typing import Any, Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit import EventSeverity, SystemEvent
from app.services.monitoring.telegram import TelegramAlertService

logger = logging.getLogger("notifier")

_alert_service: Optional[TelegramAlertService] = None

_SEVERITY_RANK = {
    EventSeverity.INFO: 0,
    EventSeverity.WARNING: 1,
    EventSeverity.ERROR: 2,
    EventSeverity.CRITICAL: 3,
}


def get_alert_service() -> TelegramAlertService:
    global _alert_service
    if _alert_service is None:
        _alert_service = TelegramAlertService(
            bot_token=settings.TELEGRAM_BOT_TOKEN,
            chat_id=settings.TELEGRAM_CHAT_ID,
        )
    return _alert_service


async def emit_system_event(
    db: AsyncSession,
    event_type: str,
    severity: Any,
    message: str,
    account_id: Optional[Any] = None,
    metadata_payload: Optional[Dict[str, Any]] = None,
    commit: bool = True,
) -> SystemEvent:
    if isinstance(severity, str):
        severity = EventSeverity(severity)

    event = SystemEvent(
        event_type=event_type,
        severity=severity,
        account_id=account_id,
        message=message,
        metadata_payload=metadata_payload,
    )
    db.add(event)
    if commit:
        await db.commit()

    if _SEVERITY_RANK.get(severity, 0) >= _SEVERITY_RANK[EventSeverity.WARNING]:
        try:
            await get_alert_service().send_alert(
                title=event_type,
                message=message,
                level=severity.value,
                details=metadata_payload or {},
            )
        except Exception as exc:
            logger.error(f"Telegram dispatch failed for {event_type}: {exc}")

    return event
