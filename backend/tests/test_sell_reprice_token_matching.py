"""
Regression coverage for the confirmed NFO SELL reprice token-matching gap.

Root cause: handle_master_modification() (the LIMIT->MARKET marketable-reprice
and missed-intermediate-state stranded-LIMIT reprice paths) called
get_available_long() without provided_token, unlike the placement-time
_enforce_sell_position_safety gate, which already passed
provided_token=event.instrument_id. Without a token, an NFO SELL reprice
fell back to symbol-only resolution of the CHILD ORDER's stored symbol -
which mirrors the master order book's own (sometimes ambiguous, e.g. a bare
"NIFTY") spelling for an option/future - and could fail to find a real
covering position whose position-book symbol spelling differs (a confirmed
production pattern: order book "NIFTY" vs position book
"NIFTY29SEP26C23900", same contract, same instrumentId).

Fix: handle_master_modification() now accepts instrument_token, threaded
from event.instrument_id at both reprice call sites in execute_replication,
and forwards it to get_available_long(provided_token=instrument_token).

No live trading and no real broker calls are made anywhere in this file -
all broker clients are AsyncMock test doubles and the DB is the in-memory
test SQLite engine (db_session fixture).
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from app.models.accounts import Account, AccountType
from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.types import FundsData, OrderResult, OrderResultCategory, OrderType, OrderUpdateEvent, Position
from app.services.copy_engine.detector import MasterTradeDetector
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator

MASTER_ID = "M_NFO_REPRICE"


def _nfo_limit_open_event(instrument_id=None):
    return OrderUpdateEvent(
        broker_order_id="ORD_NFO_1", client_order_id=None, symbol="NIFTY", side="SELL",
        quantity=65, filled_quantity=0, fill_price=Decimal("0"), status="OPEN",
        timestamp=datetime.now(timezone.utc), account_id=MASTER_ID, order_type="LIMIT",
        price=Decimal("100.00"), exchange="NFO", instrument_id=instrument_id,
    )


def _nfo_market_complete_event(instrument_id=None, fill_price=Decimal("101.00")):
    """Order converted to MARKET and filled - the delivery trigger for the
    marketable-LIMIT reprice path under test."""
    return OrderUpdateEvent(
        broker_order_id="ORD_NFO_1", client_order_id=None, symbol="NIFTY", side="SELL",
        quantity=65, filled_quantity=65, fill_price=fill_price, status="COMPLETE",
        timestamp=datetime.now(timezone.utc), account_id=MASTER_ID, order_type="MARKET",
        price=Decimal("0"), exchange="NFO", instrument_id=instrument_id,
    )


async def _make_nfo_sell_child(db_session, broker_order_id="BRK_NFO_CH_1"):
    child_acc = Account(
        id=uuid.uuid4(), account_type=AccountType.FOLLOWER, display_name="NFO Follower",
        alice_user_id=f"AB_{uuid.uuid4().hex[:8]}", is_active=True,
    )
    db_session.add(child_acc)
    await db_session.commit()

    child_order = Order(
        id=uuid.uuid4(),
        internal_order_id=f"JT_CH_{uuid.uuid4().hex[:12]}",
        broker_order_id=broker_order_id,
        account_id=child_acc.id,
        master_broker_order_id="ORD_NFO_1",
        source=OrderSource.SYSTEM,
        exchange="NFO",
        symbol="NIFTY",  # the master order book's bare, ambiguous spelling
        side=OrderSide.SELL,
        order_type="LIMIT",
        product_type="MIS",
        quantity=65,
        filled_quantity=0,
        pending_quantity=65,
        price=Decimal("100.00"),
        status=OrderStatus.OPEN,
    )
    db_session.add(child_order)
    await db_session.commit()
    return child_acc, child_order


def _reprice_client(broker_order_id, positions, redis):
    client = AsyncMock()
    client.redis = redis
    client.get_funds.return_value = FundsData(
        account_id="f", available_margin=Decimal("500000"), collateral=Decimal("0"),
        used_margin=Decimal("0"), updated_at=datetime.now(timezone.utc),
    )
    client.get_positions.return_value = positions
    client.get_positions_or_none.return_value = positions
    client.modify_order.return_value = OrderResult(
        category=OrderResultCategory.SUCCESS, client_order_id=None, broker_order_id=broker_order_id,
        message="Modified successfully", raw_response={"stat": "Ok"}, timestamp=datetime.now(timezone.utc),
    )
    return client


@pytest.mark.asyncio
async def test_nfo_sell_reprice_uses_token_primary_matching_despite_symbol_mismatch(db_session, mock_redis):
    """The money test: the follower's real covering position is reported by
    the broker under a DIFFERENT symbol spelling than the child order's own
    stored symbol ("NIFTY" vs "NIFTY29SEP26C23900" - the confirmed
    production order-book/position-book spelling mismatch for the SAME
    contract). Only instrument_id ("74056") ties them together. This can
    only succeed if event.instrument_id actually reached
    get_available_long() as provided_token - proves the full wiring:
    execute_replication -> handle_master_modification -> get_available_long.
    """
    from app.core.config import settings as app_settings

    child_acc, child_order = await _make_nfo_sell_child(db_session)
    client = _reprice_client(
        "BRK_NFO_CH_1", redis=mock_redis,
        positions=[Position(
            symbol="NIFTY29SEP26C23900", quantity=65, side="BUY", average_price=Decimal("95.00"),
            account_id="a", exchange="NFO", product_type="MIS", instrument_id="74056",
        )],
    )

    original_buffer = app_settings.MARKET_ORDER_BUFFER_PERCENT
    app_settings.MARKET_ORDER_BUFFER_PERCENT = Decimal("1.0")
    try:
        detector = MasterTradeDetector(master_account_id=MASTER_ID)
        detector.process_order_update(_nfo_limit_open_event(instrument_id="74056"))
        fill_event = detector.process_order_update(
            _nfo_market_complete_event(instrument_id="74056", fill_price=Decimal("101.00"))
        )
        assert fill_event.instrument_id == "74056"  # sanity: detector threads it through

        pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
        res = await pipeline.execute_replication(event=fill_event, clients={str(child_acc.id): client})
    finally:
        app_settings.MARKET_ORDER_BUFFER_PERCENT = original_buffer

    assert res["status"] == "completed"
    client.modify_order.assert_awaited_once()
    _, kwargs = client.modify_order.call_args
    assert kwargs["broker_order_id"] == "BRK_NFO_CH_1"
    assert kwargs["price"] == Decimal("99.99")  # 101 * 0.99, SELL sells down

    await db_session.refresh(child_order)
    assert child_order.order_type == "LIMIT"


@pytest.mark.asyncio
async def test_handle_master_modification_forwards_provided_token_to_get_available_long(db_session, mock_redis):
    """Direct, isolated proof that get_available_long() receives
    provided_token=instrument_token - independent of position-matching
    semantics, by spying on the call."""
    child_acc, child_order = await _make_nfo_sell_child(db_session, broker_order_id="BRK_SPY_1")
    client = _reprice_client("BRK_SPY_1", redis=mock_redis, positions=[])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))

    with patch(
        "app.services.copy_engine.pipeline.get_available_long",
        AsyncMock(return_value=100),
    ) as spy:
        await pipeline.handle_master_modification(
            master_order_id="ORD_NFO_1",
            new_price=Decimal("99.99"),
            new_quantity=65,
            clients={str(child_acc.id): client},
            master_account_id=MASTER_ID,
            new_order_type="LIMIT",
            enforce_sell_position_safety=True,
            instrument_token="74056",
        )

    spy.assert_awaited_once()
    _, kwargs = spy.call_args
    assert kwargs["provided_token"] == "74056"
    assert kwargs["exchange"] == "NFO"
    assert kwargs["symbol"] == "NIFTY"
    assert kwargs["product_type"] == "MIS"


@pytest.mark.asyncio
async def test_equity_sell_reprice_unaffected_by_token_threading(db_session, mock_redis):
    """Backward compatibility: an equity master event (no instrument_id, the
    overwhelming majority of existing traffic) reprices exactly as before -
    instrument_token=None flows through to provided_token=None, and
    get_available_long falls back to its original symbol-only matching."""
    from app.core.config import settings as app_settings

    child_acc, child_order = await _make_nfo_sell_child(db_session, broker_order_id="BRK_EQ_1")
    # Reuse the NFO helper's shape but as an equity, no instrument_id anywhere.
    child_order.exchange = "NSE"
    child_order.symbol = "RELIANCE-EQ"
    await db_session.commit()

    client = _reprice_client(
        "BRK_EQ_1", redis=mock_redis,
        positions=[Position(
            symbol="RELIANCE-EQ", quantity=100, side="BUY", average_price=Decimal("2950.00"),
            account_id="a", exchange="NSE", product_type="MIS",
        )],
    )

    original_buffer = app_settings.MARKET_ORDER_BUFFER_PERCENT
    app_settings.MARKET_ORDER_BUFFER_PERCENT = Decimal("1.0")
    try:
        detector = MasterTradeDetector(master_account_id=MASTER_ID)
        limit_event = OrderUpdateEvent(
            broker_order_id="ORD_NFO_1", client_order_id=None, symbol="RELIANCE-EQ", side="SELL",
            quantity=100, filled_quantity=0, fill_price=Decimal("0"), status="OPEN",
            timestamp=datetime.now(timezone.utc), account_id=MASTER_ID, order_type="LIMIT",
            price=Decimal("2950.00"), exchange="NSE",
        )
        detector.process_order_update(limit_event)
        fill_event = detector.process_order_update(OrderUpdateEvent(
            broker_order_id="ORD_NFO_1", client_order_id=None, symbol="RELIANCE-EQ", side="SELL",
            quantity=100, filled_quantity=100, fill_price=Decimal("3000.00"), status="COMPLETE",
            timestamp=datetime.now(timezone.utc), account_id=MASTER_ID, order_type="MARKET",
            price=Decimal("0"), exchange="NSE",
        ))
        assert fill_event.instrument_id is None  # sanity: no token on this event

        pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
        res = await pipeline.execute_replication(event=fill_event, clients={str(child_acc.id): client})
    finally:
        app_settings.MARKET_ORDER_BUFFER_PERCENT = original_buffer

    assert res["status"] == "completed"
    client.modify_order.assert_awaited_once()
    _, kwargs = client.modify_order.call_args
    assert kwargs["price"] == Decimal("2970.00")  # 3000 * 0.99, SELL sells down
