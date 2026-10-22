"""
R3b: safe recovery after an engine restart (restart_recovery.py + the poller's seed pass + pipeline fanout-start record).

What is pinned here:
  D1  a durable COPY_FANOUT_STARTED record is written BEFORE any Child task starts; if it cannot be written the fanout
      still runs but never qualifies for recovery;
  D2  an order that already has Children is not baselined: a cancel / modify made while the engine was down reaches
      them (decided from the Children's state, so repeating startup repeats nothing); LIMIT->MARKET, a triggered SL, a
      MARKET fill delta and a quantity change are NEVER replayed;
  D3  a provable partial fanout (flag ON, 10-minute window of OUR clock, record + persisted row + fresh book agree, order
      still working and unfilled, live trading and master toggle ON) is resumed only for Children with no decision,
      through the normal gates and the database claim - once, however often recovery runs;
  D4  never-observed and not-fanned-out orders are reported (deduplicated) and never copied;
  and the seed pass itself fails closed (an unreadable first book does not seed an empty baseline).

Mocked brokers, in-memory database, no network. The PostgreSQL race test needs R2B_POSTGRES_TEST_URL.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from app.models.accounts import Account, AccountType
from app.models.audit import AuditLog, SystemEvent
from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.types import OrderRecord, OrderSide as BookSide
from app.services.copy_engine import child_outcomes as co
from app.services.copy_engine.child_outcomes import FANOUT_STARTED_EVENT_TYPE
from app.services.copy_engine.events import derive_copy_event_id
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.reliability import restart_recovery as rr
from app.services.reliability.master_order_poller import MasterOrderPoller
from app.services.reliability.restart_recovery import RestartRecovery
from conftest import TestingSessionLocal
from strategy_helpers import SAMPLE_MASTER_ACCOUNT_ID, link_strategy
from test_child_execution_tracking import account, client_returning, ok, result, run
from test_duplicate_create_db_backstop import KINDS, bypassed_redis, make_event, pipeline, rows
from app.services.alice_blue.types import OrderResultCategory

LIMIT_PRICE = Decimal("100.00")


class StaggeredPipeline(CopyEnginePipeline):
    """The test database is SQLite on ONE shared connection, so Children that commit at the same instant can
    interleave ("cannot commit transaction - SQL statements in progress"). Children are therefore started apart here;
    genuinely simultaneous processing is the PostgreSQL test at the end of this file."""
    async def execute_replication(self, *args, **kwargs):
        kwargs.setdefault("stagger_ms", 250)
        return await super().execute_replication(*args, **kwargs)


def spipe(**kwargs):
    kwargs.setdefault("validator", EventValidator(redis=bypassed_redis()))
    return StaggeredPipeline(**kwargs)


# ── builders ─────────────────────────────────────────────────────────────────────────────────

def book_rec(oid, *, status="OPEN", order_type="LIMIT", qty=10, price="100.00", trigger="0", filled=0, symbol="RELIANCE",
             side=BookSide.BUY):
    return OrderRecord(
        broker_order_id=oid, client_order_id=None, symbol=symbol, exchange="NSE", side=side, quantity=qty,
        filled_quantity=filled, average_price=Decimal("0"), status=status, order_timestamp=datetime.now(timezone.utc),
        order_type=order_type, product_type="MIS", price=Decimal(price), trigger_price=Decimal(trigger))


async def seed_master_row(master_id, oid, *, status=OrderStatus.OPEN, order_type="LIMIT", qty=10, price="100.00",
                          trigger="0", filled=0, source=OrderSource.BROKER_EXTERNAL, symbol="RELIANCE", side=OrderSide.BUY):
    async with TestingSessionLocal() as s:
        s.add(Order(id=uuid.uuid4(), internal_order_id=f"EXT_{oid}_{str(master_id)[:8]}", broker_order_id=oid,
                    account_id=master_id, source=source, exchange="NSE", symbol=symbol, side=side, order_type=order_type,
                    product_type="MIS", quantity=qty, filled_quantity=filled, pending_quantity=qty - filled,
                    price=Decimal(price), trigger_price=Decimal(trigger), status=status))
        await s.commit()


async def seed_started(master_id, oid, child_ids, *, kind="LIMIT", age_seconds=30, overrides=None, strategy_id=None):
    event = make_event(kind, oid)
    meta = co.fanout_started_metadata(event, strategy_id, [str(c) for c in child_ids])
    meta["recorded_at"] = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    meta.update(overrides or {})
    async with TestingSessionLocal() as s:
        s.add(AuditLog(event_type=FANOUT_STARTED_EVENT_TYPE, account_id=master_id, master_order_id=oid, symbol=event.symbol,
                       exchange=event.exchange, side=event.side, quantity=event.quantity, price=event.price,
                       reason_code=f"{event.event_type}#{event.fill_sequence}", metadata_payload=meta))
        await s.commit()


async def seed_decision(child_id, oid, *, status=CopyTradeStatus.SUBMITTED, seq=1, with_order=False, order_price="100.00",
                        order_status=OrderStatus.OPEN, trigger="0", order_type="LIMIT"):
    """A Child that already has a decision for the Master order (and optionally its own working Order)."""
    async with TestingSessionLocal() as s:
        s.add(CopyTrade(id=uuid.uuid4(), master_order_id=oid, follower_account_id=child_id,
                        copy_event_id=derive_copy_event_id(oid, str(child_id), seq), symbol="RELIANCE", exchange="NSE",
                        side=OrderSide.BUY, quantity=10, product_type="MIS", estimated_price=LIMIT_PRICE,
                        client_order_id=f"JT_LIM_{uuid.uuid4().hex[:12]}" if status != CopyTradeStatus.SKIPPED else None,
                        broker_order_id=f"CH_{uuid.uuid4().hex[:8]}" if status == CopyTradeStatus.SUBMITTED else None,
                        status=status))
        if with_order:
            s.add(Order(id=uuid.uuid4(), internal_order_id=f"JT_CH_{oid[:16]}_{str(child_id)[:8]}",
                        broker_order_id=f"CH_{uuid.uuid4().hex[:8]}", account_id=child_id, source=OrderSource.SYSTEM,
                        exchange="NSE", symbol="RELIANCE", side=OrderSide.BUY, order_type=order_type, product_type="MIS",
                        quantity=10, filled_quantity=0, pending_quantity=10, price=Decimal(order_price),
                        trigger_price=Decimal(trigger), status=order_status, master_broker_order_id=oid))
        await s.commit()


async def events(event_type):
    async with TestingSessionLocal() as s:
        return (await s.execute(select(SystemEvent).where(SystemEvent.event_type == event_type))).scalars().all()


async def trade_rows(oid):
    async with TestingSessionLocal() as s:
        return {t.follower_account_id: t for t in
                (await s.execute(select(CopyTrade).where(CopyTrade.master_order_id == oid))).scalars().all()}


@pytest_asyncio.fixture
async def world(db_session, sample_accounts):
    master = sample_accounts["master"]
    kids = {n: await account(db_session, n) for n in ("A", "B", "C")}
    strategy = await link_strategy(db_session, master, list(kids.values()))
    clients = {str(k.id): client_returning(ok(f"BRK_{n}")) for n, k in kids.items()}
    return dict(master=master, kids=kids, strategy=strategy, clients=clients)


def recovery_for(world, clients=None, pl=None):
    chosen = world["clients"] if clients is None else clients
    return RestartRecovery(pipeline=pl or spipe(), redis=AsyncMock(), clients_provider=lambda master_id: chosen)


@pytest.fixture
def resume_on(monkeypatch):
    monkeypatch.setattr("app.services.reliability.restart_recovery.settings.ENGINE_RECOVERY_RESUME_ENABLED", True)
    monkeypatch.setattr("app.services.reliability.restart_recovery.is_live_trading_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr("app.services.reliability.restart_recovery.is_master_trading_enabled", AsyncMock(return_value=True))


class Book:
    """A mocked Master client: the parsed book, plus the raw read the seed pass uses to prove the fetch succeeded."""
    def __init__(self, records):
        self.records, self.raw_ok = list(records), True
        self.client = MagicMock()
        self.client.get_order_book = AsyncMock(side_effect=lambda: list(self.records))
        self.client.get_order_book_raw = AsyncMock(side_effect=self._raw)

    async def _raw(self):
        return [{"brokerOrderId": r.broker_order_id} for r in self.records] if self.raw_ok else None


def make_poller(world, book, recovery, dispatched, pl=None):
    return MasterOrderPoller(master_account_id=str(world["master"].id), client_provider=lambda _a: book.client,
                             on_order_update=lambda event: dispatched.append(event), pipeline=pl or spipe(),
                             check_interval_seconds=5, recovery=recovery)


# ══ D1: the fanout-start record ══════════════════════════════════════════════════════════════

def test_automatic_resume_is_off_by_default():
    from app.core.config import Settings
    assert Settings.model_fields["ENGINE_RECOVERY_RESUME_ENABLED"].default is False


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_the_start_record_exists_before_any_child_task_begins(db_session, world, monkeypatch, kind):
    seen = []
    name = {"LIMIT": "_process_single_limit_follower", "SL": "_process_single_sl_follower",
            "MARKET": "_process_single_market_follower"}[kind]
    original = getattr(CopyEnginePipeline, name)

    async def spy(self, *args, **kwargs):
        async with TestingSessionLocal() as s:
            seen.append((await s.execute(select(AuditLog).where(AuditLog.event_type == FANOUT_STARTED_EVENT_TYPE,
                                                                AuditLog.master_order_id == f"R3B_D1_{kind}"))).scalars().all())
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(CopyEnginePipeline, name, spy)
    await run(make_event(kind, f"R3B_D1_{kind}"), world["clients"])
    assert len(seen) == 3 and all(len(rows_) == 1 for rows_ in seen)                  # present for every Child task
    meta = seen[0][0].metadata_payload
    assert meta["master_order_id"] == f"R3B_D1_{kind}" and meta["strategy_id"] == str(world["strategy"].id)
    assert sorted(meta["routed_children"]) == sorted(str(k.id) for k in world["kids"].values())
    assert meta["event_type"] == make_event(kind, "x").event_type and meta["quantity"] == 10 and meta["symbol"] == "RELIANCE"
    assert meta["trigger_price"] and meta["limit_price"] and meta["fill_sequence"] >= 1
    recorded = datetime.fromisoformat(meta["recorded_at"])
    assert recorded.tzinfo is not None and abs((datetime.now(timezone.utc) - recorded).total_seconds()) < 60


@pytest.mark.asyncio
async def test_a_start_record_failure_keeps_the_fanout_running_but_it_never_qualifies_for_resume(db_session, world, monkeypatch, resume_on):
    async def boom(*args, **kwargs):
        raise RuntimeError("audit store down")

    monkeypatch.setattr("app.services.copy_engine.pipeline.record_fanout_started", boom)
    oid = "R3B_NOSTART"
    out = await run(make_event("LIMIT", oid), world["clients"])
    assert all(r["status"] == "SUCCESS" for r in out.values())                       # the copy itself is unaffected
    async with TestingSessionLocal() as s:
        assert (await s.execute(select(AuditLog).where(AuditLog.event_type == FANOUT_STARTED_EVENT_TYPE))).scalars().all() == []
    # simulate a crash: one Child's decision is lost, and the order is still working
    async with TestingSessionLocal() as s:
        victim = world["kids"]["C"].id
        await s.execute(update(CopyTrade).where(CopyTrade.follower_account_id == victim).values(master_order_id="LOST"))
        await s.commit()
    await seed_master_row(world["master"].id, oid)
    plan = await recovery_for(world).plan_seed(str(world["master"].id), [book_rec(oid)])
    assert plan.resume == []                                                          # no proof the fanout began


# ══ D3: resume ═══════════════════════════════════════════════════════════════════════════════

async def partial(world, oid, *, kind="LIMIT", decided=("A",), **started_kwargs):
    ids = [k.id for k in world["kids"].values()]
    await seed_master_row(world["master"].id, oid, order_type="LIMIT" if kind == "LIMIT" else "SL",
                          status=OrderStatus.OPEN if kind == "LIMIT" else OrderStatus.PENDING,
                          price="100.00" if kind == "LIMIT" else "99.50", trigger="0" if kind == "LIMIT" else "100.00")
    await seed_started(world["master"].id, oid, ids, kind=kind, strategy_id=world["strategy"].id, **started_kwargs)
    for name in decided:
        await seed_decision(world["kids"][name].id, oid)
    return book_rec(oid, order_type="LIMIT" if kind == "LIMIT" else "SL",
                    status="OPEN" if kind == "LIMIT" else "TRIGGER_PENDING",
                    price="100.00" if kind == "LIMIT" else "99.50", trigger="0" if kind == "LIMIT" else "100.00")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["LIMIT", "SL"])
async def test_a_provable_partial_fanout_is_resumed_once_for_the_children_with_no_decision(db_session, world, resume_on, kind):
    oid = f"R3B_RESUME_{kind}"
    record = await partial(world, oid, kind=kind)
    book = Book([record])
    dispatched = []
    await make_poller(world, book, recovery_for(world), dispatched).poll_once()

    a, b, c = (world["kids"][n].id for n in "ABC")
    assert world["clients"][str(a.hex if False else a)].place_order.await_count == 0          # A already had a decision
    assert world["clients"][str(b)].place_order.await_count == 1 and world["clients"][str(c)].place_order.await_count == 1
    by = await trade_rows(oid)
    assert set(by) == {a, b, c} and by[b].status == CopyTradeStatus.SUBMITTED and by[c].status == CopyTradeStatus.SUBMITTED
    assert dispatched == []                                                           # nothing went through the event path
    (resumed,) = await events(rr.REPORT_RESUMED)
    assert sorted(resumed.metadata_payload["children"]) == sorted([str(b), str(c)])
    async with TestingSessionLocal() as s:
        summaries = (await s.execute(select(AuditLog).where(AuditLog.event_type == co.SUMMARY_EVENT_TYPE,
                                                            AuditLog.master_order_id == oid))).scalars().all()
    assert len(summaries) == 1 and summaries[0].metadata_payload["recovery"] is True

    # recovery again (another restart, or the same one twice): nothing more is sent
    await make_poller(world, book, recovery_for(world), []).poll_once()
    assert world["clients"][str(b)].place_order.await_count == 1 and world["clients"][str(c)].place_order.await_count == 1


@pytest.mark.asyncio
async def test_children_that_already_have_any_decision_are_never_touched(db_session, world, resume_on):
    oid = "R3B_DECIDED"
    ids = [k.id for k in world["kids"].values()]
    await seed_master_row(world["master"].id, oid)
    await seed_started(world["master"].id, oid, ids, strategy_id=world["strategy"].id)
    await seed_decision(world["kids"]["A"].id, oid, status=CopyTradeStatus.TIMEOUT)             # ambiguous send
    await seed_decision(world["kids"]["B"].id, oid, status=CopyTradeStatus.SKIPPED)             # a recorded pre-send decision
    plan = await recovery_for(world).plan_seed(str(world["master"].id), [book_rec(oid)])
    (cand,) = plan.resume
    assert cand.missing == [str(world["kids"]["C"].id)]                                         # only the provably missing one
    await recovery_for(world).resume(str(world["master"].id), plan.resume)
    assert world["clients"][str(world["kids"]["A"].id)].place_order.await_count == 0
    assert world["clients"][str(world["kids"]["B"].id)].place_order.await_count == 0
    assert world["clients"][str(world["kids"]["C"].id)].place_order.await_count == 1
    by = await trade_rows(oid)
    assert by[world["kids"]["A"].id].status == CopyTradeStatus.TIMEOUT and by[world["kids"]["B"].id].status == CopyTradeStatus.SKIPPED


SKIP_CASES = [
    ("flag-off", dict(flag=False), "RESUME_DISABLED"),
    ("stale", dict(age_seconds=601), "OUTSIDE_10_MINUTE_WINDOW"),
    ("future", dict(age_seconds=-120), "OUTSIDE_10_MINUTE_WINDOW"),
    ("quantity", dict(book=dict(qty=11)), "ATTRIBUTES_DIFFER_QUANTITY"),
    ("price", dict(book=dict(price="101.00")), "ATTRIBUTES_DIFFER_PRICE"),
    ("symbol", dict(book=dict(symbol="TCS")), "ATTRIBUTES_DIFFER_SYMBOL"),
    ("side", dict(book=dict(side=BookSide.SELL)), "ATTRIBUTES_DIFFER_SIDE"),
    ("persisted-quantity", dict(persisted=dict(quantity=9)), "ATTRIBUTES_DIFFER_QUANTITY"),
    ("persisted-price", dict(persisted=dict(price=Decimal("99.00"))), "ATTRIBUTES_DIFFER_PRICE"),
    ("record-quantity", dict(started=dict(quantity=12)), "ATTRIBUTES_DIFFER_QUANTITY"),
    ("order-cancelled", dict(book=dict(status="CANCELLED")), "ATTRIBUTES_DIFFER_NOT_WORKING"),
    ("order-complete", dict(book=dict(status="COMPLETE", filled=10)), "ATTRIBUTES_DIFFER_NOT_WORKING"),
    ("order-partial", dict(book=dict(status="PARTIAL", filled=3)), "ATTRIBUTES_DIFFER_NOT_WORKING"),
    ("order-type-changed", dict(book=dict(order_type="MARKET")), "ATTRIBUTES_DIFFER_ORDER_TYPE"),
    ("live-off", dict(live=False), "LIVE_TRADING_OFF"),
    ("master-toggle-off", dict(toggle=False), "MASTER_TOGGLE_OFF"),
    ("no-clients", dict(clients={}), "NO_CLIENT_FOR_MISSING_CHILDREN"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,case,reason", SKIP_CASES, ids=[c[0] for c in SKIP_CASES])
async def test_any_missing_stale_or_inconsistent_proof_places_nothing_and_reports(db_session, world, monkeypatch, name, case, reason):
    monkeypatch.setattr("app.services.reliability.restart_recovery.settings.ENGINE_RECOVERY_RESUME_ENABLED", case.get("flag", True))
    monkeypatch.setattr("app.services.reliability.restart_recovery.is_live_trading_enabled", AsyncMock(return_value=case.get("live", True)))
    monkeypatch.setattr("app.services.reliability.restart_recovery.is_master_trading_enabled", AsyncMock(return_value=case.get("toggle", True)))
    oid = f"R3B_SKIP_{name}"
    ids = [k.id for k in world["kids"].values()]
    await seed_master_row(world["master"].id, oid)
    await seed_started(world["master"].id, oid, ids, age_seconds=case.get("age_seconds", 30), strategy_id=world["strategy"].id,
                       overrides=case.get("started"))
    await seed_decision(world["kids"]["A"].id, oid)
    if case.get("persisted"):
        async with TestingSessionLocal() as s:
            await s.execute(update(Order).where(Order.broker_order_id == oid).values(**case["persisted"]))
            await s.commit()
    clients = world["clients"] if "clients" not in case else case["clients"]
    rec = recovery_for(world, clients=clients)
    plan = await rec.plan_seed(str(world["master"].id), [book_rec(oid, **case.get("book", {}))])
    assert len(plan.resume) == 1
    assert await rec.resume(str(world["master"].id), plan.resume) == 0
    for client in world["clients"].values():
        client.place_order.assert_not_awaited()
    assert set(await trade_rows(oid)) == {world["kids"]["A"].id}                      # no Child gained a decision
    (skipped,) = await events(rr.REPORT_RESUME_SKIPPED)
    assert skipped.metadata_payload["reason"] == reason
    await rec.resume(str(world["master"].id), plan.resume)                            # reporting is deduplicated
    assert len(await events(rr.REPORT_RESUME_SKIPPED)) == 1


@pytest.mark.asyncio
async def test_a_market_fanout_is_never_resumed_and_a_missing_start_record_means_no_candidate(db_session, world, resume_on):
    oid = "R3B_MARKET"
    await seed_master_row(world["master"].id, oid, order_type="MARKET", status=OrderStatus.COMPLETE, filled=10)
    await seed_started(world["master"].id, oid, [k.id for k in world["kids"].values()], kind="MARKET")
    plan = await recovery_for(world).plan_seed(str(world["master"].id), [book_rec(oid, order_type="MARKET", status="COMPLETE", filled=10)])
    assert plan.resume == []
    (report,) = await events(rr.REPORT_RESUME_SKIPPED)
    assert report.metadata_payload["event_type"] == "MARKET_ACCEPTED"
    assert all(c.place_order.await_count == 0 for c in world["clients"].values())


@pytest.mark.asyncio
async def test_a_lost_redis_key_cannot_cause_a_duplicate_and_an_existing_key_does_not_block_a_deliberate_resume(db_session, mock_redis, world, resume_on):
    """The validator's copy:seen key was set when the fanout began. A normal redelivery is stopped by it; a deliberate
    recovery skips ONLY that check - and the database claim still stops a second send for any Child."""
    oid = "R3B_REDIS"
    record = await partial(world, oid)
    pl = spipe(validator=EventValidator(redis=mock_redis))
    await mock_redis.set(f"copy:seen:{oid}:1", "1", ex=3600)                           # the key survived the crash
    normal = await pl.execute_replication(event=make_event("LIMIT", oid), clients=world["clients"])
    assert normal == {"status": "rejected", "reason": "DUPLICATE_EVENT"}
    rec = recovery_for(world, pl=pl)
    plan = await rec.plan_seed(str(world["master"].id), [record])
    assert await rec.resume(str(world["master"].id), plan.resume) == 2
    # then the key is lost (flush / expiry) and recovery is attempted again with the same stale list: one send each
    await mock_redis.delete(f"copy:seen:{oid}:1")
    plan2 = await rec.plan_seed(str(world["master"].id), [record])
    assert plan2.resume == []                                                          # nothing missing any more
    await rec.resume(str(world["master"].id), plan.resume)             # a stale candidate list: the claims still refuse
    await rec.resume(str(world["master"].id), plan.resume)
    for n in "BC":
        assert world["clients"][str(world["kids"][n].id)].place_order.await_count == 1


@pytest.mark.asyncio
async def test_a_resumed_child_still_meets_the_runtime_pause_and_the_gates(db_session, world, resume_on):
    oid = "R3B_GATES"
    record = await partial(world, oid)
    paused_id = world["kids"]["B"].id

    async def provider():
        return False, {str(paused_id)}

    pl = spipe(stop_provider=provider)
    world["clients"][str(world["kids"]["C"].id)].get_funds.side_effect = RuntimeError("funds down")
    rec = recovery_for(world, pl=pl)
    plan = await rec.plan_seed(str(world["master"].id), [record])
    await rec.resume(str(world["master"].id), plan.resume)
    by = await trade_rows(oid)
    assert by[paused_id].status == CopyTradeStatus.SKIPPED and co.parse_reason(by[paused_id].failure_reason)[0] == "TRADING_DISABLED"
    assert by[world["kids"]["C"].id].status == CopyTradeStatus.REJECTED
    assert world["clients"][str(paused_id)].place_order.await_count == 0
    assert world["clients"][str(world["kids"]["C"].id)].place_order.await_count == 0


@pytest.mark.asyncio
async def test_the_recovery_fanout_is_restricted_to_the_named_children_and_to_creation_events(db_session, world):
    oid = "R3B_SCOPE"
    scope = co.RecoveryScope(only_children=frozenset({str(world["kids"]["B"].id)}))
    out = await pipeline().execute_replication(event=make_event("LIMIT", oid), clients=world["clients"], recovery=scope)
    assert set(out["results"]) == {str(world["kids"]["B"].id)}
    assert world["clients"][str(world["kids"]["A"].id)].place_order.await_count == 0
    async with TestingSessionLocal() as s:                                            # a recovery run writes no second start record
        assert (await s.execute(select(AuditLog).where(AuditLog.event_type == FANOUT_STARTED_EVENT_TYPE))).scalars().all() == []
    unsupported = await pipeline().execute_replication(event=make_event("MARKET", oid), clients=world["clients"], recovery=scope)
    assert unsupported["status"] == "recovery_not_supported"


# ══ D4: reports, never copies ════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_restart_before_any_event_was_persisted_reports_only_and_places_nothing(db_session, world, resume_on):
    book = Book([book_rec("R3B_NEVER_SEEN"), book_rec("R3B_NEVER_SEEN_DONE", status="COMPLETE", filled=10)])
    dispatched = []
    poller = make_poller(world, book, recovery_for(world), dispatched)
    assert await poller.poll_once() == 0 and poller.is_seeded
    assert dispatched == [] and all(c.place_order.await_count == 0 for c in world["clients"].values())
    reports = await events(rr.REPORT_MISSED)
    assert {r.metadata_payload["broker_order_id"] for r in reports} == {"R3B_NEVER_SEEN", "R3B_NEVER_SEEN_DONE"}
    assert {r.severity.value for r in reports} == {"WARNING", "INFO"}                  # working = WARNING, finished = INFO
    await make_poller(world, book, recovery_for(world), []).poll_once()                # another restart: no repeat alerts
    assert len(await events(rr.REPORT_MISSED)) == 2
    # and the orders were persisted (so the next restart sees them as observed), never copied
    async with TestingSessionLocal() as s:
        assert len((await s.execute(select(Order).where(Order.account_id == world["master"].id))).scalars().all()) == 2


@pytest.mark.asyncio
async def test_a_working_order_with_no_fanout_record_is_reported_not_copied_and_terminal_ones_are_not_reported(db_session, world, resume_on):
    """Observe-only processing and a crash before fanout leave the same rows, so neither is inferred to be a crash."""
    await seed_master_row(world["master"].id, "R3B_OBS_OPEN")
    await seed_master_row(world["master"].id, "R3B_OBS_DONE", status=OrderStatus.COMPLETE, filled=10)
    book = Book([book_rec("R3B_OBS_OPEN"), book_rec("R3B_OBS_DONE", status="COMPLETE", filled=10)])
    await make_poller(world, book, recovery_for(world), []).poll_once()
    (report,) = await events(rr.REPORT_NOT_FANNED)
    assert report.metadata_payload["broker_order_id"] == "R3B_OBS_OPEN"
    assert all(c.place_order.await_count == 0 for c in world["clients"].values())


# ══ D2: lifecycle continuity ═════════════════════════════════════════════════════════════════

async def copied(world, oid, *, child_price="100.00", child_status=OrderStatus.OPEN, order_type="LIMIT", trigger="0",
                 master_status=OrderStatus.OPEN, master_filled=0, master_qty=10, source=OrderSource.BROKER_EXTERNAL,
                 master_price="100.00", master_trigger="0"):
    await seed_master_row(world["master"].id, oid, order_type=order_type, status=master_status, filled=master_filled,
                          qty=master_qty, source=source, price=master_price, trigger=master_trigger)
    await seed_decision(world["kids"]["A"].id, oid, with_order=True, order_price=child_price, order_status=child_status,
                        order_type=order_type, trigger=trigger)


LIFECYCLE = [
    # id, copied(...) kwargs, book kwargs, expected action
    ("unchanged", {}, {}, rr.ACTION_PERSIST),
    ("cancelled-with-working-child", {}, dict(status="CANCELLED"), rr.ACTION_CANCEL),
    ("cancelled-children-already-cancelled", dict(child_status=OrderStatus.CANCELLED), dict(status="CANCELLED"), rr.ACTION_PERSIST),
    ("cancelled-child-without-broker-order", {}, dict(status="CANCELLED"), rr.ACTION_CANCEL),
    ("limit-price-modified", {}, dict(price="101.00"), rr.ACTION_MODIFY),
    ("modification-already-applied", dict(child_price="101.00"), dict(price="101.00"), rr.ACTION_PERSIST),
    ("limit-partly-filled", {}, dict(status="PARTIAL", filled=4), rr.ACTION_PERSIST),
    ("limit-filled", {}, dict(status="COMPLETE", filled=10), rr.ACTION_PERSIST),
    ("sl-trigger-modified", dict(order_type="SL", trigger="100.00", master_status=OrderStatus.PENDING, master_price="99.50", master_trigger="100.00"),
     dict(order_type="SL", status="TRIGGER_PENDING", price="99.50", trigger="101.00"), rr.ACTION_MODIFY),
    ("sl-unchanged", dict(order_type="SL", trigger="100.00", master_status=OrderStatus.PENDING, master_price="99.50", master_trigger="100.00"),
     dict(order_type="SL", status="TRIGGER_PENDING", price="99.50", trigger="100.00"), rr.ACTION_PERSIST),
    ("jettone-originated", dict(source=OrderSource.INTERNAL_UI), dict(status="CANCELLED"), rr.ACTION_PERSIST),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,setup,book_kwargs,expected", LIFECYCLE, ids=[c[0] for c in LIFECYCLE])
async def test_an_already_copied_order_is_continued_from_the_childrens_state(db_session, world, name, setup, book_kwargs, expected):
    oid = f"R3B_LC_{name}"
    await copied(world, oid, **setup)
    plan = await recovery_for(world).plan_seed(str(world["master"].id), [book_rec(oid, **book_kwargs)])
    assert plan.lifecycle == {oid: expected}                                          # not baselined, with this action
    assert await events(rr.REPORT_REVIEW) == []


REVIEW = [
    ("limit-turned-market", {}, dict(order_type="MARKET", status="COMPLETE", filled=10, price="0"), "ORDER_TYPE_CHANGED_LIMIT_TO_MARKET"),
    ("quantity-only-change", {}, dict(qty=15), "QUANTITY_DIFFERS"),
    ("sl-triggered", dict(order_type="SL", trigger="100.00", master_status=OrderStatus.PENDING, master_price="99.50", master_trigger="100.00"),
     dict(order_type="SL", status="OPEN", price="99.50", trigger="100.00"), "SL_TRIGGERED_WHILE_DOWN"),
    ("market-fill-delta", dict(order_type="MARKET", master_filled=4, master_price="0", master_status=OrderStatus.PARTIAL),
     dict(order_type="MARKET", status="PARTIAL", filled=10, price="0"), "MARKET_FILL_WHILE_DOWN"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,setup,book_kwargs,why", REVIEW, ids=[c[0] for c in REVIEW])
async def test_changes_that_would_place_or_reprice_at_stale_prices_are_never_replayed(db_session, world, resume_on, name, setup, book_kwargs, why):
    oid = f"R3B_RV_{name}"
    await copied(world, oid, **setup)
    book = Book([book_rec(oid, **book_kwargs)])
    dispatched = []
    poller = make_poller(world, book, recovery_for(world), dispatched)
    await poller.poll_once()
    assert dispatched == [] and all(c.place_order.await_count == 0 for c in world["clients"].values())
    (report,) = await events(rr.REPORT_REVIEW)
    assert report.metadata_payload["reason"] == why
    # within this run a review order stays baselined: a later change is not dispatched either (no replay of the
    # missed delta through a later event)
    assert oid in poller._baseline_order_ids
    book.records = [book_rec(oid, status="CANCELLED")]
    assert await poller.poll_once() == 0 and dispatched == []


@pytest.mark.asyncio
async def test_a_copied_order_is_not_baselined_so_a_cancel_after_the_restart_still_reaches_its_children(db_session, world):
    oid = "R3B_NOT_BASELINED"
    await copied(world, oid)
    book = Book([book_rec(oid)])
    dispatched = []
    poller = make_poller(world, book, recovery_for(world), dispatched)
    assert await poller.poll_once() == 0 and dispatched == []                         # unchanged: nothing is dispatched
    assert oid not in poller._baseline_order_ids and poller.pop_recovery_action(oid) is None
    book.records = [book_rec(oid, status="CANCELLED")]
    assert await poller.poll_once() == 1                                              # the normal path now sees the cancel
    assert dispatched[0].status == "CANCELLED" and dispatched[0].broker_order_id == oid
    assert poller.pop_recovery_action(oid) is None                                    # a normal dispatch carries no recovery action


@pytest.mark.asyncio
@pytest.mark.parametrize("name,action,expected_status", [("cancel", rr.ACTION_CANCEL, "CANCELLED"), ("modify", rr.ACTION_MODIFY, "OPEN")])
async def test_the_seed_pass_dispatches_only_a_cancel_or_modify_and_marks_it_once(db_session, world, name, action, expected_status):
    oid = f"R3B_SEED_{name}"
    await copied(world, oid)
    record = book_rec(oid, status="CANCELLED") if action == rr.ACTION_CANCEL else book_rec(oid, price="101.00")
    dispatched = []
    poller = make_poller(world, Book([record]), recovery_for(world), dispatched)
    assert await poller.poll_once() == 1 and dispatched[0].status == expected_status
    assert poller.pop_recovery_action(oid) == action and poller.pop_recovery_action(oid) is None      # read once
    # a second restart after the Children caught up dispatches nothing
    if action == rr.ACTION_CANCEL:
        async with TestingSessionLocal() as s:
            await s.execute(update(Order).where(Order.master_broker_order_id == oid).values(status=OrderStatus.CANCELLED))
            await s.commit()
    else:
        async with TestingSessionLocal() as s:
            await s.execute(update(Order).where(Order.master_broker_order_id == oid).values(price=Decimal("101.00")))
            await s.commit()
    again = []
    assert await make_poller(world, Book([record]), recovery_for(world), again).poll_once() == 0 and again == []


@pytest.mark.parametrize("action,etype,allowed", [
    (rr.ACTION_CANCEL, "ORDER_CANCELLED", True), (rr.ACTION_CANCEL, "ORDER_FILLED", False), (rr.ACTION_CANCEL, "LIMIT_CREATED", False),
    (rr.ACTION_MODIFY, "ORDER_MODIFIED", True), (rr.ACTION_MODIFY, "SL_PENDING_MODIFIED", True),
    (rr.ACTION_MODIFY, "LIMIT_CREATED", False), (rr.ACTION_MODIFY, "SL_PENDING_CREATED", False),
    (rr.ACTION_MODIFY, "MARKET_ACCEPTED", False), (rr.ACTION_MODIFY, "ORDER_FILLED", False), (rr.ACTION_PERSIST, "ORDER_CANCELLED", False),
])
def test_a_recovery_dispatch_can_only_become_an_event_that_acts_on_existing_children(action, etype, allowed):
    assert RestartRecovery.allows(action, MagicMock(event_type=etype)) is allowed


@pytest.mark.asyncio
async def test_a_blocked_recovery_dispatch_is_reported_once(db_session, world):
    rec = recovery_for(world)
    for _ in range(2):
        await rec.report_blocked(str(world["master"].id), "R3B_BLOCKED", rr.ACTION_CANCEL, "ORDER_FILLED")
    assert len(await events(rr.REPORT_BLOCKED)) == 1


# ══ the seed pass fails closed ═══════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["raw-none", "parsed-empty-raw-not", "raises"])
async def test_an_unreadable_first_book_does_not_seed_so_old_orders_are_never_treated_as_new(db_session, world, failure):
    """get_order_book() returns [] on a network error. Seeding that empty baseline made the NEXT poll treat every order
    already in the book as new."""
    old = [book_rec("R3B_OLD_1"), book_rec("R3B_OLD_2", status="PARTIAL", filled=3)]
    book = Book(old)
    dispatched = []
    poller = make_poller(world, book, recovery_for(world), dispatched)
    if failure == "raw-none":
        book.raw_ok = False
    elif failure == "parsed-empty-raw-not":
        book.client.get_order_book = AsyncMock(return_value=[])
    else:
        book.client.get_order_book_raw = AsyncMock(side_effect=RuntimeError("boom"))
    assert await poller.poll_once() == 0 and not poller.is_seeded                      # not seeded
    book.raw_ok = True
    book.client.get_order_book = AsyncMock(side_effect=lambda: list(old))
    book.client.get_order_book_raw = AsyncMock(side_effect=book._raw)
    assert await poller.poll_once() == 0 and poller.is_seeded and dispatched == []     # the good poll baselines them
    assert {"R3B_OLD_1", "R3B_OLD_2"} <= poller._baseline_order_ids


@pytest.mark.asyncio
async def test_a_genuinely_empty_book_still_seeds(db_session, world):
    poller = make_poller(world, Book([]), recovery_for(world), [])
    assert await poller.poll_once() == 0 and poller.is_seeded


@pytest.mark.asyncio
async def test_a_recovery_planning_failure_falls_back_to_the_plain_baseline(db_session, world, monkeypatch):
    rec = recovery_for(world)
    monkeypatch.setattr(rec, "plan_seed", AsyncMock(side_effect=RuntimeError("db down")))
    dispatched = []
    poller = make_poller(world, Book([book_rec("R3B_PLANFAIL")]), rec, dispatched)
    assert await poller.poll_once() == 0 and poller.is_seeded and dispatched == []
    assert "R3B_PLANFAIL" in poller._baseline_order_ids


@pytest.mark.asyncio
async def test_a_failed_report_write_never_breaks_recovery_or_places_anything(db_session, world, monkeypatch):
    monkeypatch.setattr("app.services.reliability.restart_recovery.emit_system_event", AsyncMock(side_effect=RuntimeError("down")))
    poller = make_poller(world, Book([book_rec("R3B_REPORTFAIL")]), recovery_for(world), [])
    assert await poller.poll_once() == 0 and poller.is_seeded
    assert all(c.place_order.await_count == 0 for c in world["clients"].values())


@pytest.mark.asyncio
async def test_without_a_recovery_object_the_poller_behaves_exactly_as_before(db_session, world):
    await copied(world, "R3B_LEGACY")
    dispatched = []
    poller = make_poller(world, Book([book_rec("R3B_LEGACY", status="CANCELLED")]), None, dispatched)
    assert await poller.poll_once() == 0 and dispatched == [] and "R3B_LEGACY" in poller._baseline_order_ids


@pytest.mark.asyncio
async def test_a_master_switch_activation_monitor_is_not_given_recovery_semantics(db_session, world):
    """Wiring check: engine_loop passes recovery only when there is no activation snapshot."""
    import inspect
    from app.services.runtime import engine_loop
    source = inspect.getsource(engine_loop.EngineLoop.sync_master_monitors)
    assert "recovery=self._recovery if activation_ids is None else None" in source


# ══ two simultaneous recovery runs on PostgreSQL ═════════════════════════════════════════════

from test_duplicate_create_postgres_concurrency import postgres_environment, requires_postgres  # noqa: E402


@requires_postgres
@pytest.mark.asyncio
async def test_two_simultaneous_recovery_runs_have_exactly_one_successful_claim_per_child_on_postgresql(monkeypatch):
    from app.models.strategies import GroupStrategy
    from strategy_helpers import SAMPLE_MASTER_ID, add_child
    monkeypatch.setattr("app.services.reliability.restart_recovery.settings.ENGINE_RECOVERY_RESUME_ENABLED", True)
    monkeypatch.setattr("app.services.reliability.restart_recovery.is_live_trading_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr("app.services.reliability.restart_recovery.is_master_trading_enabled", AsyncMock(return_value=True))
    async with postgres_environment(monkeypatch) as (factory, first):
        async with factory() as s:
            strategy = (await s.execute(select(GroupStrategy).where(GroupStrategy.master_account_id == SAMPLE_MASTER_ID))).scalar_one()
            b = Account(id=uuid.uuid4(), account_type=AccountType.FOLLOWER, display_name="B", alice_user_id="PGR_B", is_active=True)
            c = Account(id=uuid.uuid4(), account_type=AccountType.FOLLOWER, display_name="C", alice_user_id="PGR_C", is_active=True)
            s.add_all([b, c])
            await s.commit()
            await add_child(s, strategy, b)
            await add_child(s, strategy, c)
            oid = "R3B_PG_RACE"
            event = make_event("LIMIT", oid)
            s.add(Order(id=uuid.uuid4(), internal_order_id=f"EXT_{oid}_x", broker_order_id=oid, account_id=SAMPLE_MASTER_ID,
                        source=OrderSource.BROKER_EXTERNAL, exchange="NSE", symbol="RELIANCE", side=OrderSide.BUY,
                        order_type="LIMIT", product_type="MIS", quantity=10, filled_quantity=0, pending_quantity=10,
                        price=LIMIT_PRICE, status=OrderStatus.OPEN))
            meta = co.fanout_started_metadata(event, strategy.id, [str(first.id), str(b.id), str(c.id)])
            s.add(AuditLog(event_type=FANOUT_STARTED_EVENT_TYPE, account_id=SAMPLE_MASTER_ID, master_order_id=oid,
                           reason_code="LIMIT_CREATED#1", metadata_payload=meta))
            await s.commit()
        clients = {str(a.id): client_returning(ok(f"BRK_{a.display_name}")) for a in (first, b, c)}
        rec = RestartRecovery(pipeline=pipeline(), redis=AsyncMock(), clients_provider=lambda m: clients)
        plan = await rec.plan_seed(str(SAMPLE_MASTER_ID), [book_rec(oid)])
        assert len(plan.resume) == 1 and len(plan.resume[0].missing) == 3
        await asyncio.gather(rec.resume(str(SAMPLE_MASTER_ID), plan.resume), rec.resume(str(SAMPLE_MASTER_ID), plan.resume))
        for a in (first, b, c):
            assert clients[str(a.id)].place_order.await_count == 1                      # exactly one send per Child
        async with factory() as s:
            trades = (await s.execute(select(CopyTrade).where(CopyTrade.master_order_id == oid))).scalars().all()
        assert len(trades) == 3 and all(t.status == CopyTradeStatus.SUBMITTED for t in trades)
