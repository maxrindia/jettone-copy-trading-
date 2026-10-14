from typing import Any, Dict, Optional, List
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import get_redis
from app.models.reconciliation import ReconciliationRecord
from app.services.monitoring.notifier import emit_system_event
from app.services.reliability.emergency_stop import (
    EmergencyStopService,
    is_master_trading_enabled,
    set_master_trading,
)
from app.services.live_trading import get_live_trading_state, set_live_trading
from app.services.reliability.reconciliation import ReconciliationEngine

router = APIRouter(prefix="/reliability", tags=["reliability"])


class EmergencyStopRequest(BaseModel):
    active: bool
    reason: Optional[str] = "API Request"


class FollowerPauseRequest(BaseModel):
    paused: bool
    reason: Optional[str] = "API Request"


class ApiResponse(BaseModel):
    success: bool
    data: Optional[Any] = None
    message: str
    error: Optional[str] = None


@router.post("/emergency-stop", response_model=ApiResponse)
async def toggle_global_emergency_stop(
    req: EmergencyStopRequest,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    service = EmergencyStopService(redis)
    if req.active:
        await service.activate_global(db, reason=req.reason)
        return ApiResponse(success=True, message="Global emergency stop activated", data={"global_active": True})
    else:
        await service.deactivate_global(db, reason=req.reason)
        return ApiResponse(success=True, message="Global emergency stop deactivated", data={"global_active": False})


@router.get("/emergency-stop", response_model=ApiResponse)
async def get_emergency_stop_status(redis=Depends(get_redis)):
    service = EmergencyStopService(redis)
    active = await service.is_global_active()
    return ApiResponse(success=True, message="Status fetched", data={"global_active": active})


@router.post("/follower/{account_id}/pause", response_model=ApiResponse)
async def toggle_follower_pause(
    account_id: UUID,
    req: FollowerPauseRequest,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    service = EmergencyStopService(redis)
    if req.paused:
        await service.pause_follower(str(account_id), db, reason=req.reason)
        return ApiResponse(success=True, message=f"Follower {account_id} paused", data={"account_id": str(account_id), "paused": True})
    else:
        await service.resume_follower(str(account_id), db, reason=req.reason)
        return ApiResponse(success=True, message=f"Follower {account_id} resumed", data={"account_id": str(account_id), "paused": False})


class LiveTradingRequest(BaseModel):
    enabled: bool
    # Turning trading ON must be an explicit, deliberate API call.
    confirm: bool = False
    reason: Optional[str] = "Toggled from the app"


async def _live_trading_payload(db: AsyncSession, redis) -> Dict[str, Any]:
    state = await get_live_trading_state(db)
    return {
        "enabled": state.enabled,
        "source": state.source,
        "updated_at": state.updated_at.isoformat() if state.updated_at else None,
        "updated_by": state.updated_by,
        "master_trading_enabled": await is_master_trading_enabled(redis),
        "emergency_stop_active": await EmergencyStopService(redis).is_global_active(),
    }


@router.get("/live-trading", response_model=ApiResponse)
async def get_live_trading(db: AsyncSession = Depends(get_db), redis=Depends(get_redis)):
    return ApiResponse(
        success=True,
        message="Live trading state fetched",
        data=await _live_trading_payload(db, redis),
    )


@router.put("/live-trading", response_model=ApiResponse)
async def put_live_trading(
    req: LiveTradingRequest,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    """Owner-controlled Live Trading switch. OFF takes effect immediately for
    every new order; see app/services/live_trading.py for exactly what stays
    active while OFF (cancellations propagate, fills keep syncing)."""
    if req.enabled and not req.confirm:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Turning Live Trading ON requires confirm=true.",
        )
    await set_live_trading(db, req.enabled, actor="ui", reason=req.reason or "Toggled from the app")
    return ApiResponse(
        success=True,
        message=f"Live trading {'enabled' if req.enabled else 'disabled'}",
        data=await _live_trading_payload(db, redis),
    )


@router.get("/master-trading", response_model=ApiResponse)
async def get_master_trading(redis=Depends(get_redis), db: AsyncSession = Depends(get_db)):
    from app.core.config import settings

    enabled = await is_master_trading_enabled(redis)
    return ApiResponse(
        success=True,
        message="Master trading state fetched",
        data={"enabled": enabled, "live_trading_enabled": (await get_live_trading_state(db)).enabled},
    )


@router.post("/master-trading", response_model=ApiResponse)
async def set_master_trading_state(
    req: FollowerPauseRequest,
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    enabled = not req.paused
    await set_master_trading(redis, enabled)
    await emit_system_event(
        db,
        event_type="MASTER_TRADING_TOGGLED",
        severity="INFO" if enabled else "WARNING",
        message=f"Master trading {'enabled' if enabled else 'disabled'}: {req.reason}",
        metadata_payload={"enabled": enabled, "reason": req.reason},
    )
    return ApiResponse(success=True, message=f"Master trading {'enabled' if enabled else 'disabled'}", data={"enabled": enabled})


@router.get("/reconciliation/records", response_model=ApiResponse)
async def get_reconciliation_records(
    resolved: Optional[bool] = None,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(ReconciliationRecord).order_by(ReconciliationRecord.run_at.desc())
    if resolved is not None:
        stmt = stmt.where(ReconciliationRecord.resolved == resolved)

    result = await db.execute(stmt)
    records = result.scalars().all()
    
    data = [
        {
            "id": str(r.id),
            "account_id": str(r.account_id),
            "symbol": r.symbol,
            "expected_qty": r.expected_qty,
            "actual_qty": r.actual_qty,
            "discrepancy_qty": r.discrepancy_qty,
            "run_at": r.run_at.isoformat(),
            "resolved": r.resolved,
            "notes": r.notes,
        }
        for r in records
    ]
    return ApiResponse(success=True, message="Reconciliation records fetched", data=data)


@router.post("/reconciliation/run", response_model=ApiResponse)
async def trigger_reconciliation(
    account_id: Optional[UUID] = None,
    db: AsyncSession = Depends(get_db),
):
    """
    Manual reconciliation trigger. Resolves the account's real
    AliceBlueClient via the existing account manager so actual broker
    positions are genuinely fetched (see ReconciliationEngine.run_for_account
    - without a client this fails closed instead of silently comparing
    against an assumed-empty position map).
    """
    from app.services.alice_blue.manager import account_manager

    if account_id:
        client = account_manager.get_client(str(account_id))
        mismatches = await ReconciliationEngine.run_for_account(account_id, db, broker_client=client)
        return ApiResponse(
            success=True,
            message=f"Reconciliation finished for account {account_id}",
            data={"mismatches_found": len(mismatches)},
        )
    else:
        recovered = await ReconciliationEngine.run_crash_recovery(
            db, client_provider=account_manager.get_client
        )
        return ApiResponse(
            success=True,
            message="Global reconciliation & crash recovery run finished",
            data={"recovered_in_flight_trades": recovered},
        )
