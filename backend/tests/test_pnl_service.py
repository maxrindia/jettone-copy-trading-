"""PnlService tests: each account's P&L comes from its OWN broker books, there
is no DB fallback when the broker is unavailable, broker reads are cached,
mismatches alert at most once per account per trading day, and Jettone's own
tables (square-off rows at average_price 0, copy_trades estimated prices)
never feed P&L."""
import copy
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.audit import SystemEvent
from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderSide, OrderSource, OrderStatus
from app.services.pnl.service import MISMATCH_EVENT_TYPE, PnlService
from pnl_live_fixtures import (
    CHILD_ORDER_BOOK,
    CHILD_POSITIONS,
    MASTER_ORDER_BOOK,
    MASTER_POSITIONS,
    FakeBrokerClient,
)

NOW = datetime(2026, 9, 11, 9, 0, tzinfo=timezone.utc)  # 14:30 IST on the fixture's trading day


def make_service(redis, clients, now=NOW, **kwargs):
    return PnlService(redis=redis, client_provider=lambda account_id: clients.get(account_id), now=lambda: now, **kwargs)


def ids(sample_accounts):
    return str(sample_accounts["master"].id), str(sample_accounts["follower1"].id)


async def mismatch_events(db_session):
    rows = await db_session.execute(select(SystemEvent).where(SystemEvent.event_type == MISMATCH_EVENT_TYPE))
    return rows.scalars().all()


@pytest.mark.asyncio
async def test_master_and_child_pnl_come_from_their_own_broker_fills(db_session, sample_accounts, mock_redis):
    master_id, child_id = ids(sample_accounts)
    clients = {
        master_id: FakeBrokerClient(MASTER_ORDER_BOOK, MASTER_POSITIONS),
        child_id: FakeBrokerClient(CHILD_ORDER_BOOK, CHILD_POSITIONS),
    }
    summaries = await make_service(mock_redis, clients).summarize_accounts([master_id, child_id])

    master = summaries[master_id].as_api_fields()
    child = summaries[child_id].as_api_fields()
    assert (master["realized_pnl"], master["unrealized_pnl"], master["total_pnl"]) == ("-1.52", "0.00", "-1.52")
    assert (child["realized_pnl"], child["unrealized_pnl"], child["total_pnl"]) == ("-1.48", "0.00", "-1.48")
    for fields in (master, child):
        assert fields["pnl_basis"] == "GROSS"
        assert fields["pnl_status"] == "OK"
        assert fields["open_positions_count"] == 0
        assert fields["broker_check"]["status"] == "MATCH"
        assert fields["trading_day"] == "2026-09-11"
    assert "4200.16" not in json.dumps([master, child])
    assert await mismatch_events(db_session) == []


@pytest.mark.asyncio
async def test_order_book_unavailable_returns_nulls_without_db_fallback(db_session, sample_accounts, mock_redis):
    master_id, _ = ids(sample_accounts)
    # A COMPLETE row in Jettone's own table must not be used as a fallback.
    db_session.add(Order(
        internal_order_id="EXT_FALLBACK_1", broker_order_id="26091100020254", account_id=uuid.UUID(master_id),
        source=OrderSource.BROKER_EXTERNAL, exchange="NSE", symbol="CANBK", side=OrderSide.BUY,
        order_type="MARKET", product_type="INTRADAY", quantity=1, filled_quantity=1,
        price=Decimal("123.10"), average_price=Decimal("123.10"), status=OrderStatus.COMPLETE,
    ))
    await db_session.commit()

    clients = {master_id: FakeBrokerClient(None, MASTER_POSITIONS)}
    fields = (await make_service(mock_redis, clients).summarize_accounts([master_id]))[master_id].as_api_fields()

    assert fields["pnl_status"] == "UNAVAILABLE"
    assert (fields["realized_pnl"], fields["unrealized_pnl"], fields["total_pnl"]) == (None, None, None)
    assert fields["pnl_notes"] == ["BROKER_ORDER_BOOK_UNAVAILABLE"]
    assert fields["open_positions_count"] == 0  # the position book itself was readable
    assert fields["broker_check"]["status"] == "NOT_COMPARABLE"


@pytest.mark.asyncio
async def test_broker_error_is_unavailable(sample_accounts, mock_redis):
    master_id, _ = ids(sample_accounts)
    clients = {master_id: FakeBrokerClient(RuntimeError("session expired"), RuntimeError("session expired"))}
    fields = (await make_service(mock_redis, clients).summarize_accounts([master_id]))[master_id].as_api_fields()
    assert fields["pnl_status"] == "UNAVAILABLE"
    assert fields["total_pnl"] is None
    assert fields["open_positions_count"] is None
    assert fields["broker_check"]["status"] == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_account_without_broker_client_is_unavailable(sample_accounts, mock_redis):
    master_id, _ = ids(sample_accounts)
    fields = (await make_service(mock_redis, {}).summarize_accounts([master_id]))[master_id].as_api_fields()
    assert fields["pnl_status"] == "UNAVAILABLE"
    assert fields["pnl_notes"] == ["NO_BROKER_CLIENT"]
    assert fields["open_positions_count"] is None


