import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderStatus
from app.services.monitoring.notifier import emit_system_event
from app.services.alice_blue.types import OrderRecord

logger = logging.getLogger(__name__)

UNRESOLVED_REASON = "TIMEOUT not found in broker order book"

TERMINAL_BROKER_STATUSES = {"COMPLETE", "REJECTED", "CANCELLED"}

# Native SL/SL-M orders legitimately sit TRIGGER_PENDING at the broker for
# minutes to hours while waiting for their trigger price — the default 120s
# staleness threshold produces false RECONCILIATION_REQUIRED flags for these
# (confirmed against live production data on 2026-09-30: real SL orders
# pending 4-11 minutes before triggering and completing correctly). This is
# a wider ceiling, not an exemption — it still eventually surfaces a
# genuinely orphaned pending SL for manual review instead of leaving it
# invisible forever.
SL_ORDER_TYPES = {"SL", "SLM"}
SL_PENDING_STALE_THRESHOLD_SECONDS = 3600


class StaleOrderSweeper:
    """
    Two responsibilities:
    1. Flag PENDING/SUBMITTED rows untouched beyond the staleness threshold.
    2. Resolve TIMEOUT rows against the broker order book (by client_order_id):
       a timed-out request may still have executed at the broker — never re-send it.
    """

    def __init__(
        self,
        stale_threshold_seconds: int = 120,
        check_interval_seconds: int = 60,
        order_book_provider=None,
        timeout_grace_seconds: int = 30,
        sl_pending_stale_threshold_seconds: int = SL_PENDING_STALE_THRESHOLD_SECONDS,
    ):
        self.stale_threshold_seconds = stale_threshold_seconds
        self.check_interval_seconds = check_interval_seconds
        self.timeout_grace_seconds = timeout_grace_seconds
        self.sl_pending_stale_threshold_seconds = sl_pending_stale_threshold_seconds
        # order_book_provider(account_id) -> list[OrderRecord]
        self.order_book_provider = order_book_provider
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def _is_pending_sl_awaiting_trigger(self, db: AsyncSession, trade: CopyTrade) -> bool:
        """True only if this candidate's follower Order is a native SL/SL-M
        still sitting TRIGGER_PENDING at the broker — the one case that gets
        the longer SL_PENDING_STALE_THRESHOLD_SECONDS ceiling instead of the
        normal stale_threshold_seconds. Matches by (master_broker_order_id,
        account_id) rather than CopyTrade.broker_order_id, since that column
        is not populated at creation for LIMIT/SL child rows (see
        follower_fill_syncer.py's docstring for the same gap). No match, or
        a match that isn't a genuinely-pending SL/SLM, keeps the existing
        120s behavior — fails closed rather than risking a permanent blind
        spot.

        Keyed on Order.status == OrderStatus.PENDING, NOT Order.broker_status
        == "TRIGGER_PENDING" (confirmed broken live, 2026-10-01, master
        26100100168532 / follower 26100100168552): a native SL/SL-M child's
        broker_status is written ONCE at placement to the literal placement-
        acknowledgement string "SUCCESS" (see pipeline.py's
        _process_single_sl_follower), never the broker's actual lifecycle
        state - and follower_fill_syncer's own "skip the write when nothing
        changed" optimization means it is never corrected to a real broker
        status string while the order remains genuinely, unchangedly
        pending. That makes broker_status permanently unreliable for this
        specific determination. Order.status, by contrast, is the already-
        normalized internal enum: it is set correctly to OrderStatus.PENDING
        at placement and - per this codebase's existing, already-
        established convention (see master_order_poller.py's BROKER_STATUS_
        MAP) - PENDING is reserved exclusively for "native
        SL/SL-M awaiting trigger" and is moved off PENDING the moment the
        order genuinely completes, is cancelled, or is rejected. It is
        therefore the authoritative, reliable signal for this check."""
        stmt = (
            select(Order)
            .where(
                Order.master_broker_order_id == trade.master_order_id,
                Order.account_id == trade.follower_account_id,
            )
            .order_by(Order.created_at.desc())
            .limit(1)
        )
        matched = (await db.execute(stmt)).scalar_one_or_none()
        if matched is None:
            return False
        return (
            (matched.order_type or "").upper() in SL_ORDER_TYPES
            and matched.status == OrderStatus.PENDING
        )

    async def sweep_once(self, db: AsyncSession) -> int:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=self.stale_threshold_seconds)

        stmt = (
            select(CopyTrade)
            .where(
                CopyTrade.status.in_([CopyTradeStatus.PENDING, CopyTradeStatus.SUBMITTED]),
                CopyTrade.updated_at < cutoff,
            )
        )
        result = await db.execute(stmt)
        stale_trades = result.scalars().all()

        count = 0
        for trade in stale_trades:
            if await self._is_pending_sl_awaiting_trigger(db, trade):
                elapsed = (now - trade.updated_at).total_seconds()
                if elapsed < self.sl_pending_stale_threshold_seconds:
                    continue  # still within the wider native-SL grace window

            trade.status = CopyTradeStatus.RECONCILIATION_REQUIRED
            trade.failure_reason = f"Stale order untouched for >{self.stale_threshold_seconds}s"

            await emit_system_event(
                db,
                event_type="STALE_ORDER_FLAGGED",
                severity="WARNING",
                message=f"CopyTrade {trade.id} marked RECONCILIATION_REQUIRED due to inactivity",
                metadata_payload={
                    "trade_id": str(trade.id),
                    "master_order_id": trade.master_order_id,
                    "follower_account_id": str(trade.follower_account_id),
                    "updated_at": trade.updated_at.isoformat() if trade.updated_at else None,
                },
                commit=False,
            )
            count += 1

        if count > 0:
            await db.commit()
            logger.warning(f"StaleOrderSweeper flagged {count} orders as RECONCILIATION_REQUIRED.")

        resolved = await self._resolve_timeouts(db)
        return count + resolved

    async def _resolve_timeouts(self, db: AsyncSession) -> int:
        """
        Attempts to resolve TIMEOUT copy trades using the broker order book.
        Requires an order_book_provider; otherwise leaves them for manual review.

        Selection uses the moment the order may have reached the broker:
        submitted_at, falling back to completed_at then updated_at for rows
        written before submitted_at was populated (it was never set before).

        Never resubmits. Each outcome is applied with a compare-and-set on
        status == TIMEOUT, so two sweeper runs (or a run racing another
        writer) cannot overwrite each other's result.
        """
        if self.order_book_provider is None:
            return 0

        grace_cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.timeout_grace_seconds)
        submitted_marker = func.coalesce(CopyTrade.submitted_at, CopyTrade.completed_at, CopyTrade.updated_at)
        stmt = (
            select(CopyTrade)
            .where(
                CopyTrade.status == CopyTradeStatus.TIMEOUT,
                submitted_marker < grace_cutoff,
            )
            .order_by(submitted_marker)
            .limit(50)
        )
        result = await db.execute(stmt)
        timeout_trades = result.scalars().all()

        resolved = 0
        flagged_unresolved = 0
        book_cache: dict = {}

        for trade in timeout_trades:
            account_key = str(trade.follower_account_id)
            if account_key not in book_cache:
                try:
                    book_cache[account_key] = await self.order_book_provider(account_key)
                except Exception as exc:
                    logger.error(f"Sweeper order-book fetch failed for {account_key}: {exc}")
                    book_cache[account_key] = None

            book = book_cache[account_key]
            if book is None:
                continue

            match = self._find_by_client_order_id(book, trade.client_order_id)
            if match is None:
                if (trade.failure_reason or "").startswith(UNRESOLVED_REASON):
                    continue  # already flagged once; stays TIMEOUT for manual review, no repeat alert
                flagged_unresolved += 1
                await db.execute(
                    update(CopyTrade)
                    .where(CopyTrade.id == trade.id, CopyTrade.status == CopyTradeStatus.TIMEOUT)
                    .values(failure_reason=f"{UNRESOLVED_REASON} ({trade.failure_reason or 'timeout'})"[:500])
                    .execution_options(synchronize_session=False)
                )
                await emit_system_event(
                    db,
                    event_type="TIMEOUT_UNRESOLVED",
                    severity="CRITICAL",
                    message=(
                        f"TIMEOUT copy trade {trade.id} not found in broker order book. "
                        "Manual verification required."
                    ),
                    account_id=trade.follower_account_id,
                    metadata_payload={
                        "trade_id": str(trade.id),
                        "client_order_id": trade.client_order_id,
                    },
                    commit=False,
                )
                continue

            status_upper = (match.status or "").upper()
            values: dict = {"broker_order_id": match.broker_order_id or trade.broker_order_id}
            if status_upper in ("COMPLETE", "PARTIAL"):
                values.update(status=CopyTradeStatus.SUCCESS, failure_reason=None)
            elif status_upper == "REJECTED":
                values.update(
                    status=CopyTradeStatus.REJECTED,
                    failure_reason=f"Broker rejected after timeout: {status_upper}",
                )
            elif status_upper == "CANCELLED":
                values.update(status=CopyTradeStatus.FAILED, failure_reason="Order cancelled at broker")
            else:
                values.update(
                    status=CopyTradeStatus.RECONCILIATION_REQUIRED,
                    failure_reason=f"Order still open at broker: {status_upper}",
                )

            if values["status"] != CopyTradeStatus.RECONCILIATION_REQUIRED:
                values["completed_at"] = datetime.now(timezone.utc)      # a terminal outcome is now established
            cas = await db.execute(
                update(CopyTrade)
                .where(CopyTrade.id == trade.id, CopyTrade.status == CopyTradeStatus.TIMEOUT)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if cas.rowcount != 1:
                continue  # another run resolved it first
            await self._reconcile_child_order(db, trade, match, status_upper)
            resolved += 1

        if resolved > 0 or flagged_unresolved > 0:
            await db.commit()
            logger.info(
                f"Sweeper resolved {resolved} / flagged {flagged_unresolved} TIMEOUT trades."
            )

        return resolved

    async def _reconcile_child_order(self, db: AsyncSession, trade: CopyTrade, match: OrderRecord, status_upper: str) -> None:
        """
        Moves the unresolved child Order row (left with no broker id and
        broker_status TIMEOUT by the copy pipeline) to what the broker reports.
        Only when exactly one such row matches the trade - never guesses between
        several (e.g. multiple MARKET fills of one master order).
        """
        candidates = (
            await db.execute(
                select(Order).where(
                    Order.account_id == trade.follower_account_id,
                    Order.master_broker_order_id == trade.master_order_id,
                    Order.broker_order_id.is_(None),
                    Order.broker_status == "TIMEOUT",
                    Order.quantity == trade.quantity,
                    Order.status.in_([OrderStatus.PENDING, OrderStatus.FAILED]),
                )
            )
        ).scalars().all()
        if len(candidates) != 1:
            if candidates:
                logger.warning(
                    f"Sweeper: {len(candidates)} unresolved child Orders match trade {trade.id}; Order rows left unchanged."
                )
            return
        target = {
            "COMPLETE": OrderStatus.COMPLETE,
            "PARTIAL": OrderStatus.PARTIAL,
            "REJECTED": OrderStatus.REJECTED,
            "CANCELLED": OrderStatus.CANCELLED,
        }.get(status_upper, OrderStatus.OPEN)
        order = candidates[0]
        filled = match.filled_quantity or 0
        values = {
            "broker_order_id": match.broker_order_id,
            "status": target,
            "broker_status": status_upper or "UNKNOWN",
            "filled_quantity": filled,
            "pending_quantity": max(order.quantity - filled, 0)
            if target in (OrderStatus.OPEN, OrderStatus.PARTIAL)
            else 0,
        }
        if target == OrderStatus.REJECTED:
            values["rejection_reason"] = match.rejection_reason or "Broker rejected after timeout"
        await db.execute(
            update(Order)
            .where(Order.id == order.id, Order.broker_order_id.is_(None))
            .values(**values)
            .execution_options(synchronize_session=False)
        )

    @staticmethod
    def _find_by_client_order_id(book, client_order_id: Optional[str]) -> Optional[OrderRecord]:
        if not client_order_id:
            return None
        for record in book:
            if record.client_order_id == client_order_id:
                return record
        return None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("StaleOrderSweeper task started.")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("StaleOrderSweeper task stopped.")

    async def _run_loop(self) -> None:
        while self._running:
            try:
                async with AsyncSessionLocal() as db:
                    await self.sweep_once(db)
            except Exception as e:
                logger.error(f"Error in StaleOrderSweeper loop: {e}", exc_info=True)

            await asyncio.sleep(self.check_interval_seconds)
