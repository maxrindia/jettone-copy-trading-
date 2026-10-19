"""
The generic MasterOrderPoller driven by the REAL Groww and Angel One adapters (mocked HTTP transports).

This proves the polling logic is broker-agnostic for these adapters - it does NOT make either broker eligible as a
Master: `as_master` stays false until the owner's capture confirms order-list paging, stable order ids and the
part-fill vocabulary (see docs/BROKER_PARITY_MATRIX.md). Nothing here contacts a broker.
"""
import uuid
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.core.config import settings
from app.models.trades import Order, OrderSource
from app.services.brokers.registry import can_be_master
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.reliability.master_order_poller import MasterOrderPoller
from conftest import TestingSessionLocal
import angel_helpers as A
import groww_helpers as G

MASTER_ID = "caf4726b-c48c-4e54-b899-ceb0f7259da8"
GROWW_STATUS = {"open": "OPEN", "partial": "OPEN", "complete": "EXECUTED", "rejected": "REJECTED", "cancelled": "CANCELLED"}
ANGEL_STATUS = {"open": "syn_open", "partial": "syn_part", "complete": "syn_done", "rejected": "syn_rej", "cancelled": "syn_can"}


class Driver:
    """One broker's real adapter plus a way to script its order book."""

    def __init__(self, name):
        self.name = name

    async def start(self, redis):
        if self.name == "groww":
            self.fake = G.FakeGroww()
            self.client = G.make_client(redis, self.fake, account_id=MASTER_ID)
        else:
            self.fake = A.FakeAngel()
            self.client = A.make_client(redis, self.fake, account_id=MASTER_ID)
        await self.client.login()
        return self

    def row(self, oid, status="open", qty=10, filled=None, side="BUY", otype="LIMIT", price=800):
        filled = (qty if status == "complete" else (4 if status == "partial" else 0)) if filled is None else filled
        if self.name == "groww":
            return G.order_row(oid, GROWW_STATUS[status], qty, filled, side=side, otype=otype, price=price, ref="JTx" + str(oid))
        return {"orderid": str(oid), "tradingsymbol": "SBIN-EQ", "symboltoken": "3045", "exchange": "NSE",
                "transactiontype": side, "ordertype": {"SL_M": "STOPLOSS_MARKET", "SL": "STOPLOSS_LIMIT"}.get(otype, otype),
                "producttype": "INTRADAY", "price": str(price), "triggerprice": "0", "quantity": str(qty),
                "filledshares": str(filled), "averageprice": str(price if filled else 0), "status": ANGEL_STATUS[status], "text": ""}

    def set_book(self, rows):
        if self.name == "groww":
            self.fake.order_list_response = G.ok({"order_list": rows})
        else:
            self.fake.routes["getOrderBook"] = (200, {"status": True, "data": rows})

    def set_raw(self, status, body):
        if self.name == "groww":
            self.fake.order_list_response = (status, body)
        else:
            self.fake.routes["getOrderBook"] = (status, body)


@pytest_asyncio.fixture(params=["groww", "angel"])
async def drv(request, mock_redis):
    return await Driver(request.param).start(mock_redis)


def make_poller(drv, mock_redis, dispatched):
    return MasterOrderPoller(
        master_account_id=MASTER_ID,
        client_provider=lambda _acc: drv.client,
        on_order_update=lambda event: dispatched.append(event),
        pipeline=CopyEnginePipeline(validator=EventValidator(redis=mock_redis)),
        check_interval_seconds=5,
    )


async def master_rows():
    async with TestingSessionLocal() as s:
        return (await s.execute(select(Order).where(Order.account_id == uuid.UUID(MASTER_ID)))).scalars().all()


@pytest.mark.asyncio
async def test_first_poll_is_a_baseline_and_dispatches_nothing(db_session, mock_redis, drv):
    drv.set_book([drv.row("1001", "complete"), drv.row("1002", "open")])
    dispatched = []
    poller = make_poller(drv, mock_redis, dispatched)
    assert await poller.poll_once() == 0
    assert dispatched == [] and poller.is_seeded
    assert {o.broker_order_id for o in await master_rows()} == {"1001", "1002"}      # history persisted, not replicated
    assert all(o.source == OrderSource.BROKER_EXTERNAL for o in await master_rows())