@pytest.mark.asyncio
async def test_flat_account_with_positions_unavailable_reports_realized_only(sample_accounts, mock_redis):
    master_id, _ = ids(sample_accounts)
    clients = {master_id: FakeBrokerClient(MASTER_ORDER_BOOK, None)}
    fields = (await make_service(mock_redis, clients).summarize_accounts([master_id]))[master_id].as_api_fields()
    assert fields["pnl_status"] == "PARTIAL"
    assert (fields["realized_pnl"], fields["unrealized_pnl"], fields["total_pnl"]) == ("-1.52", None, None)
    assert fields["open_positions_count"] is None
    assert "BROKER_POSITIONS_UNAVAILABLE" in fields["pnl_notes"]


@pytest.mark.asyncio
async def test_broker_reads_are_cached_per_account(sample_accounts, mock_redis):
    master_id, _ = ids(sample_accounts)
    client = FakeBrokerClient(MASTER_ORDER_BOOK, MASTER_POSITIONS)
    service = make_service(mock_redis, {master_id: client})
    first = await service.summarize_accounts([master_id])
    second = await service.summarize_accounts([master_id])
    assert (client.order_book_calls, client.position_calls) == (1, 1)
    assert first[master_id].total == second[master_id].total

    uncached_client = FakeBrokerClient(MASTER_ORDER_BOOK, MASTER_POSITIONS)
    uncached = make_service(None, {master_id: uncached_client})
    await uncached.summarize_accounts([master_id])
    await uncached.summarize_accounts([master_id])
    assert (uncached_client.order_book_calls, uncached_client.position_calls) == (2, 2)


@pytest.mark.asyncio
async def test_failed_broker_read_is_not_cached(sample_accounts, mock_redis):
    master_id, _ = ids(sample_accounts)
    client = FakeBrokerClient(None, MASTER_POSITIONS)
    service = make_service(mock_redis, {master_id: client})
    await service.summarize_accounts([master_id])
    client.orders = MASTER_ORDER_BOOK
    fields = (await service.summarize_accounts([master_id]))[master_id].as_api_fields()
    assert fields["total_pnl"] == "-1.52"
    assert client.order_book_calls == 2


def _disagreeing_positions():
    positions = copy.deepcopy(MASTER_POSITIONS)
    positions[0]["realizedPnl"] = -1.0
    positions[0]["MtoM"] = -1.0
    return positions


@pytest.mark.asyncio
async def test_mismatch_emits_one_warning_per_account_per_trading_day(db_session, sample_accounts, mock_redis):
    master_id, _ = ids(sample_accounts)
    clients = {master_id: FakeBrokerClient(MASTER_ORDER_BOOK, _disagreeing_positions())}

    service = make_service(mock_redis, clients)
    first = (await service.summarize_accounts([master_id]))[master_id]
    await service.summarize_accounts([master_id])

    assert first.broker_check.status == "MISMATCH"
    assert first.as_api_fields()["total_pnl"] == "-1.52"  # Jettone's value is kept, not the broker's
    events = await mismatch_events(db_session)
    assert len(events) == 1
    assert events[0].severity.value == "WARNING"
    assert events[0].account_id == uuid.UUID(master_id)
    assert events[0].metadata_payload["trading_day"] == "2026-09-11"
    assert events[0].metadata_payload["realized"] == {
        "jettone": "-1.52", "broker": "-1.00", "diff": "-0.52", "status": "MISMATCH",
    }

    next_day = make_service(mock_redis, clients, now=NOW + timedelta(days=1))
    await next_day.summarize_accounts([master_id])
    assert len(await mismatch_events(db_session)) == 2


@pytest.mark.asyncio
async def test_mismatch_alert_dedup_survives_without_redis(db_session, sample_accounts):
    master_id, _ = ids(sample_accounts)
    clients = {master_id: FakeBrokerClient(MASTER_ORDER_BOOK, _disagreeing_positions())}
    await make_service(None, clients).summarize_accounts([master_id])
    await make_service(None, clients).summarize_accounts([master_id])  # e.g. after a restart
    assert len(await mismatch_events(db_session)) == 1


