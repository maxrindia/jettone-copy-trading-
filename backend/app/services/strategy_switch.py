"""Safe Master switching for a strategy.

A switch changes which account a strategy copies FROM. It is only allowed
when nothing about the strategy's accounts is still in motion, because once
the old Master stops being monitored, nothing would propagate its later
modifications/cancellations/fills, and its open positions' exits would never
be copied. Nothing here ever cancels an order or squares off a position: a
blocked switch just reports why, and the user resolves it at the broker.

Preflight blockers (any one blocks; every check fails closed):

- PENDING_MASTER_ORDERS   the old or new Master has a working order
                          (Jettone's records or the live broker order book)
- PENDING_CHILD_ORDERS    a Jettone-placed child order on any account of the
                          strategy is still open/partial/pending (DB or broker)
- COPY_IN_FLIGHT          a copy placement has not finished (CopyTrade
                          PENDING/SUBMITTED)
(Unresolved reconciliation items and Jettone's records-based CNC/NRML
totals are warnings, not blockers: they are noisy or incomplete, and
everything a switch depends on is verified live. See CARRY_POSITION_UNVERIFIED.)
- OPEN_POSITIONS          the live broker position book shows a non-zero
                          position on any account of the strategy in a
                          product other than CNC or intraday (NRML/carry, or a
                          product Jettone cannot classify - fail closed)
- CARRY_POSITION_UNVERIFIED  Jettone's own records show an open CNC/NRML
                          quantity. No Alice Blue holdings endpoint has been
                          verified, so a flat position book is NOT taken as
                          proof that delivery/carry-forward holdings are gone.
- BROKER_VERIFICATION_UNAVAILABLE  an order book or position book could not be
                          fetched, or the engine holds no broker client

Open CNC (delivery) and intraday positions do NOT block a switch. They are
reported as warnings:

- CNC_POSITIONS_OPEN      delivery positions stay open in Alice Blue; Jettone
                          never closes or transfers them. The switch then needs
                          the user's explicit acknowledgement
                          (acknowledge_cnc_positions), re-checked server side
                          under the locks.
- INTRADAY_POSITIONS_OPEN informational only.

Activation boundary: the new Master's order book is snapshotted here, under
the lock, while it has no working order. Its monitor treats exactly those
orders as history; anything else it sees first was placed after the switch
and is copied (see MasterOrderPoller's activation replay), so a valid order
placed while the switch completes is neither dropped nor duplicated.

The switch itself runs under the per-account switch locks of the old and new
Master (strategy_membership.accounts_switch_lock), re-runs the preflight
inside the lock, and only then commits. Order processing for a Master takes
the same lock, so no order can be routed between the check and the commit.
"""
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.trading_day import trading_day, trading_day_start_utc
from app.models.accounts import Account
from app.models.reconciliation import ReconciliationRecord
from app.models.strategies import (
    STRATEGY_ROLE_CHILD,
    STRATEGY_ROLE_MASTER,
    GroupStrategy,
    StrategyMember,
)
from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderSide, OrderSource, OrderStatus
from app.services import strategy_membership as membership
from app.services.alice_blue.client import order_book_row_id
from app.services.brokers.registry import can_be_master
from app.services.copy_engine.risk_inputs import (
    UnknownProductTypeError,
    normalize_product_for_exchange,
    normalize_product_type,
)
from app.services.monitoring.notifier import emit_system_event

logger = logging.getLogger(__name__)

OLD_MASTER_ACTIONS = ("CHILD", "REMOVE")
OPEN_STATUSES = (OrderStatus.OPEN, OrderStatus.PARTIAL, OrderStatus.PENDING)
# Broker order-book statuses that mean "this order can no longer change".
# Anything else - including a status this code has never seen - counts as
# still working (fail closed).
TERMINAL_BROKER_STATUSES = {"COMPLETE", "COMPLETED", "CANCELLED", "CANCELED", "REJECTED"}
CARRY_PRODUCTS = {"CNC", "NRML"}
MAX_ITEMS = 10
# How long after the preflight snapshot the new Master's first poll may still
# treat every order missing from that snapshot as placed after the switch
# (and so copy it). Later than this, the first poll baselines everything and
# reports what it did not copy - a stale snapshot is never trusted.
ACTIVATION_REPLAY_WINDOW = timedelta(seconds=120)

