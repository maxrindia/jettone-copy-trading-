"""
Audit P0 #2 regression coverage: MasterTradeDetector's fill_sequence must be
derived from durable cumulative filled_quantity, not a resettable in-memory
counter, so a MARKET order that partially fills before a process restart
and continues filling after it cannot have its legitimate post-restart
remainder dropped as a duplicate by EventValidator's Redis dedup or the
copy_trades DB unique constraint (both keyed by (master_order_id,
fill_sequence) / (master_order_id, follower_account_id, fill_sequence)).

No live trading is enabled and no real broker calls are made anywhere in
this file - all broker clients are AsyncMock test doubles.
"""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.alice_blue.types import FundsData, OrderResult, OrderResultCategory, OrderUpdateEvent
from app.services.copy_engine.detector import MasterTradeDetector
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator


def _market_fill_event(broker_order_id, filled_quantity, quantity=100, account_id="M1", symbol="RELIANCE"):
    status = "COMPLETE" if filled_quantity >= quantity else "PARTIAL"
    return OrderUpdateEvent(
        broker_order_id=broker_order_id,
        client_order_id=None,
        symbol=symbol,
        side="BUY",
        quantity=quantity,
        filled_quantity=filled_quantity,
        fill_price=Decimal("100.00"),
        status=status,
        timestamp=datetime.now(timezone.utc),
        account_id=account_id,
        order_type="MARKET",
    )


def _client():
    client = AsyncMock()
    client.get_funds.return_value = FundsData(
        account_id="f", available_margin=Decimal("500000"), collateral=Decimal("0"),
        used_margin=Decimal("0"), updated_at=datetime.now(timezone.utc),
    )
    client.get_positions.return_value = []
    client.place_order.return_value = OrderResult(
        category=OrderResultCategory.SUCCESS, client_order_id="c", broker_order_id="BRK_FOLLOWER",
        message="ok", raw_response={"stat": "Ok"}, timestamp=datetime.now(timezone.utc),
    )
    return client


def test_fill_sequence_is_cumulative_filled_quantity_within_a_single_run():
    detector = MasterTradeDetector(master_account_id="M1")

    event1 = detector.process_order_update(_market_fill_event("ORD_SEQ_1", filled_quantity=40, quantity=100))
    assert event1.fill_sequence == 40  # cumulative, not counter value 1

    event2 = detector.process_order_update(_market_fill_event("ORD_SEQ_1", filled_quantity=100, quantity=100))
    assert event2.fill_sequence == 100  # cumulative, not counter value 2


@pytest.mark.asyncio
async def test_post_restart_market_fill_reaches_followers_not_dropped_as_duplicate(db_session, mock_redis, sample_accounts, sample_strategy):
    """The core P0#2 scenario: a MARKET order fills 40/100 before a restart
    (replicated + dedup keys consumed), then a FRESH detector (simulating
    restart, seeded from the durable Order row exactly as EngineLoop does)
    observes the order continuing to 100/100. The genuine remaining 60 must
    reach followers, not be dropped as a duplicate of the pre-restart fill."""
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]
    master_id = str(master.id)

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    client = _client()

    # --- Pre-restart: first partial fill (40/100) ---
    detector_before = MasterTradeDetector(master_account_id=master_id)
    event_before = detector_before.process_order_update(
        _market_fill_event("ORD_RESTART_1", filled_quantity=40, quantity=100, account_id=master_id)
    )
    assert event_before is not None
    res1 = await pipeline.execute_replication(event=event_before, clients={str(follower.id): client})
    assert res1["status"] == "completed"
    assert client.place_order.await_count == 1
    assert client.place_order.call_args.args[0].quantity == 40

    # --- Simulate restart: fresh detector, seeded from the durable Order
    # row exactly as EngineLoop's startup seeding does (Issue #5). ---
    detector_after = MasterTradeDetector(master_account_id=master_id)
    detector_after.seed_state([
        SimpleNamespace(broker_order_id="ORD_RESTART_1", filled_quantity=40, order_type="MARKET", quantity=100, price=Decimal("0")),
    ])

    # --- Post-restart: order continues filling to 100/100 (remaining 60) ---
    event_after = detector_after.process_order_update(
        _market_fill_event("ORD_RESTART_1", filled_quantity=100, quantity=100, account_id=master_id)
    )
    assert event_after is not None
    assert event_after.quantity == 60           # the genuine remaining delta
    assert event_after.fill_sequence == 100      # cumulative - differs from the pre-restart key (40)

    res2 = await pipeline.execute_replication(event=event_after, clients={str(follower.id): client})
    assert res2["status"] == "completed"
    assert res2.get("reason") != "DUPLICATE_EVENT"
    assert client.place_order.await_count == 2   # second, genuine call actually reached the broker
    assert client.place_order.call_args.args[0].quantity == 60


