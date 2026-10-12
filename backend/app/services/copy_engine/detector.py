import logging
from typing import Any, Dict, Iterable, Optional, Set
from datetime import datetime
from decimal import Decimal

from app.services.alice_blue.types import OrderUpdateEvent
from app.services.copy_engine.events import MasterTradeEvent

logger = logging.getLogger("master_trade_detector")


class MasterTradeDetector:
    """
    Monitors normalized WebSocket order updates for the master account.
    Handles:
    - Immediate LIMIT order replication (status=OPEN/PENDING)
    - Incremental fills for MARKET & LIMIT orders (status=COMPLETE/PARTIAL)
    - Order cancellations (status=CANCELLED)
    - Order rejections (status=REJECTED)
    """

    def __init__(self, master_account_id: str):
        self.master_account_id = str(master_account_id)
        # Tracks previously seen filled shares per broker order ID: {order_id: (cumulative_filled_qty, fill_seq)}
        self._seen_fills: Dict[str, tuple[int, int]] = {}
        # Tracks the last known (quantity, price) for each broker order ID that
        # has already triggered immediate LIMIT replication. Membership means
        # "already seen"; the stored value is what lets a later OPEN update for
        # the same broker_order_id be told apart from a genuine duplicate
        # resend (quantity/price unchanged) versus a real master-side
        # quantity/price modification (quantity and/or price differ).
        self._limit_order_state: Dict[str, tuple[int, Decimal]] = {}
        # Every broker_order_id that already has (or had) a standing CHILD
        # order at the follower's broker - i.e. it went through
        # LIMIT_CREATED (directly, via instant-fill, or is still pending
        # after a later order_type change) - independent of its CURRENT
        # order_type. A pure MARKET order is deliberately NEVER added here:
        # it has no standing child, so each of its fills must keep
        # replicating independently (MARKET_ACCEPTED), exactly as before.
        #
        # This is the fix for the production bug where an order's identity
        # was determined by `order_type == "LIMIT"` at the moment of the
        # check: a master order modified from LIMIT to MARKET stopped being
        # "LIMIT" and was therefore treated as unknown - misclassified as a
        # brand-new order (event_type=MARKET_ACCEPTED at its next fill),
        # which the copy engine replicated as a SECOND, duplicate child,
        # while the original child was never touched. Membership here must
        # never depend on the order's CURRENT order_type - only on whether
        # it ever had a standing child order in the first place.
        self._known_order_ids: Set[str] = set()
        # Last order_type an ORDER_MODIFIED was actually dispatched for, per
        # broker_order_id - the idempotency guard for the order-type-change
        # branch below (mirrors _limit_order_state's own (quantity, price)
        # comparison for the existing price/quantity modification path).
        # Deliberately NOT seeded from Order rows on restart, for the same
        # reason _limit_order_state's quantity/price isn't fully
        # reconstructed either (see seed_state's docstring): an
        # already-applied order_type change may be harmlessly redetected
        # once after a restart, which is the existing, accepted tradeoff
        # for this whole modification-tracking mechanism, not a new one.
        self._last_order_type: Dict[str, str] = {}
        # Tracks the last known (quantity, price, trigger_price) for each
        # native SL/SL-M broker_order_id already replicated to followers
        # while TRIGGER_PENDING. Mirrors _limit_order_state's role: lets a
        # later PENDING observation be told apart as "unchanged" (ignore)
        # vs. "master modified it" (SL_PENDING_MODIFIED).
        self._sl_pending_state: Dict[str, tuple] = {}

    def seed_state(
        self,
        orders: Iterable[Any],
        known_child_master_order_ids: Optional[Iterable[str]] = None,
    ) -> None:
        """
        Issue #5: seeds _seen_fills and _limit_order_state from durable
        Order rows at EngineLoop startup, so a process restart doesn't
        silently reset them to empty.

        Without this, after a restart the first fill event for an order
        that already had prior fills would compute its ENTIRE historical
        cumulative fill as "new" (since _seen_fills defaults to (0, 0) for
        anything the fresh detector hasn't seen), and a still-open LIMIT
        order would be misclassified as brand-new (LIMIT_CREATED) instead
        of recognizing it's already been replicated.

        Caller contract: `orders` must already be filtered to the master
        account's own, non-terminal (OPEN/PARTIAL) orders - this method
        does not re-filter by account or status itself, only extracts the
        fields it needs from each item (broker_order_id, filled_quantity,
        order_type, quantity, price). Kept deliberately untyped/duck-typed
        (not importing app.models.trades.Order) so this module stays free
        of any DB/ORM dependency, matching its existing design.

        What this does and does not fully solve (see the Issue #5
        investigation for the full reasoning):
        - filled_quantity is reliably current (CopyEnginePipeline.
          save_or_update_order keeps it up to date on every update), so
          seeding it here correctly fixes the "replicates the full
          historical fill as new" bug.
        - quantity/price reflect the order's state as of when its Order
          row was first created, not necessarily its current state if it
          was modified before this restart (save_or_update_order does not
          update quantity/price on an existing row - a separate,
          pre-existing gap, not fixed here). This is still safe: if the
          seeded value differs from what the broker reports next, that is
          correctly re-detected as ORDER_MODIFIED rather than silently
          missed - it just means an already-applied modification may be
          harmlessly reapplied once after a restart.
        - fill_sequence itself is NOT reconstructed - no durable source
          exists for it anywhere (not on Order, not reversibly on
          CopyTrade) - and is always seeded at 0. Only the cumulative
          filled_quantity component is recovered.
        - An order that both filled and had no further change during the
          downtime window is only discovered at all if the caller also
          performs a fresh REST backfill after seeding (this method only
          prepares state to compare against; it does not itself observe
          anything new).

        known_child_master_order_ids: an optional set of master
        broker_order_id values that already have at least one durable
        child (SYSTEM-sourced) Order row in the DB, computed by the
        caller. This is the restart-safe fix for the case where a master
        order was replicated while it was still LIMIT, then modified to
        MARKET, and the process restarts before it fills: the master's own
        Order row now has order_type=MARKET, so the `order_type == "LIMIT"`
        branch below can no longer recognize it as already-known - but the
        existence of its child order is a durable, order_type-independent
        fact that survives the restart. Membership here is a pure OR with
        the order_type=="LIMIT" condition below; a pure MARKET order that
        never had a child is simply absent from this set, so it is
        unaffected. Kept as a caller-supplied set (rather than querying the
        DB here) to preserve this module's existing DB/ORM-free design.
        """
        known_child_master_order_ids = set(known_child_master_order_ids or ())
        for order in orders:
            broker_order_id = getattr(order, "broker_order_id", None)
            if not broker_order_id:
                continue

            filled_quantity = getattr(order, "filled_quantity", 0) or 0
            if filled_quantity > 0:
                self._seen_fills[broker_order_id] = (filled_quantity, 0)

            order_type = str(getattr(order, "order_type", "") or "").upper()
            if order_type == "LIMIT":
                self._limit_order_state[broker_order_id] = (order.quantity, order.price)
            elif order_type in ("SL", "SLM"):
                # A native SL/SL-M master row can only be PENDING via
                # TRIGGER_PENDING (see engine_loop.py's seed query comment),
                # so seeding this unconditionally on order_type is safe -
                # it only ever recovers genuine pending-SL comparison state.
                self._sl_pending_state[broker_order_id] = (
                    order.quantity, order.price, getattr(order, "trigger_price", Decimal("0")) or Decimal("0"),
                )

            # _known_order_ids means "this order already has (or had) a
            # standing child order at the broker that fills independently"
            # - true for orders that went through LIMIT_CREATED, regardless
            # of the master's CURRENT order_type. A pure MARKET order has
            # no such standing child: every incremental fill IS its own
            # replication event by design (see execute_replication's
            # MARKET_ACCEPTED path), and that must stay true across a
            # restart exactly as it does within one continuous run - so
            # membership here must never be granted just because an order
            # exists; it requires either being currently LIMIT (mirrors
            # _limit_order_state's own condition) OR having a durable child
            # order already on record (the restart-safe LIMIT->MARKET case
            # above).
            if order_type == "LIMIT" or broker_order_id in known_child_master_order_ids:
                self._known_order_ids.add(broker_order_id)

    def mark_sl_child_created(self, broker_order_id: str) -> None:
        """
        Called by the engine loop ONLY after a real follower SL/SL-M child
        has actually been created for this master order (see
        engine_loop.py's persist_and_process_event, right after a
        successful execute_replication of an SL_PENDING_CREATED event) -
        deliberately never eagerly at detection time.

        Why not eager: if registration happened as soon as the pending SL
        was first seen, and EVERY follower placement then failed (no
        funds, risk rejected, broker rejected), the master's eventual
        trigger would be misclassified as "already known" even though no
        child was ever actually created - silently losing the copy
        entirely instead of replicating it fresh at trigger time.

        Once called, this order's identity is the same as any other
        already-replicated order (see _known_order_ids' own docstring):
        the master's eventual trigger/fill is recognized as ORDER_FILLED
        against the EXISTING child, never a second LIMIT_CREATED/
        MARKET_ACCEPTED/SL_PENDING_CREATED for a duplicate one.
        """
        if broker_order_id:
            self._known_order_ids.add(broker_order_id)

    def process_order_update(self, event: OrderUpdateEvent) -> Optional[MasterTradeEvent]:
        # Ensure event is strictly from the master account
        if event.account_id != self.master_account_id:
            return None

        status = event.status
        order_type = event.order_type.upper() if event.order_type else "MARKET"
        broker_order_id = event.broker_order_id

        # 0. NATIVE SL / SL-M PENDING DETECTION / MODIFICATION DETECTION.
        # A native Alice Blue SL/SL-M order awaiting trigger is copied
        # IMMEDIATELY, mirroring the master's own trigger/limit price
        # exactly - the follower gets its own independent SL/SL-M order
        # while the master stays TRIGGER_PENDING, rather than Jettone
        # waiting for the master to trigger first (see
        # execute_replication's SL_PENDING_CREATED handling). "status"
        # here is master_order_poller.py's mapped OrderStatus.PENDING.value
        # ("PENDING"), never the raw broker string "TRIGGER_PENDING" -
        # that raw string is what TRIGGERS this branch to even be reached
        # (see _is_replication_eligible), but by the time it's normalized
        # into an OrderUpdateEvent it's already "PENDING".
        if status == "PENDING" and order_type in ("SL", "SLM"):
            current_state = (event.quantity, event.price, event.trigger_price)

            if broker_order_id and broker_order_id in self._sl_pending_state:
                if current_state == self._sl_pending_state[broker_order_id]:
                    logger.debug(f"[Detector] SL order {broker_order_id} unchanged while TRIGGER_PENDING; ignoring duplicate.")
                    return None

                self._sl_pending_state[broker_order_id] = current_state
                trade_event = MasterTradeEvent(
                    master_order_id=broker_order_id,
                    symbol=event.symbol,
                    exchange=event.exchange or "NSE",
                    side=event.side,
                    quantity=event.quantity,
                    price=event.price,
                    limit_price=event.price,
                    trigger_price=event.trigger_price,
                    product_type=event.product_type,
                    timestamp=event.timestamp,
                    fill_sequence=1,
                    account_id=self.master_account_id,
                    order_type=order_type,
                    event_type="SL_PENDING_MODIFIED",
                    source="BROKER_EXTERNAL",
                    total_quantity=event.quantity,
                    filled_quantity=event.filled_quantity,
                    pending_quantity=max(0, event.quantity - event.filled_quantity),
                    status="PENDING",
                    instrument_id=event.instrument_id,
                )
                logger.info(
                    f"[Detector] Detected Master SL Modification: {trade_event.symbol} {trade_event.side} "
                    f"qty={trade_event.quantity} price={trade_event.price} trigger={trade_event.trigger_price} "
                    f"(Order ID: {broker_order_id})"
                )
                return trade_event

            if broker_order_id:
                self._sl_pending_state[broker_order_id] = current_state

            trade_event = MasterTradeEvent(
                master_order_id=broker_order_id,
                symbol=event.symbol,
                exchange=event.exchange or "NSE",
                side=event.side,
                quantity=event.quantity,
                price=event.price,
                limit_price=event.price,
                trigger_price=event.trigger_price,
                product_type=event.product_type,
                timestamp=event.timestamp,
                fill_sequence=1,
                account_id=self.master_account_id,
                order_type=order_type,
                event_type="SL_PENDING_CREATED",
                source="BROKER_EXTERNAL",
                total_quantity=event.quantity,
                filled_quantity=event.filled_quantity,
                pending_quantity=event.quantity - event.filled_quantity,
                status="PENDING",
                instrument_id=event.instrument_id,
            )
            logger.info(
                f"[Detector] Detected Master SL Pending: {trade_event.symbol} {trade_event.side} "
                f"qty={trade_event.quantity} price={trade_event.price} trigger={trade_event.trigger_price} "
                f"order_type={order_type} (Order ID: {broker_order_id})"
            )
            return trade_event

        # 1. IMMEDIATE LIMIT ORDER DETECTION / MODIFICATION DETECTION (OPEN / PENDING)
        if status == "OPEN" and order_type == "LIMIT":
            limit_price = event.price if event.price > 0 else event.fill_price
            current_state = (event.quantity, limit_price)

            if broker_order_id and broker_order_id in self._limit_order_state:
                prev_quantity, prev_price = self._limit_order_state[broker_order_id]
                if current_state == (prev_quantity, prev_price):
                    logger.debug(f"[Detector] LIMIT order {broker_order_id} already triggered replication; ignoring duplicate OPEN.")
                    return None

                # quantity and/or price differ from what we last recorded for
                # this broker_order_id: the master modified the order.
                self._limit_order_state[broker_order_id] = current_state
                self._known_order_ids.add(broker_order_id)
                self._last_order_type[broker_order_id] = "LIMIT"

                trade_event = MasterTradeEvent(
                    master_order_id=broker_order_id,
                    symbol=event.symbol,
                    exchange=event.exchange or "NSE",
                    side=event.side,
                    quantity=event.quantity,
                    price=limit_price,
                    limit_price=limit_price,
                    trigger_price=event.trigger_price,
                    product_type=event.product_type,
                    timestamp=event.timestamp,
                    fill_sequence=1,
                    account_id=self.master_account_id,
                    order_type="LIMIT",
                    event_type="ORDER_MODIFIED",
                    source="BROKER_EXTERNAL",
                    total_quantity=event.quantity,
                    filled_quantity=event.filled_quantity,
                    pending_quantity=max(0, event.quantity - event.filled_quantity),
                    status="OPEN",
                    instrument_id=event.instrument_id,
                )
                logger.info(
                    f"[Detector] Detected Master LIMIT Modification: {trade_event.symbol} {trade_event.side} "
                    f"qty {prev_quantity}->{trade_event.quantity} price {prev_price}->{trade_event.limit_price} "
                    f"(Order ID: {trade_event.master_order_id})"
                )
                return trade_event

            if broker_order_id:
                self._limit_order_state[broker_order_id] = current_state
                self._known_order_ids.add(broker_order_id)
                self._last_order_type[broker_order_id] = "LIMIT"

            trade_event = MasterTradeEvent(
                master_order_id=broker_order_id,
                symbol=event.symbol,
                exchange=event.exchange or "NSE",
                side=event.side,
                quantity=event.quantity,
                price=limit_price,
                limit_price=limit_price,
                trigger_price=event.trigger_price,
                product_type=event.product_type,
                timestamp=event.timestamp,
                fill_sequence=1,
                account_id=self.master_account_id,
                order_type="LIMIT",
                event_type="LIMIT_CREATED",
                source="BROKER_EXTERNAL",
                total_quantity=event.quantity,
                filled_quantity=event.filled_quantity,
                pending_quantity=event.quantity - event.filled_quantity,
                status="OPEN",
                instrument_id=event.instrument_id,
            )
            logger.info(
                f"[Detector] Detected Master LIMIT Created: {trade_event.symbol} {trade_event.side} "
                f"qty={trade_event.quantity} @ ₹{trade_event.limit_price} (Order ID: {trade_event.master_order_id})"
            )
            return trade_event

        # 1b. ORDER-TYPE CHANGE DETECTION (still OPEN, no longer LIMIT).
        # A master order already known to this detector (previously LIMIT,
        # already replicated) whose order_type has changed - e.g. LIMIT ->
        # MARKET via the Alice Blue app - while still pending. Branch 1
        # above requires order_type == "LIMIT" and can never see this state.
        #
        # Before this branch existed, such an order produced NO event here
        # (not CANCELLED/REJECTED/COMPLETE/PARTIAL either - silently
        # dropped), and if it later filled, the fill-detection branch below
        # used to ALSO require order_type == "LIMIT" for the same identity
        # check, so the fill was misclassified as a brand-new
        # MARKET_ACCEPTED order - the confirmed production root cause of a
        # duplicate child being created while the original LIMIT child was
        # never touched. This branch, combined with _known_order_ids
        # replacing that order_type-gated identity check, closes both gaps:
        # the change itself is now observable as ORDER_MODIFIED, and the
        # eventual fill is correctly attributed to the SAME already-known
        # order rather than treated as a new one.
        if status == "OPEN" and order_type != "LIMIT" and broker_order_id in self._known_order_ids:
            if self._last_order_type.get(broker_order_id) == order_type:
                logger.debug(
                    f"[Detector] Order {broker_order_id} order_type already {order_type}; ignoring duplicate OPEN."
                )
                return None
            self._last_order_type[broker_order_id] = order_type

            trade_event = MasterTradeEvent(
                master_order_id=broker_order_id,
                symbol=event.symbol,
                exchange=event.exchange or "NSE",
                side=event.side,
                quantity=event.quantity,
                price=event.price if event.price > 0 else event.fill_price,
                limit_price=event.price,
                trigger_price=event.trigger_price,
                product_type=event.product_type,
                timestamp=event.timestamp,
                fill_sequence=1,
                account_id=self.master_account_id,
                order_type=order_type,
                event_type="ORDER_MODIFIED",
                source="BROKER_EXTERNAL",
                total_quantity=event.quantity,
                filled_quantity=event.filled_quantity,
                pending_quantity=max(0, event.quantity - event.filled_quantity),
                status="OPEN",
                instrument_id=event.instrument_id,
            )
            logger.info(
                f"[Detector] Detected Master order_type change to {order_type} while still OPEN "
                f"(Order ID: {broker_order_id})"
            )
            return trade_event

        # 2. CANCELLATION DETECTION
        if status == "CANCELLED":
            return MasterTradeEvent(
                master_order_id=broker_order_id,
                symbol=event.symbol,
                exchange=event.exchange or "NSE",
                side=event.side,
                quantity=event.quantity,
                price=event.price or event.fill_price,
                limit_price=event.price,
                product_type=event.product_type,
                timestamp=event.timestamp,
                account_id=self.master_account_id,
                order_type=order_type,
                event_type="ORDER_CANCELLED",
                status="CANCELLED",
                instrument_id=event.instrument_id,
            )

        # 3. REJECTION DETECTION
        if status == "REJECTED":
            return MasterTradeEvent(
                master_order_id=broker_order_id,
                symbol=event.symbol,
                exchange=event.exchange or "NSE",
                side=event.side,
                quantity=event.quantity,
                price=event.price or event.fill_price,
                product_type=event.product_type,
                timestamp=event.timestamp,
                account_id=self.master_account_id,
                order_type=order_type,
                event_type="ORDER_REJECTED",
                status="REJECTED",
                rejection_reason=event.rejection_reason,
                instrument_id=event.instrument_id,
            )

        # 4. FILL DETECTION (COMPLETE / PARTIAL)
        if status in ("COMPLETE", "PARTIAL"):
            prev_filled, _ = self._seen_fills.get(broker_order_id, (0, 0))
            new_filled = event.filled_quantity

            # If complete order and filled_quantity wasn't explicitly populated, default to total quantity
            if new_filled == 0 and status == "COMPLETE":
                new_filled = event.quantity

            incremental_qty = new_filled - prev_filled
            if incremental_qty <= 0:
                logger.debug(f"[Detector] No incremental fill for order {broker_order_id} (prev={prev_filled}, current={new_filled})")
                return None

            # P0#2: fill_sequence is the dedup differentiator EventValidator and
            # derive_copy_event_id key off (alongside master_order_id). It used
            # to be an in-memory counter (fill_seq + 1) that resets to 0 on
            # every process restart - seed_state() has no durable source to
            # reconstruct it from. A restart happening while a MARKET order has
            # partial fills outstanding would then renumber the next genuine
            # post-restart fill back to a sequence value already consumed by a
            # pre-restart fill, colliding with the Redis SET NX key (24h TTL)
            # and the copy_trades unique constraint that key already produced -
            # silently dropping that follower's legitimate remaining fill.
            #
            # new_filled (cumulative filled_quantity) is durable - it's kept
            # current by save_or_update_order and correctly reseeded by
            # seed_state() from the Order row - and strictly increases with
            # every real fill (incremental_qty > 0 is already guaranteed
            # above), so it can never reproduce a value a dedup key has
            # already used, restart or not. Using it directly removes the
            # need for an in-memory counter entirely; the second element of
            # the _seen_fills tuple is kept only for structural compatibility
            # with seed_state()'s existing (filled_quantity, 0) signature.
            new_seq = new_filled
            self._seen_fills[broker_order_id] = (new_filled, new_seq)

            # INSTANT-FILL MASTER LIMIT.
            # A master LIMIT can fill inside the poll interval, so the very
            # first time the order book shows it, it is already COMPLETE (or
            # PARTIAL) and branch 1 - which is the only place that registers
            # _limit_order_state and emits LIMIT_CREATED - never ran for it.
            # The order type carried on the event is still "LIMIT"; what was
            # missing is that event typing keyed off _limit_order_state
            # membership alone, so an unregistered LIMIT fell through to the
            # else-branch and was replicated as MARKET_ACCEPTED. That routes
            # it down the marketable-LIMIT path and applies the 0.5% buffer -
            # observed in production as master LIMIT BUY 22.46 -> child LIMIT
            # 22.57 - which business rule 4 forbids for a LIMIT order.
            #
            # Such an order has never been replicated, so it is a creation,
            # not a fill update: emit LIMIT_CREATED at the master's EXACT
            # limit price and register the state so every later poll of the
            # same order is an ORDER_FILLED update instead of a second child
            # order. The master's order quantity (not the incremental fill)
            # is used, exactly as branch 1 does, because the child mirrors
            # the master's ORDER and then executes independently.
            # broker_order_id not in self._known_order_ids: a triggered
            # native SL that now shows order_type=LIMIT (e.g. master
            # 26092900012070's confirmed pattern) is ALREADY known via
            # mark_sl_child_created once its follower SL child exists -
            # this must fall through to the ORDER_FILLED path below
            # instead of being misread as a brand-new order here, which
            # would create a SECOND, duplicate child.
            if order_type == "LIMIT" and broker_order_id not in self._limit_order_state and broker_order_id not in self._known_order_ids:
                instant_limit_price = event.price if event.price > 0 else event.fill_price
                if broker_order_id:
                    self._limit_order_state[broker_order_id] = (event.quantity, instant_limit_price)
                    self._known_order_ids.add(broker_order_id)
                    self._last_order_type[broker_order_id] = "LIMIT"

                trade_event = MasterTradeEvent(
                    master_order_id=broker_order_id,
                    symbol=event.symbol,
                    exchange=event.exchange or "NSE",
                    side=event.side,
                    quantity=event.quantity,
                    price=instant_limit_price,
                    limit_price=instant_limit_price,
                    trigger_price=event.trigger_price,
                    product_type=event.product_type,
                    timestamp=event.timestamp,
                    fill_sequence=1,
                    account_id=self.master_account_id,
                    order_type="LIMIT",
                    event_type="LIMIT_CREATED",
                    source="BROKER_EXTERNAL",
                    total_quantity=event.quantity,
                    filled_quantity=new_filled,
                    pending_quantity=max(0, event.quantity - new_filled),
                    status="OPEN",
                    instrument_id=event.instrument_id,
                )
                logger.info(
                    f"[Detector] Detected Master LIMIT Created (already {status} on first "
                    f"observation): {trade_event.symbol} {trade_event.side} "
                    f"qty={trade_event.quantity} @ ₹{trade_event.limit_price} "
                    f"(Order ID: {trade_event.master_order_id})"
                )
                return trade_event

            # Determine event_type:
            # If this broker_order_id is already known to us (its creation -
            # or a later order_type change - was already replicated), this
            # fill is purely an ORDER_FILLED update for that SAME order,
            # regardless of what order_type it currently carries. Otherwise
            # it's a genuinely new MARKET_ACCEPTED replication event.
            #
            # Deliberately keyed on _known_order_ids (identity), NOT on
            # `order_type == "LIMIT"` at fill-time: that was the exact
            # production bug - a LIMIT order modified to MARKET before
            # filling has order_type == "MARKET" here even though it is the
            # same, already-replicated order, and the old check misclassified
            # its fill as a brand-new order, replicating a duplicate child.
            if broker_order_id in self._known_order_ids:
                ev_type = "ORDER_FILLED"
            else:
                ev_type = "MARKET_ACCEPTED"

            trade_event = MasterTradeEvent(
                master_order_id=broker_order_id,
                symbol=event.symbol,
                exchange=event.exchange or "NSE",
                side=event.side,
                quantity=incremental_qty,
                price=event.fill_price,
                limit_price=event.price,
                product_type=event.product_type,
                timestamp=event.timestamp,
                fill_sequence=new_seq,
                account_id=self.master_account_id,
                order_type=order_type,
                event_type=ev_type,
                total_quantity=event.quantity,
                filled_quantity=new_filled,
                pending_quantity=max(0, event.quantity - new_filled),
                status=status,
                instrument_id=event.instrument_id,
            )

            logger.info(
                f"[Detector] Detected Master Fill ({ev_type}): {trade_event.symbol} {trade_event.side} "
                f"qty={trade_event.quantity} (seq {trade_event.fill_sequence}) @ ₹{trade_event.price}"
            )
            return trade_event

        return None
