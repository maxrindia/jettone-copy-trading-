"""
R2B: the database is the authority on "this create event was already handled" - for MARKET, LIMIT and SL alike.

Redis dedupe (EventValidator, SET NX with a 24 h TTL) is only the first line. If it is bypassed - a flush, a
restart without persistence, eviction, an expired key - a redelivered create event must still be stopped by the
unique constraint on copy_trades (master_order_id, follower_account_id, copy_event_id).

The MARKET path always did this: it inserts the CopyTrade row FIRST and returns SKIPPED / DUPLICATE_EVENT on a
conflict. The LIMIT and SL paths used to pre-write the child Order first (save_or_update_order is an UPSERT that
overwrites broker_order_id, status and fill quantities) and then merely LOGGED the conflict and went on to send
a second broker order. They now claim the event with the CopyTrade row first, exactly like MARKET.

Every test bypasses Redis (an always-"not seen" double) so only the database layer is in play. Mocked broker
clients and an isolated in-memory database throughout: no Redis, no broker request, no production anything.
"""
import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.types import OrderResult, OrderResultCategory
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.reliability.retry import RetryPolicy
from conftest import TestingSessionLocal
from strategy_helpers import SAMPLE_MASTER_ACCOUNT_ID
from test_marketable_limit_strategy import _funds, _limit_event, _market_event

import angel_helpers as A
import groww_helpers as G
from test_angel_one_sell_safety import CHILD_IP as ANGEL_IP, _angel_child, _sell as angel_sell, angel_enabled  # noqa: F401
from test_groww_sell_safety import CHILD_IP as GROWW_IP, _groww_child, _positions as groww_positions, _sell as groww_sell, groww_enabled  # noqa: F401

KINDS = ["MARKET", "LIMIT", "SL"]


def bypassed_redis():
    r = AsyncMock()
    r.set = AsyncMock(return_value=True)          # Redis dedupe bypassed: every event looks new
    return r


def pipeline():
    return CopyEnginePipeline(validator=EventValidator(redis=bypassed_redis()))


def sl_event(mid):
    return MasterTradeEvent(
        account_id=SAMPLE_MASTER_ACCOUNT_ID, master_order_id=mid, symbol="RELIANCE", exchange="NSE", side="BUY",
        quantity=10, price=Decimal("99.50"), limit_price=Decimal("99.50"), trigger_price=Decimal("100.00"),
        product_type="MIS", timestamp=datetime.now(timezone.utc), fill_sequence=1, order_type="SL",
        event_type="SL_PENDING_CREATED", status="TRIGGER_PENDING", total_quantity=10, filled_quantity=0, pending_quantity=10,
    )


def make_event(kind, mid):
    return {
        "MARKET": lambda: _market_event(quantity=10, price=Decimal("100.00"), master_order_id=mid),
        "LIMIT": lambda: _limit_event(quantity=10, price=Decimal("100.00"), master_order_id=mid),
        "SL": lambda: sl_event(mid),
    }[kind]()


def ok(boid):
    return OrderResult(category=OrderResultCategory.SUCCESS, client_order_id=None, broker_order_id=boid, message="ok",
                       raw_response={"stat": "Ok"}, timestamp=datetime.now(timezone.utc))


def broker():
    """A mocked follower client: each place_order returns a DIFFERENT broker id, so an overwrite is visible."""
    c = AsyncMock()
    c.get_funds.return_value = _funds()
    c.get_positions.return_value = []
    c.get_positions_or_none.return_value = []
    c.place_order.side_effect = [ok("BRK_FIRST"), ok("BRK_SECOND"), ok("BRK_THIRD")]
    return c


async def deliver(event, client, follower):
    res = await pipeline().execute_replication(event=event, clients={str(follower.id): client})
    return (res.get("results") or {}).get(str(follower.id))


async def rows(master_order_id):
    async with TestingSessionLocal() as s:
        trades = (await s.execute(select(CopyTrade).where(CopyTrade.master_order_id == master_order_id))).scalars().all()
        orders = (await s.execute(select(Order).where(Order.master_broker_order_id == master_order_id))).scalars().all()
    return trades, orders


