"""
Groww Child, NSE/BSE cash equity: the order LIFECYCLE after placement - modify, cancel, SL modify and trigger, partial fills, fill sync,
duplicates and ambiguous sends. OFFLINE: real CopyEnginePipeline + real GrowwClient + the in-memory database; only Groww's HTTP is faked.

The fake is STATEFUL: an order created, modified or cancelled through it is what the next order-list / order-detail read reports,
the way the real order would be. It answers in the shapes Groww's documentation (and the shapes recorded from the live account) describe,
so these tests prove what Jettone SENDS and how it RECORDS the outcome. They are code-level evidence, not proof that Groww accepts
or reports an order this way: see the Phase A report for what was seen live.
"""
import dataclasses
import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.models.audit import SystemEvent
from app.models.trades import Order, OrderStatus
from app.services.reliability.follower_fill_syncer import FollowerFillSyncer
from conftest import TestingSessionLocal
from groww_helpers import FakeGroww, make_client, order_row
from test_groww_equity_lifecycle import (CHILD_IP, EXCHANGES, copy_to_groww, groww_child, groww_enabled, holds,  # noqa: F401
                                          master_event, new_pipeline)

pytestmark = pytest.mark.usefixtures("cross_broker_pairs_enabled")


class StatefulGroww(FakeGroww):
    def __init__(self):
        super().__init__()
        self.book = {}
        self.n = 0
        self.fail_modify = False       # Groww answers a modify with a failure and changes nothing
        self.fail_cancel = False       # Groww answers a cancel with a failure and changes nothing
        self.list_override = None      # (http status, body) the order list returns instead of the real book

    def row(self, oid):
        o = self.book[oid]
        r = order_row(oid, o["status"], o["qty"], o["filled"], side=o["side"], otype=o["otype"], price=o["price"], symbol=o["symbol"],
                      product=o["product"], ref=o["ref"], trigger=o["trigger"])
        r["exchange"] = o["exchange"]
        if o["filled"] > 0:                                    # an executed SL-M has no limit price; it still fills at a real price
            r["average_fill_price"] = float(o["price"] or o["trigger"] or 1)
        return r

    def fill(self, oid, filled, status=None):
        o = self.book[oid]
        o["filled"] = filled
        o["status"] = status or ("EXECUTED" if filled >= o["qty"] else "OPEN")

    @property
    def only(self):
        (oid,) = self.book
        return oid

    async def __call__(self, request):
        path = request.url.path
        if path == "/v1/order/create":
            self.requests.append(request)
            if self.create_hook:
                self.create_hook(request)
            if self.create_exception:
                raise self.create_exception
            body = json.loads(request.content)
            self.n += 1
            oid = f"GRW-{self.n}"
            self.book[oid] = dict(status="TRIGGER_PENDING" if body["order_type"] in ("SL", "SL_M") else "OPEN", qty=body["quantity"], filled=0,
                                  side=body["transaction_type"], otype=body["order_type"], price=body.get("price", 0),
                                  trigger=body.get("trigger_price", 0), symbol=body["trading_symbol"], product=body["product"],
                                  ref=body["order_reference_id"], exchange=body["exchange"])
            return httpx.Response(200, json={"status": "SUCCESS", "payload": {"groww_order_id": oid, "order_status": "OPEN",
                                                                               "order_reference_id": body["order_reference_id"]}})
        if path == "/v1/order/modify":
            self.requests.append(request)
            body = json.loads(request.content)
            if self.fail_modify:
                return httpx.Response(400, json={"status": "FAILURE"})
            o = self.book[body["groww_order_id"]]
            o.update(qty=body["quantity"], otype=body["order_type"], price=body.get("price", o["price"]), trigger=body.get("trigger_price", o["trigger"]))
            return httpx.Response(200, json={"status": "SUCCESS", "payload": {"groww_order_id": body["groww_order_id"],
                                                                               "order_status": "MODIFICATION_REQUESTED"}})
        if path == "/v1/order/cancel":
            self.requests.append(request)
            body = json.loads(request.content)
            if self.fail_cancel or self.book[body["groww_order_id"]]["status"] in ("EXECUTED", "CANCELLED", "REJECTED"):
                return httpx.Response(400, json={"status": "FAILURE"})                      # a finished order cannot be cancelled
            self.book[body["groww_order_id"]]["status"] = "CANCELLED"
            return httpx.Response(200, json={"status": "SUCCESS", "payload": {"groww_order_id": body["groww_order_id"],
                                                                               "order_status": "CANCELLATION_REQUESTED"}})
        if path.startswith("/v1/order/detail/"):
            self.requests.append(request)
            oid = path.rsplit("/", 1)[1]
            if oid in self.book:
                return httpx.Response(200, json={"status": "SUCCESS", "payload": self.row(oid)})
            return httpx.Response(404, json={"status": "FAILURE"})
        if path == "/v1/order/list":
            self.requests.append(request)
            if self.list_override is not None:
                return httpx.Response(self.list_override[0], json=self.list_override[1])
            return httpx.Response(200, json={"status": "SUCCESS", "payload": {"order_list": [self.row(o) for o in self.book]}})
        if path.startswith("/v1/order/status/reference/"):
            self.requests.append(request)
            ref = path.rsplit("/", 1)[1]
            for oid, o in self.book.items():
                if o["ref"] == ref:
                    return httpx.Response(200, json={"status": "SUCCESS", "payload": {"groww_order_id": oid}})
            return httpx.Response(404, json={"status": "FAILURE"})
        return await super().__call__(request)


