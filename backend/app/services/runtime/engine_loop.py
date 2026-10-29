"""Live trading runtime: wires Alice Blue WebSocket -> MasterTradeDetector ->
CopyEnginePipeline, plus background reliability loops (stale-order sweeper,
crash recovery) at startup.

Order placement is gated by ENGINE_LIVE_TRADING_ENABLED. When false the loop
runs in observe-only mode: detection and logging happen, no orders are placed.

Multi-Master: every account that is a (non-deleted) strategy's Master gets
its own MasterTradeDetector + MasterOrderPoller ("monitor"); the account
created as account_type MASTER keeps its monitor (and the WebSocket) too,
unless it is currently a strategy's Child. sync_master_monitors() brings the
set of monitors in line with the database at startup and whenever a
strategy's Master changes (app/services/runtime/hooks.py), without a
restart. A newly started monitor's first poll is its activation boundary:
everything already on that account's order book is recorded, never copied.
Which Children an order is copied to is decided per order by strategy
routing (CopyEnginePipeline._get_eligible_followers_for_master), never here.
"""
import asyncio
import json
import logging
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Set, Tuple

import redis.asyncio as aioredis
from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.services.live_trading import event_allowed_when_live_off, is_live_trading_enabled
from app.models.accounts import Account
from app.models.strategies import GroupStrategy, StrategyMember
from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services import strategy_membership as membership
from app.services.alice_blue.instruments import load_and_cache_contract_master
from app.services.alice_blue.manager import account_manager
from app.services.alice_blue.websocket import AliceBlueWebSocketClient
from app.services.copy_engine.child_preview import ENTRY_EVENTS, observe_child_previews
from app.services.copy_engine.detector import MasterTradeDetector
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.monitoring.notifier import emit_system_event
from app.services.reliability.emergency_stop import (
    EmergencyStopService,
    is_master_trading_enabled,
)
from app.services.reliability.follower_fill_syncer import FollowerFillSyncer
from app.services.reliability.master_order_poller import MasterOrderPoller
from app.services.brokers.order_book_read import read_order_book_single
from app.services.brokers.registry import normalize_broker_id
from app.services.reliability.reconciliation import ReconciliationEngine
from app.services.reliability.restart_recovery import RestartRecovery
from app.services.reliability.stale_order_sweeper import StaleOrderSweeper
from app.services.reliability.stop_provider import build_stop_provider
from app.services.runtime import hooks as runtime_hooks

logger = logging.getLogger("engine_loop")

# How long a runtime monitor sync waits for a newly started Master monitor's
# baseline poll, so the API can report whether it is already active.
SEED_WAIT_SECONDS = 15.0


@dataclass
class _MasterMonitor:
    account_id: str
    detector: Any
    poller: Any


