"""
P1 #5 regression coverage: if a follower/master broker operation SUCCEEDS
but the subsequent DB persistence/update fails, this must never be reported
as if the broker operation itself failed, and must never trigger a second
broker order ("retry"). A CRITICAL SystemEvent is emitted, reusing the
exact emit_system_event(event_type, severity="CRITICAL", message,
account_id, metadata_payload) pattern already used by
handle_master_cancellation's CANCELLATION_VERIFICATION_FAILED,
square_off_child_position's SQUARE_OFF_PERSISTENCE_FAILED, and broker.py's
manual-order MANUAL_ORDER_POST_SUBMIT_FAILURE.

Covers all three target paths:
- _process_single_limit_follower  -> LIMIT_ORDER_PERSISTENCE_FAILED
- _process_single_market_follower -> MARKET_ORDER_PERSISTENCE_FAILED
- handle_master_modification      -> MODIFICATION_PERSISTENCE_FAILED

No live trading is enabled and no real broker calls are made anywhere in
this file - all broker clients are AsyncMock test doubles.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import sqlalchemy
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import SystemEvent
from app.models.strategies import GroupStrategy, StrategyMember
from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.types import FundsData, OrderResult, OrderResultCategory
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from strategy_helpers import SAMPLE_MASTER_ACCOUNT_ID


def _limit_event(quantity=100, price=Decimal("100.00"), master_order_id="ORD_LIMIT_DB_FAIL"):
    return MasterTradeEvent(
        account_id=SAMPLE_MASTER_ACCOUNT_ID,
        master_order_id=master_order_id,
        symbol="RELIANCE", exchange="NSE", side="BUY", quantity=quantity,
        price=price, limit_price=price, product_type="MIS",
        timestamp=datetime.now(timezone.utc), fill_sequence=1,
        order_type="LIMIT", event_type="LIMIT_CREATED", status="OPEN",
    )


def _market_event(quantity=100, master_order_id="ORD_MARKET_DB_FAIL"):
    return MasterTradeEvent(
        account_id=SAMPLE_MASTER_ACCOUNT_ID,
        master_order_id=master_order_id,
        symbol="RELIANCE", exchange="NSE", side="BUY", quantity=quantity,
        price=Decimal("100.00"), product_type="MIS",
        timestamp=datetime.now(timezone.utc), fill_sequence=quantity,
        order_type="MARKET", event_type="MARKET_ACCEPTED", status="COMPLETE",
        total_quantity=quantity, filled_quantity=quantity, pending_quantity=0,
    )


def _funds(available=Decimal("500000")):
    return FundsData(
        account_id="a", available_margin=available, collateral=Decimal("0"),
        used_margin=Decimal("0"), updated_at=datetime.now(timezone.utc),
    )


def _success_result(broker_order_id="BRK_OK"):
    return OrderResult(
        category=OrderResultCategory.SUCCESS, client_order_id=None, broker_order_id=broker_order_id,
        message="ok", raw_response={"stat": "Ok"}, timestamp=datetime.now(timezone.utc),
    )


def _rejected_result():
    return OrderResult(
        category=OrderResultCategory.REJECTED, client_order_id=None, broker_order_id=None,
        message="RMS rejected", raw_response={}, timestamp=datetime.now(timezone.utc),
    )


async def _events_of_type(event_type):
    # Deferred import: db_session's fixture monkeypatches
    # app.core.database.AsyncSessionLocal AFTER this module is first
    # imported, so a module-level `from app.core.database import
    # AsyncSessionLocal` would bind the real (Postgres-pointing) symbol
    # instead of the patched TestingSessionLocal.
    from app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(select(SystemEvent).where(SystemEvent.event_type == event_type))
        ).scalars().all()


# ---------------------------------------------------------------------------
# 1. LIMIT follower: broker success, DB persistence fails
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_limit_follower_broker_success_db_failure_emits_critical_event_and_no_retry(
    db_session, mock_redis, sample_accounts, sample_strategy, monkeypatch
):
    follower = sample_accounts["follower1"]
    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.get_positions.return_value = []
    client.place_order.return_value = _success_result("BRK_LIMIT_DB_FAIL")

    original_save = CopyEnginePipeline.save_or_update_order

    async def failing_save(self, *args, **kwargs):
        if kwargs.get("status") == OrderStatus.OPEN:
            raise RuntimeError("simulated DB failure")
        return await original_save(self, *args, **kwargs)

    monkeypatch.setattr(CopyEnginePipeline, "save_or_update_order", failing_save)

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    fid = str(follower.id)
    res = await pipeline.execute_replication(event=_limit_event(), clients={fid: client})

    assert res["status"] == "completed"
    result = res["results"][fid]
    assert result["status"] == "SUCCESS"  # broker succeeded - must not be reported as failed
    assert result.get("warning") == "DB_PERSISTENCE_FAILED"
    client.place_order.assert_awaited_once()  # exactly once - no retry

    events = await _events_of_type("LIMIT_ORDER_PERSISTENCE_FAILED")
    assert len(events) == 1
    assert events[0].severity.value == "CRITICAL"
    assert events[0].account_id == follower.id
    assert events[0].metadata_payload["broker_order_id"] == "BRK_LIMIT_DB_FAIL"
    assert events[0].metadata_payload["operation"] == "LIMIT_ORDER_CREATE"
    assert events[0].metadata_payload["symbol"] == "RELIANCE"
    assert events[0].metadata_payload["master_order_id"] == "ORD_LIMIT_DB_FAIL"
    assert "simulated DB failure" in events[0].metadata_payload["db_error"]


# ---------------------------------------------------------------------------
# 2. MARKET follower: broker success, DB persistence fails
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_market_follower_broker_success_db_failure_emits_critical_event_and_no_retry(
    db_session, mock_redis, sample_accounts, sample_strategy, monkeypatch
):
    follower = sample_accounts["follower1"]
    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.get_positions.return_value = []
    client.place_order.return_value = _success_result("BRK_MARKET_DB_FAIL")

    original_save = CopyEnginePipeline.save_or_update_order

    async def failing_save(self, *args, **kwargs):
        # A successful MARKET placement's final persistence write now uses
        # status=OPEN (fill not yet confirmed - see _process_single_market_
        # follower), not COMPLETE; COMPLETE is only ever reached later via
        # FollowerFillSyncer polling the real broker order book.
        if kwargs.get("status") == OrderStatus.OPEN:
            raise RuntimeError("simulated DB failure")
        return await original_save(self, *args, **kwargs)

    monkeypatch.setattr(CopyEnginePipeline, "save_or_update_order", failing_save)

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    fid = str(follower.id)
    res = await pipeline.execute_replication(event=_market_event(), clients={fid: client})

    assert res["status"] == "completed"
    result = res["results"][fid]
    assert result["status"] == OrderResultCategory.SUCCESS.value
    assert result.get("warning") == "DB_PERSISTENCE_FAILED"
    client.place_order.assert_awaited_once()

    events = await _events_of_type("MARKET_ORDER_PERSISTENCE_FAILED")
    assert len(events) == 1
    assert events[0].severity.value == "CRITICAL"
    assert events[0].metadata_payload["broker_order_id"] == "BRK_MARKET_DB_FAIL"
    assert events[0].metadata_payload["operation"] == "MARKET_ORDER_CREATE"


@pytest.mark.asyncio
async def test_market_follower_success_does_not_prematurely_mark_order_complete(
    db_session, mock_redis, sample_accounts, sample_strategy
):
    """
    Root cause of the reported "master cancellation didn't cancel the child"
    bug: OrderResult carries no fill confirmation at all (place_order only
    tells you the broker ACCEPTED the request), so treating a SUCCESS
    placement as COMPLETE was an assumption, not a fact. A master
    cancellation for this same broker order arriving shortly after would
    find nothing to cancel (handle_master_cancellation only looks at
    OPEN/PARTIAL/PENDING children). The child must instead land as OPEN with
    the full quantity still pending, exactly like a LIMIT order's initial
    state - FollowerFillSyncer's existing broker-order-book polling is what
    confirms the real terminal status later.
    """
    follower = sample_accounts["follower1"]
    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.get_positions.return_value = []
    client.place_order.return_value = _success_result("BRK_MARKET_OPEN")

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    fid = str(follower.id)
    res = await pipeline.execute_replication(event=_market_event(quantity=40), clients={fid: client})

    assert res["status"] == "completed"
    assert res["results"][fid]["status"] == OrderResultCategory.SUCCESS.value

    from app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        order = (
            await session.execute(select(Order).where(Order.broker_order_id == "BRK_MARKET_OPEN"))
        ).scalar_one()
        trade = (
            await session.execute(select(CopyTrade).where(CopyTrade.broker_order_id == "BRK_MARKET_OPEN"))
        ).scalar_one()

    assert order.status == OrderStatus.OPEN
    assert order.filled_quantity == 0
    assert order.pending_quantity == 40
    assert trade.status == CopyTradeStatus.SUBMITTED


# ---------------------------------------------------------------------------
# 3. Master modification: broker modify_order succeeds, DB update fails
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_master_modification_broker_success_db_failure_emits_critical_event_and_no_retry(
    db_session, mock_redis, sample_accounts, monkeypatch
):
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]

    strategy = GroupStrategy(
        id=uuid.uuid4(), name="DB Fail Test Strategy", master_account_id=master.id, trading_enabled=True
    )
    db_session.add(strategy)
    db_session.add(StrategyMember(
        id=uuid.uuid4(), strategy_id=strategy.id, account_id=follower.id,
        multiplier=Decimal("1.0"), trading_enabled=True,
    ))
    db_session.add(Order(
        id=uuid.uuid4(), internal_order_id="JT_CH_DBFAIL", broker_order_id="BRK_MOD_DBFAIL",
        account_id=follower.id, master_broker_order_id="ORD_MOD_DBFAIL", source=OrderSource.SYSTEM,
        exchange="NSE", symbol="RELIANCE", side=OrderSide.BUY, order_type="LIMIT", product_type="MIS",
        quantity=100, filled_quantity=0, pending_quantity=100, price=Decimal("95.00"),
        status=OrderStatus.OPEN,
    ))
    await db_session.commit()

    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.modify_order.return_value = _success_result("BRK_MOD_DBFAIL")

    original_execute = AsyncSession.execute

    async def failing_execute(self, stmt, *args, **kwargs):
        if isinstance(stmt, sqlalchemy.sql.dml.Update):
            raise RuntimeError("simulated DB failure during modify persistence")
        return await original_execute(self, stmt, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", failing_execute)

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.handle_master_modification(
        master_order_id="ORD_MOD_DBFAIL", new_price=Decimal("99.00"), new_quantity=150,
        clients={str(follower.id): client}, master_account_id=str(master.id),
    )

    assert res["status"] == "completed"
    result_str = res["modifications"][str(follower.id)]
    assert result_str == "MODIFIED_DB_PERSISTENCE_FAILED"
    assert "ERROR" not in result_str  # must not falsely claim broker failure
    assert "MODIFY_FAILED" not in result_str
    client.modify_order.assert_awaited_once()  # exactly once - no retry

    events = await _events_of_type("MODIFICATION_PERSISTENCE_FAILED")
    assert len(events) == 1
    assert events[0].severity.value == "CRITICAL"
    assert events[0].metadata_payload["broker_order_id"] == "BRK_MOD_DBFAIL"
    assert events[0].metadata_payload["operation"] == "MASTER_MODIFICATION"
    assert events[0].metadata_payload["master_order_id"] == "ORD_MOD_DBFAIL"


# ---------------------------------------------------------------------------
# 4. Regression: the new event must NOT fire when the broker itself fails
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_broker_rejection_does_not_emit_limit_persistence_failed_event(
    db_session, mock_redis, sample_accounts, sample_strategy
):
    follower = sample_accounts["follower1"]
    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.get_positions.return_value = []
    client.place_order.return_value = _rejected_result()

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    fid = str(follower.id)
    res = await pipeline.execute_replication(
        event=_limit_event(master_order_id="ORD_LIMIT_REJECT"), clients={fid: client}
    )

    assert res["results"][fid]["status"] == "REJECTED"
    assert (await _events_of_type("LIMIT_ORDER_PERSISTENCE_FAILED")) == []


@pytest.mark.asyncio
async def test_broker_rejection_with_db_failure_still_propagates_as_exception_not_persistence_failed(
    db_session, mock_redis, sample_accounts, sample_strategy, monkeypatch
):
    """The broker itself failed AND the subsequent DB write also fails - this
    must preserve pre-existing behavior (propagate, caught upstream as
    EXCEPTION) rather than being reclassified as a false 'broker succeeded'
    persistence-failure event."""
    follower = sample_accounts["follower1"]
    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.get_positions.return_value = []
    client.place_order.return_value = _rejected_result()

    original_save = CopyEnginePipeline.save_or_update_order

    async def failing_save(self, *args, **kwargs):
        if kwargs.get("status") == OrderStatus.REJECTED:
            raise RuntimeError("simulated DB failure on rejection path")
        return await original_save(self, *args, **kwargs)

    monkeypatch.setattr(CopyEnginePipeline, "save_or_update_order", failing_save)

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    fid = str(follower.id)
    res = await pipeline.execute_replication(
        event=_limit_event(master_order_id="ORD_LIMIT_REJECT_DBFAIL"), clients={fid: client}
    )

    assert res["results"][fid]["status"] == "EXCEPTION"  # unchanged pre-existing behavior
    assert (await _events_of_type("LIMIT_ORDER_PERSISTENCE_FAILED")) == []


@pytest.mark.asyncio
async def test_modify_order_failure_does_not_emit_modification_persistence_failed_event(
    db_session, mock_redis, sample_accounts
):
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]
    db_session.add(Order(
        id=uuid.uuid4(), internal_order_id="JT_CH_MODFAIL", broker_order_id="BRK_MOD_FAIL",
        account_id=follower.id, master_broker_order_id="ORD_MOD_FAIL", source=OrderSource.SYSTEM,
        exchange="NSE", symbol="RELIANCE", side=OrderSide.BUY, order_type="LIMIT", product_type="MIS",
        quantity=100, filled_quantity=0, pending_quantity=100, price=Decimal("95.00"),
        status=OrderStatus.OPEN,
    ))
    await db_session.commit()

    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.modify_order.return_value = OrderResult(
        category=OrderResultCategory.REJECTED, client_order_id=None, broker_order_id=None,
        message="broker rejected modification", raw_response={}, timestamp=datetime.now(timezone.utc),
    )

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.handle_master_modification(
        master_order_id="ORD_MOD_FAIL", new_price=Decimal("99.00"), new_quantity=150,
        clients={str(follower.id): client}, master_account_id=str(master.id),
    )

    assert "MODIFY_FAILED" in res["modifications"][str(follower.id)]
    assert (await _events_of_type("MODIFICATION_PERSISTENCE_FAILED")) == []
