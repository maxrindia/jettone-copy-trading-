import asyncio
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.accounts import Account, AccountType
from app.models.audit import AuditLog
from app.models.strategies import GroupStrategy, StrategyMember
from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.types import (
    FundsData,
    OrderRequest,
    OrderResult,
    OrderResultCategory,
    OrderSide as BrokerOrderSide,
    OrderType,
    ProductType,
)
from app.services.copy_engine.calculator import QuantityCalculator
from app.services.copy_engine.events import MasterTradeEvent, derive_copy_event_id
from app.services.alice_blue.instruments import InstrumentResolutionError
from app.services.brokers.registry import can_be_child, persisted_order_label
from app.services.copy_engine.risk import RiskCheckResult, RiskEngine
from app.services.copy_engine.risk_inputs import (
    PositionSafetyError,
    get_available_long,
    get_daily_loss_today,
    get_open_exposure,
)
from app.services.copy_engine.validator import EventValidator
from app.services.reliability.retry import RetryPolicy
from app.services.monitoring.notifier import emit_system_event

logger = logging.getLogger("copy_engine_pipeline")

# A send whose outcome the broker never confirmed either way. TIMEOUT: the adapter reached the point of
# sending and got no usable answer. UNKNOWN: produced only by RetryPolicy after place_order RAISED on every
# attempt - an exception can follow a request the broker already accepted. Neither proves the order was not
# placed, so neither may be recorded as a rejection or failure; both wait for the sweeper's order-book
# reconciliation. NETWORK_ERROR is deliberately not here: every adapter - Alice Blue included, since the
# R2A fix - returns it only for failures that prove the request never left (brokers/transport.py), and it
# is the one category the retry policy re-sends.
AMBIGUOUS_SEND_CATEGORIES = frozenset({OrderResultCategory.TIMEOUT, OrderResultCategory.UNKNOWN})
UNKNOWN_OUTCOME_REASON = "Order submission outcome unknown after an unexpected error; verify at the broker"


def is_ambiguous_send(order_res) -> bool:
    return order_res.category in AMBIGUOUS_SEND_CATEGORIES


def unresolved_reason(order_res) -> str:
    """Sanitized reason stored for an ambiguous send (raw exception text from UNKNOWN is never persisted)."""
    if order_res.category == OrderResultCategory.UNKNOWN:
        return UNKNOWN_OUTCOME_REASON
    return (order_res.message or "Order submission timed out without confirmation")[:300]


def compute_marketable_limit_price(side: str, reference_price: Optional[Decimal]) -> Optional[Decimal]:
    """
    The marketable-LIMIT price for a given side, from a confirmed reference
    (master execution) price: BUY pays up by the configured buffer, SELL
    sells down by it. Returns None when no usable reference price exists,
    so callers fail closed instead of pricing off zero.

    Single source of truth for this arithmetic. It is used both when
    replicating a master MARKET fill as a fresh child order and when
    repricing an EXISTING child after the master converted LIMIT -> MARKET,
    so the two can never drift apart. settings.MARKET_ORDER_BUFFER_PERCENT
    defaults to 0, i.e. exactly the master's fill price.
    """
    if reference_price is None or reference_price <= 0:
        return None
    buffer_fraction = settings.MARKET_ORDER_BUFFER_PERCENT / Decimal("100")
    if str(side or "").strip().upper() == "BUY":
        return (reference_price * (Decimal("1") + buffer_fraction)).quantize(Decimal("0.01"))
    return (reference_price * (Decimal("1") - buffer_fraction)).quantize(Decimal("0.01"))


