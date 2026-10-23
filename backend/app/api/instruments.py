"""
Read-only instrument comparison endpoints (R4c). Mounted under the internal-token router like every other route.

Both endpoints are GET, write nothing, and never touch a broker client, an order method or the copy pipeline.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.audit import SystemEvent
from app.services.instruments import crosswalk_evidence, report
from app.services.instruments.shadow import EVENT_TYPE

router = APIRouter(prefix="/instruments", tags=["Instruments (observe-only)"])


@router.get("/compare")
async def compare_instrument_endpoint(
    exchange: str = Query(..., min_length=1, max_length=20),
    symbol: str = Query(..., min_length=1, max_length=60),
    isin: Optional[str] = Query(None, max_length=12),
):
    """Compare one explicitly identified instrument with every broker catalog. A supplied ISIN may produce a MATCH."""
    data = report.compare_one(exchange.strip(), symbol.strip(), (isin or "").strip() or None, datetime.now(timezone.utc))
    return {"success": True, "data": data}


@router.get("/shadow-report")
async def shadow_report_endpoint(
    hours: int = Query(24),
    limit: int = Query(100),
    offset: int = Query(0),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate of recent INSTRUMENT_SHADOW_COMPARE events. Hours, limit and offset are bounded, not rejected."""
    hours = report.clamp(hours, 1, report.MAX_HOURS)
    limit = report.clamp(limit, 1, report.MAX_LIMIT)
    offset = report.clamp(offset, 0, report.MAX_OFFSET)
    now = datetime.now(timezone.utc)
    rows = (await db.execute(
        select(SystemEvent.created_at, SystemEvent.metadata_payload)
        .where(SystemEvent.event_type == EVENT_TYPE, SystemEvent.created_at >= now - timedelta(hours=hours))
        .order_by(SystemEvent.created_at.desc())
        .limit(report.REPORT_EVENT_CAP + 1)
    )).all()
    truncated = len(rows) > report.REPORT_EVENT_CAP
    events = [(created, payload) for created, payload in rows[: report.REPORT_EVENT_CAP]]
    cw_rows = (await db.execute(
        select(SystemEvent.created_at, SystemEvent.metadata_payload)
        .where(SystemEvent.event_type == crosswalk_evidence.EVENT_TYPE, SystemEvent.created_at >= now - timedelta(hours=hours))
        .order_by(SystemEvent.created_at.desc())
        .limit(report.REPORT_EVENT_CAP)
    )).all()
    data = report.build_report(events, hours=hours, limit=limit, offset=offset, truncated=truncated, now=now,
                               crosswalk_events=[(created, payload) for created, payload in cw_rows])
    return {"success": True, "data": data}
