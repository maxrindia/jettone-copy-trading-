"""
Regression coverage for PENDING/OPEN Master LIMIT lifecycle synchronisation:
Master MODIFY -> Child MODIFY and Master CANCEL -> Child CANCEL.

Production symptom (2026-09-08 / 2026-09-09, Mobile-placed master orders):
a still-pending master LIMIT was modified from the Alice Blue mobile app and
the child never moved; modifyorder was in fact never called even once.

Root cause pinned here: MasterOrderPoller._seen_state fingerprinted each
order as (status, filled_quantity) only. Modifying a pending LIMIT changes
its price and/or quantity while status stays OPEN and filled stays 0, so the
fingerprint was byte-identical to the previous poll and poll_once() skipped
the record with `continue` BEFORE it ever reached MasterTradeDetector - which
had correct ORDER_MODIFIED handling all along, and simply never got the
event. The fix widens that fingerprint (_change_key) to include quantity,
price and trigger price.

These tests therefore pin the whole chain, not just the unit that changed:
REST record -> poller dispatch -> detector event type -> pipeline routing ->
child broker call -> persistence, plus the partial-fill invariants.

No live trading is enabled and no real broker calls are made anywhere in this
file - every broker client is an AsyncMock/MagicMock test double.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.models.accounts import Account, AccountType
from app.models.audit import SystemEvent
from app.models.strategies import GroupStrategy, StrategyMember
from app.models.trades import (
    CopyTrade,
    CopyTradeStatus,
    Order,
    OrderSide,
    OrderSource,
    OrderStatus,
)
from app.services.alice_blue.types import (
    OrderRecord,
    OrderResult,
    OrderResultCategory,
    OrderSide as BrokerOrderSide,
)
from app.services.copy_engine.detector import MasterTradeDetector
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.reliability.master_order_poller import MasterOrderPoller

MASTER_ID = "caf4726b-c48c-4e54-b899-ceb0f7259da8"
MASTER_ORDER_ID = "26090900064736"
CHILD_ORDER_ID = "26090900064746"


# ── builders ──────────────────────────────────────────────────────────────
def _record(
    broker_order_id=MASTER_ORDER_ID,
    status="OPEN",
    filled_quantity=0,
    quantity=1,
    price=Decimal("22.40"),
    order_type="LIMIT",
    side=BrokerOrderSide.BUY,
):
    return OrderRecord(
        broker_order_id=broker_order_id,
        client_order_id=None,
        symbol="YESBANK",
        exchange="NSE",
        side=side,
        quantity=quantity,
        filled_quantity=filled_quantity,
        average_price=Decimal("0"),
        status=status,
        order_timestamp=datetime.now(timezone.utc),
        order_type=order_type,
        product_type="INTRADAY",
        price=price,
    )


def _build_poller(book_holder, dispatched, mock_redis):
    """Poller whose mock broker returns whatever list book_holder[0] holds."""
    client = MagicMock()
    client.get_order_book = AsyncMock(side_effect=lambda: list(book_holder[0]))
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    return MasterOrderPoller(
        master_account_id=MASTER_ID,
        client_provider=lambda _acc: client,
        on_order_update=lambda event: dispatched.append(event),
        pipeline=pipeline,
        check_interval_seconds=5,
    )


def _child_order(account_id, broker_order_id=CHILD_ORDER_ID, quantity=1,
                 filled_quantity=0, price=Decimal("22.40")):
    return Order(
        id=uuid.uuid4(),
        internal_order_id=f"JT_CH_{broker_order_id}",
        broker_order_id=broker_order_id,
        account_id=account_id,
        master_broker_order_id=MASTER_ORDER_ID,
        source=OrderSource.SYSTEM,
        exchange="NSE",
        symbol="YESBANK",
        side=OrderSide.BUY,
        order_type="LIMIT",
        product_type="INTRADAY",
        quantity=quantity,
        filled_quantity=filled_quantity,
        pending_quantity=quantity - filled_quantity,
        price=price,
        status=OrderStatus.PARTIAL if filled_quantity > 0 else OrderStatus.OPEN,
    )


async def _make_strategy(db_session, master, members):
    strategy = GroupStrategy(
        id=uuid.uuid4(),
        name="Lifecycle Sync Strategy",
        master_account_id=master.id,
        trading_enabled=True,
    )
    db_session.add(strategy)
    for account, multiplier in members:
        db_session.add(
            StrategyMember(
                id=uuid.uuid4(),
                strategy_id=strategy.id,
                account_id=account.id,
                multiplier=multiplier,
                trading_enabled=True,
            )
        )
    await db_session.commit()
    return strategy


def _as_master_event(poller, detector, record, master_id):
    """Route a REST record through _to_event -> detector, stamped with the
    fixture's master account id (the detector rejects foreign accounts)."""
    update = MasterOrderPoller._to_event(poller, record)
    update.account_id = str(master_id)
    return detector.process_order_update(update)