def evt(event, **changes):
    return dataclasses.replace(event, **changes)


async def place(db_session, mock_redis, *, side="BUY", product="INTRADAY", exchange="NSE", order_type="LIMIT", oid="L-1"):
    master, child = await groww_child(db_session)
    fake = StatefulGroww()
    fake.positions_response = holds(exchange, "CNC" if product == "LONGTERM" else "MIS", qty=50)
    created = evt(master_event(master, oid, side=side, order_type=order_type, product=product, exchange=exchange),
                  event_type="SL_PENDING_CREATED" if order_type in ("SL", "SLM") else "LIMIT_CREATED")
    out, client, pipeline = await copy_to_groww(mock_redis, master, child, fake, created)
    assert out and out["status"] == "SUCCESS", out
    return master, child, fake, client, pipeline, created


async def send(pipeline, client, child, event):
    return await pipeline.execute_replication(event=event, clients={str(child.id): client})


async def child_order(child):
    async with TestingSessionLocal() as s:
        return (await s.execute(select(Order).where(Order.account_id == child.id))).scalars().one()


async def system_events():
    async with TestingSessionLocal() as s:
        return [e.event_type for e in (await s.execute(select(SystemEvent))).scalars().all()]


def repriced(created, price):
    return evt(created, event_type="ORDER_MODIFIED", price=Decimal(price), limit_price=Decimal(price))


# ── modify ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("product", ["INTRADAY", "LONGTERM"])
@pytest.mark.parametrize("exchange", EXCHANGES)
async def test_a_master_limit_price_modify_reaches_the_groww_order(db_session, mock_redis, groww_enabled, exchange, product):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis, exchange=exchange, product=product)
    await send(pipeline, client, child, repriced(created, "801"))
    assert fake.bodies("/v1/order/modify") == [{"quantity": 10, "order_type": "LIMIT", "segment": "CASH", "groww_order_id": fake.only, "price": 801.0}]
    assert float(fake.book[fake.only]["price"]) == 801.0
    assert len(fake.bodies("/v1/order/create")) == 1                                     # modified in place, never replaced
    assert float((await child_order(child)).price) == 801.0


