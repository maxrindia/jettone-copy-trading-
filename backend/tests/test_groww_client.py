"""Groww Trading API adapter - offline only (httpx.MockTransport). Never reaches
api.groww.in, Alice Blue or Angel One. See groww_helpers.py."""
import asyncio
import hashlib
import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.core.crypto import decrypt_secret
from app.services.alice_blue.instruments import InstrumentResolutionError
from app.services.alice_blue.types import OrderRequest, OrderResultCategory, OrderSide, OrderType, ProductType
from app.services.groww import client as gc
from app.services.groww.client import (
    GrowwAuthError,
    GrowwCapabilityUnavailable,
    GrowwClient,
    GrowwRateLimited,
    normalize_status,
    order_reference_for,
)
from app.services.brokers.login_guard import LoginThrottled
from app.services.groww.contract import GROWW_CONTRACT, GrowwContract
from app.services.groww.instruments import GrowwInstrumentStore, load_instruments_if_enabled
from groww_helpers import (
    ALICE_STYLE_TOKEN, EXPECTED_TTL, FIXED_NOW, SBIN_ISIN, TOKEN_OK, Clock, FakeGroww, make_client, make_store,
    ok, order_row, position_row,
)


def req(**over):
    base = dict(symbol="SBIN", exchange="NSE", side=OrderSide.BUY, quantity=5, product_type=ProductType.MIS,
                order_type=OrderType.LIMIT, price=Decimal("800.5"), instrument_token=ALICE_STYLE_TOKEN,
                client_order_id="JT_LIM_ORDER123_acc12345")
    base.update(over)
    return OrderRequest(**base)


async def logged_in(redis, fake=None, **kw):
    fake = fake or FakeGroww()
    c = make_client(redis, fake, **kw)
    await c.login()
    return c, fake


# ── contract / defaults ─────────────────────────────────────────────────────

def test_contract_is_docs_verified_but_not_live_verified():
    assert GROWW_CONTRACT.docs_verified is True and GROWW_CONTRACT.live_verified is False


# ── authentication / session ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_login_uses_the_documented_approval_flow(mock_redis):
    c, fake = await logged_in(mock_redis)
    (r,) = fake.calls("/v1/token/api/access")
    assert r.method == "POST" and r.headers["Authorization"] == "Bearer GROWW-API-KEY"
    body = json.loads(r.content)
    assert set(body) == {"key_type", "checksum", "timestamp"} and body["key_type"] == "approval"
    assert body["timestamp"] == int(FIXED_NOW.timestamp())
    assert body["checksum"] == hashlib.sha256(("GROWW-API-SECRET" + str(body["timestamp"])).encode()).hexdigest()
    assert "GROWW-API-SECRET" not in r.content.decode()                 # the secret itself is never sent


@pytest.mark.asyncio
async def test_session_is_stored_encrypted_with_a_conservative_ttl(mock_redis):
    c, _ = await logged_in(mock_redis)
    stored = mock_redis._storage["groww:session:acc-groww"]
    assert "TOK-1" not in stored and "GROWW-API" not in stored
    assert json.loads(decrypt_secret(stored))["token"] == "TOK-1"
    assert mock_redis._expiry["groww:session:acc-groww"] == EXPECTED_TTL   # earlier of the IST / UTC readings
    assert await c.get_session() == "TOK-1"


@pytest.mark.asyncio
async def test_a_payload_wrapped_token_response_is_also_accepted(mock_redis):
    fake = FakeGroww()
    fake.token_response = (200, {"status": "SUCCESS", "payload": TOKEN_OK[1]})
    c, _ = await logged_in(mock_redis, fake)
    assert await c.get_session() == "TOK-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("response,needle", [
    ((401, {}), "approval"),
    ((403, {"status": "FAILURE"}), "approval"),
    ((400, {"status": "FAILURE"}), "HTTP 400"),
    ((200, {"expiry": "2026-10-17T06:00:00", "isActive": True}), "no token"),
    ((200, {**TOKEN_OK[1], "isActive": False}), "not active"),
    ((200, {**TOKEN_OK[1], "isActive": None}), "not active"),
    ((200, {k: v for k, v in TOKEN_OK[1].items() if k != "expiry"}), "expiry"),
    ((200, {**TOKEN_OK[1], "expiry": "tomorrow"}), "expiry"),
    ((200, {**TOKEN_OK[1], "expiry": "2026-10-15T06:00:00"}), "expired"),
], ids=["401", "403", "400", "no_token", "inactive", "isactive_missing", "no_expiry", "bad_expiry", "past_expiry"])
async def test_login_failures_fail_closed_and_store_nothing(mock_redis, response, needle):
    fake = FakeGroww()
    fake.token_response = response
    c = make_client(mock_redis, fake)
    with pytest.raises(GrowwAuthError) as ei:
        await c.login()
    assert needle in str(ei.value)
    assert "GROWW-API-SECRET" not in str(ei.value) and "GROWW-API-KEY" not in str(ei.value)
    assert "groww:session:acc-groww" not in mock_redis._storage