def _ok(broker_order_id, message="OK"):
    return OrderResult(
        category=OrderResultCategory.SUCCESS,
        client_order_id=None,
        broker_order_id=broker_order_id,
        message=message,
        raw_response={"stat": "Ok"},
        timestamp=datetime.now(timezone.utc),
    )


def _refused(broker_order_id, message="HTTP method POST is not supported by this URL"):
    """Mirrors the real Alice Blue cancelorder/modifyorder refusal."""
    return OrderResult(
        category=OrderResultCategory.REJECTED,
        client_order_id=None,
        broker_order_id=broker_order_id,
        message=f"Cancel rejected: {message}",
        raw_response={},
        timestamp=datetime.now(timezone.utc),
    )


# ── 1-3, 12: poller detection of pending LIMIT create / modify ────────────
@pytest.mark.asyncio
async def test_1_pending_master_limit_detected_from_rest_polling(db_session, mock_redis):
    """A master LIMIT sitting OPEN/pending is dispatched by REST polling."""
    book = [[]]
    dispatched = []
    poller = _build_poller(book, dispatched, mock_redis)

    await poller.poll_once()                      # baseline seed: empty book
    book[0] = [_record(status="OPEN", price=Decimal("22.40"))]
    await poller.poll_once()

    assert len(dispatched) == 1
    assert dispatched[0].broker_order_id == MASTER_ORDER_ID
    assert dispatched[0].status == "OPEN"
    assert dispatched[0].order_type == "LIMIT"
    assert dispatched[0].price == Decimal("22.40")


@pytest.mark.asyncio
async def test_3_pending_master_limit_price_modification_is_detected(db_session, mock_redis):
    """THE BUG: price change with status/fill unchanged must still dispatch."""
    book = [[]]
    dispatched = []
    poller = _build_poller(book, dispatched, mock_redis)

    await poller.poll_once()
    book[0] = [_record(status="OPEN", price=Decimal("22.40"))]
    await poller.poll_once()
    assert len(dispatched) == 1

    # Master edits the pending LIMIT price on the mobile app.
    book[0] = [_record(status="OPEN", price=Decimal("22.60"))]
    await poller.poll_once()

    assert len(dispatched) == 2, "price-only modification was skipped by the poller"
    assert dispatched[1].price == Decimal("22.60")


@pytest.mark.asyncio
async def test_3b_pending_master_limit_quantity_modification_is_detected(db_session, mock_redis):
    book = [[]]
    dispatched = []
    poller = _build_poller(book, dispatched, mock_redis)

    await poller.poll_once()
    book[0] = [_record(status="OPEN", quantity=10)]
    await poller.poll_once()
    book[0] = [_record(status="OPEN", quantity=25)]
    await poller.poll_once()

    assert len(dispatched) == 2
    assert dispatched[1].quantity == 25


@pytest.mark.asyncio
async def test_12_unchanged_repeated_polls_do_not_redispatch(db_session, mock_redis):
    """Idempotency: identical repeat polls must not produce extra events."""
    book = [[]]
    dispatched = []
    poller = _build_poller(book, dispatched, mock_redis)

    await poller.poll_once()
    book[0] = [_record(status="OPEN", price=Decimal("22.40"))]
    for _ in range(5):
        await poller.poll_once()

    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_6_master_cancellation_is_detected(db_session, mock_redis):
    book = [[]]
    dispatched = []
    poller = _build_poller(book, dispatched, mock_redis)

    await poller.poll_once()
    book[0] = [_record(status="OPEN")]
    await poller.poll_once()
    book[0] = [_record(status="CANCELLED")]
    await poller.poll_once()

    assert len(dispatched) == 2
    assert dispatched[1].status == "CANCELLED"


