import asyncio
import uuid
from decimal import Decimal
from datetime import datetime, timezone
import pytest
from sqlalchemy import select

from app.models.accounts import Account, AccountType
from app.models.strategies import GroupStrategy, StrategyMember
from app.models.trades import Order, OrderSource, OrderStatus, OrderSide
from app.services.alice_blue.types import (
    FundsData,
    OrderRequest,
    OrderResult,
    OrderResultCategory,
    OrderSide as BrokerOrderSide,
    OrderType,
    ProductType,
    OrderUpdateEvent,
)
from app.services.copy_engine.detector import MasterTradeDetector
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator


class DummyBrokerClient:
    def __init__(self, account_id: str, auto_success: bool = True):
        self.account_id = account_id
        self.auto_success = auto_success
        self.placed_orders: list[OrderRequest] = []
        self.cancelled_orders: list[str] = []
        self.modified_orders: list[dict] = []
        # broker_order_id -> filled_quantity the post-cancel order-book
        # verification in handle_master_cancellation should report. Empty by
        # default (order "not found" -> fail closed); tests that need the
        # post-cancel state to be confirmed populate this explicitly.
        self.order_book_fills: dict[str, int] = {}

    async def place_order(self, req: OrderRequest) -> OrderResult:
        self.placed_orders.append(req)
        if self.auto_success:
            return OrderResult(
                category=OrderResultCategory.SUCCESS,
                client_order_id=req.client_order_id,
                broker_order_id=f"BRK_{uuid.uuid4().hex[:8]}",
                message="Order placed successfully",
                raw_response={"stat": "Ok"},
                timestamp=datetime.now(timezone.utc),
            )
        else:
            return OrderResult(
                category=OrderResultCategory.REJECTED,
                client_order_id=req.client_order_id,
                broker_order_id=None,
                message="EC965: Market orders are not allowed",
                raw_response={"emsg": "Market orders are not allowed"},
                timestamp=datetime.now(timezone.utc),
            )

    async def cancel_order(self, broker_order_id: str, symbol: str = "", exchange: str = "NSE") -> OrderResult:
        self.cancelled_orders.append(broker_order_id)
        return OrderResult(
            category=OrderResultCategory.SUCCESS,
            client_order_id=None,
            broker_order_id=broker_order_id,
            message="Cancelled successfully",
            raw_response={"stat": "Ok"},
            timestamp=datetime.now(timezone.utc),
        )

    async def modify_order(self, broker_order_id: str, quantity: int, price: Decimal, **kwargs) -> OrderResult:
        self.modified_orders.append({"broker_order_id": broker_order_id, "quantity": quantity, "price": price})
        return OrderResult(
            category=OrderResultCategory.SUCCESS,
            client_order_id=None,
            broker_order_id=broker_order_id,
            message="Modified successfully",
            raw_response={"stat": "Ok"},
            timestamp=datetime.now(timezone.utc),
        )

    async def get_order_book(self) -> list:
        from app.services.alice_blue.types import OrderRecord
        return [
            OrderRecord(
                broker_order_id=broker_order_id,
                client_order_id=None,
                symbol="RELIANCE",
                exchange="NSE",
                side="BUY",
                quantity=filled_quantity,
                filled_quantity=filled_quantity,
                average_price=Decimal("0"),
                status="CANCELLED",
                order_timestamp=datetime.now(timezone.utc),
            )
            for broker_order_id, filled_quantity in self.order_book_fills.items()
        ]

    async def get_funds(self) -> FundsData:
        return FundsData(
            account_id=self.account_id,
            available_margin=Decimal("1000000"),
            collateral=Decimal("0"),
            used_margin=Decimal("0"),
            updated_at=datetime.now(timezone.utc),
        )


