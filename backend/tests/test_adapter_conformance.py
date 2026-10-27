"""
Child broker adapter conformance (offline). The same contract is run against the three implemented Child clients - Alice Blue,
Groww and Angel One - each behind its own fake HTTP transport, with a guard that fails the test on any real network access.

This is a gap detector, not a support claim. A passing row says only that the client behaves as the contract requires for that
scenario against a fake; it does not enable a broker pair (the R5 allowlist is unchanged) and it is not live evidence. Behaviour
that does NOT meet the contract is recorded as a strict xfail with the reason, so the gap list is machine-checked: fixing a gap
turns its xfail into a failure until the marker is removed.

Contract (what the copy pipeline relies on)
  C1  positions: a successful empty book is [], an unreadable one is None or an exception - never []
  C2  funds: an unreadable answer is never a usable positive margin
  C3  an ambiguous send (read timeout, dropped connection) is never SUCCESS or a definitive rejection, and is transmitted ONCE
  C4  a definitive rejection is REJECTED, a success is SUCCESS, and each is one transmission
  C5  native order statuses map onto the lifecycle vocabulary; an unknown status never becomes a known one
  C6  a modify never changes the order's product
  C7  a quantity that violates lot or freeze limits is refused before anything is transmitted
  C8  one Child's failure leaves every other Child's outcome and transmissions untouched
"""
import ast
import asyncio
import json
import socket
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.types import (
    OrderRequest, OrderResultCategory, OrderSide, OrderType, ProductType,
)
from app.services.groww.instruments import GrowwInstrumentStore
from app.services.reliability.retry import RetryPolicy
from angel_helpers import FakeAngel, SYNTHETIC_CONTRACT
from angel_helpers import make_client as make_angel_client
from groww_helpers import CSV_HEADER, FakeGroww, ok as groww_ok, order_row as groww_order_row, position_row as groww_position_row
from groww_helpers import make_client as make_groww_client