@pytest.mark.asyncio
async def test_network_error_during_login_is_an_auth_error(mock_redis):
    class Boom(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("down")
    c = GrowwClient("a", "l", "K", "S", mock_redis, None, http_client=httpx.AsyncClient(transport=Boom()),
                    clock=lambda: FIXED_NOW, instruments=make_store())
    with pytest.raises(GrowwAuthError, match="network error"):
        await c.login()


@pytest.mark.asyncio
async def test_expiry_in_the_past_by_either_reading_is_refused(mock_redis):
    fake = FakeGroww()
    # 2026-10-16T03:00 is 08:30 IST (still future) but already 03:00 UTC < 04:00Z now - the earlier reading must win
    fake.token_response = (200, {**TOKEN_OK[1], "expiry": "2026-10-16T03:00:00"})
    c = make_client(mock_redis, fake)
    with pytest.raises(GrowwAuthError, match="expired"):
        await c.login()


@pytest.mark.asyncio
async def test_concurrent_logins_share_one_token_request(mock_redis):
    fake = FakeGroww()
    c = make_client(mock_redis, fake)
    await asyncio.gather(*(c.get_session(force_refresh=True) for _ in range(3)))
    assert len(fake.calls("/v1/token/api/access")) == 1


@pytest.mark.asyncio
async def test_a_second_login_right_after_a_successful_one_sends_nothing(mock_redis):
    fake = FakeGroww()
    c = make_client(mock_redis, fake)
    await c.login()
    await c.login()                                          # satisfied by the session that was just established
    assert len(fake.calls("/v1/token/api/access")) == 1
    mock_redis._storage.pop("groww:session:acc-groww")       # the session is gone (e.g. rejected): re-login is throttled ...
    with pytest.raises(LoginThrottled):
        await c.login()
    assert len(fake.calls("/v1/token/api/access")) == 1
    mock_redis._advance(11)                                  # ... until the spacing window has passed
    await c.login()
    assert len(fake.calls("/v1/token/api/access")) == 2


@pytest.mark.asyncio
async def test_no_session_means_nothing_is_sent(mock_redis):
    fake = FakeGroww()
    c = make_client(mock_redis, fake)
    res = await c.place_order(req())
    assert res.category == OrderResultCategory.REJECTED and fake.requests == []
    assert await c.get_positions_or_none() is None


@pytest.mark.asyncio
async def test_a_rejected_token_clears_the_session_and_order_is_sent_once(mock_redis):
    c, fake = await logged_in(mock_redis)
    fake.session_rejected = True
    res = await c.place_order(req())
    assert res.category == OrderResultCategory.REJECTED and "rejected" in res.message
    assert len(fake.calls("/v1/order/create")) == 1
    assert "groww:session:acc-groww" not in mock_redis._storage


@pytest.mark.asyncio
async def test_logout_only_forgets_the_session(mock_redis):
    c, fake = await logged_in(mock_redis)
    n = len(fake.requests)
    await c.logout()
    assert "groww:session:acc-groww" not in mock_redis._storage and len(fake.requests) == n


@pytest.mark.asyncio
async def test_headers_on_authenticated_calls(mock_redis):
    c, fake = await logged_in(mock_redis)
    await c.get_funds()
    r = fake.calls("/v1/margins/detail/user")[0]
    assert r.headers["Authorization"] == "Bearer TOK-1" and r.headers["X-API-VERSION"] == "1.0"
    assert r.headers["Accept"] == "application/json"


def test_repr_has_no_secrets(mock_redis):
    c = make_client(mock_redis, FakeGroww())
    assert "GROWW-API" not in repr(c)


# ── instruments ─────────────────────────────────────────────────────────────

def test_instrument_resolution_rules():
    s = make_store()
    assert s.resolve("NSE", "SBIN").isin == SBIN_ISIN
    assert s.resolve("nse", "sbin-eq").trading_symbol == "SBIN"           # Alice-style suffix stripped
    assert s.resolve("BSE", "SBIN").exchange == "BSE"
    for exch, sym in [("NFO", "SBIN"), ("MCX", "GOLD"), ("NSE", "NOSUCH"), ("NSE", ""), ("NSE", "NIFTY26OCT23500CE"),
                      ("NSE", "NIFTYBEES")]:           # F&O and non-EQ instruments never resolve
        with pytest.raises(InstrumentResolutionError):
            s.resolve(exch, sym)


def test_ambiguous_or_unloaded_master_refuses():
    s = make_store()
    s.load_rows([{"exchange": "NSE", "trading_symbol": "SBIN", "instrument_type": "EQ", "segment": "CASH",
                  "isin": "X", "buy_allowed": "True", "sell_allowed": "True"}])
    with pytest.raises(InstrumentResolutionError, match="ambiguous"):
        s.resolve("NSE", "SBIN")
    with pytest.raises(InstrumentResolutionError, match="not loaded"):
        GrowwInstrumentStore().resolve("NSE", "SBIN")


@pytest.mark.asyncio
async def test_resolve_instrument_returns_groww_identity_not_the_masters(mock_redis):
    c = make_client(mock_redis, FakeGroww())
    assert await c.resolve_instrument("NSE", "SBIN-EQ") == (SBIN_ISIN, "SBIN")


@pytest.mark.asyncio
async def test_instrument_loader_is_off_unless_enabled_and_configured(monkeypatch):
    from app.core.config import settings
    target = GrowwInstrumentStore()
    monkeypatch.setattr(settings, "GROWW_ENABLED", False)
    monkeypatch.setattr(settings, "GROWW_INSTRUMENT_MASTER_URL", "https://example.invalid/i.csv")

    class NoNetwork:
        def __init__(self, *a, **k):
            raise AssertionError("the loader must not open a network client when disabled")
    monkeypatch.setattr("app.services.groww.instruments.httpx.AsyncClient", NoNetwork)
    assert await load_instruments_if_enabled(target) == 0
    monkeypatch.setattr(settings, "GROWW_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_INSTRUMENT_MASTER_URL", "")
    assert await load_instruments_if_enabled(target) == 0 and target.size == 0


@pytest.mark.asyncio
async def test_instrument_loader_failure_leaves_the_store_empty(monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "GROWW_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_INSTRUMENT_MASTER_URL", "https://example.invalid/i.csv")

    class Failing:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url): raise httpx.ConnectError("down")
    monkeypatch.setattr("app.services.groww.instruments.httpx.AsyncClient", Failing)
    target = GrowwInstrumentStore()
    assert await load_instruments_if_enabled(target) == 0 and target.size == 0


