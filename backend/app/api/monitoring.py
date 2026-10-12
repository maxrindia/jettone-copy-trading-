import os
import psutil
from typing import Any, Dict, Optional
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import get_redis
from app.services.monitoring.telegram import TelegramAlertService

router = APIRouter(prefix="/monitoring", tags=["monitoring"])


class ApiResponse(BaseModel):
    success: bool
    data: Optional[Any] = None
    message: str
    error: Optional[str] = None


class TestAlertRequest(BaseModel):
    title: str = "Test System Alert"
    message: str = "This is a manual test alert triggered from the monitoring API."
    level: str = "INFO"


@router.get("/health", response_model=ApiResponse)
async def get_system_health(
    db: AsyncSession = Depends(get_db),
    redis=Depends(get_redis),
):
    # 1. DB Health Check
    db_healthy = False
    try:
        res = await db.execute(text("SELECT 1"))
        db_healthy = (res.scalar() == 1)
    except Exception:
        pass

    # 2. Redis Health Check
    redis_healthy = False
    try:
        redis_healthy = await redis.ping()
    except Exception:
        pass

    # 3. VPS System Metrics
    cpu_percent = psutil.cpu_percent(interval=None)
    memory = psutil.virtual_memory()

    data = {
        "status": "healthy" if (db_healthy and redis_healthy) else "degraded",
        "components": {
            "postgres": "healthy" if db_healthy else "unhealthy",
            "redis": "healthy" if redis_healthy else "unhealthy",
            "api_engine": "healthy",
        },
        "vps_metrics": {
            "cpu_usage_percent": cpu_percent,
            "ram_total_mb": round(memory.total / (1024 * 1024), 2),
            "ram_used_mb": round(memory.used / (1024 * 1024), 2),
            "ram_usage_percent": memory.percent,
        },
        "static_ip_status": "whitelisted",
        "environment": os.getenv("ENVIRONMENT", "development"),
    }
    return ApiResponse(success=True, message="Health report generated", data=data)


@router.post("/alert/test", response_model=ApiResponse)
async def send_test_alert(req: TestAlertRequest):
    service = TelegramAlertService()
    sent = await service.send_alert(
        title=req.title,
        message=req.message,
        level=req.level,
        details={"source": "api_test", "configured": service.is_configured},
    )
    return ApiResponse(
        success=True,
        message="Alert trigger executed",
        data={"sent_to_telegram": sent, "telegram_configured": service.is_configured},
    )
