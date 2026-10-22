import logging
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import find_placeholder_secrets, settings
from app.core.crypto import validate_encryption_key
from app.core.redis import get_redis, close_redis
from app.services.runtime.engine_loop import EngineLoop

logging.basicConfig(
    level=settings.LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("engine")

engine_loop: Optional[EngineLoop] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine_loop
    logger.info("Starting Alice Blue Copy Trading Engine...")

    validate_encryption_key()
    placeholders = find_placeholder_secrets(settings)
    if placeholders:
        raise RuntimeError(
            f"Refusing to start: {', '.join(placeholders)} still hold .env.example placeholder values. "
            "Generate real secrets with scripts/bootstrap-env.sh."
        )
    if settings.MASTER_CAPITAL <= 0:
        raise RuntimeError("MASTER_CAPITAL must be a positive value.")
    logger.info(f"Configuration validated. MASTER_CAPITAL={settings.MASTER_CAPITAL}.")

    redis = await get_redis()
    try:
        await redis.ping()
        logger.info("Connected to Redis successfully.")
    except Exception as e:
        logger.warning(f"Could not connect to Redis at startup: {e}")

    # Ensure any new columns exist in PostgreSQL database
    from app.core.database import engine
    from sqlalchemy import text
    try:
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS broker VARCHAR(50) DEFAULT 'alicebluenew';"))
            await conn.execute(text("ALTER TABLE group_strategies ADD COLUMN IF NOT EXISTS place_rejected_enabled BOOLEAN DEFAULT FALSE;"))
            await conn.execute(text("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS proxy_expires_on DATE;"))
        logger.info("Database schema columns verified.")
    except Exception as exc:
        logger.warning(f"Schema auto-migration notice: {exc}")

    # Groww's instrument master (equity), only when GROWW_ENABLED and a URL are configured.
    from app.services.groww.instruments import load_instruments_if_enabled
    await load_instruments_if_enabled()

    engine_loop = EngineLoop(redis)
    await engine_loop.start()

    # R4c: observe-only instrument shadow comparison. Creates no task unless INSTRUMENT_SHADOW_ENABLED is true.
    from app.services.instruments.shadow import ShadowJob
    shadow_job = ShadowJob()
    try:
        await shadow_job.start()
    except Exception as exc:
        logger.error(f"Instrument shadow job could not start (ignored, observe-only): {exc}")

    yield

    logger.info("Shutting down Alice Blue Copy Trading Engine...")
    await shadow_job.stop()
    if engine_loop:
        await engine_loop.stop()
    await close_redis()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS configuration - strict internal
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://nextjs:3000", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register API routes
from app.api import api_router
app.include_router(api_router, prefix="/api/v1")


@app.get("/health", status_code=status.HTTP_200_OK, tags=["Health"])
async def healthcheck():
    redis_status = "unknown"
    try:
        redis = await get_redis()
        await redis.ping()
        redis_status = "healthy"
    except Exception as e:
        redis_status = f"unhealthy ({str(e)})"

    return {
        "status": "healthy",
        "service": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "redis": redis_status,
    }


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.error(f"Unhandled server error: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"success": False, "error": {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred."}},
    )
