"""
R3: per-Child execution tracking for a Master order fanout.

Every Child that routing hands an order to ("routed") ends the event with exactly one CopyTrade row, keyed by the
existing unique triple (master_order_id, follower_account_id, copy_event_id):

  * a Child that exits BEFORE the send (disabled/paused, zero lot, funds or risk rejection, SELL block) gets a
    terminal row: SKIPPED for a local decision, REJECTED for a funds/risk/safety rejection, `quantity` 0, no
    client_order_id, with the reason as a stable code in `failure_reason` ("CODE" or "CODE: detail");
  * a Child that proceeds to the send CLAIMS its row first (R2B claim-before-send) and the row then follows the send:
    PENDING -> SUBMITTED / REJECTED / FAILED / TIMEOUT.

Routed vs. excluded. "Routed" Children are the strategy members `_get_eligible_followers_for_master` returns: the member's
trading is enabled, the account is active, not emergency-stopped, not deleted, and its broker may receive copies.
They get a row each. Every other member of the strategy (except the Master itself) is "excluded": it is listed with
its reason in the fanout summary only, and gets NO CopyTrade row, so deliberately disabled members never inflate the
per-account counts that read CopyTrade.

Re-claiming. A pre-send terminal row (SKIPPED/REJECTED, no client_order_id, no broker id, never submitted) may be
claimed ONCE by compare-and-set when the same event is processed again and passes the gates this time. That can only
happen after the Redis `copy:seen` key was lost (the validator sets it per Master event, before any Child is looked
at, and it lives 24 hours), so it never bypasses the first-line deduplication. It is also bounded: a row older than
RECLAIM_WINDOW_SECONDS stands as a final decision. A row that was ever claimed (client_order_id set) is never
re-claimed - it is a duplicate, exactly as in R2B.

SELL-safety blocks. The SELL guard (`_record_sell_safety_rejection`) already writes the Child's row itself, as
RECONCILIATION_REQUIRED with the safety code in `failure_reason`. That row is kept as it is: the insert here finds the
unique triple taken and does nothing, so there is still exactly one row per Child.

The fanout summary is an advisory AuditLog row, NOT an exactly-once record: if an event is processed twice (Redis
loss) a second summary can be written. The CopyTrade rows are the authority.
"""
import enum
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog
from app.models.trades import CopyTrade, CopyTradeStatus, OrderSide

logger = logging.getLogger(__name__)

RECLAIM_WINDOW_SECONDS = 600
SUMMARY_EVENT_TYPE = "COPY_FANOUT_SUMMARY"
FANOUT_STARTED_EVENT_TYPE = "COPY_FANOUT_STARTED"

# Reason codes written by the pipeline's own gates are carried through unchanged (FUNDS_FETCH_FAILED,
# RISK_INPUTS_UNAVAILABLE, REFERENCE_PRICE_UNAVAILABLE, INSUFFICIENT_LONG_POSITION, ...). These are the codes R3 adds.
TRADING_DISABLED = "TRADING_DISABLED"
ZERO_LOT_RESULT = "ZERO_LOT_RESULT"
RISK_REJECTED = "RISK_REJECTED"
NO_CLIENT = "NO_CLIENT"
EXCEPTION = "EXCEPTION"
DUPLICATE_EVENT = "DUPLICATE_EVENT"

# Why a strategy member is excluded from a fanout (summary only).
MEMBER_TRADING_DISABLED = "MEMBER_TRADING_DISABLED"
ACCOUNT_MISSING = "ACCOUNT_MISSING"
ACCOUNT_INACTIVE = "ACCOUNT_INACTIVE"
ACCOUNT_EMERGENCY_STOPPED = "ACCOUNT_EMERGENCY_STOPPED"
ACCOUNT_DELETED = "ACCOUNT_DELETED"
CHILD_BROKER_NOT_READY = "CHILD_BROKER_NOT_READY"


@dataclass(frozen=True)
class RecoveryScope:
    """A deliberate restart-recovery fanout (R3b): only these Children (account ids as strings) may be routed, and the
    Redis event-deduplication check - the only check skipped - does not apply. Everything else, including the database
    claim, still does."""
    only_children: frozenset