# ── order body mapping ──────────────────────────────────────────────────────

@pytest.mark.parametrize("otype,expected,has_price,has_trigger", [
    (OrderType.MARKET, "MARKET", False, False), (OrderType.LIMIT, "LIMIT", True, False),
    (OrderType.SL, "SL", True, True), (OrderType.SL_M, "SL_M", False, True),
])
def test_order_type_mapping(mock_redis, otype, expected, has_price, has_trigger):
    c = make_client(mock_redis, FakeGroww())
    body = c.build_order_body(symbol="SBIN", exchange="NSE", side=OrderSide.SELL, quantity=3, product_type=ProductType.MIS,
                              order_type=otype, price=Decimal("5.5"), trigger_price=Decimal("5.1"), reference="JTabc")
    assert body["order_type"] == expected and ("price" in body) == has_price and ("trigger_price" in body) == has_trigger


@pytest.mark.parametrize("prod,expected", [(ProductType.CNC, "CNC"), (ProductType.MIS, "MIS")])
def test_product_mapping(mock_redis, prod, expected):
    c = make_client(mock_redis, FakeGroww())
    body = c.build_order_body(symbol="SBIN", exchange="NSE", side=OrderSide.BUY, quantity=1, product_type=prod,
                              order_type=OrderType.LIMIT, price=Decimal("1"), reference="JTabc")
    assert body["product"] == expected