ClientProvider = Callable[[str], Optional[Any]]


class SwitchValidationError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass
class SwitchBlocker:
    code: str
    message: str
    account_id: Optional[str] = None
    account_name: Optional[str] = None
    role: Optional[str] = None  # CURRENT_MASTER | NEW_MASTER | CHILD
    count: Optional[int] = None
    items: List[str] = field(default_factory=list)


@dataclass
class SwitchPreflight:
    strategy_id: str
    strategy_name: str
    current_master_id: Optional[str]
    new_master_id: str
    old_master_action: str
    allowed: bool
    # True only when EVERY blocker is "broker verification unavailable": the
    # user may acknowledge that they verified the accounts themselves and
    # switch anyway. Real findings (working orders, open positions, copies in
    # flight) are never overridable.
    can_override: bool
    blockers: List[SwitchBlocker]
    warnings: List[SwitchBlocker]
    limitations: List[str]
    checked_accounts: List[Dict[str, str]]
    # True when open CNC positions exist: the switch is allowed only with an
    # explicit acknowledgement that Jettone will not close or transfer them.
    requires_cnc_acknowledgement: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def activations(self) -> Dict[str, Tuple[Set[str], datetime]]:
        """{new Master id: (order ids in its order book at preflight time,
        replay deadline)} for the engine's activation replay; empty when no
        snapshot was taken. Not part of the API response."""
        snapshot = getattr(self, "new_master_order_ids", None)
        taken_at = getattr(self, "snapshot_at", None)
        if snapshot is None or taken_at is None:
            return {}
        return {self.new_master_id: (set(snapshot), taken_at + ACTIVATION_REPLAY_WINDOW)}


class SwitchBlocked(Exception):
    def __init__(self, preflight: SwitchPreflight):
        self.preflight = preflight
        summary = "; ".join(
            f"{b.account_name or 'strategy'}: {b.message}" for b in preflight.blockers
        )
        super().__init__(f"Master switch blocked - {summary}")


LIMITATIONS = [
    "Delivery (CNC/NRML) holdings can't be verified with Alice Blue; only Jettone's records are shown.",
]


def _raw_status(row: Dict[str, Any]) -> str:
    return str(row.get("orderStatus", row.get("Status", "")) or "").upper().strip()


def _raw_order_id(row: Dict[str, Any]) -> str:
    return str(row.get("brokerOrderId", row.get("Nstordno", row.get("orderId", ""))) or "").strip()


def _raw_label(row: Dict[str, Any]) -> str:
    sym = row.get("tradingSymbol", row.get("Trsym", row.get("symbol", ""))) or "?"
    return f"{_raw_order_id(row) or '?'} {sym} {_raw_status(row) or 'UNKNOWN'}"


async def _load_strategy(db: AsyncSession, strategy_id: uuid.UUID) -> GroupStrategy:
    strategy = (
        await db.execute(
            select(GroupStrategy)
            .where(GroupStrategy.id == strategy_id, GroupStrategy.deleted_at.is_(None))
            .options(selectinload(GroupStrategy.members).selectinload(StrategyMember.account))
        )
    ).scalar_one_or_none()
    if strategy is None:
        raise SwitchValidationError(404, "STRATEGY_NOT_FOUND", "Strategy not found.")
    return strategy


