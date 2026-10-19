"""Strategy membership rules: one strategy per account, one Master per strategy.

The database enforces both rules through strategy_account_roles (see
app/models/strategies.py). This module keeps that registry in step with
group_strategies.master_account_id and strategy_members - the rows every
existing reader (pipeline, dashboard, UI) already uses - inside the caller's
transaction, and turns constraint violations into clear API conflicts.

It also owns the per-account switch locks. A Master switch holds the locks
of the accounts it touches while it verifies and commits; order processing
for a Master account (engine_loop.persist_and_process_event and the
manual-order endpoint) takes the same gate (shared), so no new order for that
Master can be routed half-way through a switch.
"""
import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Iterable, Optional

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.accounts import CopySetting, QuantityMode
from app.models.strategies import (
    STRATEGY_ROLE_CHILD,
    STRATEGY_ROLE_MASTER,
    GroupStrategy,
    StrategyAccountRole,
)
from app.services.brokers.registry import get_broker_spec


class MembershipConflict(Exception):
    """An account is already assigned elsewhere. `code` is the API error code."""

    def __init__(self, code: str, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def assert_broker_can_be_child(broker: Optional[str]) -> None:
    """Raises MembershipConflict unless the engine's CURRENT broker capabilities say this broker may be a
    strategy Child (`as_child`). Backend-authoritative, in the same spirit as the Master check
    (strategy_switch NEW_MASTER_BROKER_UNSUPPORTED): the UI's disabled button is only a convenience.

    Fails closed. An unknown broker id, a broker the registry does not know, or any capability that is
    missing, is refused - never assumed. Legacy/blank broker values mean Alice Blue (the registry's alias
    rule), whose Child membership is unchanged. The message names the broker and the engine's own reason and
    carries no secret.
    """
    spec = get_broker_spec(broker)
    capabilities = getattr(spec, "capabilities", None) if spec is not None else None
    if spec is not None and getattr(capabilities, "as_child", None) is True:
        return
    if spec is None:
        raise MembershipConflict(
            "CHILD_BROKER_UNSUPPORTED",
            f"Broker '{broker}' is not recognised, so its account cannot be added as a strategy Child.",
        )
    reason = (getattr(spec, "reason", "") or "").strip()
    raise MembershipConflict(
        "CHILD_BROKER_UNSUPPORTED",
        f"Broker '{spec.id}' cannot be added as a strategy Child right now." + (f" {reason}" if reason else ""),
        {"broker": spec.id},
    )


async def get_assignment(db: AsyncSession, account_id: uuid.UUID) -> Optional[StrategyAccountRole]:
    return (
        await db.execute(select(StrategyAccountRole).where(StrategyAccountRole.account_id == account_id))
    ).scalar_one_or_none()


async def assert_account_available(
    db: AsyncSession,
    account_id: uuid.UUID,
    *,
    allow_strategy_id: Optional[uuid.UUID] = None,
    allow_role: Optional[str] = None,
) -> None:
    """Raises MembershipConflict when the account already belongs to a
    strategy - unless it is exactly the (strategy, role) the caller is about
    to move it out of, given by allow_strategy_id/allow_role."""
    existing = await get_assignment(db, account_id)
    if existing is None:
        return
    if allow_strategy_id is not None and existing.strategy_id == allow_strategy_id and (
        allow_role is None or existing.role == allow_role
    ):
        return
    strategy = await db.get(GroupStrategy, existing.strategy_id)
    name = strategy.name if strategy else str(existing.strategy_id)
    raise MembershipConflict(
        "ACCOUNT_ALREADY_ASSIGNED",
        f"This account is already the {existing.role.lower()} account of strategy '{name}'. "
        "Remove it from that strategy first.",
        {"strategy_id": str(existing.strategy_id), "strategy_name": name, "role": existing.role},
    )


async def assign(db: AsyncSession, strategy_id: uuid.UUID, account_id: uuid.UUID, role: str) -> None:
    """Records the account's membership. Caller has checked availability and
    commits; the unique constraints still reject a concurrent duplicate."""
    db.add(StrategyAccountRole(strategy_id=strategy_id, account_id=account_id, role=role))


async def release(db: AsyncSession, account_id: uuid.UUID) -> None:
    await db.execute(delete(StrategyAccountRole).where(StrategyAccountRole.account_id == account_id))


async def release_strategy(db: AsyncSession, strategy_id: uuid.UUID) -> None:
    await db.execute(delete(StrategyAccountRole).where(StrategyAccountRole.strategy_id == strategy_id))


async def sync_copy_enabled(db: AsyncSession, account_id: uuid.UUID, enabled: bool) -> bool:
    """Makes the Child's CopySetting.copy_enabled follow its strategy
    "Trading" toggle (StrategyMember.trading_enabled), so there is ONE
    switch for the user: toggle ON = its Master's orders copy to this Child.
    (copy_enabled used to be a second, UI-less gate that silently rejected
    every copy with FOLLOWER_PAUSED.)

    Every Child needs a CopySetting because the pipeline's RiskEngine gate
    only runs when one exists, so one is created when absent. It is created
    in MULTIPLIER mode (Master qty x the Child's strategy multiplier): that is
    exactly the quantity a Child WITHOUT a CopySetting was already getting, so
    creating the row never changes what is copied. (It used to be created as
    FIXED qty 1, which turned a Master NIFTY order of 65 into a Child order of
    1 and made Alice Blue reject it with EC954 "multiple of the lot size".)
    fixed_qty stays 1 so the user can still switch the policy to FIXED.
    Quantity mode, limits and every other field of an existing CopySetting are
    never touched. Returns True if a CopySetting was created."""
    existing = (
        await db.execute(select(CopySetting).where(CopySetting.follower_account_id == account_id))
    ).scalar_one_or_none()
    if existing is not None:
        existing.copy_enabled = enabled
        return False
    db.add(
        CopySetting(
            follower_account_id=account_id,
            copy_enabled=enabled,
            quantity_mode=QuantityMode.MULTIPLIER,
            fixed_qty=1,
        )
    )
    return True


__all__ = [
    "MembershipConflict",
    "STRATEGY_ROLE_CHILD",
    "STRATEGY_ROLE_MASTER",
    "account_order_gate",
    "accounts_switch_lock",
    "assert_account_available",
    "assign",
    "sync_copy_enabled",
    "get_assignment",
    "release",
    "release_strategy",
]


# --- per-account switch gates ----------------------------------------------
# A shared/exclusive gate per account. Order processing for a Master takes
# it SHARED, so different orders of the same Master still run concurrently
# exactly as before; a Master switch takes it EXCLUSIVE, so it waits for
# in-progress order processing to finish and holds new processing back until
# the switch has committed and monitoring is re-synced. A waiting switch
# blocks new shared entries, so it cannot be starved.
# Bounded by the number of accounts (personal setup: a handful), so entries
# are never evicted.


class _AccountGate:
    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._writers_waiting = 0

    @asynccontextmanager
    async def shared(self) -> AsyncIterator[None]:
        async with self._cond:
            await self._cond.wait_for(lambda: not self._writer and self._writers_waiting == 0)
            self._readers += 1
        try:
            yield
        finally:
            async with self._cond:
                self._readers -= 1
                self._cond.notify_all()

    async def acquire_exclusive(self) -> None:
        async with self._cond:
            self._writers_waiting += 1
            try:
                await self._cond.wait_for(lambda: not self._writer and self._readers == 0)
            finally:
                self._writers_waiting -= 1
                self._cond.notify_all()
            self._writer = True

    async def release_exclusive(self) -> None:
        async with self._cond:
            self._writer = False
            self._cond.notify_all()


_account_gates: Dict[str, _AccountGate] = {}


def _gate_for(account_id: str) -> _AccountGate:
    gate = _account_gates.get(account_id)
    if gate is None:
        gate = _AccountGate()
        _account_gates[account_id] = gate
    return gate


@asynccontextmanager
async def account_order_gate(account_id: Optional[str]) -> AsyncIterator[None]:
    """Held (shared) while an order for this Master is detected/routed/copied."""
    if not account_id:
        yield
        return
    async with _gate_for(str(account_id)).shared():
        yield


@asynccontextmanager
async def accounts_switch_lock(account_ids: Iterable[Optional[str]]) -> AsyncIterator[None]:
    """Exclusive gates for several accounts, taken in a fixed (sorted) order
    so two concurrent switches can never deadlock on each other."""
    ordered = sorted({str(a) for a in account_ids if a})
    acquired = []
    try:
        for account_id in ordered:
            gate = _gate_for(account_id)
            await gate.acquire_exclusive()
            acquired.append(gate)
        yield
    finally:
        for gate in reversed(acquired):
            await gate.release_exclusive()