def test_body_fields_match_the_documentation_and_never_carry_a_foreign_token(mock_redis):
    c = make_client(mock_redis, FakeGroww())
    body = c.build_order_body(symbol="SBIN-EQ", exchange="NSE", side=OrderSide.BUY, quantity=5, product_type=ProductType.MIS,
                              order_type=OrderType.LIMIT, price=Decimal("800.5"), reference="JTref")
    assert body == {"trading_symbol": "SBIN", "quantity": 5, "validity": "DAY", "exchange": "NSE", "segment": "CASH",
                    "product": "MIS", "order_type": "LIMIT", "transaction_type": "BUY", "order_reference_id": "JTref",
                    "price": 800.5}


@pytest.mark.parametrize("over,needle", [
    ({"product_type": ProductType.NRML}, "not supported for Groww equity"),
    ({"exchange": "NFO", "symbol": "NIFTY26OCT23500CE"}, "equity only"),
    ({"symbol": "NOSUCH"}, "not found"),
    ({"symbol": "NIFTY26OCT23500CE"}, "not found"),
    ({"symbol": "NOSELL", "side": OrderSide.SELL}, "does not allow SELL"),
    ({"symbol": "UNREADABLE"}, "does not allow BUY"),
    ({"symbol": "SMALLFRZ", "quantity": 101}, "freeze quantity"),
    ({"quantity": 0}, "quantity"),
    ({"price": Decimal("0")}, "positive price"),
    ({"order_type": OrderType.SL, "trigger_price": Decimal("0")}, "trigger"),
    ({"order_type": OrderType.SL_M, "trigger_price": Decimal("0")}, "trigger"),
])
@pytest.mark.asyncio
async def test_invalid_orders_are_rejected_locally_and_never_sent(mock_redis, over, needle):
    c, fake = await logged_in(mock_redis)
    res = await c.place_order(req(**over))
    assert res.category == OrderResultCategory.REJECTED and needle in res.message
    assert fake.calls("/v1/order/create") == []


def test_order_reference_is_valid_deterministic_and_unique():
    a = order_reference_for("JT_LIM_ORDER123_acc12345")
    assert a == order_reference_for("JT_LIM_ORDER123_acc12345") and a != order_reference_for("JT_LIM_ORDER124_acc12345")
    assert 8 <= len(a) <= 20 and a.isalnum()
    assert order_reference_for(None) != order_reference_for(None)


# ── static IP ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_static_ip_blocks_create_modify_and_cancel(mock_redis):
    c, fake = await logged_in(mock_redis, static_ip=None)
    for res in (await c.place_order(req()), await c.cancel_order("GRW1"),
                await c.modify_order("GRW1", 5, Decimal("1"), symbol="SBIN")):
        assert res.category == OrderResultCategory.REJECTED and "static IP" in res.message
    assert fake.calls("/v1/order/") == []                                # no order-category request at all


@pytest.mark.asyncio
async def test_expired_static_ip_blocks_orders_but_last_valid_day_works(mock_redis):
    c, fake = await logged_in(mock_redis, valid_until=date(2026, 10, 15))     # FIXED_NOW is 2026-10-16 IST
    res = await c.place_order(req())
    assert res.category == OrderResultCategory.REJECTED and "expired" in res.message and fake.calls("/v1/order/create") == []
    ok_client, ok_fake = await logged_in(mock_redis, valid_until=date(2026, 10, 16), account_id="acc-b")
    assert (await ok_client.place_order(req())).category == OrderResultCategory.SUCCESS