@pytest.mark.asyncio
async def test_a_master_quantity_modify_is_scaled_and_sent(db_session, mock_redis, groww_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    await send(pipeline, client, child, evt(created, event_type="ORDER_MODIFIED", quantity=20, total_quantity=20, pending_quantity=20))
    (body,) = fake.bodies("/v1/order/modify")
    assert body["quantity"] == 20 and body["order_type"] == "LIMIT" and body["price"] == 800.0
    order = await child_order(child)
    assert order.pending_quantity == 20 and order.quantity == 10          # the row keeps the original size; the working size is pending_quantity


@pytest.mark.asyncio
async def test_a_repeated_modify_event_never_creates_or_cancels_anything(db_session, mock_redis, groww_enabled):
    """The pipeline does not de-duplicate ORDER_MODIFIED (for any broker): a repeat is sent again with the same values. That is idempotent
    for the order itself - it ends at the same price, with one Groww order and no cancel - and is documented here, not changed."""
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    modified = repriced(created, "801")
    await send(pipeline, client, child, modified)
    await send(pipeline, client, child, modified)
    assert len(fake.bodies("/v1/order/modify")) <= 2
    assert len(fake.bodies("/v1/order/create")) == 1 and not fake.bodies("/v1/order/cancel") and len(fake.book) == 1
    assert float(fake.book[fake.only]["price"]) == 801.0


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type", ["SL", "SLM"])
async def test_a_master_pending_sl_modify_reaches_the_groww_order(db_session, mock_redis, groww_enabled, order_type):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis, order_type=order_type)
    zero = order_type == "SLM"
    await send(pipeline, client, child, evt(created, event_type="SL_PENDING_MODIFIED", trigger_price=Decimal("794"),
                                            limit_price=Decimal("0") if zero else Decimal("799"), price=Decimal("0") if zero else Decimal("799")))
    (body,) = fake.bodies("/v1/order/modify")
    assert body["order_type"] == ("SL_M" if zero else "SL") and body["trigger_price"] == 794.0
    assert ("price" in body) == (not zero)
    assert len(fake.bodies("/v1/order/create")) == 1


@pytest.mark.asyncio
async def test_a_master_limit_turned_market_and_filled_reprices_the_groww_order_to_the_fill_price(db_session, mock_redis, groww_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    await send(pipeline, client, child, evt(created, event_type="ORDER_FILLED", order_type="MARKET", status="COMPLETE", price=Decimal("802.5"),
                                            limit_price=Decimal("0"), filled_quantity=10, pending_quantity=0, fill_sequence=10))
    bodies = fake.bodies("/v1/order/modify")
    assert len(bodies) == 1 and bodies[0]["order_type"] == "LIMIT" and bodies[0]["price"] == 802.5     # never a MARKET modify
    assert len(fake.bodies("/v1/order/create")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type", ["SL", "SLM"])
async def test_a_triggered_master_sl_pushes_the_groww_sl_order_to_execute(db_session, mock_redis, groww_enabled, order_type):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis, order_type=order_type)
    await send(pipeline, client, child, evt(created, event_type="ORDER_FILLED", order_type="MARKET", status="COMPLETE", price=Decimal("796"),
                                            limit_price=Decimal("0"), trigger_price=Decimal("795"), filled_quantity=10, pending_quantity=0,
                                            fill_sequence=10))
    bodies = fake.bodies("/v1/order/modify")
    assert len(bodies) == 1 and bodies[0]["order_type"] == "LIMIT" and bodies[0]["price"] == 796.0
    assert len(fake.bodies("/v1/order/create")) == 1 and not fake.bodies("/v1/order/cancel")


# ── cancel ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("product", ["INTRADAY", "LONGTERM"])
async def test_a_master_cancel_cancels_the_groww_order_once(db_session, mock_redis, groww_enabled, product):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis, product=product)
    cancel = evt(created, event_type="ORDER_CANCELLED", status="CANCELLED")
    await send(pipeline, client, child, cancel)
    await send(pipeline, client, child, cancel)                                          # a repeated cancel event
    assert fake.bodies("/v1/order/cancel") == [{"segment": "CASH", "groww_order_id": fake.only}]
    assert fake.book[fake.only]["status"] == "CANCELLED"
    assert (await child_order(child)).status == OrderStatus.CANCELLED


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type", ["SL", "SLM"])
async def test_a_master_sl_cancel_cancels_the_groww_sl_order(db_session, mock_redis, groww_enabled, order_type):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis, order_type=order_type)
    await send(pipeline, client, child, evt(created, event_type="ORDER_CANCELLED", status="CANCELLED"))
    assert len(fake.bodies("/v1/order/cancel")) == 1 and fake.book[fake.only]["status"] == "CANCELLED"


