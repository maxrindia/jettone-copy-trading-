"""Shared emergency-stop provider used by both the live engine loop and the
manual-order API path, so every order path consults the same authority."""
import logging
from typing import Awaitable, Callable, Iterable, Optional, Set, Tuple

import redis.asyncio as aioredis

from app.services.reliability.emergency_stop import EmergencyStopService

logger = logging.getLogger("stop_provider")


def build_stop_provider(
    redis: aioredis.Redis,
    account_ids: Optional[Iterable[str]] = None,
) -> Callable[[], Awaitable[Tuple[bool, Set[str]]]]:
    """
    Returns an async callable -> (global_active, stopped_follower_ids).
    account_ids=None resolves the ids from the account manager on EVERY
    call, and covers every registered account - not just account_type
    FOLLOWER ones - because with strategy-specific roles any account
    (including one created as MASTER) can be a strategy's Child, and its
    per-account pause must still be honored.
    """
    service = EmergencyStopService(redis)
    fixed_ids = None if account_ids is None else list(account_ids)

    def _ids() -> Iterable[str]:
        if fixed_ids is not None:
            return fixed_ids
        from app.services.alice_blue.manager import account_manager

        return account_manager.get_all_account_ids()

    async def provider() -> Tuple[bool, Set[str]]:
        global_active = await service.is_global_active()
        stopped: Set[str] = set()
        for acc_id in _ids():
            if await service.is_follower_paused(acc_id):
                stopped.add(acc_id)
        return global_active, stopped

    return provider