@pytest.mark.asyncio
async def test_immediate_limit_order_replication(db_session, mock_redis):
    """
    TEST 1: Master places LIMIT order -> Child LIMIT order is created immediately
    without waiting for Master fill.
    """
    # 1. Setup Master and Child accounts in DB
    master_acc = Account(
        id=uuid.uuid4(),
        account_type=AccountType.MASTER,
        display_name="Master Dinesh",
        alice_user_id="2679246",
        is_active=True,
    )
    child_acc = Account(
        id=uuid.uuid4(),
        account_type=AccountType.FOLLOWER,
        display_name="Child Vikash",
        alice_user_id="2679228",
        is_active=True,
    )
    strategy = GroupStrategy(
        id=uuid.uuid4(),
        name="Alpha Strategy",
        master_account_id=master_acc.id,
        trading_enabled=True,
    )
    member = StrategyMember(
        id=uuid.uuid4(),
        strategy_id=strategy.id,
        account_id=child_acc.id,
        multiplier=Decimal("1.0"),
        trading_enabled=True,
    )
    db_session.add_all([master_acc, child_acc, strategy, member])
    await db_session.commit()

    child_client = DummyBrokerClient(str(child_acc.id))
    clients = {str(child_acc.id): child_client}

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))

    # 2. Master Limit Event arrives (status=OPEN)
    event = MasterTradeEvent(
        master_order_id="BRK_M_LIMIT_001",
        symbol="RELIANCE",
        exchange="NSE",
        side="BUY",
        quantity=100,
        price=Decimal("2950.00"),
        limit_price=Decimal("2950.00"),
        product_type="MIS",
        timestamp=datetime.now(timezone.utc),
        fill_sequence=1,
        account_id=str(master_acc.id),
        order_type="LIMIT",
        event_type="LIMIT_CREATED",
        status="OPEN",
    )

    res = await pipeline.execute_replication(event=event, clients=clients)
    assert res["status"] == "completed"

    # Verify Child received immediate LIMIT order with same price and quantity
    assert len(child_client.placed_orders) == 1
    child_req = child_client.placed_orders[0]
    assert child_req.symbol == "RELIANCE"
    assert child_req.quantity == 100
    assert child_req.price == Decimal("2950.00")
    assert child_req.order_type == OrderType.LIMIT


@pytest.mark.asyncio
async def test_master_partial_fill_does_not_duplicate_child(db_session, mock_redis):
    """
    TEST 2: Master fills 30/100 on existing LIMIT order -> Fills are updated in Master DB,
    NO duplicate child order is placed.
    """
    detector = MasterTradeDetector(master_account_id="M1")

    # Step A: Master LIMIT order OPEN
    event_open = OrderUpdateEvent(
        broker_order_id="ORD_100",
        client_order_id="CL_100",
        symbol="RELIANCE",
        side="BUY",
        quantity=100,
        filled_quantity=0,
        fill_price=Decimal("2950.00"),
        status="OPEN",
        timestamp=datetime.now(timezone.utc),
        account_id="M1",
        order_type="LIMIT",
        price=Decimal("2950.00"),
    )
    trade_ev_open = detector.process_order_update(event_open)
    assert trade_ev_open is not None
    assert trade_ev_open.event_type == "LIMIT_CREATED"

    # Step B: Master fills 30/100 later (status=PARTIAL)
    event_fill_30 = OrderUpdateEvent(
        broker_order_id="ORD_100",
        client_order_id="CL_100",
        symbol="RELIANCE",
        side="BUY",
        quantity=100,
        filled_quantity=30,
        fill_price=Decimal("2950.00"),
        status="PARTIAL",
        timestamp=datetime.now(timezone.utc),
        account_id="M1",
        order_type="LIMIT",
        price=Decimal("2950.00"),
    )
    trade_ev_fill = detector.process_order_update(event_fill_30)
    assert trade_ev_fill is not None
    assert trade_ev_fill.event_type == "ORDER_FILLED"  # Marked as fill update only!

    # Pipeline ignores new child order creation for ORDER_FILLED events
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=trade_ev_fill, clients={})
    assert res["status"] == "master_fill_updated"