APP = Path(__file__).resolve().parent.parent / "app"
KNOWN_STATES = {"OPEN", "PARTIAL", "COMPLETE", "CANCELLED", "REJECTED", "TRIGGER_PENDING"}
AMBIGUOUS = {OrderResultCategory.TIMEOUT, OrderResultCategory.UNKNOWN}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    local = {"127.0.0.1", "::1", "localhost", None, ""}
    real_connect, real_getaddrinfo = socket.socket.connect, socket.getaddrinfo

    def connect(self, address, *a, **k):
        if (address[0] if isinstance(address, tuple) else address) not in local:
            raise AssertionError("network access attempted")
        return real_connect(self, address, *a, **k)

    def getaddrinfo(host, *a, **k):
        if host not in local:
            raise AssertionError("network access attempted")
        return real_getaddrinfo(host, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def raw(status, body):
    return (status, body)


# ── harnesses: one per broker, same vocabulary ───────────────────────────────────────────────────

class AliceHarness:
    name, symbol, host = "alice", "RELIANCE", "a3.aliceblueonline.com"
    # native order-book status -> lifecycle state
    status_cases = [("COMPLETE", "COMPLETE"), ("COMPLETED", "COMPLETE"), ("OPEN", "OPEN"), ("PENDING", "OPEN"),
                    ("REJECTED", "REJECTED"), ("CANCELLED", "CANCELLED"), ("CANCELED", "CANCELLED"), ("PARTIAL", "PARTIAL"),
                    ("PARTIALLY_FILLED", "PARTIAL")]

    def __init__(self):
        self.requests = []
        self.routes = {}

    async def build(self, redis):
        await redis.set("alice:session:acc-conf", "SESSION")

        async def handler(request):
            await asyncio.sleep(0)
            self.requests.append(request)
            assert request.url.host == self.host
            for suffix, answer in self.routes.items():
                if request.url.path.endswith(suffix):
                    if isinstance(answer, Exception):
                        raise answer
                    status, body = answer
                    return httpx.Response(status, content=body if isinstance(body, bytes) else json.dumps(body).encode(),
                                          headers={"content-type": "application/json"})
            return httpx.Response(404, json={})

        self.client = AliceBlueClient(account_id="acc-conf", user_id="AB1", api_secret="x", redis=redis,
                                      http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        return self

    # scripting
    def positions_empty(self):
        self.routes["/open-api/od/v1/positions"] = (200, {"status": "Ok", "result": []})

    def positions_rows(self, qty):
        self.routes["/open-api/od/v1/positions"] = (200, {"status": "Ok", "result": [
            {"tradingSymbol": "RELIANCE-EQ", "exchange": "NSE", "product": "MIS", "netQuantity": qty, "netAveragePrice": 2500.0,
             "instrumentId": "2885"}]})

    def positions_unknown(self, kind):
        self.routes["/open-api/od/v1/positions"] = {"http500": (500, {}), "malformed": (200, b"<html>not json</html>"),
                                                    "wrong_shape": (200, {"status": "Ok", "result": {"oops": 1}})}[kind]

    def funds_unknown(self, kind):
        self.routes["/open-api/od/v1/limits"] = {"http500": (500, {}), "malformed": (200, b"<html>not json</html>"),
                                                 "wrong_shape": (200, {"status": "Ok", "result": "nope"})}[kind]

    def funds_ok(self):
        self.routes["/open-api/od/v1/limits"] = (200, {"status": "Ok", "result": [{"tradingLimit": "1000000"}]})

    def place(self, kind):
        ok_body = [{"status": "Ok", "message": "Success", "result": [{"status": "OK", "brokerOrderId": "B1"}]}]
        self.routes["/orders/placeorder"] = {
            "success": (200, ok_body),
            "rejected": (200, [{"status": "Ok", "result": [{"status": "EC941", "message": "Rejected by RMS", "brokerOrderId": ""}]}]),
            "timeout": httpx.ReadTimeout("slow"), "dropped": httpx.RemoteProtocolError("server disconnected"),
        }[kind]

    def order_book(self, native, qty=10, filled=0):
        self.routes["/open-api/od/v1/orders/book"] = (200, {"status": "Ok", "result": [{
            "brokerOrderId": "B1", "orderStatus": native, "filledQuantity": filled, "pendingQuantity": qty - filled, "quantity": qty,
            "averageTradedPrice": 100.0, "price": 100.0, "transactionType": "BUY", "tradingSymbol": "RELIANCE", "product": "INTRADAY",
            "orderType": "LIMIT", "exchange": "NSE", "orderTime": "09:31:00 22-10-2026"}]})

    def modify_ok(self):
        self.routes["/orders/modify"] = (200, {"status": "Ok", "message": "Success", "result": []})

    def order_calls(self):
        return [r for r in self.requests if r.url.path.endswith("/orders/placeorder")]

    def modify_calls(self):
        return [r for r in self.requests if r.url.path.endswith("/orders/modify")]

    def request(self, **over):
        base = dict(symbol=self.symbol, exchange="NSE", side=OrderSide.BUY, quantity=5, product_type=ProductType.MIS,
                    order_type=OrderType.LIMIT, price=Decimal("100.5"), client_order_id="JT_CONF_1")
        base.update(over)
        return OrderRequest(**base)


class GrowwHarness:
    name, symbol, host = "groww", "SBIN", "api.groww.in"
    status_cases = [("EXECUTED", "COMPLETE", 10, 10), ("OPEN", "OPEN", 10, 0), ("OPEN", "PARTIAL", 10, 4),
                    ("REJECTED", "REJECTED", 10, 0), ("CANCELLED", "CANCELLED", 10, 0)]

    async def build(self, redis, store=None):
        self.fake = FakeGroww()
        self.client = make_groww_client(redis, self.fake, store=store)
        await self.client.login()
        self.requests = self.fake.requests
        return self

    def positions_empty(self):
        self.fake.positions_response = groww_ok({"positions": []})

    def positions_rows(self, qty):
        self.fake.positions_response = groww_ok({"positions": [groww_position_row(qty=qty)]})

    def positions_unknown(self, kind):
        self.fake.positions_response = {"http500": (500, {"status": "FAILURE"}), "malformed": (200, b"<html>x</html>"),
                                        "wrong_shape": (200, {"status": "SUCCESS", "payload": {"positions": "nope"}})}[kind]

    def funds_unknown(self, kind):
        self.fake.margin_response = {"http500": (500, {"status": "FAILURE"}), "malformed": (200, b"<html>x</html>"),
                                     "wrong_shape": (200, {"status": "SUCCESS", "payload": {"clear_cash": "NaN"}})}[kind]

    def funds_ok(self):
        pass

    def place(self, kind):
        f = self.fake
        if kind == "success":
            return
        if kind == "rejected":
            f.create_response = (400, {"status": "FAILURE", "error": {"code": "GA001", "message": "rejected"}})
        if kind == "timeout":
            f.create_exception = httpx.ReadTimeout("slow")
        if kind == "dropped":
            f.create_exception = httpx.RemoteProtocolError("server disconnected")

    def order_book(self, native, qty=10, filled=0):
        self.fake.order_list_response = groww_ok({"order_list": [groww_order_row(status=native, qty=qty, filled=filled)]})

    def modify_ok(self):
        pass

    def order_calls(self):
        return self.fake.calls("/v1/order/create")

    def modify_calls(self):
        return self.fake.calls("/v1/order/modify")

    def request(self, **over):
        base = dict(symbol=self.symbol, exchange="NSE", side=OrderSide.BUY, quantity=5, product_type=ProductType.MIS,
                    order_type=OrderType.LIMIT, price=Decimal("800.5"), instrument_token="99999", client_order_id="JT_CONF_G")
        base.update(over)
        return OrderRequest(**base)


class AngelHarness:
    name, symbol, host = "angel", "SBIN", "apiconnect.angelone.in"
    status_cases = [("syn_done", "COMPLETE", 10, 10), ("syn_open", "OPEN", 10, 0), ("syn_part", "PARTIAL", 10, 4),
                    ("syn_rej", "REJECTED", 10, 0), ("syn_can", "CANCELLED", 10, 0)]

    async def build(self, redis):
        self.fake = FakeAngel()
        self.client = make_angel_client(redis, self.fake)
        await self.client.login()
        self.requests = self.fake.requests
        return self

    def positions_empty(self):
        self.fake.positions = []

    def positions_rows(self, qty):
        from angel_helpers import position_row
        self.fake.positions = [position_row(net=str(qty))]

    def positions_unknown(self, kind):
        self.fake.positions_response = {"http500": (500, {"status": False}), "malformed": (200, {"status": True, "data": "nope"}),
                                        "wrong_shape": (200, {"status": False, "message": "failed"})}[kind]

    def funds_unknown(self, kind):
        self.fake.routes["getRMS"] = {"http500": (500, {"status": False}), "malformed": (200, {"status": True, "data": "nope"}),
                                      "wrong_shape": (200, {"status": True, "data": {"syn_avail": "NaN"}})}[kind]

    def funds_ok(self):
        pass

    def place(self, kind):
        f = self.fake
        if kind == "rejected":
            f.place_response = (200, {"status": False, "message": "Insufficient funds", "errorcode": "AB1007", "data": None})
        if kind == "timeout":
            f.place_exception = httpx.ReadTimeout("slow")
        if kind == "dropped":
            f.place_exception = httpx.RemoteProtocolError("server disconnected")

    def order_book(self, native, qty=10, filled=0):
        self.fake.routes["getOrderBook"] = (200, {"status": True, "data": [{
            "orderid": "A1", "status": native, "transactiontype": "BUY", "quantity": str(qty), "filledshares": str(filled),
            "ordertype": "LIMIT", "price": "100", "averageprice": "100" if filled else "0", "tradingsymbol": "SBIN-EQ",
            "exchange": "NSE", "producttype": "INTRADAY", "symboltoken": "3045"}]})

    def modify_ok(self):
        self.fake.routes["modifyOrder"] = (200, {"status": True, "message": "SUCCESS", "data": {"orderid": "A1"}})

    def order_calls(self):
        return self.fake.calls("placeOrder")

    def modify_calls(self):
        return self.fake.calls("modifyOrder")

    def request(self, **over):
        base = dict(symbol=self.symbol, exchange="NSE", side=OrderSide.BUY, quantity=5, product_type=ProductType.MIS,
                    order_type=OrderType.LIMIT, price=Decimal("800.5"), instrument_token="99999", client_order_id="JT_CONF_A")
        base.update(over)
        return OrderRequest(**base)


HARNESSES = [AliceHarness, GrowwHarness, AngelHarness]
IDS = [h.name for h in HARNESSES]


@pytest.fixture(params=HARNESSES, ids=IDS)
def Harness(request, monkeypatch):
    if request.param is AngelHarness:
        monkeypatch.setattr("app.services.brokers.registry.ANGEL_CONTRACT", SYNTHETIC_CONTRACT)
    return request.param


async def built(Harness, redis, **kw):
    return await Harness().build(redis, **kw)


def body_of(request):
    return json.loads(request.content)


# ── C1: positions, unknown versus empty ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_c1_a_successful_empty_book_is_an_empty_list_not_unknown(Harness, mock_redis):
    h = await built(Harness, mock_redis)
    h.positions_empty()
    assert await h.client.get_positions_or_none() == []


@pytest.mark.asyncio
async def test_c1_a_successful_book_with_a_position_is_returned(Harness, mock_redis):
    h = await built(Harness, mock_redis)
    h.positions_rows(10)
    got = await h.client.get_positions_or_none()
    assert got is not None and len(got) == 1 and got[0].quantity == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["http500", "malformed", "wrong_shape"])
async def test_c1_an_unreadable_book_is_never_reported_as_empty(Harness, mock_redis, kind):
    h = await built(Harness, mock_redis)
    h.positions_unknown(kind)
    try:
        got = await h.client.get_positions_or_none()
    except Exception:
        return                                                     # raising is also fail-closed: the guard treats it as unknown
    assert got is None, f"{h.name}: {kind} was read as {got!r}"    # never [] - that would read "unreadable" as "flat"


# ── C2: funds ────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["http500", "malformed", "wrong_shape"])
async def test_c2_unknown_funds_are_never_a_usable_positive_margin(Harness, mock_redis, kind):
    h = await built(Harness, mock_redis)
    h.funds_unknown(kind)
    try:
        funds = await h.client.get_funds()
    except Exception:
        return
    assert funds.available_margin <= 0, f"{h.name}: {kind} produced available_margin={funds.available_margin}"


# ── C3 / C4: sends ───────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_c4_success_is_success_with_one_transmission(Harness, mock_redis):
    h = await built(Harness, mock_redis)
    h.funds_ok()
    h.place("success")
    res = await h.client.place_order(h.request())
    assert res.category == OrderResultCategory.SUCCESS and len(h.order_calls()) == 1


@pytest.mark.asyncio
async def test_c4_a_definitive_rejection_is_rejected_with_one_transmission(Harness, mock_redis):
    h = await built(Harness, mock_redis)
    h.place("rejected")
    res = await h.client.place_order(h.request())
    assert res.category == OrderResultCategory.REJECTED and len(h.order_calls()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["timeout", "dropped"])
async def test_c3_an_ambiguous_send_is_neither_success_nor_a_definitive_rejection_and_is_sent_once(Harness, mock_redis, fault):
    h = await built(Harness, mock_redis)
    h.place(fault)
    res = await h.client.place_order(h.request())
    assert res.category in AMBIGUOUS, f"{h.name}: {fault} -> {res.category}"
    assert len(h.order_calls()) == 1                                    # the client itself never resends


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["timeout", "dropped"])
async def test_c3_the_retry_policy_never_resends_an_ambiguous_order(Harness, mock_redis, fault):
    h = await built(Harness, mock_redis)
    h.place(fault)
    res = await RetryPolicy(max_attempts=3).execute(h.client.place_order, h.request())
    assert res.category in AMBIGUOUS and len(h.order_calls()) == 1