# ── placement ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_place_success_ignores_the_masters_instrument_token(mock_redis):
    c, fake = await logged_in(mock_redis)
    res = await c.place_order(req())
    assert res.category == OrderResultCategory.SUCCESS and res.broker_order_id == "GRW-NEW-1"
    (body,) = fake.bodies("/v1/order/create")
    assert body["trading_symbol"] == "SBIN" and body["order_reference_id"] == order_reference_for(req().client_order_id)
    assert ALICE_STYLE_TOKEN not in json.dumps(body)
    assert fake.calls("/v1/order/create")[0].headers["Authorization"] == "Bearer TOK-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    (200, {"status": "FAILURE"}),
    (400, {"status": "FAILURE", "payload": {"remark": "Insufficient margin"}}),
    (429, {"status": "FAILURE"}),
], ids=["failure", "http_400", "http_429"])
async def test_a_failed_create_is_a_rejection_without_a_broker_order_id(mock_redis, response):
    c, fake = await logged_in(mock_redis)
    fake.create_response = response
    res = await c.place_order(req())
    assert res.category == OrderResultCategory.REJECTED and res.broker_order_id is None
    assert len(fake.calls("/v1/order/create")) == 1 and fake.calls("/v1/order/status/reference") == []


@pytest.mark.asyncio
async def test_the_remark_is_surfaced_when_present(mock_redis):
    c, fake = await logged_in(mock_redis)
    fake.create_response = (400, {"status": "FAILURE", "payload": {"remark": "Insufficient margin"}})
    assert "Insufficient margin" in (await c.place_order(req())).message


@pytest.mark.asyncio
async def test_timeout_is_looked_up_by_reference_and_never_resent(mock_redis):
    c, fake = await logged_in(mock_redis)
    fake.create_exception = httpx.ReadTimeout("t")
    res = await c.place_order(req())
    assert res.category == OrderResultCategory.TIMEOUT and res.broker_order_id is None
    assert len(fake.calls("/v1/order/create")) == 1                       # not resent
    (lookup,) = fake.calls("/v1/order/status/reference/")
    assert lookup.url.path.endswith(order_reference_for(req().client_order_id)) and lookup.url.params["segment"] == "CASH"


@pytest.mark.asyncio
async def test_timeout_that_actually_placed_is_resolved_to_success(mock_redis):
    c, fake = await logged_in(mock_redis)
    fake.create_exception = httpx.ReadTimeout("t")
    fake.reference_response = ok({"groww_order_id": "GRW-LATE", "order_status": "OPEN", "filled_quantity": 0,
                                  "remark": "", "order_reference_id": "x"})
    res = await c.place_order(req())
    assert res.category == OrderResultCategory.SUCCESS and res.broker_order_id == "GRW-LATE"
    assert len(fake.calls("/v1/order/create")) == 1


@pytest.mark.asyncio
async def test_server_error_is_an_ambiguous_timeout_and_never_resent(mock_redis):
    c, fake = await logged_in(mock_redis)
    fake.create_response = (503, {})
    res = await c.place_order(req())
    assert res.category == OrderResultCategory.TIMEOUT and len(fake.calls("/v1/order/create")) == 1


@pytest.mark.asyncio
async def test_a_connection_error_proves_nothing_was_sent_so_it_is_a_retryable_network_error(mock_redis):
    c, fake = await logged_in(mock_redis)
    fake.create_exception = httpx.ConnectError("down")
    res = await c.place_order(req())
    assert res.category == OrderResultCategory.NETWORK_ERROR and len(fake.calls("/v1/order/create")) == 1
    assert fake.calls("/v1/order/status/reference") == []                 # nothing was sent, so nothing to look up


@pytest.mark.asyncio
async def test_the_same_order_always_carries_the_same_reference(mock_redis):
    c, fake = await logged_in(mock_redis)
    await c.place_order(req())
    await c.place_order(req())
    refs = {b["order_reference_id"] for b in fake.bodies("/v1/order/create")}
    assert len(refs) == 1


# ── cancel / modify ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_body_and_outcomes(mock_redis):
    c, fake = await logged_in(mock_redis)
    res = await c.cancel_order("GRW1", symbol="SBIN", exchange="NSE")
    assert res.category == OrderResultCategory.SUCCESS
    assert fake.bodies("/v1/order/cancel") == [{"segment": "CASH", "groww_order_id": "GRW1"}]
    fake.cancel_response = (200, {"status": "FAILURE"})
    assert (await c.cancel_order("GRW1")).category == OrderResultCategory.REJECTED