async def _validate_request(
    db: AsyncSession, strategy: GroupStrategy, new_master_id: uuid.UUID, old_master_action: str
) -> Account:
    if old_master_action not in OLD_MASTER_ACTIONS:
        raise SwitchValidationError(
            422, "INVALID_OLD_MASTER_ACTION", "old_master_action must be CHILD or REMOVE."
        )
    if strategy.master_account_id == new_master_id:
        raise SwitchValidationError(400, "SAME_MASTER", "This account is already the strategy's Master.")

    new_master = (
        await db.execute(
            select(Account).where(Account.id == new_master_id).options(selectinload(Account.api_credential))
        )
    ).scalar_one_or_none()
    if new_master is None or new_master.deleted_at is not None:
        raise SwitchValidationError(404, "ACCOUNT_NOT_FOUND", "The selected Master account was not found.")
    if not new_master.is_active:
        raise SwitchValidationError(
            409, "NEW_MASTER_NOT_ELIGIBLE", "The selected Master account is inactive. Activate it first."
        )
    if not can_be_master(new_master.broker):
        raise SwitchValidationError(
            409, "NEW_MASTER_BROKER_UNSUPPORTED",
            f"Broker '{new_master.broker}' can only be a Child account; its orders cannot be detected, "
            "so it cannot be a strategy's Master.",
        )
    if new_master.api_credential is None:
        raise SwitchValidationError(
            409, "NEW_MASTER_NOT_ELIGIBLE", "The selected Master account has no API credential configured."
        )

    try:
        # Allowed only if unassigned, or currently a Child of THIS strategy
        # (it is promoted and its Child membership removed).
        await membership.assert_account_available(
            db, new_master_id, allow_strategy_id=strategy.id, allow_role=STRATEGY_ROLE_CHILD
        )
    except membership.MembershipConflict as exc:
        raise SwitchValidationError(409, exc.code, exc.message)
    return new_master


