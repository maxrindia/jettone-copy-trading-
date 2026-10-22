"""
Regression coverage for the LIMIT-path COPY_EXECUTED audit gap.

Investigation confirmed this was audit-only: Order and CopyTrade state were
already correctly written by _process_single_limit_follower on every
outcome; nothing anywhere (grepped engine/ and frontend/src/) reads
AuditLog.event_type == "COPY_EXECUTED", so no dashboard/reconciliation
behavior depended on it being absent. _process_single_market_follower
already wrote this AuditLog entry on every outcome; the LIMIT path wrote
none at all, success or failure.

Fix: one AuditLog write added to _process_single_limit_follower's existing
success/reject return point, mirroring the MARKET path's exact pattern -
no new broker call, no CopyTrade state change, no execution-logic change.

No live trading and no real broker calls are made anywhere in this file -
all broker clients are AsyncMock test doubles and the DB is the in-memory
test SQLite engine (db_session fixture).
"""
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.models.audit import AuditLog
from app.services.alice_blue.types import OrderResult, OrderResultCategory
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from strategy_helpers import SAMPLE_MASTER_ACCOUNT_ID


@pytest.mark.asyncio
async def test_successful_limit_copy_emits_exactly_one_copy_executed_audit_event(
    db_session, mock_redis, sample_accounts, sample_strategy, sample_copy_settings
):
    follower = sample_accounts["follower1"]
    client = AsyncMock()
    client.get_funds.return_value = MagicMock(available_margin=Decimal("500000"))
    client.place_order.return_value = OrderResult(
        category=OrderResultCategory.SUCCESS, client_order_id="CLI_1", broker_order_id="BRK_LIM_AUDIT_1",
        message="Order placed successfully", raw_response={"status": "success"},
        timestamp=datetime.now(timezone.utc),
    )

    event = MasterTradeEvent(
        account_id=SAMPLE_MASTER_ACCOUNT_ID,
        master_order_id="ORD_LIM_AUDIT_1", symbol="NIFTY24AUG23500CE", exchange="NFO", side="BUY",
        quantity=25, price=Decimal("150.50"), limit_price=Decimal("150.50"), product_type="MIS",
        timestamp=datetime.now(timezone.utc), order_type="LIMIT", event_type="LIMIT_CREATED",
    )

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.replicate_limit_order(event=event, clients={str(follower.id): client})

    assert res["results"][str(follower.id)]["status"] == "SUCCESS"

    rows = (
        await db_session.execute(select(AuditLog).where(AuditLog.master_order_id == "ORD_LIM_AUDIT_1", AuditLog.event_type != "COPY_FANOUT_SUMMARY"))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_type == "COPY_EXECUTED"
    assert rows[0].account_id == follower.id
    assert rows[0].quantity > 0


@pytest.mark.asyncio
async def test_rejected_limit_copy_emits_exactly_one_copy_failed_audit_event(
    db_session, mock_redis, sample_accounts, sample_strategy, sample_copy_settings
):
    follower = sample_accounts["follower1"]
    client = AsyncMock()
    client.get_funds.return_value = MagicMock(available_margin=Decimal("500000"))
    client.place_order.return_value = OrderResult(
        category=OrderResultCategory.REJECTED, client_order_id=None, broker_order_id=None,
        message="RMS:Margin Exceeds", raw_response={"status": "error"},
        timestamp=datetime.now(timezone.utc),
    )

    event = MasterTradeEvent(
        account_id=SAMPLE_MASTER_ACCOUNT_ID,
        master_order_id="ORD_LIM_AUDIT_2", symbol="NIFTY24AUG23500CE", exchange="NFO", side="BUY",
        quantity=25, price=Decimal("150.50"), limit_price=Decimal("150.50"), product_type="MIS",
        timestamp=datetime.now(timezone.utc), order_type="LIMIT", event_type="LIMIT_CREATED",
    )

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.replicate_limit_order(event=event, clients={str(follower.id): client})

    assert res["results"][str(follower.id)]["status"] == "REJECTED"

    rows = (
        await db_session.execute(select(AuditLog).where(AuditLog.master_order_id == "ORD_LIM_AUDIT_2", AuditLog.event_type != "COPY_FANOUT_SUMMARY"))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_type == "COPY_FAILED"
    assert rows[0].reason_code == "RMS:Margin Exceeds"