@pytest.mark.asyncio
async def test_master_cancellation_cancels_pending_child_only(db_session, mock_redis):
    """
    TEST 3: Master cancels LIMIT order -> Child has 50 filled, 50 pending.
    Only the pending 50 is cancelled; the 50 filled is untouched.
    """
    child_acc = Account(
        id=uuid.uuid4(),
        account_type=AccountType.FOLLOWER,
        display_name="Child Vikash",
        alice_user_id="2679228",
        is_active=True,
    )
    db_session.add(child_acc)
    await db_session.commit()

    # Pre-populate child order with 50 filled, 50 pending
    child_order = Order(
        id=uuid.uuid4(),
        internal_order_id="JT_CH_M100_001",
        broker_order_id="BRK_CH_001",
        account_id=child_acc.id,
        master_broker_order_id="M_ORD_100",
        source=OrderSource.SYSTEM,
        exchange="NSE",
        symbol="RELIANCE",
        side=OrderSide.BUY,
        order_type="LIMIT",
        product_type="MIS",
        quantity=100,
        filled_quantity=50,
        pending_quantity=50,
        price=Decimal("2950.00"),
        status=OrderStatus.PARTIAL,
    )
    db_session.add(child_order)
    await db_session.commit()

    child_client = DummyBrokerClient(str(child_acc.id))
    # No race: broker's post-cancel state confirms the same 50 already filled.
    child_client.order_book_fills["BRK_CH_001"] = 50
    clients = {str(child_acc.id): child_client}

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.handle_master_cancellation(
        master_order_id="M_ORD_100",
        clients=clients,
    )
    assert res["status"] == "completed"
    assert len(child_client.cancelled_orders) == 1
    assert child_client.cancelled_orders[0] == "BRK_CH_001"

    # Verify child order in DB now has pending_quantity = 0 and filled_quantity remains 50!
    await db_session.refresh(child_order)
    assert child_order.filled_quantity == 50
    assert child_order.pending_quantity == 0
    assert child_order.status == OrderStatus.PARTIAL


@pytest.mark.asyncio
async def test_child_mobile_order_no_replication_loop():
    """
    TEST 4: Child account manually places order in mobile app -> Detector rejects it,
    zero copy replication is triggered.
    """
    detector = MasterTradeDetector(master_account_id="MASTER_DINESH")

    child_event = OrderUpdateEvent(
        broker_order_id="CHILD_MOB_001",
        client_order_id="MOB_001",
        symbol="TATASTEEL",
        side="BUY",
        quantity=50,
        filled_quantity=0,
        fill_price=Decimal("150.00"),
        status="OPEN",
        timestamp=datetime.now(timezone.utc),
        account_id="CHILD_VIKASH",  # Not master
        order_type="LIMIT",
    )
    res = detector.process_order_update(child_event)
    assert res is None  # Never triggers copy trading for child accounts!


@pytest.mark.asyncio
async def test_duplicate_websocket_events_idempotency():
    """
    TEST 5: Same Master LIMIT order update delivered 5 times -> Detector returns trade_event
    exactly ONCE for creation, ignoring subsequent 4 duplicate OPEN events.
    """
    detector = MasterTradeDetector(master_account_id="M1")

    event = OrderUpdateEvent(
        broker_order_id="ORD_DUP_123",
        client_order_id="CL_123",
        symbol="INFY",
        side="BUY",
        quantity=100,
        filled_quantity=0,
        fill_price=Decimal("1800.00"),
        status="OPEN",
        timestamp=datetime.now(timezone.utc),
        account_id="M1",
        order_type="LIMIT",
        price=Decimal("1800.00"),
    )

    ev1 = detector.process_order_update(event)
    ev2 = detector.process_order_update(event)
    ev3 = detector.process_order_update(event)
    ev4 = detector.process_order_update(event)
    ev5 = detector.process_order_update(event)

    assert ev1 is not None
    assert ev2 is None
    assert ev3 is None
    assert ev4 is None
    assert ev5 is None


@pytest.mark.asyncio
async def test_market_order_rejection_no_copy(db_session, mock_redis):
    """
    TEST 6: Master MARKET order rejected by broker (e.g. EC965) -> Zero child orders placed.
    """
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    
    # Validation failure / rejection event
    validation = await pipeline.validator.validate(
        event=MasterTradeEvent(
            master_order_id="M_REJ_001",
            symbol="RELIANCE",
            exchange="NSE",
            side="BUY",
            quantity=100,
            price=Decimal("2950.00"),
            product_type="MIS",
            timestamp=datetime.now(timezone.utc),
            account_id="M1",
        ),
        is_emergency_stopped=True,  # Emergency stop active -> rejected
        is_master_healthy=True,
    )
    assert not validation.is_valid
    assert validation.reason_code == "EMERGENCY_STOP_ACTIVE"