@pytest.mark.asyncio
async def test_modify_reads_the_order_first_then_sends_the_documented_body(mock_redis):
    c, fake = await logged_in(mock_redis)
    res = await c.modify_order("GRW1", 7, Decimal("801.5"), order_type=OrderType.LIMIT, symbol="SBIN", exchange="NSE")
    assert res.category == OrderResultCategory.SUCCESS
    assert fake.calls("/v1/order/detail/GRW1")[0].url.params["segment"] == "CASH"
    assert fake.bodies("/v1/order/modify") == [
        {"quantity": 7, "order_type": "LIMIT", "segment": "CASH", "groww_order_id": "GRW1", "price": 801.5}]
    order = [r.url.path for r in fake.requests if "/v1/order/" in r.url.path]
    assert order.index("/v1/order/detail/GRW1") < order.index("/v1/order/modify")


@pytest.mark.asyncio
@pytest.mark.parametrize("detail,needle", [
    (ok(order_row(status="OPEN", qty=10, filled=4)), "part-filled"),
    (ok(order_row(status="EXECUTED", qty=10, filled=10)), "not modifiable"),
    (ok(order_row(status="CANCELLED", filled=0)), "not modifiable"),
    (ok({"order_status": "OPEN"}), "filled_quantity"),
    (ok({"order_status": "OPEN", "filled_quantity": 0.5}), "not a whole number"),
    ((200, {"status": "FAILURE"}), "HTTP 200"),
], ids=["part_filled", "executed", "cancelled", "no_filled_field", "fractional_fill", "detail_failure"])
async def test_modify_is_refused_unless_the_order_is_pending_and_untouched(mock_redis, detail, needle):
    c, fake = await logged_in(mock_redis)
    fake.detail_response = detail
    res = await c.modify_order("GRW1", 7, Decimal("801"), symbol="SBIN")
    assert res.category == OrderResultCategory.REJECTED and needle in res.message
    assert fake.calls("/v1/order/modify") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("otype,fields", [
    (OrderType.SL, {"price": 5.0, "trigger_price": 4.0}), (OrderType.SL_M, {"trigger_price": 4.0}),
    (OrderType.MARKET, {}),
])
async def test_modify_body_per_order_type(mock_redis, otype, fields):
    c, fake = await logged_in(mock_redis)
    await c.modify_order("GRW1", 3, Decimal("5"), trigger_price=Decimal("4"), order_type=otype)
    body = fake.bodies("/v1/order/modify")[0]
    assert {k: v for k, v in body.items() if k in ("price", "trigger_price")} == fields
    assert body["order_type"] == {"SL": "SL", "SL_M": "SL_M", "MARKET": "MARKET"}[otype.name if otype != OrderType.SL_M else "SL_M"]


@pytest.mark.asyncio
async def test_modify_local_validation_sends_nothing(mock_redis):
    c, fake = await logged_in(mock_redis)
    for kwargs in ({"quantity": 0, "price": Decimal("1")}, {"quantity": 1, "price": Decimal("0")},
                   {"quantity": 1, "price": Decimal("1"), "order_type": OrderType.SL, "trigger_price": Decimal("0")}):
        assert (await c.modify_order("GRW1", **kwargs)).category == OrderResultCategory.REJECTED
    assert fake.calls("/v1/order/") == []


# ── status normalisation ────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,qty,filled,expected", [
    ("NEW", 10, 0, "OPEN"), ("ACKED", 10, 0, "OPEN"), ("APPROVED", 10, 0, "OPEN"), ("OPEN", 10, 0, "OPEN"),
    ("MODIFICATION_REQUESTED", 10, 0, "OPEN"), ("CANCELLATION_REQUESTED", 10, 0, "OPEN"),
    ("ACKED", 10, 4, "PARTIAL"), ("OPEN", 10, 9, "PARTIAL"),
    ("TRIGGER_PENDING", 10, 0, "TRIGGER_PENDING"),
    ("REJECTED", 10, 0, "REJECTED"), ("FAILED", 10, 0, "REJECTED"),
    ("CANCELLED", 10, 0, "CANCELLED"), ("CANCELLED", 10, 4, "CANCELLED"),
    ("EXECUTED", 10, 10, "COMPLETE"), ("COMPLETED", 10, 10, "COMPLETE"), ("DELIVERY_AWAITED", 10, 10, "COMPLETE"),
    ("EXECUTED", 10, 6, "PARTIAL"),
    ("executed", 10, 10, "COMPLETE"),
])
def test_status_normalisation(raw, qty, filled, expected):
    assert normalize_status(raw, qty, filled) == expected