async def run_preflight(
    db: AsyncSession,
    strategy_id: uuid.UUID,
    new_master_id: uuid.UUID,
    old_master_action: str,
    client_provider: ClientProvider,
    *,
    now: Optional[datetime] = None,
) -> SwitchPreflight:
    strategy = await _load_strategy(db, strategy_id)
    new_master = await _validate_request(db, strategy, new_master_id, old_master_action)

    # Every account whose state the switch depends on.
    scope: Dict[uuid.UUID, str] = {}
    if strategy.master_account_id:
        scope[strategy.master_account_id] = "CURRENT_MASTER"
    for m in strategy.members:
        if m.account_id != new_master_id:
            scope.setdefault(m.account_id, "CHILD")
    scope[new_master_id] = "NEW_MASTER"

    names: Dict[uuid.UUID, str] = {
        a.id: a.display_name
        for a in (await db.execute(select(Account).where(Account.id.in_(list(scope))))).scalars().all()
    }
    master_ids = [a for a, r in scope.items() if r in ("CURRENT_MASTER", "NEW_MASTER")]
    scope_ids = list(scope)

    blockers: List[SwitchBlocker] = []
    warnings: List[SwitchBlocker] = []

    def add(target: List[SwitchBlocker], code: str, acc: Optional[uuid.UUID], message: str,
            count: Optional[int] = None, items: Sequence[str] = ()):
        target.append(SwitchBlocker(
            code=code,
            message=message,
            account_id=str(acc) if acc else None,
            account_name=names.get(acc) if acc else None,
            role=scope.get(acc) if acc else None,
            count=count,
            items=list(items)[:MAX_ITEMS],
        ))

    # ── 1. Jettone's records ────────────────────────────────────────────
    master_open = (
        await db.execute(
            select(Order).where(
                Order.account_id.in_(master_ids),
                Order.source != OrderSource.SYSTEM,
                Order.status.in_(OPEN_STATUSES),
            )
        )
    ).scalars().all()
    for acc in master_ids:
        rows = [o for o in master_open if o.account_id == acc]
        if rows:
            add(blockers, "PENDING_MASTER_ORDERS", acc,
                f"{len(rows)} Master order(s) still working in Jettone's records. Wait for them to fill "
                "or cancel them in Alice Blue.",
                len(rows), [f"{o.broker_order_id or o.internal_order_id} {o.symbol} {o.status.value}" for o in rows])

    child_open = (
        await db.execute(
            select(Order).where(
                Order.account_id.in_(scope_ids),
                Order.source == OrderSource.SYSTEM,
                Order.status.in_(OPEN_STATUSES),
            )
        )
    ).scalars().all()
    for acc in scope_ids:
        rows = [o for o in child_open if o.account_id == acc]
        if rows:
            partial = sum(1 for o in rows if o.status == OrderStatus.PARTIAL or (o.filled_quantity or 0) > 0)
            no_id = sum(1 for o in rows if not o.broker_order_id)
            detail = f"{len(rows)} copied child order(s) not finished"
            if partial:
                detail += f" ({partial} partially filled)"
            if no_id:
                detail += f" ({no_id} without a broker order id - placement outcome unknown)"
            add(blockers, "PENDING_CHILD_ORDERS", acc,
                detail + ". Wait for fill sync to complete or resolve them in Alice Blue.",
                len(rows), [f"{o.broker_order_id or o.internal_order_id} {o.symbol} {o.status.value}" for o in rows])

    master_order_ids_subq = select(Order.broker_order_id).where(
        Order.account_id.in_(master_ids), Order.broker_order_id.isnot(None)
    )
    in_flight = (
        await db.execute(
            select(CopyTrade).where(
                CopyTrade.status.in_([CopyTradeStatus.PENDING, CopyTradeStatus.SUBMITTED]),
                or_(
                    CopyTrade.follower_account_id.in_(scope_ids),
                    CopyTrade.master_order_id.in_(master_order_ids_subq),
                ),
            )
        )
    ).scalars().all()
    if in_flight:
        by_acc: Dict[uuid.UUID, List[CopyTrade]] = {}
        for t in in_flight:
            by_acc.setdefault(t.follower_account_id, []).append(t)
        for acc, trades in by_acc.items():
            names.setdefault(acc, str(acc))
            add(blockers, "COPY_IN_FLIGHT", acc,
                f"{len(trades)} copy placement(s) still in progress.",
                len(trades), [f"{t.master_order_id} {t.symbol} {t.status.value}" for t in trades])

    day_start = trading_day_start_utc(trading_day(now))
    unresolved_trades = (
        await db.execute(
            select(CopyTrade).where(
                CopyTrade.status == CopyTradeStatus.RECONCILIATION_REQUIRED,
                CopyTrade.follower_account_id.in_(scope_ids),
            )
        )
    ).scalars().all()
    unresolved_records = (
        await db.execute(
            select(ReconciliationRecord).where(
                ReconciliationRecord.resolved.is_(False),
                ReconciliationRecord.account_id.in_(scope_ids),
            )
        )
    ).scalars().all()

    def _is_today(ts: Optional[datetime]) -> bool:
        if ts is None:
            return True  # unknown age: treat as current (fail closed)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts >= day_start

    # Reconciliation records are advisory, never a blocker. They are noisy
    # (crash recovery flags every in-flight copy on each restart, startup
    # reconciliation re-records the same position mismatch, and nothing
    # resolves them), while everything a switch actually depends on - working
    # orders and open positions - is verified LIVE at Alice Blue below. They
    # are shown as warnings so they stay visible.
    for acc in scope_ids:
        t_today = [t for t in unresolved_trades if t.follower_account_id == acc and _is_today(t.created_at)]
        r_today = [r for r in unresolved_records if r.account_id == acc and _is_today(r.created_at)]
        if t_today or r_today:
            add(warnings, "RECONCILIATION_UNRESOLVED", acc,
                f"{len(t_today) + len(r_today)} item(s) from today flagged for review.",
                len(t_today) + len(r_today),
                [f"copy {t.master_order_id} {t.symbol}" for t in t_today]
                + [f"position {r.symbol}" for r in r_today])
        t_old = [t for t in unresolved_trades if t.follower_account_id == acc and not _is_today(t.created_at)]
        r_old = [r for r in unresolved_records if r.account_id == acc and not _is_today(r.created_at)]
        if t_old or r_old:
            add(warnings, "RECONCILIATION_HISTORY", acc,
                f"{len(t_old) + len(r_old)} older item(s) still unresolved.",
                len(t_old) + len(r_old))

    # Carry-forward quantities from Jettone's own fills.
    filled_rows = (
        await db.execute(
            select(Order).where(Order.account_id.in_(scope_ids), Order.filled_quantity > 0)
        )
    ).scalars().all()
    for acc in scope_ids:
        net: Dict[tuple, int] = {}
        unknown: List[str] = []
        for o in (r for r in filled_rows if r.account_id == acc):
            try:
                product = normalize_product_type(o.product_type)
            except UnknownProductTypeError:
                unknown.append(f"{o.symbol} product={o.product_type}")
                continue
            if product not in CARRY_PRODUCTS:
                continue
            key = (o.exchange, o.symbol, product)
            qty = o.filled_quantity if o.side == OrderSide.BUY else -o.filled_quantity
            net[key] = net.get(key, 0) + qty
        open_carry = [f"{sym} {prod} net {q:+d}" for (ex, sym, prod), q in net.items() if q != 0]
        if open_carry or unknown:
            # Advisory only: these totals come from Jettone's own order records,
            # which miss orders placed outside Jettone (a Child's own mobile
            # trades are never recorded) and old fills closed elsewhere, so
            # they can be wrong in both directions. Live orders and positions
            # (below) are what block.
            add(warnings, "CARRY_POSITION_UNVERIFIED", acc,
                "Jettone's records show these delivery holdings. Not verified with Alice Blue.",
                len(open_carry) + len(unknown), open_carry + unknown)

    # ── 2. Live broker state (fail closed) ──────────────────────────────
    system_ids_by_acc: Dict[uuid.UUID, set] = {}
    for acc, boid in (
        await db.execute(
            select(Order.account_id, Order.broker_order_id).where(
                Order.account_id.in_(scope_ids),
                Order.source == OrderSource.SYSTEM,
                Order.broker_order_id.isnot(None),
            )
        )
    ).all():
        system_ids_by_acc.setdefault(acc, set()).add(str(boid))

    new_master_snapshot: Optional[Set[str]] = None
    snapshot_at: Optional[datetime] = None
    for acc in scope_ids:
        client = client_provider(str(acc))
        if client is None:
            add(blockers, "BROKER_VERIFICATION_UNAVAILABLE", acc,
                "The engine has no active broker session for this account, so its orders and positions "
                "cannot be verified. Log the account in and try again.")
            continue

        if not callable(getattr(client, "get_order_book_raw", None)):
            # This broker's client cannot supply the raw order book the preflight verifies against. Say so, and block:
            # never treat "cannot read" as "nothing open".
            add(blockers, "BROKER_VERIFICATION_UNAVAILABLE", acc,
                "This account's broker does not support the order-book verification a Master switch needs, so its "
                "working orders cannot be ruled out.")
            continue

        try:
            book = await client.get_order_book_raw()
        except Exception as exc:  # never let a broker error read as "nothing open"
            logger.warning(f"[MasterSwitch] Order book fetch raised for {acc}: {exc}")
            book = None
        if book is None:
            add(blockers, "BROKER_VERIFICATION_UNAVAILABLE", acc,
                "Alice Blue order book could not be fetched, so working orders cannot be ruled out.")
        else:
            if scope[acc] == "NEW_MASTER":
                new_master_snapshot = {i for i in (order_book_row_id(r) for r in book) if i}
                snapshot_at = datetime.now(timezone.utc)
            working = [r for r in book if _raw_status(r) not in TERMINAL_BROKER_STATUSES]
            if scope[acc] in ("CURRENT_MASTER", "NEW_MASTER"):
                relevant = working
                code, what = "PENDING_MASTER_ORDERS", "working order(s) at Alice Blue on the Master account"
            else:
                known = system_ids_by_acc.get(acc, set())
                relevant = [r for r in working if _raw_order_id(r) in known]
                code, what = "PENDING_CHILD_ORDERS", "copied child order(s) still working at Alice Blue"
            if relevant:
                add(blockers, code, acc,
                    f"{len(relevant)} {what}. Wait for them to fill or cancel them in Alice Blue.",
                    len(relevant), [_raw_label(r) for r in relevant])

        try:
            positions = await client.get_positions_or_none()
        except Exception as exc:
            logger.warning(f"[MasterSwitch] Position fetch raised for {acc}: {exc}")
            positions = None
        if positions is None:
            add(blockers, "BROKER_VERIFICATION_UNAVAILABLE", acc,
                "Alice Blue position book could not be fetched, so open positions cannot be ruled out.")
        else:
            open_pos = [p for p in positions if int(getattr(p, "quantity", 0) or 0) != 0]
            cnc_pos, intraday_pos, blocking_pos = [], [], []
            for p in open_pos:
                try:
                    bucket = normalize_product_for_exchange(p.product_type, getattr(p, "exchange", None))
                except UnknownProductTypeError:
                    bucket = None  # cannot classify -> keeps blocking (fail closed)
                if bucket == "CNC":
                    cnc_pos.append(p)
                elif bucket == "MIS":
                    intraday_pos.append(p)
                else:
                    blocking_pos.append(p)

            def _pos_label(p) -> str:
                return f"{p.symbol} {p.product_type} qty {int(p.quantity):+d}"

            if blocking_pos:
                add(blockers, "OPEN_POSITIONS", acc,
                    f"{len(blocking_pos)} open position(s). Square them off in Alice Blue before switching - "
                    "Jettone never squares off automatically.",
                    len(blocking_pos), [_pos_label(p) for p in blocking_pos])
            if cnc_pos:
                add(warnings, "CNC_POSITIONS_OPEN", acc,
                    f"{len(cnc_pos)} open CNC (delivery) position(s). They stay open in Alice Blue; Jettone "
                    "will not close or transfer them automatically.",
                    len(cnc_pos), [_pos_label(p) for p in cnc_pos])
            if intraday_pos:
                add(warnings, "INTRADAY_POSITIONS_OPEN", acc,
                    f"{len(intraday_pos)} open intraday position(s). Exits for them will not be copied from "
                    "the old Master after the switch.",
                    len(intraday_pos), [_pos_label(p) for p in intraday_pos])

    result = SwitchPreflight(
        strategy_id=str(strategy.id),
        strategy_name=strategy.name,
        current_master_id=str(strategy.master_account_id) if strategy.master_account_id else None,
        new_master_id=str(new_master.id),
        old_master_action=old_master_action,
        allowed=not blockers,
        can_override=bool(blockers) and all(b.code == "BROKER_VERIFICATION_UNAVAILABLE" for b in blockers),
        blockers=blockers,
        warnings=warnings,
        limitations=list(LIMITATIONS),
        checked_accounts=[
            {"account_id": str(a), "account_name": names.get(a, str(a)), "role": r} for a, r in scope.items()
        ],
        requires_cnc_acknowledgement=any(w.code == "CNC_POSITIONS_OPEN" for w in warnings),
    )
    # Kept off the dataclass fields so they never reach the API response.
    result.new_master_order_ids = new_master_snapshot
    result.snapshot_at = snapshot_at
    return result


