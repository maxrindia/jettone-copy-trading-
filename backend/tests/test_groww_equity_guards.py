"""
Groww Child, NSE/BSE cash equity: the GUARDS and failure paths around the lifecycle - what must NOT be sent, and what must be recorded
when Groww refuses or the order races a fill. OFFLINE (real pipeline + real GrowwClient + in-memory database + a stateful fake of
Groww's HTTP). Code-level evidence only; a fake proves what Jettone does with an answer, not what Groww answers.
"""
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.accounts import Account
from app.models.audit import SystemEvent
from app.models.trades import Order, OrderStatus
from app.services.reliability.follower_fill_syncer import FollowerFillSyncer
from app.services.reliability.reconciliation import ReconciliationEngine
from conftest import TestingSessionLocal
from groww_helpers import ok, position_row
from test_groww_equity_flow import (StatefulGroww, child_order, evt, place, repriced, send, system_events)  # noqa: F401
from test_groww_equity_lifecycle import (CHILD_IP, copy_to_groww, groww_child, groww_enabled, master_event)  # noqa: F401

pytestmark = pytest.mark.usefixtures("cross_broker_pairs_enabled")


async def attempt_exit(db_session, mock_redis, *, product, positions, oid):
    """A Master SELL (an exit) copied to a Groww Child whose broker-reported positions are `positions`."""
    master, child = await groww_child(db_session)
    fake = StatefulGroww()
    fake.positions_response = ok({"positions": positions})
    event = master_event(master, oid, side="SELL", order_type="LIMIT", product=product, exchange="NSE")
    out, client, pipeline = await copy_to_groww(mock_redis, master, child, fake, event)
    return fake, out


# ── a SELL is only ever copied against a position Groww itself confirms ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_delivery_sell_with_no_cnc_position_is_blocked_because_holdings_are_not_read(db_session, mock_redis, groww_enabled):
    """Shares bought on an earlier day are HOLDINGS, not positions, and Jettone has no verified Groww holdings call. So a delivery (CNC)
    exit with no CNC position is refused whole and nothing is sent - fail closed, never a guess."""
    fake, out = await attempt_exit(db_session, mock_redis, product="LONGTERM", positions=[], oid="CNC-NOPOS")
    assert out["status"] != "SUCCESS" and not fake.bodies("/v1/order/create")


@pytest.mark.asyncio
async def test_an_intraday_sell_with_no_position_is_blocked_so_a_short_sale_is_never_opened(db_session, mock_redis, groww_enabled):
    fake, out = await attempt_exit(db_session, mock_redis, product="INTRADAY", positions=[], oid="MIS-SHORT")
    assert out["status"] != "SUCCESS" and not fake.bodies("/v1/order/create")


@pytest.mark.asyncio
async def test_a_sell_larger_than_the_confirmed_long_is_refused_whole(db_session, mock_redis, groww_enabled):
    fake, out = await attempt_exit(db_session, mock_redis, product="INTRADAY", oid="MIS-BIG",
                                   positions=[position_row(symbol="SBIN", exchange="NSE", product="MIS", qty=4)])
    assert out["status"] != "SUCCESS" and not fake.bodies("/v1/order/create")


@pytest.mark.asyncio
async def test_a_mis_sell_is_not_covered_by_a_cnc_position_and_the_reverse(db_session, mock_redis, groww_enabled):
    fake, out = await attempt_exit(db_session, mock_redis, product="INTRADAY", oid="MIS-VS-CNC",
                                   positions=[position_row(symbol="SBIN", exchange="NSE", product="CNC", qty=50)])
    assert out["status"] != "SUCCESS" and not fake.bodies("/v1/order/create")
    fake, out = await attempt_exit(db_session, mock_redis, product="LONGTERM", oid="CNC-VS-MIS",
                                   positions=[position_row(symbol="SBIN", exchange="NSE", product="MIS", qty=50)])
    assert out["status"] != "SUCCESS" and not fake.bodies("/v1/order/create")


@pytest.mark.asyncio
async def test_a_bse_sell_is_not_covered_by_an_nse_position(db_session, mock_redis, groww_enabled):
    master, child = await groww_child(db_session)
    fake = StatefulGroww()
    fake.positions_response = ok({"positions": [position_row(symbol="SBIN", exchange="NSE", product="MIS", qty=50)]})
    event = master_event(master, "BSE-VS-NSE", side="SELL", order_type="LIMIT", product="INTRADAY", exchange="BSE")
    out, _, _ = await copy_to_groww(mock_redis, master, child, fake, event)
    assert out["status"] != "SUCCESS" and not fake.bodies("/v1/order/create")


# ── Groww refuses, or the order races a fill ───────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_groww_modify_refusal_leaves_the_order_unchanged_and_is_alerted(db_session, mock_redis, groww_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    fake.fail_modify = True
    await send(pipeline, client, child, repriced(created, "801"))
    assert len(fake.bodies("/v1/order/modify")) == 1                                     # tried once, not retried in a loop
    assert float(fake.book[fake.only]["price"]) == 800.0 and float((await child_order(child)).price) == 800.0
    assert "CHILD_MODIFICATION_FAILED" in await system_events()