@pytest.mark.parametrize("raw,qty,filled", [
    ("EXECUTED", 10, 0), ("COMPLETED", 10, 0), ("DELIVERY_AWAITED", 0, 0), ("MYSTERY", 10, 0), ("", 10, 0), (None, 10, 0),
])
def test_status_that_is_unknown_or_contradicts_the_fill_is_refused(raw, qty, filled):
    with pytest.raises(GrowwCapabilityUnavailable):
        normalize_status(raw, qty, filled)


def test_every_documented_status_is_handled():
    from app.services.groww.contract import EXTRA_DOCUMENTED_STATUSES, ORDER_STATUSES
    for s in sorted(ORDER_STATUSES | EXTRA_DOCUMENTED_STATUSES):
        for filled in (0, 5, 10):
            try:
                out = normalize_status(s, 10, filled)
                assert out in {"OPEN", "PARTIAL", "TRIGGER_PENDING", "COMPLETE", "REJECTED", "CANCELLED"}
            except GrowwCapabilityUnavailable:
                assert s in {"EXECUTED", "COMPLETED", "DELIVERY_AWAITED"} and filled == 0


# ── order book ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_order_book_lifecycle(mock_redis):
    c, fake = await logged_in(mock_redis)
    fake.order_list_response = ok({"order_list": [
        order_row("1", "EXECUTED", 10, 10), order_row("2", "ACKED", 10, 3), order_row("3", "REJECTED", 10, 0),
        order_row("4", "CANCELLED", 10, 0), order_row("5", "TRIGGER_PENDING", 10, 0, otype="SL_M", side="SELL"),
    ]})
    book = await c.get_order_book()
    assert [(r.broker_order_id, r.status, r.filled_quantity) for r in book] == [
        ("1", "COMPLETE", 10), ("2", "PARTIAL", 3), ("3", "REJECTED", 0), ("4", "CANCELLED", 0), ("5", "TRIGGER_PENDING", 0)]
    assert book[4].order_type == "SLM" and book[4].side == OrderSide.SELL and book[0].average_price == Decimal("800")
    q = fake.calls("/v1/order/list")[0].url.params
    assert q["segment"] == "CASH" and q["page_size"] == "100"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    ok({"order_list": [order_row(str(i)) for i in range(100)]}),            # full page: possibly truncated
    ok({}),                                                                  # no order_list
    ok({"order_list": "nope"}),
    (200, {"status": "FAILURE"}),
    (500, {}),
    ok({"order_list": [{**order_row("1"), "order_status": "MYSTERY"}]}),
    ok({"order_list": [{k: v for k, v in order_row("1").items() if k != "quantity"}]}),
    ok({"order_list": [{k: v for k, v in order_row("1", "OPEN", 10, 0).items() if k != "filled_quantity"}]}),
    ok({"order_list": [{**order_row("1", "EXECUTED", 10, 0)}]}),            # EXECUTED with no fill
], ids=["truncated", "no_array", "not_list", "failure", "http_500", "unknown_status", "missing_qty",
     "missing_filled_qty", "contradiction"])
async def test_an_unusable_order_list_raises_instead_of_guessing(mock_redis, response):
    c, fake = await logged_in(mock_redis)
    fake.order_list_response = response
    with pytest.raises(Exception) as ei:
        await c.get_order_book()
    assert "Groww" in type(ei.value).__name__ or isinstance(ei.value, gc.GrowwError)


# ── positions ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_positions_parse_with_the_documented_fields(mock_redis):
    c, fake = await logged_in(mock_redis)
    fake.positions_response = ok({"positions": [
        position_row(qty=10), position_row("TCS", product="CNC", qty=-4, isin="INE467B01029", price=3500),
        position_row("NIFTY26OCT23500CE", segment="FNO", qty=65)]})
    pos = await c.get_positions_or_none()
    assert [(p.symbol, p.quantity, p.side, p.product_type, p.exchange, p.instrument_id) for p in pos] == [
        ("SBIN", 10, "BUY", "MIS", "NSE", SBIN_ISIN), ("TCS", -4, "SELL", "CNC", "NSE", "INE467B01029")]   # FNO row ignored
    assert pos[0].average_price == Decimal("800")
    assert fake.calls("/v1/positions/user")[0].url.params["segment"] == "CASH"


