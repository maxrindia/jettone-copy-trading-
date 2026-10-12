"""Bridge from the API layer to the running EngineLoop.

The API and the engine share one process (uvicorn runs with --workers 1 and
main.py's lifespan starts the EngineLoop), so strategy changes can bring
Master monitoring in line immediately instead of waiting for a restart.
When no engine is running (tests, or startup failed) every hook is a no-op
and readiness reads as False, so callers fail closed.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_engine_loop = None


def set_engine_loop(loop) -> None:
    global _engine_loop
    _engine_loop = loop


def get_engine_loop():
    return _engine_loop


async def sync_master_monitors(activations=None) -> None:
    """Starts monitors for newly assigned strategy Masters and stops
    monitors for accounts that are no longer one. activations: a Master
    switch's preflight snapshot (SwitchPreflight.activations()). Never
    raises."""
    loop = _engine_loop
    if loop is None:
        return
    try:
        await loop.sync_master_monitors(activations=activations)
    except Exception as exc:
        logger.error(f"[RuntimeHooks] Master monitor sync failed: {exc}", exc_info=True)


def is_master_monitor_ready(account_id: Optional[str]) -> bool:
    """True only when the engine is monitoring this account as a Master and
    its first (baseline) order-book poll has completed - the activation
    boundary before which the account's orders are never copied."""
    loop = _engine_loop
    if loop is None or not account_id:
        return False
    try:
        return bool(loop.is_master_monitor_ready(str(account_id)))
    except Exception:
        return False