# ── C5: order status mapping ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_c5_native_statuses_map_onto_the_lifecycle_vocabulary(Harness, mock_redis):
    h = await built(Harness, mock_redis)
    for case in Harness.status_cases:
        native, expected = case[0], case[1]
        extra = {"qty": case[2], "filled": case[3]} if len(case) > 2 else {}
        h.order_book(native, **extra)
        book = await h.client.get_order_book()
        assert [o.status for o in book] == [expected], f"{h.name}: {native} -> {[o.status for o in book]}"


@pytest.mark.asyncio
async def test_c5_an_unknown_native_status_never_becomes_a_known_lifecycle_state(Harness, mock_redis):
    h = await built(Harness, mock_redis)
    h.order_book("TOTALLY_NEW_STATUS")
    try:
        book = await h.client.get_order_book()
    except Exception:
        return                                                          # refusing the row is fail-closed
    assert all(o.status not in KNOWN_STATES for o in book), f"{h.name}: {[o.status for o in book]}"


@pytest.mark.asyncio
async def test_c5_a_fill_status_that_contradicts_the_fill_quantity_is_not_trusted(mock_redis):
    """Groww: EXECUTED with nothing filled is refused rather than guessed (the strictest of the three)."""
    h = await GrowwHarness().build(mock_redis)
    h.order_book("EXECUTED", qty=10, filled=0)
    with pytest.raises(Exception):
        await h.client.get_order_book()