# ── detector: event typing for create vs modify ───────────────────────────
@pytest.mark.asyncio
async def test_2_detector_emits_limit_created_then_modified(db_session, mock_redis):
    """Poller output must type as LIMIT_CREATED first, ORDER_MODIFIED after."""
    book = [[]]
    dispatched = []
    poller = _build_poller(book, dispatched, mock_redis)
    detector = MasterTradeDetector(master_account_id=MASTER_ID)

    await poller.poll_once()
    book[0] = [_record(status="OPEN", price=Decimal("22.40"))]
    await poller.poll_once()
    book[0] = [_record(status="OPEN", price=Decimal("22.60"))]
    await poller.poll_once()

    created = detector.process_order_update(dispatched[0])
    modified = detector.process_order_update(dispatched[1])

    assert created.event_type == "LIMIT_CREATED"
    assert created.limit_price == Decimal("22.40")
    assert modified.event_type == "ORDER_MODIFIED"
    assert modified.limit_price == Decimal("22.60")


@pytest.mark.asyncio
async def test_13_restart_still_detects_later_modification(db_session, mock_redis):
    """After a restart the detector seeds from Order rows, so a subsequent
    master MODIFY is still typed ORDER_MODIFIED (not a fresh LIMIT_CREATED,
    which would place a second child order)."""
    seed_order = Order(
        id=uuid.uuid4(),
        internal_order_id=f"EXT_{MASTER_ORDER_ID}",
        broker_order_id=MASTER_ORDER_ID,
        account_id=uuid.UUID(MASTER_ID),
        source=OrderSource.BROKER_EXTERNAL,
        exchange="NSE",
        symbol="YESBANK",
        side=OrderSide.BUY,
        order_type="LIMIT",
        product_type="INTRADAY",
        quantity=1,
        filled_quantity=0,
        pending_quantity=1,
        price=Decimal("22.40"),
        status=OrderStatus.OPEN,
    )
    db_session.add(seed_order)
    await db_session.commit()

    detector = MasterTradeDetector(master_account_id=MASTER_ID)
    detector.seed_state([seed_order])

    book = [[_record(status="OPEN", price=Decimal("22.40"))]]
    dispatched = []
    poller = _build_poller(book, dispatched, mock_redis)
    await poller.poll_once()                       # baseline: this order
    book[0] = [_record(status="OPEN", price=Decimal("22.75"))]
    await poller.poll_once()

    # Baseline orders are never replication-eligible, so the poller does not
    # dispatch - but the detector state survived the restart, which is what
    # keeps a later modification from being misread as a brand-new order.
    event = detector.process_order_update(
        MasterOrderPoller._to_event(poller, _record(status="OPEN", price=Decimal("22.75")))
    )
    assert event is not None
    assert event.event_type == "ORDER_MODIFIED"
    assert event.limit_price == Decimal("22.75")


# ── 4, 5, 10: modification reaches the child at the exact master price ────
@pytest.mark.asyncio
async def test_4_5_modification_reaches_child_at_exact_master_price(
    db_session, mock_redis, sample_accounts
):
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]
    await _make_strategy(db_session, master, [(follower, Decimal("1.0"))])
    db_session.add(_child_order(follower.id))
    await db_session.commit()

    client = AsyncMock()
    client.modify_order.return_value = _ok(CHILD_ORDER_ID, "Modified")

    detector = MasterTradeDetector(master_account_id=str(master.id))
    detector._limit_order_state[MASTER_ORDER_ID] = (1, Decimal("22.40"))

    poller = _build_poller([[]], [], mock_redis)
    event = _as_master_event(
        poller, detector, _record(status="OPEN", price=Decimal("22.60")), master.id
    )

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=event, clients={str(follower.id): client}
    )

    assert res["status"] == "completed"
    client.modify_order.assert_awaited_once()
    # EXACT master limit price - no 0.5% buffer on the LIMIT path.
    assert client.modify_order.call_args.kwargs["price"] == Decimal("22.60")


