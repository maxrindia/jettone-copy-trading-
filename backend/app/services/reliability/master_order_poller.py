"""Periodically polls the MASTER account's Alice Blue order book and feeds
externally-originated orders into the normal copy pipeline.

Why this exists
---------------
Orders placed from Alice Blue's own Mobile app / Web terminal never reach
Jettone through the NorenWS WebSocket that engine_loop.py connects to.
Verified in production: with that connection authenticated, subscribed and
heartbeating continuously, seven Mobile-originated orders on the master
account executed at the broker and produced ZERO order frames (no `t:"om"`,
no `t:"o"`) - while the same orders were returned correctly by the REST
order book. Alice Blue's own NorenWS documentation only covers market-data
subscriptions (tick/depth); order-update delivery over that socket is not
documented, and empirically does not happen for this vendor session (not
even for Jettone's own API-placed orders' fills).

REST order-book polling is therefore the only mechanism *proven* to observe
Mobile/Web-originated master orders. This module runs the same
order-book -> OrderUpdateEvent -> detector -> replication path the WebSocket
callback already uses, on a timer, so external master activity is both
persisted and copied. The WebSocket is deliberately left connected and
untouched: if it ever does start delivering order frames, the existing
idempotency layers (Redis `copy:seen:` keys, MasterTradeDetector's
_seen_fills/_limit_order_state, and the copy_trades unique constraint)
absorb the overlap harmlessly.

Two safety rules this module enforces, both deliberately fail-closed
-------------------------------------------------------------------
1. BASELINE SEED PASS. Every order already present in the very first poll is
   recorded as baseline and is NEVER eligible for replication, no matter how
   its status changes later. Without this, starting the poller mid-session
   would see every pre-existing order of the day as brand new and fire real
   follower orders for master activity that happened before the engine was
   watching - the same hazard MasterTradeDetector.seed_state exists to
   prevent, except seed_state cannot help here: it seeds only from persisted
   Order rows, and un-ingested Mobile orders have no such row (that absence
   is the very bug this module fixes). Baseline orders are still persisted,
   so order history becomes complete either way.

2. JETTONE-ORIGINATED ORDERS ARE NEVER RE-REPLICATED. A master order placed
   through Jettone's own manual-order endpoint (source=INTERNAL_UI) was
   already replicated synchronously by app/api/broker.py at placement time.
   Re-emitting it here could double-replicate: the Redis dedup key is
   `copy:seen:{master_order_id}:{fill_sequence}`, and a later poll observing
   the same order as filled derives fill_sequence from cumulative filled
   quantity (detector.py), which only coincides with the LIMIT path's
   fill_sequence=1 when the order happens to be for a single share. Such
   orders are still persisted/updated here (which also keeps the master's
   own fills current - nothing else updates them once the WS is silent),
   they are simply never handed to the copy engine.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, Optional, Set, Tuple

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.types import OrderRecord, OrderUpdateEvent
from app.services.brokers.order_book_read import OrderBookState, read_order_book
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.reliability.restart_recovery import DISPATCH_ACTIONS
from app.services.monitoring.notifier import emit_system_event

logger = logging.getLogger(__name__)

# Alice Blue order-book status -> Order.status. Same mapping used by
# engine_loop.py's persist_and_process_event and FollowerFillSyncer.
#
# TRIGGER_PENDING is Alice Blue's status for a native SL/SL-M order that has
# not yet triggered (confirmed in production, 2026-09-29: master orders
# 26092900007188 and 26092900012070 both showed this status before an
# ordinary status this map already understood). Left unmapped, such an order
# was invisible to Jettone for its entire pending lifetime - no DB row, no
# way to see it modified or cancelled before it triggered - and once it did
# trigger, the resulting MARKET/LIMIT fill was misclassified as a brand-new
# order with the original trigger price and SL/SL-M order_type discarded.
# Mapped to OrderStatus.PENDING (an existing, previously-broker-unused enum
# member - no migration needed) rather than OPEN, so it stays clearly
# distinguishable from a live, working order in the DB and in any UI reading
# Order.status. _is_replication_eligible below treats this status as
# explicitly non-replicable (never inferred from "unmapped").
BROKER_STATUS_MAP: Dict[str, OrderStatus] = {
    "COMPLETE": OrderStatus.COMPLETE,
    "COMPLETED": OrderStatus.COMPLETE,
    "OPEN": OrderStatus.OPEN,
    "PENDING": OrderStatus.OPEN,
    "TRIGGER_PENDING": OrderStatus.PENDING,
    "PARTIAL": OrderStatus.PARTIAL,
    "PARTIALLY_FILLED": OrderStatus.PARTIAL,
    "CANCELLED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
}

# Sources that mean "Jettone placed this itself" - never re-replicated here
# (see rule 2 in the module docstring).
JETTONE_ORIGINATED_SOURCES = {OrderSource.INTERNAL_UI, OrderSource.SYSTEM}


class MasterOrderPoller:
    """
    Polls the master account's broker order book on an interval and pushes
    newly-observed EXTERNAL order activity through `on_order_update` - the
    same callback engine_loop.py hands to the WebSocket client, so detection,
    persistence, risk and replication all behave identically regardless of
    whether an event arrived by socket or by poll.
    """

    def __init__(
        self,
        master_account_id: str,
        client_provider: Callable[[str], Optional[AliceBlueClient]],
        on_order_update: Callable[[OrderUpdateEvent], Any],
        pipeline: CopyEnginePipeline,
        check_interval_seconds: int = 5,
        activation_baseline: Optional[Set[str]] = None,
        activation_deadline: Optional[datetime] = None,
        recovery: Optional[Any] = None,
    ):
        self.master_account_id = str(master_account_id)
        self.client_provider = client_provider
        self.on_order_update = on_order_update
        self.pipeline = pipeline
        self.check_interval_seconds = check_interval_seconds

        # Master-switch activation (see poll_once). When a strategy's Master
        # is switched, the preflight takes this account's full order book
        # under the switch lock, with no working order on it. Those order ids
        # are its history; an order NOT in that snapshot was placed after it,
        # i.e. by the account in its new role, and must not be swallowed by
        # the baseline just because this poller's first poll came later.
        # Only honored until activation_deadline; after that the first poll
        # baselines everything as before and reports what it did not copy.
        self._activation_baseline: Optional[Set[str]] = (
            set(activation_baseline) if activation_baseline is not None else None
        )
        self._activation_deadline = activation_deadline

        # R3b restart recovery (restart_recovery.RestartRecovery), used on the SEED pass only. None = the original
        # behaviour: the whole first book is baselined and nothing is recovered.
        self.recovery = recovery
        # broker_order_id -> CANCEL / MODIFY, for the ONE dispatch the seed pass made for that order; the engine reads
        # it once (pop_recovery_action) to restrict what that dispatch may turn into.
        self._recovery_actions: Dict[str, str] = {}

        self._running = False
        self._task: Optional[asyncio.Task] = None

        # broker_order_id -> _change_key(record) as of the last poll.
        # Membership means "already observed"; the value is what tells a
        # genuine change apart from an unchanged repeat.
        self._seen_state: Dict[str, Tuple[str, int, int, Decimal, Decimal]] = {}
        # Orders present in the very first poll - permanently replication-
        # ineligible (rule 1).
        self._baseline_order_ids: Set[str] = set()
        self._seeded = False

    def pop_recovery_action(self, broker_order_id: str) -> Optional[str]:
        return self._recovery_actions.pop(broker_order_id, None)

    @property
    def is_seeded(self) -> bool:
        """True once the baseline seed pass has completed - the activation
        boundary: only orders first seen AFTER it are ever replicated."""
        return self._seeded

    # ── lifecycle ────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            f"MasterOrderPoller task started (interval={self.check_interval_seconds}s, "
            f"master={self.master_account_id})."
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("MasterOrderPoller task stopped.")

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self.poll_once()
            except Exception as exc:
                logger.error(f"[MasterOrderPoller] Poll loop error: {exc}", exc_info=True)
            await asyncio.sleep(self.check_interval_seconds)

    # ── polling ──────────────────────────────────────────────────────────
    async def poll_once(self) -> int:
        """
        One poll pass. Returns the number of orders dispatched to the copy
        pipeline (0 on the seed pass by construction). A broker/network
        failure logs and returns 0 rather than raising - the loop must keep
        running, and a missed poll is recovered by the next one.
        """
        client = self.client_provider(self.master_account_id)
        if client is None:
            logger.warning(
                f"[MasterOrderPoller] No broker client for master {self.master_account_id}; skipping poll."
            )
            return 0

        is_seed_pass = not self._seeded
        read = await read_order_book(client, checked=is_seed_pass)
        if read.state is OrderBookState.FAILED:
            logger.warning(f"[MasterOrderPoller] Order book fetch failed: {read.detail}")
            return 0
        if not read.is_known:
            if is_seed_pass:
                # The seed pass must be built on a book that was PROVABLY read. get_order_book() turns a network error or a
                # non-200 into an empty list, and an empty baseline would make the next poll treat every existing order as
                # new. Not seeded: the next poll tries again.
                logger.warning("[MasterOrderPoller] Seed-pass order book could not be read reliably; not seeding, will retry.")
            # A later poll whose read cannot be shown to have succeeded (e.g. Alice Blue's [] on failure) changes nothing:
            # exactly what processing that empty list did before. Nothing is removed from the seen state.
            return 0
        book = read.records

        dispatched = 0

        # Activation replay applies to the seed pass only, and only while the
        # snapshot is fresh. In replay mode the snapshot IS the baseline and
        # every other order goes through the normal eligibility rules (so
        # Jettone-originated orders are still never re-replicated).
        replay = False
        missed_activation_orders = []
        if is_seed_pass and self._activation_baseline is not None:
            if self._activation_deadline is None or datetime.now(timezone.utc) <= self._activation_deadline:
                replay = True
                self._baseline_order_ids |= self._activation_baseline
            else:
                logger.warning(
                    f"[MasterOrderPoller] Activation snapshot for {self.master_account_id} expired before the "
                    "first poll; baselining the whole order book (nothing in it is copied)."
                )

        plan = None
        if is_seed_pass and not replay and self.recovery is not None:
            try:
                plan = await self.recovery.plan_seed(self.master_account_id, book)
            except Exception as exc:
                # Fail closed: without a plan this is the original behaviour - baseline everything, copy nothing.
                logger.error(f"[MasterOrderPoller] Restart-recovery planning failed; plain baseline: {exc}", exc_info=True)
                plan = None

        for record in book:
            if not record.broker_order_id:
                continue

            current_state = self._change_key(record)
            if self._seen_state.get(record.broker_order_id) == current_state:
                continue  # nothing changed since the last poll
            self._seen_state[record.broker_order_id] = current_state

            if plan is not None and record.broker_order_id in plan.lifecycle:
                # R3b: an order that ALREADY has Children stays under normal change detection (it is not baselined), so
                # a later cancel / modify still reaches them. What the seed pass itself may dispatch is limited to a
                # cancel or modify of Children that are still working; everything else is just persisted.
                action = plan.lifecycle[record.broker_order_id]
                if action in DISPATCH_ACTIONS and BROKER_STATUS_MAP.get(str(record.status or "").upper()) is not None:
                    self._recovery_actions[record.broker_order_id] = action
                    result = self.on_order_update(self._to_event(record))
                    if asyncio.iscoroutine(result):
                        await result
                    dispatched += 1
                else:
                    await self._persist_only(record)
                continue

            if is_seed_pass and not replay:
                self._baseline_order_ids.add(record.broker_order_id)
                if (
                    self._activation_baseline is not None
                    and record.broker_order_id not in self._activation_baseline
                ):
                    missed_activation_orders.append(record)

            if await self._is_replication_eligible(record, is_seed_pass and not replay):
                logger.info(
                    f"[MasterOrderPoller] External master order observed: "
                    f"{record.broker_order_id} {record.symbol} {self._side_value(record)} "
                    f"{record.order_type} status={record.status} filled={record.filled_quantity}"
                    f"/{record.quantity} -> dispatching to copy pipeline."
                )
                result = self.on_order_update(self._to_event(record))
                if asyncio.iscoroutine(result):
                    await result
                dispatched += 1
            else:
                # Persist/update only: keeps master order history complete
                # (including Mobile/Web orders and Jettone's own fills)
                # without handing anything to the copy engine.
                await self._persist_only(record)

        if is_seed_pass:
            self._seeded = True
            logger.info(
                f"[MasterOrderPoller] Baseline seeded from {len(self._baseline_order_ids)} existing "
                "master order(s); these are persisted but permanently excluded from replication."
                + (f" Activation replay dispatched {dispatched} order(s) placed after the switch." if replay else "")
            )
            if missed_activation_orders:
                await self._report_missed_activation_orders(missed_activation_orders)

        if is_seed_pass and plan is not None and plan.resume:
            # After the seed pass is complete (readiness is not held up). Never raises.
            try:
                await self.recovery.resume(self.master_account_id, plan.resume)
            except Exception as exc:
                logger.error(f"[MasterOrderPoller] Restart-recovery resume failed closed: {exc}", exc_info=True)

        return dispatched

    async def _report_missed_activation_orders(self, records) -> None:
        """Orders placed after the switch snapshot that the expired-snapshot
        fallback baselined: never copied, so never silently - a WARNING
        lists them for manual handling."""
        ids = [r.broker_order_id for r in records]
        try:
            async with AsyncSessionLocal() as session:
                await emit_system_event(
                    session,
                    event_type="MASTER_ACTIVATION_ORDERS_NOT_COPIED",
                    severity="WARNING",
                    message=(
                        f"{len(ids)} order(s) on new Master {self.master_account_id} were placed after the "
                        "Master switch but were first observed too late to be copied safely. They were NOT "
                        "copied to the strategy's Children; handle them manually."
                    ),
                    account_id=uuid.UUID(self.master_account_id),
                    metadata_payload={"broker_order_ids": ids[:50]},
                )
        except Exception as exc:
            logger.error(f"[MasterOrderPoller] Failed to report missed activation orders {ids}: {exc}")

    async def _is_replication_eligible(self, record: OrderRecord, is_seed_pass: bool) -> bool:
        """
        Eligible only when ALL hold:
          - not the seed pass, and not a baseline order (rule 1)
          - the broker status maps to something we understand
          - no Jettone-originated Order row already exists for it (rule 2)

        A native SL/SL-M order in TRIGGER_PENDING (mapped to
        OrderStatus.PENDING) IS eligible here, same as any other
        understood status: it is dispatched to the copy engine, which
        replicates it immediately as the follower's own independent
        SL/SL-M order rather than waiting for the master to trigger - see
        execute_replication's SL_PENDING_CREATED handling. Unknown/
        unmapped statuses remain fail-closed, unchanged from before.
        """
        if is_seed_pass or record.broker_order_id in self._baseline_order_ids:
            return False

        mapped_status = BROKER_STATUS_MAP.get(str(record.status or "").upper())
        if mapped_status is None:
            logger.warning(
                f"[MasterOrderPoller] Unmapped broker status '{record.status}' for order "
                f"{record.broker_order_id}; not dispatching (failing closed)."
            )
            return False

        existing = await self._find_existing_order(record.broker_order_id)
        if existing is not None and existing.source in JETTONE_ORIGINATED_SOURCES:
            return False

        return True

    async def _find_existing_order(self, broker_order_id: str) -> Optional[Order]:
        try:
            acc_uuid = uuid.UUID(self.master_account_id)
        except ValueError:
            return None

        async with AsyncSessionLocal() as session:
            stmt = select(Order).where(
                Order.account_id == acc_uuid,
                Order.broker_order_id == broker_order_id,
            )
            return (await session.execute(stmt)).scalars().first()

    async def _persist_only(self, record: OrderRecord) -> None:
        """
        Upserts the order row without touching the copy engine.

        save_or_update_order resolves an existing row by (broker,
        account_id, broker_order_id) when internal_order_id doesn't match,
        so a Jettone-placed order keeps its original row and `source` -
        the update branch never rewrites source - and only its
        status/fill fields move forward.
        """
        status = BROKER_STATUS_MAP.get(str(record.status or "").upper())
        if status is None:
            return

        try:
            acc_uuid = uuid.UUID(self.master_account_id)
        except ValueError:
            return

        try:
            await self.pipeline.save_or_update_order(
                internal_order_id=f"EXT_{record.broker_order_id}_{self.master_account_id[:8]}",
                broker_order_id=record.broker_order_id,
                account_id=acc_uuid,
                source=OrderSource.BROKER_EXTERNAL,
                exchange=record.exchange or "NSE",
                symbol=record.symbol,
                side=OrderSide.BUY if self._side_value(record) == "BUY" else OrderSide.SELL,
                order_type=record.order_type or "MARKET",
                product_type=record.product_type,
                quantity=record.quantity,
                filled_quantity=record.filled_quantity,
                pending_quantity=max(0, record.quantity - record.filled_quantity),
                price=record.price if record.price > 0 else record.average_price,
                trigger_price=record.trigger_price,
                average_price=record.average_price,
                status=status,
                broker_status=str(record.status or ""),
                rejection_reason=record.rejection_reason,
                instrument_token=record.instrument_id,
                instrument_exchange=getattr(record, "instrument_exchange", None),
            )
        except Exception as exc:
            logger.error(
                f"[MasterOrderPoller] Failed to persist master order {record.broker_order_id}: {exc}",
                exc_info=True,
            )

    def _to_event(self, record: OrderRecord) -> OrderUpdateEvent:
        """Maps a REST OrderRecord into the same OrderUpdateEvent shape the
        WebSocket path produces, so downstream handling is identical."""
        return OrderUpdateEvent(
            broker_order_id=record.broker_order_id,
            client_order_id=record.client_order_id,
            symbol=record.symbol,
            side="BUY" if self._side_value(record) == "BUY" else "SELL",
            quantity=record.quantity,
            filled_quantity=record.filled_quantity,
            fill_price=record.average_price,
            status=BROKER_STATUS_MAP[str(record.status or "").upper()].value,
            timestamp=record.order_timestamp or datetime.now(timezone.utc),
            account_id=self.master_account_id,
            product_type=record.product_type,
            order_type=record.order_type or "MARKET",
            price=record.price,
            trigger_price=record.trigger_price,
            rejection_reason=record.rejection_reason,
            exchange=record.exchange or "NSE",
            instrument_id=record.instrument_id,
            instrument_exchange=getattr(record, "instrument_exchange", None),
        )

    @staticmethod
    def _change_key(record: OrderRecord) -> Tuple[str, int, int, Decimal, Decimal]:
        """
        The per-order fingerprint used to tell a genuine change from an
        unchanged repeat between polls.

        Status and cumulative fill alone are NOT enough. When the master
        modifies a still-pending LIMIT order from the Alice Blue mobile app,
        only its price (and/or quantity) changes - the order stays OPEN with
        filled=0, so a status+fill fingerprint is byte-identical to the
        previous poll and the modification is skipped before it ever reaches
        MasterTradeDetector, which is what silently broke Master MODIFY ->
        Child MODIFY in production (modifyorder was never called once).

        Order quantity, limit price and trigger price are therefore part of
        the fingerprint. Widening it can only ever dispatch MORE often, never
        less, and a redundant dispatch is already absorbed downstream:
        MasterTradeDetector compares its own (quantity, price) state and
        returns None when nothing meaningful changed, and fills are deduped
        by _seen_fills plus the Redis copy:seen: keys and the copy_trades
        unique constraint. Nothing here can by itself cause a duplicate child
        order.
        """
        return (
            str(record.status or "").upper(),
            record.filled_quantity,
            record.quantity,
            record.price,
            record.trigger_price,
        )

    @staticmethod
    def _side_value(record: OrderRecord) -> str:
        raw = str(getattr(record.side, "value", record.side)).upper()
        return "BUY" if raw in ("BUY", "ORDERSIDE.BUY") else "SELL"
