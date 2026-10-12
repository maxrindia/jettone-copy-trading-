"""Trading-day boundary for reporting.

The trading day starts at 00:00 in settings.TRADING_TIMEZONE (Asia/Kolkata).
Used by the Demat card metrics (P&L date filtering, daily order counts); the
copy engine and risk code keep their own existing boundaries.
"""
from datetime import date, datetime, time, timezone
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo

from app.core.config import settings


@lru_cache(maxsize=None)
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def trading_tz() -> ZoneInfo:
    return _zone(settings.TRADING_TIMEZONE)


def trading_day(now: Optional[datetime] = None) -> date:
    """The trading day `now` (default: current time) falls on."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(trading_tz()).date()


def trading_day_start_utc(day: date) -> datetime:
    """00:00 of `day` in the trading timezone, as a UTC datetime."""
    return datetime.combine(day, time.min, tzinfo=trading_tz()).astimezone(timezone.utc)
