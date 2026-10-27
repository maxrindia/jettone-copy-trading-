"""
The StaleOrderSweeper's order-book provider (EngineLoop._fetch_order_book_for_account) on the explicit read contract.

Before: a missing client and Alice Blue's failed read (its plain get_order_book() returns [] on any failure) both arrived as [], so
every TIMEOUT trade of that Child was reported CRITICAL "not found in broker order book" and stamped UNRESOLVED although nothing had
been read. Now an unproven read is None, the sweeper's existing "unavailable - leave it" answer. A genuinely empty book (proven by
the raw read) is still a real absence and is still flagged exactly once. Nothing here ever sends, cancels or modifies an order.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.accounts import AccountType
from app.models.audit import SystemEvent
from app.models.trades import CopyTrade, CopyTradeStatus
from app.services.reliability.stale_order_sweeper import UNRESOLVED_REASON, StaleOrderSweeper
from app.services.runtime.engine_loop import EngineLoop
from conftest import TestingSessionLocal
from strategy_helpers import ensure_account
from test_order_book_read import Fake
from test_timeout_reconciliation import _book, _legacy_timeout_trade

CID = "JT_SWEEP_1"


@pytest.fixture
def client_for(monkeypatch):
    holder = {}
    monkeypatch.setattr("app.services.runtime.engine_loop.account_manager.get_client", lambda acc_id: holder.get("client"))
    return holder


async def _setup(db_session):
    child = await ensure_account(db_session, account_type=AccountType.FOLLOWER)
    trade_id = await _legacy_timeout_trade(child, submitted_at=datetime.now(timezone.utc) - timedelta(hours=1), cid=CID)
    return child, trade_id


async def _sweep():
    async with TestingSessionLocal() as db:
        return await StaleOrderSweeper(order_book_provider=EngineLoop._fetch_order_book_for_account,
                                       timeout_grace_seconds=-1)._resolve_timeouts(db)


async def _state(trade_id):
    async with TestingSessionLocal() as db:
        trade = (await db.execute(select(CopyTrade).where(CopyTrade.id == trade_id))).scalar_one()
        events = (await db.execute(select(SystemEvent).where(SystemEvent.event_type == "TIMEOUT_UNRESOLVED"))).scalars().all()
        return trade, events


UNREADABLE = {
    "no_client": None,
    "alice_failed_read": Fake(book=[], raw=None),                     # raw None, plain [] - Alice's failure shape
    "alice_disagreeing_read": Fake(book=[], raw=[{"brokerOrderId": "X"}]),
    "raising_client": Fake(book_raises=ConnectionError("broker unreachable")),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(UNREADABLE))
async def test_an_unproven_book_changes_nothing_and_raises_no_false_alarm(db_session, client_for, name):
    child, trade_id = await _setup(db_session)
    before, _ = await _state(trade_id)
    client_for["client"] = UNREADABLE[name]
    for _ in range(2):
        assert await _sweep() == 0
    trade, events = await _state(trade_id)
    assert trade.status == CopyTradeStatus.TIMEOUT and trade.failure_reason == before.failure_reason
    assert events == []                                              # no CRITICAL "not found" for a book nobody read


@pytest.mark.asyncio
async def test_a_proven_empty_book_is_still_a_real_absence_flagged_once(db_session, client_for):
    child, trade_id = await _setup(db_session)
    client_for["client"] = Fake(book=[], raw=[])                      # raw read succeeded and is empty
    for _ in range(3):
        assert await _sweep() == 0
    trade, events = await _state(trade_id)
    assert trade.status == CopyTradeStatus.TIMEOUT and (trade.failure_reason or "").startswith(UNRESOLVED_REASON)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_a_readable_book_with_the_order_still_resolves_it(db_session, client_for):
    child, trade_id = await _setup(db_session)
    client_for["client"] = Fake(book=_book(CID, "COMPLETE"), raw=[{"brokerOrderId": "BRK-1"}])
    assert await _sweep() == 1
    trade, events = await _state(trade_id)
    assert trade.status == CopyTradeStatus.SUCCESS and events == []


@pytest.mark.asyncio
async def test_the_provider_returns_rows_only_for_a_proven_read(client_for):
    client_for["client"] = Fake(book=_book(CID), raw=[{"brokerOrderId": "BRK-1"}])
    rows = await EngineLoop._fetch_order_book_for_account("acc")
    assert [r.client_order_id for r in rows] == [CID]
    client_for["client"] = Fake(book=[], raw=[])
    assert await EngineLoop._fetch_order_book_for_account("acc") == []
    for client in UNREADABLE.values():
        client_for["client"] = client
        assert await EngineLoop._fetch_order_book_for_account("acc") is None


@pytest.mark.asyncio
async def test_a_bare_mock_client_is_read_as_before(client_for):
    """MagicMock-style clients (raw probe callable but not awaitable) keep the old behaviour: the plain read is used."""
    from unittest.mock import MagicMock
    client = MagicMock()
    client.get_order_book = AsyncMock(return_value=_book(CID))
    client_for["client"] = client
    rows = await EngineLoop._fetch_order_book_for_account("acc")
    assert [r.client_order_id for r in rows] == [CID]