class CopyEnginePipeline:
    """
    Main trade & limit order replication coordinator:
    - Master LIMIT order creation -> Immediate parallel child LIMIT submission (no wait for master fill)
    - Master MARKET order fill -> Parallel child MARKET submission
    - Independent execution & partial fill tracking per account
    - Master cancellation propagation to pending child quantities
    - Master modification propagation
    - Deterministic duplicate protection
    """

    def __init__(
        self,
        validator: EventValidator,
        lot_sizes: Optional[Dict[str, int]] = None,
        retry_policy: Optional[RetryPolicy] = None,
        stop_provider: Optional[Any] = None,
    ):
        self.validator = validator
        self.lot_sizes = lot_sizes or {"DEFAULT": 1}
        self.retry_policy = retry_policy or RetryPolicy(max_attempts=3)
        self.stop_provider = stop_provider

    def _get_lot_size(self, symbol: str) -> int:
        for prefix, lot in self.lot_sizes.items():
            if prefix in symbol:
                return lot
        return self.lot_sizes.get("DEFAULT", 1)

    async def _resolve_stop_state(self, global_emergency_stop: bool) -> tuple:
        """Returns (global_active, stopped_follower_ids)."""
        if self.stop_provider is None:
            return global_emergency_stop, set()
        try:
            state = await self.stop_provider()
            return bool(state[0]), set(state[1])
        except Exception as exc:
            logger.error(f"[Pipeline] Stop provider failed ({exc}); failing closed.")
            return True, set()

    async def save_or_update_order(
        self,
        internal_order_id: str,
        broker_order_id: Optional[str],
        account_id: uuid.UUID,
        source: OrderSource,
        exchange: str,
        symbol: str,
        side: OrderSide,
        order_type: str,
        product_type: str,
        quantity: int,
        price: Decimal,
        status: OrderStatus,
        filled_quantity: int = 0,
        pending_quantity: Optional[int] = None,
        trigger_price: Decimal = Decimal("0"),
        average_price: Decimal = Decimal("0"),
        broker_status: Optional[str] = None,
        rejection_reason: Optional[str] = None,
        parent_order_id: Optional[uuid.UUID] = None,
        master_broker_order_id: Optional[str] = None,
        group_strategy_id: Optional[uuid.UUID] = None,
        correlation_id: Optional[str] = None,
    ) -> Order:
        """
        Durable atomic upsert for an Order record.
        """
        if pending_quantity is None:
            pending_quantity = max(0, quantity - filled_quantity)

        async with AsyncSessionLocal() as session:
            # The account's own broker label (Alice Blue accounts, and any account
            # that cannot be resolved, keep the historical "ALICE_BLUE").
            owner = await session.get(Account, account_id)
            broker_label = persisted_order_label(owner.broker if owner is not None else None)

            stmt = select(Order).where(Order.internal_order_id == internal_order_id)
            existing = (await session.execute(stmt)).scalar_one_or_none()

            if not existing and broker_order_id:
                stmt_broker = select(Order).where(
                    Order.broker == broker_label,
                    Order.account_id == account_id,
                    Order.broker_order_id == broker_order_id,
                )
                existing = (await session.execute(stmt_broker)).scalar_one_or_none()

            if existing:
                existing.broker_order_id = broker_order_id or existing.broker_order_id
                existing.filled_quantity = filled_quantity
                existing.pending_quantity = pending_quantity
                existing.average_price = average_price if average_price > 0 else existing.average_price
                # Requested order parameters (the price/quantity the order is
                # WORKING at) were never refreshed here, only the fill/status
                # fields were. A master who modified a still-pending LIMIT on
                # the mobile app therefore had the change detected correctly
                # but never persisted: order 26090900100077 stayed at 125.00
                # in the DB after the broker moved it to 124.00, so the UI
                # could never show the live master price.
                #
                # Both are written only when the incoming event actually
                # carries a meaningful value. A 0 means "this event does not
                # report it" (Alice Blue reports price 0 for MARKET orders),
                # never "set it to zero" - blindly assigning would wipe a
                # LIMIT order's price the first time a payload omitted it.
                #
                # price and average_price stay semantically distinct: price is
                # the requested/working LIMIT price, average_price is the
                # actual fill price, and each is assigned only from its own
                # source field. A fill arriving for a LIMIT order carries that
                # order's unchanged limit price, so a fill can never overwrite
                # the limit price with the average fill price.
                if price > 0:
                    existing.price = price
                if quantity > 0:
                    existing.quantity = quantity
                # order_type was never refreshed here either, only set at
                # creation - a master/follower order whose broker-reported
                # type later changes (LIMIT->MARKET while still open, or a
                # native SL/SL-M converting to LIMIT/MARKET once triggered)
                # kept showing its ORIGINAL order_type in the DB forever.
                # Proven safe to correct: every behavioral consumer of
                # order_type (detector branches, seed_state's _known_order_ids
                # membership) reads it from the LIVE broker event each poll,
                # never from this persisted field - restart recovery's own
                # identity check additionally has an independent,
                # order_type-free safety net (known_child_master_order_ids,
                # see engine_loop.py). This write is purely a persistence
                # correction so the DB/UI reflect the broker's current type;
                # it never dispatches an event, calls a broker API, or
                # changes any replication decision. Same "0/falsy means not
                # reported" guard as price/quantity above.
                if order_type:
                    existing.order_type = order_type
                # Same "0 means not reported" rule as price/quantity above.
                # Needed for native SL/SL-M orders: Alice Blue reports a real
                # triggerPrice while TRIGGER_PENDING, then stops reporting it
                # (0) once the order has triggered - blindly assigning would
                # wipe the only record of what the trigger price ever was.
                if trigger_price > 0:
                    existing.trigger_price = trigger_price
                existing.status = status
                existing.broker_status = broker_status or existing.broker_status
                existing.rejection_reason = rejection_reason or existing.rejection_reason
                existing.updated_at = datetime.now(timezone.utc)
                if master_broker_order_id:
                    existing.master_broker_order_id = master_broker_order_id
                if group_strategy_id:
                    existing.group_strategy_id = group_strategy_id
                await session.commit()
                return existing
            else:
                new_order = Order(
                    internal_order_id=internal_order_id,
                    broker_order_id=broker_order_id,
                    account_id=account_id,
                    group_strategy_id=group_strategy_id,
                    parent_order_id=parent_order_id,
                    master_broker_order_id=master_broker_order_id,
                    source=source,
                    broker=broker_label,
                    exchange=exchange,
                    symbol=symbol,
                    side=side,
                    order_type=order_type,
                    product_type=product_type,
                    quantity=quantity,
                    filled_quantity=filled_quantity,
                    pending_quantity=pending_quantity,
                    price=price,
                    trigger_price=trigger_price,
                    average_price=average_price,
                    status=status,
                    broker_status=broker_status,
                    rejection_reason=rejection_reason,
                    correlation_id=correlation_id,
                )
                session.add(new_order)
                try:
                    await session.commit()
                    return new_order
                except IntegrityError:
                    await session.rollback()
                    # Query existing order on unique conflict
                    stmt_dup = select(Order).where(Order.internal_order_id == internal_order_id)
                    return (await session.execute(stmt_dup)).scalar_one()

    async def execute_replication(
        self,
        event: MasterTradeEvent,
        clients: Dict[str, AliceBlueClient],
        global_emergency_stop: bool = False,
        stagger_ms: int = 0,
    ) -> Dict[str, Any]:
        """
        Main replication router: dispatches immediate limit replication or market replication.
        """
        if event.event_type == "ORDER_MODIFIED":
            # A master order observed as LIMIT -> MARKET while STILL OPEN has
            # not executed yet, so there is no confirmed execution price to
            # derive a marketable LIMIT from (Alice Blue reports price 0.0
            # for a MARKET order). Repricing the child off that 0 would
            # destroy its working price. The child is therefore left exactly
            # as it is and the conversion is applied by the ORDER_FILLED
            # branch below, which fires with the real fill price the moment
            # the master executes. If the master is cancelled instead of
            # filled, the existing cancellation propagation handles the
            # child; if it somehow neither fills nor cancels, the existing
            # StaleOrderSweeper still surfaces the untouched child.
            if str(event.order_type or "").upper() == "MARKET":
                logger.info(
                    f"[Pipeline] Master order {event.master_order_id} changed to MARKET while "
                    "still open; deferring child reprice until the master's confirmed fill "
                    "price is known. Child left untouched."
                )
                return {"status": "deferred_pending_master_fill"}

            return await self.handle_master_modification(
                master_order_id=event.master_order_id,
                new_price=event.limit_price if event.limit_price > 0 else event.price,
                new_quantity=event.quantity,
                clients=clients,
                master_account_id=event.account_id,
                global_emergency_stop=global_emergency_stop,
                new_order_type=event.order_type,
            )
        elif event.event_type == "LIMIT_CREATED" or (event.order_type == "LIMIT" and event.status == "OPEN"):
            return await self.replicate_limit_order(
                event=event,
                clients=clients,
                global_emergency_stop=global_emergency_stop,
                stagger_ms=stagger_ms,
            )
        elif event.event_type == "SL_PENDING_CREATED":
            # Native SL/SL-M order still TRIGGER_PENDING at the master:
            # replicated IMMEDIATELY as the follower's own independent
            # SL/SL-M order, mirroring the master's price/trigger_price
            # exactly - never compute_marketable_limit_price, never the
            # MARKET_ORDER_BUFFER_PERCENT buffer. See replicate_sl_order.
            return await self.replicate_sl_order(
                event=event,
                clients=clients,
                global_emergency_stop=global_emergency_stop,
                stagger_ms=stagger_ms,
            )
        elif event.event_type == "SL_PENDING_MODIFIED":
            return await self.handle_pending_sl_modification(
                event=event,
                clients=clients,
                global_emergency_stop=global_emergency_stop,
            )
        elif event.event_type == "ORDER_CANCELLED":
            return await self.handle_master_cancellation(
                master_order_id=event.master_order_id,
                clients=clients,
            )
        elif event.event_type == "ORDER_REJECTED":
            # A master order rejected at the broker was never executed -
            # there is nothing to mirror. Without this branch, ORDER_REJECTED
            # matched none of the branches above and fell through to the
            # unconditional _execute_market_replication fallback at the
            # bottom of this function, which replicated the rejected order's
            # price/quantity to followers as if it were a real fill
            # (confirmed live, 2026-09-30: a rejected master LIMIT reached
            # client.place_order() for the follower). This is a pure no-op:
            # no follower replication, no broker call of any kind.
            logger.info(
                f"[Pipeline] Master order {event.master_order_id} was REJECTED at broker; "
                "no follower action taken."
            )
            return {
                "status": "master_order_rejected",
                "reason": event.rejection_reason,
            }
        elif event.event_type == "ORDER_FILLED":
            # Master LIMIT fill update only - does NOT replicate new child orders
            logger.info(f"[Pipeline] Master fill update for {event.master_order_id} (seq {event.fill_sequence}). Fills tracked independently.")

            # Master native SL/SL-M triggered and filled while its follower
            # SL/SL-M child is still PENDING: push that SAME child to execute
            # now (modify-in-place, never cancel/replace). Only children
            # created by the immediate-SL-replication path can match, so
            # every other order type continues below exactly as before.
            async with AsyncSessionLocal() as session:
                pending_sl_children = (await session.execute(select(Order).where(
                    Order.master_broker_order_id == event.master_order_id,
                    Order.source == OrderSource.SYSTEM,
                    Order.status == OrderStatus.PENDING,
                    Order.order_type.in_(["SL", "SLM"]),
                ))).scalars().all()
            if pending_sl_children:
                return await self.handle_sl_trigger_execution(
                    event=event,
                    child_orders=pending_sl_children,
                    clients=clients,
                    global_emergency_stop=global_emergency_stop,
                )

            # Missed-intermediate-state reconciliation: a LIMIT -> MARKET
            # conversion is normally caught by detector.py's branch "1b"
            # while the master order is still OPEN, which fires
            # ORDER_MODIFIED above and propagates the type change to the
            # child via handle_master_modification. But if the poller's
            # interval misses that OPEN+MARKET window entirely (the master
            # converts AND fills between two polls), the detector correctly
            # classifies the resulting fill as ORDER_FILLED - same
            # already-known order, not a new one - and no ORDER_MODIFIED
            # event is ever produced for it. Without this check, the
            # existing child order is left stranded as a stale LIMIT order
            # at its old price and never executes (the confirmed production
            # incident this reconciles - master 26092200137366).
            #
            # The conversion is delivered by REPRICING the existing child to
            # a marketable LIMIT - never by changing its order_type. Alice
            # Blue rejects orderType=MARKET on modify with EC965 "Market
            # orders are not allowed" (confirmed in production), which is
            # why the whole platform already places master MARKET fills as
            # marketable LIMITs. The child therefore keeps order_type=LIMIT
            # and keeps its broker order id; only its price moves, via the
            # SAME handle_master_modification path ORDER_MODIFIED uses.
            # _execute_market_replication is never called here - that would
            # create a brand-new child.
            if event.order_type.upper() == "MARKET":
                async with AsyncSessionLocal() as session:
                    children_stmt = select(Order).where(
                        Order.master_broker_order_id == event.master_order_id,
                        Order.source == OrderSource.SYSTEM,
                    )
                    child_orders = (await session.execute(children_stmt)).scalars().all()

                if child_orders and all(str(c.order_type or "").upper() in ("SL", "SLM") for c in child_orders):
                    # Only reached once no SL/SL-M child is still PENDING
                    # (pending ones are handled above by
                    # handle_sl_trigger_execution).
                    #
                    # This master order's children are native SL/SL-M
                    # orders from the immediate-SL-replication path (see
                    # replicate_sl_order), not LIMIT children awaiting a
                    # reprice. The master triggering does not mean these
                    # need repricing or even touching - they are
                    # independent broker orders that trigger/fill on their
                    # own. Without this, they would fall through to
                    # "not working_children" below (their order_type isn't
                    # "LIMIT", so they never count as working) and be
                    # wrongly flagged RECONCILIATION_REQUIRED as if they
                    # had gone missing.
                    return {"status": "master_fill_updated"}

                # Price comes from the master's CONFIRMED execution price
                # (event.price is the fill price for a fill event), never
                # from the order's superseded pre-conversion LIMIT price.
                marketable_price = compute_marketable_limit_price(event.side, event.price)

                working_children = [
                    c for c in child_orders
                    if c.status in (OrderStatus.OPEN, OrderStatus.PARTIAL)
                    and str(c.order_type or "").upper() == "LIMIT"
                ]

                if not child_orders:
                    # No child was ever created for this master order, so
                    # there is nothing to reprice and one must NOT be
                    # invented now - replicating a fill as a fresh order is
                    # exactly the duplicate-child bug this path exists to
                    # avoid.
                    return await self._record_reprice_reconciliation(
                        master_order_id=event.master_order_id,
                        detail="No follower child order exists for this master order.",
                    )

                if not working_children:
                    # Nothing open to reprice. If every child already
                    # COMPLETEd, the conversion is moot and this is a clean
                    # no-op. Otherwise the children are CANCELLED/REJECTED/
                    # FAILED and the conversion silently went undelivered,
                    # which needs to be visible - but still never replaced.
                    if all(c.status == OrderStatus.COMPLETE for c in child_orders):
                        return {"status": "master_fill_updated"}
                    return await self._record_reprice_reconciliation(
                        master_order_id=event.master_order_id,
                        detail=(
                            "The follower child order(s) are no longer working "
                            "(cancelled/rejected), so the LIMIT->MARKET conversion could "
                            "not be applied. No replacement order was created."
                        ),
                    )

                if marketable_price is None:
                    # Requirement: never price off a missing/zero master
                    # execution price. Leave the child exactly as it is.
                    return await self._record_reprice_reconciliation(
                        master_order_id=event.master_order_id,
                        detail=(
                            "The master fill event carried no usable execution price, so no "
                            "marketable LIMIT price could be derived for the child order(s)."
                        ),
                        children=working_children,
                    )

                # Idempotency: a child already sitting at the target price
                # needs nothing. Previously this was implicit (the child's
                # order_type flipped to MARKET and stopped matching), but a
                # repriced child stays LIMIT, so staleness is now judged on
                # the price itself. A redelivered or multi-increment fill
                # therefore re-modifies nothing.
                stale_children = [c for c in working_children if c.price != marketable_price]
                if not stale_children:
                    return {"status": "master_fill_updated"}

                logger.warning(
                    f"[Pipeline] Master order {event.master_order_id} converted LIMIT->MARKET "
                    f"and filled at {event.price}; repricing {len(stale_children)} existing "
                    f"child LIMIT order(s) to marketable {marketable_price} "
                    "(order type stays LIMIT, broker order id unchanged)."
                )
                return await self.handle_master_modification(
                    master_order_id=event.master_order_id,
                    new_price=marketable_price,
                    new_quantity=event.total_quantity,
                    clients=clients,
                    master_account_id=event.account_id,
                    global_emergency_stop=global_emergency_stop,
                    new_order_type="LIMIT",
                    enforce_sell_position_safety=True,
                    instrument_token=event.instrument_id,
                )

            elif event.order_type.upper() == "LIMIT" and event.limit_price and event.limit_price > 0:
                # Missed-intermediate-state reconciliation for a master LIMIT
                # order whose fill Alice Blue STILL reports as order_type
                # LIMIT (unlike the branch above, where the broker itself
                # relabels the order MARKET). A master LIMIT price change can
                # happen and fill inside a single poll gap without ever
                # producing an ORDER_MODIFIED event - MasterOrderPoller only
                # dispatches when a poll's state differs from the previous
                # one, so a price move that occurs between poll N (still
                # OPEN, old price) and poll N+1 (already COMPLETE) is never
                # itself observed. Production incident: master
                # 26092800036213 (ITC SELL) was created at 268.00, silently
                # repriced by the master, and filled at 266.85; the existing
                # child was left stranded working at 268.00 forever (a SELL
                # limit above the market can never fill) until it was
                # cancelled outside Jettone.
                #
                # Staleness is proven ONLY against event.limit_price - the
                # broker's own reported price field for the MASTER at this
                # same poll (threaded onto MasterTradeEvent.limit_price;
                # see detector.py) - never against the fill price alone. A
                # plain, unmodified LIMIT fill routinely executes at a
                # different price than its own limit through ordinary
                # favorable slippage (e.g. NTPC BUY LIMIT 327.00 filled at
                # 325.95 with no modification, order_type staying LIMIT
                # throughout, and correctly left untouched); pricing off
                # event.price alone there would wrongly reprice a healthy,
                # correctly-mirrored child. A child whose price already
                # equals the master's own current limit price mirrors it
                # correctly and is left exactly as it is.
                async with AsyncSessionLocal() as session:
                    children_stmt = select(Order).where(
                        Order.master_broker_order_id == event.master_order_id,
                        Order.source == OrderSource.SYSTEM,
                    )
                    child_orders = (await session.execute(children_stmt)).scalars().all()

                working_children = [
                    c for c in child_orders
                    if c.status in (OrderStatus.OPEN, OrderStatus.PARTIAL)
                    and str(c.order_type or "").upper() == "LIMIT"
                ]

                marketable_price = compute_marketable_limit_price(event.side, event.price)
                if marketable_price is not None:
                    # Idempotency: only act on a child that is BOTH proven
                    # stale against the master's own price (not just fill
                    # noise, per the comment above) AND not already sitting
                    # at the price this call would set it to - so a later,
                    # unrelated event for the same already-repaired order
                    # (e.g. a further partial-fill increment once the
                    # master's price has settled) never re-issues a
                    # redundant broker modify call.
                    stale_children = [
                        c for c in working_children
                        if c.price != event.limit_price and c.price != marketable_price
                    ]
                    if stale_children:
                        logger.warning(
                            f"[Pipeline] Master order {event.master_order_id} LIMIT price moved to "
                            f"{event.limit_price} and filled at {event.price} without an intermediate "
                            f"ORDER_MODIFIED ever being observed; repricing {len(stale_children)} "
                            f"stranded child LIMIT order(s) to marketable {marketable_price} "
                            "(order type stays LIMIT, broker order id unchanged)."
                        )
                        return await self.handle_master_modification(
                            master_order_id=event.master_order_id,
                            new_price=marketable_price,
                            new_quantity=event.total_quantity,
                            clients=clients,
                            master_account_id=event.account_id,
                            global_emergency_stop=global_emergency_stop,
                            new_order_type="LIMIT",
                            enforce_sell_position_safety=True,
                            instrument_token=event.instrument_id,
                        )

            return {"status": "master_fill_updated"}

        # Market Order Replication
        return await self._execute_market_replication(
            event=event,
            clients=clients,
            global_emergency_stop=global_emergency_stop,
            stagger_ms=stagger_ms,
        )

    async def replicate_limit_order(
        self,
        event: MasterTradeEvent,
        clients: Dict[str, AliceBlueClient],
        global_emergency_stop: bool = False,
        stagger_ms: int = 0,
    ) -> Dict[str, Any]:
        """
        IMMEDIATE LIMIT ORDER REPLICATION:
        Dispatches LIMIT orders immediately to all eligible followers without waiting for master fill.
        """
        stop_active, stopped_followers = await self._resolve_stop_state(global_emergency_stop)

        validation = await self.validator.validate(
            event=event,
            is_emergency_stopped=stop_active,
            is_master_healthy=True,
        )
        if not validation.is_valid:
            logger.warning(f"[Pipeline] Master LIMIT order {event.master_order_id} validation failed: {validation.reason_code}")
            return {"status": "rejected", "reason": validation.reason_code}

        # Resolve Strategy and Followers
        followers, strategy_id = await self._get_eligible_followers_for_master(
            event.account_id, master_order_id=event.master_order_id
        )
        if not followers:
            logger.info(f"[Pipeline] No active followers configured for master {event.account_id}.")
            return {"status": "no_followers"}

        lot_size = self._get_lot_size(event.symbol)

        tasks = []
        for idx, (follower, multiplier) in enumerate(followers):
            if idx > 0 and stagger_ms > 0:
                await asyncio.sleep(stagger_ms / 1000.0)
            tasks.append(
                asyncio.create_task(
                    self._process_single_limit_follower(
                        follower=follower,
                        multiplier=multiplier,
                        event=event,
                        client=clients.get(str(follower.id)),
                        lot_size=lot_size,
                        strategy_id=strategy_id,
                        # Finding P3 #12: thread the ALREADY-RESOLVED stop_active
                        # value (from self._resolve_stop_state above), not the raw
                        # global_emergency_stop parameter this function received.
                        # engine_loop.py never passes global_emergency_stop=... on
                        # its execute_replication() call, so the raw parameter is
                        # always False in production; the real signal lives in
                        # stop_active (resolved via self.stop_provider). Without
                        # this, RiskEngine.evaluate()'s own internal "Check 1:
                        # Global emergency stop" could never fire - harmless today
                        # only because the upstream EventValidator.validate(
                        # is_emergency_stopped=stop_active) gate above already
                        # blocks the whole event first, but this keeps the inner
                        # defense-in-depth check meaningful too.
                        global_emergency_stop=stop_active,
                        follower_runtime_paused=str(follower.id) in stopped_followers,
                    )
                )
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)
        summary = {}
        for (follower, _), res in zip(followers, results):
            if isinstance(res, Exception):
                logger.error(f"[Pipeline] Exception replicating limit order to {follower.display_name}: {res}")
                summary[str(follower.id)] = {"status": "EXCEPTION", "error": str(res)}
            else:
                summary[str(follower.id)] = res

        return {"status": "completed", "results": summary}

    async def _process_single_limit_follower(
        self,
        follower: Account,
        multiplier: Decimal,
        event: MasterTradeEvent,
        client: Optional[AliceBlueClient],
        lot_size: int,
        strategy_id: Optional[uuid.UUID],
        global_emergency_stop: bool,
        follower_runtime_paused: bool = False,
    ) -> Dict[str, Any]:
        """
        Places an immediate LIMIT order on the follower account.
        """
        acc_id = follower.id
        if not follower.is_active or follower.is_emergency_stopped or follower_runtime_paused:
            logger.info(f"[Pipeline] Follower {follower.display_name} paused/disabled. Skipping limit order.")
            return {"status": "SKIPPED", "reason": "TRADING_DISABLED"}

        limit_price = event.limit_price if event.limit_price > 0 else event.price

        # --- Pre-trade risk gate: same funds/RiskEngine pattern as MARKET
        # replication (_process_single_market_follower). Fail closed - if
        # funds cannot be fetched or risk evaluation cannot be completed, the
        # follower LIMIT order is never placed, and nothing is pre-written to
        # Order/CopyTrade for a rejected event (mirrors MARKET exactly).
        settings_obj = follower.copy_settings
        risk_audit_event_id = derive_copy_event_id(event.master_order_id, str(acc_id), 1)

        funds_data: Optional[FundsData] = None
        if client:
            try:
                funds_data = await client.get_funds()
            except Exception as exc:
                logger.error(f"[Pipeline] Funds fetch failed for {follower.display_name}: {exc}")

        if funds_data is None:
            async with AsyncSessionLocal() as session:
                session.add(
                    AuditLog(
                        event_type="RISK_REJECTED",
                        account_id=acc_id,
                        master_order_id=event.master_order_id,
                        copy_event_id=risk_audit_event_id,
                        symbol=event.symbol,
                        side=event.side,
                        quantity=0,
                        price=limit_price,
                        reason_code="FUNDS_FETCH_FAILED",
                    )
                )
                await emit_system_event(
                    session,
                    event_type="FUNDS_FETCH_FAILED",
                    severity="WARNING",
                    message=(
                        f"LIMIT copy rejected for {follower.display_name}: broker funds unavailable. "
                        "Failing closed; order NOT placed."
                    ),
                    account_id=acc_id,
                )
            return {"status": "REJECTED", "reasons": ["FUNDS_FETCH_FAILED"]}

        # Calculate Child Limit Quantity - mirrors _process_single_market_follower's
        # convention exactly: honor the follower's configured QuantityCalculator
        # strategy (FIXED/RATIO/CAPITAL_PROPORTIONAL/RISK_BASED) when a
        # CopySetting exists, instead of always using StrategyMember.multiplier
        # regardless of what the follower is configured for. Falls back to the
        # plain multiplier formula only when no CopySetting exists at all.
        if settings_obj:
            base_qty = QuantityCalculator.calculate_quantity(
                mode=settings_obj.quantity_mode,
                master_qty=event.quantity,
                master_capital=settings.MASTER_CAPITAL,
                follower_capital=funds_data.available_margin,
                lot_size=lot_size,
                settings=settings_obj,
                reference_price=limit_price,
                multiplier=multiplier,
            )
            target_qty = base_qty
        else:
            base_qty = int(event.quantity * float(multiplier))
            target_qty = (base_qty // lot_size) * lot_size if lot_size > 1 else max(1, base_qty)

        if target_qty <= 0:
            return {"status": "SKIPPED", "reason": "ZERO_LOT_RESULT"}

        if settings_obj:
            try:
                daily_loss = await get_daily_loss_today(acc_id)
                open_exposure = await get_open_exposure(client)
            except Exception as exc:
                logger.error(f"[Pipeline] Risk input fetch failed for {follower.display_name}: {exc}")
                async with AsyncSessionLocal() as session:
                    session.add(
                        AuditLog(
                            event_type="RISK_REJECTED",
                            account_id=acc_id,
                            master_order_id=event.master_order_id,
                            copy_event_id=risk_audit_event_id,
                            symbol=event.symbol,
                            side=event.side,
                            quantity=target_qty,
                            price=limit_price,
                            reason_code="RISK_INPUTS_UNAVAILABLE",
                        )
                    )
                    await session.commit()
                return {"status": "REJECTED", "reasons": ["RISK_INPUTS_UNAVAILABLE"]}

            risk_res = await self._evaluate_risk_exit_aware(
                client=client,
                side=event.side,
                exchange=event.exchange,
                product_type=event.product_type,
                instrument_token=event.instrument_id,
                account=follower,
                settings=settings_obj,
                symbol=event.symbol,
                quantity=target_qty,
                price=limit_price,
                funds=funds_data,
                daily_loss_today=daily_loss,
                open_exposure=open_exposure,
                follower_runtime_paused=follower_runtime_paused,
                global_emergency_stop=global_emergency_stop,
            )

            if not risk_res.passed:
                reasons_str = ",".join(risk_res.failures)
                logger.warning(f"[Pipeline] Follower {follower.display_name} LIMIT order rejected by risk engine: {reasons_str}")
                async with AsyncSessionLocal() as session:
                    session.add(
                        AuditLog(
                            event_type="RISK_REJECTED",
                            account_id=acc_id,
                            master_order_id=event.master_order_id,
                            copy_event_id=risk_audit_event_id,
                            symbol=event.symbol,
                            side=event.side,
                            quantity=target_qty,
                            price=limit_price,
                            reason_code=reasons_str,
                        )
                    )
                    await session.commit()
                return {"status": "REJECTED", "reasons": risk_res.failures}
        # --- end risk gate ---

        # Follower SELL position-safety gate - the master SELL LIMIT path.
        # A master SELL LIMIT reaches followers through here, not through
        # _execute_market_replication, so gating only the MARKET path would
        # leave this route wide open (a real master SELL LIMIT 12 was
        # replicated this way in production). Outside the `if settings_obj:`
        # block above so it protects followers with no CopySetting too.
        sell_block = await self._enforce_sell_position_safety(
            follower=follower,
            event=event,
            client=client,
            target_qty=target_qty,
            reference_price=limit_price,
            copy_event_uuid=risk_audit_event_id,
        )
        if sell_block is not None:
            return sell_block

        internal_child_ord_id = f"JT_CH_{event.master_order_id[:16]}_{str(acc_id)[:8]}"
        client_order_id = f"JT_LIM_{event.master_order_id[:16]}_{str(acc_id)[:8]}"

        # Step 1: Claim the event with its CopyTrade uniqueness record BEFORE anything else is written or
        # sent, exactly as the MARKET path does. The unique constraint on (master_order_id,
        # follower_account_id, copy_event_id) is the authority on "already handled": if Redis dedupe was
        # bypassed (flush, key expiry) a redelivered event stops HERE - it must not overwrite the existing
        # child Order (save_or_update_order is an upsert) and must not reach the broker a second time.
        copy_event_uuid = derive_copy_event_id(event.master_order_id, str(acc_id), 1)
        async with AsyncSessionLocal() as session:
            ct = CopyTrade(
                id=uuid.uuid4(),
                master_order_id=event.master_order_id,
                follower_account_id=acc_id,
                copy_event_id=copy_event_uuid,
                symbol=event.symbol,
                exchange=event.exchange,
                side=OrderSide.BUY if event.side == "BUY" else OrderSide.SELL,
                quantity=target_qty,
                product_type=event.product_type,
                estimated_price=limit_price,
                client_order_id=client_order_id,
                status=CopyTradeStatus.PENDING,
            )
            session.add(ct)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                logger.info(
                    f"[Pipeline] Duplicate LIMIT copy detected for master_order_id={event.master_order_id}, "
                    f"follower={acc_id}. Skipping: nothing written, nothing sent."
                )
                return {"status": "SKIPPED", "reason": "DUPLICATE_EVENT"}

        # Step 2: Pre-write child Order in PENDING status
        child_order = await self.save_or_update_order(
            internal_order_id=internal_child_ord_id,
            broker_order_id=None,
            account_id=acc_id,
            group_strategy_id=strategy_id,
            source=OrderSource.SYSTEM,
            exchange=event.exchange,
            symbol=event.symbol,
            side=OrderSide.BUY if event.side == "BUY" else OrderSide.SELL,
            order_type="LIMIT",
            product_type=event.product_type,
            quantity=target_qty,
            filled_quantity=0,
            pending_quantity=target_qty,
            price=limit_price,
            status=OrderStatus.PENDING,
            master_broker_order_id=event.master_order_id,
            correlation_id=f"{event.master_order_id}:{acc_id}",
        )

        # Step 3: Submit LIMIT order to Child Broker
        if not client:
            return {"status": "REJECTED", "reason": "NO_CLIENT"}

        broker_side = BrokerOrderSide.BUY if event.side == "BUY" else BrokerOrderSide.SELL
        prod_upper = str(event.product_type).upper()
        if prod_upper in ("CNC", "LONGTERM", "DELIVERY"):
            broker_prod = ProductType.CNC
        elif prod_upper in ("NRML", "MTF", "CARRYFORWARD", "MARGIN"):
            broker_prod = ProductType.NRML
        else:
            broker_prod = ProductType.MIS

        order_req = OrderRequest(
            symbol=event.symbol,
            exchange=event.exchange,
            side=broker_side,
            quantity=target_qty,
            product_type=broker_prod,
            order_type=OrderType.LIMIT,
            price=limit_price,
            trigger_price=Decimal("0"),
            client_order_id=client_order_id,
            # The broker's own instrument identity for the master's order,
            # when known - takes precedence over ambiguous symbol-based NFO
            # resolution (see resolve_instrument_info's provided_token).
            instrument_token=event.instrument_id,
        )

        await self._stamp_submitted(client_order_id)
        order_res = await self.retry_policy.execute(client.place_order, order_req)

        # Step 4: Update Child Order record with Broker result
        unresolved = is_ambiguous_send(order_res)
        if order_res.category == OrderResultCategory.SUCCESS:
            final_status = OrderStatus.OPEN
        elif unresolved:
            # Ambiguous: the broker may hold this order. Not REJECTED - stays
            # PENDING (no broker id) with broker_status TIMEOUT until the sweeper
            # establishes the real outcome from the broker order book.
            final_status = OrderStatus.PENDING
        else:
            final_status = OrderStatus.REJECTED
        try:
            if unresolved:
                await self._record_unresolved_send(client_order_id, order_res)
            await self.save_or_update_order(
                internal_order_id=internal_child_ord_id,
                broker_order_id=order_res.broker_order_id,
                account_id=acc_id,
                group_strategy_id=strategy_id,
                source=OrderSource.SYSTEM,
                exchange=event.exchange,
                symbol=event.symbol,
                side=OrderSide.BUY if event.side == "BUY" else OrderSide.SELL,
                order_type="LIMIT",
                product_type=event.product_type,
                quantity=target_qty,
                filled_quantity=0,
                pending_quantity=target_qty,
                price=limit_price,
                status=final_status,
                broker_status=(
                    "SUCCESS" if final_status == OrderStatus.OPEN else "TIMEOUT" if unresolved else "REJECTED"
                ),
                rejection_reason=order_res.message if final_status == OrderStatus.REJECTED else None,
                master_broker_order_id=event.master_order_id,
            )
        except Exception as db_exc:
            if order_res.category != OrderResultCategory.SUCCESS:
                # Broker itself did not succeed - preserve existing behavior
                # (propagate, caught by replicate_limit_order's gather).
                raise
            # Broker SUCCEEDED but persisting that result failed. The broker
            # already holds a real, live order for this follower - this must
            # never be reported back as if the order itself failed, and no
            # second order may ever be placed to "retry" it.
            logger.critical(
                f"[Pipeline] Broker LIMIT order SUCCEEDED for {follower.display_name} "
                f"(broker_order_id={order_res.broker_order_id}) but DB persistence failed: {db_exc}",
                exc_info=True,
            )
            try:
                async with AsyncSessionLocal() as session:
                    await emit_system_event(
                        session,
                        event_type="LIMIT_ORDER_PERSISTENCE_FAILED",
                        severity="CRITICAL",
                        message=(
                            f"Broker LIMIT order for {follower.display_name} (broker_order_id="
                            f"{order_res.broker_order_id}) succeeded but could not be persisted to the "
                            "Order table. Manual verification required."
                        ),
                        account_id=acc_id,
                        metadata_payload={
                            "account_id": str(acc_id),
                            "symbol": event.symbol,
                            "master_order_id": event.master_order_id,
                            "broker_order_id": order_res.broker_order_id,
                            "internal_order_id": internal_child_ord_id,
                            "operation": "LIMIT_ORDER_CREATE",
                            "db_error": str(db_exc),
                        },
                    )
            except Exception as event_exc:
                logger.critical(
                    f"[Pipeline] Failed to emit LIMIT_ORDER_PERSISTENCE_FAILED SystemEvent: {event_exc}",
                    exc_info=True,
                )
            return {
                "status": "SUCCESS",
                "broker_order_id": order_res.broker_order_id,
                "quantity": target_qty,
                "price": str(limit_price),
                "warning": "DB_PERSISTENCE_FAILED",
            }

        logger.info(
            f"[Pipeline] Child LIMIT order placed for {follower.display_name}: {target_qty} @ ₹{limit_price} "
            f"-> Status: {final_status.value} (Broker ID: {order_res.broker_order_id})"
        )

        # Audit-trail parity with _process_single_market_follower, which
        # already writes this on every broker outcome - the LIMIT path wrote
        # Order/CopyTrade state correctly but never recorded an AuditLog
        # entry at all for its outcome (not even on success). Pure
        # observability addition: no new broker call, no CopyTrade state
        # change (already finalized above), never consulted by any
        # dashboard/reconciliation code today (grepped: nothing reads
        # AuditLog.event_type == "COPY_EXECUTED" anywhere).
        async with AsyncSessionLocal() as session:
            session.add(
                AuditLog(
                    event_type="COPY_EXECUTED" if order_res.category == OrderResultCategory.SUCCESS else "COPY_FAILED",
                    account_id=acc_id,
                    master_order_id=event.master_order_id,
                    copy_event_id=copy_event_uuid,
                    symbol=event.symbol,
                    side=event.side,
                    quantity=target_qty,
                    price=limit_price,
                    reason_code=(
                        unresolved_reason(order_res) if unresolved
                        else order_res.message if order_res.category != OrderResultCategory.SUCCESS else None
                    ),
                )
            )
            await session.commit()

        return {
            "status": "SUCCESS" if final_status == OrderStatus.OPEN else "TIMEOUT" if unresolved else "REJECTED",
            "broker_order_id": order_res.broker_order_id,
            "quantity": target_qty,
            "price": str(limit_price),
        }

    async def _stamp_submitted(self, client_order_id: str) -> None:
        """
        Persist CopyTrade.submitted_at immediately BEFORE the broker call, in its
        own short transaction, so a crash or timeout mid-send still leaves the
        moment the order may have reached the broker (the stale-order sweeper's
        TIMEOUT reconciliation selects on it). Only ever fills a NULL, so a
        repeat can never move it. A failure here must not block or repeat the
        send - the sweeper falls back to completed_at / updated_at.
        """
        try:
            async with AsyncSessionLocal() as session:
                await session.execute(
                    update(CopyTrade)
                    .where(CopyTrade.client_order_id == client_order_id, CopyTrade.submitted_at.is_(None))
                    .values(submitted_at=datetime.now(timezone.utc))
                )
                await session.commit()
        except Exception as exc:
            logger.error(f"[Pipeline] Could not stamp submitted_at for {client_order_id}: {exc}")

    async def _record_unresolved_send(self, client_order_id: str, order_res) -> None:
        """
        An ambiguous (TIMEOUT) LIMIT/SL send: the broker may or may not hold the
        order. Record CopyTrade TIMEOUT (the state the sweeper reconciles against
        the broker order book); never REJECTED/FAILED, never resent.
        """
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(CopyTrade)
                .where(
                    CopyTrade.client_order_id == client_order_id,
                    CopyTrade.status.in_([CopyTradeStatus.PENDING, CopyTradeStatus.SUBMITTED]),
                )
                .values(
                    status=CopyTradeStatus.TIMEOUT,
                    failure_reason=unresolved_reason(order_res),
                    broker_response=order_res.raw_response if order_res.category == OrderResultCategory.TIMEOUT else None,
                )
            )
            await session.commit()

    async def replicate_sl_order(
        self,
        event: MasterTradeEvent,
        clients: Dict[str, AliceBlueClient],
        global_emergency_stop: bool = False,
        stagger_ms: int = 0,
    ) -> Dict[str, Any]:
        """
        IMMEDIATE NATIVE SL / SL-M REPLICATION:
        A master SL/SL-M order still TRIGGER_PENDING is copied to every
        eligible follower as ITS OWN independent SL/SL-M order right away
        - never waiting for the master to trigger. Structurally mirrors
        replicate_limit_order (same follower resolution, staggering,
        gather/summary shape); the SL-specific differences are entirely in
        _process_single_sl_follower.
        """
        stop_active, stopped_followers = await self._resolve_stop_state(global_emergency_stop)

        validation = await self.validator.validate(
            event=event,
            is_emergency_stopped=stop_active,
            is_master_healthy=True,
        )
        if not validation.is_valid:
            logger.warning(f"[Pipeline] Master SL order {event.master_order_id} validation failed: {validation.reason_code}")
            return {"status": "rejected", "reason": validation.reason_code}

        followers, strategy_id = await self._get_eligible_followers_for_master(
            event.account_id, master_order_id=event.master_order_id
        )
        if not followers:
            logger.info(f"[Pipeline] No active followers configured for master {event.account_id}.")
            return {"status": "no_followers"}

        lot_size = self._get_lot_size(event.symbol)

        tasks = []
        for idx, (follower, multiplier) in enumerate(followers):
            if idx > 0 and stagger_ms > 0:
                await asyncio.sleep(stagger_ms / 1000.0)
            tasks.append(
                asyncio.create_task(
                    self._process_single_sl_follower(
                        follower=follower,
                        multiplier=multiplier,
                        event=event,
                        client=clients.get(str(follower.id)),
                        lot_size=lot_size,
                        strategy_id=strategy_id,
                        global_emergency_stop=stop_active,
                        follower_runtime_paused=str(follower.id) in stopped_followers,
                    )
                )
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)
        summary = {}
        for (follower, _), res in zip(followers, results):
            if isinstance(res, Exception):
                logger.error(f"[Pipeline] Exception replicating SL order to {follower.display_name}: {res}")
                summary[str(follower.id)] = {"status": "EXCEPTION", "error": str(res)}
            else:
                summary[str(follower.id)] = res

        return {"status": "completed", "results": summary}

    async def _process_single_sl_follower(
        self,
        follower: Account,
        multiplier: Decimal,
        event: MasterTradeEvent,
        client: Optional[AliceBlueClient],
        lot_size: int,
        strategy_id: Optional[uuid.UUID],
        global_emergency_stop: bool,
        follower_runtime_paused: bool = False,
    ) -> Dict[str, Any]:
        """
        Places an immediate native SL/SL-M order on the follower account,
        mirroring the master's own trigger_price (and, for SL, limit
        price) EXACTLY - never compute_marketable_limit_price, never
        MARKET_ORDER_BUFFER_PERCENT. Those exist only for turning a
        MARKET fill into a marketable LIMIT; a native SL/SL-M order
        already carries its own broker-managed trigger, so there is
        nothing to "make marketable" here.
        """
        acc_id = follower.id
        if not follower.is_active or follower.is_emergency_stopped or follower_runtime_paused:
            logger.info(f"[Pipeline] Follower {follower.display_name} paused/disabled. Skipping SL order.")
            return {"status": "SKIPPED", "reason": "TRADING_DISABLED"}

        sl_price = event.price
        trigger_price = event.trigger_price

        # --- Pre-trade risk gate: identical pattern to
        # _process_single_limit_follower/_process_single_market_follower.
        settings_obj = follower.copy_settings
        risk_audit_event_id = derive_copy_event_id(event.master_order_id, str(acc_id), 1)

        funds_data: Optional[FundsData] = None
        if client:
            try:
                funds_data = await client.get_funds()
            except Exception as exc:
                logger.error(f"[Pipeline] Funds fetch failed for {follower.display_name}: {exc}")

        if funds_data is None:
            async with AsyncSessionLocal() as session:
                session.add(
                    AuditLog(
                        event_type="RISK_REJECTED",
                        account_id=acc_id,
                        master_order_id=event.master_order_id,
                        copy_event_id=risk_audit_event_id,
                        symbol=event.symbol,
                        side=event.side,
                        quantity=0,
                        price=sl_price,
                        reason_code="FUNDS_FETCH_FAILED",
                    )
                )
                await emit_system_event(
                    session,
                    event_type="FUNDS_FETCH_FAILED",
                    severity="WARNING",
                    message=(
                        f"SL copy rejected for {follower.display_name}: broker funds unavailable. "
                        "Failing closed; order NOT placed."
                    ),
                    account_id=acc_id,
                )
            return {"status": "REJECTED", "reasons": ["FUNDS_FETCH_FAILED"]}

        if settings_obj:
            base_qty = QuantityCalculator.calculate_quantity(
                mode=settings_obj.quantity_mode,
                master_qty=event.quantity,
                master_capital=settings.MASTER_CAPITAL,
                follower_capital=funds_data.available_margin,
                lot_size=lot_size,
                settings=settings_obj,
                reference_price=trigger_price if trigger_price > 0 else sl_price,
                multiplier=multiplier,
            )
            target_qty = base_qty
        else:
            base_qty = int(event.quantity * float(multiplier))
            target_qty = (base_qty // lot_size) * lot_size if lot_size > 1 else max(1, base_qty)

        if target_qty <= 0:
            return {"status": "SKIPPED", "reason": "ZERO_LOT_RESULT"}

        if settings_obj:
            try:
                daily_loss = await get_daily_loss_today(acc_id)
                open_exposure = await get_open_exposure(client)
            except Exception as exc:
                logger.error(f"[Pipeline] Risk input fetch failed for {follower.display_name}: {exc}")
                async with AsyncSessionLocal() as session:
                    session.add(
                        AuditLog(
                            event_type="RISK_REJECTED",
                            account_id=acc_id,
                            master_order_id=event.master_order_id,
                            copy_event_id=risk_audit_event_id,
                            symbol=event.symbol,
                            side=event.side,
                            quantity=target_qty,
                            price=sl_price,
                            reason_code="RISK_INPUTS_UNAVAILABLE",
                        )
                    )
                    await session.commit()
                return {"status": "REJECTED", "reasons": ["RISK_INPUTS_UNAVAILABLE"]}

            risk_res = await self._evaluate_risk_exit_aware(
                client=client,
                side=event.side,
                exchange=event.exchange,
                product_type=event.product_type,
                instrument_token=event.instrument_id,
                account=follower,
                settings=settings_obj,
                symbol=event.symbol,
                quantity=target_qty,
                price=trigger_price if trigger_price > 0 else sl_price,
                funds=funds_data,
                daily_loss_today=daily_loss,
                open_exposure=open_exposure,
                follower_runtime_paused=follower_runtime_paused,
                global_emergency_stop=global_emergency_stop,
            )

            if not risk_res.passed:
                reasons_str = ",".join(risk_res.failures)
                logger.warning(f"[Pipeline] Follower {follower.display_name} SL order rejected by risk engine: {reasons_str}")
                async with AsyncSessionLocal() as session:
                    session.add(
                        AuditLog(
                            event_type="RISK_REJECTED",
                            account_id=acc_id,
                            master_order_id=event.master_order_id,
                            copy_event_id=risk_audit_event_id,
                            symbol=event.symbol,
                            side=event.side,
                            quantity=target_qty,
                            price=sl_price,
                            reason_code=reasons_str,
                        )
                    )
                    await session.commit()
                return {"status": "REJECTED", "reasons": risk_res.failures}
        # --- end risk gate ---

        # Follower SELL position-safety gate - reused unchanged. A pending
        # SL SELL, once triggered, becomes a real market sell, so it is
        # gated at PLACEMENT time exactly like a LIMIT/MARKET SELL - never
        # deferred until it actually triggers.
        sell_block = await self._enforce_sell_position_safety(
            follower=follower,
            event=event,
            client=client,
            target_qty=target_qty,
            reference_price=trigger_price if trigger_price > 0 else sl_price,
            copy_event_uuid=risk_audit_event_id,
        )
        if sell_block is not None:
            return sell_block

        internal_child_ord_id = f"JT_CH_SL_{event.master_order_id[:16]}_{str(acc_id)[:8]}"
        client_order_id = f"JT_SL_{event.master_order_id[:16]}_{str(acc_id)[:8]}"
        child_order_type = "SL" if event.order_type.upper() == "SL" else "SLM"

        # Step 1: Claim the event with its CopyTrade uniqueness record BEFORE anything else is written or
        # sent, exactly as the MARKET path does (see _process_single_limit_follower): a redelivered event
        # whose Redis dedupe was bypassed stops HERE - it must not overwrite the existing child Order
        # (save_or_update_order is an upsert) and must not reach the broker a second time.
        copy_event_uuid = derive_copy_event_id(event.master_order_id, str(acc_id), 1)
        async with AsyncSessionLocal() as session:
            ct = CopyTrade(
                id=uuid.uuid4(),
                master_order_id=event.master_order_id,
                follower_account_id=acc_id,
                copy_event_id=copy_event_uuid,
                symbol=event.symbol,
                exchange=event.exchange,
                side=OrderSide.BUY if event.side == "BUY" else OrderSide.SELL,
                quantity=target_qty,
                product_type=event.product_type,
                estimated_price=trigger_price if trigger_price > 0 else sl_price,
                client_order_id=client_order_id,
                status=CopyTradeStatus.PENDING,
            )
            session.add(ct)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                logger.info(
                    f"[Pipeline] Duplicate SL copy detected for master_order_id={event.master_order_id}, "
                    f"follower={acc_id}. Skipping: nothing written, nothing sent."
                )
                return {"status": "SKIPPED", "reason": "DUPLICATE_EVENT"}

        # Step 2: Pre-write child Order in PENDING status - the follower's
        # own SL is, from this point, ALSO TRIGGER_PENDING at the broker,
        # exactly mirroring the master's own pending state.
        await self.save_or_update_order(
            internal_order_id=internal_child_ord_id,
            broker_order_id=None,
            account_id=acc_id,
            group_strategy_id=strategy_id,
            source=OrderSource.SYSTEM,
            exchange=event.exchange,
            symbol=event.symbol,
            side=OrderSide.BUY if event.side == "BUY" else OrderSide.SELL,
            order_type=child_order_type,
            product_type=event.product_type,
            quantity=target_qty,
            filled_quantity=0,
            pending_quantity=target_qty,
            price=sl_price,
            trigger_price=trigger_price,
            status=OrderStatus.PENDING,
            master_broker_order_id=event.master_order_id,
            correlation_id=f"{event.master_order_id}:{acc_id}",
        )

        # Step 3: Submit SL/SL-M order to Child Broker
        if not client:
            return {"status": "REJECTED", "reason": "NO_CLIENT"}

        broker_side = BrokerOrderSide.BUY if event.side == "BUY" else BrokerOrderSide.SELL
        prod_upper = str(event.product_type).upper()
        if prod_upper in ("CNC", "LONGTERM", "DELIVERY"):
            broker_prod = ProductType.CNC
        elif prod_upper in ("NRML", "MTF", "CARRYFORWARD", "MARGIN"):
            broker_prod = ProductType.NRML
        else:
            broker_prod = ProductType.MIS

        order_req = OrderRequest(
            symbol=event.symbol,
            exchange=event.exchange,
            side=broker_side,
            quantity=target_qty,
            product_type=broker_prod,
            order_type=OrderType.SL if child_order_type == "SL" else OrderType.SL_M,
            price=sl_price,
            trigger_price=trigger_price,
            client_order_id=client_order_id,
            instrument_token=event.instrument_id,
        )

        await self._stamp_submitted(client_order_id)
        order_res = await self.retry_policy.execute(client.place_order, order_req)

        # Step 4: Update Child Order record with Broker result. PENDING
        # (not OPEN) on success - the follower's own SL is now sitting
        # TRIGGER_PENDING at the broker too, tracked independently from
        # here (FollowerFillSyncer observes its own eventual trigger/fill;
        # nothing here waits on it or the master's own trigger).
        unresolved = is_ambiguous_send(order_res)
        final_status = (
            OrderStatus.PENDING
            if order_res.category == OrderResultCategory.SUCCESS or unresolved
            else OrderStatus.REJECTED
        )
        try:
            if unresolved:
                await self._record_unresolved_send(client_order_id, order_res)
            await self.save_or_update_order(
                internal_order_id=internal_child_ord_id,
                broker_order_id=order_res.broker_order_id,
                account_id=acc_id,
                group_strategy_id=strategy_id,
                source=OrderSource.SYSTEM,
                exchange=event.exchange,
                symbol=event.symbol,
                side=OrderSide.BUY if event.side == "BUY" else OrderSide.SELL,
                order_type=child_order_type,
                product_type=event.product_type,
                quantity=target_qty,
                filled_quantity=0,
                pending_quantity=target_qty,
                price=sl_price,
                trigger_price=trigger_price,
                status=final_status,
                broker_status=(
                    "TIMEOUT" if unresolved else "SUCCESS" if final_status == OrderStatus.PENDING else "REJECTED"
                ),
                rejection_reason=order_res.message if final_status == OrderStatus.REJECTED else None,
                master_broker_order_id=event.master_order_id,
            )
        except Exception as db_exc:
            if order_res.category != OrderResultCategory.SUCCESS:
                raise
            logger.critical(
                f"[Pipeline] Broker SL order SUCCEEDED for {follower.display_name} "
                f"(broker_order_id={order_res.broker_order_id}) but DB persistence failed: {db_exc}",
                exc_info=True,
            )
            try:
                async with AsyncSessionLocal() as session:
                    await emit_system_event(
                        session,
                        event_type="SL_ORDER_PERSISTENCE_FAILED",
                        severity="CRITICAL",
                        message=(
                            f"Broker SL order for {follower.display_name} (broker_order_id="
                            f"{order_res.broker_order_id}) succeeded but could not be persisted to the "
                            "Order table. Manual verification required."
                        ),
                        account_id=acc_id,
                        metadata_payload={
                            "account_id": str(acc_id),
                            "symbol": event.symbol,
                            "master_order_id": event.master_order_id,
                            "broker_order_id": order_res.broker_order_id,
                            "internal_order_id": internal_child_ord_id,
                            "operation": "SL_ORDER_CREATE",
                            "db_error": str(db_exc),
                        },
                    )
            except Exception as event_exc:
                logger.critical(
                    f"[Pipeline] Failed to emit SL_ORDER_PERSISTENCE_FAILED SystemEvent: {event_exc}",
                    exc_info=True,
                )
            return {
                "status": "SUCCESS",
                "broker_order_id": order_res.broker_order_id,
                "quantity": target_qty,
                "price": str(sl_price),
                "trigger_price": str(trigger_price),
                "warning": "DB_PERSISTENCE_FAILED",
            }

        logger.info(
            f"[Pipeline] Child {child_order_type} order placed for {follower.display_name}: {target_qty} @ "
            f"₹{sl_price} trigger ₹{trigger_price} -> Status: {final_status.value} (Broker ID: {order_res.broker_order_id})"
        )
        return {
            "status": "TIMEOUT" if unresolved else "SUCCESS" if final_status == OrderStatus.PENDING else "REJECTED",
            "broker_order_id": order_res.broker_order_id,
            "quantity": target_qty,
            "price": str(sl_price),
            "trigger_price": str(trigger_price),
        }

    async def handle_pending_sl_modification(
        self,
        event: MasterTradeEvent,
        clients: Dict[str, AliceBlueClient],
        global_emergency_stop: bool = False,
    ) -> Dict[str, Any]:
        """
        Modifies a follower's still-pending native SL/SL-M child when the
        master's own pending SL is modified before it triggers.

        A dedicated, minimal sibling to handle_master_modification rather
        than a reuse of it: that function hardcodes
        modify_order_type = OrderType.LIMIT on every call (a master
        LIMIT->MARKET change is delivered as a LIMIT reprice, never a
        real order-type change - see its own docstring) and never accepts
        a trigger_price, both wrong for a native SL modification. Reuses
        its surrounding patterns (funds/risk-free price-only modify,
        SELL-safety not required here - a pending SL's SELL safety was
        already enforced at creation, and a price/trigger-only edit does
        not change whether the follower is covered) and its "no open
        child" / per-follower failure isolation shape.
        """
        async with AsyncSessionLocal() as session:
            stmt = select(Order).where(
                Order.master_broker_order_id == event.master_order_id,
                Order.source == OrderSource.SYSTEM,
                Order.status == OrderStatus.PENDING,
                Order.order_type.in_(["SL", "SLM"]),
            )
            child_orders = (await session.execute(stmt)).scalars().all()

        if not child_orders:
            return {"status": "no_pending_sl_child", "modifications": {}}

        eligible_followers, _ = await self._get_eligible_followers_for_master(
            event.account_id, existing_children_lookup=True
        )
        follower_multiplier: Dict[str, Decimal] = {str(a.id): m for a, m in eligible_followers}
        _, stopped_followers = await self._resolve_stop_state(global_emergency_stop)

        modify_order_type = OrderType.SL if event.order_type.upper() == "SL" else OrderType.SL_M
        results: Dict[str, str] = {}

        for ord_rec in child_orders:
            acc_id_str = str(ord_rec.account_id)
            client = clients.get(acc_id_str)
            if not client or not ord_rec.broker_order_id:
                continue
            if acc_id_str in stopped_followers:
                results[acc_id_str] = "SKIPPED: TRADING_DISABLED"
                continue

            multiplier = follower_multiplier.get(acc_id_str, Decimal("1.0"))
            lot_size = self._get_lot_size(ord_rec.symbol)
            base_qty = int(event.quantity * float(multiplier))
            target_qty = (base_qty // lot_size) * lot_size if lot_size > 1 else max(1, base_qty)

            try:
                res = await client.modify_order(
                    broker_order_id=ord_rec.broker_order_id,
                    quantity=target_qty,
                    price=event.price,
                    trigger_price=event.trigger_price,
                    symbol=ord_rec.symbol,
                    exchange=ord_rec.exchange,
                    order_type=modify_order_type,
                )
                if res.category == OrderResultCategory.SUCCESS:
                    async with AsyncSessionLocal() as session:
                        up_stmt = (
                            update(Order)
                            .where(Order.id == ord_rec.id)
                            .values(
                                price=event.price,
                                trigger_price=event.trigger_price,
                                quantity=target_qty,
                                pending_quantity=target_qty,
                                updated_at=datetime.now(timezone.utc),
                            )
                        )
                        await session.execute(up_stmt)
                        await session.commit()
                    results[acc_id_str] = "MODIFIED"
                else:
                    await self._record_failed_child_modification(
                        master_order_id=event.master_order_id,
                        ord_rec=ord_rec,
                        acc_id_str=acc_id_str,
                        detail=f"Pending SL modify rejected by broker: {res.message}. Child left untouched.",
                    )
                    results[acc_id_str] = f"MODIFY_FAILED: {res.message}"
            except Exception as exc:
                logger.error(
                    f"[Pipeline] Pending SL modify failed for {acc_id_str} (child {ord_rec.broker_order_id}): {exc}",
                    exc_info=True,
                )
                results[acc_id_str] = f"EXCEPTION: {exc}"

        if not results:
            return {"status": "no_open_child_orders", "modifications": {}}

        failed_accounts = [k for k, v in results.items() if v != "MODIFIED"]
        return {
            "status": "completed" if not failed_accounts else "completed_with_failures",
            "modifications": results,
            "failed_accounts": failed_accounts,
        }

    async def handle_sl_trigger_execution(
        self,
        event: MasterTradeEvent,
        child_orders: List[Order],
        clients: Dict[str, AliceBlueClient],
        global_emergency_stop: bool = False,
    ) -> Dict[str, Any]:
        """
        The master's native SL/SL-M has triggered and filled (ORDER_FILLED,
        reported as MARKET or LIMIT) while its follower SL/SL-M child is
        still PENDING in the DB. Pushes that SAME follower broker order to
        execute now instead of waiting for its own broker-side trigger.

        Modify-in-place only - never cancels, never places. The broker
        order book is the source of truth before and after the modify:
        - broker TRIGGER_PENDING          -> SL/SL-M -> LIMIT (no trigger sent;
          the broker may keep reporting the old slTriggerPrice on the row)
        - broker OPEN/PARTIAL as LIMIT    -> LIMIT reprice of the remainder
          (already triggered independently but stranded at its own limit)
        - anything else                   -> no broker call
        The same funds/RiskEngine and SELL position-safety gates as
        handle_master_modification's reprice apply; any block leaves the
        child untouched as an independent SL. A rejected or unconfirmed
        modify goes through _record_failed_child_modification and the DB
        is not written. Only a broker-confirmed state is persisted, and
        status only ever moves forward.
        """
        marketable_price = compute_marketable_limit_price(event.side, event.price)
        eligible_followers, _ = await self._get_eligible_followers_for_master(
            event.account_id, existing_children_lookup=True
        )
        follower_accounts: Dict[str, Account] = {str(a.id): a for a, _ in eligible_followers}
        stop_active, stopped_followers = await self._resolve_stop_state(global_emergency_stop)

        results: Dict[str, str] = {}
        failed_accounts: List[str] = []
        for ord_rec in child_orders:
            acc_id_str = str(ord_rec.account_id)
            client = clients.get(acc_id_str)
            if not client or not ord_rec.broker_order_id:
                continue
            if stop_active or acc_id_str in stopped_followers:
                results[acc_id_str] = "SKIPPED: TRADING_DISABLED"
                continue
            try:
                outcome = await self._execute_triggered_sl_child(
                    event=event,
                    ord_rec=ord_rec,
                    client=client,
                    account=follower_accounts.get(acc_id_str),
                    marketable_price=marketable_price,
                    stop_active=stop_active,
                )
            except Exception as exc:
                logger.error(
                    f"[Pipeline] SL trigger execution failed for {acc_id_str} "
                    f"(child {ord_rec.broker_order_id}): {exc}",
                    exc_info=True,
                )
                outcome = f"EXCEPTION: {exc}"
            results[acc_id_str] = outcome
            if outcome.split(":")[0] in ("FAILED", "REJECTED", "SELL_BLOCKED", "EXCEPTION"):
                failed_accounts.append(acc_id_str)

        return {
            "status": "completed" if not failed_accounts else "completed_with_failures",
            "executions": results,
            "failed_accounts": failed_accounts,
        }

    async def _execute_triggered_sl_child(
        self,
        *,
        event: MasterTradeEvent,
        ord_rec: Order,
        client: AliceBlueClient,
        account: Optional[Account],
        marketable_price: Optional[Decimal],
        stop_active: bool,
    ) -> str:
        """One follower's half of handle_sl_trigger_execution."""
        master_order_id = event.master_order_id
        acc_id_str = str(ord_rec.account_id)

        before = await self._read_child_broker_record(client, ord_rec.broker_order_id)
        if before is None:
            await self._record_failed_child_modification(
                master_order_id=master_order_id, ord_rec=ord_rec, acc_id_str=acc_id_str,
                detail="Master SL triggered but the follower order book could not be read; child left untouched.",
            )
            return "FAILED: PRE_READ_UNAVAILABLE"

        broker_status = str(before.status or "").upper()
        broker_type = str(before.order_type or "").upper()
        if broker_status == "TRIGGER_PENDING":
            reprice_only = False
        elif broker_status in ("OPEN", "PARTIAL") and broker_type == "LIMIT":
            reprice_only = True
        elif broker_status in ("OPEN", "PARTIAL", "COMPLETE", "CANCELLED", "REJECTED"):
            # Executing, executed or gone on its own - nothing to push.
            # follower_fill_syncer syncs this state through its normal path.
            return f"NO_ACTION: BROKER_{broker_status}"
        else:
            await self._record_failed_child_modification(
                master_order_id=master_order_id, ord_rec=ord_rec, acc_id_str=acc_id_str,
                detail=f"Master SL triggered but the follower order shows unrecognised status '{before.status}'; child left untouched.",
            )
            return f"FAILED: UNRECOGNISED_STATUS_{broker_status}"

        remaining_qty = int(before.quantity or 0) - int(before.filled_quantity or 0)
        if remaining_qty <= 0:
            return "NO_ACTION: NOTHING_REMAINING"

        if marketable_price is None:
            await self._record_failed_child_modification(
                master_order_id=master_order_id, ord_rec=ord_rec, acc_id_str=acc_id_str,
                detail="Master SL fill event carried no usable execution price; child left untouched.",
            )
            return "FAILED: NO_MARKETABLE_PRICE"

        if reprice_only and before.price == marketable_price:
            return "NO_ACTION: ALREADY_AT_TARGET_PRICE"

        # --- SELL position safety: identical to handle_master_modification's
        # reprice gate (honours SELL_POSITION_SAFETY_BYPASS unchanged).
        if ord_rec.side == OrderSide.SELL and settings.SELL_POSITION_SAFETY_BYPASS:
            logger.warning(
                f"[Pipeline] SELL_POSITION_SAFETY_BYPASS active: SL-trigger execution position check "
                f"SKIPPED for child {ord_rec.broker_order_id} (account {acc_id_str}, master {master_order_id})."
            )
        elif ord_rec.side == OrderSide.SELL:
            try:
                available_long = await get_available_long(
                    client=client,
                    exchange=ord_rec.exchange,
                    symbol=ord_rec.symbol,
                    product_type=ord_rec.product_type,
                    provided_token=event.instrument_id,
                )
                insufficient = remaining_qty > available_long
                safety_detail = (
                    f"Follower holds {available_long} long but execution would sell {remaining_qty}."
                )
            except (InstrumentResolutionError, PositionSafetyError) as exc:
                insufficient = True
                safety_detail = f"Follower position could not be established: {exc}"
            if insufficient:
                await self._record_failed_child_modification(
                    master_order_id=master_order_id, ord_rec=ord_rec, acc_id_str=acc_id_str,
                    detail=f"SL-trigger SELL execution blocked by follower position safety. {safety_detail} Child left untouched.",
                )
                return "SELL_BLOCKED: INSUFFICIENT_LONG_POSITION"

        # --- Funds/RiskEngine: identical to handle_master_modification's
        # pre-modify gate, evaluated at the marketable price.
        risk_audit_event_id = derive_copy_event_id(master_order_id, acc_id_str, 1)
        settings_obj = account.copy_settings if account else None

        async def _risk_reject(reason_code: str, quantity: int) -> str:
            async with AsyncSessionLocal() as session:
                session.add(
                    AuditLog(
                        event_type="RISK_REJECTED",
                        account_id=ord_rec.account_id,
                        master_order_id=master_order_id,
                        copy_event_id=risk_audit_event_id,
                        symbol=ord_rec.symbol,
                        side=ord_rec.side.value,
                        quantity=quantity,
                        price=marketable_price,
                        reason_code=reason_code,
                    )
                )
                await session.commit()
            return f"REJECTED: {reason_code}"

        try:
            funds_data = await client.get_funds()
        except Exception as exc:
            logger.error(f"[Pipeline] Funds fetch failed for {acc_id_str} during SL-trigger execution: {exc}")
            funds_data = None
        if funds_data is None:
            return await _risk_reject("FUNDS_FETCH_FAILED", 0)

        if settings_obj:
            try:
                daily_loss = await get_daily_loss_today(ord_rec.account_id)
                open_exposure = await get_open_exposure(client)
            except Exception as exc:
                logger.error(f"[Pipeline] Risk input fetch failed for {acc_id_str} during SL-trigger execution: {exc}")
                return await _risk_reject("RISK_INPUTS_UNAVAILABLE", remaining_qty)

            risk_res = await self._evaluate_risk_exit_aware(
                client=client,
                side=ord_rec.side,
                exchange=ord_rec.exchange,
                product_type=ord_rec.product_type,
                instrument_token=event.instrument_id,
                account=account,
                settings=settings_obj,
                symbol=ord_rec.symbol,
                quantity=remaining_qty,
                price=marketable_price,
                funds=funds_data,
                daily_loss_today=daily_loss,
                open_exposure=open_exposure,
                follower_runtime_paused=False,
                global_emergency_stop=stop_active,
            )
            if not risk_res.passed:
                return await _risk_reject(",".join(risk_res.failures), remaining_qty)

        # --- The single broker call: modify the SAME order in place.
        try:
            res = await client.modify_order(
                broker_order_id=ord_rec.broker_order_id,
                quantity=remaining_qty,
                price=marketable_price,
                trigger_price=Decimal("0"),
                order_type=OrderType.LIMIT,
                symbol=ord_rec.symbol,
                exchange=ord_rec.exchange,
            )
            modify_note = f"modify {res.category.value}: {res.message}"
        except Exception as exc:
            modify_note = f"modify raised: {exc}"

        # --- Broker read-back decides the outcome, not the modify response.
        after = await self._read_child_broker_record(client, ord_rec.broker_order_id)
        if after is None:
            await self._record_failed_child_modification(
                master_order_id=master_order_id, ord_rec=ord_rec, acc_id_str=acc_id_str,
                detail=f"SL-trigger execution could not be verified ({modify_note}); follower order book unreadable. DB not updated.",
            )
            return "FAILED: POST_READ_UNAVAILABLE"

        after_status = str(after.status or "").upper()
        after_type = str(after.order_type or "").upper()
        if after_status in ("COMPLETE", "PARTIAL"):
            # Executed - by our modify or by its own trigger, either way once,
            # under the same brokerOrderId. Persist the broker's state.
            await self._persist_triggered_sl_child_state(ord_rec, after)
            if after_status == "PARTIAL" and after.price != marketable_price:
                await self._record_failed_child_modification(
                    master_order_id=master_order_id, ord_rec=ord_rec, acc_id_str=acc_id_str,
                    detail=(
                        f"Child partially executed but its remaining quantity is working at "
                        f"{after.price}, not the marketable {marketable_price} ({modify_note})."
                    ),
                )
                return "FAILED: PARTIAL_NOT_REPRICED"
            return f"EXECUTED: BROKER_{after_status}"

        # trigger_price is deliberately not checked: Alice Blue keeps the old
        # slTriggerPrice on the row after a successful SL -> LIMIT modify
        # (confirmed live, 2026-10-05, child 26100500126150: OPEN / LIMIT /
        # 504.65 with trigger still 503.85, filled seconds later). A modify
        # that did not apply reads back TRIGGER_PENDING / SL, or OPEN at the
        # child's own limit - both rejected here by status, type and price.
        if (
            after_status == "OPEN"
            and after_type == "LIMIT"
            and after.price == marketable_price
        ):
            await self._persist_triggered_sl_child_state(ord_rec, after)
            return "MODIFIED: WORKING_MARKETABLE_LIMIT"

        await self._record_failed_child_modification(
            master_order_id=master_order_id, ord_rec=ord_rec, acc_id_str=acc_id_str,
            detail=(
                f"SL-trigger execution not confirmed by broker ({modify_note}); read-back shows "
                f"status={after.status} type={after.order_type} price={after.price} "
                f"trigger={after.trigger_price}. Child left as the broker reports it; DB not updated."
            ),
        )
        return "FAILED: NOT_CONFIRMED"

    @staticmethod
    async def _read_child_broker_record(client: AliceBlueClient, broker_order_id: str) -> Optional[Any]:
        """The follower broker order-book record for one order, or None if
        the book cannot be read or the order is not in it."""
        try:
            book = await client.get_order_book()
        except Exception as exc:
            logger.error(f"[Pipeline] Order book fetch failed while checking {broker_order_id}: {exc}")
            return None
        for record in book or []:
            if getattr(record, "broker_order_id", None) == broker_order_id:
                return record
        return None

    async def _persist_triggered_sl_child_state(self, ord_rec: Order, record: Any) -> None:
        """Write the broker-confirmed state of a triggered SL child. Direct
        update(): save_or_update_order cannot clear trigger_price to 0, and
        follower_fill_syncer never refreshes order_type/price/trigger. When
        the state is terminal, the CopyTrade is synced here with the same
        function follower_fill_syncer uses, because the syncer only scans
        non-terminal rows and would never see this transition."""
        status_map = {
            "OPEN": OrderStatus.OPEN,
            "PARTIAL": OrderStatus.PARTIAL,
            "COMPLETE": OrderStatus.COMPLETE,
        }
        new_status = status_map[str(record.status or "").upper()]
        quantity = int(record.quantity or 0) or ord_rec.quantity
        filled = int(record.filled_quantity or 0)
        values = {
            "order_type": str(record.order_type or "LIMIT").upper(),
            "trigger_price": record.trigger_price or Decimal("0"),
            "status": new_status,
            "broker_status": record.status,
            "quantity": quantity,
            "filled_quantity": filled,
            "pending_quantity": max(0, quantity - filled),
            "updated_at": datetime.now(timezone.utc),
        }
        if record.price and record.price > 0:
            values["price"] = record.price
        if record.average_price and record.average_price > 0:
            values["average_price"] = record.average_price

        async with AsyncSessionLocal() as session:
            await session.execute(update(Order).where(Order.id == ord_rec.id).values(**values))
            await session.commit()

        if new_status == OrderStatus.COMPLETE:
            from app.services.reliability.follower_fill_syncer import FollowerFillSyncer
            await FollowerFillSyncer._sync_copy_trade(ord_rec, new_status)

    async def handle_master_cancellation(
        self,
        master_order_id: str,
        clients: Dict[str, AliceBlueClient],
    ) -> Dict[str, Any]:
        """
        Cancels remaining pending quantities on child accounts when master LIMIT order is cancelled.
        Never touches or reverses already filled quantities.

        A fill can land at the broker in the race window between the
        Order row being read here and client.cancel_order() confirming -
        the pre-cancel ord_rec.filled_quantity snapshot cannot be trusted to
        finalize the row. After a successful cancel, the broker's order
        book is re-fetched (client.get_order_book(), the same interface
        FollowerFillSyncer already uses) and matched by broker_order_id to
        get the true post-cancel filled_quantity before the row is
        finalized. If that verification fails or the order can't be found,
        the row is left unchanged (never marked CANCELLED on unverified
        data) and a SystemEvent is emitted for manual review - one
        follower's verification failure never affects any other follower.
        """
        async with AsyncSessionLocal() as session:
            stmt = select(Order).where(
                Order.master_broker_order_id == master_order_id,
                Order.source == OrderSource.SYSTEM,
                Order.status.in_([OrderStatus.OPEN, OrderStatus.PARTIAL, OrderStatus.PENDING]),
            )
            child_orders = (await session.execute(stmt)).scalars().all()

        if not child_orders:
            logger.info(f"[Pipeline] No open child orders to cancel for master order {master_order_id}")
            return {"status": "no_open_child_orders"}

        results = {}
        cancel_failures: List[str] = []
        for ord_rec in child_orders:
            acc_id_str = str(ord_rec.account_id)
            client = clients.get(acc_id_str)
            if not client or not ord_rec.broker_order_id:
                continue

            try:
                res = await client.cancel_order(
                    broker_order_id=ord_rec.broker_order_id,
                    symbol=ord_rec.symbol,
                    exchange=ord_rec.exchange,
                )
                if res.category == OrderResultCategory.SUCCESS:
                    confirmed_filled_quantity: Optional[int] = None
                    try:
                        book = await client.get_order_book()
                        record = next(
                            (r for r in book if r.broker_order_id == ord_rec.broker_order_id), None
                        )
                        if record is not None:
                            confirmed_filled_quantity = record.filled_quantity
                    except Exception as exc:
                        logger.error(
                            f"[Pipeline] Post-cancel order-book verification failed for "
                            f"{ord_rec.broker_order_id}: {exc}"
                        )
                        confirmed_filled_quantity = None

                    if confirmed_filled_quantity is None:
                        logger.warning(
                            f"[Pipeline] Could not verify post-cancel fill state for child order "
                            f"{ord_rec.broker_order_id} (account {acc_id_str}); leaving row unchanged."
                        )
                        async with AsyncSessionLocal() as session:
                            await emit_system_event(
                                session,
                                event_type="CANCELLATION_VERIFICATION_FAILED",
                                severity="CRITICAL",
                                message=(
                                    f"Broker confirmed cancel for child order {ord_rec.broker_order_id} "
                                    f"(account {acc_id_str}) but post-cancel fill verification failed or "
                                    "the order was not found in the broker order book. Order row left "
                                    "unchanged; manual verification required."
                                ),
                                account_id=ord_rec.account_id,
                            )
                        results[acc_id_str] = "VERIFICATION_FAILED"
                        continue

                    # Never let a verified value regress a fill already recorded.
                    final_filled_quantity = max(confirmed_filled_quantity, ord_rec.filled_quantity)
                    final_status = OrderStatus.CANCELLED if final_filled_quantity == 0 else OrderStatus.PARTIAL

                    async with AsyncSessionLocal() as session:
                        up_stmt = (
                            update(Order)
                            .where(Order.id == ord_rec.id)
                            .values(
                                status=final_status,
                                broker_status="CANCELLED",
                                filled_quantity=final_filled_quantity,
                                pending_quantity=0,
                                updated_at=datetime.now(timezone.utc),
                            )
                        )
                        await session.execute(up_stmt)
                        await session.commit()

                    # Mirror onto the matching CopyTrade row - this function
                    # updates Order directly (not via save_or_update_order/
                    # FollowerFillSyncer), so without this the CopyTrade row
                    # a follower's Demat card metrics actually reads
                    # (accounts.py::get_all_account_metrics) would stay
                    # stuck at PENDING/SUBMITTED even though the order was
                    # correctly resolved. Both cancellation outcomes are
                    # covered: a zero-fill CANCELLED has no CopyTradeStatus
                    # equivalent, so it maps to FAILED - the same bucket the
                    # metrics endpoint already groups into "cancelled_count".
                    # A PARTIAL (some quantity genuinely filled before the
                    # remainder was cancelled) maps to SUCCESS instead - real
                    # shares were bought/sold, which is what SUCCESS means
                    # for a CopyTrade row (contributing to completed_count/
                    # pnl), not a failure.
                    if final_status in (OrderStatus.CANCELLED, OrderStatus.PARTIAL):
                        new_ct_status = (
                            CopyTradeStatus.FAILED
                            if final_status == OrderStatus.CANCELLED
                            else CopyTradeStatus.SUCCESS
                        )
                        async with AsyncSessionLocal() as session:
                            ct_stmt = select(CopyTrade).where(
                                CopyTrade.broker_order_id == ord_rec.broker_order_id
                            )
                            trade = (await session.execute(ct_stmt)).scalar_one_or_none()
                            if trade is None:
                                ct_stmt = (
                                    select(CopyTrade)
                                    .where(
                                        CopyTrade.master_order_id == master_order_id,
                                        CopyTrade.follower_account_id == ord_rec.account_id,
                                        CopyTrade.status.in_(
                                            [CopyTradeStatus.PENDING, CopyTradeStatus.SUBMITTED]
                                        ),
                                    )
                                    .order_by(CopyTrade.created_at.asc())
                                )
                                trade = (await session.execute(ct_stmt)).scalars().first()
                            if trade is not None:
                                trade.status = new_ct_status
                                trade.broker_order_id = trade.broker_order_id or ord_rec.broker_order_id
                                if final_status == OrderStatus.CANCELLED:
                                    trade.failure_reason = "Master order cancelled; child order cancelled at broker"
                                else:
                                    trade.failure_reason = (
                                        f"Master order cancelled after partial fill; child filled "
                                        f"{final_filled_quantity}/{ord_rec.quantity} before broker cancellation"
                                    )
                                trade.completed_at = datetime.now(timezone.utc)
                                await session.commit()

                    results[acc_id_str] = final_status.value
                else:
                    await self._record_failed_child_cancellation(
                        master_order_id=master_order_id,
                        ord_rec=ord_rec,
                        acc_id_str=acc_id_str,
                        detail=f"Broker did not accept the cancel request: {res.message}",
                    )
                    cancel_failures.append(acc_id_str)
                    results[acc_id_str] = f"CANCEL_FAILED: {res.message}"
            except Exception as e:
                await self._record_failed_child_cancellation(
                    master_order_id=master_order_id,
                    ord_rec=ord_rec,
                    acc_id_str=acc_id_str,
                    detail=f"Cancel request raised {type(e).__name__}: {e}",
                )
                cancel_failures.append(acc_id_str)
                results[acc_id_str] = f"ERROR: {e}"

        if cancel_failures:
            return {
                "status": "completed_with_failures",
                "cancellations": results,
                "failed_accounts": cancel_failures,
            }
        return {"status": "completed", "cancellations": results}

    async def _record_failed_child_cancellation(
        self,
        master_order_id: str,
        ord_rec: Order,
        acc_id_str: str,
        detail: str,
    ) -> None:
        """
        Surface a child-order cancellation the broker did not accept.

        This is the fail-closed direction for cancels. When the master is
        already flat but the child cancel did not go through, the child
        order is still live at the broker and can still fill into a
        position the master no longer wants - so a failed cancel must be
        louder than a successful one, never quieter. Previously both
        failure branches only wrote a string into the returned dict, the
        method still returned {"status": "completed"}, and the caller
        logged the whole cancellation as completed: a broker HTTP 400 was
        indistinguishable from a clean cancel in the logs, in
        system_events, and in copy_trades.

        The Order row is deliberately NOT written here. The child's true
        state is unknown at this point, and FollowerFillSyncer stays the
        only writer that moves an order row on verified broker data - the
        same rule the success path follows when its post-cancel
        verification fails.

        Alerting and the CopyTrade update are isolated so that a failure
        in either one still leaves the other attempted, and neither can
        abort the caller's loop over the remaining followers.
        """
        logger.error(
            f"[Pipeline] Child order cancellation FAILED for {ord_rec.broker_order_id} "
            f"(account {acc_id_str}, master order {master_order_id}): {detail} "
            "Child order may still be live at the broker."
        )

        try:
            async with AsyncSessionLocal() as session:
                await emit_system_event(
                    session,
                    event_type="CHILD_CANCELLATION_FAILED",
                    severity="CRITICAL",
                    message=(
                        f"Failed to cancel child order {ord_rec.broker_order_id} "
                        f"(account {acc_id_str}) after master order {master_order_id} "
                        f"was cancelled. {detail} The child order may still be live at "
                        "the broker and could fill into a position the master no longer "
                        "holds; manual verification required."
                    ),
                    account_id=ord_rec.account_id,
                )
        except Exception as exc:
            logger.error(
                f"[Pipeline] Failed to emit CHILD_CANCELLATION_FAILED SystemEvent for "
                f"{ord_rec.broker_order_id}: {exc}",
                exc_info=True,
            )

        try:
            async with AsyncSessionLocal() as session:
                ct_stmt = select(CopyTrade).where(
                    CopyTrade.broker_order_id == ord_rec.broker_order_id
                )
                trade = (await session.execute(ct_stmt)).scalar_one_or_none()
                if trade is None:
                    ct_stmt = (
                        select(CopyTrade)
                        .where(
                            CopyTrade.master_order_id == master_order_id,
                            CopyTrade.follower_account_id == ord_rec.account_id,
                            CopyTrade.status.in_(
                                [CopyTradeStatus.PENDING, CopyTradeStatus.SUBMITTED]
                            ),
                        )
                        .order_by(CopyTrade.created_at.asc())
                    )
                    trade = (await session.execute(ct_stmt)).scalars().first()
                if trade is not None:
                    trade.status = CopyTradeStatus.RECONCILIATION_REQUIRED
                    trade.broker_order_id = trade.broker_order_id or ord_rec.broker_order_id
                    trade.failure_reason = (
                        f"Master order cancelled but child cancel failed: {detail}"
                    )
                    await session.commit()
        except Exception as exc:
            logger.error(
                f"[Pipeline] Failed to mark CopyTrade RECONCILIATION_REQUIRED after failed "
                f"cancel of {ord_rec.broker_order_id}: {exc}",
                exc_info=True,
            )

    async def handle_master_modification(
        self,
        master_order_id: str,
        new_price: Decimal,
        new_quantity: int,
        clients: Dict[str, AliceBlueClient],
        master_account_id: Optional[str] = None,
        global_emergency_stop: bool = False,
        new_order_type: Optional[str] = None,
        enforce_sell_position_safety: bool = False,
        instrument_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Modifies pending price/quantity (and, since the LIMIT->MARKET
        production fix, order_type) on open child orders.

        The master's new_quantity is turned into each follower's target
        quantity using the SAME rules _process_single_limit_follower uses at
        order creation (Issue #7 / #8): QuantityCalculator.calculate_quantity
        when the follower has a CopySetting (honoring quantity_mode - FIXED/
        RATIO/CAPITAL_PROPORTIONAL/RISK_BASED), else the plain
        int(new_quantity * multiplier) formula with lot-size flooring
        applied. Already-filled quantity is then subtracted to get the
        remaining pending requirement, floored at 1, and is never itself
        touched or reduced.

        Runs the same funds/RiskEngine gate the LIMIT creation path uses
        before sending modify_order, failing closed per-follower (funds
        unavailable, risk inputs unavailable, or a risk check failure all
        skip that follower only) without affecting any other follower.

        new_order_type ("LIMIT"/"MARKET"/None): the master's CURRENT
        order_type. A child is NEVER converted to MARKET - Alice Blue
        rejects orderType=MARKET on the modify endpoint with EC965 "Market
        orders are not allowed" (HTTP 200, confirmed in production against
        child order 26092200160139 on 2026-09-22, after this code first
        attempted exactly that). "MARKET" is therefore clamped to "LIMIT"
        below, and a master LIMIT->MARKET change reaches the child as a
        marketable-LIMIT REPRICE instead: execute_replication's
        ORDER_FILLED branch derives the price from the master's CONFIRMED
        fill via compute_marketable_limit_price and passes it as new_price
        with new_order_type="LIMIT". The child keeps order_type=LIMIT and
        keeps its own broker order id; only its price moves. Nothing is
        ever cancelled or replaced.

        enforce_sell_position_safety: when True, a SELL child is only
        repriced if the follower still holds enough long to cover it,
        reusing the deployed get_available_long helper. Repricing a SELL to
        a marketable price makes it very likely to fill, so the same rule
        that guards placement has to guard this. Defaults to False so every
        pre-existing price/quantity modification call site keeps its exact
        current behavior.

        instrument_token: the master event's own broker instrument id
        (MasterTradeEvent.instrument_id), forwarded to get_available_long's
        provided_token so this SELL-safety check uses the same token-primary
        position matching _enforce_sell_position_safety already uses at
        placement time. Without it, an NFO SELL reprice fell back to
        symbol-only resolution of ord_rec.symbol - the master order book's
        own (sometimes ambiguous, e.g. a bare "NIFTY") spelling for an
        option/future - which can resolve to the wrong contract's token and
        check the wrong position. Optional/None-safe: get_available_long
        already falls back to symbol matching when no token is available,
        so this is purely additive.

        If the broker rejects a modify, this reuses the EXISTING failure
        path (_record_failed_child_modification -> CopyTrade
        RECONCILIATION_REQUIRED) unchanged: no automatic cancel-and-replace
        is attempted. A blind replace could violate every one of "never
        touch a filled child", "never duplicate pending quantity", and
        "idempotent under repeated events" at once; surfacing an unresolved
        change for manual reconciliation is the safer choice.
        """
        async with AsyncSessionLocal() as session:
            stmt = select(Order).where(
                Order.master_broker_order_id == master_order_id,
                Order.source == OrderSource.SYSTEM,
                Order.status.in_([OrderStatus.OPEN, OrderStatus.PARTIAL]),
            )
            child_orders = (await session.execute(stmt)).scalars().all()

            # --- Finding P2 #7: PENDING-status race visibility -------------
            # _process_single_limit_follower pre-writes a follower's child
            # Order row as status=PENDING with broker_order_id=None BEFORE
            # calling the broker, and only flips it to OPEN (with the real
            # broker_order_id) AFTER the broker confirms. If the master LIMIT
            # order is modified inside that window, this query (OPEN/PARTIAL
            # only) will not see that follower's row.
            #
            # Simply adding PENDING to the status filter above would NOT fix
            # this: the per-order loop below already has
            # `if not client or not ord_rec.broker_order_id: continue`, and a
            # PENDING row always has broker_order_id=None at this stage - it
            # would just be silently skipped via a different code path, with
            # no risk of calling modify_order against a null broker_order_id
            # but also no actual delivery of the modification. There is no
            # broker order yet to modify, and no safe/small way from inside
            # this single request-response call to defer-and-retry the
            # modification once the in-flight order creation completes
            # (that would need a retry queue / background scheduler, which
            # is out of scope for a pipeline.py-only, non-architectural fix).
            #
            # So instead of leaving this a genuinely silent drop, make it
            # OBSERVABLE: find any PENDING child rows for this
            # master_order_id and emit a WARNING SystemEvent per row so the
            # gap is visible for manual follow-up, matching the existing
            # emit_system_event pattern used elsewhere in this file (e.g.
            # CANCELLATION_VERIFICATION_FAILED, *_PERSISTENCE_FAILED).
            pending_stmt = select(Order).where(
                Order.master_broker_order_id == master_order_id,
                Order.source == OrderSource.SYSTEM,
                Order.status == OrderStatus.PENDING,
            )
            pending_child_orders = (await session.execute(pending_stmt)).scalars().all()

        pending_race_accounts: List[str] = []
        modify_failures: List[str] = []
        for pending_ord in pending_child_orders:
            acc_id_str_pending = str(pending_ord.account_id)
            pending_race_accounts.append(acc_id_str_pending)
            logger.warning(
                f"[Pipeline] Master order {master_order_id} modification received while "
                f"child order {pending_ord.internal_order_id} (account {acc_id_str_pending}) "
                "was still PENDING broker confirmation (no broker_order_id yet); the "
                "modification could NOT be applied to this follower's order."
            )
            async with AsyncSessionLocal() as session:
                await emit_system_event(
                    session,
                    event_type="MODIFICATION_SKIPPED_PENDING_RACE",
                    severity="WARNING",
                    message=(
                        f"Master order {master_order_id} was modified while the child order "
                        f"for account {acc_id_str_pending} was still PENDING (broker "
                        "round-trip for order creation not yet confirmed). The new "
                        "price/quantity could not be applied to this follower's order. "
                        "Manual verification/follow-up required."
                    ),
                    account_id=pending_ord.account_id,
                    metadata_payload={
                        "account_id": acc_id_str_pending,
                        "master_order_id": master_order_id,
                        "internal_order_id": pending_ord.internal_order_id,
                        "symbol": pending_ord.symbol,
                    },
                )
        # --- end Finding P2 #7 -------------------------------------------

        if not child_orders:
            return {
                "status": "no_open_child_orders",
                "modifications": {},
                "pending_race_accounts": pending_race_accounts,
            }

        eligible_followers, _ = await self._get_eligible_followers_for_master(
            master_account_id, existing_children_lookup=True
        )
        follower_info: Dict[str, tuple] = {
            str(account.id): (account, multiplier) for account, multiplier in eligible_followers
        }
        stop_active, stopped_followers = await self._resolve_stop_state(global_emergency_stop)

        results = {}
        for ord_rec in child_orders:
            acc_id_str = str(ord_rec.account_id)
            client = clients.get(acc_id_str)
            if not client or not ord_rec.broker_order_id:
                continue

            account, multiplier = follower_info.get(acc_id_str, (None, Decimal("1.0")))
            settings_obj = account.copy_settings if account else None
            lot_size = self._get_lot_size(ord_rec.symbol)

            # --- Pre-modify risk gate: same funds/RiskEngine pattern as the
            # LIMIT creation path (_process_single_limit_follower). Fail
            # closed - this follower's order is never modified if funds
            # can't be fetched, risk inputs can't be fetched, or the risk
            # engine rejects; other followers are unaffected.
            risk_audit_event_id = derive_copy_event_id(master_order_id, acc_id_str, 1)
            follower_runtime_paused = acc_id_str in stopped_followers

            # The RiskEngine block below is the only emergency-stop check on
            # this path, and it only runs when the child has a CopySetting.
            # A child without one (e.g. its member toggle is off, or it was
            # created as account_type MASTER and its strategy's trading is
            # off) was therefore modified even with the global stop active
            # or the account paused. Apply exactly those two stop checks
            # here, with RiskEngine's own reason codes; the CopySetting path
            # is untouched.
            if settings_obj is None and (stop_active or follower_runtime_paused):
                stop_reason = "EMERGENCY_STOP_ACTIVE" if stop_active else "FOLLOWER_EMERGENCY_STOP"
                logger.warning(
                    f"[Pipeline] Master modification of {master_order_id} blocked for {acc_id_str}: {stop_reason}."
                )
                async with AsyncSessionLocal() as session:
                    session.add(
                        AuditLog(
                            event_type="RISK_REJECTED",
                            account_id=ord_rec.account_id,
                            master_order_id=master_order_id,
                            copy_event_id=risk_audit_event_id,
                            symbol=ord_rec.symbol,
                            side=ord_rec.side.value,
                            quantity=0,
                            price=new_price,
                            reason_code=stop_reason,
                        )
                    )
                    await session.commit()
                results[acc_id_str] = f"REJECTED: {stop_reason}"
                continue

            try:
                funds_data = await client.get_funds()
            except Exception as exc:
                logger.error(f"[Pipeline] Funds fetch failed for {acc_id_str} during modification: {exc}")
                funds_data = None

            if funds_data is None:
                async with AsyncSessionLocal() as session:
                    session.add(
                        AuditLog(
                            event_type="RISK_REJECTED",
                            account_id=ord_rec.account_id,
                            master_order_id=master_order_id,
                            copy_event_id=risk_audit_event_id,
                            symbol=ord_rec.symbol,
                            side=ord_rec.side.value,
                            quantity=0,
                            price=new_price,
                            reason_code="FUNDS_FETCH_FAILED",
                        )
                    )
                    await emit_system_event(
                        session,
                        event_type="FUNDS_FETCH_FAILED",
                        severity="WARNING",
                        message=(
                            f"Master order modification skipped for account {acc_id_str}: broker funds "
                            "unavailable. Failing closed; order NOT modified."
                        ),
                        account_id=ord_rec.account_id,
                    )
                results[acc_id_str] = "REJECTED: FUNDS_FETCH_FAILED"
                continue

            # Calculate target quantity - mirrors the LIMIT creation path
            # exactly (_process_single_limit_follower / Issue #7): honor the
            # follower's configured QuantityCalculator strategy when a
            # CopySetting exists, else fall back to the plain multiplier
            # formula with lot-size flooring applied (previously this
            # fallback never floored to lot size at all).
            if settings_obj:
                scaled_target_quantity = QuantityCalculator.calculate_quantity(
                    mode=settings_obj.quantity_mode,
                    master_qty=new_quantity,
                    master_capital=settings.MASTER_CAPITAL,
                    follower_capital=funds_data.available_margin,
                    lot_size=lot_size,
                    settings=settings_obj,
                    reference_price=new_price,
                    multiplier=multiplier,
                )
            else:
                base_qty = int(new_quantity * float(multiplier))
                scaled_target_quantity = (base_qty // lot_size) * lot_size if lot_size > 1 else base_qty
            remaining_qty = max(1, scaled_target_quantity - ord_rec.filled_quantity)

            if settings_obj:
                try:
                    daily_loss = await get_daily_loss_today(ord_rec.account_id)
                    open_exposure = await get_open_exposure(client)
                except Exception as exc:
                    logger.error(f"[Pipeline] Risk input fetch failed for {acc_id_str} during modification: {exc}")
                    async with AsyncSessionLocal() as session:
                        session.add(
                            AuditLog(
                                event_type="RISK_REJECTED",
                                account_id=ord_rec.account_id,
                                master_order_id=master_order_id,
                                copy_event_id=risk_audit_event_id,
                                symbol=ord_rec.symbol,
                                side=ord_rec.side.value,
                                quantity=remaining_qty,
                                price=new_price,
                                reason_code="RISK_INPUTS_UNAVAILABLE",
                            )
                        )
                        await session.commit()
                    results[acc_id_str] = "REJECTED: RISK_INPUTS_UNAVAILABLE"
                    continue

                risk_res = await self._evaluate_risk_exit_aware(
                    client=client,
                    side=ord_rec.side,
                    exchange=ord_rec.exchange,
                    product_type=ord_rec.product_type,
                    instrument_token=instrument_token,
                    account=account,
                    settings=settings_obj,
                    symbol=ord_rec.symbol,
                    quantity=remaining_qty,
                    price=new_price,
                    funds=funds_data,
                    daily_loss_today=daily_loss,
                    open_exposure=open_exposure,
                    follower_runtime_paused=follower_runtime_paused,
                    # Finding P3 #12: thread the ALREADY-RESOLVED stop_active
                    # value (computed once above via self._resolve_stop_state),
                    # not the raw global_emergency_stop parameter this function
                    # received - engine_loop.py never passes that parameter
                    # explicitly on execute_replication(), so it is always
                    # False in production. Harmless today only because
                    # EventValidator's upstream gate is not consulted here at
                    # all for modifications; this keeps RiskEngine's own inner
                    # check meaningful as defense-in-depth.
                    global_emergency_stop=stop_active,
                )

                if not risk_res.passed:
                    reasons_str = ",".join(risk_res.failures)
                    logger.warning(f"[Pipeline] Master modification rejected by risk engine for {acc_id_str}: {reasons_str}")
                    async with AsyncSessionLocal() as session:
                        session.add(
                            AuditLog(
                                event_type="RISK_REJECTED",
                                account_id=ord_rec.account_id,
                                master_order_id=master_order_id,
                                copy_event_id=risk_audit_event_id,
                                symbol=ord_rec.symbol,
                                side=ord_rec.side.value,
                                quantity=remaining_qty,
                                price=new_price,
                                reason_code=reasons_str,
                            )
                        )
                        await session.commit()
                    results[acc_id_str] = f"REJECTED: {reasons_str}"
                    continue
            # --- end risk gate ---

            # LIMIT->MARKET production fix: resolve what order_type this
            # specific child should end up as. None/unchanged means every
            # existing price/quantity-only call site behaves exactly as
            # before (target_order_type == this child's own current type,
            # so the modify_order call below sends the same orderType it
            # always has). See this function's docstring for what happens
            # when it DOES differ.
            child_order_type = str(ord_rec.order_type or "LIMIT").upper()
            requested_order_type = (new_order_type or child_order_type).upper()
            # Alice Blue refuses orderType=MARKET on the modify endpoint:
            # HTTP 200 with {"status":"EC965","message":"Market orders are
            # not allowed."} - confirmed in production against child order
            # 26092200160139 on 2026-09-22. A child is therefore NEVER
            # converted to MARKET. A master LIMIT->MARKET change is
            # delivered instead as a marketable-LIMIT REPRICE of the same
            # child order (see execute_replication's ORDER_FILLED branch,
            # which computes the price from the master's confirmed fill),
            # so the child keeps order_type=LIMIT and keeps its broker
            # order id. Clamped here as well as at the call site so no
            # caller can reintroduce a request the broker will always
            # reject.
            target_order_type = "LIMIT" if requested_order_type == "MARKET" else requested_order_type
            modify_order_type = OrderType.LIMIT

            # Repricing a SELL child to a MARKETABLE price turns an order
            # that was sitting harmlessly unfilled into one that will very
            # likely execute, so the deployed follower-position safety rule
            # has to hold here too - otherwise a follower whose long
            # disappeared meanwhile gets pushed short by a reprice. Opt-in
            # so existing price/quantity modification call sites keep their
            # current behavior byte-for-byte. Reuses the deployed
            # get_available_long helper unchanged.
            if enforce_sell_position_safety and ord_rec.side == OrderSide.SELL and settings.SELL_POSITION_SAFETY_BYPASS:
                logger.warning(
                    f"[Pipeline] SELL_POSITION_SAFETY_BYPASS active: SELL reprice position check "
                    f"SKIPPED for child {ord_rec.broker_order_id} (account {acc_id_str}, master {master_order_id})."
                )
            elif enforce_sell_position_safety and ord_rec.side == OrderSide.SELL:
                try:
                    available_long = await get_available_long(
                        client=client,
                        exchange=ord_rec.exchange,
                        symbol=ord_rec.symbol,
                        product_type=ord_rec.product_type,
                        provided_token=instrument_token,
                    )
                    insufficient = remaining_qty > available_long
                    safety_detail = (
                        f"Follower holds {available_long} long but the reprice would leave "
                        f"{remaining_qty} working to sell."
                    )
                except (InstrumentResolutionError, PositionSafetyError) as exc:
                    available_long, insufficient = 0, True
                    safety_detail = f"Follower position could not be established: {exc}"

                if insufficient:
                    await self._record_failed_child_modification(
                        master_order_id=master_order_id,
                        ord_rec=ord_rec,
                        acc_id_str=acc_id_str,
                        detail=(
                            "SELL reprice blocked by follower position safety. "
                            f"{safety_detail} Child left untouched; nothing cancelled or replaced."
                        ),
                    )
                    modify_failures.append(acc_id_str)
                    results[acc_id_str] = "SELL_REPRICE_BLOCKED: INSUFFICIENT_LONG_POSITION"
                    continue

            try:
                res = await client.modify_order(
                    broker_order_id=ord_rec.broker_order_id,
                    quantity=remaining_qty,
                    price=new_price,
                    symbol=ord_rec.symbol,
                    exchange=ord_rec.exchange,
                    order_type=modify_order_type,
                )
                if res.category == OrderResultCategory.SUCCESS:
                    try:
                        async with AsyncSessionLocal() as session:
                            up_stmt = (
                                update(Order)
                                .where(Order.id == ord_rec.id)
                                .values(
                                    price=new_price,
                                    pending_quantity=remaining_qty,
                                    order_type=target_order_type,
                                    updated_at=datetime.now(timezone.utc),
                                )
                            )
                            await session.execute(up_stmt)
                            await session.commit()
                        results[acc_id_str] = "MODIFIED"
                    except Exception as db_exc:
                        # Broker modify_order already SUCCEEDED - the broker
                        # now reflects the new price/quantity for this child
                        # order. Losing the local status update must never be
                        # reported as if the modification itself failed, and
                        # must never trigger a second modify_order call.
                        logger.critical(
                            f"[Pipeline] Broker modify_order SUCCEEDED for child order "
                            f"{ord_rec.broker_order_id} (account {acc_id_str}) but DB persistence "
                            f"failed: {db_exc}",
                            exc_info=True,
                        )
                        try:
                            async with AsyncSessionLocal() as session:
                                await emit_system_event(
                                    session,
                                    event_type="MODIFICATION_PERSISTENCE_FAILED",
                                    severity="CRITICAL",
                                    message=(
                                        f"Broker order modification for child order "
                                        f"{ord_rec.broker_order_id} (account {acc_id_str}) succeeded "
                                        "but could not be persisted to the Order table. Manual "
                                        "verification required."
                                    ),
                                    account_id=ord_rec.account_id,
                                    metadata_payload={
                                        "account_id": acc_id_str,
                                        "symbol": ord_rec.symbol,
                                        "master_order_id": master_order_id,
                                        "broker_order_id": ord_rec.broker_order_id,
                                        "operation": "MASTER_MODIFICATION",
                                        "db_error": str(db_exc),
                                    },
                                )
                        except Exception as event_exc:
                            logger.critical(
                                f"[Pipeline] Failed to emit MODIFICATION_PERSISTENCE_FAILED "
                                f"SystemEvent: {event_exc}",
                                exc_info=True,
                            )
                        results[acc_id_str] = "MODIFIED_DB_PERSISTENCE_FAILED"
                else:
                    await self._record_failed_child_modification(
                        master_order_id=master_order_id,
                        ord_rec=ord_rec,
                        acc_id_str=acc_id_str,
                        detail=f"Broker did not accept the modify request: {res.message}",
                    )
                    modify_failures.append(acc_id_str)
                    results[acc_id_str] = f"MODIFY_FAILED: {res.message}"
            except Exception as e:
                await self._record_failed_child_modification(
                    master_order_id=master_order_id,
                    ord_rec=ord_rec,
                    acc_id_str=acc_id_str,
                    detail=f"Modify request raised {type(e).__name__}: {e}",
                )
                modify_failures.append(acc_id_str)
                results[acc_id_str] = f"ERROR: {e}"

        return {
            "status": "completed_with_failures" if modify_failures else "completed",
            "modifications": results,
            "pending_race_accounts": pending_race_accounts,
            "failed_accounts": modify_failures,
        }

    async def _record_failed_child_modification(
        self,
        master_order_id: str,
        ord_rec: Order,
        acc_id_str: str,
        detail: str,
    ) -> None:
        """
        Surface a child-order modification the broker did not accept.

        Mirrors _record_failed_child_cancellation for the modify path, which
        carried the identical swallow: both failure branches only wrote a
        string into the returned dict while the method still returned
        {"status": "completed"}. That path had never actually run in
        production (modifyorder was never called once, because the poller
        never dispatched ORDER_MODIFIED), so enabling MODIFY without this
        would have shipped the same defect that was just fixed for CANCEL.

        A refused modify is less dangerous than a refused cancel - the child
        order simply stays at its previous price rather than being left
        unwanted - but it does leave the child diverged from the master, so
        the CopyTrade is flagged for reconciliation rather than left looking
        healthy.

        The Order row is deliberately NOT written: the broker did not apply
        the change, so the stored price/quantity remain correct as-is.
        """
        logger.error(
            f"[Pipeline] Child order modification FAILED for {ord_rec.broker_order_id} "
            f"(account {acc_id_str}, master order {master_order_id}): {detail} "
            "Child order remains at its previous price/quantity."
        )

        try:
            async with AsyncSessionLocal() as session:
                await emit_system_event(
                    session,
                    event_type="CHILD_MODIFICATION_FAILED",
                    severity="CRITICAL",
                    message=(
                        f"Failed to modify child order {ord_rec.broker_order_id} "
                        f"(account {acc_id_str}) after master order {master_order_id} was "
                        f"modified. {detail} The child order is still working at its "
                        "previous price/quantity and is now diverged from the master; "
                        "manual verification required."
                    ),
                    account_id=ord_rec.account_id,
                )
        except Exception as exc:
            logger.error(
                f"[Pipeline] Failed to emit CHILD_MODIFICATION_FAILED SystemEvent for "
                f"{ord_rec.broker_order_id}: {exc}",
                exc_info=True,
            )

        try:
            async with AsyncSessionLocal() as session:
                ct_stmt = select(CopyTrade).where(
                    CopyTrade.broker_order_id == ord_rec.broker_order_id
                )
                trade = (await session.execute(ct_stmt)).scalar_one_or_none()
                if trade is not None:
                    trade.status = CopyTradeStatus.RECONCILIATION_REQUIRED
                    trade.failure_reason = (
                        f"Master order modified but child modify failed: {detail}"
                    )
                    await session.commit()
        except Exception as exc:
            logger.error(
                f"[Pipeline] Failed to mark CopyTrade RECONCILIATION_REQUIRED after failed "
                f"modify of {ord_rec.broker_order_id}: {exc}",
                exc_info=True,
            )

    async def _record_reprice_reconciliation(
        self,
        *,
        master_order_id: str,
        detail: str,
        children: Optional[List[Order]] = None,
    ) -> Dict[str, Any]:
        """
        Records a master LIMIT->MARKET conversion that could NOT be
        delivered to the child order(s), and returns the result dict.

        Never places, cancels or replaces anything - it only makes the
        undelivered conversion visible. Any child rows passed in get their
        CopyTrade marked RECONCILIATION_REQUIRED through the existing
        _record_failed_child_modification path; when there is no child at
        all, a SystemEvent alone carries it, since there is no CopyTrade to
        attach to.
        """
        logger.warning(
            f"[Pipeline] Master order {master_order_id} LIMIT->MARKET conversion NOT "
            f"delivered to child order(s): {detail} No order placed, cancelled or replaced."
        )

        for ord_rec in children or []:
            try:
                await self._record_failed_child_modification(
                    master_order_id=master_order_id,
                    ord_rec=ord_rec,
                    acc_id_str=str(ord_rec.account_id),
                    detail=detail,
                )
            except Exception as exc:
                logger.error(
                    f"[Pipeline] Failed to record reprice reconciliation for child "
                    f"{ord_rec.broker_order_id}: {exc}",
                    exc_info=True,
                )

        try:
            async with AsyncSessionLocal() as session:
                await emit_system_event(
                    session,
                    event_type="LIMIT_TO_MARKET_REPRICE_FAILED",
                    severity="WARNING",
                    message=(
                        f"Master order {master_order_id} was converted LIMIT->MARKET but the "
                        f"change could not be applied to the follower child order(s): {detail} "
                        "The child was left exactly as it was - nothing was cancelled or "
                        "replaced. Manual verification required."
                    ),
                    metadata_payload={
                        "master_order_id": master_order_id,
                        "detail": detail,
                        "child_count": len(children or []),
                    },
                )
        except Exception as exc:
            logger.error(
                f"[Pipeline] Failed to emit LIMIT_TO_MARKET_REPRICE_FAILED event: {exc}",
                exc_info=True,
            )

        return {"status": "reconciliation_required", "reason": detail}

    async def _record_sell_safety_rejection(
        self,
        *,
        follower: Account,
        event: MasterTradeEvent,
        target_qty: int,
        reference_price: Decimal,
        copy_event_uuid: uuid.UUID,
        reason_code: str,
        detail: str,
    ) -> Dict[str, Any]:
        """
        Records the fail-closed artifacts for a blocked follower SELL and
        returns the rejection result. Reuses the existing conventions
        exactly: AuditLog(event_type="RISK_REJECTED"), a WARNING
        SystemEvent, and a CopyTrade marked RECONCILIATION_REQUIRED so the
        blocked copy surfaces in the same Needs-Review bucket every other
        unresolved copy already uses.

        No CopyTrade exists yet at this point in either path (both reject
        before pre-writing one), so the marker row is created here. If the
        idempotency triple already has a row - a redelivered event for a
        copy that was already acted on - the existing row is left completely
        untouched rather than overwritten, since it may legitimately record
        a SUCCESS/SUBMITTED order that this rejection must not clobber. The
        AuditLog and SystemEvent are still written in that case, so the
        block is never invisible.
        """
        acc_id = follower.id
        logger.warning(
            f"[Pipeline] Follower SELL blocked for {follower.display_name} "
            f"({reason_code}): {detail} Order NOT placed."
        )

        try:
            async with AsyncSessionLocal() as session:
                session.add(
                    AuditLog(
                        event_type="RISK_REJECTED",
                        account_id=acc_id,
                        master_order_id=event.master_order_id,
                        copy_event_id=copy_event_uuid,
                        symbol=event.symbol,
                        side=event.side,
                        quantity=target_qty,
                        price=reference_price,
                        reason_code=reason_code,
                    )
                )
                await emit_system_event(
                    session,
                    event_type=reason_code,
                    severity="WARNING",
                    message=(
                        f"Follower SELL copy rejected for {follower.display_name} on "
                        f"{event.symbol}: {detail} Failing closed; no order placed and no "
                        "short position opened."
                    ),
                    account_id=acc_id,
                    metadata_payload={
                        "account_id": str(acc_id),
                        "master_order_id": event.master_order_id,
                        "symbol": event.symbol,
                        "exchange": event.exchange,
                        "product_type": event.product_type,
                        "requested_sell_quantity": target_qty,
                        "reason_code": reason_code,
                    },
                )
        except Exception as exc:
            logger.error(
                f"[Pipeline] Failed to persist SELL-safety audit for {acc_id}: {exc}",
                exc_info=True,
            )

        try:
            async with AsyncSessionLocal() as session:
                session.add(
                    CopyTrade(
                        id=uuid.uuid4(),
                        master_order_id=event.master_order_id,
                        follower_account_id=acc_id,
                        copy_event_id=copy_event_uuid,
                        symbol=event.symbol,
                        exchange=event.exchange,
                        side=OrderSide.SELL,
                        quantity=target_qty,
                        product_type=event.product_type,
                        estimated_price=reference_price,
                        status=CopyTradeStatus.RECONCILIATION_REQUIRED,
                        failure_reason=f"{reason_code}: {detail}",
                    )
                )
                try:
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    logger.info(
                        f"[Pipeline] CopyTrade already exists for master_order_id="
                        f"{event.master_order_id}, follower={acc_id}; leaving it untouched."
                    )
        except Exception as exc:
            logger.error(
                f"[Pipeline] Failed to record RECONCILIATION_REQUIRED CopyTrade for "
                f"{acc_id}: {exc}",
                exc_info=True,
            )

        return {"status": "REJECTED", "reasons": [reason_code]}

    # Risk failures that a verified position-reducing SELL may skip: it needs no
    # new margin and can only lower exposure. Nothing else is ever skipped.
    _EXIT_SKIPPABLE_RISK_FAILURES = frozenset({"INSUFFICIENT_MARGIN", "MAX_EXPOSURE_BREACHED"})

    async def _is_verified_exit(
        self,
        *,
        client: Optional[AliceBlueClient],
        side: Any,
        exchange: str,
        symbol: str,
        product_type: Any,
        instrument_token: Optional[str],
        quantity: int,
    ) -> bool:
        """
        True only when this is a SELL and the follower's broker-confirmed LONG
        position for that exact instrument AND product (get_available_long,
        the same lookup the SELL position-safety gate uses) covers the whole
        quantity. A BUY, no/short position, a long smaller than the quantity,
        or any failure to establish the position returns False, so the caller
        keeps its normal risk behaviour (fail closed). Deliberately ignores
        SELL_POSITION_SAFETY_BYPASS: the position is always checked here.
        """
        side_text = str(getattr(side, "value", side) or "").strip().upper()
        if side_text != "SELL" or quantity <= 0 or client is None:
            return False
        try:
            available_long = await get_available_long(
                client=client,
                exchange=exchange,
                symbol=symbol,
                product_type=product_type,
                provided_token=instrument_token,
            )
        except Exception as exc:  # InstrumentResolutionError, PositionSafetyError, broker errors
            logger.warning(
                f"[Pipeline] Exit check for {symbol} SELL qty={quantity}: position could not be "
                f"established ({type(exc).__name__}); normal margin/exposure checks apply."
            )
            return False
        return available_long >= quantity

    async def _evaluate_risk_exit_aware(
        self,
        *,
        client: Optional[AliceBlueClient],
        side: Any,
        exchange: str,
        product_type: Any,
        instrument_token: Optional[str],
        **risk_kwargs: Any,
    ) -> RiskCheckResult:
        """
        RiskEngine.evaluate, plus: when the ONLY failures are margin and/or max
        exposure and the order is a verified position-reducing SELL
        (_is_verified_exit), re-evaluates with reduces_position=True. The
        position is read only in that case, so ordinary copies make no extra
        broker call. All other failures (emergency stop, copy disabled, symbol,
        max trade value, daily loss) still reject.
        """
        result = RiskEngine.evaluate(**risk_kwargs)
        if result.passed or not result.failures:
            return result
        if not set(result.failures) <= self._EXIT_SKIPPABLE_RISK_FAILURES:
            return result
        verified = await self._is_verified_exit(
            client=client,
            side=side,
            exchange=exchange,
            symbol=risk_kwargs["symbol"],
            product_type=product_type,
            instrument_token=instrument_token,
            quantity=int(risk_kwargs["quantity"]),
        )
        if not verified:
            return result
        logger.info(
            f"[Pipeline] Verified exit SELL {risk_kwargs['symbol']} qty={risk_kwargs['quantity']}: "
            f"skipping {','.join(result.failures)} (follower long covers the quantity)."
        )
        return RiskEngine.evaluate(**risk_kwargs, reduces_position=True)

    async def _enforce_sell_position_safety(
        self,
        *,
        follower: Account,
        event: MasterTradeEvent,
        client: Optional[AliceBlueClient],
        target_qty: int,
        reference_price: Decimal,
        copy_event_uuid: uuid.UUID,
    ) -> Optional[Dict[str, Any]]:
        """
        Follower SELL position-safety gate. Returns None when the SELL may
        proceed; otherwise returns the rejection result the caller returns
        unchanged, having already recorded AuditLog/SystemEvent/CopyTrade.

        Rule (Option B): a follower SELL is only permitted when the
        follower's broker-confirmed LONG position for that exact instrument
        AND product fully covers the requested quantity. Anything short of
        that rejects the ENTIRE copy - never a partial sell, never a short.
        Automatic new short entries are not supported, so a flat or
        already-short follower is always rejected.

        Deliberately unconditional: it runs for every follower, including
        ones with no CopySetting. The existing RiskEngine block only runs
        under `if settings_obj:`, so putting this there would leave a
        follower without settings entirely unprotected - the opposite of
        what a safety gate is for.

        BUY replication is untouched. The BUY test is written as "explicitly
        BUY" rather than "not SELL" so it mirrors the downstream
        `BUY if event.side == "BUY" else SELL` placement logic exactly:
        anything that would be PLACED as a sell is also GATED as a sell,
        leaving no gap for a malformed side value to slip through unchecked.
        """
        if str(event.side or "").strip().upper() == "BUY":
            return None

        if settings.SELL_POSITION_SAFETY_BYPASS:
            logger.warning(
                f"[Pipeline] SELL_POSITION_SAFETY_BYPASS active: follower SELL position check "
                f"SKIPPED for {follower.display_name} on {event.symbol} qty={target_qty} "
                f"(master {event.master_order_id})."
            )
            return None

        try:
            available_long = await get_available_long(
                client=client,
                exchange=event.exchange,
                symbol=event.symbol,
                product_type=event.product_type,
                provided_token=event.instrument_id,
            )
        except InstrumentResolutionError as exc:
            return await self._record_sell_safety_rejection(
                follower=follower,
                event=event,
                target_qty=target_qty,
                reference_price=reference_price,
                copy_event_uuid=copy_event_uuid,
                reason_code="INSTRUMENT_RESOLUTION_FAILED",
                detail=f"Could not identify the instrument to check the position: {exc}",
            )
        except PositionSafetyError as exc:
            return await self._record_sell_safety_rejection(
                follower=follower,
                event=event,
                target_qty=target_qty,
                reference_price=reference_price,
                copy_event_uuid=copy_event_uuid,
                reason_code=exc.reason_code,
                detail=str(exc),
            )

        if target_qty > available_long:
            return await self._record_sell_safety_rejection(
                follower=follower,
                event=event,
                target_qty=target_qty,
                reference_price=reference_price,
                copy_event_uuid=copy_event_uuid,
                reason_code="INSUFFICIENT_LONG_POSITION",
                detail=(
                    f"Follower holds {available_long} long but the copy requires selling "
                    f"{target_qty}; selling would open or deepen a short position."
                ),
            )

        return None

    async def _execute_market_replication(
        self,
        event: MasterTradeEvent,
        clients: Dict[str, AliceBlueClient],
        global_emergency_stop: bool,
        stagger_ms: int,
    ) -> Dict[str, Any]:
        """
        MARKET Order Replication for accepted/filled market trades.
        """
        stop_active, stopped_followers = await self._resolve_stop_state(global_emergency_stop)

        validation = await self.validator.validate(
            event=event,
            is_emergency_stopped=stop_active,
            is_master_healthy=True,
        )
        if not validation.is_valid:
            logger.warning(f"[Pipeline] Master trade event {event.master_order_id} validation failed: {validation.reason_code}")
            return {"status": "rejected", "reason": validation.reason_code}

        followers, strategy_id = await self._get_eligible_followers_for_master(
            event.account_id, master_order_id=event.master_order_id
        )
        if not followers:
            logger.info("[Pipeline] No active follower accounts configured.")
            return {"status": "no_followers"}

        lot_size = self._get_lot_size(event.symbol)

        tasks = []
        for idx, (follower, multiplier) in enumerate(followers):
            if idx > 0 and stagger_ms > 0:
                await asyncio.sleep(stagger_ms / 1000.0)
            tasks.append(
                asyncio.create_task(
                    self._process_single_market_follower(
                        follower=follower,
                        multiplier=multiplier,
                        event=event,
                        client=clients.get(str(follower.id)),
                        lot_size=lot_size,
                        strategy_id=strategy_id,
                        # Finding P3 #12: thread the ALREADY-RESOLVED stop_active
                        # value (see matching comment in replicate_limit_order)
                        # instead of the raw global_emergency_stop parameter,
                        # which is always False from engine_loop.py in practice.
                        global_emergency_stop=stop_active,
                        follower_runtime_paused=str(follower.id) in stopped_followers,
                    )
                )
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)
        summary = {}
        for (follower, _), res in zip(followers, results):
            if isinstance(res, Exception):
                logger.error(f"[Pipeline] Exception processing follower {follower.display_name}: {res}")
                summary[str(follower.id)] = {"status": "EXCEPTION", "error": str(res)}
            else:
                summary[str(follower.id)] = res

        return {"status": "completed", "results": summary}

    async def _process_single_follower(
        self,
        follower: Account,
        event: MasterTradeEvent,
        client: Optional[AliceBlueClient],
        lot_size: int,
        global_emergency_stop: bool,
        follower_runtime_paused: bool = False,
        multiplier: Decimal = Decimal("1.0"),
        strategy_id: Optional[uuid.UUID] = None,
    ) -> Dict[str, Any]:
        """
        Alias for market follower processing used by reliability tests.
        """
        return await self._process_single_market_follower(
            follower=follower,
            multiplier=multiplier,
            event=event,
            client=client,
            lot_size=lot_size,
            strategy_id=strategy_id,
            global_emergency_stop=global_emergency_stop,
            follower_runtime_paused=follower_runtime_paused,
        )

    async def _process_single_market_follower(
        self,
        follower: Account,
        multiplier: Decimal,
        event: MasterTradeEvent,
        client: Optional[AliceBlueClient],
        lot_size: int,
        strategy_id: Optional[uuid.UUID],
        global_emergency_stop: bool,
        follower_runtime_paused: bool = False,
    ) -> Dict[str, Any]:
        """
        Executes market order replication for a single follower with complete risk checks.
        """
        acc_id = follower.id
        settings_obj = follower.copy_settings

        copy_event_uuid = derive_copy_event_id(
            event.master_order_id, str(acc_id), event.fill_sequence
        )
        client_order_id = (
            f"JT__{event.master_order_id[:24]}__{str(acc_id)[:8]}__{event.fill_sequence}"
        )

        # ── Marketable LIMIT execution strategy ──────────────────────────
        # Alice Blue rejects MARKET orders on this API/vendor channel with
        # EC965 "Market orders are not allowed" (confirmed broker/vendor-
        # channel restriction, not a payload bug - the same account places
        # MARKET orders successfully via Alice Blue's own Web/Mobile app).
        # Default mode converts the master's already-CONFIRMED fill price
        # (event.price - a real, just-executed trade, not a stale quote;
        # this function is only ever reached after MasterTradeDetector has
        # seen an actual COMPLETE/PARTIAL fill, never on mere acceptance)
        # into an aggressive LIMIT order per side, which this same API
        # accepts. No tick-size normalization is applied here, matching
        # replicate_limit_order's existing behavior - no such utility
        # exists in this codebase today, and LIMIT orders already work
        # correctly in production without one.
        use_marketable_limit = settings.MARKET_ORDER_EXECUTION_MODE == "MARKETABLE_LIMIT"

        if use_marketable_limit and (not event.price or event.price <= 0):
            async with AsyncSessionLocal() as session:
                session.add(
                    AuditLog(
                        event_type="RISK_REJECTED",
                        account_id=acc_id,
                        master_order_id=event.master_order_id,
                        copy_event_id=copy_event_uuid,
                        symbol=event.symbol,
                        side=event.side,
                        quantity=0,
                        price=Decimal("0"),
                        reason_code="REFERENCE_PRICE_UNAVAILABLE",
                    )
                )
                await emit_system_event(
                    session,
                    event_type="REFERENCE_PRICE_UNAVAILABLE",
                    severity="WARNING",
                    message=(
                        f"Marketable LIMIT copy rejected for {follower.display_name}: "
                        "the master fill event carried no usable reference price. "
                        "Failing closed; order NOT placed."
                    ),
                    account_id=acc_id,
                )
            return {"status": "REJECTED", "reasons": ["REFERENCE_PRICE_UNAVAILABLE"]}

        if use_marketable_limit:
            execution_price = compute_marketable_limit_price(event.side, event.price)
            execution_order_type = OrderType.LIMIT
            execution_order_type_str = "LIMIT"
        else:
            execution_price = Decimal("0")
            execution_order_type = OrderType.MARKET
            execution_order_type_str = "MARKET"
        # ── end marketable LIMIT setup ────────────────────────────────────

        # Funds Check
        funds_data: Optional[FundsData] = None
        if client:
            try:
                funds_data = await client.get_funds()
            except Exception as exc:
                logger.error(f"[Pipeline] Funds fetch failed for {follower.display_name}: {exc}")

        if funds_data is None:
            async with AsyncSessionLocal() as session:
                audit = AuditLog(
                    event_type="RISK_REJECTED",
                    account_id=acc_id,
                    master_order_id=event.master_order_id,
                    copy_event_id=copy_event_uuid,
                    symbol=event.symbol,
                    side=event.side,
                    quantity=0,
                    price=event.price,
                    reason_code="FUNDS_FETCH_FAILED",
                )
                session.add(audit)
                await emit_system_event(
                    session,
                    event_type="FUNDS_FETCH_FAILED",
                    severity="WARNING",
                    message=(
                        f"Copy rejected for {follower.display_name}: broker funds unavailable. "
                        "Failing closed; order NOT placed."
                    ),
                    account_id=acc_id,
                )
            return {"status": "REJECTED", "reasons": ["FUNDS_FETCH_FAILED"]}

        # Calculate Quantity
        if settings_obj:
            target_qty = QuantityCalculator.calculate_quantity(
                mode=settings_obj.quantity_mode,
                master_qty=event.quantity,
                master_capital=settings.MASTER_CAPITAL,
                follower_capital=funds_data.available_margin,
                lot_size=lot_size,
                settings=settings_obj,
                reference_price=event.price,
                multiplier=multiplier,
            )
        else:
            base_qty = int(event.quantity * float(multiplier))
            target_qty = (base_qty // lot_size) * lot_size if lot_size > 1 else max(1, base_qty)

        if target_qty <= 0:
            async with AsyncSessionLocal() as session:
                audit = AuditLog(
                    event_type="COPY_SKIPPED",
                    account_id=acc_id,
                    master_order_id=event.master_order_id,
                    copy_event_id=copy_event_uuid,
                    symbol=event.symbol,
                    side=event.side,
                    quantity=0,
                    price=event.price,
                    reason_code="ZERO_LOT_RESULT",
                )
                session.add(audit)
                await session.commit()
            return {"status": "SKIPPED", "reason": "ZERO_LOT_RESULT"}

        # Risk Evaluation
        if settings_obj:
            try:
                daily_loss = await get_daily_loss_today(acc_id)
                open_exposure = await get_open_exposure(client)
            except Exception as exc:
                logger.error(f"[Pipeline] Risk input fetch failed for {follower.display_name}: {exc}")
                async with AsyncSessionLocal() as session:
                    session.add(
                        AuditLog(
                            event_type="RISK_REJECTED",
                            account_id=acc_id,
                            master_order_id=event.master_order_id,
                            copy_event_id=copy_event_uuid,
                            symbol=event.symbol,
                            side=event.side,
                            quantity=target_qty,
                            price=event.price,
                            reason_code="RISK_INPUTS_UNAVAILABLE",
                        )
                    )
                    await session.commit()
                return {"status": "REJECTED", "reasons": ["RISK_INPUTS_UNAVAILABLE"]}

            risk_res = await self._evaluate_risk_exit_aware(
                client=client,
                side=event.side,
                exchange=event.exchange,
                product_type=event.product_type,
                instrument_token=event.instrument_id,
                account=follower,
                settings=settings_obj,
                symbol=event.symbol,
                quantity=target_qty,
                price=event.price,
                funds=funds_data,
                daily_loss_today=daily_loss,
                open_exposure=open_exposure,
                follower_runtime_paused=follower_runtime_paused,
                global_emergency_stop=global_emergency_stop,
            )

            if not risk_res.passed:
                reasons_str = ",".join(risk_res.failures)
                logger.warning(f"[Pipeline] Follower {follower.display_name} rejected by risk engine: {reasons_str}")
                async with AsyncSessionLocal() as session:
                    audit = AuditLog(
                        event_type="RISK_REJECTED",
                        account_id=acc_id,
                        master_order_id=event.master_order_id,
                        copy_event_id=copy_event_uuid,
                        symbol=event.symbol,
                        side=event.side,
                        quantity=target_qty,
                        price=event.price,
                        reason_code=reasons_str,
                    )
                    session.add(audit)
                    await session.commit()
                return {"status": "REJECTED", "reasons": risk_res.failures}

        # Follower SELL position-safety gate. Placed here because target_qty
        # is only known after the quantity calculation above, and OUTSIDE the
        # `if settings_obj:` risk block so it runs for every follower. The
        # marketable-LIMIT price computed earlier is pure arithmetic with no
        # broker call, so it is simply discarded on rejection - no broker
        # place_order is reachable past this point without passing the gate.
        sell_block = await self._enforce_sell_position_safety(
            follower=follower,
            event=event,
            client=client,
            target_qty=target_qty,
            reference_price=execution_price if use_marketable_limit else event.price,
            copy_event_uuid=copy_event_uuid,
        )
        if sell_block is not None:
            return sell_block

        # Pre-write CopyTrade and Order in DB
        copy_trade_id = uuid.uuid4()
        internal_child_ord_id = f"JT_CH_{event.master_order_id[:16]}_{str(acc_id)[:8]}_{event.fill_sequence}"

        async with AsyncSessionLocal() as session:
            trade_record = CopyTrade(
                id=copy_trade_id,
                master_order_id=event.master_order_id,
                follower_account_id=acc_id,
                copy_event_id=copy_event_uuid,
                symbol=event.symbol,
                exchange=event.exchange,
                side=OrderSide.BUY if event.side == "BUY" else OrderSide.SELL,
                quantity=target_qty,
                product_type=event.product_type,
                estimated_price=execution_price if use_marketable_limit else event.price,
                client_order_id=client_order_id,
                status=CopyTradeStatus.PENDING,
            )
            session.add(trade_record)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                logger.info(f"[Pipeline] Duplicate copy trade detected for master_order_id={event.master_order_id}, follower={acc_id}. Skipping silently.")
                return {"status": "SKIPPED", "reason": "DUPLICATE_EVENT"}

        await self.save_or_update_order(
            internal_order_id=internal_child_ord_id,
            broker_order_id=None,
            account_id=acc_id,
            group_strategy_id=strategy_id,
            source=OrderSource.SYSTEM,
            exchange=event.exchange,
            symbol=event.symbol,
            side=OrderSide.BUY if event.side == "BUY" else OrderSide.SELL,
            order_type=execution_order_type_str,
            product_type=event.product_type,
            quantity=target_qty,
            filled_quantity=0,
            pending_quantity=target_qty,
            price=execution_price,
            status=OrderStatus.PENDING,
            master_broker_order_id=event.master_order_id,
        )

        if not client:
            return {"status": "REJECTED", "reason": "NO_CLIENT"}

        broker_side = BrokerOrderSide.BUY if event.side == "BUY" else BrokerOrderSide.SELL
        prod_upper = str(event.product_type).upper()
        broker_prod = ProductType.CNC if prod_upper in ("CNC", "LONGTERM") else (ProductType.NRML if prod_upper in ("NRML", "MTF") else ProductType.MIS)

        order_req = OrderRequest(
            symbol=event.symbol,
            exchange=event.exchange,
            side=broker_side,
            quantity=target_qty,
            product_type=broker_prod,
            order_type=execution_order_type,
            price=execution_price,
            client_order_id=client_order_id,
            # See replicate_limit_order's identical comment: the master's
            # own broker-provided instrument id beats symbol-based NFO guessing.
            instrument_token=event.instrument_id,
        )

        await self._stamp_submitted(client_order_id)
        order_res = await self.retry_policy.execute(client.place_order, order_req)

        # Update CopyTrade & Order Status.
        #
        # OrderResultCategory.SUCCESS only means the broker ACCEPTED the
        # placement request - AliceBlueClient.OrderResult carries no fill
        # quantity/price at all, so there is no way to know from this
        # response alone whether the market order has actually been filled
        # yet (illiquid symbols, partial fills, or just normal exchange
        # latency can all leave it genuinely still open for a moment).
        # Marking it OrderStatus.COMPLETE here regardless was a real bug:
        # a master-order cancellation arriving for this same broker order
        # shortly after would find nothing to cancel (handle_master_
        # cancellation only looks at OPEN/PARTIAL/PENDING children), even
        # though the child might still be genuinely open at the broker.
        # Treat SUCCESS as "submitted, not yet confirmed" instead - CopyTrade
        # SUBMITTED / Order OPEN, exactly like a LIMIT order's initial state -
        # and let FollowerFillSyncer's existing broker-order-book polling
        # (already running every 60s, already the only thing that confirms a
        # LIMIT child's true fill state) resolve the real terminal status.
        unresolved = is_ambiguous_send(order_res)
        if order_res.category == OrderResultCategory.SUCCESS:
            ct_status, ord_status = CopyTradeStatus.SUBMITTED, OrderStatus.OPEN
        elif unresolved:
            # Ambiguous: the broker may hold this order. CopyTrade TIMEOUT (what the sweeper reconciles) and the
            # child Order stays PENDING with broker_status TIMEOUT and no broker id - never FAILED/REJECTED.
            ct_status, ord_status = CopyTradeStatus.TIMEOUT, OrderStatus.PENDING
        else:
            status_map = {
                OrderResultCategory.REJECTED: (CopyTradeStatus.REJECTED, OrderStatus.REJECTED),
                OrderResultCategory.NETWORK_ERROR: (CopyTradeStatus.FAILED, OrderStatus.FAILED),
            }
            ct_status, ord_status = status_map.get(order_res.category, (CopyTradeStatus.FAILED, OrderStatus.FAILED))

        try:
            async with AsyncSessionLocal() as session:
                stmt = select(CopyTrade).where(CopyTrade.id == copy_trade_id)
                saved_trade = (await session.execute(stmt)).scalar_one()
                saved_trade.status = ct_status
                saved_trade.broker_order_id = order_res.broker_order_id
                saved_trade.broker_response = order_res.raw_response
                if unresolved:
                    saved_trade.failure_reason = unresolved_reason(order_res)
                    if order_res.category != OrderResultCategory.TIMEOUT:
                        saved_trade.broker_response = None
                elif order_res.category != OrderResultCategory.SUCCESS:
                    saved_trade.failure_reason = order_res.message
                if not unresolved:
                    saved_trade.completed_at = datetime.now(timezone.utc)   # an unresolved send is not complete

                audit = AuditLog(
                    # Keyed off order_res.category (the broker's actual
                    # outcome), not ct_status/ord_status - SUBMITTED/OPEN is
                    # a genuine success (accepted, fill not yet confirmed),
                    # not a failure, and must not be logged as COPY_FAILED.
                    event_type="COPY_EXECUTED" if order_res.category == OrderResultCategory.SUCCESS else "COPY_FAILED",
                    account_id=acc_id,
                    master_order_id=event.master_order_id,
                    copy_event_id=copy_event_uuid,
                    symbol=event.symbol,
                    side=event.side,
                    quantity=target_qty,
                    price=event.price,
                    reason_code=(
                        unresolved_reason(order_res) if unresolved
                        else order_res.message if order_res.category != OrderResultCategory.SUCCESS else None
                    ),
                )
                session.add(audit)
                await session.commit()

            await self.save_or_update_order(
                internal_order_id=internal_child_ord_id,
                broker_order_id=order_res.broker_order_id,
                account_id=acc_id,
                group_strategy_id=strategy_id,
                source=OrderSource.SYSTEM,
                exchange=event.exchange,
                symbol=event.symbol,
                side=OrderSide.BUY if event.side == "BUY" else OrderSide.SELL,
                order_type=execution_order_type_str,
                product_type=event.product_type,
                quantity=target_qty,
                filled_quantity=0,
                pending_quantity=target_qty if ord_status in (OrderStatus.OPEN, OrderStatus.PENDING) else 0,
                price=execution_price,
                average_price=Decimal("0"),
                status=ord_status,
                broker_status="TIMEOUT" if unresolved else order_res.category.value,
                rejection_reason=(
                    None if unresolved
                    else order_res.message if order_res.category != OrderResultCategory.SUCCESS else None
                ),
                master_broker_order_id=event.master_order_id,
            )
        except Exception as db_exc:
            if order_res.category != OrderResultCategory.SUCCESS:
                # Broker itself did not succeed - preserve existing behavior
                # (propagate, caught by _execute_market_replication's gather).
                raise
            # Broker SUCCEEDED but persisting that result failed. The broker
            # already holds a real, live order for this follower - this must
            # never be reported back as if the order itself failed, and no
            # second order may ever be placed to "retry" it.
            logger.critical(
                f"[Pipeline] Broker MARKET order SUCCEEDED for {follower.display_name} "
                f"(broker_order_id={order_res.broker_order_id}) but DB persistence failed: {db_exc}",
                exc_info=True,
            )
            try:
                async with AsyncSessionLocal() as session:
                    await emit_system_event(
                        session,
                        event_type="MARKET_ORDER_PERSISTENCE_FAILED",
                        severity="CRITICAL",
                        message=(
                            f"Broker MARKET order for {follower.display_name} (broker_order_id="
                            f"{order_res.broker_order_id}) succeeded but could not be persisted to the "
                            "Order/CopyTrade tables. Manual verification required."
                        ),
                        account_id=acc_id,
                        metadata_payload={
                            "account_id": str(acc_id),
                            "symbol": event.symbol,
                            "master_order_id": event.master_order_id,
                            "broker_order_id": order_res.broker_order_id,
                            "internal_order_id": internal_child_ord_id,
                            "operation": "MARKET_ORDER_CREATE",
                            "db_error": str(db_exc),
                        },
                    )
            except Exception as event_exc:
                logger.critical(
                    f"[Pipeline] Failed to emit MARKET_ORDER_PERSISTENCE_FAILED SystemEvent: {event_exc}",
                    exc_info=True,
                )
            return {
                "status": OrderResultCategory.SUCCESS.value,
                "broker_order_id": order_res.broker_order_id,
                "quantity": target_qty,
                "warning": "DB_PERSISTENCE_FAILED",
            }

        return {
            "status": "TIMEOUT" if unresolved else order_res.category.value,
            "broker_order_id": order_res.broker_order_id,
            "quantity": target_qty,
        }

    async def _get_eligible_followers_for_master(
        self,
        master_account_id: str,
        *,
        existing_children_lookup: bool = False,
        master_order_id: Optional[str] = None,
    ) -> tuple[List[tuple[Account, Decimal]], Optional[uuid.UUID]]:
        """
        Resolves which Child accounts a Master's order concerns.

        New copies (default; LIMIT/SL/MARKET creation): only the Children of
        the one non-deleted strategy whose configured Master is
        `master_account_id`, and only while that strategy's trading is
        enabled. There is NO global fallback: an order from an account that
        is not a strategy's Master is copied to nobody, audited as
        NO_MATCHING_STRATEGY; a disabled strategy is audited as
        STRATEGY_TRADING_DISABLED. The old fallback copied such orders to
        every active FOLLOWER at 1.0x, which after a Master change could
        silently reverse copy direction.

        existing_children_lookup=True (modification, pending-SL modification,
        SL trigger execution of orders that ALREADY have child orders): these
        callers never create a child - they act only on child Order rows
        found by master_broker_order_id - and use this list solely to look up
        each existing child's Account/CopySetting (which drives the RiskEngine
        and global-stop gates) and multiplier. They keep the exact original
        resolution, fallback included, so the quantity and risk behavior of
        managing an already-copied order is unchanged.

        Member-level eligibility (member trading_enabled, active, not
        emergency-stopped, not deleted) is unchanged. A member that is the
        strategy's own Master is never returned for a new copy, as a defense
        against self-copying.
        """
        if existing_children_lookup:
            return await self._lookup_existing_children_context(master_account_id)

        async with AsyncSessionLocal() as session:
            try:
                master_uuid = uuid.UUID(str(master_account_id))
            except Exception:
                master_uuid = None

            strategies: List[GroupStrategy] = []
            if master_uuid:
                strat_stmt = select(GroupStrategy).where(
                    GroupStrategy.master_account_id == master_uuid,
                    GroupStrategy.deleted_at.is_(None),
                ).options(
                    selectinload(GroupStrategy.members).selectinload(StrategyMember.account).selectinload(Account.copy_settings)
                )
                strategies = list((await session.execute(strat_stmt)).scalars().all())

            if len(strategies) != 1:
                # Zero matches, or (prevented by the strategy_account_roles
                # constraints, but never guessed at) more than one.
                await self._record_routing_skip(
                    session,
                    master_account_id=master_uuid,
                    master_order_id=master_order_id,
                    reason_code="NO_MATCHING_STRATEGY" if not strategies else "AMBIGUOUS_STRATEGY_MATCH",
                    message=(
                        f"Order {master_order_id or '?'} from account {master_account_id} was not copied: "
                        + (
                            "no strategy has this account as its Master."
                            if not strategies
                            else f"{len(strategies)} strategies list this account as Master."
                        )
                    ),
                )
                return [], None

            strategy = strategies[0]
            if not strategy.trading_enabled:
                await self._record_routing_skip(
                    session,
                    master_account_id=master_uuid,
                    master_order_id=master_order_id,
                    reason_code="STRATEGY_TRADING_DISABLED",
                    message=(
                        f"Order {master_order_id or '?'} from the Master of strategy '{strategy.name}' was not "
                        "copied: trading is disabled for this strategy."
                    ),
                    strategy_id=strategy.id,
                )
                return [], strategy.id

            eligible: List[tuple[Account, Decimal]] = []
            for m in strategy.members:
                if m.account_id == strategy.master_account_id:
                    continue
                if m.trading_enabled and m.account and m.account.is_active and not m.account.is_emergency_stopped and m.account.deleted_at is None:
                    if not can_be_child(m.account.broker):
                        # Broker not cleared to receive copies (unsupported, flag
                        # off, or its response contract unverified): never copy.
                        await self._record_routing_skip(
                            session,
                            master_account_id=master_uuid,
                            master_order_id=master_order_id,
                            reason_code="CHILD_BROKER_NOT_READY",
                            message=(
                                f"Order {master_order_id or '?'} was not copied to {m.account.display_name}: "
                                f"broker '{m.account.broker}' is not cleared to receive copied orders."
                            ),
                            strategy_id=strategy.id,
                        )
                        continue
                    eligible.append((m.account, m.multiplier))
            return eligible, strategy.id

    async def _lookup_existing_children_context(
        self, master_account_id: str
    ) -> tuple[List[tuple[Account, Decimal]], Optional[uuid.UUID]]:
        """
        The original (pre-strategy-routing) resolution, unchanged, used only
        to look up Account/CopySetting/multiplier for children that ALREADY
        exist. Falls back to all active followers with 1.0x multiplier if no
        trading-enabled group strategy is bound - harmless here because no
        caller of this path ever creates a child order.
        """
        async with AsyncSessionLocal() as session:
            try:
                master_uuid = uuid.UUID(str(master_account_id))
            except Exception:
                master_uuid = None

            if master_uuid:
                # 1. Query Group Strategy where this account is master
                strat_stmt = select(GroupStrategy).where(
                    GroupStrategy.master_account_id == master_uuid,
                    GroupStrategy.deleted_at.is_(None),
                    GroupStrategy.trading_enabled.is_(True),
                ).options(
                    selectinload(GroupStrategy.members).selectinload(StrategyMember.account).selectinload(Account.copy_settings)
                )
                strategy = (await session.execute(strat_stmt)).scalars().first()

                if strategy and strategy.members:
                    eligible: List[tuple[Account, Decimal]] = []
                    for m in strategy.members:
                        if m.trading_enabled and m.account and m.account.is_active and not m.account.is_emergency_stopped and m.account.deleted_at is None:
                            eligible.append((m.account, m.multiplier))
                    return eligible, strategy.id

            # 2. Fallback: all active follower accounts
            all_stmt = select(Account).where(
                Account.account_type == AccountType.FOLLOWER,
                Account.deleted_at.is_(None),
                Account.is_active.is_(True),
            ).options(selectinload(Account.copy_settings))
            followers = (await session.execute(all_stmt)).scalars().all()
            return [(f, Decimal("1.0")) for f in followers], None

    @staticmethod
    async def _record_routing_skip(
        session,
        *,
        master_account_id: Optional[uuid.UUID],
        master_order_id: Optional[str],
        reason_code: str,
        message: str,
        strategy_id: Optional[uuid.UUID] = None,
    ) -> None:
        """Audit trail for an order that strategy routing decided not to copy.
        Never raises - a failed audit write must not turn into a copy."""
        try:
            session.add(
                AuditLog(
                    event_type="COPY_NOT_ROUTED",
                    account_id=master_account_id,
                    master_order_id=master_order_id,
                    reason_code=reason_code,
                )
            )
            await emit_system_event(
                session,
                event_type=reason_code,
                severity="INFO",
                message=message,
                account_id=master_account_id,
                metadata_payload={
                    "master_order_id": master_order_id,
                    "master_account_id": str(master_account_id) if master_account_id else None,
                    "strategy_id": str(strategy_id) if strategy_id else None,
                    "reason_code": reason_code,
                },
            )
        except Exception as exc:
            logger.error(f"[Pipeline] Failed to audit routing skip {reason_code}: {exc}", exc_info=True)
        logger.info(f"[Pipeline] {message}")
