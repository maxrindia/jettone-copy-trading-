"""
Ambiguous (TIMEOUT / UNKNOWN) manual sends: shared guard, claim lock, explicit resolution, message and event.

A manual master order or a manual square-off whose broker call ended without a usable answer may or may not
exist at the broker. Recording it as REJECTED invites the user to send it again, which for a square-off can
reverse the position and for a master LIMIT can replicate a second order to every Child. Instead it is stored as
an UNRESOLVED order - Order PENDING with no broker id and broker_status "TIMEOUT", the same shape the copy
pipeline uses for an ambiguous child send - and new manual orders / square-offs for the same account and symbol
are refused while it is outstanding.

SCOPE: manual master orders (source INTERNAL_UI) and manual square-offs (source SYSTEM, no master order). Copy-pipeline
child orders (source SYSTEM WITH a master_broker_order_id) are deliberately NOT handled here: their ambiguous sends have
a CopyTrade (TIMEOUT) and their own safety path (the sweeper's CRITICAL TIMEOUT_UNRESOLVED alert). Resolving only the
Order would leave that CopyTrade TIMEOUT forever, so the guard, the list and the resolver all ignore or refuse them.

THE BLOCK NEVER EXPIRES ON ITS OWN. The StaleOrderSweeper does not resolve these rows (it only walks CopyTrade
rows and a manual order has none; Alice Blue returns no tag to match on either), and an order of unknown fate must
not be treated as "probably not placed" because time has passed. It is cleared only by an explicit, verified
resolution - `resolve_unresolved_send`, exposed as POST /broker/unresolved-orders/resolve - after the operator has
checked the broker's order book and positions. UNRESOLVED_ESCALATION_MINUTES is only the point after which a still-
unresolved order is escalated (a once-only CRITICAL event and a stronger message); it never unblocks anything.

A PLACED resolution cannot trigger copying: it records the broker's order id on an INTERNAL_UI row, and
MasterOrderPoller never replicates Jettone-originated orders. If the poller already saw the order first it recorded
it as BROKER_EXTERNAL (and replicated it as an external master order); a PLACED resolution then LINKS to that row
(see `resolve_unresolved_send`). If the order was resolved before the poller saw it, the Children were not copied
and that has to be done by hand.

Two simultaneous requests for the same account + symbol must not both pass the guard: `send_claim_lock` serialises
the check-and-pre-write (NOT the broker call) per account + exchange + symbol, so the second request always sees the
first one's pre-written row. It is an in-process lock, which is sufficient because the engine runs a single uvicorn
worker (backend/Dockerfile); with more workers it would need a cross-process mechanism (see the R2C notes).
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import SystemEvent
from app.models.trades import Order, OrderSource, OrderStatus
from app.services.monitoring.notifier import emit_system_event

logger = logging.getLogger(__name__)

UNRESOLVED_ESCALATION_MINUTES = 60
UNRESOLVED_BROKER_STATUS = "TIMEOUT"
OUTCOME_NOT_PLACED = "NOT_PLACED"
OUTCOME_PLACED = "PLACED"
RESOLVE_PATH = "POST /api/v1/broker/unresolved-orders/resolve"


class ResolutionError(Exception):
    """An unresolved send cannot be resolved as requested. `status_code` is the HTTP status to answer with."""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def normalize_symbol(symbol: Optional[str]) -> str:
    """Upper-cased, without the cash-segment "-EQ" suffix (order rows carry both RELIANCE and RELIANCE-EQ)."""
    sym = (symbol or "").strip().upper()
    return sym[:-3] if sym.endswith("-EQ") else sym


def _aware(moment: Optional[datetime]) -> Optional[datetime]:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def is_copy_pipeline_child(order: Order) -> bool:
    """A child order written by the copy pipeline (LIMIT / SL / MARKET paths all set master_broker_order_id). A manual
    square-off is also source SYSTEM but has no master order; a manual master order is INTERNAL_UI."""
    return order.source == OrderSource.SYSTEM and order.master_broker_order_id is not None


def _is_outstanding(order: Order) -> bool:
    return (
        order.status == OrderStatus.PENDING
        and order.broker_order_id is None
        and order.broker_status in (None, UNRESOLVED_BROKER_STATUS)
    )


# ── claim lock: the guard and the pre-write must be one step per account + symbol ──────────────

_claim_locks: Dict[Tuple[str, str, str], List[Any]] = {}      # key -> [lock, holders+waiters]; idle keys are removed


@asynccontextmanager
async def send_claim_lock(account_id: Any, symbol: str, exchange: str) -> AsyncIterator[None]:
    """Held across `find_unresolved_send` + the PENDING pre-write, released BEFORE the broker call. Different
    accounts, symbols and exchanges never wait on each other."""
    key = (str(account_id), normalize_symbol(symbol), (exchange or "").strip().upper())
    entry = _claim_locks.get(key)
    if entry is None:
        entry = _claim_locks[key] = [asyncio.Lock(), 0]
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if entry[1] == 0:
            _claim_locks.pop(key, None)


# ── guard ─────────────────────────────────────────────────────────────────────────────────────

async def find_unresolved_send(
    db: AsyncSession, account_id: Any, symbol: str, exchange: str
) -> Optional[Order]:
    """
    The outstanding unresolved send for this account + exchange + symbol, if any, regardless of its age: an Order
    that is PENDING with no broker id and either broker_status TIMEOUT (an ambiguous send) or none (a send in
    flight, or one whose result could not be recorded). Other accounts, other symbols, orders that reached the
    broker (they have a broker id), finished orders (REJECTED, FAILED, OPEN, ...) and copy-pipeline children never match.
    """
    rows = (
        await db.execute(
            select(Order)
            .where(
                Order.account_id == account_id,
                Order.status == OrderStatus.PENDING,
                Order.broker_order_id.is_(None),
            )
            .order_by(Order.created_at.asc())
        )
    ).scalars().all()
    wanted, wanted_exchange = normalize_symbol(symbol), (exchange or "").strip().upper()
    for order in rows:
        if (
            _is_outstanding(order)
            and not is_copy_pipeline_child(order)
            and normalize_symbol(order.symbol) == wanted
            and (order.exchange or "").strip().upper() == wanted_exchange
        ):
            return order
    return None


def age_minutes(order: Order, now: Optional[datetime] = None) -> float:
    created = _aware(order.created_at)
    if created is None:
        return 0.0
    return max(0.0, ((now or datetime.now(timezone.utc)) - created).total_seconds() / 60.0)


def blocked_detail(order: Order, now: Optional[datetime] = None) -> str:
    minutes = int(age_minutes(order, now))
    overdue = (
        f" It has been unresolved for {minutes} minutes and has been escalated." if minutes >= UNRESOLVED_ESCALATION_MINUTES else ""
    )
    return (
        f"Blocked: an earlier {order.symbol} order on this account (client_order_id={order.internal_order_id}) has an "
        f"unknown broker outcome.{overdue} Check the broker's order book and positions, then clear it with "
        f"{RESOLVE_PATH} (outcome NOT_PLACED or PLACED). It does not clear by itself."
    )


def outcome_unknown_message(what: str, symbol: str, client_order_id: str) -> str:
    return (
        f"{what} outcome UNKNOWN: the broker did not confirm or reject it, so it may have been placed. "
        f"Do NOT retry. Check the broker's order book and positions for {symbol} first "
        f"(client_order_id={client_order_id}). New {symbol} orders on this account stay blocked until you clear it "
        f"with {RESOLVE_PATH} after verifying."
    )


# ── events ────────────────────────────────────────────────────────────────────────────────────

async def emit_unresolved_send_event(
    db: AsyncSession,
    event_type: str,
    account_id: Any,
    message: str,
    metadata: Dict[str, Any],
    severity: str = "CRITICAL",
) -> None:
    """Best effort: a failure to alert must never change the response the user gets."""
    try:
        await emit_system_event(
            db,
            event_type=event_type,
            severity=severity,
            message=message,
            account_id=account_id,
            metadata_payload=metadata,
        )
    except Exception as exc:
        logger.critical(f"[UnresolvedSend] Failed to emit {event_type} SystemEvent: {exc}", exc_info=True)


async def escalate_if_overdue(db: AsyncSession, order: Order, now: Optional[datetime] = None) -> None:
    """Once per order, after UNRESOLVED_ESCALATION_MINUTES: a CRITICAL UNRESOLVED_SEND_ESCALATED event. Escalation
    only alerts - it never unblocks and never changes the order."""
    try:
        if age_minutes(order, now) < UNRESOLVED_ESCALATION_MINUTES:
            return
        existing = (
            await db.execute(
                select(SystemEvent).where(
                    SystemEvent.event_type == "UNRESOLVED_SEND_ESCALATED", SystemEvent.account_id == order.account_id
                )
            )
        ).scalars().all()
        if any((e.metadata_payload or {}).get("client_order_id") == order.internal_order_id for e in existing):
            return
    except Exception as exc:
        logger.error(f"[UnresolvedSend] Could not check escalation state: {exc}")
        return
    await emit_unresolved_send_event(
        db,
        event_type="UNRESOLVED_SEND_ESCALATED",
        account_id=order.account_id,
        message=(
            f"Order {order.internal_order_id} ({order.symbol}) has had an UNKNOWN broker outcome for "
            f"{int(age_minutes(order, now))} minutes and still blocks new {order.symbol} orders on this account. "
            f"Verify it at the broker and clear it with {RESOLVE_PATH}."
        ),
        metadata={
            "account_id": str(order.account_id),
            "client_order_id": order.internal_order_id,
            "symbol": order.symbol,
            "age_minutes": int(age_minutes(order, now)),
        },
    )


# ── explicit resolution ───────────────────────────────────────────────────────────────────────

async def list_unresolved_sends(db: AsyncSession) -> List[Order]:
    rows = (
        await db.execute(
            select(Order)
            .where(Order.status == OrderStatus.PENDING, Order.broker_order_id.is_(None))
            .order_by(Order.created_at.asc())
        )
    ).scalars().all()
    return [o for o in rows if _is_outstanding(o) and not is_copy_pipeline_child(o)]


async def _linkable_external_order(db: AsyncSession, unresolved: Order, broker_order_id: str) -> Optional[Order]:
    """
    For a PLACED resolution: is `broker_order_id` already recorded locally?
      - not at all                                 -> None (record it on the unresolved row, as before)
      - only on THIS account as a BROKER_EXTERNAL row whose symbol (ignoring the cash "-EQ" suffix), side and quantity
        match the unresolved order exactly       -> that row (MasterOrderPoller recorded the real order first)
      - anything else (another account, another source, any mismatch) -> ResolutionError, nothing changed
    No guessing: it never matches on time, price or "close enough".
    """
    rows = (await db.execute(select(Order).where(Order.broker_order_id == broker_order_id))).scalars().all()
    if not rows:
        return None
    other_account = [r for r in rows if r.account_id != unresolved.account_id]
    if other_account:
        raise ResolutionError(
            f"Broker order id {broker_order_id} is already recorded on a different account; nothing changed.", 409
        )
    same = [r for r in rows if r.account_id == unresolved.account_id]
    external = [r for r in same if r.source == OrderSource.BROKER_EXTERNAL]
    if len(same) != 1 or len(external) != 1:
        raise ResolutionError(
            f"Broker order id {broker_order_id} is already recorded for this account (not as a single external order); "
            "nothing changed.",
            409,
        )
    candidate = external[0]
    mismatches = [
        name
        for name, same_value in (
            ("symbol", normalize_symbol(candidate.symbol) == normalize_symbol(unresolved.symbol)),
            ("side", candidate.side == unresolved.side),
            ("quantity", candidate.quantity == unresolved.quantity),
        )
        if not same_value
    ]
    if mismatches:
        raise ResolutionError(
            f"Broker order id {broker_order_id} is already recorded as {candidate.internal_order_id}, which does not match "
            f"this order ({', '.join(mismatches)} differ); nothing changed.",
            409,
        )
    return candidate


async def resolve_unresolved_send(
    db: AsyncSession,
    client_order_id: str,
    outcome: str,
    note: str,
    verified_at_broker: bool,
    broker_order_id: Optional[str] = None,
) -> Order:
    """
    Clears an unresolved send after the operator verified it at the broker. Never places, cancels or replicates
    anything; it only records what the operator found:
      NOT_PLACED  the broker has no such order  -> Order REJECTED (rejection_reason names the operator's note)
      PLACED      the broker holds it           -> Order OPEN with the broker's order id (`broker_order_id` required);
                                                   the normal order sync takes it from there.
                  If MasterOrderPoller already recorded that broker order as a BROKER_EXTERNAL row with the same
                  account, symbol, side and quantity, the unresolved row is instead closed as CANCELLED ("superseded")
                  and the real row is left untouched. Any other already-recorded id is refused.
    Copy-pipeline child orders are refused (409). Resolving never triggers copying.
    The update is conditional on the row still being unresolved, so it cannot overwrite a state set meanwhile.
    """
    if not verified_at_broker:
        raise ResolutionError("verified_at_broker must be true: check the broker's order book and positions first.", 422)
    if outcome not in (OUTCOME_NOT_PLACED, OUTCOME_PLACED):
        raise ResolutionError(f"outcome must be {OUTCOME_NOT_PLACED} or {OUTCOME_PLACED}.", 422)
    note = (note or "").strip()
    if len(note) < 5:
        raise ResolutionError("note is required (what you checked at the broker).", 422)
    broker_order_id = (broker_order_id or "").strip() or None
    if outcome == OUTCOME_PLACED and not broker_order_id:
        raise ResolutionError("broker_order_id is required when the order was placed.", 422)
    if outcome == OUTCOME_NOT_PLACED and broker_order_id:
        raise ResolutionError("broker_order_id must not be given when the order was not placed.", 422)

    order = (await db.execute(select(Order).where(Order.internal_order_id == client_order_id))).scalar_one_or_none()
    if order is None:
        raise ResolutionError(f"No order with client_order_id {client_order_id}.", 404)
    if is_copy_pipeline_child(order):
        raise ResolutionError(
            f"Order {client_order_id} is a copy-pipeline child order (master order {order.master_broker_order_id}); it is "
            "not resolved here. Its CopyTrade (TIMEOUT) and the TIMEOUT_UNRESOLVED alert are the safety path for it; "
            "nothing was changed.",
            409,
        )
    if not _is_outstanding(order):
        raise ResolutionError(
            f"Order {client_order_id} is not an unresolved send (status={order.status.value}, "
            f"broker_order_id={order.broker_order_id}, broker_status={order.broker_status}); nothing changed.",
            409,
        )

    superseding: Optional[Order] = None
    if outcome == OUTCOME_PLACED:
        superseding = await _linkable_external_order(db, order, broker_order_id)

    if superseding is not None:
        values: Dict[str, Any] = {
            "status": OrderStatus.CANCELLED,
            "broker_status": "SUPERSEDED",
            "pending_quantity": 0,
            "rejection_reason": (
                f"Superseded by broker order {broker_order_id} (recorded as {superseding.internal_order_id}). "
                f"Operator note: {note}"
            )[:500],
        }
    elif outcome == OUTCOME_NOT_PLACED:
        values = {
            "status": OrderStatus.REJECTED,
            "broker_status": "REJECTED",
            "pending_quantity": 0,
            "rejection_reason": f"Operator verified not placed at broker: {note}"[:500],
        }
    else:
        values = {
            "status": OrderStatus.OPEN,
            "broker_status": "OPEN",
            "broker_order_id": broker_order_id,
            "pending_quantity": order.quantity,
            "rejection_reason": None,
        }
    previous = {"status": order.status.value, "broker_status": order.broker_status}
    try:
        result = await db.execute(
            update(Order)
            .where(
                Order.id == order.id,
                Order.status == OrderStatus.PENDING,
                Order.broker_order_id.is_(None),
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await db.rollback()
            raise ResolutionError("The order changed while resolving; nothing was changed. Re-check and retry.", 409)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ResolutionError("That broker_order_id is already recorded for this account; nothing changed.", 409)
    await db.refresh(order)

    await emit_unresolved_send_event(
        db,
        event_type="UNRESOLVED_SEND_RESOLVED",
        account_id=order.account_id,
        severity="WARNING",
        message=(
            f"Unresolved order {client_order_id} ({order.symbol}) resolved by the operator as {outcome}: {note}"
        ),
        metadata={
            "account_id": str(order.account_id),
            "client_order_id": client_order_id,
            "symbol": order.symbol,
            "outcome": outcome,
            "broker_order_id": broker_order_id,
            "note": note,
            "previous": previous,
            "superseded_by": superseding.internal_order_id if superseding is not None else None,
        },
    )
    return order