@pytest.mark.asyncio
async def test_a_second_modify_while_groww_still_shows_modification_requested_is_refused_and_alerted(db_session, mock_redis, groww_enabled):
    """Real Groww reports MODIFICATION_REQUESTED between a modify and its confirmation. The adapter only modifies an order it sees in an
    open, settled state, so a second quick Master change is refused (alerted), never sent on top of an unconfirmed one."""
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    await send(pipeline, client, child, repriced(created, "801"))
    fake.book[fake.only]["status"] = "MODIFICATION_REQUESTED"
    await send(pipeline, client, child, repriced(created, "802"))
    assert len(fake.bodies("/v1/order/modify")) == 1 and float(fake.book[fake.only]["price"]) == 801.0
    assert "CHILD_MODIFICATION_FAILED" in await system_events()


@pytest.mark.asyncio
async def test_a_groww_cancel_refusal_never_marks_the_order_cancelled(db_session, mock_redis, groww_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    fake.fail_cancel = True
    await send(pipeline, client, child, evt(created, event_type="ORDER_CANCELLED", status="CANCELLED"))
    assert len(fake.bodies("/v1/order/cancel")) == 1
    assert (await child_order(child)).status == OrderStatus.OPEN and fake.book[fake.only]["status"] == "OPEN"
    assert any("CANCEL" in kind for kind in await system_events())                       # surfaced, not swallowed


@pytest.mark.asyncio
async def test_a_cancel_that_races_a_full_fill_does_not_record_a_cancellation(db_session, mock_redis, groww_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    fake.fill(fake.only, 10)                                                             # it filled at Groww before the cancel got there
    await send(pipeline, client, child, evt(created, event_type="ORDER_CANCELLED", status="CANCELLED"))
    order = await child_order(child)
    assert order.status != OrderStatus.CANCELLED and fake.book[fake.only]["status"] == "EXECUTED"


@pytest.mark.asyncio
async def test_a_master_cancel_after_the_child_is_already_complete_sends_no_cancel(db_session, mock_redis, groww_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    fake.fill(fake.only, 10)
    await FollowerFillSyncer(follower_clients_provider=lambda: {str(child.id): client}, pipeline=pipeline).sync_once()
    assert (await child_order(child)).status == OrderStatus.COMPLETE
    await send(pipeline, client, child, evt(created, event_type="ORDER_CANCELLED", status="CANCELLED"))
    assert not fake.bodies("/v1/order/cancel")


# ── reconciliation against Groww's own positions ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reconciliation_matches_a_filled_groww_order_against_groww_positions(db_session, mock_redis, groww_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    fake.fill(fake.only, 10)
    await FollowerFillSyncer(follower_clients_provider=lambda: {str(child.id): client}, pipeline=pipeline).sync_once()
    fake.positions_response = ok({"positions": [position_row(symbol="SBIN", exchange="NSE", product="MIS", qty=10)]})
    async with TestingSessionLocal() as db:
        assert await ReconciliationEngine.run_for_account(child.id, db, broker_client=client) == []
    fake.positions_response = ok({"positions": [position_row(symbol="SBIN", exchange="NSE", product="MIS", qty=7)]})
    async with TestingSessionLocal() as db:
        records = await ReconciliationEngine.run_for_account(child.id, db, broker_client=client)
    assert [(r.symbol, r.expected_qty, r.actual_qty) for r in records] == [("SBIN", 10, 7)]


@pytest.mark.asyncio
async def test_a_modify_that_races_a_full_fill_is_refused_and_never_resurrects_the_order(db_session, mock_redis, groww_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    fake.fill(fake.only, 10)                                                             # filled at Groww; Jettone has not synced it yet
    await send(pipeline, client, child, repriced(created, "801"))
    assert not fake.bodies("/v1/order/modify") and not fake.bodies("/v1/order/cancel")
    assert float(fake.book[fake.only]["price"]) == 800.0 and len(fake.bodies("/v1/order/create")) == 1
    assert "CHILD_MODIFICATION_FAILED" in await system_events()


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type", ["SL", "SLM"])
async def test_a_master_sl_trigger_after_groww_already_executed_the_child_does_not_modify_it(db_session, mock_redis, groww_enabled, order_type):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis, order_type=order_type)
    fake.fill(fake.only, 10)                                                             # Groww's own trigger fired and filled it first
    triggered = evt(created, event_type="ORDER_FILLED", order_type="MARKET", status="COMPLETE", price=Decimal("796"), limit_price=Decimal("0"),
                    trigger_price=Decimal("795"), filled_quantity=10, pending_quantity=0, fill_sequence=10)
    await send(pipeline, client, child, triggered)
    assert not fake.bodies("/v1/order/modify") and not fake.bodies("/v1/order/cancel") and len(fake.bodies("/v1/order/create")) == 1
