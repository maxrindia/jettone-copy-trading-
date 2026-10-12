"""Periodically polls each follower's Alice Blue order book and updates the
matching SYSTEM-sourced `Order` rows with the broker's actual
filled_quantity/status.

Without this, a follower LIMIT order's `Order` row is frozen forever at
whatever the broker's initial placement acknowledgment said (status=OPEN,
filled_quantity=0) - nothing else in the codebase observes what happens to
a follower's order after it's placed. See backend/app/services/runtime/
engine_loop.py: only the MASTER account gets a persistent WebSocket
connection; StaleOrderSweeper only watches CopyTrade rows in
PENDING/SUBMITTED/TIMEOUT status, never Order rows in OPEN/PARTIAL.

Primarily updates the `Order` table - the intended unified source of truth
for order lifecycle (see alembic migration 0006 "unified_order_lifecycle"),
safely upsertable via the existing (broker, account_id, broker_order_id)
unique constraint through CopyEnginePipeline.save_or_update_order, reused
here unchanged.

Also mirrors a terminal Order transition onto the matching `CopyTrade` row
(see `_sync_copy_trade`) - without this, the Demat card metrics
(accounts.py::get_all_account_metrics, which reads CopyTrade.status
exclusively) stayed stuck showing a LIMIT copy as pending/in-flight forever
after it had actually resolved, since nothing else in the codebase ever
updated that CopyTrade row past its initial PENDING/SUBMITTED write. There
is no foreign key between the two tables, and for LIMIT orders specifically
`CopyTrade.broker_order_id` is never populated at creation (a separate,
pre-existing gap in
CopyEnginePipeline._process_single_limit_follower - the CopyTrade row was
written once at PENDING and never updated again), so matching falls back to
(master_order_id, follower_account_id) restricted to a still-PENDING/
SUBMITTED row when broker_order_id doesn't resolve one.

Consequence worth knowing: ReconciliationEngine.compute_expected_positions
reads CopyTrade, not Order - so LIMIT fills observed here will correctly
update Order records, but will not yet be reflected in position
reconciliation. That is a separate, already-identified follow-on gap, not
something this module claims to fix.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from sqlalchemy import and_, or_, select

from app.core.database import AsyncSessionLocal
from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderSource, OrderStatus
from app.services.alice_blue.client import AliceBlueClient
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.monitoring.notifier import emit_system_event

logger = logging.getLogger(__name__)

# Order.status -> CopyTradeStatus for the terminal states worth mirroring
# back onto CopyTrade. CopyTradeStatus has no dedicated CANCELLED value, so
# OrderStatus.CANCELLED maps to FAILED - this is exactly the bucket
# backend/app/api/accounts.py's metrics endpoint already groups into
# "cancelled_count" (alongside FAILED/TIMEOUT/SKIPPED). Not a perfect
# semantic match, but it lands the CopyTrade in the correct metrics bucket
# without a CopyTradeStatus enum migration.
ORDER_TO_COPY_TRADE_STATUS = {
    OrderStatus.COMPLETE: CopyTradeStatus.SUCCESS,
    OrderStatus.CANCELLED: CopyTradeStatus.FAILED,
    OrderStatus.REJECTED: CopyTradeStatus.REJECTED,
    OrderStatus.FAILED: CopyTradeStatus.FAILED,
}

# Once a follower Order reaches one of these, the broker will never change
# it again - no need to keep polling for that row.
TERMINAL_STATUSES = {
    OrderStatus.COMPLETE,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
    OrderStatus.FAILED,
}

# Leading text of the failure_reason StaleOrderSweeper.sweep_once writes
# ("Stale order untouched for >{n}s") - how _sync_copy_trade recognises a
# flag the sweeper itself raised.
STALE_SWEEPER_REASON_PREFIX = "Stale order untouched"

# Alice Blue order-book status -> Order.status. Mirrors the mapping already
# used in engine_loop.py's persist_and_process_event for the master's own
# order-book-derived events.
BROKER_STATUS_MAP = {
    "COMPLETE": OrderStatus.COMPLETE,
    "OPEN": OrderStatus.OPEN,
    "PARTIAL": OrderStatus.PARTIAL,
    # A follower's own native SL/SL-M order (see the immediate-SL-
    # replication feature) sits TRIGGER_PENDING at the broker too, exactly
    # mirroring master_order_poller.py's own mapping, so its eventual
    # independent trigger/fill/cancel is correctly observed and tracked
    # rather than silently skipped as an unmapped status.
    "TRIGGER_PENDING": OrderStatus.PENDING,
    "CANCELLED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
}


class FollowerFillSyncer:
    """
    Two responsibilities, both scoped to SYSTEM-sourced (follower) Order rows:
    1. Poll each follower's broker order book on an interval.
    2. Upsert any Order row whose broker-reported status/filled_quantity has
       changed since we last saw it.
    """

    def __init__(
        self,
        follower_clients_provider: Callable[[], Dict[str, AliceBlueClient]],
        pipeline: CopyEnginePipeline,
        check_interval_seconds: int = 60,
    ):
        self.follower_clients_provider = follower_clients_provider
        self.pipeline = pipeline
        self.check_interval_seconds = check_interval_seconds
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def sync_once(self) -> int:
        """Runs a single sync pass across all current follower clients.
        Returns the number of Order rows updated. One follower's failure
        never prevents the others in the same pass from being synced."""
        clients = self.follower_clients_provider()
        updated = 0
        for account_id, client in clients.items():
            try:
                updated += await self._sync_account(account_id, client)
            except Exception as exc:
                logger.error(f"[FollowerFillSyncer] Sync failed for account {account_id}: {exc}", exc_info=True)
        return updated

    async def _sync_account(self, account_id: str, client: AliceBlueClient) -> int:
        try:
            acc_uuid = uuid.UUID(str(account_id))
        except ValueError:
            return 0

        async with AsyncSessionLocal() as session:
            stmt = select(Order).where(
                Order.account_id == acc_uuid,
                Order.source == OrderSource.SYSTEM,
                Order.status.notin_(TERMINAL_STATUSES),
                Order.broker_order_id.isnot(None),
            )
            open_orders: List[Order] = (await session.execute(stmt)).scalars().all()

        if not open_orders:
            return 0

        try:
            book = await client.get_order_book()
        except Exception as exc:
            logger.warning(f"[FollowerFillSyncer] Order book fetch failed for {account_id}: {exc}")
            return 0

        book_by_broker_id = {rec.broker_order_id: rec for rec in book if rec.broker_order_id}
        updated = 0

        for ord_rec in open_orders:
            record = book_by_broker_id.get(ord_rec.broker_order_id)
            if record is None:
                continue

            new_status = BROKER_STATUS_MAP.get(record.status)
            if new_status is None:
                continue

            if new_status == ord_rec.status and record.filled_quantity == ord_rec.filled_quantity:
                continue  # nothing actually changed - skip the write

            prev_status, prev_filled = ord_rec.status, ord_rec.filled_quantity
            pending_qty = max(0, ord_rec.quantity - record.filled_quantity)

            # order_type and price are pipeline-owned and must be re-read, not
            # carried from the snapshot above. That snapshot was taken before
            # the order-book round trip, and the pipeline can convert this very
            # child from SL/SL-M to LIMIT inside that window
            # (_execute_triggered_sl_child -> _persist_triggered_sl_child_state).
            # Passing the pre-trip values would make save_or_update_order write
            # "SL" and the trigger-era price back over the newer converted
            # state, undoing a modification the broker has already applied.
            owned = await self._current_pipeline_owned_fields(ord_rec.internal_order_id)
            if owned is None:
                continue  # row vanished mid-pass; nothing safe to write
            current_order_type, current_price = owned

            await self.pipeline.save_or_update_order(
                internal_order_id=ord_rec.internal_order_id,
                broker_order_id=ord_rec.broker_order_id,
                account_id=acc_uuid,
                source=OrderSource.SYSTEM,
                exchange=ord_rec.exchange,
                symbol=ord_rec.symbol,
                side=ord_rec.side,
                order_type=current_order_type,
                product_type=ord_rec.product_type,
                quantity=ord_rec.quantity,
                filled_quantity=record.filled_quantity,
                pending_quantity=pending_qty,
                price=current_price,
                average_price=record.average_price,
                status=new_status,
                broker_status=record.status,
                rejection_reason=record.rejection_reason,
                master_broker_order_id=ord_rec.master_broker_order_id,
                group_strategy_id=ord_rec.group_strategy_id,
            )
            updated += 1
            logger.info(
                f"[FollowerFillSyncer] {account_id}: order {ord_rec.broker_order_id} "
                f"{prev_status.value}->{new_status.value}, filled {prev_filled}->{record.filled_quantity}"
            )

            if new_status in TERMINAL_STATUSES:
                await self._sync_copy_trade(ord_rec, new_status)
                async with AsyncSessionLocal() as session:
                    await emit_system_event(
                        session,
                        event_type="FOLLOWER_ORDER_FILL_SYNCED",
                        severity="INFO",
                        message=(
                            f"Follower order {ord_rec.broker_order_id} reached terminal status "
                            f"{new_status.value} (filled {record.filled_quantity}/{ord_rec.quantity})."
                        ),
                        account_id=acc_uuid,
                    )

        return updated

    @staticmethod
    async def _current_pipeline_owned_fields(internal_order_id: str):
        """This order's CURRENT order_type and price, read fresh.

        Only the broker-reported fields (status, filled/pending quantity,
        average price, broker_status, rejection_reason) belong to this module.
        order_type and price belong to whoever last modified the order at the
        broker - in practice the pipeline - so they are read immediately before
        the write instead of being reused from the snapshot taken before the
        order-book fetch. Returns None if the row is gone.
        """
        async with AsyncSessionLocal() as session:
            stmt = select(Order.order_type, Order.price).where(
                Order.internal_order_id == internal_order_id
            )
            row = (await session.execute(stmt)).one_or_none()
        if row is None:
            return None
        return row[0], row[1]

    @staticmethod
    async def _sync_copy_trade(ord_rec: Order, new_status: OrderStatus) -> None:
        """
        Mirrors a terminal Order status onto the matching CopyTrade row, so
        the Demat card metrics (accounts.py::get_all_account_metrics, which
        reads CopyTrade.status exclusively) don't stay stuck at PENDING/
        SUBMITTED forever after the real order has actually resolved - this
        module otherwise deliberately updates Order only (see module
        docstring), which is what caused that desync in the first place.

        Match by broker_order_id first - reliable for MARKET copies, which
        set CopyTrade.broker_order_id at creation
        (_process_single_market_follower). LIMIT copies never populate that
        field (a pre-existing, documented gap - see this module's own
        docstring), so fall back to matching by (master_order_id,
        follower_account_id) restricted to a CopyTrade still in
        PENDING/SUBMITTED - there is at most one such row per LIMIT order
        per follower.
        """
        new_ct_status = ORDER_TO_COPY_TRADE_STATUS.get(new_status)
        if new_ct_status is None or not ord_rec.master_broker_order_id:
            return

        async with AsyncSessionLocal() as session:
            trade = None
            if ord_rec.broker_order_id:
                stmt = select(CopyTrade).where(CopyTrade.broker_order_id == ord_rec.broker_order_id)
                trade = (await session.execute(stmt)).scalar_one_or_none()

            if trade is None:
                # Also matches a row StaleOrderSweeper itself flagged while
                # the child was merely slow (a resting LIMIT, or an SL child
                # converted in place and still working): once the child's
                # terminal state is known that flag is resolved, and nothing
                # else would ever clear it. Strictly limited to the
                # sweeper's own reason text on a row with no broker order
                # id - a RECONCILIATION_REQUIRED raised by a failed modify/
                # cancel, a timeout or crash recovery is a real finding and
                # is never touched here.
                stale_sweeper_flag = and_(
                    CopyTrade.status == CopyTradeStatus.RECONCILIATION_REQUIRED,
                    CopyTrade.failure_reason.startswith(STALE_SWEEPER_REASON_PREFIX),
                    CopyTrade.broker_order_id.is_(None),
                )
                stmt = (
                    select(CopyTrade)
                    .where(
                        CopyTrade.master_order_id == ord_rec.master_broker_order_id,
                        CopyTrade.follower_account_id == ord_rec.account_id,
                        or_(
                            CopyTrade.status.in_([CopyTradeStatus.PENDING, CopyTradeStatus.SUBMITTED]),
                            stale_sweeper_flag,
                        ),
                    )
                    .order_by(CopyTrade.created_at.asc())
                )
                trade = (await session.execute(stmt)).scalars().first()

            if trade is None:
                return

            clearing_stale_flag = (
                trade.status == CopyTradeStatus.RECONCILIATION_REQUIRED
                and (trade.failure_reason or "").startswith(STALE_SWEEPER_REASON_PREFIX)
            )
            trade.status = new_ct_status
            trade.broker_order_id = trade.broker_order_id or ord_rec.broker_order_id
            if new_status != OrderStatus.COMPLETE:
                trade.failure_reason = f"Order reached {new_status.value} at broker"
            elif clearing_stale_flag:
                trade.failure_reason = None
            trade.completed_at = datetime.now(timezone.utc)
            await session.commit()

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("FollowerFillSyncer task started.")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("FollowerFillSyncer task stopped.")

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self.sync_once()
            except Exception as exc:
                logger.error(f"[FollowerFillSyncer] Sync loop error: {exc}", exc_info=True)
            await asyncio.sleep(self.check_interval_seconds)