@pytest.mark.asyncio
async def test_10_modification_preserves_child_filled_quantity(
    db_session, mock_redis, sample_accounts
):
    """Master 10 (4 filled) modified; child 10 with 3 filled -> only the
    remaining 7 may be modified, the filled 3 is never touched."""
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]
    await _make_strategy(db_session, master, [(follower, Decimal("1.0"))])
    child = _child_order(follower.id, quantity=10, filled_quantity=3)
    db_session.add(child)
    await db_session.commit()

    client = AsyncMock()
    client.modify_order.return_value = _ok(CHILD_ORDER_ID, "Modified")

    detector = MasterTradeDetector(master_account_id=str(master.id))
    detector._limit_order_state[MASTER_ORDER_ID] = (10, Decimal("22.40"))
    poller = _build_poller([[]], [], mock_redis)
    event = _as_master_event(
        poller,
        detector,
        _record(status="OPEN", quantity=10, filled_quantity=4, price=Decimal("22.60")),
        master.id,
    )

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await pipeline.execute_replication(event=event, clients={str(follower.id): client})

    client.modify_order.assert_awaited_once()
    # 10 * 1.0 target - 3 already filled on the child = 7 pending only.
    assert client.modify_order.call_args.kwargs["quantity"] == 7

    await db_session.refresh(child)
    assert child.filled_quantity == 3, "already-filled child quantity was altered"


@pytest.mark.asyncio
async def test_failed_child_modification_is_surfaced(db_session, mock_redis, sample_accounts):
    """A refused modify must not be reported as completed (same discipline as
    the cancel fix in 20354b0)."""
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]
    await _make_strategy(db_session, master, [(follower, Decimal("1.0"))])
    db_session.add(_child_order(follower.id))
    db_session.add(
        CopyTrade(
            id=uuid.uuid4(),
            master_order_id=MASTER_ORDER_ID,
            follower_account_id=follower.id,
            copy_event_id=uuid.uuid4(),
            broker_order_id=CHILD_ORDER_ID,
            symbol="YESBANK",
            exchange="NSE",
            side=OrderSide.BUY,
            quantity=1,
            product_type="INTRADAY",
            status=CopyTradeStatus.SUBMITTED,
        )
    )
    await db_session.commit()

    client = AsyncMock()
    client.modify_order.return_value = _refused(CHILD_ORDER_ID)

    detector = MasterTradeDetector(master_account_id=str(master.id))
    detector._limit_order_state[MASTER_ORDER_ID] = (1, Decimal("22.40"))
    poller = _build_poller([[]], [], mock_redis)
    event = _as_master_event(
        poller, detector, _record(status="OPEN", price=Decimal("22.60")), master.id
    )

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=event, clients={str(follower.id): client})

    assert res["status"] == "completed_with_failures"
    assert res["failed_accounts"] == [str(follower.id)]

    events = (
        await db_session.execute(
            select(SystemEvent).where(SystemEvent.event_type == "CHILD_MODIFICATION_FAILED")
        )
    ).scalars().all()
    assert len(events) == 1


# ── 7, 8, 9, 11: cancellation dispatch, confirmation, failure, partials ───
@pytest.mark.asyncio
async def test_7_child_cancellation_is_actually_submitted(db_session, mock_redis):
    follower = Account(
        id=uuid.uuid4(), account_type=AccountType.FOLLOWER,
        display_name="vikash", alice_user_id="AB_V", is_active=True,
    )
    db_session.add(follower)
    await db_session.commit()
    db_session.add(_child_order(follower.id))
    await db_session.commit()

    client = AsyncMock()
    client.cancel_order.return_value = _ok(CHILD_ORDER_ID, "Cancelled")
    client.get_order_book.return_value = [
        _record(broker_order_id=CHILD_ORDER_ID, status="CANCELLED", filled_quantity=0)
    ]

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.handle_master_cancellation(
        master_order_id=MASTER_ORDER_ID, clients={str(follower.id): client}
    )

    client.cancel_order.assert_awaited_once()
    assert client.cancel_order.call_args.kwargs["broker_order_id"] == CHILD_ORDER_ID
    assert res["status"] == "completed"


@pytest.mark.asyncio
async def test_8_cancellation_persisted_only_after_broker_confirmation(db_session, mock_redis):
    """Broker confirms the cancel but the order book cannot verify it -> the
    Order row must be left untouched rather than optimistically CANCELLED."""
    follower = Account(
        id=uuid.uuid4(), account_type=AccountType.FOLLOWER,
        display_name="vikash", alice_user_id="AB_V2", is_active=True,
    )
    db_session.add(follower)
    await db_session.commit()
    order = _child_order(follower.id)
    db_session.add(order)
    await db_session.commit()

    client = AsyncMock()
    client.cancel_order.return_value = _ok(CHILD_ORDER_ID, "Cancelled")
    client.get_order_book.return_value = []          # order not found -> unverified

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.handle_master_cancellation(
        master_order_id=MASTER_ORDER_ID, clients={str(follower.id): client}
    )

    assert res["cancellations"][str(follower.id)] == "VERIFICATION_FAILED"
    await db_session.refresh(order)
    assert order.status == OrderStatus.OPEN


