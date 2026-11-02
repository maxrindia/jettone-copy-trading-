"""
Regression: an Alice Blue Master F&O order is NOT copied to a Groww Child - by design, whatever the Groww session says.

Production, 2 Nov 2026 (10:44-10:48 IST): the owner placed NIFTY option orders (NFO, MARKET, LONGTERM, qty 65) on the Alice Blue Master. The
Master poller saw each one, the fan-out started, the Alice Child was copied - and the Groww Child was SKIPPED with
`INSTRUMENT_UNMAPPED: CROSS_BROKER_DERIVATIVE_UNMAPPED:alicebluenew->groww:NFO`, although the Groww login had succeeded and the pair canary
was open. The skip is decided before any Groww call (instruments/planner.cross_broker_derivative_skip, applied in the pipeline), because
no broker's derivative symbol is a verified identity on another broker and the Groww Child only supports NSE/BSE cash equity.

These tests pin that behaviour with the production-shaped event, with a valid Groww session AND with none, and show that the same
Groww setup copies a CASH order (so the session/gates are sufficient for what Groww supports). They change nothing: the rule is
documented, intended and fail-closed. Offline: fakes only.
"""
import pytest
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.trades import CopyTrade, CopyTradeStatus, Order
from conftest import TestingSessionLocal
from groww_helpers import FakeGroww, make_client
from test_groww_equity_lifecycle import (CHILD_IP, copy_to_groww, groww_child, groww_enabled, holds, master_event,  # noqa: F401
                                         new_pipeline)
from test_order_handling_matrix import _event

pytestmark = pytest.mark.usefixtures("cross_broker_pairs_enabled")

PRODUCTION_REASON = "INSTRUMENT_UNMAPPED: CROSS_BROKER_DERIVATIVE_UNMAPPED:alicebluenew->groww:NFO"


def nifty_option(master, oid, *, side="BUY", order_type="MARKET"):
    """The production event: NIFTY, NFO, LONGTERM (Alice's name for carry), qty 65 - `_event`'s own defaults."""
    return _event(master, oid, side=side, order_type=order_type)


def groww_calls(fake):
    return [r.url.path for r in fake.requests if r.url.path != "/v1/token/api/access"]


async def child_rows(child):
    async with TestingSessionLocal() as s:
        copies = (await s.execute(select(CopyTrade).where(CopyTrade.follower_account_id == child.id))).scalars().all()
        orders = (await s.execute(select(Order).where(Order.account_id == child.id))).scalars().all()
        audits = (await s.execute(select(AuditLog).where(AuditLog.account_id == child.id))).scalars().all()
    return copies, orders, audits


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("order_type", ["MARKET", "LIMIT", "SL", "SLM"])
async def test_an_alice_derivative_order_is_skipped_for_a_groww_child_with_a_valid_session(db_session, mock_redis, groww_enabled, order_type, side):
    master, child = await groww_child(db_session)
    fake = FakeGroww()
    out, client, _ = await copy_to_groww(mock_redis, master, child, fake, nifty_option(master, f"NFO-{order_type}-{side}", side=side, order_type=order_type))
    assert (await client.get_session()) and groww_calls(fake) == []                  # logged in, session valid - and not one Groww call was made
    assert out is not None and out.get("status") != "SUCCESS" and "INSTRUMENT_UNMAPPED" in str(out)
    copies, orders, audits = await child_rows(child)
    assert [c.status for c in copies] == [CopyTradeStatus.SKIPPED] and copies[0].failure_reason == PRODUCTION_REASON and copies[0].quantity == 0
    assert orders == [] and "COPY_SKIPPED" in {a.event_type for a in audits}


@pytest.mark.asyncio
async def test_the_skip_does_not_depend_on_the_groww_session_at_all(db_session, mock_redis, groww_enabled):
    """No login, no session: the reason is still the instrument rule, not a session error - the decision comes before the client is touched."""
    master, child = await groww_child(db_session)
    fake = FakeGroww()
    client = make_client(mock_redis, fake, account_id=str(child.id), static_ip=CHILD_IP)               # never logged in
    res = await new_pipeline(mock_redis).execute_replication(event=nifty_option(master, "NFO-NOSESSION"), clients={str(child.id): client})
    assert "INSTRUMENT_UNMAPPED" in str((res.get("results") or {}).get(str(child.id))) and fake.requests == []
    copies, _, _ = await child_rows(child)
    assert copies[0].failure_reason == PRODUCTION_REASON


@pytest.mark.asyncio
async def test_the_same_groww_setup_copies_a_cash_order_so_session_and_gates_are_sufficient_for_what_groww_supports(db_session, mock_redis, groww_enabled):
    master, child = await groww_child(db_session)
    fake = FakeGroww()
    fake.positions_response = holds("NSE", "MIS")
    out, client, _ = await copy_to_groww(mock_redis, master, child, fake,
                                         master_event(master, "CASH-CONTROL", side="BUY", order_type="MARKET", product="INTRADAY", exchange="NSE"))
    assert out and out.get("status") == "SUCCESS" and len(fake.bodies("/v1/order/create")) == 1
    copies, orders, _ = await child_rows(child)
    assert [c.status for c in copies] == [CopyTradeStatus.SUBMITTED] and len(orders) == 1          # SUBMITTED until fill sync


@pytest.mark.asyncio
async def test_a_skipped_derivative_for_groww_leaves_the_other_child_to_be_copied(db_session, mock_redis, groww_enabled):
    """The skip is per Child ("other Children are unaffected"): the plan for an Alice Child of the same Master is not touched by it."""
    from app.services.instruments.planner import cross_broker_derivative_skip

    groww = cross_broker_derivative_skip(master_broker="alicebluenew", child_broker="groww", exchange="NFO")
    alice = cross_broker_derivative_skip(master_broker="alicebluenew", child_broker="alicebluenew", exchange="NFO")
    cash = cross_broker_derivative_skip(master_broker="alicebluenew", child_broker="groww", exchange="NSE")
    assert groww is not None and groww.code == "INSTRUMENT_UNMAPPED" and alice is None and cash is None