@pytest.mark.asyncio
async def test_documented_empty_list_is_flat_but_every_other_shape_is_unknown(mock_redis):
    c, fake = await logged_in(mock_redis)
    fake.positions_response = ok({"positions": []})
    assert await c.get_positions_or_none() == []                          # documented shape: genuinely flat
    for bad in (ok({}), ok({"positions": "x"}), (200, {"status": "FAILURE"}), (500, {}), (200, {}),
                ok({"positions": [{"trading_symbol": "SBIN"}]}),                        # row missing fields
                ok({"positions": [position_row(qty=1.5)]}),                              # fractional quantity
                ok({"positions": [{k: v for k, v in position_row().items() if k != "net_price"}]}),   # no price: exposure unknown
                ok({"positions": [{**position_row(), "product": ""}]})):                 # empty identifying field
        fake.positions_response = bad
        assert await c.get_positions_or_none() is None, bad


@pytest.mark.asyncio
async def test_a_rejected_session_reads_as_unknown_not_flat(mock_redis):
    c, fake = await logged_in(mock_redis)
    fake.session_rejected = True
    assert await c.get_positions_or_none() is None


# ── funds ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_funds_parse_and_failures_refuse(mock_redis):
    c, fake = await logged_in(mock_redis)
    fake.margin_response = ok({"clear_cash": 5000.5, "net_margin_used": 100, "collateral_available": 250.25,
                               "equity_margin_details": {"mis_balance_available": 99999}})
    f = await c.get_funds()
    assert (f.available_margin, f.used_margin, f.collateral) == (Decimal("5000.5"), Decimal("100"), Decimal("250.25"))
    for bad in (ok({"clear_cash": 5000}), ok({"clear_cash": "abc", "net_margin_used": 0, "collateral_available": 0}),
                (200, {"status": "FAILURE"}), (500, {})):
        fake.margin_response = bad
        with pytest.raises(Exception):
            await c.get_funds()


@pytest.mark.asyncio
async def test_an_unverified_contract_blocks_every_read(mock_redis):
    c, fake = await logged_in(mock_redis, contract=GrowwContract(docs_verified=False))
    for call in (c.get_order_book, c.get_positions, c.get_funds):
        with pytest.raises(GrowwCapabilityUnavailable):
            await call()
    assert await c.get_positions_or_none() is None
    assert len(fake.requests) == 1                                       # the login only


# ── rate limits ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_order_rate_limit_waits_then_fails_closed(mock_redis):
    clock = Clock()
    c, fake = await logged_in(mock_redis, clock=clock)
    for i in range(10):                                                   # 10 orders/second is the documented limit
        await c.place_order(req(client_order_id=f"JT_{i:04d}_ORDER"))
    assert clock.slept == []
    await c.place_order(req(client_order_id="JT_0011_ORDER"))             # the 11th waits for the next second
    assert clock.slept and max(clock.slept) <= 1.0
    assert len(fake.calls("/v1/order/create")) == 11


@pytest.mark.asyncio
async def test_a_blocked_limiter_rejects_without_sending(mock_redis):
    clock = Clock()
    c, fake = await logged_in(mock_redis, clock=clock)
    c._orders_limiter._max_wait = 0.0
    for i in range(10):
        await c.place_order(req(client_order_id=f"JT_{i:04d}_ORDER"))
    res = await c.place_order(req(client_order_id="JT_0011_ORDER"))
    assert res.category == OrderResultCategory.REJECTED and "rate limit" in res.message
    assert len(fake.calls("/v1/order/create")) == 10


def test_only_groww_hosts_are_ever_referenced():
    import inspect
    from app.services.groww import client, contract, instruments
    text = inspect.getsource(client) + inspect.getsource(contract) + inspect.getsource(instruments)
    for other in ("aliceblue", "a3.alice", "angelone", "apiconnect"):
        assert other not in text.lower().replace("angel one", "")
