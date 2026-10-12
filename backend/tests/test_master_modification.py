"""
P1.2 regression coverage: master-side LIMIT order price/quantity
modifications must be detected (told apart from a duplicate resend of the
same OPEN update) and propagated to follower orders exactly once via
CopyEnginePipeline.handle_master_modification.

No live trading is enabled and no real broker calls are made anywhere in
this file - all broker clients are AsyncMock test doubles and the DB is the
in-memory test SQLite engine (db_session fixture).
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.models.accounts import Account, AccountType
from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.types import OrderResult, OrderResultCategory, OrderUpdateEvent
from app.services.copy_engine.detector import MasterTradeDetector
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator


def _open_limit_event(broker_order_id="ORD_MOD_1", quantity=100, price=Decimal("2950.00"), account_id="M1"):
    return OrderUpdateEvent(
        broker_order_id=broker_order_id,
        client_order_id=None,
        symbol="RELIANCE",
        side="BUY",
        quantity=quantity,
        filled_quantity=0,
        fill_price=Decimal("0"),
        status="OPEN",
        timestamp=datetime.now(timezone.utc),
        account_id=account_id,
        order_type="LIMIT",
        price=price,
    )


# ---------------------------------------------------------------------------
# Detector-level: modification vs. duplicate detection
# ---------------------------------------------------------------------------

def test_modified_limit_order_quantity_change_is_detected():
    """A second OPEN update for the same broker_order_id with a different
    quantity must be surfaced as ORDER_MODIFIED, not swallowed as a
    duplicate."""
    detector = MasterTradeDetector(master_account_id="M1")

    created = detector.process_order_update(_open_limit_event(quantity=100, price=Decimal("2950.00")))
    assert created is not None
    assert created.event_type == "LIMIT_CREATED"

    modified = detector.process_order_update(_open_limit_event(quantity=150, price=Decimal("2950.00")))
    assert modified is not None
    assert modified.event_type == "ORDER_MODIFIED"
    assert modified.quantity == 150
    assert modified.master_order_id == "ORD_MOD_1"


def test_modified_limit_order_price_change_is_detected():
    detector = MasterTradeDetector(master_account_id="M1")
    detector.process_order_update(_open_limit_event(quantity=100, price=Decimal("2950.00")))

    modified = detector.process_order_update(_open_limit_event(quantity=100, price=Decimal("2999.00")))
    assert modified is not None
    assert modified.event_type == "ORDER_MODIFIED"
    assert modified.limit_price == Decimal("2999.00")
    assert modified.quantity == 100


def test_duplicate_unchanged_open_update_is_still_ignored():
    """The exact same OPEN update (same qty, same price) resent for an
    already-known broker_order_id must still be ignored, not misread as a
    modification."""
    detector = MasterTradeDetector(master_account_id="M1")
    detector.process_order_update(_open_limit_event(quantity=100, price=Decimal("2950.00")))

    duplicate = detector.process_order_update(_open_limit_event(quantity=100, price=Decimal("2950.00")))
    assert duplicate is None


def test_no_modification_event_when_order_has_not_actually_changed():
    """Several identical resends in a row after creation never produce a
    second event of any kind (no false-positive modifications)."""
    detector = MasterTradeDetector(master_account_id="M1")
    first = detector.process_order_update(_open_limit_event())
    assert first is not None

    for _ in range(4):
        assert detector.process_order_update(_open_limit_event()) is None


# ---------------------------------------------------------------------------
# Pipeline-level: modification reaches handle_master_modification exactly once
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_modification_reaches_handle_master_modification_and_is_sent_once(db_session, mock_redis):
    """A detected ORDER_MODIFIED event routed through execute_replication must
    call client.modify_order() exactly once for the open child order, with
    the new price/quantity, and update the persisted Order row."""
    child_acc = Account(
        id=uuid.uuid4(),
        account_type=AccountType.FOLLOWER,
        display_name="Child Vikash",
        alice_user_id="2679228",
        is_active=True,
    )
    db_session.add(child_acc)
    await db_session.commit()

    child_order = Order(
        id=uuid.uuid4(),
        internal_order_id="JT_CH_M_MOD_1_001",
        broker_order_id="BRK_CH_MOD_1",
        account_id=child_acc.id,
        master_broker_order_id="ORD_MOD_1",
        source=OrderSource.SYSTEM,
        exchange="NSE",
        symbol="RELIANCE",
        side=OrderSide.BUY,
        order_type="LIMIT",
        product_type="MIS",
        quantity=100,
        filled_quantity=0,
        pending_quantity=100,
        price=Decimal("2950.00"),
        status=OrderStatus.OPEN,
    )
    db_session.add(child_order)
    await db_session.commit()

    child_client = AsyncMock()
    child_client.modify_order.return_value = OrderResult(
        category=OrderResultCategory.SUCCESS,
        client_order_id=None,
        broker_order_id="BRK_CH_MOD_1",
        message="Modified successfully",
        raw_response={"stat": "Ok"},
        timestamp=datetime.now(timezone.utc),
    )
    clients = {str(child_acc.id): child_client}

    detector = MasterTradeDetector(master_account_id="M1")
    detector.process_order_update(_open_limit_event(quantity=100, price=Decimal("2950.00")))
    modified_event = detector.process_order_update(_open_limit_event(quantity=150, price=Decimal("2999.00")))
    assert modified_event is not None
    assert modified_event.event_type == "ORDER_MODIFIED"

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=modified_event, clients=clients)

    assert res["status"] == "completed"
    assert res["modifications"][str(child_acc.id)] == "MODIFIED"

    child_client.modify_order.assert_awaited_once()
    _, call_kwargs = child_client.modify_order.call_args
    assert call_kwargs["broker_order_id"] == "BRK_CH_MOD_1"
    assert call_kwargs["quantity"] == 150
    assert call_kwargs["price"] == Decimal("2999.00")

    await db_session.refresh(child_order)
    assert child_order.price == Decimal("2999.00")
    assert child_order.pending_quantity == 150


@pytest.mark.asyncio
async def test_duplicate_open_update_never_triggers_a_modification_call(db_session, mock_redis):
    """An unchanged duplicate OPEN never even produces a trade_event in the
    first place (mirrors engine_loop.py, which only calls
    execute_replication when the detector returns non-None) -> the follower
    broker's modify_order() must never be called."""
    child_acc = Account(
        id=uuid.uuid4(),
        account_type=AccountType.FOLLOWER,
        display_name="Child Vikash",
        alice_user_id="2679228",
        is_active=True,
    )
    db_session.add(child_acc)
    await db_session.commit()

    db_session.add(
        Order(
            id=uuid.uuid4(),
            internal_order_id="JT_CH_M_MOD_2_001",
            broker_order_id="BRK_CH_MOD_2",
            account_id=child_acc.id,
            master_broker_order_id="ORD_MOD_1",
            source=OrderSource.SYSTEM,
            exchange="NSE",
            symbol="RELIANCE",
            side=OrderSide.BUY,
            order_type="LIMIT",
            product_type="MIS",
            quantity=100,
            filled_quantity=0,
            pending_quantity=100,
            price=Decimal("2950.00"),
            status=OrderStatus.OPEN,
        )
    )
    await db_session.commit()

    child_client = AsyncMock()

    detector = MasterTradeDetector(master_account_id="M1")
    detector.process_order_update(_open_limit_event(quantity=100, price=Decimal("2950.00")))
    duplicate_event = detector.process_order_update(_open_limit_event(quantity=100, price=Decimal("2950.00")))
    assert duplicate_event is None

    child_client.modify_order.assert_not_called()
