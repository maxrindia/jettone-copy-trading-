"""
P2 Finding #6 regression coverage: ReconciliationEngine.run_crash_recovery
must not run position reconciliation for MASTER-type accounts.

Background: compute_expected_positions only ever sums Order rows with
source=SYSTEM. A master account's own orders are always tagged
INTERNAL_UI (manually placed via broker.py) or BROKER_EXTERNAL (observed
via the WS feed) - never SYSTEM, which is reserved for follower orders the
copy engine places. That makes compute_expected_positions structurally
return {} for a master account on every run, regardless of what it
actually holds at the broker - so comparing that {} against the master's
real broker positions always reports a full mismatch and fires a CRITICAL
POSITION_MISMATCH_DETECTED SystemEvent, purely as false-positive noise on
every crash-recovery run (i.e. every process restart).

This file does NOT touch compute_expected_positions or run_for_account
themselves (their behavior for follower accounts, and their fail-closed
behavior with no broker data source, is deliberate and correct - see
test_reconciliation_source_of_truth.py). It only proves that
run_crash_recovery no longer hands a MASTER account to run_for_account.

No live trading is enabled and no real broker calls are made anywhere in
this file - all broker clients are AsyncMock test doubles.
"""
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.reconciliation import ReconciliationRecord
from app.models.audit import SystemEvent
from app.services.alice_blue.types import Position
from app.services.reliability.reconciliation import ReconciliationEngine


@pytest.mark.asyncio
async def test_crash_recovery_does_not_fire_false_master_mismatch(db_session, sample_accounts):
    """
    The exact bug in P2 Finding #6: the master account has a real position
    at the broker (NIFTY qty 50) but - correctly - no SYSTEM-sourced Order
    rows (master orders are never source=SYSTEM). Before the fix,
    run_crash_recovery reconciled the master account too, so
    compute_expected_positions returned {} for it and run_for_account
    reported a fabricated "expected 0, actual 50" mismatch: a
    POSITION_MISMATCH_DETECTED SystemEvent and a ReconciliationRecord for
    the master account, on every crash recovery.

    After the fix, the master account must not be reconciled at all, so
    neither of those must exist for it.
    """
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]

    master_client = AsyncMock()
    master_client.get_positions.return_value = [
        Position(
            symbol="NIFTY",
            exchange="NFO",
            product_type="MIS",
            quantity=50,
            side="BUY",
            average_price=Decimal("100"),
            account_id=str(master.id),
        )
    ]
    follower_client = AsyncMock()
    follower_client.get_positions.return_value = []  # follower flat, no SYSTEM orders either - no real mismatch

    clients = {str(master.id): master_client, str(follower.id): follower_client}

    def client_provider(account_id: str):
        return clients.get(account_id)

    await ReconciliationEngine.run_crash_recovery(db_session, client_provider=client_provider)

    # The broker client for the master must never even be consulted.
    master_client.get_positions.assert_not_called()

    mismatch_events = (
        await db_session.execute(select(SystemEvent).where(SystemEvent.event_type == "POSITION_MISMATCH_DETECTED"))
    ).scalars().all()
    assert mismatch_events == []  # no false-positive fired for the master (and follower had no real mismatch)

    master_records = (
        await db_session.execute(select(ReconciliationRecord).where(ReconciliationRecord.account_id == master.id))
    ).scalars().all()
    assert master_records == []


@pytest.mark.asyncio
async def test_crash_recovery_never_calls_run_for_account_with_master_id(db_session, sample_accounts):
    """
    Direct proof of the fix: run_crash_recovery's account-iteration loop
    must never invoke run_for_account with the master account's id, while
    it must still invoke it for the follower (follower reconciliation is
    unaffected).
    """
    master = sample_accounts["master"]
    follower = sample_accounts["follower1"]

    original_run_for_account = ReconciliationEngine.run_for_account
    spy = AsyncMock(side_effect=original_run_for_account)

    client = AsyncMock()
    client.get_positions.return_value = []

    with patch.object(ReconciliationEngine, "run_for_account", spy):
        await ReconciliationEngine.run_crash_recovery(db_session, client_provider=lambda acc_id: client)

    called_account_ids = [call.args[0] for call in spy.await_args_list]
    assert master.id not in called_account_ids
    assert follower.id in called_account_ids
