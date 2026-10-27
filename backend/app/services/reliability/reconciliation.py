import logging
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set
from uuid import UUID

from sqlalchemy import or_, select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.accounts import Account, AccountType
from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderSide, OrderSource, OrderStatus
from app.models.reconciliation import ReconciliationRecord
from app.models.strategies import GroupStrategy, StrategyMember
from app.models.audit import SystemEvent, EventSeverity
from app.services.alice_blue.client import AliceBlueClient, order_book_row_id
from app.services.alice_blue.types import Position
from app.services.brokers.order_book_read import OrderBookState, read_order_book_rows

logger = logging.getLogger(__name__)


class ReconciliationEngine:
    @staticmethod
    async def compute_expected_positions(account_id: UUID, db: AsyncSession) -> Dict[str, int]:
        """
        Computes net expected quantity per symbol from follower Order rows
        (source=SYSTEM) - NOT CopyTrade. Uses Order.filled_quantity (the
        broker-confirmed filled amount, kept current post-placement by
        FollowerFillSyncer for LIMIT orders and accurate at placement for
        MARKET orders), never Order.quantity (the target/requested amount,
        which would overstate a partially-filled or unfilled order).

        Status-agnostic by design: a PARTIAL order contributes its partial
        fill; a CANCELLED order that was partially filled before
        cancellation still retains and contributes that filled amount; a
        PENDING/REJECTED/FAILED order with filled_quantity=0 contributes
        nothing. No separate per-status branching is needed - filtering on
        filled_quantity > 0 already produces the correct result for every
        case.

        Deliberately reads Order only, never CopyTrade, and the two are
        never merged/summed together here: CopyTrade.status already reaches
        SUCCESS for MARKET orders (written directly in
        CopyEnginePipeline._process_single_market_follower) at the same
        time Order reaches COMPLETE for that identical fill, so aggregating
        both tables would double-count every MARKET order. CopyTrade's
        read/write behavior elsewhere (e.g. GET /trades order history) is
        unaffected - this function simply stops being one of its readers.
        """
        stmt = select(Order).where(
            Order.account_id == account_id,
            Order.source == OrderSource.SYSTEM,
        )
        result = await db.execute(stmt)
        orders = result.scalars().all()

        positions: Dict[str, int] = {}
        for order in orders:
            if order.filled_quantity <= 0:
                continue
            qty = order.filled_quantity if order.side == OrderSide.BUY else -order.filled_quantity
            positions[order.symbol] = positions.get(order.symbol, 0) + qty

        return positions

    @classmethod
    async def run_for_account(
        cls,
        account_id: UUID,
        db: AsyncSession,
        actual_positions: Optional[List[Position]] = None,
        broker_client: Optional[AliceBlueClient] = None,
    ) -> List[ReconciliationRecord]:
        """
        Reconciles expected DB positions vs actual broker positions for an account.
        Creates ReconciliationRecord and SystemEvent for any discrepancies.

        Fails closed: if neither actual_positions nor a broker_client is
        supplied at all, or the broker_client call itself fails, this
        records a RECONCILIATION_FAILED SystemEvent and returns [] rather
        than silently comparing expected positions against an empty
        (implicitly "broker has zero of everything") actual-position map -
        which would misreport every real position as a full mismatch.
        """
        expected_positions = await cls.compute_expected_positions(account_id, db)

        actual_map: Dict[str, int] = {}
        if actual_positions is not None:
            for p in actual_positions:
                actual_map[p.symbol] = p.quantity
        elif broker_client is not None:
            try:
                positions_data = await broker_client.get_positions()
                for p in positions_data:
                    actual_map[p.symbol] = p.quantity
            except Exception as exc:
                logger.error(f"Failed to fetch actual positions for account {account_id}: {exc}")
                event = SystemEvent(
                    event_type="RECONCILIATION_FAILED",
                    severity=EventSeverity.CRITICAL,
                    message=f"Failed to fetch positions for account {account_id}: {exc}",
                    metadata_payload={"account_id": str(account_id), "error": str(exc)},
                )
                db.add(event)
                await db.commit()
                return []
        else:
            logger.error(
                f"Reconciliation skipped for account {account_id}: no broker client or "
                "actual_positions supplied - refusing to compare against an assumed-empty position."
            )
            event = SystemEvent(
                event_type="RECONCILIATION_FAILED",
                severity=EventSeverity.CRITICAL,
                message=(
                    f"Reconciliation could not run for account {account_id}: no broker "
                    "session/client was available to fetch actual positions."
                ),
                metadata_payload={"account_id": str(account_id), "reason": "NO_BROKER_CLIENT"},
            )
            db.add(event)
            await db.commit()
            return []

        all_symbols = set(expected_positions.keys()).union(set(actual_map.keys()))
        mismatches: List[ReconciliationRecord] = []

        for symbol in all_symbols:
            expected_qty = expected_positions.get(symbol, 0)
            actual_qty = actual_map.get(symbol, 0)
            discrepancy = actual_qty - expected_qty

            if discrepancy != 0:
                record = ReconciliationRecord(
                    account_id=account_id,
                    symbol=symbol,
                    expected_qty=expected_qty,
                    actual_qty=actual_qty,
                    discrepancy_qty=discrepancy,
                    run_at=datetime.now(timezone.utc),
                    resolved=False,
                    notes=f"Mismatch detected during reconciliation: expected {expected_qty}, got {actual_qty}",
                )
                db.add(record)
                mismatches.append(record)

                event = SystemEvent(
                    event_type="POSITION_MISMATCH_DETECTED",
                    severity=EventSeverity.CRITICAL,
                    message=f"Position mismatch for account {account_id} on {symbol}: expected {expected_qty}, actual {actual_qty}",
                    metadata_payload={
                        "account_id": str(account_id),
                        "symbol": symbol,
                        "expected_qty": expected_qty,
                        "actual_qty": actual_qty,
                        "discrepancy": discrepancy,
                    },
                )
                db.add(event)

        if mismatches:
            await db.commit()
            logger.warning(f"Reconciliation for account {account_id} found {len(mismatches)} mismatches.")

        return mismatches

    @classmethod
    async def flag_ambiguous_orders(cls, db: AsyncSession) -> int:
        """
        Crash-time equivalent, for the unified Order table, of the
        CopyTrade PENDING/SUBMITTED flagging in run_crash_recovery below: a
        SYSTEM-sourced Order row still at PENDING with no broker_order_id
        means the local pre-write happened but we never learned whether the
        broker call that followed it completed before the crash - it may
        have been placed, rejected, or never sent at all.

        Deliberately does NOT:
        - mutate Order.status. OrderStatus has no RECONCILIATION_REQUIRED
          value (unlike CopyTradeStatus), and this does not add one -
          forcing the row to any existing status without evidence of what
          actually happened at the broker would misrepresent it, which is
          worse than leaving it accurately marked as "still pending/unknown".
        - attempt automated recovery via an order-book/client_order_id
          lookup. Order does not persist the client_order_id a follower
          order was actually submitted with (only CopyTrade does, and only
          for MARKET orders - see follower_fill_syncer.py's module
          docstring for the full reasoning); reconstructing it here would
          mean re-deriving CopyEnginePipeline's internal naming formula by
          guesswork, which is exactly the kind of unverified assumption
          that risks matching the wrong broker order.

        Instead, each ambiguous row is reported via a SystemEvent carrying
        enough identifying detail (order id, internal_order_id,
        master_broker_order_id, symbol, quantity, account) for manual
        broker-side verification.
        """
        stmt = select(Order).where(
            Order.source == OrderSource.SYSTEM,
            Order.status == OrderStatus.PENDING,
            Order.broker_order_id.is_(None),
        )
        result = await db.execute(stmt)
        ambiguous_orders = result.scalars().all()

        # One alert per unresolved order, however many times the engine restarts (R3b).
        already = {
            (e.metadata_payload or {}).get("order_id")
            for e in (await db.execute(
                select(SystemEvent).where(SystemEvent.event_type == "CRASH_RECOVERY_AMBIGUOUS_ORDER")
            )).scalars().all()
        }
        new_orders = 0
        for order in ambiguous_orders:
            if str(order.id) in already:
                continue
            new_orders += 1
            event = SystemEvent(
                event_type="CRASH_RECOVERY_AMBIGUOUS_ORDER",
                severity=EventSeverity.WARNING,
                message=(
                    f"Follower order {order.internal_order_id} ({order.symbol}, qty={order.quantity}) "
                    "was pre-written PENDING with no broker_order_id recorded before restart - "
                    "unknown whether the broker call completed. Manual verification required."
                ),
                account_id=order.account_id,
                metadata_payload={
                    "order_id": str(order.id),
                    "internal_order_id": order.internal_order_id,
                    "master_broker_order_id": order.master_broker_order_id,
                    "symbol": order.symbol,
                    "quantity": order.quantity,
                },
            )
            db.add(event)

        if new_orders:
            await db.commit()
            logger.warning(f"Crash recovery flagged {new_orders} new ambiguous Order row(s) for manual review.")

        return len(ambiguous_orders)

    @staticmethod
    async def _event_once(db: AsyncSession, event_type: str, account_id: Any, key: str, severity: Any,
                          message: str, metadata: Dict[str, Any]) -> bool:
        """One SystemEvent per (type, account, key): repeated recovery runs do not repeat the alert."""
        existing = (await db.execute(
            select(SystemEvent).where(SystemEvent.event_type == event_type, SystemEvent.account_id == account_id)
        )).scalars().all()
        if any((e.metadata_payload or {}).get("dedupe_key") == key for e in existing):
            return False
        db.add(SystemEvent(event_type=event_type, severity=severity, account_id=account_id, message=message,
                           metadata_payload={**metadata, "dedupe_key": key}))
        await db.commit()
        return True

    @staticmethod
    async def _submitted_verdict(trade: CopyTrade, client_provider: Optional[Callable], cache: Dict[str, Optional[Set[str]]]) -> str:
        """KNOWN | NOT_IN_BOOK | NO_BROKER_ID | UNVERIFIABLE for a SUBMITTED row. UNVERIFIABLE (no client, the read
        failed or returned nothing usable) is never read as 'missing'."""
        if not trade.broker_order_id:
            return "NO_BROKER_ID"
        key = str(trade.follower_account_id)
        if key not in cache:
            ids: Optional[Set[str]] = None
            client = client_provider(key) if client_provider else None
            # One raw read, classified explicitly (brokers/order_book_read): ids exist only for a book that was PROVEN read - ORDERS,
            # or EMPTY (an empty list is the broker's own answer). An absent client, a client without the raw read, a None / non-list
            # result and a raise are all "cannot verify" (ids stays None -> UNVERIFIABLE): never "the order is missing".
            read = await read_order_book_rows(client)
            if read.is_known:
                ids = {order_book_row_id(r) for r in read.rows if isinstance(r, dict)}
            elif read.state is OrderBookState.FAILED:
                logger.warning(f"Crash recovery could not read the order book for {key}: {read.detail}")
            cache[key] = ids
        ids = cache[key]
        if ids is None:
            return "UNVERIFIABLE"
        return "KNOWN" if trade.broker_order_id in ids else "NOT_IN_BOOK"

    @classmethod
    async def run_crash_recovery(
        cls,
        db: AsyncSession,
        client_provider: Optional[Callable[[str], Optional[AliceBlueClient]]] = None,
    ) -> int:
        """
        On startup recovery:
        1. Classify copy_trades left in PENDING or SUBMITTED (R3b - evidence based, nothing is resent):
             PENDING, never submitted  -> RECONCILIATION_REQUIRED "CRASH_BEFORE_SEND" (the submit stamp is best
                                          effort, so its absence does not prove nothing was sent);
             PENDING, submitted        -> RECONCILIATION_REQUIRED "CRASH_DURING_SEND";
             SUBMITTED, broker id present in the Child's freshly read order book -> left alone (the fill syncer
                                          owns it);
             SUBMITTED, id missing from a successfully read book -> RECONCILIATION_REQUIRED "SUBMITTED_NOT_IN_BOOK";
             SUBMITTED, book unreadable / no client -> left as it is and reported once: a failed read is not proof
                                          that the order is missing, still less that it is safe to resend.
           TIMEOUT rows are not touched here (the R2 unresolved-send handling owns them).
        2. Flag ambiguous SYSTEM-sourced Order rows left PENDING with no
           broker_order_id (see flag_ambiguous_orders) - reported, not
           mutated.
        3. Run reconciliation check for all active FOLLOWER accounts (P2
           Finding #6: MASTER accounts are deliberately excluded here -
           compute_expected_positions only ever sums Order rows with
           source=SYSTEM, and a master account's own orders are always
           INTERNAL_UI or BROKER_EXTERNAL, never SYSTEM (SYSTEM is reserved
           for follower orders the copy engine places). That makes
           compute_expected_positions structurally return {} for a master
           account every time, regardless of what it actually holds at the
           broker - so run_for_account would misreport every real master
           position as a full mismatch and fire a CRITICAL
           POSITION_MISMATCH_DETECTED alert on every crash-recovery run,
           purely as noise that can mask genuine follower-side mismatches.
           This does not change compute_expected_positions or
           run_for_account themselves - a master account passed to
           run_for_account directly (e.g. a manual
           POST /reliability/reconciliation/run?account_id=<master> call)
           would still hit this same structural false positive; that call
           site is unaffected by this method and is not in scope here).
           When client_provider is given, resolves and passes a real
           AliceBlueClient per account so actual broker positions are
           genuinely fetched (see run_for_account's fail-closed behavior
           otherwise) - reuses whatever client lookup the caller already
           has (e.g. AliceBlueAccountManager.get_client), no new broker
           integration.

        Return value is unchanged: the count of CopyTrade rows flagged in
        step 1, exactly as before - callers (engine_loop.py, the
        /reliability/reconciliation/run API route) already surface this
        number and its contract is left intact. Ambiguous-order counts and
        reconciliation mismatches are reported via SystemEvent/
        ReconciliationRecord as they already were.
        """
        stmt = (
            select(CopyTrade)
            .where(CopyTrade.status.in_([CopyTradeStatus.PENDING, CopyTradeStatus.SUBMITTED]))
        )
        result = await db.execute(stmt)
        unresolved_trades = result.scalars().all()

        recovered_count = 0
        child_books: Dict[str, Optional[Set[str]]] = {}
        for trade in unresolved_trades:
            if trade.status == CopyTradeStatus.PENDING:
                code = "CRASH_DURING_SEND" if trade.submitted_at is not None else "CRASH_BEFORE_SEND"
                detail = (
                    "System restarted after the order may have been sent; verify at the broker, do not resend"
                    if trade.submitted_at is not None else
                    "System restarted after the claim and before a send was recorded; the submit stamp is best effort, "
                    "so verify at the broker before assuming nothing was sent, and do not resend"
                )
            else:
                verdict = await cls._submitted_verdict(trade, client_provider, child_books)
                if verdict == "KNOWN":
                    continue                      # in the Child's book: the fill syncer reconciles it
                if verdict == "UNVERIFIABLE":
                    await cls._event_once(
                        db, "CRASH_RECOVERY_CHILD_BOOK_UNAVAILABLE", trade.follower_account_id, str(trade.id),
                        EventSeverity.WARNING,
                        f"CopyTrade {trade.id} is SUBMITTED but the Child's order book could not be read at startup; left "
                        "as it is. A failed read does not show the order is missing or safe to resend.",
                        {"trade_id": str(trade.id), "master_order_id": trade.master_order_id},
                    )
                    continue
                code = "SUBMITTED_NOT_IN_BOOK" if verdict == "NOT_IN_BOOK" else "SUBMITTED_NO_BROKER_ID"
                detail = "Not found in the Child's freshly read order book; verify at the broker, do not resend"
            trade.status = CopyTradeStatus.RECONCILIATION_REQUIRED
            trade.failure_reason = f"{code}: {detail}"
            recovered_count += 1

            event = SystemEvent(
                event_type="CRASH_RECOVERY_FLAGGED_TRADE",
                severity=EventSeverity.WARNING,
                message=f"CopyTrade {trade.id} flagged RECONCILIATION_REQUIRED on crash recovery startup ({code})",
                metadata_payload={"trade_id": str(trade.id), "master_order_id": trade.master_order_id, "code": code},
            )
            db.add(event)

        if recovered_count > 0:
            await db.commit()
            logger.warning(f"Crash recovery flagged {recovered_count} in-flight trades.")

        await cls.flag_ambiguous_orders(db)

        # Run reconciliation for all active FOLLOWER accounts only - MASTER
        # accounts are excluded (see docstring: compute_expected_positions
        # is structurally always {} for a master, so comparing it against
        # real broker positions would always be a false-positive mismatch).
        #
        # Roles are per strategy now, so "follower" means: an account_type
        # FOLLOWER or any current strategy Child - and never an account that
        # is currently a strategy's Master (its own trades are never SYSTEM
        # rows, the same structural false positive as above).
        strategy_masters = select(GroupStrategy.master_account_id).where(
            GroupStrategy.deleted_at.is_(None), GroupStrategy.master_account_id.isnot(None)
        )
        strategy_children = (
            select(StrategyMember.account_id)
            .join(GroupStrategy, GroupStrategy.id == StrategyMember.strategy_id)
            .where(GroupStrategy.deleted_at.is_(None))
        )
        acc_stmt = select(Account.id).where(
            Account.is_active == True,
            or_(Account.account_type == AccountType.FOLLOWER, Account.id.in_(strategy_children)),
            Account.id.not_in(strategy_masters),
        )
        acc_ids = (await db.execute(acc_stmt)).scalars().all()
        for acc_id in acc_ids:
            client = client_provider(str(acc_id)) if client_provider else None
            await cls.run_for_account(acc_id, db, broker_client=client)

        return recovered_count
