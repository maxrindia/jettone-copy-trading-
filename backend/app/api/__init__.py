from fastapi import APIRouter, Depends
from app.api.accounts import router as accounts_router
from app.api.trades import router as trades_router
from app.api.system import router as system_router
from app.api.broker import router as broker_router
from app.api.reliability import router as reliability_router
from app.api.monitoring import router as monitoring_router
from app.api.strategies import router as strategies_router
from app.api.dashboard import router as dashboard_router
from app.api.instruments import router as instruments_router
from app.api.brokers import router as brokers_router
from app.core.security import require_internal_token

api_router = APIRouter(dependencies=[Depends(require_internal_token)])
api_router.include_router(accounts_router)
api_router.include_router(trades_router)
api_router.include_router(system_router)
api_router.include_router(broker_router)
api_router.include_router(reliability_router)
api_router.include_router(monitoring_router)
api_router.include_router(strategies_router)
api_router.include_router(dashboard_router)
api_router.include_router(instruments_router)
api_router.include_router(brokers_router)