# ── 1 + 2. a duplicate create event is stopped by the database (Redis bypassed) ───────────

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_a_duplicate_create_event_sends_once_and_is_skipped(db_session, sample_accounts, sample_strategy, kind):
    f = sample_accounts["follower1"]; c = broker(); mid = f"R2B_DUP_{kind}"
    first = await deliver(make_event(kind, mid), c, f)
    second = await deliver(make_event(kind, mid), c, f)
    assert first["status"] in ("SUCCESS",) and c.place_order.await_count == 1
    assert second == {"status": "SKIPPED", "reason": "DUPLICATE_EVENT"}
    trades, orders = await rows(mid)
    assert len(trades) == 1 and len(orders) == 1


# ── 3. the existing child order survives the duplicate untouched ──────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_a_duplicate_does_not_overwrite_the_existing_child_order(db_session, sample_accounts, sample_strategy, kind):
    f = sample_accounts["follower1"]; c = broker(); mid = f"R2B_STATE_{kind}"
    await deliver(make_event(kind, mid), c, f)
    async with TestingSessionLocal() as s:                         # the fill syncer records 3 of 10 filled
        await s.execute(update(Order).where(Order.master_broker_order_id == mid)
                        .values(status=OrderStatus.PARTIAL, filled_quantity=3, pending_quantity=7))
        await s.commit()
    before = (await rows(mid))[1][0]
    snapshot = (before.status, before.broker_order_id, before.filled_quantity, before.pending_quantity, before.updated_at)
    assert snapshot[:4] == (OrderStatus.PARTIAL, "BRK_FIRST", 3, 7)

    assert (await deliver(make_event(kind, mid), c, f))["reason"] == "DUPLICATE_EVENT"

    after = (await rows(mid))[1][0]
    assert (after.status, after.broker_order_id, after.filled_quantity, after.pending_quantity) == (OrderStatus.PARTIAL, "BRK_FIRST", 3, 7)
    assert after.updated_at == before.updated_at                  # not even touched
    assert c.place_order.await_count == 1


# ── 4. a duplicate that arrives WHILE the first delivery is still in flight ───────────────
#
# The overlap that matters in production: delivery #1 has claimed the event and is waiting on the broker when a
# duplicate arrives. The claim is already committed, so #2 must be skipped without touching anything.
#
# Deliberately NOT an asyncio.gather of two deliveries: the test database is in-memory SQLite on a StaticPool,
# i.e. ONE shared connection for every session, so a loser's failed insert + rollback can also discard the
# winner's pending write. That is a property of this harness (the unchanged MARKET path fails a gather test
# identically on 5826bb9); production Postgres gives every session its own connection and an atomic unique
# constraint. A truly simultaneous commit race therefore needs a real-database integration test.

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_a_duplicate_arriving_while_the_first_is_in_flight_is_skipped(db_session, sample_accounts, sample_strategy, kind):
    f = sample_accounts["follower1"]; mid = f"R2B_INFLIGHT_{kind}"
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_place(order):
        if started.is_set():                                       # a SECOND send would be a regression:
            return ok("BRK_SECOND")                                # answer at once so the asserts fail, not hang
        started.set()
        await release.wait()                                       # the first send is "on the wire"
        return ok("BRK_FIRST")

    c = AsyncMock()
    c.get_funds.return_value = _funds()
    c.get_positions.return_value = []
    c.get_positions_or_none.return_value = []
    c.place_order = AsyncMock(side_effect=slow_place)

    first = asyncio.create_task(deliver(make_event(kind, mid), c, f))
    await asyncio.wait_for(started.wait(), timeout=10)             # delivery #1 is now mid-send
    duplicate = await asyncio.wait_for(deliver(make_event(kind, mid), c, f), timeout=10)   # #2 runs to completion meanwhile
    assert duplicate == {"status": "SKIPPED", "reason": "DUPLICATE_EVENT"}
    assert c.place_order.await_count == 1                          # still the one in-flight send
    release.set()
    done = await asyncio.wait_for(first, timeout=10)
    assert done["status"] == "SUCCESS" and c.place_order.await_count == 1
    trades, orders = await rows(mid)
    assert len(trades) == 1 and len(orders) == 1
    assert orders[0].broker_order_id == "BRK_FIRST"


