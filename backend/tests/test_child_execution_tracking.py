"""
R3: per-Child execution tracking for a Master fanout.

Every Child that routing hands an order to ends the event with exactly ONE CopyTrade row, independent of the others:
  * leaves before the send (funds / risk / safety rejection, disabled or paused)  -> terminal SKIPPED / REJECTED row,
    quantity 0, no client_order_id, reason code in failure_reason;
  * proceeds to the send -> claims its row first (R2B), then PENDING -> SUBMITTED / REJECTED / FAILED / TIMEOUT.
Members the strategy did not route (member trading disabled, inactive account, ...) get NO row; they are listed in the
advisory COPY_FANOUT_SUMMARY AuditLog row and in GET /trades/fanout/{master_order_id}.

A pre-send terminal row can be re-claimed ONCE by compare-and-set, but only after the Redis `copy:seen` key was lost
(the validator stops a redelivered event before any Child is looked at), and only inside a short window. A row that was
ever claimed is never re-claimed. Ambiguous sends are never re-sent (R2A-R2C unchanged).

Mocked brokers, in-memory database. The PostgreSQL race test needs R2B_POSTGRES_TEST_URL and is skipped otherwise.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from app.models.accounts import Account, AccountType, CopySetting, QuantityMode
from app.models.audit import AuditLog, SystemEvent
from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderSide, OrderStatus
from app.services.alice_blue.types import OrderResult, OrderResultCategory
from app.services.copy_engine import child_outcomes as co
from app.services.copy_engine.child_outcomes import Claim, ClaimKind, claim_child_trade
from app.services.copy_engine.events import derive_copy_event_id
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.reliability.follower_fill_syncer import FollowerFillSyncer
from app.services.reliability.stale_order_sweeper import StaleOrderSweeper
from conftest import TestingSessionLocal
from strategy_helpers import SAMPLE_MASTER_ID, add_child, link_strategy
from test_api_endpoints import AUTH_HEADER, api_client  # noqa: F401
from test_duplicate_create_db_backstop import KINDS, bypassed_redis, make_event, ok, pipeline, rows
from test_marketable_limit_strategy import _funds

MASTER_SYMBOL = "RELIANCE"


def result(category, broker_order_id=None, message="m"):
    return OrderResult(category=category, client_order_id=None, broker_order_id=broker_order_id, message=message,
                       raw_response={"stat": category.value}, timestamp=datetime.now(timezone.utc))


def client_returning(*results, funds=True):
    c = AsyncMock()
    if funds is True:
        c.get_funds.return_value = _funds()
    elif funds:
        c.get_funds.side_effect = funds
    c.get_positions.return_value = []
    c.get_positions_or_none.return_value = []
    c.place_order.side_effect = list(results)
    return c


async def account(db_session, name, *, active=True):
    acc = Account(id=uuid.uuid4(), account_type=AccountType.FOLLOWER, display_name=name,
                  alice_user_id=f"AB_R3_{name}_{uuid.uuid4().hex[:6]}", is_active=active)
    db_session.add(acc)
    await db_session.commit()
    return acc


@pytest_asyncio.fixture
async def fan(db_session, sample_accounts):
    """Master + routed Children A (accepted), B (pre-send funds rejection), C (ambiguous send) and two EXCLUDED
    members: D (member trading disabled) and E (inactive account)."""
    master = sample_accounts["master"]
    a, b, c = await account(db_session, "A"), await account(db_session, "B"), await account(db_session, "C")
    d, e = await account(db_session, "D"), await account(db_session, "E", active=False)
    strategy = await link_strategy(db_session, master, [a, b, c])
    await add_child(db_session, strategy, d, trading_enabled=False)
    await add_child(db_session, strategy, e)
    clients = {
        str(a.id): client_returning(ok("BRK_A")),
        str(b.id): client_returning(funds=RuntimeError("funds down")),
        str(c.id): client_returning(result(OrderResultCategory.TIMEOUT)),
    }
    return dict(master=master, a=a, b=b, c=c, d=d, e=e, strategy=strategy, clients=clients)


async def run(event, clients, pl=None, stagger_ms=250):
    """The test database is SQLite on ONE shared connection, so Children that commit at the same instant can interleave
    ("cannot commit transaction - SQL statements in progress"). The Children are therefore started a little apart;
    simultaneous processing is covered on PostgreSQL (test at the end of this file and the R2B concurrency tests)."""
    return (await (pl or pipeline()).execute_replication(event=event, clients=clients, stagger_ms=stagger_ms))["results"]


async def trades_by_follower(master_order_id):
    trades, _ = await rows(master_order_id)
    out = {}
    for t in trades:
        out.setdefault(t.follower_account_id, []).append(t)
    return out


async def summaries(master_order_id):
    async with TestingSessionLocal() as s:
        return (await s.execute(select(AuditLog).where(AuditLog.event_type == co.SUMMARY_EVENT_TYPE,
                                                       AuditLog.master_order_id == master_order_id))).scalars().all()


# ══ per-Child isolation: success, pre-send rejection, ambiguous send, excluded members ═══════

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_each_routed_child_gets_its_own_independent_outcome(db_session, fan, kind):
    mid = f"R3_FAN_{kind}"
    out = await run(make_event(kind, mid), fan["clients"])
    a, b, c = fan["a"], fan["b"], fan["c"]
    assert out[str(a.id)]["status"] == "SUCCESS"
    assert out[str(b.id)] == {"status": "REJECTED", "reasons": ["FUNDS_FETCH_FAILED"]}
    assert out[str(c.id)]["status"] == "TIMEOUT"

    by = await trades_by_follower(mid)
    assert set(by) == {a.id, b.id, c.id}                                          # D and E (excluded) have no row
    (ta,), (tb,), (tc,) = by[a.id], by[b.id], by[c.id]
    assert ta.status == CopyTradeStatus.SUBMITTED and ta.broker_order_id == "BRK_A" and ta.client_order_id
    assert tb.status == CopyTradeStatus.REJECTED and tb.quantity == 0 and tb.client_order_id is None
    assert tb.submitted_at is None and co.parse_reason(tb.failure_reason) == ("FUNDS_FETCH_FAILED", "")
    assert tc.status == CopyTradeStatus.TIMEOUT and tc.submitted_at is not None and tc.broker_order_id is None

    assert fan["clients"][str(a.id)].place_order.await_count == 1                 # one send each; B never sent
    assert fan["clients"][str(b.id)].place_order.await_count == 0
    assert fan["clients"][str(c.id)].place_order.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_the_summary_lists_the_routed_children_and_the_excluded_members(db_session, fan, kind):
    mid = f"R3_SUM_{kind}"
    await run(make_event(kind, mid), fan["clients"])
    (row,) = await summaries(mid)
    meta = row.metadata_payload
    assert row.reason_code == f"{make_event(kind, mid).event_type}#{make_event(kind, mid).fill_sequence}"
    assert meta["routed_count"] == 3 and meta["strategy_id"] == str(fan["strategy"].id)
    assert meta["counts"] == {"SUCCESS": 1, "REJECTED": 1, "TIMEOUT": 1}
    assert meta["children"][str(fan["b"].id)] == {"status": "REJECTED", "code": "FUNDS_FETCH_FAILED", "copy_trade_status": "REJECTED"}
    assert meta["children"][str(fan["a"].id)]["copy_trade_status"] == "SUBMITTED"      # result SUCCESS, row SUBMITTED
    assert meta["children"][str(fan["c"].id)]["copy_trade_status"] == "TIMEOUT"
    assert {x["account_id"]: x["reason"] for x in meta["excluded"]} == {
        str(fan["d"].id): co.MEMBER_TRADING_DISABLED, str(fan["e"].id): co.ACCOUNT_INACTIVE}
    assert meta["authoritative_source"] == "copy_trades" and "advisory" in meta["note"]


@pytest.mark.asyncio
async def test_a_runtime_paused_child_is_routed_and_recorded_as_skipped(db_session, fan, monkeypatch):
    mid = "R3_PAUSED"
    pl = pipeline()
    monkeypatch.setattr(pl, "_resolve_stop_state", AsyncMock(return_value=(False, {str(fan["a"].id)})))
    out = await run(make_event("LIMIT", mid), fan["clients"], pl)
    assert out[str(fan["a"].id)] == {"status": "SKIPPED", "reason": "TRADING_DISABLED"}
    (ta,) = (await trades_by_follower(mid))[fan["a"].id]
    assert ta.status == CopyTradeStatus.SKIPPED and ta.failure_reason == "TRADING_DISABLED" and ta.quantity == 0
    assert fan["clients"][str(fan["a"].id)].place_order.await_count == 0
    assert (await trades_by_follower(mid)).keys() >= {fan["b"].id, fan["c"].id}     # the others were unaffected


@pytest.mark.asyncio
async def test_a_risk_engine_rejection_is_recorded_with_its_reason_code(db_session, sample_accounts):
    master, follower = sample_accounts["master"], await account(db_session, "RISK")
    db_session.add(CopySetting(follower_account_id=follower.id, copy_enabled=True, quantity_mode=QuantityMode.FIXED, fixed_qty=100))
    await link_strategy(db_session, master, [follower])
    client = client_returning()
    client.get_funds.return_value = _funds(available=Decimal("10"))
    out = await run(make_event("LIMIT", "R3_RISK"), {str(follower.id): client})
    assert out[str(follower.id)]["reasons"] == ["INSUFFICIENT_MARGIN"]
    (t,) = (await trades_by_follower("R3_RISK"))[follower.id]
    assert t.status == CopyTradeStatus.REJECTED and t.failure_reason == "INSUFFICIENT_MARGIN" and t.quantity == 0
    client.place_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_exception_in_one_child_does_not_disturb_the_others_and_never_auto_fails_its_claim(db_session, fan, monkeypatch):
    mid = "R3_EXC"
    original = CopyEnginePipeline.save_or_update_order

    async def flaky(self, *args, **kwargs):
        if kwargs.get("account_id") == fan["c"].id:
            raise RuntimeError("injected failure after the claim")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(CopyEnginePipeline, "save_or_update_order", flaky)
    out = await run(make_event("LIMIT", mid), fan["clients"])
    assert out[str(fan["c"].id)]["status"] == "EXCEPTION"
    assert out[str(fan["a"].id)]["status"] == "SUCCESS" and out[str(fan["b"].id)]["status"] == "REJECTED"
    by = await trades_by_follower(mid)
    (tc,) = by[fan["c"].id]
    assert tc.status == CopyTradeStatus.PENDING and tc.submitted_at is None          # claimed, left for reconciliation
    assert by[fan["a"].id][0].status == CopyTradeStatus.SUBMITTED
    assert fan["clients"][str(fan["c"].id)].place_order.await_count == 0             # it failed before the send
    async with TestingSessionLocal() as s:
        events = (await s.execute(select(SystemEvent).where(SystemEvent.event_type == "COPY_CHILD_EXCEPTION"))).scalars().all()
    assert len(events) == 1 and events[0].account_id == fan["c"].id
    (row,) = await summaries(mid)
    assert row.metadata_payload["children"][str(fan["c"].id)] == {"status": "EXCEPTION", "code": "EXCEPTION", "copy_trade_status": "PENDING"}


# ══ normal LIMIT / SL outcomes are finalised ═════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["LIMIT", "SL"])
@pytest.mark.parametrize("category,expected,code", [
    (OrderResultCategory.REJECTED, CopyTradeStatus.REJECTED, "REJECTED"),
    (OrderResultCategory.NETWORK_ERROR, CopyTradeStatus.FAILED, "NETWORK_ERROR"),
])
async def test_a_normal_broker_answer_finalises_the_row_and_the_sweeper_leaves_it_alone(db_session, sample_accounts, kind, category, expected, code):
    follower = await account(db_session, "FIN")
    await link_strategy(db_session, sample_accounts["master"], [follower])
    mid = f"R3_FIN_{kind}_{code}"
    client = client_returning(*[result(category, message="refused by broker")] * 3)
    out = await run(make_event(kind, mid), {str(follower.id): client})
    assert out[str(follower.id)]["status"] in ("REJECTED", "FAILED")
    (t,) = (await trades_by_follower(mid))[follower.id]
    assert t.status == expected and t.completed_at is not None and t.client_order_id
    assert co.parse_reason(t.failure_reason)[0] == code and "refused by broker" in t.failure_reason
    async with TestingSessionLocal() as s:                                           # the old false stale flag
        await StaleOrderSweeper(stale_threshold_seconds=0).sweep_once(s)
    assert (await trades_by_follower(mid))[follower.id][0].status == expected        # not RECONCILIATION_REQUIRED


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["LIMIT", "SL"])
async def test_a_successful_send_is_submitted_with_the_broker_id_and_fills_arrive_through_the_order(db_session, sample_accounts, kind):
    follower = await account(db_session, "FILL")
    await link_strategy(db_session, sample_accounts["master"], [follower])
    mid = f"R3_FILL_{kind}"
    await run(make_event(kind, mid), {str(follower.id): client_returning(ok("BRK_FILL"))})
    (t,) = (await trades_by_follower(mid))[follower.id]
    assert t.status == CopyTradeStatus.SUBMITTED and t.broker_order_id == "BRK_FILL"
    _, (order,) = await rows(mid)
    assert order.broker_order_id == "BRK_FILL" and order.filled_quantity == 0

    # a PARTIAL fill is not terminal: the CopyTrade stays SUBMITTED and the Order carries the progress
    async with TestingSessionLocal() as s:
        await s.execute(update(Order).where(Order.id == order.id).values(status=OrderStatus.PARTIAL, filled_quantity=4, pending_quantity=6))
        await s.commit()
        order = (await s.execute(select(Order).where(Order.id == order.id))).scalar_one()
    await FollowerFillSyncer._sync_copy_trade(order, OrderStatus.PARTIAL)
    assert (await trades_by_follower(mid))[follower.id][0].status == CopyTradeStatus.SUBMITTED


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["LIMIT", "SL", "MARKET"])
@pytest.mark.parametrize("order_status,expected", [
    (OrderStatus.COMPLETE, CopyTradeStatus.SUCCESS),
    (OrderStatus.CANCELLED, CopyTradeStatus.FAILED),
    (OrderStatus.REJECTED, CopyTradeStatus.REJECTED),
])
async def test_the_fill_syncer_mirrors_a_terminal_order_onto_the_submitted_row(db_session, sample_accounts, kind, order_status, expected):
    follower = await account(db_session, "SYNC")
    await link_strategy(db_session, sample_accounts["master"], [follower])
    mid = f"R3_SYNC_{kind}_{order_status.value}"
    await run(make_event(kind, mid), {str(follower.id): client_returning(ok("BRK_SYNC"))})
    assert (await trades_by_follower(mid))[follower.id][0].status == CopyTradeStatus.SUBMITTED
    _, (order,) = await rows(mid)
    await FollowerFillSyncer._sync_copy_trade(order, order_status)
    assert (await trades_by_follower(mid))[follower.id][0].status == expected         # matched by broker id / master order


# ══ ambiguous sends and duplicate deliveries ═════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("category", [OrderResultCategory.TIMEOUT, OrderResultCategory.UNKNOWN])
async def test_an_ambiguous_send_is_never_resent_even_when_the_event_is_redelivered(db_session, sample_accounts, kind, category):
    follower = await account(db_session, "AMB")
    await link_strategy(db_session, sample_accounts["master"], [follower])
    mid = f"R3_AMB_{kind}_{category.value}"
    client = client_returning(result(category), ok("BRK_SECOND_ATTEMPT"))
    first = await run(make_event(kind, mid), {str(follower.id): client})
    assert first[str(follower.id)]["status"] == "TIMEOUT" and client.place_order.await_count == 1
    again = await run(make_event(kind, mid), {str(follower.id): client})            # Redis dedupe bypassed
    assert again[str(follower.id)] == {"status": "SKIPPED", "reason": "DUPLICATE_EVENT"}
    assert client.place_order.await_count == 1                                      # still one send
    (t,) = (await trades_by_follower(mid))[follower.id]
    assert t.status == CopyTradeStatus.TIMEOUT and t.retry_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_a_replay_writes_no_second_summary_and_no_second_row(db_session, sample_accounts, kind):
    follower = await account(db_session, "REPLAY")
    await link_strategy(db_session, sample_accounts["master"], [follower])
    mid = f"R3_REPLAY_{kind}"
    client = client_returning(ok("BRK_R"), ok("BRK_R2"))
    await run(make_event(kind, mid), {str(follower.id): client})
    await run(make_event(kind, mid), {str(follower.id): client})
    assert len((await trades_by_follower(mid))[follower.id]) == 1 and client.place_order.await_count == 1
    assert len(await summaries(mid)) == 1                                            # all-duplicate replay: not recorded


@pytest.mark.asyncio
async def test_the_real_redis_dedupe_stops_a_redelivery_before_any_child_is_reevaluated(db_session, mock_redis, sample_accounts):
    """The validator sets copy:seen per Master event BEFORE any Child is looked at. A redelivery inside its window is
    rejected there, so a recorded pre-send decision is not re-evaluated and nothing is recorded twice."""
    follower = await account(db_session, "DEDUP")
    await link_strategy(db_session, sample_accounts["master"], [follower])
    pl = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    down = client_returning(funds=RuntimeError("funds down"))
    first = await pl.execute_replication(event=make_event("LIMIT", "R3_DEDUP"), clients={str(follower.id): down})
    assert first["results"][str(follower.id)]["reasons"] == ["FUNDS_FETCH_FAILED"]
    healthy = client_returning(ok("BRK_NEVER"))
    second = await pl.execute_replication(event=make_event("LIMIT", "R3_DEDUP"), clients={str(follower.id): healthy})
    assert second == {"status": "rejected", "reason": "DUPLICATE_EVENT"}              # stopped by Redis, per event
    healthy.place_order.assert_not_awaited()
    (t,) = (await trades_by_follower("R3_DEDUP"))[follower.id]
    assert t.status == CopyTradeStatus.REJECTED and len(await summaries("R3_DEDUP")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_a_pre_send_row_is_reclaimed_once_after_redis_loss_and_then_behaves_like_any_claim(db_session, sample_accounts, kind):
    follower = await account(db_session, "RECLAIM")
    await link_strategy(db_session, sample_accounts["master"], [follower])
    mid = f"R3_RECLAIM_{kind}"
    await run(make_event(kind, mid), {str(follower.id): client_returning(funds=RuntimeError("funds down"))})
    (first,) = (await trades_by_follower(mid))[follower.id]
    assert first.status == CopyTradeStatus.REJECTED and first.client_order_id is None

    healthy = client_returning(ok("BRK_RECLAIMED"), ok("BRK_EXTRA"))
    out = await run(make_event(kind, mid), {str(follower.id): healthy})              # Redis lost: the event looks new
    assert out[str(follower.id)]["status"] == "SUCCESS" and healthy.place_order.await_count == 1
    (t,) = (await trades_by_follower(mid))[follower.id]
    assert t.id == first.id and t.retry_count == 1                                   # the same row, taken over once
    assert t.status == CopyTradeStatus.SUBMITTED and t.quantity == 10 and t.failure_reason is None
    assert t.client_order_id and t.broker_order_id == "BRK_RECLAIMED"
    if kind != "MARKET":                                                               # MARKET stamps completed_at on accept
        assert t.completed_at is None

    third = await run(make_event(kind, mid), {str(follower.id): healthy})            # now it IS a claimed row
    assert third[str(follower.id)] == {"status": "SKIPPED", "reason": "DUPLICATE_EVENT"}
    assert healthy.place_order.await_count == 1


@pytest.mark.asyncio
async def test_a_pre_send_decision_just_inside_the_window_is_still_reclaimed(db_session, sample_accounts):
    follower = await account(db_session, "EDGE")
    await link_strategy(db_session, sample_accounts["master"], [follower])
    await run(make_event("LIMIT", "R3_EDGE"), {str(follower.id): client_returning(funds=RuntimeError("funds down"))})
    async with TestingSessionLocal() as s:
        await s.execute(update(CopyTrade).where(CopyTrade.master_order_id == "R3_EDGE")
                        .values(created_at=datetime.now(timezone.utc) - timedelta(seconds=co.RECLAIM_WINDOW_SECONDS - 30)))
        await s.commit()
    healthy = client_returning(ok("BRK_EDGE"))
    out = await run(make_event("LIMIT", "R3_EDGE"), {str(follower.id): healthy})
    assert out[str(follower.id)]["status"] == "SUCCESS" and healthy.place_order.await_count == 1
    assert co.RECLAIM_WINDOW_SECONDS == 600                                          # the documented 10 minutes


@pytest.mark.asyncio
async def test_a_stale_pre_send_decision_stands_and_is_not_reclaimed(db_session, sample_accounts):
    follower = await account(db_session, "STALE")
    await link_strategy(db_session, sample_accounts["master"], [follower])
    await run(make_event("LIMIT", "R3_STALE"), {str(follower.id): client_returning(funds=RuntimeError("funds down"))})
    async with TestingSessionLocal() as s:
        await s.execute(update(CopyTrade).where(CopyTrade.master_order_id == "R3_STALE")
                        .values(created_at=datetime.now(timezone.utc) - timedelta(seconds=co.RECLAIM_WINDOW_SECONDS + 60)))
        await s.commit()
    healthy = client_returning(ok("BRK_LATE"))
    out = await run(make_event("LIMIT", "R3_STALE"), {str(follower.id): healthy})
    assert out[str(follower.id)] == {"status": "SKIPPED", "reason": "DUPLICATE_EVENT"}
    healthy.place_order.assert_not_awaited()
    assert (await trades_by_follower("R3_STALE"))[follower.id][0].status == CopyTradeStatus.REJECTED


# ══ every early exit of every path is recorded for the eligible Child ═══════════════════════════
# The early returns inside _process_single_* are unchanged; the outcome is recorded from the result they return
# (_record_child_result), so each exit type is proven here per path rather than assumed.

EARLY_EXITS = ["zero-lot", "risk-inputs-unavailable", "insufficient-margin", "funds-fetch-failed", "sell-blocked", "no-reference-price"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("exit_id", EARLY_EXITS)
async def test_each_early_exit_leaves_a_terminal_row_and_never_reaches_the_broker(db_session, sample_accounts, monkeypatch, kind, exit_id):
    from dataclasses import replace
    if exit_id == "no-reference-price" and kind != "MARKET":
        pytest.skip("the reference-price gate belongs to the MARKET path only")
    follower = await account(db_session, "EXIT")
    await link_strategy(db_session, sample_accounts["master"], [follower])
    client = client_returning(ok("BRK_NEVER"))
    event = make_event(kind, f"R3_EXIT_{kind}_{exit_id}")
    expected_status, expected_codes = CopyTradeStatus.REJECTED, None

    if exit_id in ("zero-lot", "risk-inputs-unavailable", "insufficient-margin"):
        db_session.add(CopySetting(follower_account_id=follower.id, copy_enabled=True, quantity_mode=QuantityMode.FIXED,
                                   fixed_qty=100 if exit_id == "insufficient-margin" else 5))
        await db_session.commit()
    if exit_id == "zero-lot":
        from app.services.copy_engine.calculator import QuantityCalculator
        monkeypatch.setattr(QuantityCalculator, "calculate_quantity", staticmethod(lambda **kwargs: 0))
        expected_status, expected_codes = CopyTradeStatus.SKIPPED, {"ZERO_LOT_RESULT"}
    elif exit_id == "risk-inputs-unavailable":
        monkeypatch.setattr("app.services.copy_engine.pipeline.get_daily_loss_today", AsyncMock(side_effect=RuntimeError("down")))
        expected_codes = {"RISK_INPUTS_UNAVAILABLE"}
    elif exit_id == "insufficient-margin":
        client.get_funds.return_value = _funds(available=Decimal("10"))
        expected_codes = {"INSUFFICIENT_MARGIN", "RISK_REJECTED"}                  # several failures collapse to RISK_REJECTED
    elif exit_id == "funds-fetch-failed":
        client.get_funds.side_effect = RuntimeError("funds down")
        expected_codes = {"FUNDS_FETCH_FAILED"}
    elif exit_id == "sell-blocked":
        event = replace(event, side="SELL")                                          # no long position at the Child
    elif exit_id == "no-reference-price":
        event = replace(event, price=Decimal("0"))
        expected_codes = {"REFERENCE_PRICE_UNAVAILABLE"}

    out = await run(event, {str(follower.id): client})
    result_dict = out[str(follower.id)]
    client.place_order.assert_not_awaited()
    assert result_dict["status"] in ("REJECTED", "SKIPPED"), result_dict
    (t,) = (await trades_by_follower(event.master_order_id))[follower.id]            # exactly one row for the Child
    assert t.client_order_id is None and t.submitted_at is None and t.broker_order_id is None
    code, _ = co.parse_reason(t.failure_reason)
    assert code, t.failure_reason
    if exit_id == "sell-blocked":
        # The SELL guard records its own row (RECONCILIATION_REQUIRED, the copy's quantity, the safety code) and R3 leaves
        # it as it is: the unique triple is taken, so there is still exactly one row.
        assert t.status == CopyTradeStatus.RECONCILIATION_REQUIRED and code == "INSUFFICIENT_LONG_POSITION"
    else:
        assert t.quantity == 0 and t.completed_at is not None
        assert t.status in (CopyTradeStatus.REJECTED, CopyTradeStatus.SKIPPED)
        assert t.status == expected_status and code in expected_codes, (t.status, t.failure_reason)
    (summary,) = await summaries(event.master_order_id)
    child = summary.metadata_payload["children"][str(follower.id)]
    assert child["status"] == result_dict["status"] and child["copy_trade_status"] == t.status.value
    _, orders = await rows(event.master_order_id)
    assert orders == []                                                              # no child Order for a pre-send exit


# ══ the compare-and-set itself ════════════════════════════════════════════════════════════════

async def seed_trade(master_order_id="R3_CAS", status=CopyTradeStatus.SKIPPED, follower=None, **overrides):
    follower = follower or uuid.uuid4()
    async with TestingSessionLocal() as s:
        if not await s.get(Account, follower):
            s.add(Account(id=follower, account_type=AccountType.FOLLOWER, display_name="cas", alice_user_id=f"CAS_{follower.hex[:8]}", is_active=True))
            await s.commit()
        values = dict(id=uuid.uuid4(), master_order_id=master_order_id, follower_account_id=follower,
                      copy_event_id=derive_copy_event_id(master_order_id, str(follower), 1), symbol="RELIANCE", exchange="NSE",
                      side=OrderSide.BUY, quantity=0, product_type="MIS", status=status, failure_reason="TRADING_DISABLED")
        values.update(overrides)
        s.add(CopyTrade(**values))
        await s.commit()
    return values


def claim_args(values, client_order_id="JT_LIM_CAS_aaaaaaaa"):
    return dict(master_order_id=values["master_order_id"], follower_account_id=values["follower_account_id"],
                copy_event_id=values["copy_event_id"], symbol="RELIANCE", exchange="NSE", side=OrderSide.BUY, quantity=10,
                product_type="MIS", estimated_price=Decimal("100"), client_order_id=client_order_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [CopyTradeStatus.SKIPPED, CopyTradeStatus.REJECTED])
async def test_compare_and_set_takes_over_a_fresh_pre_send_row_exactly_once(db_session, status):
    values = await seed_trade(status=status)
    async with TestingSessionLocal() as s1, TestingSessionLocal() as s2:
        first = await claim_child_trade(s1, **claim_args(values))
        second = await claim_child_trade(s2, **claim_args(values, "JT_LIM_CAS_bbbbbbbb"))
    assert first.kind == ClaimKind.RECLAIMED and first.trade_id == values["id"]
    assert second.kind == ClaimKind.LOST and not second.won
    async with TestingSessionLocal() as s:
        t = (await s.execute(select(CopyTrade).where(CopyTrade.id == values["id"]))).scalar_one()
    assert t.status == CopyTradeStatus.PENDING and t.client_order_id == "JT_LIM_CAS_aaaaaaaa" and t.retry_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides,why", [
    (dict(status=CopyTradeStatus.PENDING), "claimed and pending"),
    (dict(status=CopyTradeStatus.SUBMITTED, broker_order_id="B1", client_order_id="X1"), "submitted"),
    (dict(status=CopyTradeStatus.TIMEOUT, client_order_id="X2", submitted_at=datetime.now(timezone.utc)), "ambiguous"),
    (dict(status=CopyTradeStatus.REJECTED, client_order_id="X3"), "claimed then rejected by the broker"),
    (dict(status=CopyTradeStatus.REJECTED, submitted_at=datetime.now(timezone.utc)), "ever submitted"),
    (dict(status=CopyTradeStatus.REJECTED, broker_order_id="B9"), "has a broker id"),
    (dict(status=CopyTradeStatus.SUCCESS), "finished"),
    (dict(status=CopyTradeStatus.FAILED), "failed"),
    (dict(status=CopyTradeStatus.RECONCILIATION_REQUIRED), "flagged"),
], ids=lambda v: v if isinstance(v, str) else None)
async def test_a_row_that_was_ever_claimed_or_is_not_pre_send_terminal_is_never_reclaimed(db_session, overrides, why):
    values = await seed_trade(**overrides)
    async with TestingSessionLocal() as s:
        claim = await claim_child_trade(s, **claim_args(values))
    assert claim.kind == ClaimKind.LOST, why
    async with TestingSessionLocal() as s:
        t = (await s.execute(select(CopyTrade).where(CopyTrade.id == values["id"]))).scalar_one()
    assert t.status == values["status"] and t.client_order_id == values.get("client_order_id") and t.retry_count == 0


@pytest.mark.asyncio
async def test_a_client_order_id_collision_fails_closed_and_leaves_both_rows_alone(db_session):
    other = await seed_trade("R3_OTHER", status=CopyTradeStatus.PENDING, client_order_id="JT_LIM_CAS_taken")
    values = await seed_trade("R3_CAS2", status=CopyTradeStatus.SKIPPED)
    async with TestingSessionLocal() as s:
        claim = await claim_child_trade(s, **claim_args(values, "JT_LIM_CAS_taken"))
    assert claim.kind == ClaimKind.LOST
    async with TestingSessionLocal() as s:
        mine = (await s.execute(select(CopyTrade).where(CopyTrade.id == values["id"]))).scalar_one()
        theirs = (await s.execute(select(CopyTrade).where(CopyTrade.id == other["id"]))).scalar_one()
    assert mine.status == CopyTradeStatus.SKIPPED and mine.client_order_id is None and theirs.status == CopyTradeStatus.PENDING


@pytest.mark.asyncio
async def test_a_fresh_claim_inserts_the_row(db_session):
    values = dict(master_order_id="R3_NEW", follower_account_id=uuid.uuid4())
    async with TestingSessionLocal() as s:
        s.add(Account(id=values["follower_account_id"], account_type=AccountType.FOLLOWER, display_name="new", alice_user_id="NEW_1", is_active=True))
        await s.commit()
        values["copy_event_id"] = derive_copy_event_id("R3_NEW", str(values["follower_account_id"]), 1)
        claim = await claim_child_trade(s, **claim_args(values))
    assert claim.kind == ClaimKind.WON and claim.won and claim.trade_id


# ══ helpers: reason codes, mapping, finalisation, NO_CLIENT ═══════════════════════════════════

def test_reason_codes_round_trip_and_free_text_is_not_misread():
    assert co.parse_reason(co.format_reason("RISK_REJECTED", "A,B")) == ("RISK_REJECTED", "A,B")
    assert co.parse_reason("FUNDS_FETCH_FAILED") == ("FUNDS_FETCH_FAILED", "")
    assert co.parse_reason("Order was rejected by the broker") == (None, "Order was rejected by the broker")
    assert co.parse_reason(None) == (None, "") and co.parse_reason("") == (None, "")
    assert co.format_reason("X", "X") == "X"


@pytest.mark.parametrize("result_dict,expected", [
    ({"status": "SKIPPED", "reason": "TRADING_DISABLED"}, (CopyTradeStatus.SKIPPED, "TRADING_DISABLED", "")),
    ({"status": "SKIPPED", "reason": "ZERO_LOT_RESULT"}, (CopyTradeStatus.SKIPPED, "ZERO_LOT_RESULT", "")),
    ({"status": "REJECTED", "reasons": ["FUNDS_FETCH_FAILED"]}, (CopyTradeStatus.REJECTED, "FUNDS_FETCH_FAILED", "FUNDS_FETCH_FAILED")),
    ({"status": "REJECTED", "reasons": ["MAX_TRADE_VALUE", "INSUFFICIENT_MARGIN"]}, (CopyTradeStatus.REJECTED, "RISK_REJECTED", "MAX_TRADE_VALUE,INSUFFICIENT_MARGIN")),
    ({"status": "SKIPPED", "reason": "DUPLICATE_EVENT"}, None),
    ({"status": "REJECTED", "reason": "NO_CLIENT"}, None),
    ({"status": "REJECTED", "broker_order_id": None, "quantity": 10}, None),
    ({"status": "SUCCESS", "broker_order_id": "B"}, None),
    ({"status": "TIMEOUT"}, None),
    ({"status": "EXCEPTION", "error": "x"}, None),
    ("not a dict", None),
])
def test_only_pre_send_exits_map_to_an_outcome_row(result_dict, expected):
    assert co.pre_send_outcome(result_dict) == expected


@pytest.mark.asyncio
async def test_a_never_attempted_claim_is_failed_but_a_submitted_one_is_not(db_session, sample_accounts):
    values = await seed_trade("R3_NC", status=CopyTradeStatus.PENDING, client_order_id="JT_LIM_NC_1")
    pl = pipeline()
    follower = (await db_session.get(Account, values["follower_account_id"]))
    event = make_event("LIMIT", "R3_NC")
    await pl._record_child_result(event, follower, {"status": "REJECTED", "reason": "NO_CLIENT"}, 1)
    async with TestingSessionLocal() as s:
        t = (await s.execute(select(CopyTrade).where(CopyTrade.id == values["id"]))).scalar_one()
    assert t.status == CopyTradeStatus.FAILED and co.parse_reason(t.failure_reason)[0] == "NO_CLIENT"

    sent = await seed_trade("R3_NC2", status=CopyTradeStatus.PENDING, client_order_id="JT_LIM_NC_2", submitted_at=datetime.now(timezone.utc))
    follower2 = await db_session.get(Account, sent["follower_account_id"])
    await pl._record_child_result(make_event("LIMIT", "R3_NC2"), follower2, {"status": "REJECTED", "reason": "NO_CLIENT"}, 1)
    async with TestingSessionLocal() as s:
        t2 = (await s.execute(select(CopyTrade).where(CopyTrade.id == sent["id"]))).scalar_one()
    assert t2.status == CopyTradeStatus.PENDING                                       # it may have reached the broker


@pytest.mark.asyncio
async def test_finalisation_only_touches_a_row_that_is_still_pending(db_session):
    values = await seed_trade("R3_FIN2", status=CopyTradeStatus.TIMEOUT, client_order_id="JT_LIM_FIN_1")
    async with TestingSessionLocal() as s:
        changed = await co.finalize_child_trade(s, "JT_LIM_FIN_1", result(OrderResultCategory.REJECTED, message="late"))
    assert changed is False
    async with TestingSessionLocal() as s:
        assert (await s.execute(select(CopyTrade.status).where(CopyTrade.id == values["id"]))).scalar_one() == CopyTradeStatus.TIMEOUT


# ══ summary robustness ════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_summary_write_failure_never_changes_the_copy_outcome(db_session, fan, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("audit store down")

    monkeypatch.setattr("app.services.copy_engine.pipeline.record_fanout_summary", boom)
    out = await run(make_event("LIMIT", "R3_NOSUM"), fan["clients"])
    assert out[str(fan["a"].id)]["status"] == "SUCCESS"
    assert (await trades_by_follower("R3_NOSUM"))[fan["a"].id][0].status == CopyTradeStatus.SUBMITTED
    assert await summaries("R3_NOSUM") == []


@pytest.mark.asyncio
async def test_reprocessing_after_redis_loss_can_add_another_summary_so_it_is_not_exactly_once(db_session, sample_accounts):
    follower = await account(db_session, "TWICE")
    await link_strategy(db_session, sample_accounts["master"], [follower])
    await run(make_event("LIMIT", "R3_TWICE"), {str(follower.id): client_returning(funds=RuntimeError("funds down"))})
    await run(make_event("LIMIT", "R3_TWICE"), {str(follower.id): client_returning(ok("BRK_T"))})      # reclaimed and sent
    assert len(await summaries("R3_TWICE")) == 2                                                          # advisory, may repeat
    assert len((await trades_by_follower("R3_TWICE"))[follower.id]) == 1                                  # the rows stay unique


# ══ the fanout read endpoint ══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_the_fanout_endpoint_requires_the_internal_token(api_client):
    assert (await api_client.get("/api/v1/trades/fanout/ANY")).status_code == 401
    assert (await api_client.get("/api/v1/trades/fanout/ANY", headers={"X-Internal-Token": "nope"})).status_code == 401


@pytest.mark.asyncio
async def test_the_fanout_endpoint_reports_every_child_with_its_order_and_the_excluded_members(api_client, db_session, fan):
    mid = "R3_API"
    await run(make_event("LIMIT", mid), fan["clients"])
    async with TestingSessionLocal() as s:                                       # the Child's order has progressed
        await s.execute(update(Order).where(Order.master_broker_order_id == mid, Order.account_id == fan["a"].id)
                        .values(status=OrderStatus.PARTIAL, filled_quantity=4, pending_quantity=6))
        await s.commit()
    resp = await api_client.get(f"/api/v1/trades/fanout/{mid}", headers=AUTH_HEADER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["master_order_id"] == mid and body["counts"] == {"SUBMITTED": 1, "REJECTED": 1, "TIMEOUT": 1}
    kids = {c["follower_name"]: c for c in body["children"]}
    assert set(kids) == {"A", "B", "C"}
    assert kids["A"]["status"] == "SUBMITTED" and kids["A"]["broker_order_id"] == "BRK_A"
    assert kids["A"]["orders"][0]["filled_quantity"] == 4 and kids["A"]["orders"][0]["status"] == "PARTIAL"
    assert kids["B"]["status"] == "REJECTED" and kids["B"]["reason_code"] == "FUNDS_FETCH_FAILED" and kids["B"]["orders"] == []
    assert kids["C"]["status"] == "TIMEOUT"
    (summary,) = body["summaries"]
    assert summary["routed_count"] == 3 and {x["reason"] for x in summary["excluded"]} == {co.MEMBER_TRADING_DISABLED, co.ACCOUNT_INACTIVE}
    assert "authority" in body["note"]


@pytest.mark.asyncio
async def test_the_fanout_endpoint_is_empty_for_an_unknown_order_and_the_list_filters_by_master_order(api_client, db_session, fan):
    empty = (await api_client.get("/api/v1/trades/fanout/NOPE", headers=AUTH_HEADER)).json()
    assert empty["children"] == [] and empty["counts"] == {} and empty["summaries"] == []
    await run(make_event("LIMIT", "R3_LIST_1"), fan["clients"])
    listing = (await api_client.get("/api/v1/trades", params={"master_order_id": "R3_LIST_1"}, headers=AUTH_HEADER)).json()
    assert len(listing) == 3 and {t["master_order_id"] for t in listing} == {"R3_LIST_1"}
    assert (await api_client.get("/api/v1/trades", params={"master_order_id": "R3_LIST_2"}, headers=AUTH_HEADER)).json() == []


# ══ a real compare-and-set race on PostgreSQL ═════════════════════════════════════════════════

from test_duplicate_create_postgres_concurrency import postgres_environment, requires_postgres  # noqa: E402


@requires_postgres
@pytest.mark.asyncio
async def test_two_simultaneous_claims_of_a_pre_send_row_have_exactly_one_winner_on_postgresql(monkeypatch):
    async with postgres_environment(monkeypatch) as (factory, follower):
        mid = "R3_PG_CAS"
        copy_event = derive_copy_event_id(mid, str(follower.id), 1)
        async with factory() as s:
            s.add(CopyTrade(id=uuid.uuid4(), master_order_id=mid, follower_account_id=follower.id, copy_event_id=copy_event,
                            symbol="RELIANCE", exchange="NSE", side=OrderSide.BUY, quantity=0, product_type="MIS",
                            status=CopyTradeStatus.REJECTED, failure_reason="FUNDS_FETCH_FAILED"))
            await s.commit()
        args = dict(master_order_id=mid, follower_account_id=follower.id, copy_event_id=copy_event, symbol="RELIANCE",
                    exchange="NSE", side=OrderSide.BUY, quantity=10, product_type="MIS", estimated_price=Decimal("100"))

        async def attempt(n):
            async with factory() as s:
                return await claim_child_trade(s, client_order_id=f"JT_LIM_PG_{n}", **args)

        claims = await asyncio.gather(*[attempt(n) for n in range(6)])
        kinds = sorted(c.kind.value for c in claims)
        assert kinds == ["LOST"] * 5 + ["RECLAIMED"]
        async with factory() as s:
            (t,) = (await s.execute(select(CopyTrade).where(CopyTrade.master_order_id == mid))).scalars().all()
        assert t.status == CopyTradeStatus.PENDING and t.retry_count == 1 and t.client_order_id.startswith("JT_LIM_PG_")


@requires_postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_children_processed_at_the_same_instant_keep_independent_rows_on_postgresql(monkeypatch, kind):
    """No staggering: three Children run concurrently on independent PostgreSQL connections."""
    from app.models.strategies import GroupStrategy
    async with postgres_environment(monkeypatch) as (factory, first):
        async with factory() as s:
            strategy = (await s.execute(select(GroupStrategy).where(GroupStrategy.master_account_id == SAMPLE_MASTER_ID))).scalar_one()
            b = Account(id=uuid.uuid4(), account_type=AccountType.FOLLOWER, display_name="B", alice_user_id="PG_B", is_active=True)
            c = Account(id=uuid.uuid4(), account_type=AccountType.FOLLOWER, display_name="C", alice_user_id="PG_C", is_active=True)
            s.add_all([b, c])
            await s.commit()
            await add_child(s, strategy, b)
            await add_child(s, strategy, c)
        clients = {
            str(first.id): client_returning(ok("BRK_PG_A")),
            str(b.id): client_returning(funds=RuntimeError("funds down")),
            str(c.id): client_returning(result(OrderResultCategory.TIMEOUT)),
        }
        mid = f"R3_PG_FAN_{kind}"
        out = await pipeline().execute_replication(event=make_event(kind, mid), clients=clients)
        assert out["results"][str(first.id)]["status"] == "SUCCESS"
        assert out["results"][str(b.id)]["reasons"] == ["FUNDS_FETCH_FAILED"] and out["results"][str(c.id)]["status"] == "TIMEOUT"
        async with factory() as s:
            trades = {t.follower_account_id: t for t in (await s.execute(select(CopyTrade).where(CopyTrade.master_order_id == mid))).scalars().all()}
            summary = (await s.execute(select(AuditLog).where(AuditLog.event_type == co.SUMMARY_EVENT_TYPE, AuditLog.master_order_id == mid))).scalars().all()
        assert trades[first.id].status == CopyTradeStatus.SUBMITTED and trades[b.id].status == CopyTradeStatus.REJECTED
        assert trades[c.id].status == CopyTradeStatus.TIMEOUT and len(trades) == 3
        assert len(summary) == 1 and summary[0].metadata_payload["counts"] == {"SUCCESS": 1, "REJECTED": 1, "TIMEOUT": 1}