def fanout_started_metadata(event: Any, strategy_id: Optional[uuid.UUID], routed_ids: List[str]) -> Dict[str, Any]:
    """Everything a later recovery needs to prove what was started and to rebuild the event. Values are JSON-safe."""
    return {
        "v": 1,
        "master_order_id": event.master_order_id,
        "master_account_id": str(event.account_id) if event.account_id else None,
        "strategy_id": str(strategy_id) if strategy_id else None,
        "event_type": event.event_type,
        "fill_sequence": event.fill_sequence,
        "order_type": event.order_type,
        "symbol": event.symbol,
        "exchange": event.exchange,
        "side": event.side,
        "quantity": event.quantity,
        "price": str(event.price),
        "limit_price": str(event.limit_price),
        "trigger_price": str(event.trigger_price),
        "product_type": event.product_type,
        "instrument_id": event.instrument_id,
        "status": event.status,
        "source": event.source,
        "routed_children": list(routed_ids),
        "recorded_at": datetime.now(timezone.utc).isoformat(),      # OUR clock; never the broker's timestamp
    }


async def record_fanout_started(
    session: AsyncSession, *, event: Any, strategy_id: Optional[uuid.UUID], routed_ids: List[str]
) -> bool:
    """
    Durable proof that a fanout BEGAN, written before any Child task starts. An observe-only Master order and a crash
    before fanout leave the same rows (a Master Order and nothing else); this record is what tells them apart. It
    places nothing. If it cannot be written the caller carries on, and that event simply never qualifies for
    automatic recovery.
    """
    session.add(AuditLog(
        event_type=FANOUT_STARTED_EVENT_TYPE,
        account_id=uuid.UUID(str(event.account_id)) if event.account_id else None,
        master_order_id=event.master_order_id,
        symbol=event.symbol,
        exchange=event.exchange,
        side=event.side,
        quantity=event.quantity,
        price=event.price,
        reason_code=f"{event.event_type}#{event.fill_sequence}"[:100],
        metadata_payload=fanout_started_metadata(event, strategy_id, routed_ids),
    ))
    await session.commit()
    return True


def format_reason(code: str, detail: Optional[str] = None) -> str:
    detail = (detail or "").strip()
    return f"{code}: {detail}" if detail and detail != code else code


def parse_reason(text: Optional[str]) -> Tuple[Optional[str], str]:
    """(code, detail) from a failure_reason written by format_reason; (None, text) for free text."""
    if not text:
        return None, ""
    head, sep, tail = text.partition(": ")
    if head and head == head.upper() and " " not in head and head.replace("_", "").replace(",", "").isalnum():
        return head, tail if sep else ""
    return None, text


def excluded_reason(member: Any) -> Optional[str]:
    """Why a strategy member is NOT routed an order (None = it is eligible on the DB-state checks)."""
    account = getattr(member, "account", None)
    if not getattr(member, "trading_enabled", False):
        return MEMBER_TRADING_DISABLED
    if account is None:
        return ACCOUNT_MISSING
    if not account.is_active:
        return ACCOUNT_INACTIVE
    if account.is_emergency_stopped:
        return ACCOUNT_EMERGENCY_STOPPED
    if account.deleted_at is not None:
        return ACCOUNT_DELETED
    return None


def pre_send_outcome(result: Any) -> Optional[Tuple[CopyTradeStatus, str, str]]:
    """
    Maps a per-Child result dict returned BEFORE the claim to (status, code, detail), or None when the result is not a
    pre-send exit (a claimed row already exists for it, or it is a duplicate).
      {"status": "SKIPPED",  "reason": X}       -> SKIPPED  X          (DUPLICATE_EVENT is not an outcome)
      {"status": "REJECTED", "reasons": [..]}   -> REJECTED code      (one reason: itself; several: RISK_REJECTED)
    A broker rejection or NO_CLIENT carries no "reasons" list and is finalised on its claimed row instead.
    """
    if not isinstance(result, dict):
        return None
    status = str(result.get("status") or "")
    if status == "SKIPPED":
        reason = result.get("reason")
        if not reason or reason == DUPLICATE_EVENT:
            return None
        return CopyTradeStatus.SKIPPED, str(reason), ""
    if status == "REJECTED":
        reasons = result.get("reasons")
        if isinstance(reasons, (list, tuple)) and reasons:
            codes = [str(r) for r in reasons]
            return CopyTradeStatus.REJECTED, (codes[0] if len(codes) == 1 else RISK_REJECTED), ",".join(codes)
    return None


def describe_result(result: Any) -> Tuple[str, Optional[str]]:
    """(status, code) of a per-Child result for the summary."""
    if not isinstance(result, dict):
        return "UNKNOWN", None
    status = str(result.get("status") or "UNKNOWN")
    outcome = pre_send_outcome(result)
    if outcome is not None:
        return status, outcome[1]
    code = result.get("reason")
    if status == "EXCEPTION":
        code = EXCEPTION
    return status, str(code) if code else None