@pytest.mark.asyncio
async def test_square_off_row_at_average_price_zero_does_not_affect_pnl(db_session, sample_accounts, mock_redis):
    _, child_id = ids(sample_accounts)
    child_uuid = uuid.UUID(child_id)
    # Jettone's square-off endpoint stores its exit as COMPLETE at average_price 0,
    # and copy_trades carry master-derived estimated prices. Neither may leak in.
    db_session.add(Order(
        internal_order_id="JT_SQOFF_1", broker_order_id="26091100067096", account_id=child_uuid,
        source=OrderSource.SYSTEM, exchange="NSE", symbol="CANBK", side=OrderSide.SELL,
        order_type="MARKET", product_type="INTRADAY", quantity=10, filled_quantity=10,
        price=Decimal("0"), average_price=Decimal("0"), status=OrderStatus.COMPLETE,
    ))
    db_session.add(CopyTrade(
        master_order_id="26091100064718", follower_account_id=child_uuid, copy_event_id=uuid.uuid4(),
        symbol="CANBK", exchange="NSE", side=OrderSide.BUY, quantity=1, product_type="INTRADAY",
        estimated_price=Decimal("123.54"), client_order_id="JT__PNL_TEST_1", status=CopyTradeStatus.SUCCESS,
    ))
    await db_session.commit()

    clients = {child_id: FakeBrokerClient(CHILD_ORDER_BOOK, CHILD_POSITIONS)}
    fields = (await make_service(mock_redis, clients).summarize_accounts([child_id]))[child_id].as_api_fields()
    assert (fields["realized_pnl"], fields["total_pnl"]) == ("-1.48", "-1.48")
    assert fields["broker_check"]["status"] == "MATCH"


@pytest.mark.asyncio
async def test_realized_timeline_matches_summary_and_shares_the_broker_cache(sample_accounts, mock_redis):
    master_id, child_id = ids(sample_accounts)
    master_client = FakeBrokerClient(MASTER_ORDER_BOOK, MASTER_POSITIONS)
    child_client = FakeBrokerClient(CHILD_ORDER_BOOK, CHILD_POSITIONS)
    clients = {master_id: master_client, child_id: child_client}
    service = make_service(mock_redis, clients)

    summaries = await service.summarize_accounts([master_id, child_id])
    timelines = await service.realized_timelines([master_id, child_id])

    master_tl = timelines[master_id].as_api_fields()
    child_tl = timelines[child_id].as_api_fields()
    assert master_tl["status"] == "OK" and child_tl["status"] == "OK"
    assert master_tl["points"][-1]["cumulative_realized"] == summaries[master_id].as_api_fields()["realized_pnl"] == "-1.52"
    assert child_tl["points"][-1]["cumulative_realized"] == summaries[child_id].as_api_fields()["realized_pnl"] == "-1.48"
    # realized_timelines reused the order-book read summarize_accounts already cached.
    assert master_client.order_book_calls == 1
    assert child_client.order_book_calls == 1


@pytest.mark.asyncio
async def test_realized_timeline_unavailable_without_broker_client(sample_accounts, mock_redis):
    master_id, _ = ids(sample_accounts)
    fields = (await make_service(mock_redis, {}).realized_timelines([master_id]))[master_id].as_api_fields()
    assert fields == {"status": "UNAVAILABLE", "points": [], "notes": ["NO_BROKER_CLIENT"]}


@pytest.mark.asyncio
async def test_child_manual_trade_outside_jettone_is_included(sample_accounts, mock_redis):
    _, child_id = ids(sample_accounts)
    book = copy.deepcopy(CHILD_ORDER_BOOK)
    manual = copy.deepcopy(book[0])
    # Placed by the account holder in the Alice Blue app - Jettone has no row for it.
    book.insert(0, {**manual, "brokerOrderId": "26091100099001", "transactionType": "BUY", "quantity": 2,
                    "filledQuantity": 2, "averageTradedPrice": 123.20, "exchangeTimestamp": "2026-09-11 12:00:00"})
    book.insert(0, {**manual, "brokerOrderId": "26091100099002", "transactionType": "SELL", "quantity": 2,
                    "filledQuantity": 2, "averageTradedPrice": 123.30, "exchangeTimestamp": "2026-09-11 12:05:00"})
    positions = copy.deepcopy(CHILD_POSITIONS)
    positions[0]["realizedPnl"] = -1.2799999713897705
    positions[0]["MtoM"] = -1.28

    clients = {child_id: FakeBrokerClient(book, positions)}
    fields = (await make_service(mock_redis, clients).summarize_accounts([child_id]))[child_id].as_api_fields()
    assert (fields["realized_pnl"], fields["total_pnl"]) == ("-1.28", "-1.28")  # -1.48 + 2*(123.30-123.20)
    assert fields["broker_check"]["status"] == "MATCH"
