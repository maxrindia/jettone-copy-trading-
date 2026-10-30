"""
Groww Child, NSE/BSE cash equity: how Jettone TRACKS an accepted Groww order afterwards (the fill syncer), including the statuses Groww
reports for stop-loss orders and for orders ended outside Jettone. OFFLINE (real syncer + real GrowwClient + in-memory database + a
stateful fake of Groww's HTTP). Code-level evidence only: the statuses the fake reports are the documented vocabulary, not a recording.
"""
import pytest
from sqlalchemy import select

from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderStatus
from app.services.reliability.follower_fill_syncer import FollowerFillSyncer
from conftest import TestingSessionLocal
from test_groww_equity_flow import child_order, place
from test_groww_equity_lifecycle import groww_enabled  # noqa: F401

pytestmark = pytest.mark.usefixtures("cross_broker_pairs_enabled")


def syncer_for(child, client, pipeline):
    return FollowerFillSyncer(follower_clients_provider=lambda: {str(child.id): client}, pipeline=pipeline)


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type", ["SL", "SLM"])
async def test_a_stop_loss_waiting_for_its_trigger_stays_pending_then_completes_when_groww_fills_it(db_session, mock_redis, groww_enabled, order_type):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis, order_type=order_type)
    sync = syncer_for(child, client, pipeline)
    assert fake.book[fake.only]["status"] == "TRIGGER_PENDING"
    await sync.sync_once()
    order = await child_order(child)
    assert order.status == OrderStatus.PENDING and order.filled_quantity == 0            # still waiting: not failed, not open, not done
    fake.book[fake.only]["status"] = "OPEN"                                              # Groww's trigger fired; now working as a limit/market
    await sync.sync_once()
    fake.fill(fake.only, 10)
    await sync.sync_once()
    order = await child_order(child)
    assert order.status == OrderStatus.COMPLETE and order.filled_quantity == 10


@pytest.mark.asyncio
async def test_an_order_groww_rejects_after_accepting_it_is_recorded_rejected(db_session, mock_redis, groww_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    fake.book[fake.only]["status"] = "REJECTED"
    await syncer_for(child, client, pipeline).sync_once()
    order = await child_order(child)
    assert order.status == OrderStatus.REJECTED and order.filled_quantity == 0


@pytest.mark.asyncio
async def test_an_order_cancelled_outside_jettone_is_recorded_cancelled(db_session, mock_redis, groww_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    fake.book[fake.only]["status"] = "CANCELLED"                                         # the owner cancelled it in the Groww app
    await syncer_for(child, client, pipeline).sync_once()
    order = await child_order(child)
    assert order.status == OrderStatus.CANCELLED and order.filled_quantity == 0


@pytest.mark.asyncio
async def test_a_partly_filled_order_cancelled_outside_jettone_keeps_its_fill(db_session, mock_redis, groww_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    fake.fill(fake.only, 4)
    sync = syncer_for(child, client, pipeline)
    await sync.sync_once()
    fake.book[fake.only]["status"] = "CANCELLED"
    await sync.sync_once()
    order = await child_order(child)
    assert order.filled_quantity == 4 and order.status in (OrderStatus.PARTIAL, OrderStatus.CANCELLED)
    assert order.status != OrderStatus.COMPLETE                                          # never read as a full fill


@pytest.mark.asyncio
async def test_the_syncer_never_writes_a_second_order_or_changes_what_it_did_not_place(db_session, mock_redis, groww_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    sync = syncer_for(child, client, pipeline)
    for _ in range(3):
        await sync.sync_once()
    async with TestingSessionLocal() as s:
        assert len((await s.execute(select(Order).where(Order.account_id == child.id))).scalars().all()) == 1
        copies = (await s.execute(select(CopyTrade).where(CopyTrade.follower_account_id == child.id))).scalars().all()
    assert len(copies) == 1 and len(fake.bodies("/v1/order/create")) == 1
    assert not fake.bodies("/v1/order/modify") and not fake.bodies("/v1/order/cancel")   # the syncer only ever reads


@pytest.mark.asyncio
async def test_an_unreadable_groww_order_list_changes_nothing(db_session, mock_redis, groww_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    before = await child_order(child)
    fake.list_override = (200, {"status": "SUCCESS", "payload": {"unexpected": []}})        # no order_list array
    await syncer_for(child, client, pipeline).sync_once()
    after = await child_order(child)
    assert (after.status, after.filled_quantity) == (before.status, before.filled_quantity)