async def record_pre_send_outcome(
    session: AsyncSession,
    *,
    master_order_id: str,
    follower_account_id: uuid.UUID,
    copy_event_id: uuid.UUID,
    symbol: str,
    exchange: str,
    side: str,
    product_type: str,
    estimated_price: Any,
    status: CopyTradeStatus,
    code: str,
    detail: str = "",
) -> bool:
    """Insert the Child's terminal pre-send row. Insert-only: if a row for the triple exists (a claimed row, or an
    earlier pre-send row) nothing is written and False is returned."""
    session.add(CopyTrade(
        id=uuid.uuid4(),
        master_order_id=master_order_id,
        follower_account_id=follower_account_id,
        copy_event_id=copy_event_id,
        symbol=symbol,
        exchange=exchange,
        side=OrderSide.BUY if str(side).upper() == "BUY" else OrderSide.SELL,
        quantity=0,
        product_type=product_type,
        estimated_price=estimated_price,
        client_order_id=None,
        status=status,
        failure_reason=format_reason(code, detail)[:500],
        completed_at=datetime.now(timezone.utc),
    ))
    try:
        await session.commit()
        return True
    except IntegrityError:
        await session.rollback()
        return False


class ClaimKind(str, enum.Enum):
    WON = "WON"              # a fresh claim row was inserted
    RECLAIMED = "RECLAIMED"  # a pre-send terminal row was taken over by compare-and-set
    LOST = "LOST"            # the event was already handled: a duplicate


class Claim:
    __slots__ = ("kind", "trade_id")

    def __init__(self, kind: ClaimKind, trade_id: Optional[uuid.UUID]):
        self.kind, self.trade_id = kind, trade_id

    @property
    def won(self) -> bool:
        return self.kind != ClaimKind.LOST


async def claim_child_trade(
    session: AsyncSession,
    *,
    master_order_id: str,
    follower_account_id: uuid.UUID,
    copy_event_id: uuid.UUID,
    symbol: str,
    exchange: str,
    side: OrderSide,
    quantity: int,
    product_type: str,
    estimated_price: Any,
    client_order_id: str,
    trade_id: Optional[uuid.UUID] = None,
    now: Optional[datetime] = None,
) -> Claim:
    """
    The R2B claim, extended. Insert the PENDING row; on a unique-constraint hit, take over a pre-send terminal row by
    compare-and-set (see the module docstring), otherwise report LOST. Nothing is sent by this function.
    """
    trade_id = trade_id or uuid.uuid4()
    session.add(CopyTrade(
        id=trade_id,
        master_order_id=master_order_id,
        follower_account_id=follower_account_id,
        copy_event_id=copy_event_id,
        symbol=symbol,
        exchange=exchange,
        side=side,
        quantity=quantity,
        product_type=product_type,
        estimated_price=estimated_price,
        client_order_id=client_order_id,
        status=CopyTradeStatus.PENDING,
    ))
    try:
        await session.commit()
        return Claim(ClaimKind.WON, trade_id)
    except IntegrityError:
        await session.rollback()

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=RECLAIM_WINDOW_SECONDS)
    try:
        result = await session.execute(
            update(CopyTrade)
            .where(
                CopyTrade.master_order_id == master_order_id,
                CopyTrade.follower_account_id == follower_account_id,
                CopyTrade.copy_event_id == copy_event_id,
                CopyTrade.status.in_([CopyTradeStatus.SKIPPED, CopyTradeStatus.REJECTED]),
                CopyTrade.client_order_id.is_(None),
                CopyTrade.broker_order_id.is_(None),
                CopyTrade.submitted_at.is_(None),
                CopyTrade.created_at >= cutoff,
            )
            .values(
                status=CopyTradeStatus.PENDING,
                symbol=symbol,
                exchange=exchange,
                side=side,
                quantity=quantity,
                product_type=product_type,
                estimated_price=estimated_price,
                client_order_id=client_order_id,
                failure_reason=None,
                completed_at=None,
                retry_count=CopyTrade.retry_count + 1,
            )
            .execution_options(synchronize_session=False)
        )
        if result.rowcount != 1:
            await session.rollback()
            return Claim(ClaimKind.LOST, None)
        await session.commit()
    except IntegrityError:                      # e.g. the client_order_id belongs to another row: fail closed
        await session.rollback()
        return Claim(ClaimKind.LOST, None)

    existing = await session.execute(
        select(CopyTrade.id).where(
            CopyTrade.master_order_id == master_order_id,
            CopyTrade.follower_account_id == follower_account_id,
            CopyTrade.copy_event_id == copy_event_id,
        )
    )
    return Claim(ClaimKind.RECLAIMED, existing.scalar_one())


