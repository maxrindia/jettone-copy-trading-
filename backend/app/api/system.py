import uuid
from datetime import datetime
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import get_redis
from app.models.accounts import Account
from app.services.static_ip import summarize_static_ips
from app.models.audit import SystemEvent
from app.schemas.system import (
    EmergencyStopRequest,
    StaticIPStatusResponse,
    SystemEventResponse,
)
from app.services.reliability.emergency_stop import EmergencyStopService

router = APIRouter(prefix="/system", tags=["System & Health"])


@router.get("/events", response_model=List[SystemEventResponse])
async def list_system_events(
    resolved: bool = Query(False),
    limit: int = Query(20, le=100),
    db: AsyncSession = Depends(get_db),
):
    query = (
        select(SystemEvent)
        .where(SystemEvent.resolved == resolved)
        .order_by(SystemEvent.created_at.desc())
        .limit(limit)
    )
    res = await db.execute(query)
    return res.scalars().all()


@router.post("/emergency-stop")
async def trigger_emergency_stop(
    payload: EmergencyStopRequest,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """
    Single runtime authority: the Redis-backed EmergencyStopService.
    Also mirrors the display/audit flag onto Account rows.
    """
    service = EmergencyStopService(redis)

    if payload.account_id:
        if payload.stop:
            await service.pause_follower(str(payload.account_id), db, reason=payload.reason)
        else:
            await service.resume_follower(str(payload.account_id), db, reason=payload.reason)
        stmt = (
            update(Account)
            .where(Account.id == payload.account_id)
            .values(is_emergency_stopped=payload.stop)
        )
        await db.execute(stmt)
        await db.commit()
        event_msg = f"Account emergency stop set to {payload.stop}. Reason: {payload.reason}"
    else:
        if payload.stop:
            await service.activate_global(db, reason=payload.reason)
        else:
            await service.deactivate_global(db, reason=payload.reason)
        stmt = update(Account).values(is_emergency_stopped=payload.stop)
        await db.execute(stmt)
        await db.commit()
        event_msg = f"GLOBAL emergency stop set to {payload.stop}. Reason: {payload.reason}"

    return {"status": "ok", "emergency_stop": payload.stop, "message": event_msg}


@router.get("/static-ip", response_model=StaticIPStatusResponse)
async def get_static_ip_status(db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Account).where(Account.deleted_at.is_(None)))
    summary = summarize_static_ips(res.scalars().all())
    return StaticIPStatusResponse(
        vps_ip="178.18.252.165",
        is_whitelisted=True,
        last_verified_at=datetime.utcnow(),
        available_count=summary.available,
        active_count=summary.active,
        untracked_count=summary.untracked,
        expired_count=summary.expired,
    )