def test_pre_restart_fill_itself_still_not_duplicated_by_the_same_detector():
    """Regression: the exact same (unchanged) event redelivered to the SAME,
    un-restarted detector must still be suppressed by the detector's own
    incremental-fill check - unaffected by this fix."""
    detector = MasterTradeDetector(master_account_id="M1")

    first = detector.process_order_update(_market_fill_event("ORD_DUP_1", filled_quantity=40, quantity=100))
    assert first is not None

    duplicate = detector.process_order_update(_market_fill_event("ORD_DUP_1", filled_quantity=40, quantity=100))
    assert duplicate is None


@pytest.mark.asyncio
async def test_redis_dedup_still_blocks_a_genuine_duplicate_event(db_session, mock_redis, sample_accounts, sample_strategy):
    """Two independently-constructed MasterTradeEvents carrying the SAME
    (master_order_id, fill_sequence) - as if the same WS message were
    redelivered and somehow reached execute_replication twice - must still
    be rejected the second time by EventValidator's Redis dedup."""
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    client = _client()

    def _trade_event():
        return MasterTradeEvent(
            master_order_id="ORD_REDIS_DEDUP",
            symbol="RELIANCE", exchange="NSE", side="BUY", quantity=40,
            price=Decimal("100.00"), product_type="MIS",
            timestamp=datetime.now(timezone.utc), fill_sequence=40,
            account_id=str(master.id), order_type="MARKET", event_type="MARKET_ACCEPTED",
            total_quantity=100, filled_quantity=40, pending_quantity=60, status="PARTIAL",
        )

    res1 = await pipeline.execute_replication(event=_trade_event(), clients={str(follower.id): client})
    assert res1["status"] == "completed"
    assert client.place_order.await_count == 1

    res2 = await pipeline.execute_replication(event=_trade_event(), clients={str(follower.id): client})
    assert res2["status"] == "rejected"
    assert res2["reason"] == "DUPLICATE_EVENT"
    assert client.place_order.await_count == 1  # not called again


@pytest.mark.asyncio
async def test_db_unique_constraint_still_blocks_duplicate_copy_trade_independent_of_redis(db_session, sample_accounts, sample_strategy):
    """Isolates the DB-level backstop: bypass Redis dedup entirely (two
    separate EventValidator instances, each backed by its own always-passes
    Redis double) and confirm the copy_trades unique constraint on
    (master_order_id, follower_account_id, copy_event_id) still
    independently blocks a duplicate call with the same fill_sequence-
    derived key."""
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]
    client = _client()

    event = MasterTradeEvent(
        master_order_id="ORD_DB_BACKSTOP",
        symbol="RELIANCE", exchange="NSE", side="BUY", quantity=40,
        price=Decimal("100.00"), product_type="MIS",
        timestamp=datetime.now(timezone.utc), fill_sequence=40,
        account_id=str(master.id), order_type="MARKET", event_type="MARKET_ACCEPTED",
        total_quantity=100, filled_quantity=40, pending_quantity=60, status="PARTIAL",
    )

    def _fresh_always_new_redis():
        r = AsyncMock()
        r.set = AsyncMock(return_value=True)  # always "not seen before" - Redis dedup bypassed
        return r

    pipeline1 = CopyEnginePipeline(validator=EventValidator(redis=_fresh_always_new_redis()))
    res1 = await pipeline1.execute_replication(event=event, clients={str(follower.id): client})
    assert res1["status"] == "completed"
    assert client.place_order.await_count == 1

    pipeline2 = CopyEnginePipeline(validator=EventValidator(redis=_fresh_always_new_redis()))
    res2 = await pipeline2.execute_replication(event=event, clients={str(follower.id): client})
    # Redis said "not seen" both times, but the DB unique constraint on
    # (master_order_id, follower_account_id, copy_event_id) still catches it.
    assert res2["status"] == "completed"
    assert res2["results"][str(follower.id)]["status"] == "SKIPPED"
    assert res2["results"][str(follower.id)]["reason"] == "DUPLICATE_EVENT"
    assert client.place_order.await_count == 1  # broker never called a second time