# ── C6: product preservation on modify ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_c6_a_modify_never_changes_the_product(Harness, mock_redis):
    h = await built(Harness, mock_redis)
    h.modify_ok()
    h.order_book("OPEN")
    kwargs = dict(quantity=5, price=Decimal("101"), order_type=OrderType.LIMIT, symbol=h.symbol, exchange="NSE")
    if Harness is GrowwHarness:
        h.fake.detail_response = groww_ok(groww_order_row(status="OPEN", filled=0, product="MIS"))
    try:
        await h.client.modify_order("B1" if Harness is AliceHarness else ("GRW1" if Harness is GrowwHarness else "A1"),
                                    product_type=ProductType.CNC, **kwargs)
    except Exception:
        pass                                                            # a refusal changes nothing; the check below covers a send
    for call in h.modify_calls():
        body = body_of(call)
        body = body[0] if isinstance(body, list) else body
        products = {v for k, v in body.items() if "product" in k.lower()}
        assert products <= {"DELIVERY", "CNC", "LONGTERM"}, f"{h.name}: modify sent a different product {products}"


@pytest.mark.asyncio
async def test_c6_angel_one_refuses_a_modify_without_a_product(mock_redis, monkeypatch):
    monkeypatch.setattr("app.services.brokers.registry.ANGEL_CONTRACT", SYNTHETIC_CONTRACT)
    h = await AngelHarness().build(mock_redis)
    h.modify_ok()
    res = await h.client.modify_order("A1", quantity=5, price=Decimal("101"), order_type=OrderType.LIMIT, symbol="SBIN", exchange="NSE")
    assert res.category == OrderResultCategory.REJECTED and "product" in (res.message or "").lower()
    assert h.modify_calls() == []                                       # nothing was transmitted
    sent = await h.client.modify_order("A1", quantity=5, price=Decimal("101"), order_type=OrderType.LIMIT, symbol="SBIN",
                                       exchange="NSE", product_type=ProductType.CNC)
    assert sent.category == OrderResultCategory.SUCCESS and len(h.modify_calls()) == 1
    assert body_of(h.modify_calls()[0])["producttype"] in ("DELIVERY", "CNC")        # the product the caller named, mapped


