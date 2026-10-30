"""
Groww order-book PAGINATION, offline. Groww's HTTP is a fake that honours `page` and `page_size` (0-based, newest first, an empty list at
the end - the shape seen live on 2026-10-30 at page_size=2) and can fail, time out or shift on a chosen page. Nothing contacts Groww.

What this proves is what Jettone does with those answers: it returns the COMPLETE book or raises, it never returns a half-read book, and
the Master monitor therefore never mistakes a missing order for a cancelled one. It does not prove how Groww behaves at 100 or more rows;
that has not been seen live.
"""
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.audit import SystemEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.groww import client as groww_client
from app.services.groww import contract as C
from app.services.reliability.follower_fill_syncer import FollowerFillSyncer
from app.services.reliability.master_order_poller import BLIND_ALERT_AFTER_FAILURES, MasterOrderPoller
from conftest import TestingSessionLocal
from groww_helpers import Clock, FakeGroww, make_client, order_row
from strategy_helpers import ensure_account
from test_groww_equity_flow import StatefulGroww, child_order, evt, place, send
from test_groww_equity_lifecycle import groww_enabled  # noqa: F401
from app.models.trades import OrderStatus

MASTER_ID = "caf4726b-c48c-4e54-b899-ceb0f7259da8"
SIZE = C.ORDER_LIST_PAGE_SIZE
pytestmark = pytest.mark.usefixtures("cross_broker_pairs_enabled")


def book(n, *, start=0, status="EXECUTED"):
    """n orders, NEWEST FIRST, ids G00000-style so their order is easy to read."""
    return [order_row(f"G{i:05d}", status, 10, 10 if status == "EXECUTED" else 0, ref=f"JT{i:018d}") for i in range(start + n - 1, start - 1, -1)]


class PagedGroww(FakeGroww):
    """An order list that honours page / page_size, per segment, with hooks to break or move it."""

    def __init__(self, cash=None, fno=None):
        super().__init__()
        self.rows = {"CASH": list(cash or []), "FNO": list(fno or [])}
        self.script = {}            # (segment, page) -> rows (or a callable returning rows) replacing the real page
        self.fail = {}              # (segment, page) -> (http status, body) or an exception to raise
        self.on_request = None      # callable(segment, page, call_number) run before each page is served
        self.log = []               # (segment, page, page_size) for every list request

    async def __call__(self, request):
        if request.url.path != "/v1/order/list":
            return await super().__call__(request)
        self.requests.append(request)
        q = request.url.params
        seg, page, size = q.get("segment"), int(q.get("page", "0")), int(q.get("page_size", "100"))
        self.log.append((seg, page, size))
        if self.on_request:
            self.on_request(seg, page, len(self.log))
        failure = self.fail.get((seg, page))
        if isinstance(failure, Exception):
            raise failure
        if failure is not None:
            return httpx.Response(failure[0], json=failure[1])
        scripted = self.script.get((seg, page))
        if scripted is not None:
            chunk = scripted() if callable(scripted) else scripted
        else:
            chunk = self.rows[seg][page * size:(page + 1) * size]
        return httpx.Response(200, json={"status": "SUCCESS", "payload": {"order_list": chunk}})

    def requests_for(self, seg="CASH"):
        return [x for x in self.log if x[0] == seg]


async def logged_in(redis, fake, **kw):
    client = make_client(redis, fake, **kw)
    await client.login()
    return client


def ids(records):
    return [r.broker_order_id for r in records]


# ── sizes: the page boundaries ───────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("n", [0, 1, 4, 99, 100, 101, 199, 200, 201, 250, 500])
async def test_every_book_size_is_read_completely_in_order_with_no_repeats(mock_redis, n):
    fake = PagedGroww(cash=book(n))
    c = await logged_in(mock_redis, fake)
    records = await c.get_order_book()
    assert ids(records) == [f"G{i:05d}" for i in range(n - 1, -1, -1)]                      # all of them, newest first, each once
    pages = n // SIZE + 1                                                                    # a short page - an empty one when n is a multiple
    expected_requests = 1 if n < SIZE else pages + 1                                         # a multi-page read re-checks page 0 once
    assert len(fake.requests_for("CASH")) == expected_requests
    assert [r[1] for r in fake.requests_for("CASH")][:pages] == list(range(pages))           # page 0, 1, 2 ... in order
    assert {r[2] for r in fake.requests_for("CASH")} == {SIZE} and {r[0] for r in fake.requests_for("CASH")} == {"CASH"}