class EngineLoop:
    def __init__(self, redis_client: aioredis.Redis):
        self.redis = redis_client
        self._task: Optional[asyncio.Task] = None
        self._ws_client: Optional[AliceBlueWebSocketClient] = None
        self._sweeper: Optional[StaleOrderSweeper] = None
        self._fill_syncer: Optional[FollowerFillSyncer] = None
        # account_id -> its detector + poller. Mutated only under
        # _monitor_lock (sync_master_monitors); read lock-free by event
        # processing (plain dict reads cannot interleave with an await).
        self._monitors: Dict[str, _MasterMonitor] = {}
        self._monitor_lock = asyncio.Lock()
        # Accounts that are currently some strategy's Child: never monitored
        # as a Master, and their order events are never ingested as one.
        self._child_account_ids: Set[str] = set()
        self._legacy_master_id: Optional[str] = None
        self._pipeline: Optional[CopyEnginePipeline] = None
        self._recovery: Optional[RestartRecovery] = None
        self._on_order_update = None
        # P2 #11: per-broker_order_id locks serializing persist_and_process_event
        # so that concurrently-scheduled WS update tasks for the SAME order
        # reach the stateful detector in arrival order. See
        # _serialize_by_broker_order_id for the full reasoning. Refcounted so
        # entries are dropped the instant no task is holding/waiting on a
        # given key, keeping this bounded by in-flight orders, not by every
        # broker_order_id ever seen over the process's lifetime.
        self._order_processing_locks: Dict[str, asyncio.Lock] = {}
        self._order_processing_lock_refcounts: Dict[str, int] = {}
        # Observe-preview dedupe only: bounded, in-memory, never persisted (see _log_observe_preview).
        self._observe_previewed: "OrderedDict[Tuple[str, str, str], None]" = OrderedDict()

    OBSERVE_PREVIEW_DEDUPE_MAX = 2048

    async def start(self) -> None:
        runtime_hooks.set_engine_loop(self)
        self._task = asyncio.create_task(self._run())
        logger.info("EngineLoop task created.")

    # Upper bound on one observe-only preview; past it the preview is abandoned and only a "skipped" line is logged.
    OBSERVE_PREVIEW_TIMEOUT_SECONDS = 5.0

    @asynccontextmanager
    async def _order_gate_then_preview(self, source_id: str, pending: list):
        """The Master's shared order gate (strategy_membership.account_order_gate), exactly as before, followed - only after the gate
        is RELEASED, in the same task - by the observe-only preview of the event the body queued in `pending` (at most one, and only
        on the live-trading-OFF return). A Master switch therefore never waits on a preview. If the body raises, the exception
        propagates as before and nothing is previewed."""
        async with membership.account_order_gate(source_id):
            yield
        if pending:
            await self._log_observe_preview(pending[0], source_id)

    async def _log_observe_preview(self, trade_event: Any, source_id: str) -> None:
        """Live Trading OFF only: log what each candidate Child would face for a new Master entry ([ObservePreview], one JSON line
        per account; copy_engine/child_preview.py). Read-only - no pipeline, client, broker call or database write - and bounded:
        any error or a slow database only skips the log line, it never changes the observe-only outcome."""
        if not settings.OBSERVE_CHILD_PREVIEW_ENABLED or str(trade_event.event_type or "").upper() not in ENTRY_EVENTS:
            return
        # One preview per logical Master order and entry event: a repeated MARKET_ACCEPTED (one per partial fill of the same
        # order) or a re-delivered update is not previewed again. A different order id or event type is always previewed.
        key = (str(source_id), str(trade_event.master_order_id), str(trade_event.event_type or "").upper())
        if key in self._observe_previewed:
            return
        self._observe_previewed[key] = None
        while len(self._observe_previewed) > self.OBSERVE_PREVIEW_DEDUPE_MAX:
            self._observe_previewed.popitem(last=False)
        try:
            records = await asyncio.wait_for(observe_child_previews(trade_event, source_id),
                                             timeout=self.OBSERVE_PREVIEW_TIMEOUT_SECONDS)
        except Exception as exc:
            logger.warning(f"[ObservePreview] skipped ({type(exc).__name__}); observe-only behaviour unchanged")
            return
        for record in records:
            logger.info("[ObservePreview] " + json.dumps(record, default=str, sort_keys=True))

    async def stop(self) -> None:
        if runtime_hooks.get_engine_loop() is self:
            runtime_hooks.set_engine_loop(None)
        if self._ws_client:
            await self._ws_client.close()
        if self._sweeper:
            await self._sweeper.stop()
        if self._fill_syncer:
            await self._fill_syncer.stop()
        for monitor in list(self._monitors.values()):
            await monitor.poller.stop()
        self._monitors.clear()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("EngineLoop stopped.")

    def _build_stop_provider(self):
        return build_stop_provider(self.redis, account_ids=None)

    @asynccontextmanager
    async def _serialize_by_broker_order_id(self, broker_order_id: Optional[str]):
        """
        P2 #11: `handle_order_update()` spawns an independent, uncoordinated
        asyncio.Task (via asyncio.create_task) for EVERY incoming WS order
        update. Inside that task, `await pipeline.save_or_update_order(...)`
        (a real DB round-trip) happens BEFORE the synchronous, stateful
        `detector.process_order_update(event)` call, which mutates
        MasterTradeDetector's `_limit_order_state`/`_seen_fills` dicts, keyed
        by broker_order_id. Two rapid back-to-back WS updates for the SAME
        broker_order_id (e.g. a LIMIT order created, then almost immediately
        modified) are two independent tasks - under real Postgres network
        latency (not reproducible against this project's fast in-memory
        SQLite test DB), the SECOND task's DB write can resolve before the
        FIRST's, so the detector would process the modification before the
        creation, corrupting its per-order state and emitting replication
        events with quantities/prices exactly swapped.

        This wraps the guarded section in a per-broker_order_id asyncio.Lock
        so tasks for the SAME order are forced back into arrival (FIFO)
        order at the point they'd otherwise race, while tasks for DIFFERENT
        broker_order_ids are entirely unaffected and keep running fully
        concurrently - only related, same-order work is serialized.

        Bounded memory: a naive "one lock per broker_order_id, forever"
        dict would grow for the life of the process. Instead this keeps a
        refcount per key of how many tasks currently hold or are queued on
        that key's lock, and drops the lock/refcount entries the instant the
        count reaches zero - so memory is bounded by orders with in-flight
        processing at any instant, not by every broker_order_id ever seen.

        Concurrency-safety of the bookkeeping itself: every dict read/write
        below happens within a single synchronous statement (no `await`
        inside it), so nothing else can interleave mid-operation under
        asyncio's single-threaded cooperative scheduler - unlike threads,
        an asyncio task is only ever suspended at an explicit `await`. (A
        plain `dict.setdefault(key, asyncio.Lock())` would in fact be
        equally safe for the same reason - the refcounting here is needed
        regardless, for the cleanup behavior above.)
        """
        if not broker_order_id:
            # No stable key to serialize on (shouldn't happen for a real WS
            # update - OrderUpdateEvent.broker_order_id is a required str -
            # but fail open to "no serialization" rather than crash).
            yield
            return

        lock = self._order_processing_locks.get(broker_order_id)
        if lock is None:
            lock = asyncio.Lock()
            self._order_processing_locks[broker_order_id] = lock
            self._order_processing_lock_refcounts[broker_order_id] = 0
        self._order_processing_lock_refcounts[broker_order_id] += 1
        try:
            async with lock:
                yield
        finally:
            remaining = self._order_processing_lock_refcounts.get(broker_order_id, 1) - 1
            if remaining <= 0:
                self._order_processing_locks.pop(broker_order_id, None)
                self._order_processing_lock_refcounts.pop(broker_order_id, None)
            else:
                self._order_processing_lock_refcounts[broker_order_id] = remaining

    async def _run(self) -> None:
        logger.info("EngineLoop starting: initializing broker clients...")

        await account_manager.initialize()

        auth_results = await account_manager.authenticate_all()
        failed = [acc for acc, ok in auth_results.items() if not ok]
        if failed:
            logger.warning(f"Authentication failed for accounts: {failed}")
            async with AsyncSessionLocal() as db:
                await emit_system_event(
                    db,
                    event_type="ACCOUNT_AUTH_FAILED",
                    severity="ERROR",
                    message=f"Startup authentication failed for {len(failed)} account(s).",
                    metadata_payload={"account_ids": failed},
                )

        # The account created as account_type MASTER keeps the WebSocket and
        # its monitor, as before; strategy Masters are monitored in addition
        # (sync_master_monitors below). Having no such account is no longer
        # fatal - strategy Masters are still monitored.
        master_client = account_manager.get_master_client()
        self._legacy_master_id = str(master_client.account_id) if master_client is not None else None
        if master_client is None:
            logger.warning(
                "No active account_type MASTER account; only strategy Masters will be monitored "
                "and no WebSocket is opened."
            )

        # Crash recovery: flag in-flight trades from previous runs, reconcile.
        # account_manager.get_client lets run_crash_recovery fetch each
        # account's real broker positions during reconciliation instead of
        # silently comparing against an assumed-empty position map.
        try:
            async with AsyncSessionLocal() as db:
                recovered = await ReconciliationEngine.run_crash_recovery(
                    db, client_provider=account_manager.get_client
                )
            logger.info(f"Crash recovery processed {recovered} in-flight trade(s).")
        except Exception as exc:
            logger.error(f"Crash recovery failed at startup: {exc}", exc_info=True)

        # Reliability loops
        self._sweeper = StaleOrderSweeper(
            order_book_provider=self._fetch_order_book_for_account,
        )
        await self._sweeper.start()

        # Pre-cache official Alice Blue contract master in background - NSE
        # and NFO. NFO was missing entirely (only NSE was ever auto-synced),
        # which left real NFO contracts (NIFTY/BANKNIFTY/FINNIFTY options,
        # stock futures, etc.) with no numeric instrumentId source beyond the
        # small static KNOWN_INSTRUMENTS table's stale example strikes -
        # see InstrumentResolutionError in alice_blue/instruments.py.
        asyncio.create_task(load_and_cache_contract_master(redis=self.redis, exchange="NSE"))
        asyncio.create_task(load_and_cache_contract_master(redis=self.redis, exchange="NFO"))

        validator = EventValidator(redis=self.redis)
        pipeline = CopyEnginePipeline(
            validator=validator,
            stop_provider=self._build_stop_provider(),
        )
        # Follower fill tracking: nothing else observes what happens to a
        # follower's order after it's placed (only the master gets a
        # persistent WebSocket connection, above). Polls each follower's
        # order book and keeps Order rows in sync - see module docstring for
        # why this updates Order only, never CopyTrade.
        # Every registered account, not just account_type FOLLOWER ones: any
        # account can be a strategy's Child, and a Child's copied orders must
        # be fill-synced whatever its account_type. The syncer only ever
        # touches SYSTEM-sourced (Jettone-placed child) Order rows.
        self._fill_syncer = FollowerFillSyncer(
            follower_clients_provider=account_manager.get_all_clients_map,
            pipeline=pipeline,
        )
        await self._fill_syncer.start()

        # Live Trading is a runtime switch (UI), re-read for every event below;
        # this only logs where it stands at startup.
        live_mode = await is_live_trading_enabled()
        logger.info(
            f"EngineLoop mode: {'LIVE TRADING' if live_mode else 'OBSERVE-ONLY (no orders will be placed)'} "
            "(switchable at runtime from the app)."
        )

        async def persist_and_process_event(event):
            try:
                # P2 #11: serialize the DB-write-then-detector-call sequence
                # per broker_order_id so concurrently-scheduled tasks for the
                # SAME order can't reach the stateful detector out of arrival
                # order. See _serialize_by_broker_order_id for the full
                # reasoning. Different broker_order_ids are unaffected.
                async with self._serialize_by_broker_order_id(event.broker_order_id):
                    if str(event.account_id) in self._child_account_ids and str(event.account_id) not in self._monitors:
                        # A strategy Child's order stream is Jettone's own
                        # copies (tracked by FollowerFillSyncer). Ingesting
                        # one here as BROKER_EXTERNAL before its SYSTEM row
                        # has a broker order id would create a duplicate row.
                        logger.debug(
                            f"[EngineLoop] Ignoring order event {event.broker_order_id} for strategy Child "
                            f"{event.account_id}; child orders are synced by FollowerFillSyncer."
                        )
                        return
                    # 1. Update/Persist Order in DB
                    account_id_uuid = uuid.UUID(event.account_id)
                    status_mapped = {
                        "COMPLETE": OrderStatus.COMPLETE,
                        "OPEN": OrderStatus.OPEN,
                        "PARTIAL": OrderStatus.PARTIAL,
                        "PENDING": OrderStatus.PENDING,
                        "CANCELLED": OrderStatus.CANCELLED,
                        "REJECTED": OrderStatus.REJECTED,
                    }.get(event.status, OrderStatus.OPEN)

                    internal_id = f"EXT_{event.broker_order_id}_{str(account_id_uuid)[:8]}"
                    await pipeline.save_or_update_order(
                        internal_order_id=internal_id,
                        broker_order_id=event.broker_order_id,
                        account_id=account_id_uuid,
                        source=OrderSource.BROKER_EXTERNAL,
                        exchange=event.exchange or "NSE",
                        symbol=event.symbol,
                        side=OrderSide.BUY if event.side == "BUY" else OrderSide.SELL,
                        order_type=event.order_type or "MARKET",
                        product_type=event.product_type,
                        quantity=event.quantity,
                        filled_quantity=event.filled_quantity,
                        pending_quantity=max(0, event.quantity - event.filled_quantity),
                        price=event.price if event.price > 0 else event.fill_price,
                        trigger_price=event.trigger_price,
                        average_price=event.fill_price,
                        status=status_mapped,
                        broker_status=event.status,
                        rejection_reason=event.rejection_reason,
                        instrument_token=event.instrument_id,
                        instrument_exchange=getattr(event, "instrument_exchange", None),
                    )

                    # 2. Only an account currently monitored as a Master is
                    # ever replicated from (never replicate child orders!).
                    source_id = str(event.account_id)
                    monitor = self._monitors.get(source_id)
                    if monitor is None:
                        logger.debug(f"[EngineLoop] Saved external child order for account {event.account_id}. No replication.")
                        return

                    # 3. Master account: detect + route + copy, holding this
                    # Master's order gate (shared) so a Master switch can
                    # never interleave with it.
                    observe_pending: list = []
                    async with self._order_gate_then_preview(source_id, observe_pending):
                        if self._monitors.get(source_id) is not monitor:
                            # The Master was switched away while this event
                            # waited for the switch to finish: it now belongs
                            # to no strategy as Master, so it is not copied -
                            # but never silently.
                            logger.warning(
                                f"[EngineLoop] {event.broker_order_id} from {source_id} arrived during a Master "
                                "switch; account is no longer monitored as a Master. Not replicated."
                            )
                            async with AsyncSessionLocal() as db:
                                await emit_system_event(
                                    db,
                                    event_type="ORDER_DURING_MASTER_SWITCH_NOT_COPIED",
                                    severity="WARNING",
                                    message=(
                                        f"Order {event.broker_order_id} ({event.symbol} {event.side} {event.status}) "
                                        f"from account {source_id} arrived while that account stopped being a "
                                        "strategy Master. It was recorded but NOT copied."
                                    ),
                                    account_id=account_id_uuid,
                                    metadata_payload={
                                        "broker_order_id": event.broker_order_id,
                                        "account_id": source_id,
                                        "status": event.status,
                                    },
                                )
                            return
                        detector = monitor.detector

                        # R3b: the one CANCEL / MODIFY the poller's seed pass dispatched for this order (if any). It may
                        # only become an event that acts on existing Children - never a new order.
                        pop_action = getattr(monitor.poller, "pop_recovery_action", None)
                        recovery_action = pop_action(event.broker_order_id) if callable(pop_action) else None
                        if not isinstance(recovery_action, str):
                            recovery_action = None

                        trade_event = detector.process_order_update(event)
                        if trade_event is None:
                            return
                        if recovery_action is not None and not RestartRecovery.allows(recovery_action, trade_event):
                            logger.warning(
                                f"[EngineLoop] Restart recovery: {recovery_action} of {event.broker_order_id} produced "
                                f"{trade_event.event_type}, which recovery may not apply. Not applied."
                            )
                            if self._recovery is not None:
                                await self._recovery.report_blocked(
                                    source_id, event.broker_order_id, recovery_action, trade_event.event_type
                                )
                            return

                        # Re-evaluated per event so the UI switch applies at once.
                        # OFF: no new order, no modification; a master CANCELLATION
                        # may still propagate (it only reduces exposure).
                        if not await is_live_trading_enabled() and not event_allowed_when_live_off(
                            trade_event.event_type
                        ):
                            logger.info(
                                f"[EngineLoop][live-trading OFF] Would process {trade_event.event_type} for {trade_event.symbol} "
                                f"{trade_event.side} qty={trade_event.quantity}"
                            )
                            observe_pending.append(trade_event)          # previewed after the gate is released
                            return

                        if not await is_master_trading_enabled(self.redis):
                            logger.info(
                                f"[EngineLoop] Master trading toggle is OFF; skipping replication of {trade_event.master_order_id}."
                            )
                            return

                        # Read per event, not once at startup, so followers
                        # activated/deactivated at runtime (sync_account) apply.
                        # Every registered account except the order's own
                        # source; strategy routing picks the actual Children.
                        result = await pipeline.execute_replication(
                            event=trade_event,
                            clients=account_manager.get_clients_map_excluding(source_id),
                        )
                        logger.info(f"[EngineLoop] Replicated {trade_event.master_order_id} ({trade_event.event_type}): {result.get('status')}")

                        # Only AFTER a real follower SL/SL-M child has actually
                        # been created do we tell the detector this master order
                        # is "known" - never eagerly at detection time. This is
                        # what lets the master's eventual trigger be recognized
                        # as belonging to the SAME already-replicated order
                        # (ORDER_FILLED) instead of creating a second, duplicate
                        # child (see MasterTradeDetector.mark_sl_child_created).
                        if trade_event.event_type == "SL_PENDING_CREATED":
                            child_results = result.get("results") if isinstance(result, dict) else None
                            if isinstance(child_results, dict) and any(
                                isinstance(r, dict) and r.get("status") == "SUCCESS" for r in child_results.values()
                            ):
                                detector.mark_sl_child_created(trade_event.master_order_id)
            except Exception as exc:
                logger.error(f"[EngineLoop] Error processing order event {event.broker_order_id}: {exc}", exc_info=True)

        def handle_order_update(event):
            asyncio.create_task(persist_and_process_event(event))

        self._pipeline = pipeline
        self._on_order_update = handle_order_update
        # R3b: classifies each Master's first order book after a (re)start. See restart_recovery.py.
        self._recovery = RestartRecovery(
            pipeline=pipeline,
            redis=self.redis,
            clients_provider=lambda master_id: account_manager.get_clients_map_excluding(master_id),
        )

        # REST order-book polling for every monitored Master. This is the only
        # mechanism proven to observe orders placed from Alice Blue's own
        # Mobile app / Web terminal - the NorenWS socket below delivers no
        # order frames for them (see master_order_poller.py's docstring for
        # the production evidence). It feeds the SAME persist_and_process_event
        # callback the socket uses, so detection/risk/replication behave
        # identically no matter which path observed the order first;
        # duplicates across both paths are absorbed by the existing Redis
        # `copy:seen:` dedup, the detector's own state, and the copy_trades
        # unique constraint. The socket is deliberately left connected.
        # One detector + poller per monitored Master (see
        # sync_master_monitors). Not waiting for baseline polls at startup:
        # each poller's own first pass is its baseline regardless.
        await self.sync_master_monitors(seed_timeout_seconds=0)

        if master_client is None:
            return

        self._ws_client = AliceBlueWebSocketClient(master_client)
        self._ws_client.on_order_update = handle_order_update

        logger.info("EngineLoop connecting to master order stream...")
        try:
            async for _event in self._ws_client.connect_and_listen():
                pass  # events are handled via callback; generator drives the connection
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(f"EngineLoop WS stream terminated unexpectedly: {exc}", exc_info=True)

    @staticmethod
    async def _master_broker_for(account_id: str) -> Optional[str]:
        """Canonical broker id of a Master account, or None when it cannot be read (then Alice Blue's unchanged keys are used,
        which is also what every Master was before multi-broker support). Read-only."""
        try:
            async with AsyncSessionLocal() as db:
                acc = await db.get(Account, uuid.UUID(str(account_id)))
            return normalize_broker_id(acc.broker) if acc is not None else None
        except Exception as exc:
            logger.warning(f"[EngineLoop] Could not read the broker of Master {account_id} ({type(exc).__name__}); using the legacy key scope.")
            return None

    async def _build_detector(self, account_id: str) -> MasterTradeDetector:
        """A Master's detector, seeded from its durable Order rows (moved
        unchanged from _run so every monitored Master is seeded the same
        way the single master always was)."""
        detector = MasterTradeDetector(master_account_id=account_id)
        detector.master_broker = await self._master_broker_for(account_id)         # scopes this Master's event keys (identity.py)

        # Issue #5: seed detector state from durable Order rows so a
        # restart doesn't reset cumulative-fill tracking to zero (which
        # would replicate an order's entire historical fill as if brand
        # new) or lose LIMIT-order comparison state (which would
        # misclassify an already-replicated, still-open order as newly
        # created). SYSTEM-sourced rows are excluded - those are follower
        # orders, never the master's own. See MasterTradeDetector.seed_state
        # for exactly what is and is not safely recoverable this way.
        try:
            async with AsyncSessionLocal() as db:
                seed_stmt = select(Order).where(
                    Order.account_id == uuid.UUID(str(account_id)),
                    Order.source != OrderSource.SYSTEM,
                    # PENDING included alongside OPEN/PARTIAL so a native
                    # SL/SL-M order still TRIGGER_PENDING at restart is
                    # re-seeded into _sl_pending_state (see seed_state) -
                    # a master-account row can only be PENDING via that
                    # status, since PENDING as a follower pre-write status
                    # only ever applies to SYSTEM-sourced rows, already
                    # excluded above.
                    Order.status.in_([OrderStatus.OPEN, OrderStatus.PARTIAL, OrderStatus.PENDING]),
                )
                seed_orders = (await db.execute(seed_stmt)).scalars().all()

                # Restart-safety for the LIMIT->MARKET modification edge case:
                # a master order that was replicated while still LIMIT, then
                # changed to MARKET, then survives a restart before it fills,
                # has order_type=MARKET on its own Order row by the time we
                # seed above - so seed_state's `order_type == "LIMIT"` check
                # alone can no longer recognize it as already-known. The
                # durable, order_type-independent fact that it already has a
                # child is the existence of a SYSTEM-sourced (follower) Order
                # row whose master_broker_order_id points back at it - so we
                # look that up separately here and pass it through.
                seed_broker_order_ids = [
                    o.broker_order_id for o in seed_orders if getattr(o, "broker_order_id", None)
                ]
                known_child_master_order_ids: Set[str] = set()
                if seed_broker_order_ids:
                    child_stmt = select(Order.master_broker_order_id).where(
                        Order.source == OrderSource.SYSTEM,
                        Order.master_broker_order_id.in_(seed_broker_order_ids),
                    ).distinct()
                    known_child_master_order_ids = {
                        row[0] for row in (await db.execute(child_stmt)).all() if row[0]
                    }
            detector.seed_state(seed_orders, known_child_master_order_ids=known_child_master_order_ids)
            logger.info(
                f"Seeded MasterTradeDetector from {len(seed_orders)} open/partial master Order row(s), "
                f"{len(known_child_master_order_ids)} with a durable child order on record."
            )
        except Exception as exc:
            logger.error(f"Failed to seed MasterTradeDetector state from Order rows: {exc}", exc_info=True)
        return detector

    async def _desired_master_ids(self) -> Tuple[Set[str], Set[str]]:
        """(accounts to monitor as Master, accounts that are strategy Children)."""
        async with AsyncSessionLocal() as db:
            masters = {
                str(row[0])
                for row in (
                    await db.execute(
                        select(GroupStrategy.master_account_id).where(
                            GroupStrategy.deleted_at.is_(None),
                            GroupStrategy.master_account_id.isnot(None),
                        )
                    )
                ).all()
            }
            children = {
                str(row[0])
                for row in (
                    await db.execute(
                        select(StrategyMember.account_id)
                        .join(GroupStrategy, GroupStrategy.id == StrategyMember.strategy_id)
                        .where(GroupStrategy.deleted_at.is_(None))
                    )
                ).all()
            }
        desired = set(masters)
        if self._legacy_master_id and self._legacy_master_id not in children:
            desired.add(self._legacy_master_id)
        conflicted = desired & children
        if conflicted:
            # Prevented by strategy_account_roles; never guessed at. Such an
            # account is not monitored as a Master, so nothing is copied
            # from it (fail closed).
            logger.error(
                f"[EngineLoop] Accounts listed as both a strategy Master and a Child: {sorted(conflicted)}. "
                "Not monitoring them as Masters."
            )
        return desired - conflicted, children

    async def sync_master_monitors(
        self,
        *,
        seed_timeout_seconds: float = SEED_WAIT_SECONDS,
        activations: Optional[Dict[str, Tuple[Set[str], datetime]]] = None,
    ) -> None:
        """Brings the running Master monitors in line with the database.

        Stops the monitor of every account that is no longer a Master and
        starts one (detector seeded from DB + poller whose first pass is the
        baseline) for every newly assigned Master. Unrelated monitors are
        left untouched. On a database error the current monitors are kept
        as they are.

        activations: {account_id: (order ids, deadline)} from a Master
        switch's preflight snapshot. A NEWLY started monitor for such an
        account uses that snapshot as its baseline (activation replay - see
        MasterOrderPoller) instead of everything its own first poll sees,
        so orders the new Master places while the switch completes are
        copied rather than swallowed. Ignored for monitors already running."""
        activations = activations or {}
        if self._pipeline is None or self._on_order_update is None:
            return  # _run has not wired the pipeline yet; it syncs itself.
        started = []
        async with self._monitor_lock:
            try:
                desired, children = await self._desired_master_ids()
            except Exception as exc:
                logger.error(f"[EngineLoop] Could not read strategy Masters; monitors unchanged: {exc}", exc_info=True)
                return
            self._child_account_ids = children

            for account_id in [a for a in self._monitors if a not in desired]:
                monitor = self._monitors.pop(account_id)
                try:
                    await monitor.poller.stop()
                finally:
                    logger.info(f"[EngineLoop] Stopped Master monitor for {account_id}.")

            for account_id in sorted(desired - set(self._monitors)):
                detector = await self._build_detector(account_id)
                activation_ids, activation_deadline = activations.get(account_id, (None, None))
                poller = MasterOrderPoller(
                    master_account_id=account_id,
                    client_provider=account_manager.get_client,
                    on_order_update=self._on_order_update,
                    pipeline=self._pipeline,
                    check_interval_seconds=settings.MASTER_ORDER_POLL_INTERVAL_SECONDS,
                    activation_baseline=activation_ids,
                    activation_deadline=activation_deadline,
                    # A monitor started by a Master SWITCH has its own activation snapshot; restart recovery is for
                    # a monitor that starts without one.
                    recovery=self._recovery if activation_ids is None else None,
                )
                self._monitors[account_id] = _MasterMonitor(account_id, detector, poller)
                await poller.start()
                started.append(poller)
                logger.info(f"[EngineLoop] Started Master monitor for {account_id}.")

        if started and seed_timeout_seconds > 0:
            deadline = asyncio.get_running_loop().time() + seed_timeout_seconds
            while asyncio.get_running_loop().time() < deadline and not all(
                getattr(p, "is_seeded", False) for p in started
            ):
                await asyncio.sleep(0.2)

    def is_master_monitor_ready(self, account_id: str) -> bool:
        monitor = self._monitors.get(str(account_id))
        return bool(monitor is not None and getattr(monitor.poller, "is_seeded", False))

    def monitored_master_ids(self) -> Set[str]:
        return set(self._monitors)

    @staticmethod
    async def _fetch_order_book_for_account(account_id: str):
        """The StaleOrderSweeper's order book for one Child account: the rows when the book was PROVABLY read, else None.

        None is the sweeper's existing "unavailable, leave the trade as it is" answer. Before, a missing client and Alice Blue's
        failed read (which returns []) both arrived as [], so every TIMEOUT trade of that account was reported CRITICAL
        "not found in broker order book" although nothing had been read. A genuinely empty book still returns [] and is still
        treated as a real absence."""
        read = await read_order_book_single(account_manager.get_client(account_id))      # one broker call for a real Alice client
        if not read.is_known:
            logger.warning(f"[EngineLoop] Order book for {account_id} not available to the sweeper ({read.reason}"
                           f"{': ' + read.detail if read.detail else ''}); its TIMEOUT trades are left for the next run.")
            return None
        return list(read.records)