# ── partial fills ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_part_filled_groww_order_is_not_modified_and_the_failure_is_recorded(db_session, mock_redis, groww_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    fake.fill(fake.only, 4)                                                              # 4 of 10 filled at Groww, order still working
    await send(pipeline, client, child, repriced(created, "801"))
    assert not fake.bodies("/v1/order/modify")                                           # the adapter refuses: quantity semantics undocumented
    assert float(fake.book[fake.only]["price"]) == 800.0
    assert "CHILD_MODIFICATION_FAILED" in await system_events()                          # a visible failure, never a silent skip


@pytest.mark.asyncio
async def test_a_master_cancel_after_a_part_fill_cancels_the_rest_and_keeps_the_fill(db_session, mock_redis, groww_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    fake.fill(fake.only, 4)
    await send(pipeline, client, child, evt(created, event_type="ORDER_CANCELLED", status="CANCELLED"))
    assert len(fake.bodies("/v1/order/cancel")) == 1
    order = await child_order(child)
    # Design: a cancelled order that FILLED something stays PARTIAL (broker_status CANCELLED) so the executed part is never erased.
    assert order.status == OrderStatus.PARTIAL and order.broker_status == "CANCELLED" and order.filled_quantity == 4
    assert order.pending_quantity == 0


@pytest.mark.asyncio
async def test_the_fill_syncer_records_partial_then_full_fills_from_groww(db_session, mock_redis, groww_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    syncer = FollowerFillSyncer(follower_clients_provider=lambda: {str(child.id): client}, pipeline=pipeline)
    fake.fill(fake.only, 4)
    await syncer.sync_once()
    order = await child_order(child)
    assert order.filled_quantity == 4 and order.status == OrderStatus.PARTIAL
    fake.fill(fake.only, 10)
    await syncer.sync_once()
    order = await child_order(child)
    assert order.filled_quantity == 10 and order.status == OrderStatus.COMPLETE and order.pending_quantity == 0


# ── duplicates and ambiguous sends ─────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_same_master_event_twice_creates_one_groww_order(db_session, mock_redis, groww_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    await send(pipeline, client, child, created)
    await send(new_pipeline(mock_redis), client, child, created)                         # even from a fresh process
    assert len(fake.bodies("/v1/order/create")) == 1 and len(fake.book) == 1


@pytest.mark.asyncio
async def test_an_ambiguous_send_that_groww_actually_took_is_found_by_reference_and_not_resent(db_session, mock_redis, groww_enabled):
    master, child = await groww_child(db_session)
    fake = StatefulGroww()

    async def accept_then_time_out(request):
        response = await StatefulGroww.__call__(fake, request)
        if request.url.path == "/v1/order/create":
            raise httpx.ReadTimeout("the answer never arrived")                          # Groww took it, but the answer was lost
        return response

    client = make_client(mock_redis, fake, account_id=str(child.id), static_ip=CHILD_IP)
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(accept_then_time_out))
    await client.login()
    created = evt(master_event(master, "L-AMB", side="BUY", order_type="LIMIT", product="INTRADAY", exchange="NSE"), event_type="LIMIT_CREATED")
    res = await new_pipeline(mock_redis).execute_replication(event=created, clients={str(child.id): client})
    out = res["results"][str(child.id)]
    assert out["status"] == "SUCCESS", out
    assert len(fake.bodies("/v1/order/create")) == 1 and len(fake.book) == 1


@pytest.mark.asyncio
async def test_an_ambiguous_send_groww_never_saw_is_recorded_unresolved_and_not_resent(db_session, mock_redis, groww_enabled):
    master, child = await groww_child(db_session)
    fake = StatefulGroww()
    fake.create_exception = httpx.ReadTimeout("no answer")
    created = evt(master_event(master, "L-AMB2", side="BUY", order_type="LIMIT", product="INTRADAY", exchange="NSE"), event_type="LIMIT_CREATED")
    out, client, pipeline = await copy_to_groww(mock_redis, master, child, fake, created)
    assert out["status"] in ("TIMEOUT", "UNKNOWN", "FAILED"), out
    await send(pipeline, client, child, created)                                         # the same event again
    assert len(fake.bodies("/v1/order/create")) == 1                                     # never sent twice