@pytest.mark.asyncio
async def test_a_small_book_is_still_a_single_request(mock_redis):
    fake = PagedGroww(cash=book(4))
    c = await logged_in(mock_redis, fake)
    assert len(await c.get_order_book()) == 4 and fake.log == [("CASH", 0, SIZE)]


@pytest.mark.asyncio
async def test_exactly_one_full_page_needs_the_next_page_to_prove_it_is_the_end(mock_redis):
    fake = PagedGroww(cash=book(SIZE))
    c = await logged_in(mock_redis, fake)
    assert len(await c.get_order_book()) == SIZE
    assert [r[1] for r in fake.log][:2] == [0, 1]                                            # page 1 (empty) is what ends it


# ── duplicates ───────────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_identical_repeat_across_pages_is_dropped(mock_redis):
    rows = book(150)
    fake = PagedGroww(cash=rows)
    fake.script[("CASH", 1)] = [rows[SIZE - 1]] + rows[SIZE:]                                # the last row of page 0 shows again on page 1
    c = await logged_in(mock_redis, fake)
    records = await c.get_order_book()
    assert ids(records) == [r["groww_order_id"] for r in rows] and len(set(ids(records))) == 150


@pytest.mark.asyncio
async def test_the_same_order_seen_twice_with_different_content_is_refused_after_the_bounded_retries(mock_redis):
    rows = book(150)
    fake = PagedGroww(cash=rows)
    moved = dict(rows[SIZE - 1], order_status="CANCELLED", filled_quantity=0, remaining_quantity=10)
    fake.script[("CASH", 1)] = [moved] + rows[SIZE:]
    c = await logged_in(mock_redis, fake)
    with pytest.raises(groww_client.GrowwOrderListTruncated):
        await c.get_order_book()
    assert len(fake.requests_for("CASH")) == C.ORDER_LIST_SCAN_ATTEMPTS * 2                  # 3 attempts x 2 pages, then fail closed


@pytest.mark.asyncio
async def test_a_repeated_row_inside_one_response_is_dropped_when_identical_and_refused_when_not(mock_redis):
    row = book(1)[0]
    fake = PagedGroww()
    fake.script[("CASH", 0)] = [row, dict(row)]
    c = await logged_in(mock_redis, fake)
    assert ids(await c.get_order_book()) == ["G00000"]
    fake.script[("CASH", 0)] = [row, dict(row, order_status="CANCELLED", filled_quantity=0, remaining_quantity=10)]
    with pytest.raises(groww_client.GrowwOrderListTruncated):
        await c.get_order_book()


# ── anything wrong with ANY page fails the whole read ────────────────────────────────────────────────────────────────────────