@pytest.mark.asyncio
async def test_pipeline_level_dedup_alone_cannot_prevent_a_second_independent_replication_trigger(db_session, mock_redis, sample_accounts, sample_strategy):
    """
    Historical/defense-in-depth note: this test simulates what USED TO
    happen before the P1 fix (broker.py::place_manual_master_order no
    longer replicates MARKET fills itself at all - see
    test_manual_master_order.py's
    test_manual_market_order_partial_fill_does_not_replicate_immediately /
    ..._complete_fill_does_not_replicate_immediately /
    ..._then_ws_detector_replicates_exactly_once_with_correct_quantity for
    the real, current, fixed behavior through the actual endpoint).

    It's kept here as a pipeline-level demonstration of WHY the P1 fix had
    to prevent the second replication trigger from existing at all, rather
    than relying on the dedup layer: two independently-constructed
    MasterTradeEvents for the same broker order, with genuinely different
    fill_sequence values (40 vs 100, both legitimate cumulative-quantity
    keys per P0#2's own scheme), are NOT duplicates from the dedup layer's
    point of view - EventValidator/derive_copy_event_id correctly do not
    (and should not) collapse them. Dedup alone cannot distinguish "two
    real, sequential fills of the same order" from "two different code
    paths racing to replicate the same fill" - only removing the second
    trigger (this test's `manual_event`, which broker.py no longer sends)
    actually fixes it.
    """
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]
    master_id = str(master.id)
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    client = _client()

    # Simulates what broker.py's manual-order endpoint USED TO send before
    # the P1 fix - fill_sequence hardcoded to 1, no knowledge of the
    # detector's state. The real endpoint no longer constructs this event.
    manual_event = MasterTradeEvent(
        master_order_id="ORD_MANUAL_PARTIAL", symbol="RELIANCE", exchange="NSE", side="BUY",
        quantity=40, price=Decimal("100.00"), product_type="MIS",
        timestamp=datetime.now(timezone.utc), fill_sequence=1,
        account_id=master_id, order_type="MARKET", event_type="MARKET_ACCEPTED", status="PARTIAL",
    )
    res1 = await pipeline.execute_replication(event=manual_event, clients={str(follower.id): client})
    assert res1["status"] == "completed"
    assert client.place_order.await_count == 1
    assert client.place_order.call_args.args[0].quantity == 40

    # The running detector never learned about this - its own _seen_fills
    # for this broker_order_id is still empty (prev_filled=0).
    detector = MasterTradeDetector(master_account_id=master_id)
    ws_event = detector.process_order_update(
        _market_fill_event("ORD_MANUAL_PARTIAL", filled_quantity=100, quantity=100, account_id=master_id)
    )
    assert ws_event is not None
    assert ws_event.quantity == 100  # detector computes the FULL amount, unaware 40 was already sent

    res2 = await pipeline.execute_replication(event=ws_event, clients={str(follower.id): client})
    assert res2["status"] == "completed"
    assert res2.get("reason") != "DUPLICATE_EVENT"  # P0#2: cumulative-quantity keys differ, correctly not a dedup match
    assert client.place_order.await_count == 2
    assert client.place_order.call_args.args[0].quantity == 100
    # ...so at the pipeline level alone, followers would receive 40 + 100 =
    # 140 total instead of the intended 100 - this is exactly why the P1 fix
    # removes `manual_event` from ever being sent in the first place (see
    # test_manual_master_order.py), rather than trying to fix this at the
    # dedup layer.
