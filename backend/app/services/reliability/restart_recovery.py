"""
R3b: safe recovery after an engine restart.

The Master order poller keeps its state in memory, so the first poll after a restart used to baseline the whole order
book for good: orders placed during the downtime were silently never copied, and a cancel / modify of an order that WAS
already copied was dropped, leaving its Children live. This module classifies each Master order in the first, freshly
fetched book from DURABLE evidence and decides what - if anything - may happen. It never trusts a broker timestamp (the
parsed `orderTime` is label-as-UTC and falls back to "now"), and it never replays a missed order on its own.

Evidence, strongest first (all in existing tables, no migration):
  * Child evidence      - a copy_trades row, or a SYSTEM child Order, for the Master order  -> "already copied";
  * COPY_FANOUT_STARTED - an audit record written before the Children were processed: proof the fanout BEGAN, the routed
                          Children at that moment, and the order's attributes (the only thing that tells a crash before
                          fanout apart from observe-only history, which look the same: a Master Order and nothing else);
  * the persisted Master Order row and the fresh broker book.

What happens (everything else is only REPORTED):
  D2  lifecycle continuity   an already-copied order stays under normal change detection (it is not baselined). A
                             cancel is dispatched only while a Child of it is still working; a modify only when a
                             working Child's price/trigger differs from the book. Both are decided from the CHILDREN's
                             state, so repeating startup repeats nothing once they are applied. Fill progress is just
                             persisted. A LIMIT->MARKET change, a triggered SL, a MARKET fill delta or a quantity
                             change is NEVER replayed: it is baselined and reported for review.
  D3  resume partial fanout  behind ENGINE_RECOVERY_RESUME_ENABLED (default off), a LIMIT / SL-pending creation whose
                             fanout provably began is resumed for the snapshot Children that have no decision yet, only
                             inside a 10-minute window of OUR clock, only if the persisted record and the fresh book
                             agree and the order is still working and unfilled, only with live trading and the master
                             toggle ON. Every Child still goes through the normal gates, runtime pause, risk, and the
                             database claim; only the Redis dedupe check is skipped.
  D4  reports                an order with no persisted row (MASTER_ORDER_MISSED_WHILE_DOWN) or a persisted working
                             order with no fanout evidence (MASTER_ORDER_OBSERVED_NOT_FANNED_OUT). Nothing is copied.

Anything unreadable, inconsistent, stale or unproven results in a report and no order.
"""
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Set

from sqlalchemy import select

from app.core.config import settings
from app.models.audit import AuditLog, SystemEvent
from app.models.trades import CopyTrade, Order, OrderSource, OrderStatus
from app.services import strategy_membership as membership
from app.services.alice_blue.types import OrderRecord
from app.services.copy_engine.child_outcomes import FANOUT_STARTED_EVENT_TYPE, RecoveryScope
from app.services.copy_engine.events import MasterTradeEvent, derive_copy_event_id
from app.services.live_trading import is_live_trading_enabled
from app.services.monitoring.notifier import emit_system_event
from app.services.reliability.emergency_stop import is_master_trading_enabled

logger = logging.getLogger(__name__)

RESUME_WINDOW_SECONDS = 600                 # 10 minutes, measured on OUR clock from the COPY_FANOUT_STARTED record

ACTION_CANCEL = "CANCEL"                    # dispatched: a Child of a cancelled Master order is still working
ACTION_MODIFY = "MODIFY"                    # dispatched: a working Child's price / trigger differs from the book
ACTION_PERSIST = "PERSIST_ONLY"             # copied order, nothing to dispatch (progress / unchanged)
ACTION_REVIEW = "REVIEW"                    # copied order whose change is NOT replayed: baselined and reported
DISPATCH_ACTIONS = frozenset({ACTION_CANCEL, ACTION_MODIFY})

REPORT_MISSED = "MASTER_ORDER_MISSED_WHILE_DOWN"
REPORT_NOT_FANNED = "MASTER_ORDER_OBSERVED_NOT_FANNED_OUT"
REPORT_REVIEW = "MASTER_ORDER_CHANGE_NEEDS_REVIEW"
REPORT_BLOCKED = "RECOVERY_LIFECYCLE_NOT_APPLIED"
REPORT_RESUME_SKIPPED = "RECOVERY_RESUME_SKIPPED"
REPORT_RESUMED = "RECOVERY_FANOUT_RESUMED"