# ── 5. a later, distinct event still processes ────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_a_later_distinct_event_still_processes_after_a_skipped_duplicate(db_session, sample_accounts, sample_strategy, kind):
    f = sample_accounts["follower1"]; c = broker()
    await deliver(make_event(kind, f"R2B_A_{kind}"), c, f)
    assert (await deliver(make_event(kind, f"R2B_A_{kind}"), c, f))["reason"] == "DUPLICATE_EVENT"
    later = await deliver(make_event(kind, f"R2B_B_{kind}"), c, f)
    assert later["status"] == "SUCCESS" and c.place_order.await_count == 2
    assert len((await rows(f"R2B_A_{kind}"))[0]) == 1 and len((await rows(f"R2B_B_{kind}"))[0]) == 1
    assert (await rows(f"R2B_B_{kind}"))[1][0].broker_order_id == "BRK_SECOND"


# ── 6 + 7. single-delivery controls: sends once, persists the state it always did ────────

CONTROL = {          # kind: (copy_trade status, client_order_id prefix, order status, order type)
    "MARKET": (CopyTradeStatus.SUBMITTED, "JT__", OrderStatus.OPEN, "LIMIT"),          # marketable LIMIT conversion
    "LIMIT": (CopyTradeStatus.PENDING, "JT_LIM_", OrderStatus.OPEN, "LIMIT"),
    "SL": (CopyTradeStatus.PENDING, "JT_SL_", OrderStatus.PENDING, "SL"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_a_single_delivery_sends_once_and_persists_the_expected_state(db_session, sample_accounts, sample_strategy, kind):
    f = sample_accounts["follower1"]; c = broker(); mid = f"R2B_ONE_{kind}"
    out = await deliver(make_event(kind, mid), c, f)
    ct_status, prefix, order_status, order_type = CONTROL[kind]
    assert out["status"] == "SUCCESS" and c.place_order.await_count == 1
    trades, orders = await rows(mid)
    assert len(trades) == 1 and len(orders) == 1
    (t,), (o,) = trades, orders
    assert t.status == ct_status and t.client_order_id.startswith(prefix) and t.submitted_at is not None
    assert o.status == order_status and o.broker_order_id == "BRK_FIRST" and o.order_type == order_type
    assert (o.quantity, o.filled_quantity, o.pending_quantity) == (10, 0, 10)
    sent = c.place_order.call_args.args[0]
    assert sent.client_order_id == t.client_order_id and sent.quantity == 10
    if kind == "SL":
        assert sent.trigger_price == Decimal("100.00") and o.trigger_price == Decimal("100.00")


async def deliver_fast(event, client, follower):
    """deliver() with a RetryPolicy that does not sleep, for tests where the broker call raises on every attempt."""
    fast = CopyEnginePipeline(validator=EventValidator(redis=bypassed_redis()), retry_policy=RetryPolicy(max_attempts=3, base_delay_sec=0.0))
    res = await fast.execute_replication(event=event, clients={str(follower.id): client})
    return (res.get("results") or {}).get(str(follower.id))


# ── a send that RAISED on every attempt: the claim stays as an unresolved TIMEOUT, no resend ─

@pytest.mark.asyncio
async def test_a_send_that_raised_on_every_attempt_leaves_an_unresolved_claim_and_is_never_resent(
        db_session, sample_accounts, sample_strategy):
    """What this tests (and what it does NOT): the claim and the child-Order pre-write both succeed, then the broker
    call RAISES on every attempt (RetryPolicy retries a raised exception, then reports UNKNOWN). The pipeline records
    an UNRESOLVED send - CopyTrade TIMEOUT with submitted_at stamped, child Order PENDING with broker_status TIMEOUT -
    and a later redelivery (Redis bypassed) is skipped with no broker call. It does NOT simulate a crash between the
    claim and the Order pre-write; that is test_failure_after_the_claim_commits_and_before_the_order_pre_write_*."""
    f = sample_accounts["follower1"]; mid = "R2B_RAISED_LIMIT"
    c = broker()
    c.place_order.side_effect = RuntimeError("the send raised")
    first = await deliver_fast(make_event("LIMIT", mid), c, f)
    assert first["status"] == "TIMEOUT" and c.place_order.await_count == 3        # a raised exception is retried by policy
    (trade,), (order,) = await rows(mid)
    assert trade.status == CopyTradeStatus.TIMEOUT and trade.submitted_at is not None
    assert order.status == OrderStatus.PENDING and order.broker_status == "TIMEOUT" and order.broker_order_id is None

    c2 = broker()
    assert await deliver_fast(make_event("LIMIT", mid), c2, f) == {"status": "SKIPPED", "reason": "DUPLICATE_EVENT"}
    assert c2.place_order.await_count == 0
    trades, orders = await rows(mid)
    assert len(trades) == 1 and len(orders) == 1 and trades[0].status == CopyTradeStatus.TIMEOUT


# ── client_order_id prefix collision: fails closed, the first order is untouched ──────────
#
# client_order_id is built from a TRUNCATED master order id (16 characters for LIMIT and SL, 24 for MARKET) and
# copy_trades.client_order_id is UNIQUE. Two DIFFERENT master orders that share that prefix therefore conflict on
# client_order_id although their (master_order_id, follower, copy_event_id) triples differ. The claim INSERT raises
# IntegrityError and the second order is skipped as DUPLICATE_EVENT: nothing is sent and the first order's row is
# not touched. (Before R2B the LIMIT/SL paths overwrote the first child Order through the upsert and then sent.)
#
# Documented consequence of failing closed: the second, distinct master order is NOT copied. Alice Blue order ids are
# short numerics, so this cannot happen there; a broker with long ids sharing a 16-character prefix would need a
# client_order_id derived from the FULL id (a follow-up, not part of R2B).

COLLIDING_MASTER_IDS = ("R2B_COLLISION_PREFIX_0001", "R2B_COLLISION_PREFIX_0002")      # identical first 24 characters


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_a_client_order_id_prefix_collision_fails_closed_and_leaves_the_first_order_untouched(
        db_session, sample_accounts, sample_strategy, kind):
    first_id, second_id = COLLIDING_MASTER_IDS
    assert first_id != second_id and first_id[:24] == second_id[:24]               # they really collide under every path
    f = sample_accounts["follower1"]; c = broker()

    assert (await deliver(make_event(kind, first_id), c, f))["status"] == "SUCCESS"
    async with TestingSessionLocal() as s:                                         # partial fill recorded on the first order
        await s.execute(update(Order).where(Order.master_broker_order_id == first_id)
                        .values(status=OrderStatus.PARTIAL, filled_quantity=3, pending_quantity=7))
        await s.commit()
    (first_trade,), (before,) = await rows(first_id)

    second = await deliver(make_event(kind, second_id), c, f)
    assert second == {"status": "SKIPPED", "reason": "DUPLICATE_EVENT"}           # fails closed
    assert c.place_order.await_count == 1                                          # the second order was never sent

    assert await rows(second_id) == ([], [])                                       # nothing was written for it
    (trade_after,), (after,) = await rows(first_id)
    assert trade_after.id == first_trade.id and trade_after.master_order_id == first_id
    assert (after.status, after.broker_order_id, after.filled_quantity, after.pending_quantity) == (OrderStatus.PARTIAL, "BRK_FIRST", 3, 7)
    assert after.updated_at == before.updated_at
    async with TestingSessionLocal() as s:                                         # still exactly one claim for the follower
        assert len((await s.execute(select(CopyTrade).where(CopyTrade.follower_account_id == f.id))).scalars().all()) == 1


# ── a failure AFTER the claim commits but BEFORE the Order pre-write ──────────────────────
#
# The claim is the first write; the child-Order pre-write (save_or_update_order) is the second. If the second fails
# the claim stays (PENDING, never submitted), nothing is sent, no Order row is created or altered, and a redelivery
# is skipped at the claim instead of "repairing" by sending. Fail closed: the stale-order sweeper flags a PENDING
# claim for review. A child Order row that already exists for this event is seeded to prove it is NOT overwritten.

def child_internal_id(kind, master_order_id, follower, event):
    acc8 = str(follower.id)[:8]
    return {"LIMIT": f"JT_CH_{master_order_id[:16]}_{acc8}",
            "SL": f"JT_CH_SL_{master_order_id[:16]}_{acc8}",
            "MARKET": f"JT_CH_{master_order_id[:16]}_{acc8}_{event.fill_sequence}"}[kind]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_failure_after_the_claim_commits_and_before_the_order_pre_write_keeps_the_claim_and_blocks_a_resend(
        db_session, sample_accounts, sample_strategy, monkeypatch, kind):
    f = sample_accounts["follower1"]; mid = f"R2B_PREWRITE_{kind}"; c = broker()
    event = make_event(kind, mid)
    async with TestingSessionLocal() as s:                                         # an existing child order the path would touch
        s.add(Order(id=uuid.uuid4(), internal_order_id=child_internal_id(kind, mid, f, event), broker_order_id="BRK_PRE",
                    account_id=f.id, source=OrderSource.SYSTEM, exchange="NSE", symbol="RELIANCE", side=OrderSide.BUY,
                    order_type="LIMIT", product_type="MIS", quantity=10, filled_quantity=3, pending_quantity=7,
                    price=Decimal("100.00"), status=OrderStatus.PARTIAL, master_broker_order_id=mid))
        await s.commit()
    (preexisting,) = (await rows(mid))[1]
    snapshot = (preexisting.status, preexisting.broker_order_id, preexisting.filled_quantity, preexisting.pending_quantity,
                preexisting.price, preexisting.updated_at)

    state = {"fail": True}
    original = CopyEnginePipeline.save_or_update_order

    async def flaky_pre_write(self, *args, **kwargs):
        if state["fail"]:
            raise RuntimeError("injected: the Order pre-write failed after the claim committed")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(CopyEnginePipeline, "save_or_update_order", flaky_pre_write)

    first = await deliver(event, c, f)
    assert first["status"] == "EXCEPTION" and "injected" in first["error"]
    trades, orders = await rows(mid)
    assert len(trades) == 1                                                        # the claim REMAINS ...
    assert trades[0].status == CopyTradeStatus.PENDING and trades[0].submitted_at is None and trades[0].broker_order_id is None
    assert c.place_order.await_count == 0                                          # ... and nothing was sent

    state["fail"] = False                                                          # the failure is gone; redeliver
    again = await deliver(make_event(kind, mid), c, f)
    assert again == {"status": "SKIPPED", "reason": "DUPLICATE_EVENT"}             # skipped at the claim, no "repair" send
    assert c.place_order.await_count == 0

    trades, orders = await rows(mid)
    assert len(trades) == 1 and len(orders) == 1
    now = orders[0]
    assert (now.status, now.broker_order_id, now.filled_quantity, now.pending_quantity, now.price, now.updated_at) == snapshot


# ── cross-broker: the same backstop for the other adapters (Redis bypassed) ───────────────

@pytest.mark.asyncio
async def test_groww_duplicate_limit_create_sends_one_request(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = G.FakeGroww()
    fake.positions_response = groww_positions(G.position_row(qty=10))
    client = G.make_client(mock_redis, fake, account_id=str(child.id), static_ip=GROWW_IP)
    await client.login()
    event = groww_sell(master, "R2B-GROWW-DUP")
    await pipeline().execute_replication(event=event, clients={str(child.id): client})
    again = await pipeline().execute_replication(event=event, clients={str(child.id): client})
    assert len(fake.bodies("/v1/order/create")) == 1
    assert again["results"][str(child.id)] == {"status": "SKIPPED", "reason": "DUPLICATE_EVENT"}


@pytest.mark.asyncio
async def test_angel_one_duplicate_limit_create_sends_one_request(db_session, mock_redis, angel_enabled):
    master, child = await _angel_child(db_session)
    fake = A.FakeAngel(positions=[A.position_row(net="10")])
    client = A.make_client(mock_redis, fake, account_id=str(child.id), static_ip=ANGEL_IP)
    await client.login()
    event = angel_sell(master, "R2B-ANGEL-DUP")
    await pipeline().execute_replication(event=event, clients={str(child.id): client})
    again = await pipeline().execute_replication(event=event, clients={str(child.id): client})
    assert len(fake.order_bodies()) == 1
    assert again["results"][str(child.id)] == {"status": "SKIPPED", "reason": "DUPLICATE_EVENT"}
