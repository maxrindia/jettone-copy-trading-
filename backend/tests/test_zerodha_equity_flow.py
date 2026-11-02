"""
Zerodha Child, NSE/BSE cash equity: the order LIFECYCLE after placement - modify, cancel, stop-loss modify and trigger, partial fills, fill
sync, duplicates and ambiguous sends, plus the guards around them. OFFLINE: real pipeline + real ZerodhaClient + the in-memory database;
only Kite's HTTP is faked, and the fake is STATEFUL (an order placed, modified or cancelled through it is what the next order-book read
reports). Code-level evidence only: nothing here has been seen against a real Zerodha account.
"""
import dataclasses
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.models.audit import SystemEvent
from app.models.trades import Order, OrderStatus
from app.services.reliability.follower_fill_syncer import FollowerFillSyncer
from app.services.reliability.reconciliation import ReconciliationEngine
from conftest import TestingSessionLocal
from test_zerodha_equity_lifecycle import (EXCHANGES, copy_to_zerodha, holds, master_event, new_pipeline, zerodha_child,  # noqa: F401
                                           zerodha_enabled)
from zerodha_helpers import FakeKite, err, make_client, ok, order_row, position_row, seed_session


class StatefulKite(FakeKite):
    def __init__(self):
        super().__init__()
        self.book = {}
        self.n = 0
        self.fail_modify = False
        self.fail_cancel = False
        self.fail_place = None               # (http status, body) returned for a placement instead of accepting it
        self.list_override = None            # (http status, body) returned by GET /orders instead of the real book

    def row(self, oid):
        o = self.book[oid]
        row = order_row(oid, o["status"], o["qty"], o["filled"], side=o["side"], otype=o["otype"], price=float(o["price"]), symbol=o["symbol"],
                        product=o["product"], tag=o["tag"], exchange=o["exchange"], trigger=float(o["trigger"]),
                        average=float(o["price"] or o["trigger"] or 1) if o["filled"] else 0)
        return row

    def fill(self, oid, filled, status=None):
        o = self.book[oid]
        o["filled"] = filled
        o["status"] = status or ("COMPLETE" if filled >= o["qty"] else "OPEN")

    @property
    def only(self):
        (oid,) = self.book
        return oid

    async def __call__(self, request):
        path, method = request.url.path, request.method
        if path == "/orders/regular" and method == "POST":
            self.requests.append(request)
            if self.place_exception:
                raise self.place_exception
            if self.fail_place is not None:
                return httpx.Response(self.fail_place[0], json=self.fail_place[1])
            f = self.form(request)
            self.n += 1
            oid = f"2601010000{self.n:05d}"
            self.book[oid] = dict(status="TRIGGER PENDING" if f["order_type"] in ("SL", "SL-M") else "OPEN", qty=int(f["quantity"]), filled=0,
                                  side=f["transaction_type"], otype=f["order_type"], price=Decimal(f.get("price", "0")),
                                  trigger=Decimal(f.get("trigger_price", "0")), symbol=f["tradingsymbol"], product=f["product"], tag=f["tag"],
                                  exchange=f["exchange"])
            return httpx.Response(200, json={"status": "success", "data": {"order_id": oid}})
        if path.startswith("/orders/regular/") and method == "PUT":
            self.requests.append(request)
            oid = path.rsplit("/", 1)[1]
            if self.fail_modify:
                return httpx.Response(400, json={"status": "error", "message": "x", "error_type": "OrderException"})
            f = self.form(request)
            o = self.book[oid]
            o.update(qty=int(f["quantity"]), otype=f["order_type"], price=Decimal(f.get("price", str(o["price"]))),
                     trigger=Decimal(f.get("trigger_price", str(o["trigger"]))))
            return httpx.Response(200, json={"status": "success", "data": {"order_id": oid}})
        if path.startswith("/orders/regular/") and method == "DELETE":
            self.requests.append(request)
            oid = path.rsplit("/", 1)[1]
            if self.fail_cancel or self.book[oid]["status"] in ("COMPLETE", "CANCELLED", "REJECTED"):
                return httpx.Response(400, json={"status": "error", "message": "x", "error_type": "OrderException"})
            self.book[oid]["status"] = "CANCELLED"
            return httpx.Response(200, json={"status": "success", "data": {"order_id": oid}})
        if path == "/orders" and method == "GET":
            self.requests.append(request)
            if self.list_override is not None:
                return httpx.Response(self.list_override[0], json=self.list_override[1])
            return httpx.Response(200, json={"status": "success", "data": [self.row(o) for o in self.book]})
        if path.startswith("/orders/") and method == "GET":
            self.requests.append(request)
            oid = path.rsplit("/", 1)[1]
            if oid in self.book:
                return httpx.Response(200, json={"status": "success", "data": [self.row(oid)]})
            return httpx.Response(404, json={"status": "error", "message": "no", "error_type": "GeneralException"})
        return await super().__call__(request)

    def forms(self, method):
        return [self.form(r) for r in self.requests if r.method == method and r.url.path.startswith("/orders/regular")]