WORKING_BOOK = frozenset({"OPEN", "PARTIAL", "TRIGGER_PENDING"})
WORKING_CHILD = (OrderStatus.OPEN, OrderStatus.PARTIAL, OrderStatus.PENDING)
RESUMABLE_EVENTS = ("LIMIT_CREATED", "SL_PENDING_CREATED")

_TWO_DP = Decimal("0.01")


def _d(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else "0")).quantize(_TWO_DP)
    except Exception:
        return Decimal("NaN")


def _norm_symbol(symbol: Any) -> str:
    text = str(symbol or "").strip().upper()
    return text[:-3] if text.endswith("-EQ") else text


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except Exception:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass
class ResumeCandidate:
    master_order_id: str
    record: OrderRecord
    persisted: Optional[Order]
    meta: Dict[str, Any]
    started_at: Optional[datetime]
    missing: List[str]


@dataclass
class SeedPlan:
    lifecycle: Dict[str, str] = field(default_factory=dict)   # copied order id -> action; NOT baselined (D2)
    resume: List[ResumeCandidate] = field(default_factory=list)
    reports: int = 0


def _session():
    # Looked up at call time so the test suite's patched factory (and any future one) is honoured.
    from app.core import database
    return database.AsyncSessionLocal()


class RestartRecovery:
    def __init__(
        self,
        *,
        pipeline: Any,
        redis: Any,
        clients_provider: Callable[[str], Dict[str, Any]],
        window_seconds: int = RESUME_WINDOW_SECONDS,
    ):
        self.pipeline = pipeline
        self.redis = redis
        self.clients_provider = clients_provider
        self.window_seconds = window_seconds

    # ── reports (deduplicated) ───────────────────────────────────────────────────────────────

    async def _report(
        self, event_type: str, severity: str, message: str, account_id: Any, key: str, details: Dict[str, Any]
    ) -> bool:
        """One SystemEvent per (type, account, key). Best effort; never raises."""
        try:
            async with _session() as session:
                existing = (await session.execute(
                    select(SystemEvent).where(SystemEvent.event_type == event_type, SystemEvent.account_id == account_id)
                )).scalars().all()
                if any((e.metadata_payload or {}).get("dedupe_key") == key for e in existing):
                    return False
                await emit_system_event(
                    session, event_type=event_type, severity=severity, message=message, account_id=account_id,
                    metadata_payload={**details, "dedupe_key": key},
                )
                return True
        except Exception as exc:
            logger.error(f"[Recovery] Could not record {event_type} ({key}): {exc}")
            return False

    async def report_blocked(self, master_account_id: str, broker_order_id: str, action: str, event_type: Optional[str]) -> None:
        await self._report(
            REPORT_BLOCKED, "WARNING",
            f"Recovery found a change on Master order {broker_order_id} ({action}) but the event the detector produced "
            f"({event_type}) is not one recovery may apply. Nothing was applied; review the Children by hand.",
            uuid.UUID(str(master_account_id)), f"{broker_order_id}:{action}:{event_type}",
            {"broker_order_id": broker_order_id, "action": action, "event_type": event_type},
        )

    @staticmethod
    def allows(action: str, trade_event: Any) -> bool:
        """The ONLY event types a recovery-seeded dispatch may turn into: they act on existing Children and create none."""
        etype = getattr(trade_event, "event_type", None)
        if action == ACTION_CANCEL:
            return etype == "ORDER_CANCELLED"
        if action == ACTION_MODIFY:
            return etype in ("ORDER_MODIFIED", "SL_PENDING_MODIFIED")
        return False

    # ── D2 / D4: classify the first, freshly fetched book ────────────────────────────────────

    async def plan_seed(self, master_account_id: str, book: List[OrderRecord]) -> SeedPlan:
        plan = SeedPlan()
        ids = [r.broker_order_id for r in book if r.broker_order_id]
        if not ids:
            return plan
        master_uuid = uuid.UUID(str(master_account_id))
        async with _session() as session:
            persisted = {
                o.broker_order_id: o for o in (await session.execute(
                    select(Order).where(Order.account_id == master_uuid, Order.source != OrderSource.SYSTEM,
                                        Order.broker_order_id.in_(ids)))).scalars().all()
            }
            copied = set((await session.execute(
                select(CopyTrade.master_order_id).where(CopyTrade.master_order_id.in_(ids)).distinct())).scalars().all())
            copied |= set((await session.execute(
                select(Order.master_broker_order_id).where(Order.source == OrderSource.SYSTEM,
                                                           Order.master_broker_order_id.in_(ids)).distinct())).scalars().all())
            kids_by_master: Dict[str, List[Order]] = {}
            if copied:
                for child in (await session.execute(
                        select(Order).where(Order.source == OrderSource.SYSTEM, Order.master_broker_order_id.in_(list(copied))))
                        ).scalars().all():
                    kids_by_master.setdefault(child.master_broker_order_id, []).append(child)
            started = await self._started(session, master_uuid, ids)
            missing_by_master = {oid: await self._missing_children(session, oid, meta) for oid, (meta, _) in started.items()}

        for rec in book:
            oid = rec.broker_order_id
            if not oid:
                continue
            P = persisted.get(oid)
            working = [c for c in kids_by_master.get(oid, []) if c.status in WORKING_CHILD and c.broker_order_id]
            is_working = (rec.status or "").upper() in WORKING_BOOK

            if oid in started and missing_by_master.get(oid):
                meta, started_at = started[oid]
                if meta.get("event_type") in RESUMABLE_EVENTS:
                    plan.resume.append(ResumeCandidate(oid, rec, P, meta, started_at, missing_by_master[oid]))
                elif await self._report(
                    REPORT_RESUME_SKIPPED, "WARNING",
                    f"Master order {oid} ({meta.get('event_type')}) has Children with no decision, but this event type is "
                    "never resumed automatically (a late MARKET copy would be at a stale price).",
                    master_uuid, f"{oid}:{meta.get('event_type')}:{meta.get('fill_sequence')}:NOT_RESUMABLE",
                    {"broker_order_id": oid, "event_type": meta.get("event_type"), "missing_children": missing_by_master[oid]},
                ):
                    plan.reports += 1

            if oid in copied:
                action, why = self._lifecycle_action(P, rec, working)
                if action == ACTION_REVIEW:
                    if await self._report(
                        REPORT_REVIEW, "WARNING",
                        f"Master order {oid} changed while the engine was down ({why}). The change was NOT replayed to its "
                        "Children; review them by hand.",
                        master_uuid, f"{oid}:{why}:{rec.status}:{rec.filled_quantity}:{rec.quantity}:{rec.price}:{rec.trigger_price}",
                        {"broker_order_id": oid, "reason": why, "book_status": rec.status, "book_filled": rec.filled_quantity,
                         "book_quantity": rec.quantity},
                    ):
                        plan.reports += 1
                else:
                    plan.lifecycle[oid] = action
                continue

            if P is None:
                if await self._report(
                    REPORT_MISSED, "WARNING" if is_working else "INFO",
                    f"Master order {oid} ({rec.symbol} {getattr(rec.side, 'value', rec.side)} {rec.quantity}, {rec.status}) is in "
                    "the broker book but was never observed by this engine. It was NOT copied.",
                    master_uuid, oid,
                    {"broker_order_id": oid, "symbol": rec.symbol, "quantity": rec.quantity, "status": rec.status,
                     "order_type": rec.order_type},
                ):
                    plan.reports += 1
            elif oid not in started and is_working:
                if await self._report(
                    REPORT_NOT_FANNED, "WARNING",
                    f"Master order {oid} was observed earlier but has no copy decisions and no record that a fanout began "
                    "(a crash before fanout and observe-only processing look identical). It was NOT copied.",
                    master_uuid, oid,
                    {"broker_order_id": oid, "symbol": rec.symbol, "quantity": rec.quantity, "status": rec.status},
                ):
                    plan.reports += 1
        return plan

    @staticmethod
    def _lifecycle_action(P: Optional[Order], B: OrderRecord, working_children: List[Order]):
        """(action, review_reason) for an order that already has Children. Decided from the CHILDREN's state wherever a
        change would otherwise be re-applied on every restart."""
        b_status = (B.status or "").upper()
        b_type = (B.order_type or "").upper()
        if P is None:
            return ACTION_REVIEW, "NO_PERSISTED_MASTER_ROW"
        if P.source == OrderSource.INTERNAL_UI:
            return ACTION_PERSIST, None                      # Jettone-originated: never re-dispatched (as in a normal run)
        if b_status == "CANCELLED":
            return (ACTION_CANCEL if working_children else ACTION_PERSIST), None
        if b_status == "REJECTED":
            return ACTION_PERSIST, None
        if b_type != (P.order_type or "").upper():
            return ACTION_REVIEW, f"ORDER_TYPE_CHANGED_{(P.order_type or '?').upper()}_TO_{b_type}"
        if b_type in ("SL", "SLM"):
            if P.status == OrderStatus.PENDING and b_status != "TRIGGER_PENDING":
                return ACTION_REVIEW, "SL_TRIGGERED_WHILE_DOWN"
            if b_status == "TRIGGER_PENDING" and any(_d(c.trigger_price) != _d(B.trigger_price) for c in working_children):
                return ACTION_MODIFY, None
            return ACTION_PERSIST, None
        if b_type == "LIMIT":
            if b_status in ("OPEN", "PARTIAL") and any(_d(c.price) != _d(B.price) for c in working_children):
                return ACTION_MODIFY, None
            if B.quantity != P.quantity:
                return ACTION_REVIEW, "QUANTITY_DIFFERS"
            return ACTION_PERSIST, None
        if (B.filled_quantity or 0) > (P.filled_quantity or 0):
            return ACTION_REVIEW, "MARKET_FILL_WHILE_DOWN"
        return ACTION_PERSIST, None

    async def _started(self, session, master_uuid: uuid.UUID, ids: List[str]) -> Dict[str, Any]:
        """latest COPY_FANOUT_STARTED per Master order id -> (metadata, recorded_at). Rows without a readable
        recorded_at are ignored (they cannot be placed inside the window)."""
        rows = (await session.execute(
            select(AuditLog).where(AuditLog.event_type == FANOUT_STARTED_EVENT_TYPE, AuditLog.account_id == master_uuid,
                                   AuditLog.master_order_id.in_(ids)))).scalars().all()
        out: Dict[str, Any] = {}
        for row in rows:
            meta = row.metadata_payload or {}
            at = _parse_time(meta.get("recorded_at"))
            if at is None or not isinstance(meta.get("routed_children"), list):
                continue
            if row.master_order_id not in out or at > out[row.master_order_id][1]:
                out[row.master_order_id] = (meta, at)
        return out

    async def _missing_children(self, session, master_order_id: str, meta: Dict[str, Any]) -> List[str]:
        routed = [str(c) for c in meta.get("routed_children") or []]
        seq = meta.get("fill_sequence", 1)
        if not routed:
            return []
        wanted = {derive_copy_event_id(master_order_id, cid, seq): cid for cid in routed}
        have = set((await session.execute(
            select(CopyTrade.copy_event_id).where(CopyTrade.master_order_id == master_order_id,
                                                  CopyTrade.copy_event_id.in_(list(wanted))))).scalars().all())
        return [cid for ce, cid in wanted.items() if ce not in have]

    # ── D3: resume (only provable, only when enabled) ─────────────────────────────────────────

    async def resume(self, master_account_id: str, candidates: List[ResumeCandidate]) -> int:
        """Returns the number of Children a resume was started for. Never raises."""
        started = 0
        for cand in candidates:
            try:
                started += await self._resume_one(str(master_account_id), cand)
            except Exception as exc:
                logger.error(f"[Recovery] Resume of {cand.master_order_id} failed closed: {exc}", exc_info=True)
        return started

    async def _skip(self, master_account_id: str, cand: ResumeCandidate, reason: str) -> int:
        seq = cand.meta.get("fill_sequence", 1)
        await self._report(
            REPORT_RESUME_SKIPPED, "WARNING",
            f"Master order {cand.master_order_id} has {len(cand.missing)} routed Child(ren) with no decision, and was NOT "
            f"resumed: {reason}.",
            uuid.UUID(master_account_id), f"{cand.master_order_id}:{cand.meta.get('event_type')}:{seq}:{reason}",
            {"broker_order_id": cand.master_order_id, "reason": reason, "missing_children": cand.missing},
        )
        return 0

    async def _resume_one(self, master_account_id: str, cand: ResumeCandidate) -> int:
        meta, B, P = cand.meta, cand.record, cand.persisted
        if not settings.ENGINE_RECOVERY_RESUME_ENABLED:
            return await self._skip(master_account_id, cand, "RESUME_DISABLED")
        if meta.get("event_type") not in RESUMABLE_EVENTS:
            return await self._skip(master_account_id, cand, "EVENT_NOT_RESUMABLE")
        age = (datetime.now(timezone.utc) - cand.started_at).total_seconds() if cand.started_at else None
        if age is None or age < 0 or age > self.window_seconds:
            return await self._skip(master_account_id, cand, "OUTSIDE_10_MINUTE_WINDOW")
        mismatch = self._attribute_mismatch(meta, P, B)
        if mismatch:
            return await self._skip(master_account_id, cand, f"ATTRIBUTES_DIFFER_{mismatch}")
        if not await is_live_trading_enabled():
            return await self._skip(master_account_id, cand, "LIVE_TRADING_OFF")
        if not await is_master_trading_enabled(self.redis):
            return await self._skip(master_account_id, cand, "MASTER_TOGGLE_OFF")
        clients = self.clients_provider(master_account_id) or {}
        # A Child with no usable client is left WITHOUT a decision rather than given a spurious rejection.
        runnable = [cid for cid in cand.missing if clients.get(cid) is not None]
        if not runnable:
            return await self._skip(master_account_id, cand, "NO_CLIENT_FOR_MISSING_CHILDREN")

        event = MasterTradeEvent(
            master_order_id=cand.master_order_id,
            symbol=meta["symbol"], exchange=meta["exchange"], side=meta["side"], quantity=int(meta["quantity"]),
            price=Decimal(str(meta["price"])), product_type=meta["product_type"], timestamp=datetime.now(timezone.utc),
            fill_sequence=int(meta.get("fill_sequence", 1)), account_id=master_account_id,
            order_type=meta["order_type"], limit_price=Decimal(str(meta["limit_price"])),
            trigger_price=Decimal(str(meta["trigger_price"])), event_type=meta["event_type"],
            source=meta.get("source") or "BROKER_EXTERNAL", total_quantity=int(meta["quantity"]), filled_quantity=0,
            pending_quantity=int(meta["quantity"]), status=meta.get("status") or "OPEN",
            instrument_id=meta.get("instrument_id"),
        )
        # Under the same shared gate a normal order takes, so a Master switch cannot interleave with it.
        async with membership.account_order_gate(master_account_id):
            result = await self.pipeline.execute_replication(
                event=event, clients=clients, recovery=RecoveryScope(only_children=frozenset(runnable)),
            )
        await self._report(
            REPORT_RESUMED, "WARNING",
            f"Resumed the interrupted fanout of Master order {cand.master_order_id} for {len(runnable)} Child(ren) with no "
            f"decision yet: {result.get('status')}.",
            uuid.UUID(master_account_id), f"{cand.master_order_id}:{meta.get('event_type')}:{meta.get('fill_sequence', 1)}:"
            f"{','.join(sorted(runnable))}",
            {"broker_order_id": cand.master_order_id, "children": runnable, "pipeline_status": result.get("status")},
        )
        return len(runnable)

    @staticmethod
    def _attribute_mismatch(meta: Dict[str, Any], P: Optional[Order], B: OrderRecord) -> Optional[str]:
        """None when the START record, the persisted Master row and the fresh book all describe the same, unmodified,
        unfilled, still-working order; otherwise the name of the first attribute that disagrees."""
        if P is None:
            return "NO_PERSISTED_ROW"
        otype = str(meta.get("order_type") or "").upper()
        b_status = (B.status or "").upper()
        if otype == "LIMIT":
            if b_status != "OPEN":
                return "NOT_WORKING"
        elif otype in ("SL", "SLM"):
            if b_status != "TRIGGER_PENDING":
                return "NOT_WORKING"
        else:
            return "ORDER_TYPE"
        if (B.order_type or "").upper() != otype or (P.order_type or "").upper() != otype:
            return "ORDER_TYPE"
        if (B.filled_quantity or 0) != 0 or (P.filled_quantity or 0) != 0:
            return "FILLED"
        qty = int(meta.get("quantity") or 0)
        if not (qty == B.quantity == P.quantity):
            return "QUANTITY"
        if not (_norm_symbol(meta.get("symbol")) == _norm_symbol(B.symbol) == _norm_symbol(P.symbol)):
            return "SYMBOL"
        side = str(meta.get("side") or "").upper()
        if not (side == str(getattr(B.side, "value", B.side)).upper() == str(getattr(P.side, "value", P.side)).upper()):
            return "SIDE"
        if str(meta.get("exchange") or "").upper() != str(B.exchange or "").upper():
            return "EXCHANGE"
        if otype == "LIMIT":
            wanted = _d(meta.get("limit_price") if _d(meta.get("limit_price")) > 0 else meta.get("price"))
            if not (wanted == _d(B.price) == _d(P.price)):
                return "PRICE"
        else:
            if not (_d(meta.get("trigger_price")) == _d(B.trigger_price) == _d(P.trigger_price)):
                return "TRIGGER"
            if otype == "SL" and _d(B.price) > 0 and _d(meta.get("price")) != _d(B.price):
                return "PRICE"
        return None
