"""
P2 #7 regression coverage: handle_master_modification's PENDING-status race.

_process_single_limit_follower pre-writes a follower's child Order row as
status=PENDING (broker_order_id=None) BEFORE calling the broker, and only
flips it to OPEN (with the real broker_order_id) AFTER the broker confirms.
If the master LIMIT order is modified in that window, handle_master_
modification's query (status IN [OPEN, PARTIAL]) does not see that
follower's row at all.

Simply adding PENDING to the status filter would NOT actually deliver the
modification: the per-order loop's existing
`if not client or not ord_rec.broker_order_id: continue` guard would still
silently skip a PENDING row (broker_order_id is always None at that stage)
via a different code path. There is no broker order yet to modify, and a
real retry/defer mechanism would need a persistent queue or background
scheduler - out of scope for a pipeline.py-only fix.

Instead, the fix makes the drop OBSERVABLE rather than silent: any PENDING
child rows for the given master_order_id are found and a WARNING
SystemEvent (event_type="MODIFICATION_SKIPPED_PENDING_RACE") is emitted for
each, and the accounts affected are surfaced in the response under
"pending_race_accounts". This does not close the race - it makes it visible
for manual follow-up instead of silently vanishing.

No live trading is enabled and no real broker calls are made anywhere in
this file - all broker clients are AsyncMock test doubles and the DB is the
in-memory test SQLite engine (db_session fixture).
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.accounts import Account, AccountType
from app.models.audit import SystemEvent
from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.types import OrderResult, OrderResultCategory
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator


async def _events_of_type(event_type):
    # Deferred import: db_session's fixture monkeypatches
    # app.core.database.AsyncSessionLocal AFTER this module is first
    # imported, so a module-level import would bind the real (unpatched)
    # symbol instead of the patched TestingSessionLocal.
    from app.core.database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        return (
            await session.execute(select(SystemEvent).where(SystemEvent.event_type == event_type))
        ).scalars().all()


def _make_pipeline(mock_redis):
    return CopyEnginePipeline(validator=EventValidator(redis=mock_redis))


@pytest.mark.asyncio
async def test_pending_only_child_order_surfaces_warning_instead_of_silent_drop(db_session, mock_redis):
    """All affected followers still PENDING (mid broker round-trip) when the
    master modifies -> handle_master_modification must not silently report
    'no_open_child_orders' and move on with zero trace. It must emit a
    WARNING SystemEvent per affected follower and surface the accounts in
    the response."""
    child_acc = Account(
        id=uuid.uuid4(),
        account_type=AccountType.FOLLOWER,
        display_name="Child Pending Only",
        alice_user_id="PEND001",
        is_active=True,
    )
    db_session.add(child_acc)
    await db_session.commit()

    db_session.add(
        Order(
            id=uuid.uuid4(),
            internal_order_id="JT_CH_M_PEND_1_001",
            broker_order_id=None,
            account_id=child_acc.id,
            master_broker_order_id="ORD_MOD_PEND_1",
            source=OrderSource.SYSTEM,
            exchange="NSE",
            symbol="RELIANCE",
            side=OrderSide.BUY,
            order_type="LIMIT",
            product_type="MIS",
            quantity=100,
            filled_quantity=0,
            pending_quantity=100,
            price=Decimal("2950.00"),
            status=OrderStatus.PENDING,
        )
    )
    await db_session.commit()

    pipeline = _make_pipeline(mock_redis)
    res = await pipeline.handle_master_modification(
        master_order_id="ORD_MOD_PEND_1",
        new_price=Decimal("2999.00"),
        new_quantity=150,
        clients={},
    )

    assert res["status"] == "no_open_child_orders"
    assert res["modifications"] == {}
    assert res["pending_race_accounts"] == [str(child_acc.id)]

    events = await _events_of_type("MODIFICATION_SKIPPED_PENDING_RACE")
    assert len(events) == 1
    assert events[0].severity.value == "WARNING"
    assert events[0].account_id == child_acc.id
    assert events[0].metadata_payload["master_order_id"] == "ORD_MOD_PEND_1"
    assert events[0].metadata_payload["internal_order_id"] == "JT_CH_M_PEND_1_001"
    assert events[0].metadata_payload["symbol"] == "RELIANCE"


@pytest.mark.asyncio
async def test_pending_child_alongside_open_child_does_not_block_the_open_one(db_session, mock_redis):
    """One follower is still PENDING (race window) while a second follower's
    order already reached OPEN. The OPEN follower must still be modified
    normally; the PENDING follower must be surfaced via a WARNING
    SystemEvent and must NOT appear in "modifications" (it was never
    attempted - there is no broker_order_id to modify yet)."""
    pending_acc = Account(
        id=uuid.uuid4(),
        account_type=AccountType.FOLLOWER,
        display_name="Child Still Pending",
        alice_user_id="PEND002",
        is_active=True,
    )
    open_acc = Account(
        id=uuid.uuid4(),
        account_type=AccountType.FOLLOWER,
        display_name="Child Already Open",
        alice_user_id="OPEN002",
        is_active=True,
    )
    db_session.add(pending_acc)
    db_session.add(open_acc)
    await db_session.commit()

    db_session.add(
        Order(
            id=uuid.uuid4(),
            internal_order_id="JT_CH_M_PEND_2_001",
            broker_order_id=None,
            account_id=pending_acc.id,
            master_broker_order_id="ORD_MOD_PEND_2",
            source=OrderSource.SYSTEM,
            exchange="NSE",
            symbol="RELIANCE",
            side=OrderSide.BUY,
            order_type="LIMIT",
            product_type="MIS",
            quantity=100,
            filled_quantity=0,
            pending_quantity=100,
            price=Decimal("2950.00"),
            status=OrderStatus.PENDING,
        )
    )
    open_order = Order(
        id=uuid.uuid4(),
        internal_order_id="JT_CH_M_PEND_2_002",
        broker_order_id="BRK_PEND_2_OPEN",
        account_id=open_acc.id,
        master_broker_order_id="ORD_MOD_PEND_2",
        source=OrderSource.SYSTEM,
        exchange="NSE",
        symbol="RELIANCE",
        side=OrderSide.BUY,
        order_type="LIMIT",
        product_type="MIS",
        quantity=100,
        filled_quantity=0,
        pending_quantity=100,
        price=Decimal("2950.00"),
        status=OrderStatus.OPEN,
    )
    db_session.add(open_order)
    await db_session.commit()

    open_client = AsyncMock()
    open_client.modify_order.return_value = OrderResult(
        category=OrderResultCategory.SUCCESS,
        client_order_id=None,
        broker_order_id="BRK_PEND_2_OPEN",
        message="Modified successfully",
        raw_response={"stat": "Ok"},
        timestamp=datetime.now(timezone.utc),
    )
    clients = {str(open_acc.id): open_client}

    pipeline = _make_pipeline(mock_redis)
    res = await pipeline.handle_master_modification(
        master_order_id="ORD_MOD_PEND_2",
        new_price=Decimal("2999.00"),
        new_quantity=150,
        clients=clients,
    )

    assert res["status"] == "completed"
    assert res["modifications"][str(open_acc.id)] == "MODIFIED"
    assert str(pending_acc.id) not in res["modifications"]
    assert res["pending_race_accounts"] == [str(pending_acc.id)]

    open_client.modify_order.assert_awaited_once()

    events = await _events_of_type("MODIFICATION_SKIPPED_PENDING_RACE")
    assert len(events) == 1
    assert events[0].account_id == pending_acc.id