BAD_PAGES = {
    "http-500": (500, {}),
    "failure-body": (200, {"status": "FAILURE"}),
    "no-order-list": (200, {"status": "SUCCESS", "payload": {}}),
    "order-list-not-a-list": (200, {"status": "SUCCESS", "payload": {"order_list": "nope"}}),
    "row-not-an-object": (200, {"status": "SUCCESS", "payload": {"order_list": ["x"]}}),
    "row-without-id": (200, {"status": "SUCCESS", "payload": {"order_list": [{"order_status": "OPEN"}]}}),
    "row-with-boolean-id": (200, {"status": "SUCCESS", "payload": {"order_list": [dict(book(1)[0], groww_order_id=True)]}}),
    "row-with-blank-id": (200, {"status": "SUCCESS", "payload": {"order_list": [dict(book(1)[0], groww_order_id="  ")]}}),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("page", [0, 1, 2])
@pytest.mark.parametrize("kind", sorted(BAD_PAGES))
async def test_a_bad_page_anywhere_fails_the_whole_read_and_returns_no_partial_book(mock_redis, kind, page):
    fake = PagedGroww(cash=book(250))
    fake.fail[("CASH", page)] = BAD_PAGES[kind]
    c = await logged_in(mock_redis, fake)
    with pytest.raises(Exception) as err:
        await c.get_order_book()                                                             # raises: there is no return value to misuse
    assert isinstance(err.value, groww_client.GrowwError)


@pytest.mark.asyncio
@pytest.mark.parametrize("page", [0, 1, 2])
async def test_a_timeout_on_any_page_fails_the_whole_read(mock_redis, page):
    fake = PagedGroww(cash=book(250))
    fake.fail[("CASH", page)] = httpx.ReadTimeout("no answer")
    c = await logged_in(mock_redis, fake)
    with pytest.raises(httpx.TimeoutException):
        await c.get_order_book()


@pytest.mark.asyncio
async def test_a_failure_leaves_no_state_behind_and_the_next_read_is_complete(mock_redis):
    fake = PagedGroww(cash=book(250))
    fake.fail[("CASH", 1)] = (500, {})
    c = await logged_in(mock_redis, fake)
    with pytest.raises(groww_client.GrowwError):
        await c.get_order_book()
    del fake.fail[("CASH", 1)]
    assert len(await c.get_order_book()) == 250


@pytest.mark.asyncio
async def test_a_rate_limit_refusal_in_the_middle_of_a_scan_fails_the_read(mock_redis, monkeypatch):
    fake = PagedGroww(cash=book(450))
    c = await logged_in(mock_redis, fake)
    real, calls = c._reads_limiter.acquire, {"n": 0}

    async def limited():
        calls["n"] += 1
        if calls["n"] == 3:
            raise groww_client.GrowwRateLimited("slow down")
        await real()

    monkeypatch.setattr(c._reads_limiter, "acquire", limited)
    with pytest.raises(groww_client.GrowwRateLimited):
        await c.get_order_book()


# ── bounds ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_page_limit_refuses_a_book_it_cannot_finish_reading(mock_redis):
    fake = PagedGroww(cash=book(SIZE * (C.ORDER_LIST_MAX_PAGES + 5)))
    c = await logged_in(mock_redis, fake)
    with pytest.raises(groww_client.GrowwOrderListTruncated):
        await c.get_order_book()
    assert len(fake.requests_for("CASH")) == C.ORDER_LIST_MAX_PAGES                          # it stopped asking, it did not run on


@pytest.mark.asyncio
async def test_the_largest_book_the_limit_can_prove_complete_is_read_and_one_more_page_is_refused(mock_redis):
    biggest = SIZE * (C.ORDER_LIST_MAX_PAGES - 1) + (SIZE - 1)
    fake = PagedGroww(cash=book(biggest))
    c = await logged_in(mock_redis, fake)
    assert len(await c.get_order_book()) == biggest
    fake = PagedGroww(cash=book(SIZE * C.ORDER_LIST_MAX_PAGES))
    c = await logged_in(mock_redis, fake)
    with pytest.raises(groww_client.GrowwOrderListTruncated):                                # an exact multiple at the limit cannot be proven complete
        await c.get_order_book()


@pytest.mark.asyncio
async def test_a_full_page_that_never_advances_is_refused_instead_of_looping(mock_redis):
    fake = PagedGroww()
    fake.script.update({("CASH", p): book(SIZE) for p in range(10)})                         # the server ignores `page`: every page is the same
    c = await logged_in(mock_redis, fake)
    with pytest.raises(groww_client.GrowwOrderListTruncated):
        await c.get_order_book()
    assert len(fake.requests_for("CASH")) == 2


@pytest.mark.asyncio
async def test_a_read_that_takes_too_long_is_refused(mock_redis):
    clock = Clock()
    fake = PagedGroww(cash=book(450))
    fake.on_request = lambda seg, page, n: setattr(clock, "t", clock.t + C.ORDER_LIST_READ_DEADLINE_SECONDS / 2 + 1)
    c = await logged_in(mock_redis, fake, clock=clock)
    with pytest.raises(groww_client.GrowwOrderListTruncated):
        await c.get_order_book()


# ── the book moving while it is read ─────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_new_order_arriving_between_pages_is_caught_by_the_recheck_and_the_read_settles(mock_redis):
    fake = PagedGroww(cash=book(250))
    fired = {"done": False}

    def new_order_before_page_1(seg, page, n):
        if page == 1 and not fired["done"]:
            fired["done"] = True
            fake.rows["CASH"].insert(0, order_row("GNEW01", "OPEN", 10, 0, ref="JT" + "9" * 18))   # a newer order pushes every row down

    fake.on_request = new_order_before_page_1
    c = await logged_in(mock_redis, fake)
    got = ids(await c.get_order_book())
    assert got[0] == "GNEW01" and len(got) == 251 and len(set(got)) == 251                   # complete, with the new order, no repeats, none skipped


@pytest.mark.asyncio
async def test_a_book_that_never_stops_moving_is_refused(mock_redis):
    fake = PagedGroww(cash=book(250))
    counter = {"n": 0}

    def always_a_new_order(seg, page, n):
        if page == 1:
            counter["n"] += 1
            fake.rows["CASH"].insert(0, order_row(f"GMOVE{counter['n']:02d}", "OPEN", 10, 0, ref=f"JT{counter['n']:018d}"))

    fake.on_request = always_a_new_order
    c = await logged_in(mock_redis, fake)
    with pytest.raises(groww_client.GrowwOrderListTruncated):
        await c.get_order_book()


# ── CASH and FNO are independent ─────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cash_and_fno_are_paged_independently(mock_redis):
    fake = PagedGroww(cash=book(250), fno=book(120, start=5000))
    c = await logged_in(mock_redis, fake)
    cash = await c._read_order_rows("CASH")
    fno = await c._read_order_rows("FNO")
    assert len(cash) == 250 and len(fno) == 120 and not ({r["groww_order_id"] for r in cash} & {r["groww_order_id"] for r in fno})
    assert {r[1] for r in fake.requests_for("CASH")} == {0, 1, 2} and {r[1] for r in fake.requests_for("FNO")} == {0, 1}


@pytest.mark.asyncio
async def test_a_failure_in_one_segment_does_not_touch_the_other(mock_redis):
    fake = PagedGroww(cash=book(250), fno=book(120, start=5000))
    fake.fail[("FNO", 1)] = (500, {})
    c = await logged_in(mock_redis, fake)
    assert len(await c._read_order_rows("CASH")) == 250
    with pytest.raises(groww_client.GrowwError):
        await c._read_order_rows("FNO")


@pytest.mark.asyncio
async def test_an_empty_book_in_either_segment_is_an_empty_list_in_one_request(mock_redis):
    fake = PagedGroww()
    c = await logged_in(mock_redis, fake)
    assert await c._read_order_rows("CASH") == [] and await c._read_order_rows("FNO") == []
    assert fake.log == [("CASH", 0, SIZE), ("FNO", 0, SIZE)]


# ── the Master monitor on a book of more than 100 orders ─────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def master_account(db_session):
    return await ensure_account(db_session, MASTER_ID)


async def make_poller(redis, fake, dispatched):
    client = await logged_in(redis, fake, account_id=MASTER_ID)
    poller = MasterOrderPoller(master_account_id=MASTER_ID, client_provider=lambda _id: client, on_order_update=lambda e: dispatched.append(e),
                               pipeline=CopyEnginePipeline(validator=EventValidator(redis=redis)), check_interval_seconds=5)
    return client, poller


async def event_types(kind):
    async with TestingSessionLocal() as s:
        return [e.event_type for e in (await s.execute(select(SystemEvent).where(SystemEvent.event_type == kind))).scalars().all()]


@pytest.mark.asyncio
async def test_a_large_book_is_baselined_never_copied_and_a_new_order_is_copied_once(db_session, master_account, mock_redis):
    fake, dispatched = PagedGroww(cash=book(250)), []
    client, poller = await make_poller(mock_redis, fake, dispatched)
    assert await poller.poll_once() == 0 and poller.is_seeded and dispatched == []
    for _ in range(5):
        assert await poller.poll_once() == 0                                                  # repeated polls repeat nothing
    fake.rows["CASH"].insert(0, order_row("GNEW01", "OPEN", 10, 0, ref="JT" + "8" * 18))
    assert await poller.poll_once() == 1 and [e.broker_order_id for e in dispatched] == ["GNEW01"]
    for _ in range(5):
        assert await poller.poll_once() == 0
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_a_restart_over_a_large_book_baselines_all_of_it_and_copies_none(db_session, master_account, mock_redis):
    fake, dispatched = PagedGroww(cash=book(250)), []
    client, first = await make_poller(mock_redis, fake, dispatched)
    await first.poll_once()
    restarted = MasterOrderPoller(master_account_id=MASTER_ID, client_provider=lambda _id: client, on_order_update=lambda e: dispatched.append(e),
                                  pipeline=first.pipeline, check_interval_seconds=5)
    assert await restarted.poll_once() == 0 and restarted.is_seeded and dispatched == []     # nothing from the old book is replayed
    fake.rows["CASH"].insert(0, order_row("GNEW02", "OPEN", 10, 0, ref="JT" + "7" * 18))
    assert await restarted.poll_once() == 1 and [e.broker_order_id for e in dispatched] == ["GNEW02"]


@pytest.mark.asyncio
async def test_a_change_to_an_order_that_has_sunk_below_page_zero_is_detected_once(db_session, master_account, mock_redis):
    """An order placed AFTER the baseline is watched. Newer orders push it down the list; when it is then cancelled the change is on a
    later page, and is found and reported exactly once. (An order that was already in the baseline is never dispatched: that rule is
    unchanged.)"""
    fake, dispatched = PagedGroww(cash=book(10)), []
    client, poller = await make_poller(mock_redis, fake, dispatched)
    await poller.poll_once()
    fake.rows["CASH"].insert(0, order_row("GWATCH", "OPEN", 10, 0, ref="JT" + "5" * 18))
    assert await poller.poll_once() == 1
    for i in range(SIZE + 20):                                                               # newer orders push GWATCH onto page 1
        fake.rows["CASH"].insert(0, order_row(f"GPUSH{i:03d}", "OPEN", 10, 0, ref=f"JT{4000 + i:018d}"))
    assert await poller.poll_once() == SIZE + 20
    assert [r["groww_order_id"] for r in fake.rows["CASH"]].index("GWATCH") >= SIZE
    dispatched.clear()
    fake.rows["CASH"][[r["groww_order_id"] for r in fake.rows["CASH"]].index("GWATCH")] = order_row("GWATCH", "CANCELLED", 10, 0, ref="JT" + "5" * 18)
    assert await poller.poll_once() == 1 and await poller.poll_once() == 0
    assert [(e.broker_order_id, e.status) for e in dispatched] == [("GWATCH", "CANCELLED")]  # found below page 0, reported once


@pytest.mark.asyncio
async def test_a_failed_later_page_changes_nothing_and_is_never_read_as_cancellations(db_session, master_account, mock_redis):
    fake, dispatched = PagedGroww(cash=book(250)), []
    client, poller = await make_poller(mock_redis, fake, dispatched)
    await poller.poll_once()
    before = dict(poller._seen_state)
    fake.fail[("CASH", 2)] = (500, {})
    for _ in range(BLIND_ALERT_AFTER_FAILURES):
        assert await poller.poll_once() == 0
    assert dispatched == [] and poller._seen_state == before                                 # the orders on pages 0-1 were not "missing"
    assert await event_types("MASTER_MONITOR_BLIND") == ["MASTER_MONITOR_BLIND"]             # reported once, not silent
    del fake.fail[("CASH", 2)]
    assert await poller.poll_once() == 0 and dispatched == []                                # recovered: nothing is replayed
    assert await event_types("MASTER_MONITOR_RECOVERED") == ["MASTER_MONITOR_RECOVERED"]


@pytest.mark.asyncio
async def test_a_failed_first_read_does_not_seed_a_partial_baseline(db_session, master_account, mock_redis):
    fake, dispatched = PagedGroww(cash=book(250)), []
    fake.fail[("CASH", 1)] = (500, {})
    client, poller = await make_poller(mock_redis, fake, dispatched)
    assert await poller.poll_once() == 0 and not poller.is_seeded                            # nothing was baselined from a half-read book
    del fake.fail[("CASH", 1)]
    assert await poller.poll_once() == 0 and poller.is_seeded and dispatched == []           # the full book becomes the baseline


# ── a Groww CHILD with a large book ──────────────────────────────────────────────────────────────────────────────────────────

def paged_child_transport(fake, filler):
    """The Child's real order sits BELOW 120 newer orders, so it is on page 1: the order list is served from `filler + the Child's book`."""
    async def serve(request):
        if request.url.path == "/v1/order/list":
            q = request.url.params
            page, size = int(q.get("page", "0")), int(q.get("page_size", "100"))
            rows = filler + [fake.row(o) for o in fake.book]
            return httpx.Response(200, json={"status": "SUCCESS", "payload": {"order_list": rows[page * size:(page + 1) * size]}})
        return await StatefulGroww.__call__(fake, request)
    return httpx.MockTransport(serve)


@pytest.mark.asyncio
async def test_the_fill_syncer_finds_a_childs_order_that_sits_on_a_later_page(db_session, mock_redis, groww_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    client._http = httpx.AsyncClient(transport=paged_child_transport(fake, book(120, start=7000)))
    fake.fill(fake.only, 10)
    await FollowerFillSyncer(follower_clients_provider=lambda: {str(child.id): client}, pipeline=pipeline).sync_once()
    order = await child_order(child)
    assert order.status == OrderStatus.COMPLETE and order.filled_quantity == 10


@pytest.mark.asyncio
async def test_a_cancel_is_verified_through_a_book_of_more_than_one_page(db_session, mock_redis, groww_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    client._http = httpx.AsyncClient(transport=paged_child_transport(fake, book(120, start=7000)))
    await send(pipeline, client, child, evt(created, event_type="ORDER_CANCELLED", status="CANCELLED"))
    assert len(fake.bodies("/v1/order/cancel")) == 1 and (await child_order(child)).status == OrderStatus.CANCELLED
