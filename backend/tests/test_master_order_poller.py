"""Coverage for MasterOrderPoller - the REST order-book poll that observes
master orders placed OUTSIDE Jettone (Alice Blue Mobile app / Web terminal),
which the NorenWS socket never delivers.

The two safety rules are what these tests mostly exist to pin down:
 1. The first poll is a baseline seed - it must persist what it sees but
    dispatch NOTHING to the copy engine, and those baseline orders must stay
    permanently replication-ineligible even after their status later changes.
 2. Orders Jettone placed itself (source=INTERNAL_UI) are never handed to the
    copy engine again - app/api/broker.py already replicated them at
    placement time.

No live trading is enabled and no real broker calls are made anywhere in
this file - the broker client is a mock returning canned OrderRecords.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.types import OrderRecord, OrderSide as BrokerOrderSide
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.reliability.master_order_poller import MasterOrderPoller

MASTER_ID = "caf4726b-c48c-4e54-b899-ceb0f7259da8"


def _record(broker_order_id, status="COMPLETE", filled_quantity=1, quantity=1,
            order_type="LIMIT", side=BrokerOrderSide.BUY, price=Decimal("22.49"),
            trigger_price=Decimal("0"), instrument_id=None):
    return OrderRecord(
        broker_order_id=broker_order_id,
        client_order_id=None,
        symbol="YESBANK-EQ",
        exchange="NSE",
        side=side,
        quantity=quantity,
        filled_quantity=filled_quantity,
        average_price=price,
        status=status,
        order_timestamp=datetime.now(timezone.utc),
        order_type=order_type,
        product_type="MIS",
        price=price,
        trigger_price=trigger_price,
        instrument_id=instrument_id,
    )


def _build_poller(book, dispatched, mock_redis, interval=5):
    """Poller wired to a mock broker client returning `book`, recording every
    dispatched event into `dispatched`."""
    client = MagicMock()
    client.get_order_book = AsyncMock(return_value=book)

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))

    return MasterOrderPoller(
        master_account_id=MASTER_ID,
        client_provider=lambda _acc_id: client,
        on_order_update=lambda event: dispatched.append(event),
        pipeline=pipeline,
        check_interval_seconds=interval,
    )


async def _order_rows(session, broker_order_id=None):
    from sqlalchemy import select
    stmt = select(Order).where(Order.account_id == uuid.UUID(MASTER_ID))
    if broker_order_id:
        stmt = stmt.where(Order.broker_order_id == broker_order_id)
    return (await session.execute(stmt)).scalars().all()


@pytest.mark.asyncio
async def test_first_poll_seeds_baseline_and_dispatches_nothing(db_session, mock_redis):
    """Rule 1: starting the poller mid-session must never replicate the
    orders that were already there - it would fire real follower orders for
    master activity that happened before the engine was watching."""
    book = [_record("26090800085772"), _record("26090800086037", side=BrokerOrderSide.SELL)]
    dispatched = []
    poller = _build_poller(book, dispatched, mock_redis)

    count = await poller.poll_once()

    assert count == 0
    assert dispatched == []
    # ...but they ARE persisted, so master order history becomes complete.
    rows = await _order_rows(db_session)
    assert {r.broker_order_id for r in rows} == {"26090800085772", "26090800086037"}
    assert all(r.source == OrderSource.BROKER_EXTERNAL for r in rows)


@pytest.mark.asyncio
async def test_baseline_order_never_dispatches_even_after_status_change(db_session, mock_redis):
    """A pre-existing order transitioning OPEN->COMPLETE after the baseline
    is still master activity that predates us: persist the change, never
    replicate it."""
    open_rec = _record("26090800085772", status="OPEN", filled_quantity=0)
    dispatched = []
    poller = _build_poller([open_rec], dispatched, mock_redis)

    await poller.poll_once()  # baseline
    assert dispatched == []

    # Same order, now filled.
    poller.client_provider(MASTER_ID).get_order_book = AsyncMock(
        return_value=[_record("26090800085772", status="COMPLETE", filled_quantity=1)]
    )
    count = await poller.poll_once()

    assert count == 0
    assert dispatched == []
    rows = await _order_rows(db_session, "26090800085772")
    assert rows[0].status == OrderStatus.COMPLETE
    assert rows[0].filled_quantity == 1


@pytest.mark.asyncio
async def test_new_external_order_after_baseline_is_dispatched(db_session, mock_redis):
    """The actual bug this module fixes: a Mobile-placed master order that
    appears after the baseline must reach the copy pipeline."""
    dispatched = []
    poller = _build_poller([_record("26090800085772")], dispatched, mock_redis)
    await poller.poll_once()  # baseline
    assert dispatched == []

    mobile_order = _record("26090800089999", status="OPEN", filled_quantity=0, order_type="LIMIT")
    poller.client_provider(MASTER_ID).get_order_book = AsyncMock(
        return_value=[_record("26090800085772"), mobile_order]
    )
    count = await poller.poll_once()

    assert count == 1
    assert len(dispatched) == 1
    event = dispatched[0]
    assert event.broker_order_id == "26090800089999"
    assert event.account_id == MASTER_ID
    assert event.status == "OPEN"
    assert event.order_type == "LIMIT"
    assert event.side == "BUY"


@pytest.mark.asyncio
async def test_jettone_placed_order_is_persisted_but_never_dispatched(db_session, mock_redis):
    """Rule 2: broker.py already replicated INTERNAL_UI orders synchronously.
    Re-dispatching risks a second follower order, because the detector
    derives fill_sequence from cumulative filled quantity, which only
    collides with the LIMIT path's fill_sequence=1 for single-share orders."""
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await pipeline.save_or_update_order(
        internal_order_id="JT_MANUAL_abc123",
        broker_order_id="26090800089510",
        account_id=uuid.UUID(MASTER_ID),
        source=OrderSource.INTERNAL_UI,
        exchange="NSE",
        symbol="YESBANK-EQ",
        side=OrderSide.BUY,
        order_type="LIMIT",
        product_type="MIS",
        quantity=1,
        price=Decimal("22.50"),
        status=OrderStatus.OPEN,
    )

    dispatched = []
    poller = _build_poller([_record("00000000000001")], dispatched, mock_redis)
    await poller.poll_once()  # baseline (unrelated order)

    poller.client_provider(MASTER_ID).get_order_book = AsyncMock(
        return_value=[_record("00000000000001"),
                      _record("26090800089510", status="COMPLETE", filled_quantity=1, price=Decimal("22.50"))]
    )
    count = await poller.poll_once()

    assert count == 0
    assert dispatched == []
    # The fill is still recorded against the original INTERNAL_UI row -
    # nothing else updates the master's own fills once the socket is silent.
    rows = await _order_rows(db_session, "26090800089510")
    assert len(rows) == 1
    assert rows[0].source == OrderSource.INTERNAL_UI
    assert rows[0].status == OrderStatus.COMPLETE
    assert rows[0].filled_quantity == 1