async def finalize_child_trade(session: AsyncSession, client_order_id: str, order_res: Any) -> bool:
    """
    Finalise a LIMIT / SL child's claimed row from a NON-ambiguous broker result (the pipeline records an ambiguous
    TIMEOUT / UNKNOWN through its unresolved-send path and never calls this):
      SUCCESS -> SUBMITTED (broker id kept; the fill syncer moves it to its terminal status)
      REJECTED -> REJECTED,  anything else (provably unsent NETWORK_ERROR) -> FAILED.
    Only a row still PENDING is changed, so a later state is never overwritten.
    """
    category = getattr(getattr(order_res, "category", None), "value", None)
    now = datetime.now(timezone.utc)
    if category == "SUCCESS":
        values: Dict[str, Any] = dict(status=CopyTradeStatus.SUBMITTED, broker_order_id=order_res.broker_order_id,
                                      broker_response=order_res.raw_response, failure_reason=None)
    else:
        status = CopyTradeStatus.REJECTED if category == "REJECTED" else CopyTradeStatus.FAILED
        values = dict(status=status, broker_response=order_res.raw_response, completed_at=now,
                      failure_reason=format_reason(category or "FAILED", getattr(order_res, "message", ""))[:500])
    result = await session.execute(
        update(CopyTrade)
        .where(CopyTrade.client_order_id == client_order_id, CopyTrade.status == CopyTradeStatus.PENDING)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return result.rowcount == 1


async def fail_unsent_claim(
    session: AsyncSession, *, master_order_id: str, follower_account_id: uuid.UUID, copy_event_id: uuid.UUID, code: str
) -> bool:
    """A claimed row whose send was never attempted (for example NO_CLIENT): provably unsent, so FAILED. Never touches a
    row that has a submitted_at."""
    result = await session.execute(
        update(CopyTrade)
        .where(
            CopyTrade.master_order_id == master_order_id,
            CopyTrade.follower_account_id == follower_account_id,
            CopyTrade.copy_event_id == copy_event_id,
            CopyTrade.status == CopyTradeStatus.PENDING,
            CopyTrade.submitted_at.is_(None),
        )
        .values(status=CopyTradeStatus.FAILED, failure_reason=format_reason(code, "request never sent"),
                completed_at=datetime.now(timezone.utc))
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return result.rowcount == 1


async def record_fanout_summary(
    session: AsyncSession,
    *,
    event: Any,
    strategy_id: Optional[uuid.UUID],
    routed: List[Tuple[Any, Any]],
    excluded: List[Dict[str, str]],
    results: Dict[str, Any],
    row_statuses: Optional[Dict[str, str]] = None,
    recovery: bool = False,
) -> bool:
    """
    One advisory AuditLog row for a Master event: the routed Children with their outcomes, and the excluded members.
    Not written for a pure replay (every Child answered DUPLICATE_EVENT). It is NOT exactly-once: reprocessing after a
    Redis loss may add another row. The CopyTrade rows are the authority: `status`/`code` describe what the pipeline
    returned for the Child, `copy_trade_status` is the status its row actually has (they differ, for example, for a
    SELL-safety block, which the SELL guard records itself as RECONCILIATION_REQUIRED).
    """
    children: Dict[str, Dict[str, Any]] = {}
    counts: Dict[str, int] = {}
    for follower, _ in routed:
        key = str(follower.id)
        status, code = describe_result(results.get(key))
        children[key] = {"status": status, "code": code, "copy_trade_status": (row_statuses or {}).get(key)}
        counts[status] = counts.get(status, 0) + 1
    if children and all(c["code"] == DUPLICATE_EVENT for c in children.values()):
        return False
    session.add(AuditLog(
        event_type=SUMMARY_EVENT_TYPE,
        account_id=uuid.UUID(str(event.account_id)) if event.account_id else None,
        master_order_id=event.master_order_id,
        symbol=event.symbol,
        side=event.side,
        quantity=event.quantity,
        price=event.price,
        reason_code=f"{event.event_type}#{event.fill_sequence}"[:100],
        metadata_payload={
            "strategy_id": str(strategy_id) if strategy_id else None,
            "event_type": event.event_type,
            "fill_sequence": event.fill_sequence,
            "routed_count": len(children),
            "excluded": excluded,
            "children": children,
            "counts": counts,
            "authoritative_source": "copy_trades",
            "recovery": recovery,
            "note": "advisory; may be written again if the event is reprocessed",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        },
    ))
    await session.commit()
    return True