async def execute_switch(
    db: AsyncSession,
    strategy_id: uuid.UUID,
    new_master_id: uuid.UUID,
    old_master_action: str,
    client_provider: ClientProvider,
    *,
    after_commit: Optional[Callable[["SwitchPreflight"], Any]] = None,
    acknowledge_unverified: bool = False,
    acknowledge_cnc_positions: bool = False,
) -> SwitchPreflight:
    """Re-verifies and commits a Master switch under the switch locks.

    after_commit(preflight) (awaited while the locks are still held) lets the
    caller bring engine monitoring in line before any order for the old or
    new Master can be processed again; preflight.activations() carries the
    new Master's order-book snapshot taken here, under the lock, so its
    monitor can tell its history from orders placed after the switch."""
    current = (
        await db.execute(select(GroupStrategy.master_account_id).where(GroupStrategy.id == strategy_id))
    ).scalar_one_or_none()
    async with membership.accounts_switch_lock([str(current) if current else None, str(new_master_id)]):
        db.expire_all()
        preflight = await run_preflight(db, strategy_id, new_master_id, old_master_action, client_provider)
        override_used = False
        if not preflight.allowed:
            # The only bypass: the broker could not be read AND the user
            # explicitly acknowledged verifying the accounts themselves.
            if not (acknowledge_unverified and preflight.can_override):
                raise SwitchBlocked(preflight)
            override_used = True
            logger.warning(
                f"[MasterSwitch] Switching strategy {strategy_id} with UNVERIFIED broker state "
                f"(user acknowledged): {[(b.account_name, b.message) for b in preflight.blockers]}"
            )

        if preflight.requires_cnc_acknowledgement and not acknowledge_cnc_positions:
            # Decided on the preflight re-run under the locks, so a client can
            # never skip the acknowledgement by not asking for the preflight.
            raise SwitchValidationError(
                409,
                "CNC_ACKNOWLEDGEMENT_REQUIRED",
                "Open CNC positions will stay open in Alice Blue. Acknowledge that Jettone will not close "
                "or transfer them to switch the Master.",
            )

        strategy = await _load_strategy(db, strategy_id)
        old_master_id = strategy.master_account_id
        if old_master_id != current:
            # The Master changed between reading it and taking the locks.
            raise SwitchValidationError(
                409, "STRATEGY_CHANGED", "The strategy's Master changed while switching. Refresh and try again."
            )

        # New Master leaves its Child membership of this strategy, if any.
        for m in list(strategy.members):
            if m.account_id == new_master_id:
                await db.delete(m)
        await membership.release(db, new_master_id)

        created_copy_setting = False
        if old_master_id is not None:
            await membership.release(db, old_master_id)
            if old_master_action == "CHILD":
                # Joins as a Child with Trading ON (copying ON): the user chose
                # "Keep as a Child", and can switch it off from the strategy page.
                db.add(StrategyMember(
                    strategy_id=strategy.id, account_id=old_master_id, trading_enabled=True,
                ))
                await db.flush()
                await membership.assign(db, strategy.id, old_master_id, STRATEGY_ROLE_CHILD)
                created_copy_setting = await membership.sync_copy_enabled(db, old_master_id, True)

        await db.flush()
        await membership.assign(db, strategy.id, new_master_id, STRATEGY_ROLE_MASTER)
        strategy.master_account_id = new_master_id
        await db.flush()

        await emit_system_event(
            db,
            event_type="STRATEGY_MASTER_SWITCHED",
            severity="WARNING" if override_used else "INFO",
            message=(
                f"{'[UNVERIFIED broker state acknowledged by user] ' if override_used else ''}"
                f"Strategy '{strategy.name}' Master switched from {old_master_id or 'none'} to "
                f"{new_master_id}; old Master {'kept as Child (copying on)' if old_master_action == 'CHILD' else 'removed'}."
            ),
            account_id=new_master_id,
            metadata_payload={
                "strategy_id": str(strategy.id),
                "old_master_account_id": str(old_master_id) if old_master_id else None,
                "new_master_account_id": str(new_master_id),
                "old_master_action": old_master_action,
                "old_master_copy_setting_created": created_copy_setting,
                "unverified_override": override_used,
                "unverified_accounts": [b.account_name for b in preflight.blockers] if override_used else [],
            },
        )  # commits

        if after_commit is not None:
            result = after_commit(preflight)
            if hasattr(result, "__await__"):
                await result
        return preflight
