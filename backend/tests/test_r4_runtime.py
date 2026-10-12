"""R4 tests: emergency-stop unification, TIMEOUT reconciliation, risk inputs,
order-book back-fill mapping, notifier dispatch."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.accounts import Account, AccountType, CopySetting
from app.models.audit import EventSeverity, SystemEvent
from app.models.trades import CopyTrade, CopyTradeStatus, OrderSide
from app.services.alice_blue.types import OrderRecord, OrderResult, OrderResultCategory
from app.services.alice_blue.websocket import AliceBlueWebSocketClient
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.risk import RiskEngine
from app.services.copy_engine.risk_inputs import get_daily_loss_today, get_open_exposure
from app.services.copy_engine.validator import EventValidator
from app.services.monitoring.notifier import emit_system_event
from app.services.reliability.stale_order_sweeper import StaleOrderSweeper
from strategy_helpers import SAMPLE_MASTER_ACCOUNT_ID


def _event(master_order_id="ORD_R4", fill_sequence=1):
    return MasterTradeEvent(
        account_id=SAMPLE_MASTER_ACCOUNT_ID,
        master_order_id=master_order_id,
        symbol="NIFTY",
        exchange="NFO",
        side="BUY",
        quantity=25,
        price=Decimal("100.00"),
        product_type="MIS",
        timestamp=datetime.now(timezone.utc),
        fill_sequence=fill_sequence,
    )


def _funds():
    from app.services.alice_blue.types import FundsData

    return FundsData(
        account_id="a",
        available_margin=Decimal("500000"),
        collateral=Decimal("0"),
        used_margin=Decimal("0"),
        updated_at=datetime.now(timezone.utc),
    )


def _ok_order_result():
    return OrderResult(
        category=OrderResultCategory.SUCCESS,
        client_order_id="cli",
        broker_order_id="BRK_OK",
        message="ok",
        raw_response={"status": "success"},
        timestamp=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# B: Emergency stop provider gating
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_global_stop_provider_blocks_all_followers(db_session, sample_accounts):
    setting = CopySetting(
        follower_account_id=sample_accounts["follower1"].id,
        copy_enabled=True,
        quantity_mode="FIXED",
        fixed_qty=25,
    )
    db_session.add(setting)
    await db_session.commit()

    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.place_order.return_value = _ok_order_result()

    async def stop_provider():
        return True, set()

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=AsyncMock()), stop_provider=stop_provider)
    res = await pipeline.execute_replication(
        event=_event(),
        clients={str(sample_accounts["follower1"].id): client},
    )

    fid = str(sample_accounts["follower1"].id)
    assert res["status"] == "rejected"
    assert res["reason"] == "EMERGENCY_STOP_ACTIVE"
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_follower_specific_stop_blocks_only_that_follower(db_session, sample_accounts, sample_strategy):
    setting = CopySetting(
        follower_account_id=sample_accounts["follower1"].id,
        copy_enabled=True,
        quantity_mode="FIXED",
        fixed_qty=25,
    )
    db_session.add(setting)
    await db_session.commit()

    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.get_positions.return_value = []
    client.place_order.return_value = _ok_order_result()

    fid = str(sample_accounts["follower1"].id)

    async def stop_provider():
        return False, {fid}

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=AsyncMock()), stop_provider=stop_provider)
    res = await pipeline.execute_replication(event=_event(), clients={fid: client})

    assert res["results"][fid]["status"] == "REJECTED"
    assert "FOLLOWER_EMERGENCY_STOP" in res["results"][fid]["reasons"]
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_broken_stop_provider_fails_closed(db_session, sample_accounts):
    setting = CopySetting(
        follower_account_id=sample_accounts["follower1"].id,
        copy_enabled=True,
        quantity_mode="FIXED",
        fixed_qty=25,
    )
    db_session.add(setting)
    await db_session.commit()

    client = AsyncMock()
    client.get_funds.return_value = _funds()

    async def broken_provider():
        raise RuntimeError("redis down")

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=AsyncMock()), stop_provider=broken_provider)
    res = await pipeline.execute_replication(
        event=_event(), clients={str(sample_accounts["follower1"].id): client}
    )
    assert res["status"] == "rejected"
    assert res["reason"] == "EMERGENCY_STOP_ACTIVE"


# ---------------------------------------------------------------------------
# E: Risk inputs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_daily_loss_computed_from_today_success_trades(db_session, sample_accounts):
    fid = sample_accounts["follower1"].id
    for i in range(2):
        db_session.add(
            CopyTrade(
                id=uuid.uuid4(),
                master_order_id=f"ORD_DL_{i}",
                follower_account_id=fid,
                copy_event_id=uuid.uuid4(),
                symbol="NIFTY",
                exchange="NFO",
                side=OrderSide.BUY,
                quantity=50,
                product_type="MIS",
                estimated_price=Decimal("100.00"),
                status=CopyTradeStatus.SUCCESS,
                completed_at=datetime.now(timezone.utc),
            )
        )
    await db_session.commit()

    loss = await get_daily_loss_today(fid, db=db_session)
    assert loss == Decimal("10000")


def test_risk_engine_daily_loss_and_exposure_checks_fire():
    acc = Account(
        id=uuid.uuid4(),
        account_type=AccountType.FOLLOWER,
        display_name="F",
        alice_user_id="AB_R4",
    )
    settings_obj = CopySetting(
        follower_account_id=acc.id,
        copy_enabled=True,
        max_daily_loss=Decimal("5000"),
        max_exposure=Decimal("20000"),
    )
    res = RiskEngine.evaluate(
        account=acc,
        settings=settings_obj,
        symbol="NIFTY",
        quantity=10,
        price=Decimal("100"),
        funds=_funds(),
        daily_loss_today=Decimal("5000"),
        open_exposure=Decimal("19500"),
    )
    assert "MAX_DAILY_LOSS_BREACHED" in res.failures
    assert "MAX_EXPOSURE_BREACHED" in res.failures


@pytest.mark.asyncio
async def test_open_exposure_from_positions_with_cache():
    client = AsyncMock()

    from app.services.alice_blue.types import Position

    client.get_positions.return_value = [
        Position(symbol="NIFTY", quantity=50, side="BUY", average_price=Decimal("100"),
                 account_id="a", exchange="NSE", product_type="MIS"),
        Position(symbol="BANKNIFTY", quantity=-20, side="SELL", average_price=Decimal("250"),
                 account_id="a", exchange="NSE", product_type="MIS"),
    ]
    redis = AsyncMock()
    redis.get.return_value = None

    exposure = await get_open_exposure(client, redis=redis)
    assert exposure == Decimal("5000") + Decimal("5000")
    redis.set.assert_awaited_once()


# ---------------------------------------------------------------------------
# Sweeper TIMEOUT resolution
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sweeper_resolves_timeout_from_order_book(db_session, sample_accounts):
    trade = CopyTrade(
        id=uuid.uuid4(),
        master_order_id="ORD_TO_1",
        follower_account_id=sample_accounts["follower1"].id,
        copy_event_id=uuid.uuid4(),
        symbol="NIFTY",
        exchange="NFO",
        side=OrderSide.BUY,
        quantity=25,
        product_type="MIS",
        client_order_id="JT__ORD_TO_1__abc__1",
        status=CopyTradeStatus.TIMEOUT,
        submitted_at=datetime.now(timezone.utc) - timedelta(seconds=120),
    )
    db_session.add(trade)
    await db_session.commit()

    book = [
        OrderRecord(
            broker_order_id="BRK_FOUND_1",
            client_order_id="JT__ORD_TO_1__abc__1",
            symbol="NIFTY",
            exchange="NFO",
            side="BUY",
            quantity=25,
            filled_quantity=25,
            average_price=Decimal("100"),
            status="COMPLETE",
            order_timestamp=datetime.now(timezone.utc),
        )
    ]

    async def provider(account_id):
        return book

    sweeper = StaleOrderSweeper(order_book_provider=provider)
    resolved = await sweeper._resolve_timeouts(db_session)

    await db_session.refresh(trade)
    assert resolved >= 1
    assert trade.status == CopyTradeStatus.SUCCESS
    assert trade.broker_order_id == "BRK_FOUND_1"
    assert trade.failure_reason is None


@pytest.mark.asyncio
async def test_sweeper_flags_unresolved_timeout_critical(db_session, sample_accounts):
    trade = CopyTrade(
        id=uuid.uuid4(),
        master_order_id="ORD_TO_2",
        follower_account_id=sample_accounts["follower1"].id,
        copy_event_id=uuid.uuid4(),
        symbol="NIFTY",
        exchange="NFO",
        side=OrderSide.BUY,
        quantity=25,
        product_type="MIS",
        client_order_id="JT__ORD_TO_2__abc__1",
        status=CopyTradeStatus.TIMEOUT,
        submitted_at=datetime.now(timezone.utc) - timedelta(seconds=120),
    )
    db_session.add(trade)
    await db_session.commit()

    async def provider(account_id):
        return []  # order not at broker

    sweeper = StaleOrderSweeper(order_book_provider=provider)
    await sweeper._resolve_timeouts(db_session)

    stmt = select(SystemEvent).where(SystemEvent.event_type == "TIMEOUT_UNRESOLVED")
    event = (await db_session.execute(stmt)).scalar_one_or_none()
    assert event is not None
    assert event.severity == EventSeverity.CRITICAL


# ---------------------------------------------------------------------------
# D: WS back-fill mapper
# ---------------------------------------------------------------------------

def test_orderbook_record_maps_to_event():
    record = OrderRecord(
        broker_order_id="BRK_9",
        client_order_id="JT__X__y__1",
        symbol="NIFTY",
        exchange="NFO",
        side="BUY",
        quantity=50,
        filled_quantity=50,
        average_price=Decimal("123.45"),
        status="COMPLETE",
        order_timestamp=datetime.now(timezone.utc),
    )
    event = AliceBlueWebSocketClient._orderbook_to_event(record)
    assert event is not None
    assert event.status == "COMPLETE"
    assert event.filled_quantity == 50
    assert event.fill_price == Decimal("123.45")


def test_orderbook_open_status_maps_to_event_for_detector_filtering():
    record = OrderRecord(
        broker_order_id="BRK_10",
        client_order_id=None,
        symbol="X",
        exchange="NSE",
        side="BUY",
        quantity=1,
        filled_quantity=0,
        average_price=Decimal("0"),
        status="OPEN",
        order_timestamp=None,
    )
    event = AliceBlueWebSocketClient._orderbook_to_event(record)
    assert event is not None
    assert event.status == "OPEN"


# ---------------------------------------------------------------------------
# F: Notifier
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notifier_persists_warning_event(db_session, monkeypatch):
    dispatched = []

    class FakeService:
        async def send_alert(self, title, message, level="INFO", details=None):
            dispatched.append((title, level))
            return True

    from app.services.monitoring import notifier as notifier_mod

    monkeypatch.setattr(notifier_mod, "get_alert_service", lambda: FakeService())

    await emit_system_event(
        db_session,
        event_type="TEST_WARN",
        severity="WARNING",
        message="test warning",
        metadata_payload={"k": "v"},
    )

    stmt = select(SystemEvent).where(SystemEvent.event_type == "TEST_WARN")
    rows = (await db_session.execute(stmt)).scalars().all()
    assert len(rows) == 1
    assert dispatched == [("TEST_WARN", "WARNING")]


@pytest.mark.asyncio
async def test_notifier_info_level_does_not_dispatch(db_session):
    dispatched = []

    class FakeService:
        async def send_alert(self, title, message, level="INFO", details=None):
            dispatched.append(title)
            return True

    from app.services.monitoring import notifier as notifier_mod

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(notifier_mod, "get_alert_service", lambda: FakeService())
    try:
        await emit_system_event(
            db_session,
            event_type="TEST_INFO",
            severity="INFO",
            message="info only",
        )
    finally:
        monkeypatch.undo()

    assert dispatched == []