def evt(event, **changes):
    return dataclasses.replace(event, **changes)


async def place(db_session, mock_redis, *, side="BUY", product="INTRADAY", exchange="NSE", order_type="LIMIT", oid="L-1"):
    master, child = await zerodha_child(db_session)
    fake = StatefulKite()
    fake.positions_response = holds(exchange, "CNC" if product == "LONGTERM" else "MIS", qty=50)
    created = evt(master_event(master, oid, side=side, order_type=order_type, product=product, exchange=exchange),
                  event_type="SL_PENDING_CREATED" if order_type in ("SL", "SLM") else "LIMIT_CREATED")
    out, client, pipeline = await copy_to_zerodha(mock_redis, master, child, fake, created)
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
async def test_a_master_limit_price_modify_reaches_the_zerodha_order(db_session, mock_redis, zerodha_enabled, exchange, product):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis, exchange=exchange, product=product)
    await send(pipeline, client, child, repriced(created, "801"))
    assert fake.forms("PUT") == [{"order_type": "LIMIT", "quantity": "10", "validity": "DAY", "price": "801"}]
    assert fake.book[fake.only]["price"] == Decimal("801") and len(fake.forms("POST")) == 1        # modified in place, never replaced
    assert float((await child_order(child)).price) == 801.0


@pytest.mark.asyncio
async def test_a_master_quantity_modify_is_scaled_and_sent(db_session, mock_redis, zerodha_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    await send(pipeline, client, child, evt(created, event_type="ORDER_MODIFIED", quantity=20, total_quantity=20, pending_quantity=20))
    (form,) = fake.forms("PUT")
    assert form["quantity"] == "20" and form["order_type"] == "LIMIT" and form["price"] == "800"
    order = await child_order(child)
    assert order.pending_quantity == 20 and order.quantity == 10


@pytest.mark.asyncio
async def test_a_repeated_modify_event_never_creates_or_cancels_anything(db_session, mock_redis, zerodha_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    modified = repriced(created, "801")
    await send(pipeline, client, child, modified)
    await send(pipeline, client, child, modified)
    assert len(fake.forms("PUT")) <= 2 and len(fake.forms("POST")) == 1 and not fake.forms("DELETE") and len(fake.book) == 1
    assert fake.book[fake.only]["price"] == Decimal("801")


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type", ["SL", "SLM"])
async def test_a_master_pending_sl_modify_reaches_the_zerodha_order(db_session, mock_redis, zerodha_enabled, order_type):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis, order_type=order_type)
    zero = order_type == "SLM"
    await send(pipeline, client, child, evt(created, event_type="SL_PENDING_MODIFIED", trigger_price=Decimal("794"),
                                            limit_price=Decimal("0") if zero else Decimal("799"), price=Decimal("0") if zero else Decimal("799")))
    (form,) = fake.forms("PUT")
    assert form["order_type"] == ("SL-M" if zero else "SL") and form["trigger_price"] == "794" and ("price" in form) == (not zero)
    assert len(fake.forms("POST")) == 1


@pytest.mark.asyncio
async def test_a_master_limit_turned_market_and_filled_reprices_the_zerodha_order_to_the_fill_price(db_session, mock_redis, zerodha_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    await send(pipeline, client, child, evt(created, event_type="ORDER_FILLED", order_type="MARKET", status="COMPLETE", price=Decimal("802.5"),
                                            limit_price=Decimal("0"), filled_quantity=10, pending_quantity=0, fill_sequence=10))
    forms = fake.forms("PUT")
    assert len(forms) == 1 and forms[0]["order_type"] == "LIMIT" and forms[0]["price"] == "802.5"      # never a MARKET modify
    assert len(fake.forms("POST")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type", ["SL", "SLM"])
async def test_a_triggered_master_sl_pushes_the_zerodha_sl_order_to_execute(db_session, mock_redis, zerodha_enabled, order_type):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis, order_type=order_type)
    await send(pipeline, client, child, evt(created, event_type="ORDER_FILLED", order_type="MARKET", status="COMPLETE", price=Decimal("796"),
                                            limit_price=Decimal("0"), trigger_price=Decimal("795"), filled_quantity=10, pending_quantity=0,
                                            fill_sequence=10))
    forms = fake.forms("PUT")
    assert len(forms) == 1 and forms[0]["order_type"] == "LIMIT" and forms[0]["price"] == "796"
    assert len(fake.forms("POST")) == 1 and not fake.forms("DELETE")


# ── cancel ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("order_type", ["LIMIT", "SL", "SLM"])
@pytest.mark.parametrize("product", ["INTRADAY", "LONGTERM"])
async def test_a_master_cancel_cancels_the_zerodha_order_once(db_session, mock_redis, zerodha_enabled, product, order_type):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis, product=product, order_type=order_type)
    cancel = evt(created, event_type="ORDER_CANCELLED", status="CANCELLED")
    await send(pipeline, client, child, cancel)
    await send(pipeline, client, child, cancel)                                          # a repeated cancel event
    assert len(fake.forms("DELETE")) == 1 and fake.book[fake.only]["status"] == "CANCELLED"
    assert (await child_order(child)).status == OrderStatus.CANCELLED


# ── partial fills ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_part_filled_zerodha_order_is_not_modified_and_the_failure_is_recorded(db_session, mock_redis, zerodha_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    fake.fill(fake.only, 4)
    await send(pipeline, client, child, repriced(created, "801"))
    assert not fake.forms("PUT") and fake.book[fake.only]["price"] == Decimal("800")
    assert "CHILD_MODIFICATION_FAILED" in await system_events()


@pytest.mark.asyncio
async def test_a_master_cancel_after_a_part_fill_cancels_the_rest_and_keeps_the_fill(db_session, mock_redis, zerodha_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    fake.fill(fake.only, 4)
    await send(pipeline, client, child, evt(created, event_type="ORDER_CANCELLED", status="CANCELLED"))
    assert len(fake.forms("DELETE")) == 1
    order = await child_order(child)
    assert order.status == OrderStatus.PARTIAL and order.broker_status == "CANCELLED" and order.filled_quantity == 4 and order.pending_quantity == 0


@pytest.mark.asyncio
async def test_the_fill_syncer_records_partial_then_full_fills_from_zerodha(db_session, mock_redis, zerodha_enabled):
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
async def test_the_same_master_event_twice_creates_one_zerodha_order(db_session, mock_redis, zerodha_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    await send(pipeline, client, child, created)
    await send(new_pipeline(mock_redis), client, child, created)                         # even from a fresh process
    assert len(fake.forms("POST")) == 1 and len(fake.book) == 1


@pytest.mark.asyncio
async def test_an_ambiguous_send_that_kite_actually_took_is_found_by_tag_and_not_resent(db_session, mock_redis, zerodha_enabled):
    master, child = await zerodha_child(db_session)
    fake = StatefulKite()

    async def accept_then_time_out(request):
        response = await StatefulKite.__call__(fake, request)
        if request.method == "POST" and request.url.path == "/orders/regular":
            raise httpx.ReadTimeout("the answer never arrived")                          # Kite took it, but the answer was lost
        return response

    client = make_client(mock_redis, fake, account_id=str(child.id))
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(accept_then_time_out))
    await seed_session(mock_redis, account_id=str(child.id))
    created = evt(master_event(master, "L-AMB", side="BUY", order_type="LIMIT", product="INTRADAY", exchange="NSE"), event_type="LIMIT_CREATED")
    res = await new_pipeline(mock_redis).execute_replication(event=created, clients={str(child.id): client})
    assert res["results"][str(child.id)]["status"] == "SUCCESS"
    assert len(fake.forms("POST")) == 1 and len(fake.book) == 1


@pytest.mark.asyncio
async def test_an_ambiguous_send_kite_never_saw_is_recorded_unresolved_and_not_resent(db_session, mock_redis, zerodha_enabled):
    master, child = await zerodha_child(db_session)
    fake = StatefulKite()
    fake.place_exception = httpx.ReadTimeout("no answer")
    created = evt(master_event(master, "L-AMB2", side="BUY", order_type="LIMIT", product="INTRADAY", exchange="NSE"), event_type="LIMIT_CREATED")
    out, client, pipeline = await copy_to_zerodha(mock_redis, master, child, fake, created)
    assert out["status"] in ("TIMEOUT", "UNKNOWN", "FAILED"), out
    await send(pipeline, client, child, created)
    assert len(fake.forms("POST")) == 1                                                  # never sent twice


@pytest.mark.asyncio
async def test_a_five_hundred_after_the_send_is_ambiguous_and_never_repeated(db_session, mock_redis, zerodha_enabled):
    master, child = await zerodha_child(db_session)
    fake = StatefulKite()
    fake.fail_place = err(502, "NetworkException", "gateway")
    created = evt(master_event(master, "L-502", side="BUY", order_type="LIMIT", product="INTRADAY", exchange="NSE"), event_type="LIMIT_CREATED")
    out, client, pipeline = await copy_to_zerodha(mock_redis, master, child, fake, created)
    assert out["status"] != "SUCCESS"
    assert len([r for r in fake.requests if r.method == "POST" and r.url.path == "/orders/regular"]) == 1


# ── Kite refusals, races and interim states ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_kite_modify_refusal_leaves_the_order_unchanged_and_is_alerted(db_session, mock_redis, zerodha_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    fake.fail_modify = True
    await send(pipeline, client, child, repriced(created, "801"))
    assert len(fake.forms("PUT")) == 1 and fake.book[fake.only]["price"] == Decimal("800")
    assert float((await child_order(child)).price) == 800.0 and "CHILD_MODIFICATION_FAILED" in await system_events()


@pytest.mark.asyncio
@pytest.mark.parametrize("interim", ["MODIFY PENDING", "MODIFY VALIDATION PENDING", "OPEN PENDING", "VALIDATION PENDING", "CANCEL PENDING"])
async def test_a_modify_while_kite_still_shows_an_interim_state_is_refused_and_alerted(db_session, mock_redis, zerodha_enabled, interim):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    fake.book[fake.only]["status"] = interim
    await send(pipeline, client, child, repriced(created, "802"))
    assert not fake.forms("PUT") and "CHILD_MODIFICATION_FAILED" in await system_events()


@pytest.mark.asyncio
async def test_a_kite_cancel_refusal_never_marks_the_order_cancelled(db_session, mock_redis, zerodha_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    fake.fail_cancel = True
    await send(pipeline, client, child, evt(created, event_type="ORDER_CANCELLED", status="CANCELLED"))
    assert len(fake.forms("DELETE")) == 1 and (await child_order(child)).status == OrderStatus.OPEN
    assert any("CANCEL" in kind for kind in await system_events())


@pytest.mark.asyncio
async def test_a_cancel_that_races_a_full_fill_does_not_record_a_cancellation(db_session, mock_redis, zerodha_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    fake.fill(fake.only, 10)
    await send(pipeline, client, child, evt(created, event_type="ORDER_CANCELLED", status="CANCELLED"))
    assert (await child_order(child)).status != OrderStatus.CANCELLED and fake.book[fake.only]["status"] == "COMPLETE"


@pytest.mark.asyncio
async def test_a_modify_that_races_a_full_fill_is_refused(db_session, mock_redis, zerodha_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    fake.fill(fake.only, 10)
    await send(pipeline, client, child, repriced(created, "801"))
    assert not fake.forms("PUT") and not fake.forms("DELETE") and "CHILD_MODIFICATION_FAILED" in await system_events()


@pytest.mark.asyncio
async def test_an_order_in_another_variety_is_never_copied_modified_or_cancelled_by_jettone(db_session, mock_redis, zerodha_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    row = fake.row(fake.only)
    book = [dict(row, variety="amo"), dict(row, order_id="240101000000777", variety="co")]
    records = [client.parse_order_row(r) for r in book]
    assert all(str(r.status).startswith("UNSUPPORTED_VARIETY_") for r in records)         # surfaced as unsupported, which the Master poller never dispatches


# ── SELL guard and reconciliation ──────────────────────────────────────────────────────────────────────────────────────────────

async def attempt_exit(db_session, mock_redis, *, product, net, oid, exchange="NSE"):
    master, child = await zerodha_child(db_session)
    fake = StatefulKite()
    fake.positions_response = ok({"net": net, "day": []})
    event = master_event(master, oid, side="SELL", order_type="LIMIT", product=product, exchange=exchange)
    out, client, pipeline = await copy_to_zerodha(mock_redis, master, child, fake, event)
    return fake, out


@pytest.mark.asyncio
@pytest.mark.parametrize("product", ["INTRADAY", "LONGTERM"])
async def test_a_sell_with_no_position_is_blocked_so_a_short_sale_is_never_opened(db_session, mock_redis, zerodha_enabled, product):
    fake, out = await attempt_exit(db_session, mock_redis, product=product, net=[], oid=f"NOPOS-{product}")
    assert out["status"] != "SUCCESS" and not fake.forms("POST")


@pytest.mark.asyncio
async def test_a_sell_larger_than_the_confirmed_long_is_refused_whole(db_session, mock_redis, zerodha_enabled):
    fake, out = await attempt_exit(db_session, mock_redis, product="INTRADAY", oid="BIG", net=[position_row(product="MIS", qty=4)])
    assert out["status"] != "SUCCESS" and not fake.forms("POST")


@pytest.mark.asyncio
async def test_a_mis_sell_is_not_covered_by_a_cnc_position_and_the_reverse(db_session, mock_redis, zerodha_enabled):
    fake, out = await attempt_exit(db_session, mock_redis, product="INTRADAY", oid="MIS-VS-CNC", net=[position_row(product="CNC", qty=50)])
    assert out["status"] != "SUCCESS" and not fake.forms("POST")
    fake, out = await attempt_exit(db_session, mock_redis, product="LONGTERM", oid="CNC-VS-MIS", net=[position_row(product="MIS", qty=50)])
    assert out["status"] != "SUCCESS" and not fake.forms("POST")


@pytest.mark.asyncio
async def test_a_bse_sell_is_not_covered_by_an_nse_position(db_session, mock_redis, zerodha_enabled):
    fake, out = await attempt_exit(db_session, mock_redis, product="INTRADAY", oid="BSE-VS-NSE", exchange="BSE",
                                   net=[position_row(exchange="NSE", product="MIS", qty=50)])
    assert out["status"] != "SUCCESS" and not fake.forms("POST")


@pytest.mark.asyncio
async def test_reconciliation_matches_a_filled_zerodha_order_against_kites_net_positions(db_session, mock_redis, zerodha_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    fake.fill(fake.only, 10)
    await FollowerFillSyncer(follower_clients_provider=lambda: {str(child.id): client}, pipeline=pipeline).sync_once()
    fake.positions_response = ok({"net": [position_row(symbol="SBIN", exchange="NSE", product="MIS", qty=10)], "day": []})
    async with TestingSessionLocal() as db:
        assert await ReconciliationEngine.run_for_account(child.id, db, broker_client=client) == []
    fake.positions_response = ok({"net": [position_row(symbol="SBIN", exchange="NSE", product="MIS", qty=7)], "day": []})
    async with TestingSessionLocal() as db:
        records = await ReconciliationEngine.run_for_account(child.id, db, broker_client=client)
    assert [(r.symbol, r.expected_qty, r.actual_qty) for r in records] == [("SBIN", 10, 7)]


# ── fill tracking of statuses Kite reports after acceptance ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("order_type", ["SL", "SLM"])
async def test_a_stop_loss_waiting_for_its_trigger_stays_pending_then_completes(db_session, mock_redis, zerodha_enabled, order_type):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis, order_type=order_type)
    sync = FollowerFillSyncer(follower_clients_provider=lambda: {str(child.id): client}, pipeline=pipeline)
    assert fake.book[fake.only]["status"] == "TRIGGER PENDING"
    await sync.sync_once()
    order = await child_order(child)
    assert order.status == OrderStatus.PENDING and order.filled_quantity == 0
    fake.book[fake.only]["status"] = "OPEN"                                              # the trigger fired; now working
    await sync.sync_once()
    fake.fill(fake.only, 10)
    await sync.sync_once()
    order = await child_order(child)
    assert order.status == OrderStatus.COMPLETE and order.filled_quantity == 10


@pytest.mark.asyncio
async def test_an_order_kite_rejects_after_accepting_or_cancelled_outside_jettone_is_recorded(db_session, mock_redis, zerodha_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    fake.book[fake.only]["status"] = "REJECTED"
    await FollowerFillSyncer(follower_clients_provider=lambda: {str(child.id): client}, pipeline=pipeline).sync_once()
    assert (await child_order(child)).status == OrderStatus.REJECTED


@pytest.mark.asyncio
async def test_an_order_cancelled_in_the_kite_app_is_recorded_cancelled(db_session, mock_redis, zerodha_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    fake.book[fake.only]["status"] = "CANCELLED"
    await FollowerFillSyncer(follower_clients_provider=lambda: {str(child.id): client}, pipeline=pipeline).sync_once()
    order = await child_order(child)
    assert order.status == OrderStatus.CANCELLED and order.filled_quantity == 0


@pytest.mark.asyncio
async def test_an_unreadable_order_book_changes_nothing(db_session, mock_redis, zerodha_enabled):
    master, child, fake, client, pipeline, created = await place(db_session, mock_redis)
    before = await child_order(child)
    fake.list_override = (200, {"status": "success", "data": {"not": "a list"}})
    await FollowerFillSyncer(follower_clients_provider=lambda: {str(child.id): client}, pipeline=pipeline).sync_once()
    after = await child_order(child)
    assert (after.status, after.filled_quantity) == (before.status, before.filled_quantity)
