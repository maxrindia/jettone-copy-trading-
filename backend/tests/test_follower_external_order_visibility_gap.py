"""
P3 Finding #13 - documentation-only regression coverage for a known, accepted
design limitation (NOT a bug - read-only investigated, no source file
touched or changed by this file).

Background: only the MASTER account gets a persistent WebSocket connection
(see backend/app/services/runtime/engine_loop.py). `FollowerFillSyncer`
(backend/app/services/reliability/follower_fill_syncer.py) only polls Order
rows that ALREADY exist in Jettone's DB (source=SYSTEM, i.e. rows the copy
engine itself created when it placed a follower order) - it matches broker
order-book records back to existing rows by broker_order_id and never
creates new Order rows for broker_order_ids it doesn't already know about.

Consequence: if a follower's demat account holder places a trade directly
at the broker (outside Jettone entirely - e.g. via the Alice Blue mobile
app), Jettone never learns about it as an Order row at all. It has no
corresponding row, so it is invisible to both FollowerFillSyncer and to
ReconciliationEngine.compute_expected_positions (which only sums existing
SYSTEM-sourced Order rows - see test_reconciliation_source_of_truth.py).

This investigation (read-only against reconciliation.py and
follower_fill_syncer.py, neither of which is modified here) confirms the
prior audit's conclusion still holds: ReconciliationEngine correctly
reports `actual_qty > expected_qty` for that symbol and fires a CRITICAL
POSITION_MISMATCH_DETECTED SystemEvent for as long as the externally-placed
position stays open - it does NOT silently ignore the extra position, and
does NOT misattribute it to the wrong symbol/account. That is the safer
failure mode (alert + require manual reconciliation) versus silently
under-reporting a real position. This is a design gap in *visibility*
(Jettone doesn't know an externally-placed order happened until the next
reconciliation pass notices the resulting position drift), not a bug in
the *mismatch detection* logic itself, which behaves correctly given the
information available to it.

One caveat surfaced by this investigation, worth flagging even though it
doesn't change the "documented design limitation, not a bug" conclusion:
reconciliation for follower accounts only runs (a) once at EngineLoop
startup via run_crash_recovery, or (b) on-demand via the manual
POST /reliability/reconciliation/run API route - there is no periodic/
scheduled background reconciliation loop anywhere in the engine (confirmed
by grep across backend/app for reconciliation callers and backend/app/main.py's
lifespan). So an externally-placed follower position is only surfaced at
the next process restart or the next manual trigger, not continuously -
a latency characteristic of *when* the correct alert fires, not a
correctness problem with the alert itself.

This file adds no source changes - it only exercises the existing, already
-tested-elsewhere ReconciliationEngine.run_for_account with a fabricated
"broker reports a position with zero matching Order rows" scenario, using
the same conventions as test_reconciliation_source_of_truth.py, to make the
by-design behavior concrete and demonstrable rather than only asserted in
prose.
"""
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.audit import SystemEvent
from app.models.reconciliation import ReconciliationRecord
from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.types import Position
from app.services.reliability.reconciliation import ReconciliationEngine


@pytest.mark.asyncio
async def test_externally_placed_follower_order_correctly_triggers_mismatch_alert(db_session, sample_accounts):
    """
    Simulates the exact P3 #13 scenario: a follower has one Jettone-known
    position (a SYSTEM-sourced Order Jettone itself placed, fully filled)
    plus a second symbol the follower bought directly at the broker with no
    corresponding Order row at all (nothing in this codebase observes a
    follower's own broker-side activity outside what Jettone itself placed).

    Expected/by-design outcome: the known symbol reconciles cleanly, and the
    externally-placed symbol is reported as a real mismatch (expected 0,
    actual > 0) via both a ReconciliationRecord and a CRITICAL
    POSITION_MISMATCH_DETECTED SystemEvent - proving Jettone alerts on the
    gap rather than silently under-reporting the follower's real position.
    """
    follower = sample_accounts["follower1"]

    # The only Order row Jettone actually knows about for this follower.
    known_order = Order(
        internal_order_id="JT_CH_KNOWN_1",
        broker_order_id="BRK_KNOWN_1",
        account_id=follower.id,
        master_broker_order_id="ORD_MASTER_1",
        source=OrderSource.SYSTEM,
        exchange="NFO",
        symbol="NIFTY",
        side=OrderSide.BUY,
        order_type="MARKET",
        product_type="MIS",
        quantity=50,
        filled_quantity=50,
        pending_quantity=0,
        price=Decimal("100.00"),
        status=OrderStatus.COMPLETE,
    )
    db_session.add(known_order)
    await db_session.commit()

    # The broker reports BOTH the known position AND a second symbol the
    # follower bought directly at the broker - Jettone has zero Order rows
    # for "BANKNIFTY" for this account, by construction.
    client = AsyncMock()
    client.get_positions.return_value = [
        Position(symbol="NIFTY", exchange="NFO", product_type="MIS", quantity=50, side="BUY", average_price=Decimal("100"), account_id=str(follower.id)),
        Position(symbol="BANKNIFTY", exchange="NFO", product_type="MIS", quantity=20, side="BUY", average_price=Decimal("450"), account_id=str(follower.id)),
    ]

    mismatches = await ReconciliationEngine.run_for_account(follower.id, db_session, broker_client=client)

    # The known symbol reconciles cleanly; only the externally-placed one mismatches.
    assert len(mismatches) == 1
    assert mismatches[0].symbol == "BANKNIFTY"
    assert mismatches[0].expected_qty == 0  # Jettone has no Order row for it at all
    assert mismatches[0].actual_qty == 20
    assert mismatches[0].discrepancy_qty == 20

    records = (
        await db_session.execute(select(ReconciliationRecord).where(ReconciliationRecord.account_id == follower.id))
    ).scalars().all()
    assert len(records) == 1
    assert records[0].symbol == "BANKNIFTY"

    events = (
        await db_session.execute(select(SystemEvent).where(SystemEvent.event_type == "POSITION_MISMATCH_DETECTED"))
    ).scalars().all()
    assert len(events) == 1
    assert events[0].metadata_payload["symbol"] == "BANKNIFTY"
    assert events[0].metadata_payload["expected_qty"] == 0
    assert events[0].metadata_payload["actual_qty"] == 20
    # CRITICAL, not silently ignored - the safer failure mode for a real-money system.
    assert events[0].severity == "CRITICAL"
