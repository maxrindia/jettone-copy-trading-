"""
R3b D5: evidence-based startup reconciliation of Child copy_trades (ReconciliationEngine.run_crash_recovery).

It used to flag EVERY PENDING / SUBMITTED row RECONCILIATION_REQUIRED with one generic reason, healthy resting LIMIT
children included. Now:

  PENDING, never submitted   -> RECONCILIATION_REQUIRED "CRASH_BEFORE_SEND"   (a missing submit stamp is not proof)
  PENDING, submitted         -> RECONCILIATION_REQUIRED "CRASH_DURING_SEND"
  SUBMITTED, id in the Child's freshly read book  -> left to the fill syncer
  SUBMITTED, id missing from a successfully read book -> RECONCILIATION_REQUIRED "SUBMITTED_NOT_IN_BOOK"
  SUBMITTED, book unreadable / no client -> left alone and reported ONCE (a failed read proves nothing)
  TIMEOUT and every other status -> untouched (R2 unresolved-send handling)

Nothing here ever sends anything; alerts are deduplicated across repeated recovery runs.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.models.audit import SystemEvent
from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderSide, OrderSource, OrderStatus
from app.services.reliability.reconciliation import ReconciliationEngine
from conftest import TestingSessionLocal


async def trade(db_session, follower, status, *, submitted=False, broker_order_id=None, master="R3B_D5"):
    row = CopyTrade(id=uuid.uuid4(), master_order_id=f"{master}_{uuid.uuid4().hex[:6]}", follower_account_id=follower.id,
                    copy_event_id=uuid.uuid4(), symbol="RELIANCE", exchange="NSE", side=OrderSide.BUY, quantity=10,
                    product_type="MIS", client_order_id=f"JT_{uuid.uuid4().hex[:12]}", status=status,
                    submitted_at=datetime.now(timezone.utc) if submitted else None, broker_order_id=broker_order_id)
    db_session.add(row)
    await db_session.commit()
    return row


def provider(book=None, *, raises=False, raw_value="rows"):
    """A client provider whose get_order_book_raw returns `book` (rows), None (failure), raises, or a non-awaitable."""
    client = MagicMock()
    if raw_value == "non-awaitable":
        client.get_order_book_raw = MagicMock(return_value=None)
    elif raises:
        client.get_order_book_raw = AsyncMock(side_effect=RuntimeError("broker down"))
    else:
        client.get_order_book_raw = AsyncMock(return_value=book)
    client.place_order = AsyncMock()
    return (lambda account_id: client), client


@pytest.fixture(autouse=True)
def no_position_reconciliation(monkeypatch):
    """Only the Child-record classification is under test here, not position reconciliation."""
    monkeypatch.setattr(ReconciliationEngine, "run_for_account", AsyncMock(return_value=[]))


async def reload(db_session, row):
    row_id = row.id                                       # read before expiring: an expired attribute cannot load here
    db_session.expire_all()
    return (await db_session.execute(select(CopyTrade).where(CopyTrade.id == row_id))).scalar_one()


async def events(event_type):
    async with TestingSessionLocal() as s:
        return (await s.execute(select(SystemEvent).where(SystemEvent.event_type == event_type))).scalars().all()


@pytest.mark.asyncio
async def test_a_pending_claim_that_never_stamped_a_submit_is_flagged_crash_before_send(db_session, sample_accounts):
    row = await trade(db_session, sample_accounts["follower1"], CopyTradeStatus.PENDING)
    assert await ReconciliationEngine.run_crash_recovery(db_session) == 1
    row = await reload(db_session, row)
    assert row.status == CopyTradeStatus.RECONCILIATION_REQUIRED
    assert row.failure_reason.startswith("CRASH_BEFORE_SEND:") and "do not resend" in row.failure_reason
    assert "may have been sent" not in row.failure_reason or "verify" in row.failure_reason.lower()


@pytest.mark.asyncio
async def test_a_pending_claim_with_a_submit_stamp_is_flagged_crash_during_send(db_session, sample_accounts):
    row = await trade(db_session, sample_accounts["follower1"], CopyTradeStatus.PENDING, submitted=True)
    assert await ReconciliationEngine.run_crash_recovery(db_session) == 1
    row = await reload(db_session, row)
    assert row.status == CopyTradeStatus.RECONCILIATION_REQUIRED and row.failure_reason.startswith("CRASH_DURING_SEND:")


@pytest.mark.asyncio
async def test_a_submitted_order_found_in_the_childs_book_is_left_to_the_fill_syncer(db_session, sample_accounts):
    row = await trade(db_session, sample_accounts["follower1"], CopyTradeStatus.SUBMITTED, submitted=True, broker_order_id="CH_1")
    get_client, client = provider([{"brokerOrderId": "CH_1"}, {"brokerOrderId": "OTHER"}])
    assert await ReconciliationEngine.run_crash_recovery(db_session, client_provider=get_client) == 0
    assert (await reload(db_session, row)).status == CopyTradeStatus.SUBMITTED
    assert await events("CRASH_RECOVERY_FLAGGED_TRADE") == [] and await events("CRASH_RECOVERY_CHILD_BOOK_UNAVAILABLE") == []
    client.place_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("book", [[], [{"brokerOrderId": "SOMETHING_ELSE"}]], ids=["empty-but-read", "other-orders"])
async def test_a_submitted_order_missing_from_a_successfully_read_book_is_flagged(db_session, sample_accounts, book):
    row = await trade(db_session, sample_accounts["follower1"], CopyTradeStatus.SUBMITTED, submitted=True, broker_order_id="CH_2")
    get_client, client = provider(book)
    assert await ReconciliationEngine.run_crash_recovery(db_session, client_provider=get_client) == 1
    row = await reload(db_session, row)
    assert row.status == CopyTradeStatus.RECONCILIATION_REQUIRED and row.failure_reason.startswith("SUBMITTED_NOT_IN_BOOK:")
    (event,) = await events("CRASH_RECOVERY_FLAGGED_TRADE")
    assert event.metadata_payload["code"] == "SUBMITTED_NOT_IN_BOOK"
    client.place_order.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["no-client", "read-failed", "read-raises", "not-awaitable"])
async def test_an_unreadable_child_book_is_not_proof_the_order_is_missing_and_is_reported_once(db_session, sample_accounts, how):
    row = await trade(db_session, sample_accounts["follower1"], CopyTradeStatus.SUBMITTED, submitted=True, broker_order_id="CH_3")
    if how == "no-client":
        get_client = lambda account_id: None                                          # noqa: E731
    elif how == "read-failed":
        get_client, _ = provider(None)
    elif how == "read-raises":
        get_client, _ = provider(raises=True)
    else:
        get_client, _ = provider(raw_value="non-awaitable")
    for _ in range(3):                                                                # three restarts
        assert await ReconciliationEngine.run_crash_recovery(db_session, client_provider=get_client) == 0
    assert (await reload(db_session, row)).status == CopyTradeStatus.SUBMITTED        # untouched
    assert len(await events("CRASH_RECOVERY_CHILD_BOOK_UNAVAILABLE")) == 1            # reported once, not three times
    assert await events("CRASH_RECOVERY_FLAGGED_TRADE") == []


@pytest.mark.asyncio
async def test_without_any_client_provider_a_submitted_order_is_left_alone(db_session, sample_accounts):
    row = await trade(db_session, sample_accounts["follower1"], CopyTradeStatus.SUBMITTED, broker_order_id="CH_4")
    assert await ReconciliationEngine.run_crash_recovery(db_session) == 0
    assert (await reload(db_session, row)).status == CopyTradeStatus.SUBMITTED


@pytest.mark.asyncio
async def test_a_submitted_row_with_no_broker_id_is_flagged(db_session, sample_accounts):
    row = await trade(db_session, sample_accounts["follower1"], CopyTradeStatus.SUBMITTED, submitted=True)
    get_client, _ = provider([])
    assert await ReconciliationEngine.run_crash_recovery(db_session, client_provider=get_client) == 1
    assert (await reload(db_session, row)).failure_reason.startswith("SUBMITTED_NO_BROKER_ID:")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [CopyTradeStatus.TIMEOUT, CopyTradeStatus.SKIPPED, CopyTradeStatus.REJECTED, CopyTradeStatus.FAILED,
                                    CopyTradeStatus.SUCCESS, CopyTradeStatus.RECONCILIATION_REQUIRED])
async def test_every_other_status_is_untouched(db_session, sample_accounts, status):
    row = await trade(db_session, sample_accounts["follower1"], status, submitted=status == CopyTradeStatus.TIMEOUT)
    row.failure_reason = "original reason"
    await db_session.commit()
    get_client, client = provider([])
    assert await ReconciliationEngine.run_crash_recovery(db_session, client_provider=get_client) == 0
    row = await reload(db_session, row)
    assert row.status == status and row.failure_reason == "original reason"
    client.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_recovery_runs_flag_nothing_twice_and_add_no_duplicate_alerts(db_session, sample_accounts):
    f = sample_accounts["follower1"]
    await trade(db_session, f, CopyTradeStatus.PENDING)
    await trade(db_session, f, CopyTradeStatus.PENDING, submitted=True)
    await trade(db_session, f, CopyTradeStatus.SUBMITTED, submitted=True, broker_order_id="GONE")
    get_client, _ = provider([])
    assert await ReconciliationEngine.run_crash_recovery(db_session, client_provider=get_client) == 3
    assert await ReconciliationEngine.run_crash_recovery(db_session, client_provider=get_client) == 0
    assert await ReconciliationEngine.run_crash_recovery(db_session, client_provider=get_client) == 0
    assert len(await events("CRASH_RECOVERY_FLAGGED_TRADE")) == 3


@pytest.mark.asyncio
async def test_a_childs_book_is_read_once_however_many_of_its_orders_are_checked(db_session, sample_accounts):
    f = sample_accounts["follower1"]
    for n in range(3):
        await trade(db_session, f, CopyTradeStatus.SUBMITTED, submitted=True, broker_order_id=f"CH_{n}")
    get_client, client = provider([{"brokerOrderId": "CH_0"}, {"brokerOrderId": "CH_1"}, {"brokerOrderId": "CH_2"}])
    assert await ReconciliationEngine.run_crash_recovery(db_session, client_provider=get_client) == 0
    assert client.get_order_book_raw.await_count == 1


@pytest.mark.asyncio
async def test_ambiguous_follower_orders_are_alerted_once_however_often_the_engine_restarts(db_session, sample_accounts):
    f = sample_accounts["follower1"]
    db_session.add(Order(id=uuid.uuid4(), internal_order_id="JT_CH_AMBIG_1", broker_order_id=None, account_id=f.id,
                         source=OrderSource.SYSTEM, exchange="NSE", symbol="RELIANCE", side=OrderSide.BUY, order_type="LIMIT",
                         product_type="MIS", quantity=10, filled_quantity=0, pending_quantity=10, price=Decimal("100"),
                         status=OrderStatus.PENDING, master_broker_order_id="M1"))
    await db_session.commit()
    assert await ReconciliationEngine.flag_ambiguous_orders(db_session) == 1          # the contract: rows found
    assert await ReconciliationEngine.flag_ambiguous_orders(db_session) == 1
    assert await ReconciliationEngine.flag_ambiguous_orders(db_session) == 1
    assert len(await events("CRASH_RECOVERY_AMBIGUOUS_ORDER")) == 1                   # ...alerted once