def test_c6_every_pipeline_modify_passes_the_childs_product():
    """Was a recorded gap; fixed by broker_product_for_modify (see test_modify_product_propagation.py for the behavioural tests)."""
    tree = ast.parse((APP / "services" / "copy_engine" / "pipeline.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "modify_order"]
    assert calls
    missing = [n.lineno for n in calls if "product_type" not in {k.arg for k in n.keywords}]
    assert not missing, f"modify_order calls without product_type at pipeline.py lines {missing}"


# ── C7: lot and freeze limits ────────────────────────────────────────────────────────────────────

async def refused(call):
    """True when the client refused: it raised, or returned a definitive REJECTED result (the pipeline's own contract)."""
    try:
        res = await call
    except Exception:
        return True
    return res.category == OrderResultCategory.REJECTED


def groww_store_with(row):
    s = GrowwInstrumentStore()
    s.load_csv_text("\n".join([CSV_HEADER, row]))
    return s


@pytest.mark.asyncio
async def test_c7_groww_refuses_a_quantity_that_is_not_a_lot_multiple_before_sending(mock_redis):
    store = groww_store_with("NSE,4242,LOTCO,NSE-LOTCO,Lot Co,EQ,CASH,SM,INE000X00009,,,,,5,0.05,1000,False,True,True")
    h = await GrowwHarness().build(mock_redis, store=store)
    res = await h.client.place_order(h.request(symbol="LOTCO", quantity=7))
    assert res.category == OrderResultCategory.REJECTED and "lot size" in (res.message or "")
    assert h.order_calls() == []
    h.place("success")
    assert (await h.client.place_order(h.request(symbol="LOTCO", quantity=10))).category == OrderResultCategory.SUCCESS


@pytest.mark.asyncio
async def test_c7_groww_refuses_a_quantity_over_the_freeze_limit_without_splitting(mock_redis):
    store = groww_store_with("NSE,8888,SMALLFRZ,NSE-SMALLFRZ,Small Freeze,EQ,CASH,EQ,INE000X00002,,,,,1,0.05,100,False,True,True")
    h = await GrowwHarness().build(mock_redis, store=store)
    res = await h.client.place_order(h.request(symbol="SMALLFRZ", quantity=150))
    assert res.category == OrderResultCategory.REJECTED and "freeze" in (res.message or "")
    assert h.order_calls() == []                                        # not sent, and not split into two orders


@pytest.mark.xfail(strict=True, reason="GAP: Angel One's instrument store carries no lot size or freeze quantity, so the client "
                                       "transmits an over-freeze quantity and leaves the refusal to the broker.")
@pytest.mark.asyncio
async def test_c7_angel_one_refuses_an_over_freeze_quantity_before_sending(mock_redis, monkeypatch):
    monkeypatch.setattr("app.services.brokers.registry.ANGEL_CONTRACT", SYNTHETIC_CONTRACT)
    h = await AngelHarness().build(mock_redis)
    h.place("success")
    assert await refused(h.client.place_order(h.request(quantity=5_000_000))) and h.order_calls() == []


@pytest.mark.xfail(strict=True, reason="GAP: Alice Blue has no client-side lot or freeze data (the calculator's lot_sizes defaults to 1); "
                                       "an over-freeze or non-lot quantity is transmitted and the broker rejects it (EC954).")
@pytest.mark.asyncio
async def test_c7_alice_blue_refuses_an_over_freeze_quantity_before_sending(mock_redis):
    h = await AliceHarness().build(mock_redis)
    h.place("success")
    assert await refused(h.client.place_order(h.request(quantity=5_000_000))) and h.order_calls() == []


# ── C8: independent per-Child outcomes ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_c8_one_childs_fault_never_changes_another_childs_outcome_or_transmissions(mock_redis, monkeypatch):
    monkeypatch.setattr("app.services.brokers.registry.ANGEL_CONTRACT", SYNTHETIC_CONTRACT)
    alice = await AliceHarness().build(mock_redis)
    groww = await GrowwHarness().build(mock_redis)
    angel = await AngelHarness().build(mock_redis)
    alice.place("success")
    groww.place("timeout")
    angel.place("rejected")
    results = await asyncio.gather(
        alice.client.place_order(alice.request()), groww.client.place_order(groww.request()),
        angel.client.place_order(angel.request()), return_exceptions=True,
    )
    assert [r.category for r in results] == [OrderResultCategory.SUCCESS, results[1].category, OrderResultCategory.REJECTED]
    assert results[1].category in AMBIGUOUS
    for h in (alice, groww, angel):
        assert len(h.order_calls()) == 1                                # one transmission per Child, none duplicated or shared
    assert all(r.url.host == "a3.aliceblueonline.com" for r in alice.requests)       # no cross-broker traffic
    assert all(r.url.host == "api.groww.in" for r in groww.requests)
    assert all(r.url.host == "apiconnect.angelone.in" for r in angel.requests)


def test_the_conformance_suite_enables_no_pair_and_changes_no_default():
    from app.core.config import Settings
    from app.services.brokers import pairs
    assert set(pairs.SUPPORTED_PAIRS) == {("alicebluenew", "alicebluenew")}
    s = Settings(_env_file=None)
    assert s.ENGINE_LIVE_TRADING_ENABLED is False and s.SELL_POSITION_SAFETY_BYPASS is False