@pytest.mark.asyncio
async def test_a_new_order_after_the_baseline_is_dispatched_once_with_normalised_fields(db_session, mock_redis, drv):
    drv.set_book([drv.row("1001", "complete")])
    dispatched = []
    poller = make_poller(drv, mock_redis, dispatched)
    await poller.poll_once()
    drv.set_book([drv.row("1001", "complete"), drv.row("2001", "open", qty=7, price=801)])
    assert await poller.poll_once() == 1
    (ev,) = dispatched
    assert (ev.broker_order_id, ev.side, ev.quantity, ev.filled_quantity, ev.status, ev.order_type) == \
        ("2001", "BUY", 7, 0, "OPEN", "LIMIT")
    assert ev.price == Decimal("801") and ev.exchange == "NSE" and ev.account_id == MASTER_ID


@pytest.mark.asyncio
async def test_repeated_identical_polls_never_redispatch(db_session, mock_redis, drv):
    drv.set_book([])
    dispatched = []
    poller = make_poller(drv, mock_redis, dispatched)
    await poller.poll_once()
    drv.set_book([drv.row("3001", "open")])
    for _ in range(5):
        await poller.poll_once()
    assert len(dispatched) == 1                                                       # stable identity: one order, one dispatch


@pytest.mark.asyncio
async def test_partial_then_complete_dispatches_each_genuine_change_once(db_session, mock_redis, drv):
    drv.set_book([])
    dispatched = []
    poller = make_poller(drv, mock_redis, dispatched)
    await poller.poll_once()
    for state in ("open", "partial", "partial", "complete", "complete"):
        drv.set_book([drv.row("4001", state)])
        await poller.poll_once()
    assert [(e.status, e.filled_quantity) for e in dispatched] == [("OPEN", 0), ("PARTIAL", 4), ("COMPLETE", 10)]


@pytest.mark.asyncio
async def test_a_duplicate_row_in_one_response_dispatches_once(db_session, mock_redis, drv):
    drv.set_book([])
    dispatched = []
    poller = make_poller(drv, mock_redis, dispatched)
    await poller.poll_once()
    row = drv.row("5001", "open")
    drv.set_book([row, dict(row)])
    await poller.poll_once()
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_a_restart_rebaselines_and_never_replays_earlier_orders(db_session, mock_redis, drv):
    drv.set_book([])
    first_run = []
    p1 = make_poller(drv, mock_redis, first_run)
    await p1.poll_once()
    drv.set_book([drv.row("6001", "open")])
    await p1.poll_once()
    assert len(first_run) == 1
    # engine restart: a brand new poller sees the same book
    second_run = []
    p2 = make_poller(drv, mock_redis, second_run)
    assert await p2.poll_once() == 0 and second_run == []                           # 6001 is history now
    drv.set_book([drv.row("6001", "complete"), drv.row("6002", "open")])
    await p2.poll_once()
    assert [e.broker_order_id for e in second_run] == ["6002"]                      # 6001's later fill is NOT copied; 6002 is


@pytest.mark.asyncio
async def test_a_jettone_originated_order_is_persisted_but_never_dispatched(db_session, mock_redis, drv):
    drv.set_book([])
    dispatched = []
    poller = make_poller(drv, mock_redis, dispatched)
    await poller.poll_once()
    async with TestingSessionLocal() as s:
        s.add(Order(internal_order_id="JT_UI_1", broker_order_id="7001", account_id=uuid.UUID(MASTER_ID), source=OrderSource.INTERNAL_UI,
                    exchange="NSE", symbol="SBIN", side="BUY", order_type="LIMIT", product_type="MIS", quantity=10,
                    pending_quantity=10, price=Decimal("800"), status="OPEN", broker="ALICE_BLUE"))
        await s.commit()
    drv.set_book([drv.row("7001", "complete")])
    await poller.poll_once()
    assert dispatched == []