@pytest.mark.asyncio
async def test_9_failed_child_cancellation_follows_20354b0(db_session, mock_redis):
    """The deployed cancel-failure discipline must remain intact."""
    follower = Account(
        id=uuid.uuid4(), account_type=AccountType.FOLLOWER,
        display_name="vikash", alice_user_id="AB_V3", is_active=True,
    )
    db_session.add(follower)
    await db_session.commit()
    order = _child_order(follower.id)
    db_session.add(order)
    db_session.add(
        CopyTrade(
            id=uuid.uuid4(),
            master_order_id=MASTER_ORDER_ID,
            follower_account_id=follower.id,
            copy_event_id=uuid.uuid4(),
            broker_order_id=CHILD_ORDER_ID,
            symbol="YESBANK",
            exchange="NSE",
            side=OrderSide.BUY,
            quantity=1,
            product_type="INTRADAY",
            status=CopyTradeStatus.SUBMITTED,
        )
    )
    await db_session.commit()

    client = AsyncMock()
    client.cancel_order.return_value = _refused(CHILD_ORDER_ID)

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.handle_master_cancellation(
        master_order_id=MASTER_ORDER_ID, clients={str(follower.id): client}
    )

    assert res["status"] == "completed_with_failures"
    events = (
        await db_session.execute(
            select(SystemEvent).where(SystemEvent.event_type == "CHILD_CANCELLATION_FAILED")
        )
    ).scalars().all()
    assert len(events) == 1

    trade = (
        await db_session.execute(select(CopyTrade).where(CopyTrade.broker_order_id == CHILD_ORDER_ID))
    ).scalar_one()
    assert trade.status == CopyTradeStatus.RECONCILIATION_REQUIRED

    await db_session.refresh(order)
    assert order.status == OrderStatus.OPEN


@pytest.mark.asyncio
async def test_11_cancellation_after_partial_fill_keeps_filled_quantity(db_session, mock_redis):
    """Child filled 3 of 10 -> cancel leaves the 3 intact and only clears the
    remaining 7 (row becomes PARTIAL, never CANCELLED-with-zero)."""
    follower = Account(
        id=uuid.uuid4(), account_type=AccountType.FOLLOWER,
        display_name="vikash", alice_user_id="AB_V4", is_active=True,
    )
    db_session.add(follower)
    await db_session.commit()
    order = _child_order(follower.id, quantity=10, filled_quantity=3)
    db_session.add(order)
    await db_session.commit()

    client = AsyncMock()
    client.cancel_order.return_value = _ok(CHILD_ORDER_ID, "Cancelled")
    client.get_order_book.return_value = [
        _record(broker_order_id=CHILD_ORDER_ID, status="CANCELLED",
                quantity=10, filled_quantity=3)
    ]

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await pipeline.handle_master_cancellation(
        master_order_id=MASTER_ORDER_ID, clients={str(follower.id): client}
    )

    await db_session.refresh(order)
    assert order.filled_quantity == 3, "already-filled child quantity was altered"
    assert order.pending_quantity == 0
    assert order.status == OrderStatus.PARTIAL


# ── 15: exact-price LIMIT semantics are unchanged ─────────────────────────
@pytest.mark.asyncio
async def test_15_master_limit_copies_at_exact_price_no_buffer(db_session, mock_redis, monkeypatch):
    """Business rule 1: the LIMIT path must never apply the MARKET buffer."""
    from app.core.config import settings

    # Pin a non-zero buffer so "buffered" is distinguishable from exact.
    monkeypatch.setattr(settings, "MARKET_ORDER_BUFFER_PERCENT", Decimal("0.5"))
    detector = MasterTradeDetector(master_account_id=MASTER_ID)
    poller = _build_poller([[]], [], mock_redis)
    event = detector.process_order_update(
        MasterOrderPoller._to_event(poller, _record(status="OPEN", price=Decimal("22.40")))
    )

    assert event.event_type == "LIMIT_CREATED"
    assert event.limit_price == Decimal("22.40")
    # Unbuffered: a buffered price would be 22.40 * 1.005 = 22.51.
    buffered = Decimal("22.40") * (1 + settings.MARKET_ORDER_BUFFER_PERCENT / Decimal("100"))
    assert event.limit_price != buffered.quantize(Decimal("0.01"))