@pytest.mark.asyncio
async def test_unchanged_order_is_not_redispatched(db_session, mock_redis):
    dispatched = []
    poller = _build_poller([_record("00000000000001")], dispatched, mock_redis)
    await poller.poll_once()  # baseline

    new_order = _record("26090800089999", status="OPEN", filled_quantity=0)
    poller.client_provider(MASTER_ID).get_order_book = AsyncMock(
        return_value=[_record("00000000000001"), new_order]
    )
    assert await poller.poll_once() == 1
    # Same book again - nothing changed, so nothing is dispatched a second time.
    assert await poller.poll_once() == 0
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_partial_then_complete_dispatches_each_change(db_session, mock_redis):
    dispatched = []
    poller = _build_poller([], dispatched, mock_redis)
    await poller.poll_once()  # empty baseline

    client = poller.client_provider(MASTER_ID)
    client.get_order_book = AsyncMock(
        return_value=[_record("26090800089999", status="PARTIAL", filled_quantity=40, quantity=100)]
    )
    assert await poller.poll_once() == 1

    client.get_order_book = AsyncMock(
        return_value=[_record("26090800089999", status="COMPLETE", filled_quantity=100, quantity=100)]
    )
    assert await poller.poll_once() == 1

    assert [e.status for e in dispatched] == ["PARTIAL", "COMPLETE"]
    assert [e.filled_quantity for e in dispatched] == [40, 100]


@pytest.mark.asyncio
async def test_broker_failure_does_not_raise_or_seed(db_session, mock_redis):
    """A failed fetch must not be mistaken for 'the account has no orders' -
    that would seed an empty baseline and make every existing order look new
    on the next successful poll."""
    dispatched = []
    poller = _build_poller([], dispatched, mock_redis)
    poller.client_provider(MASTER_ID).get_order_book = AsyncMock(side_effect=Exception("broker down"))

    assert await poller.poll_once() == 0
    assert poller._seeded is False

    poller.client_provider(MASTER_ID).get_order_book = AsyncMock(
        return_value=[_record("26090800085772")]
    )
    assert await poller.poll_once() == 0  # this is now the real baseline
    assert dispatched == []


@pytest.mark.asyncio
async def test_missing_client_does_not_seed(db_session, mock_redis):
    dispatched = []
    poller = _build_poller([_record("26090800085772")], dispatched, mock_redis)
    poller.client_provider = lambda _acc_id: None

    assert await poller.poll_once() == 0
    assert poller._seeded is False


@pytest.mark.asyncio
async def test_unmapped_broker_status_is_not_dispatched(db_session, mock_redis):
    """Fail closed on a status we don't understand rather than guessing at
    an order lifecycle state for a real-money copy."""
    dispatched = []
    poller = _build_poller([], dispatched, mock_redis)
    await poller.poll_once()

    poller.client_provider(MASTER_ID).get_order_book = AsyncMock(
        return_value=[_record("26090800089999", status="SOME_NEW_STATUS")]
    )
    assert await poller.poll_once() == 0
    assert dispatched == []



# G. Existing normal statuses are unaffected --------------------------------

@pytest.mark.asyncio
async def test_ordinary_statuses_are_still_mapped_and_replicated_normally(db_session, mock_redis):
    """Regression guard: adding TRIGGER_PENDING must not perturb any
    already-understood status, including the broker's own literal "PENDING"
    string (a different thing entirely - it maps to OrderStatus.OPEN, not
    the new OrderStatus.PENDING used for TRIGGER_PENDING)."""
    for raw_status, expected in [
        ("OPEN", OrderStatus.OPEN),
        ("PENDING", OrderStatus.OPEN),
        ("PARTIAL", OrderStatus.PARTIAL),
        ("COMPLETE", OrderStatus.COMPLETE),
        ("CANCELLED", OrderStatus.CANCELLED),
        ("REJECTED", OrderStatus.REJECTED),
    ]:
        dispatched = []
        poller = _build_poller([], dispatched, mock_redis)
        await poller.poll_once()  # empty baseline

        broker_order_id = f"26090800{hash(raw_status) % 100000:05d}"
        poller.client_provider(MASTER_ID).get_order_book = AsyncMock(
            return_value=[_record(broker_order_id, status=raw_status, order_type="LIMIT",
                                   filled_quantity=1 if raw_status in ("COMPLETE", "PARTIAL") else 0)]
        )
        count = await poller.poll_once()

        assert count == 1, f"status {raw_status} should still dispatch"
        assert dispatched[0].status == expected.value
