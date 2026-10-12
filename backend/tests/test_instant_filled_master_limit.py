"""
Regression coverage for the instantly-filled Master LIMIT business-rule
violation.

Production evidence (2026-09-09):

    master 26090900055830  LIMIT BUY  22.46  filled 1/1
    child                  LIMIT BUY  22.57          <-- 22.46 * 1.005

The master LIMIT filled inside the 5-second MasterOrderPoller interval, so
the very first order-book observation already showed it COMPLETE. The
detector's OPEN/LIMIT branch - the only place that registers
_limit_order_state and emits LIMIT_CREATED - therefore never ran for it, and
the fill branch typed the event by _limit_order_state MEMBERSHIP alone:

    if order_type == "LIMIT" and broker_order_id in self._limit_order_state:
        ev_type = "ORDER_FILLED"
    else:
        ev_type = "MARKET_ACCEPTED"       # <-- an unregistered LIMIT lands here

so a master LIMIT was replicated down the MARKET path and picked up the
0.5% marketable-LIMIT buffer, violating the rule that the buffer applies
ONLY to master MARKET orders.

The order type was never actually lost - _to_event carries order_type
faithfully. The fix keys the decision off the order type itself: an
unregistered LIMIT is a creation that has never been replicated, so it emits
LIMIT_CREATED at the master's EXACT limit price and registers its state so
later polls are ORDER_FILLED updates rather than a second child order.

No live trading is enabled and no real broker calls are made anywhere in
this file - every broker client is an AsyncMock/MagicMock test double.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.config import settings
from app.models.strategies import GroupStrategy, StrategyMember
from app.services.alice_blue.types import (
    FundsData,
    OrderRecord,
    OrderResult,
    OrderResultCategory,
    OrderSide as BrokerOrderSide,
    OrderType,
)
from app.services.copy_engine.detector import MasterTradeDetector
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.reliability.master_order_poller import MasterOrderPoller
from strategy_helpers import SAMPLE_MASTER_ACCOUNT_ID

# The sample_accounts master, so events from MASTER_ID route to sample_strategy.
MASTER_ID = SAMPLE_MASTER_ACCOUNT_ID
MASTER_ORDER_ID = "26090900055830"


def _record(status="COMPLETE", order_type="LIMIT", side=BrokerOrderSide.BUY,
            price=Decimal("22.46"), quantity=1, filled_quantity=1,
            broker_order_id=MASTER_ORDER_ID, average_price=None):
    return OrderRecord(
        broker_order_id=broker_order_id,
        client_order_id=None,
        symbol="YESBANK",
        exchange="NSE",
        side=side,
        quantity=quantity,
        filled_quantity=filled_quantity,
        average_price=average_price if average_price is not None else price,
        status=status,
        order_timestamp=datetime.now(timezone.utc),
        order_type=order_type,
        product_type="INTRADAY",
        price=price,
    )


def _build_poller(book_holder, dispatched, mock_redis):
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


async def _observe_after_baseline(mock_redis, record, master_id=MASTER_ID):
    """Poll once with an empty book (baseline seed), then once with `record`
    already in its final state - the real instant-fill sequence."""
    book = [[]]
    dispatched = []
    poller = _build_poller(book, dispatched, mock_redis)
    await poller.poll_once()
    book[0] = [record]
    await poller.poll_once()

    detector = MasterTradeDetector(master_account_id=str(master_id))
    events = []
    for upd in dispatched:
        upd.account_id = str(master_id)
        ev = detector.process_order_update(upd)
        if ev is not None:
            events.append(ev)
    return dispatched, events, detector


def _funds():
    return FundsData(
        account_id="f",
        available_margin=Decimal("500000"),
        collateral=Decimal("0"),
        used_margin=Decimal("0"),
        updated_at=datetime.now(timezone.utc),
    )


def _success_result(broker_order_id):
    return OrderResult(
        category=OrderResultCategory.SUCCESS,
        client_order_id=None,
        broker_order_id=broker_order_id,
        message="Success",
        raw_response={},
        timestamp=datetime.now(timezone.utc),
    )


async def _make_strategy(db_session, master, follower):
    strategy = GroupStrategy(
        id=uuid.uuid4(),
        name="Instant Fill Strategy",
        master_account_id=master.id,
        trading_enabled=True,
    )
    db_session.add(strategy)
    db_session.add(
        StrategyMember(
            id=uuid.uuid4(),
            strategy_id=strategy.id,
            account_id=follower.id,
            multiplier=Decimal("1.0"),
            trading_enabled=True,
        )
    )
    await db_session.commit()
    return strategy


# ── 11, 12, 14: instant-filled master LIMIT keeps the exact price ─────────
@pytest.mark.parametrize(
    "side,price",
    [(BrokerOrderSide.BUY, Decimal("22.46")), (BrokerOrderSide.SELL, Decimal("22.45"))],
)
@pytest.mark.asyncio
async def test_11_14_instant_filled_limit_detected_as_limit_created(
    db_session, mock_redis, side, price
):
    """BUY and SELL: a LIMIT first seen COMPLETE is a LIMIT creation, not a
    MARKET acceptance."""
    _dispatched, events, _detector = await _observe_after_baseline(
        mock_redis, _record(status="COMPLETE", order_type="LIMIT", side=side, price=price)
    )

    assert len(events) == 1
    assert events[0].event_type == "LIMIT_CREATED", "instant-filled LIMIT routed as MARKET"
    assert events[0].order_type == "LIMIT"
    assert events[0].limit_price == price


@pytest.mark.parametrize(
    "side,price",
    [(BrokerOrderSide.BUY, Decimal("22.46")), (BrokerOrderSide.SELL, Decimal("22.45"))],
)
@pytest.mark.asyncio
async def test_12_14_instant_filled_limit_never_gets_the_buffer(
    db_session, mock_redis, side, price, monkeypatch
):
    """The reported violation: no MARKET buffer may be applied to a LIMIT."""
    # Pin a non-zero buffer so "buffered" is distinguishable from exact.
    monkeypatch.setattr(settings, "MARKET_ORDER_BUFFER_PERCENT", Decimal("0.5"))
    _dispatched, events, _detector = await _observe_after_baseline(
        mock_redis, _record(status="COMPLETE", order_type="LIMIT", side=side, price=price)
    )

    buffer_fraction = settings.MARKET_ORDER_BUFFER_PERCENT / Decimal("100")
    buffered_buy = (price * (1 + buffer_fraction)).quantize(Decimal("0.01"))
    buffered_sell = (price * (1 - buffer_fraction)).quantize(Decimal("0.01"))

    # Decisive: MARKET_ACCEPTED is what routes an order into the buffered
    # marketable-LIMIT path, so the event type is the real guarantee here.
    assert events[0].event_type == "LIMIT_CREATED"
    assert events[0].event_type != "MARKET_ACCEPTED"
    assert events[0].limit_price == price
    assert events[0].limit_price != buffered_buy
    assert events[0].limit_price != buffered_sell


@pytest.mark.asyncio
async def test_11_child_order_placed_at_exact_master_limit_price(
    db_session, mock_redis, sample_accounts
):
    """End to end: poller -> detector -> pipeline -> child broker order."""
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]
    await _make_strategy(db_session, master, follower)

    _dispatched, events, _detector = await _observe_after_baseline(
        mock_redis,
        _record(status="COMPLETE", order_type="LIMIT", side=BrokerOrderSide.BUY,
                price=Decimal("22.46")),
        master_id=master.id,
    )

    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.place_order.return_value = _success_result("CHILD_1")

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await pipeline.execute_replication(
        event=events[0], clients={str(follower.id): client}
    )

    client.place_order.assert_awaited_once()
    sent = client.place_order.call_args.args[0]
    assert sent.order_type == OrderType.LIMIT
    assert sent.price == Decimal("22.46"), "child did not use the exact master LIMIT price"
    assert sent.price != Decimal("22.57")   # the buffered price seen in production


# ── 13: master MARKET behaviour is untouched ─────────────────────────────
@pytest.mark.asyncio
async def test_13_instant_filled_market_still_uses_marketable_limit(db_session, mock_redis):
    """A COMPLETE MARKET order must still be MARKET_ACCEPTED so the existing
    marketable-LIMIT conversion (and its buffer) keeps applying."""
    _dispatched, events, _detector = await _observe_after_baseline(
        mock_redis,
        _record(status="COMPLETE", order_type="MARKET", side=BrokerOrderSide.BUY,
                price=Decimal("0"), average_price=Decimal("22.47")),
    )

    assert len(events) == 1
    assert events[0].event_type == "MARKET_ACCEPTED"
    assert events[0].order_type == "MARKET"


@pytest.mark.asyncio
async def test_13_market_child_still_gets_the_buffer(db_session, mock_redis, sample_accounts, sample_strategy, monkeypatch):
    # Pin a non-zero buffer so the MARKET routing is distinguishable from LIMIT.
    monkeypatch.setattr(settings, "MARKET_ORDER_BUFFER_PERCENT", Decimal("0.5"))
    follower = sample_accounts["follower1"]
    _dispatched, events, _detector = await _observe_after_baseline(
        mock_redis,
        _record(status="COMPLETE", order_type="MARKET", side=BrokerOrderSide.BUY,
                price=Decimal("0"), average_price=Decimal("1000.00")),
    )

    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.place_order.return_value = _success_result("CHILD_MKT")

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await pipeline.execute_replication(event=events[0], clients={str(follower.id): client})

    sent = client.place_order.call_args.args[0]
    assert sent.order_type == OrderType.LIMIT
    assert sent.price == Decimal("1005.00")     # 1000 * 1.005, unchanged


# ── 15: no duplicate child order ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_15_repeated_polls_do_not_produce_a_second_creation(db_session, mock_redis):
    """After the instant-fill creation is emitted, further observations of the
    same order must not emit a second LIMIT_CREATED."""
    book = [[]]
    dispatched = []
    poller = _build_poller(book, dispatched, mock_redis)
    await poller.poll_once()
    book[0] = [_record(status="COMPLETE", price=Decimal("22.46"))]
    for _ in range(4):
        await poller.poll_once()

    detector = MasterTradeDetector(master_account_id=MASTER_ID)
    events = []
    for upd in dispatched:
        ev = detector.process_order_update(upd)
        if ev is not None:
            events.append(ev)

    creations = [e for e in events if e.event_type == "LIMIT_CREATED"]
    assert len(creations) == 1


@pytest.mark.asyncio
async def test_15b_later_fill_of_instant_limit_is_a_fill_update(db_session, mock_redis):
    """A LIMIT first seen PARTIAL then COMPLETE: one creation, then a fill
    update - never a second child order."""
    book = [[]]
    dispatched = []
    poller = _build_poller(book, dispatched, mock_redis)
    await poller.poll_once()
    book[0] = [_record(status="PARTIAL", quantity=10, filled_quantity=4, price=Decimal("22.46"))]
    await poller.poll_once()
    book[0] = [_record(status="COMPLETE", quantity=10, filled_quantity=10, price=Decimal("22.46"))]
    await poller.poll_once()

    detector = MasterTradeDetector(master_account_id=MASTER_ID)
    events = [e for e in (detector.process_order_update(u) for u in dispatched) if e]

    assert [e.event_type for e in events] == ["LIMIT_CREATED", "ORDER_FILLED"]
    assert events[0].limit_price == Decimal("22.46")
    assert events[0].quantity == 10          # mirrors the master ORDER, not the fill


# ── 16, 17: the pending-LIMIT paths are unchanged ────────────────────────
@pytest.mark.asyncio
async def test_16_pending_limit_creation_unchanged(db_session, mock_redis):
    _dispatched, events, _detector = await _observe_after_baseline(
        mock_redis, _record(status="OPEN", filled_quantity=0, price=Decimal("22.40"))
    )
    assert events[0].event_type == "LIMIT_CREATED"
    assert events[0].limit_price == Decimal("22.40")


@pytest.mark.asyncio
async def test_17_pending_limit_modification_unchanged(db_session, mock_redis):
    book = [[]]
    dispatched = []
    poller = _build_poller(book, dispatched, mock_redis)
    await poller.poll_once()
    book[0] = [_record(status="OPEN", filled_quantity=0, price=Decimal("22.40"))]
    await poller.poll_once()
    book[0] = [_record(status="OPEN", filled_quantity=0, price=Decimal("22.60"))]
    await poller.poll_once()

    detector = MasterTradeDetector(master_account_id=MASTER_ID)
    events = [e for e in (detector.process_order_update(u) for u in dispatched) if e]

    assert [e.event_type for e in events] == ["LIMIT_CREATED", "ORDER_MODIFIED"]
    assert events[1].limit_price == Decimal("22.60")
