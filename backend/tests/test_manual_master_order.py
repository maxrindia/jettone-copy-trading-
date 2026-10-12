"""
Regression coverage for the P0.2 fix in app/api/broker.py::place_manual_master_order.

Before the fix this endpoint:
  - referenced undefined names (ManualOrderSide, ManualOrderType, plus the
    unimported OrderStatus/OrderSource), guaranteeing a NameError on every
    call that reached the live-trading branch,
  - called client.place_order(req) twice for a single UI request,
  - wrote nothing to the DB before hitting the broker, so a crash between the
    broker call and the DB write left a real, unrecorded order with no way to
    tell it had ever been placed.

These tests exercise the real (unmodified) route function directly - not
through the test-only route-registration checks - with the broker client and
`_load_master_client` faked out, an in-memory DB, and ENGINE_LIVE_TRADING_ENABLED
patched True only inside this test module (never touched in the actual
project settings/.env - no real broker call is possible, `client` is a mock).
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.broker import ManualOrderRequest, place_manual_master_order
from app.models.audit import SystemEvent
from app.models.trades import Order, OrderStatus
from app.services.alice_blue.types import (
    FundsData,
    OrderRecord,
    OrderRequest,
    OrderResult,
    OrderResultCategory,
    OrderSide,
    OrderType,
    OrderUpdateEvent,
)
from app.services.copy_engine.detector import MasterTradeDetector
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator


def _order_result(category, broker_order_id=None, message="ok"):
    return OrderResult(
        category=category,
        client_order_id=None,
        broker_order_id=broker_order_id,
        message=message,
        raw_response={},
        timestamp=datetime.now(timezone.utc),
    )


def _limit_payload():
    return ManualOrderRequest(
        strategy_id=uuid.uuid4(),
        symbol="reliance-eq",
        exchange="nse",
        side=OrderSide.BUY,
        quantity=10,
        product_type="MIS",
        order_type=OrderType.LIMIT,
        price=Decimal("2500.00"),
    )


def _market_payload(quantity=100):
    return ManualOrderRequest(
        strategy_id=uuid.uuid4(),
        symbol="reliance-eq",
        exchange="nse",
        side=OrderSide.BUY,
        quantity=quantity,
        product_type="MIS",
        order_type=OrderType.MARKET,
    )


def _order_book_record(broker_order_id, status, filled_quantity, quantity=100):
    return OrderRecord(
        broker_order_id=broker_order_id,
        client_order_id=None,
        symbol="RELIANCE",
        exchange="NSE",
        side=OrderSide.BUY,
        quantity=quantity,
        filled_quantity=filled_quantity,
        average_price=Decimal("2500.00"),
        status=status,
        order_timestamp=datetime.now(timezone.utc),
        order_type="MARKET",
    )


@pytest.fixture
def fake_master_client_patch(sample_accounts):
    """Patches _load_master_client so the route doesn't need real credentials,
    and returns (master_account, broker_client_mock) for assertions."""
    master = sample_accounts["master"]
    broker_client = AsyncMock()
    with patch(
        "app.api.broker._load_master_client",
        AsyncMock(return_value=(master, broker_client)),
    ):
        yield master, broker_client


@pytest.mark.asyncio
async def test_no_undefined_manual_order_names_remain():
    """Static regression guard: ManualOrderSide/ManualOrderType must never
    reappear in this module, independent of which code path a test happens
    to exercise."""
    import inspect

    import app.api.broker as broker_module

    source = inspect.getsource(broker_module)
    assert "ManualOrderSide" not in source
    assert "ManualOrderType" not in source


@pytest.mark.asyncio
async def test_manual_limit_order_success_calls_broker_exactly_once(
    mock_redis, db_session, fake_master_client_patch, monkeypatch
):
    monkeypatch.setattr("app.api.broker.settings.ENGINE_LIVE_TRADING_ENABLED", True)
    master, broker_client = fake_master_client_patch
    broker_client.place_order = AsyncMock(
        return_value=_order_result(OrderResultCategory.SUCCESS, broker_order_id="BRK-999")
    )

    with patch("app.api.broker.is_master_trading_enabled", AsyncMock(return_value=True)), \
         patch(
             "app.services.alice_blue.manager.account_manager.get_follower_clients_map",
             MagicMock(return_value={}),
         ):
        response = await place_manual_master_order(
            payload=_limit_payload(), db=db_session, redis=mock_redis
        )

    # Exactly one broker submission for one UI request.
    broker_client.place_order.assert_awaited_once()

    assert response["placed"] is True
    assert response["master_order_id"] == "BRK-999"
    assert response["category"] == OrderResultCategory.SUCCESS.value

    # The persisted order must reflect the broker's response.
    row = (
        await db_session.execute(
            select(Order).where(Order.internal_order_id == response["client_order_id"])
        )
    ).scalar_one()
    assert row.broker_order_id == "BRK-999"
    assert row.status == OrderStatus.OPEN
    assert row.account_id == master.id


@pytest.mark.asyncio
async def test_order_is_persisted_pending_before_broker_is_called(
    mock_redis, db_session, fake_master_client_patch, monkeypatch
):
    """The PENDING row must exist in the DB at the moment place_order() is
    invoked - proving persistence happens before, not after, broker submission."""
    monkeypatch.setattr("app.api.broker.settings.ENGINE_LIVE_TRADING_ENABLED", True)
    master, broker_client = fake_master_client_patch
    seen_client_order_ids = []

    async def _fake_place_order(req: OrderRequest):
        # At the moment the broker is called, the PENDING row must already
        # be durably committed and readable from a fresh query.
        row = (
            await db_session.execute(
                select(Order).where(Order.internal_order_id == req.client_order_id)
            )
        ).scalar_one()
        assert row.status == OrderStatus.PENDING
        assert row.broker_order_id is None
        seen_client_order_ids.append(req.client_order_id)
        return _order_result(OrderResultCategory.SUCCESS, broker_order_id="BRK-PRE-1")

    broker_client.place_order = AsyncMock(side_effect=_fake_place_order)

    with patch("app.api.broker.is_master_trading_enabled", AsyncMock(return_value=True)), \
         patch(
             "app.services.alice_blue.manager.account_manager.get_follower_clients_map",
             MagicMock(return_value={}),
         ):
        response = await place_manual_master_order(
            payload=_limit_payload(), db=db_session, redis=mock_redis
        )

    assert len(seen_client_order_ids) == 1
    assert response["client_order_id"] == seen_client_order_ids[0]
    broker_client.place_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_order_rejection_updates_status_and_skips_replication(
    mock_redis, db_session, fake_master_client_patch, monkeypatch
):
    monkeypatch.setattr("app.api.broker.settings.ENGINE_LIVE_TRADING_ENABLED", True)
    master, broker_client = fake_master_client_patch
    broker_client.place_order = AsyncMock(
        return_value=_order_result(
            OrderResultCategory.REJECTED, broker_order_id=None, message="RMS: margin insufficient"
        )
    )

    with patch("app.api.broker.is_master_trading_enabled", AsyncMock(return_value=True)):
        response = await place_manual_master_order(
            payload=_limit_payload(), db=db_session, redis=mock_redis
        )

    broker_client.place_order.assert_awaited_once()
    assert response["placed"] is False
    assert response["master_order_id"] is None
    assert response["replication"] is None

    row = (
        await db_session.execute(
            select(Order).where(Order.internal_order_id.like("JT_MANUAL_%"))
        )
    ).scalars().first()
    assert row.status == OrderStatus.REJECTED
    assert row.broker_status == "REJECTED"
    assert row.rejection_reason == "RMS: margin insufficient"
    assert row.broker_order_id is None


@pytest.mark.asyncio
async def test_broker_success_then_persistence_failure_does_not_resubmit_and_stays_recoverable(
    mock_redis, db_session, fake_master_client_patch, monkeypatch
):
    """
    Crash-safety: broker accepts the order, but the post-acceptance status
    update raises (simulated DB outage). This must:
      - NOT trigger a second broker submission,
      - surface loudly (HTTPException 502 carrying broker_order_id/client_order_id,
        not a generic swallowed error),
      - leave the original PENDING row identifiable by client_order_id,
      - record a CRITICAL SystemEvent for manual reconciliation.
    """
    monkeypatch.setattr("app.api.broker.settings.ENGINE_LIVE_TRADING_ENABLED", True)
    master, broker_client = fake_master_client_patch
    broker_client.place_order = AsyncMock(
        return_value=_order_result(OrderResultCategory.SUCCESS, broker_order_id="BRK-CRASH-1")
    )

    original_save_or_update_order = CopyEnginePipeline.save_or_update_order
    call_count = {"n": 0}

    async def flaky_save_or_update_order(self, *args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            # Simulated failure of the POST-broker-acceptance status update.
            raise RuntimeError("simulated DB outage")
        return await original_save_or_update_order(self, *args, **kwargs)

    with patch("app.api.broker.is_master_trading_enabled", AsyncMock(return_value=True)), \
         patch.object(CopyEnginePipeline, "save_or_update_order", flaky_save_or_update_order):
        with pytest.raises(HTTPException) as exc_info:
            await place_manual_master_order(
                payload=_limit_payload(), db=db_session, redis=mock_redis
            )

    # Broker was contacted exactly once, even though the local write failed.
    broker_client.place_order.assert_awaited_once()

    assert exc_info.value.status_code == 502
    assert "BRK-CRASH-1" in exc_info.value.detail
    assert "JT_MANUAL_" in exc_info.value.detail  # client_order_id echoed back

    # The pre-broker-call PENDING row (call #1, which succeeded for real)
    # must still be present and findable by client_order_id - not lost.
    rows = (
        await db_session.execute(
            select(Order).where(Order.internal_order_id.like("JT_MANUAL_%"))
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == OrderStatus.PENDING  # update #2 never committed

    # A CRITICAL audit trail entry exists for manual reconciliation.
    events = (
        await db_session.execute(
            select(SystemEvent).where(SystemEvent.event_type == "MANUAL_ORDER_POST_SUBMIT_FAILURE")
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].severity.value == "CRITICAL"
    assert "BRK-CRASH-1" in events[0].message


# ---------------------------------------------------------------------------
# P1 fix: manual MARKET orders must never replicate immediately from this
# endpoint - only the WebSocket/MasterTradeDetector path replicates MARKET
# fills, avoiding the over-replication desync where both paths independently
# replicated the same fill.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_manual_market_order_partial_fill_does_not_replicate_immediately(
    mock_redis, db_session, fake_master_client_patch, monkeypatch
):
    monkeypatch.setattr("app.api.broker.settings.ENGINE_LIVE_TRADING_ENABLED", True)
    master, broker_client = fake_master_client_patch
    broker_client.place_order = AsyncMock(
        return_value=_order_result(OrderResultCategory.SUCCESS, broker_order_id="BRK-MKT-PARTIAL")
    )
    broker_client.get_order_book = AsyncMock(
        return_value=[_order_book_record("BRK-MKT-PARTIAL", status="PARTIAL", filled_quantity=40, quantity=100)]
    )

    with patch("app.api.broker.is_master_trading_enabled", AsyncMock(return_value=True)), \
         patch.object(CopyEnginePipeline, "execute_replication", AsyncMock()) as mock_execute_replication:
        response = await place_manual_master_order(
            payload=_market_payload(quantity=100), db=db_session, redis=mock_redis
        )

    mock_execute_replication.assert_not_called()
    assert response["placed"] is True
    assert response["replication"] is None

    # Master's own Order row still correctly reflects the partial fill observed.
    row = (
        await db_session.execute(
            select(Order).where(Order.internal_order_id == response["client_order_id"])
        )
    ).scalar_one()
    assert row.filled_quantity == 40


@pytest.mark.asyncio
async def test_manual_market_order_complete_fill_does_not_replicate_immediately(
    mock_redis, db_session, fake_master_client_patch, monkeypatch
):
    monkeypatch.setattr("app.api.broker.settings.ENGINE_LIVE_TRADING_ENABLED", True)
    master, broker_client = fake_master_client_patch
    broker_client.place_order = AsyncMock(
        return_value=_order_result(OrderResultCategory.SUCCESS, broker_order_id="BRK-MKT-COMPLETE")
    )
    broker_client.get_order_book = AsyncMock(
        return_value=[_order_book_record("BRK-MKT-COMPLETE", status="COMPLETE", filled_quantity=100, quantity=100)]
    )

    with patch("app.api.broker.is_master_trading_enabled", AsyncMock(return_value=True)), \
         patch.object(CopyEnginePipeline, "execute_replication", AsyncMock()) as mock_execute_replication:
        response = await place_manual_master_order(
            payload=_market_payload(quantity=100), db=db_session, redis=mock_redis
        )

    mock_execute_replication.assert_not_called()
    assert response["placed"] is True
    assert response["replication"] is None

    row = (
        await db_session.execute(
            select(Order).where(Order.internal_order_id == response["client_order_id"])
        )
    ).scalar_one()
    assert row.status == OrderStatus.COMPLETE
    assert row.filled_quantity == 100


@pytest.mark.asyncio
async def test_manual_market_fill_then_ws_detector_replicates_exactly_once_with_correct_quantity(
    mock_redis, db_session, fake_master_client_patch, monkeypatch, sample_accounts, sample_strategy
):
    """End-to-end: after the manual-order endpoint observes a MARKET fill
    during polling (and, per the fix, does NOT replicate it itself), the
    WebSocket/MasterTradeDetector path is the one that replicates it -
    exactly once, with the correct total quantity, to followers."""
    monkeypatch.setattr("app.api.broker.settings.ENGINE_LIVE_TRADING_ENABLED", True)
    master, broker_client = fake_master_client_patch
    follower = sample_accounts["follower1"]

    broker_client.place_order = AsyncMock(
        return_value=_order_result(OrderResultCategory.SUCCESS, broker_order_id="BRK-MKT-E2E")
    )
    broker_client.get_order_book = AsyncMock(
        return_value=[_order_book_record("BRK-MKT-E2E", status="COMPLETE", filled_quantity=100, quantity=100)]
    )

    with patch("app.api.broker.is_master_trading_enabled", AsyncMock(return_value=True)), \
         patch.object(CopyEnginePipeline, "execute_replication", AsyncMock()) as mock_execute_replication:
        response = await place_manual_master_order(
            payload=_market_payload(quantity=100), db=db_session, redis=mock_redis
        )

    mock_execute_replication.assert_not_called()
    assert response["placed"] is True

    # Now simulate the WS/detector path independently observing the same
    # broker order's fill - exactly as EngineLoop's WS listener would.
    detector = MasterTradeDetector(master_account_id=str(master.id))
    ws_event = detector.process_order_update(OrderUpdateEvent(
        broker_order_id="BRK-MKT-E2E",
        client_order_id=None,
        symbol="RELIANCE",
        side="BUY",
        quantity=100,
        filled_quantity=100,
        fill_price=Decimal("2500.00"),
        status="COMPLETE",
        timestamp=datetime.now(timezone.utc),
        account_id=str(master.id),
        order_type="MARKET",
    ))
    assert ws_event is not None
    assert ws_event.quantity == 100  # full amount - detector never double-subtracts

    follower_client = AsyncMock()
    follower_client.get_funds.return_value = FundsData(
        account_id=str(follower.id), available_margin=Decimal("500000"), collateral=Decimal("0"),
        used_margin=Decimal("0"), updated_at=datetime.now(timezone.utc),
    )
    follower_client.get_positions.return_value = []
    follower_client.place_order.return_value = _order_result(
        OrderResultCategory.SUCCESS, broker_order_id="BRK-FOLLOWER-E2E"
    )

    real_pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await real_pipeline.execute_replication(
        event=ws_event, clients={str(follower.id): follower_client}
    )

    assert res["status"] == "completed"
    follower_client.place_order.assert_awaited_once()  # exactly once - no double replication
    assert follower_client.place_order.call_args.args[0].quantity == 100  # correct total, not 40+100


@pytest.mark.asyncio
async def test_manual_limit_order_still_calls_execute_replication_immediately(
    mock_redis, db_session, fake_master_client_patch, monkeypatch
):
    """Regression: LIMIT manual orders are unaffected by this fix and must
    still replicate immediately from this endpoint."""
    monkeypatch.setattr("app.api.broker.settings.ENGINE_LIVE_TRADING_ENABLED", True)
    master, broker_client = fake_master_client_patch
    broker_client.place_order = AsyncMock(
        return_value=_order_result(OrderResultCategory.SUCCESS, broker_order_id="BRK-LIMIT-REG")
    )

    with patch("app.api.broker.is_master_trading_enabled", AsyncMock(return_value=True)), \
         patch.object(
             CopyEnginePipeline, "execute_replication", AsyncMock(return_value={"status": "completed"})
         ) as mock_execute_replication:
        response = await place_manual_master_order(
            payload=_limit_payload(), db=db_session, redis=mock_redis
        )

    mock_execute_replication.assert_awaited_once()
    assert response["replication"] == {"status": "completed"}