# ── failure handling: an unreadable response is a missed poll, never an empty book ────────────

MALFORMED = [
    ("http_500", 500, {}), ("not_json", 200, b"<html>gateway</html>"), ("failure_body", 200, {"status": "FAILURE"}),
    ("rows_not_a_list", 200, {"status": "SUCCESS", "payload": {"order_list": "x"}}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,status,body", MALFORMED, ids=[m[0] for m in MALFORMED])
async def test_an_unreadable_first_response_does_not_seed_and_old_orders_are_still_not_replicated(db_session, mock_redis, drv, name, status, body):
    if drv.name == "angel":
        body = {"status": False, "message": "x"} if isinstance(body, dict) else body
    drv.set_raw(status, body)
    dispatched = []
    poller = make_poller(drv, mock_redis, dispatched)
    assert await poller.poll_once() == 0 and not poller.is_seeded                 # not read as "an empty book"
    drv.set_book([drv.row("8001", "complete")])                                    # the first READABLE book becomes the baseline
    assert await poller.poll_once() == 0 and poller.is_seeded and dispatched == []


@pytest.mark.asyncio
async def test_a_mid_session_failure_loses_nothing_and_a_late_order_is_still_copied(db_session, mock_redis, drv):
    drv.set_book([drv.row("9001", "complete")])
    dispatched = []
    poller = make_poller(drv, mock_redis, dispatched)
    await poller.poll_once()
    drv.set_raw(500, {})
    assert await poller.poll_once() == 0
    drv.set_book([drv.row("9001", "complete"), drv.row("9002", "open")])
    assert await poller.poll_once() == 1
    assert [e.broker_order_id for e in dispatched] == ["9002"]


@pytest.mark.asyncio
async def test_one_unrecognised_status_makes_the_whole_poll_a_miss_not_a_partial_book(db_session, mock_redis, drv):
    drv.set_book([])
    dispatched = []
    poller = make_poller(drv, mock_redis, dispatched)
    await poller.poll_once()
    bad = drv.row("9101", "open")
    if drv.name == "groww":
        bad["order_status"] = "SOMETHING_NEW"
    else:
        bad["status"] = "syn_never_seen"
    drv.set_book([drv.row("9100", "open"), bad])
    assert await poller.poll_once() == 0 and dispatched == []                      # fail closed: nothing from a doubtful book


# ── stop-loss orders arrive in the common vocabulary the detector expects ─────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("raw_type,expected", [("SL", "SL"), ("SL_M", "SLM"), ("LIMIT", "LIMIT"), ("MARKET", "MARKET")])
async def test_order_types_use_the_common_vocabulary(db_session, mock_redis, drv, raw_type, expected):
    drv.set_book([])
    dispatched = []
    poller = make_poller(drv, mock_redis, dispatched)
    await poller.poll_once()
    row = drv.row("9201", "open", otype=raw_type)
    if drv.name == "groww" and raw_type in ("SL", "SL_M"):
        row["trigger_price"] = 795
    if drv.name == "angel" and raw_type in ("SL", "SL_M"):
        row["triggerprice"] = "795"
    drv.set_book([row])
    await poller.poll_once()
    assert dispatched and dispatched[0].order_type == expected


# ── Groww order-list paging boundary (the page base is NOT verified; a full page is never trusted) ──

@pytest.mark.asyncio
async def test_a_full_groww_page_is_a_missed_poll_not_a_complete_book(db_session, mock_redis):
    d = await Driver("groww").start(mock_redis)
    page = lambda n: [d.row(str(20000 + i), "complete") for i in range(n)]       # noqa: E731
    d.set_book(page(99))                                                          # just under a page: readable
    dispatched = []
    poller = make_poller(d, mock_redis, dispatched)
    assert await poller.poll_once() == 0 and poller.is_seeded
    d.set_book(page(99) + [d.row("29999", "open")])                               # exactly a full page: possibly truncated
    assert await poller.poll_once() == 0 and dispatched == []
    d.set_book(page(99) + [d.row("29998", "open")][:0])                           # back under a page, nothing new
    assert await poller.poll_once() == 0
