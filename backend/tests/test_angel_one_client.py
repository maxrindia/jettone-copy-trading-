"""Angel One SmartAPI adapter - offline only (httpx.MockTransport). Never
reaches apiconnect.angelone.in or any Alice Blue host. See angel_helpers.py:
the position/funds/status fields used here are SYNTHETIC."""
import asyncio
import json
from datetime import date, datetime, timezone
from decimal import Decimal

import httpx
import pytest

from app.core.crypto import decrypt_secret
from app.services.alice_blue.instruments import InstrumentResolutionError
from app.services.alice_blue.types import OrderRequest, OrderResultCategory, OrderSide, OrderType, ProductType
from app.services.angel_one.client import (
    AngelAuthError,
    AngelCapabilityUnavailable,
    AngelOneClient,
    seconds_until_ist_midnight,
    totp_now,
)
from app.services.angel_one.contract import ANGEL_CONTRACT, AngelContract, contract_is_usable
from angel_helpers import (
    ALICE_STYLE_TOKEN, LOGIN_OK, SBIN_TOKEN, SYNTHETIC_CONTRACT, FakeAngel, make_client, make_store, position_row,
)


def req(**over):
    base = dict(symbol="SBIN", exchange="NSE", side=OrderSide.BUY, quantity=5, product_type=ProductType.MIS,
                order_type=OrderType.LIMIT, price=Decimal("800.5"), instrument_token=ALICE_STYLE_TOKEN,
                client_order_id="JT1")
    base.update(over)
    return OrderRequest(**base)


# ── production contract is unverified ───────────────────────────────────────

def test_production_contract_is_unverified_and_unusable():
    assert ANGEL_CONTRACT.verified is False and contract_is_usable(ANGEL_CONTRACT) is False


def test_partial_contract_is_not_verified():
    only_statuses = AngelContract(status_map=SYNTHETIC_CONTRACT.status_map)
    no_statuses = AngelContract(positions=SYNTHETIC_CONTRACT.positions, rms=SYNTHETIC_CONTRACT.rms)
    assert not only_statuses.verified and not no_statuses.verified
    assert SYNTHETIC_CONTRACT.verified and contract_is_usable(SYNTHETIC_CONTRACT)


def test_contract_with_a_non_lifecycle_state_is_unusable():
    bad = AngelContract(status_map={"x": "PENDING_MAYBE"}, positions=SYNTHETIC_CONTRACT.positions, rms=SYNTHETIC_CONTRACT.rms)
    assert bad.verified and not contract_is_usable(bad)


# ── TOTP / session ──────────────────────────────────────────────────────────

def test_totp_matches_rfc6238_reference():
    import base64
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp_now(secret, at=59, digits=8) == "94287082"
    assert totp_now(secret, at=1111111109, digits=8) == "07081804"


def test_session_expires_at_ist_midnight():
    now = datetime(2026, 10, 16, 18, 0, tzinfo=timezone.utc)   # 23:30 IST
    assert seconds_until_ist_midnight(now) == 1800


@pytest.mark.asyncio
async def test_login_sends_verified_fields_and_stores_encrypted_session(mock_redis):
    fake = FakeAngel()
    c = make_client(mock_redis, fake)
    await c.login()
    r = fake.requests[0]
    assert r.url.path == "/rest/auth/angelbroking/user/v1/loginByPassword"
    body = json.loads(r.content)
    assert set(body) == {"clientcode", "password", "totp"} and body["clientcode"] == "A123"
    assert len(body["totp"]) == 6 and body["totp"].isdigit()
    assert r.headers["X-PrivateKey"] == "KEY" and r.headers["X-UserType"] == "USER" and r.headers["X-SourceID"] == "WEB"
    stored = mock_redis._storage["angel:session:acc-angel"]
    assert "JWT1" not in stored and "1234" not in stored          # encrypted at rest
    assert json.loads(decrypt_secret(stored))["jwt"] == "JWT1"
    assert await c.get_session() == "JWT1"
    assert mock_redis.set.await_args.kwargs["ex"] == seconds_until_ist_midnight(datetime(2026, 10, 16, 4, 0, tzinfo=timezone.utc))


@pytest.mark.asyncio
async def test_login_failure_raises_and_stores_nothing(mock_redis):
    fake = FakeAngel()
    fake.routes["loginByPassword"] = (200, {"status": False, "message": "Invalid totp", "errorcode": "AB1050"})
    c = make_client(mock_redis, fake)
    with pytest.raises(AngelAuthError) as ei:
        await c.login()
    assert "Invalid totp" in str(ei.value)
    assert "1234" not in str(ei.value) and "JBSWY3DPEHPK3PXP" not in str(ei.value)
    assert "angel:session:acc-angel" not in mock_redis._storage


@pytest.mark.asyncio
async def test_concurrent_logins_share_one_broker_login(mock_redis):
    fake = FakeAngel()
    c = make_client(mock_redis, fake)
    await asyncio.gather(c.get_session(force_refresh=True), c.get_session(force_refresh=True), c.get_session(force_refresh=True))
    assert len(fake.calls("loginByPassword")) == 1


@pytest.mark.asyncio
async def test_no_session_means_no_order_is_sent(mock_redis):
    fake = FakeAngel()
    c = make_client(mock_redis, fake)
    res = await c.place_order(req())
    assert res.category == OrderResultCategory.REJECTED and fake.requests == []


@pytest.mark.asyncio
async def test_token_rejection_clears_session_and_is_not_retried(mock_redis):
    fake = FakeAngel()
    fake.place_response = (403, {"error_type": "TokenException"})
    c = make_client(mock_redis, fake)
    await c.login()
    res = await c.place_order(req())
    assert res.category == OrderResultCategory.REJECTED
    assert len(fake.calls("placeOrder")) == 1
    assert "angel:session:acc-angel" not in mock_redis._storage
    await c.get_session(force_refresh=True)                         # daily/forced re-login restores it
    assert "angel:session:acc-angel" in mock_redis._storage


@pytest.mark.asyncio
async def test_logout_calls_logout_and_clears_session(mock_redis):
    fake = FakeAngel()
    fake.routes["logout"] = (200, {"status": True})
    c = make_client(mock_redis, fake)
    await c.login()
    await c.logout()
    assert fake.requests[-1].url.path.endswith("/user/v1/logout")
    assert fake.requests[-1].headers["Authorization"] == "Bearer JWT1"
    assert "angel:session:acc-angel" not in mock_redis._storage


def test_repr_has_no_secrets(mock_redis):
    c = make_client(mock_redis, FakeAngel())
    assert "1234" not in repr(c) and "JBSWY3DPEHPK3PXP" not in repr(c) and "KEY" not in repr(c)


# ── mapping ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("prod,expected", [(ProductType.CNC, "DELIVERY"), (ProductType.MIS, "INTRADAY"), (ProductType.NRML, "CARRYFORWARD")])
def test_product_mapping(mock_redis, prod, expected):
    c = make_client(mock_redis, FakeAngel())
    body = c.build_order_body(symbol="SBIN", exchange="NSE", side=OrderSide.BUY, quantity=1, product_type=prod,
                              order_type=OrderType.LIMIT, price=Decimal("1"))
    assert body["producttype"] == expected


@pytest.mark.parametrize("ot,variety,ordertype", [
    (OrderType.MARKET, "NORMAL", "MARKET"), (OrderType.LIMIT, "NORMAL", "LIMIT"),
    (OrderType.SL, "STOPLOSS", "STOPLOSS_LIMIT"), (OrderType.SL_M, "STOPLOSS", "STOPLOSS_MARKET"),
])
def test_order_type_mapping(mock_redis, ot, variety, ordertype):
    c = make_client(mock_redis, FakeAngel())
    body = c.build_order_body(symbol="SBIN", exchange="NSE", side=OrderSide.SELL, quantity=1, product_type=ProductType.MIS,
                              order_type=ot, price=Decimal("5"), trigger_price=Decimal("4"))
    assert (body["variety"], body["ordertype"]) == (variety, ordertype)
    assert ("triggerprice" in body) == (variety == "STOPLOSS")
    if ordertype in ("MARKET", "STOPLOSS_MARKET"):
        assert body["price"] == "0"


def test_body_fields_match_sdk_and_use_the_angel_token(mock_redis):
    c = make_client(mock_redis, FakeAngel())
    body = c.build_order_body(symbol="SBIN", exchange="NSE", side=OrderSide.BUY, quantity=5, product_type=ProductType.MIS,
                              order_type=OrderType.LIMIT, price=Decimal("800.5"))
    assert set(body) == {"variety", "tradingsymbol", "symboltoken", "transactiontype", "exchange", "ordertype",
                         "producttype", "duration", "price", "squareoff", "stoploss", "quantity"}
    assert body["symboltoken"] == SBIN_TOKEN and body["tradingsymbol"] == "SBIN-EQ" and body["quantity"] == "5"


@pytest.mark.asyncio
async def test_other_brokers_instrument_token_is_never_sent(mock_redis):
    fake = FakeAngel()
    c = make_client(mock_redis, fake)
    await c.login()
    res = await c.place_order(req(instrument_token=ALICE_STYLE_TOKEN))
    assert res.category == OrderResultCategory.SUCCESS
    assert fake.order_bodies()[0]["symboltoken"] == SBIN_TOKEN
    assert ALICE_STYLE_TOKEN not in json.dumps(fake.order_bodies())


def test_instrument_resolution_rules():
    s = make_store()
    assert s.resolve("NSE", "SBIN") == (SBIN_TOKEN, "SBIN-EQ")
    assert s.resolve("nse", "sbin-eq") == (SBIN_TOKEN, "SBIN-EQ")
    assert s.resolve("BSE", "SBIN") == ("500112", "SBIN")
    for exch, sym in [("NFO", "NIFTY26OCT23500CE"), ("MCX", "GOLD"), ("NSE", "NOSUCH"), ("NSE", "")]:
        with pytest.raises(InstrumentResolutionError):
            s.resolve(exch, sym)


def test_ambiguous_or_unloaded_master_refuses():
    s = make_store()
    s.load_records([{"token": "777", "symbol": "SBIN-EQ", "exch_seg": "NSE"}])      # second token, same symbol
    with pytest.raises(InstrumentResolutionError, match="ambiguous"):
        s.resolve("NSE", "SBIN")
    from app.services.angel_one.instruments import AngelInstrumentStore
    with pytest.raises(InstrumentResolutionError, match="not loaded"):
        AngelInstrumentStore().resolve("NSE", "SBIN")


@pytest.mark.parametrize("over,needle", [
    ({"exchange": "NFO", "symbol": "NIFTY26OCT23500CE"}, "equity only"),
    ({"symbol": "NOSUCH"}, "not found"),
    ({"order_type": OrderType.SL, "trigger_price": Decimal("0")}, "trigger price"),
    ({"price": Decimal("0")}, "positive price"),
    ({"quantity": 0}, "quantity"),
])
@pytest.mark.asyncio
async def test_invalid_orders_are_rejected_locally_and_never_sent(mock_redis, over, needle):
    fake = FakeAngel()
    c = make_client(mock_redis, fake)
    await c.login()
    res = await c.place_order(req(**over))
    assert res.category == OrderResultCategory.REJECTED and needle in res.message
    assert fake.calls("placeOrder") == []


# ── static IP enforcement ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_static_ip_blocks_every_order_call(mock_redis):
    fake = FakeAngel()
    c = make_client(mock_redis, fake, static_ip=None)
    await c.login()
    for res in (await c.place_order(req()), await c.cancel_order("1"),
                await c.modify_order("1", 5, Decimal("1"), product_type=ProductType.MIS, symbol="SBIN")):
        assert res.category == OrderResultCategory.REJECTED and "static IP" in res.message
    assert [r.url.path.rsplit("/", 1)[-1] for r in fake.requests] == ["loginByPassword"]


@pytest.mark.asyncio
async def test_expired_static_ip_blocks_orders(mock_redis):
    fake = FakeAngel()
    # FIXED_NOW is 2026-10-16 (IST); the IP's last valid day was 2026-10-15.
    c = make_client(mock_redis, fake, valid_until=date(2026, 10, 15))
    await c.login()
    res = await c.place_order(req())
    assert res.category == OrderResultCategory.REJECTED and "expired" in res.message
    assert fake.calls("placeOrder") == []
    ok = make_client(mock_redis, fake, valid_until=date(2026, 10, 16), account_id="acc-angel")
    await ok.get_session()
    assert (await ok.place_order(req())).category == OrderResultCategory.SUCCESS    # last valid day still works


@pytest.mark.asyncio
async def test_order_requests_carry_the_accounts_own_ip(mock_redis):
    fake = FakeAngel()
    c = make_client(mock_redis, fake, static_ip="151.245.182.59")
    await c.login()
    await c.place_order(req())
    assert fake.calls("placeOrder")[0].headers["X-ClientPublicIP"] == "151.245.182.59"


# ── placement / rejection / cancel / modify ─────────────────────────────────

@pytest.mark.asyncio
async def test_place_success_returns_order_id(mock_redis):
    fake = FakeAngel()
    c = make_client(mock_redis, fake)
    await c.login()
    res = await c.place_order(req())
    assert res.category == OrderResultCategory.SUCCESS and res.broker_order_id == "ANG-1"
    assert fake.calls("placeOrder")[0].headers["Authorization"] == "Bearer JWT1"


@pytest.mark.asyncio
async def test_place_broker_rejection(mock_redis):
    fake = FakeAngel()
    fake.place_response = (200, {"status": False, "message": "Insufficient funds", "errorcode": "AB4036", "data": None})
    c = make_client(mock_redis, fake)
    await c.login()
    res = await c.place_order(req())
    assert res.category == OrderResultCategory.REJECTED and res.broker_order_id is None
    assert "Insufficient funds" in res.message


@pytest.mark.asyncio
async def test_place_timeout_is_ambiguous_and_sent_once(mock_redis):
    fake = FakeAngel()
    fake.routes["placeOrder"] = httpx.ReadTimeout("t")
    c = make_client(mock_redis, fake)
    await c.login()
    res = await c.place_order(req())
    assert res.category == OrderResultCategory.TIMEOUT
    assert len(fake.calls("placeOrder")) == 1


@pytest.mark.asyncio
async def test_cancel_and_modify_bodies(mock_redis):
    fake = FakeAngel()
    fake.routes["cancelOrder"] = (200, {"status": True, "data": {"orderid": "9"}})
    fake.routes["modifyOrder"] = (200, {"status": True, "data": {"orderid": "9"}})
    c = make_client(mock_redis, fake)
    await c.login()
    assert (await c.cancel_order("9")).category == OrderResultCategory.SUCCESS
    assert json.loads(fake.requests[-1].content) == {"variety": "NORMAL", "orderid": "9"}
    res = await c.modify_order("9", 5, Decimal("801"), symbol="SBIN", exchange="NSE", product_type=ProductType.MIS)
    assert res.category == OrderResultCategory.SUCCESS
    mb = json.loads(fake.requests[-1].content)
    assert mb["orderid"] == "9" and mb["price"] == "801" and mb["symboltoken"] == SBIN_TOKEN and "transactiontype" not in mb


@pytest.mark.asyncio
async def test_modify_without_product_is_refused(mock_redis):
    fake = FakeAngel()
    c = make_client(mock_redis, fake)
    await c.login()
    res = await c.modify_order("9", 5, Decimal("801"), symbol="SBIN", exchange="NSE")
    assert res.category == OrderResultCategory.REJECTED and "product" in res.message
    assert fake.calls("modifyOrder") == []


# ── contract-gated reads ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unverified_contract_blocks_every_read_without_a_request(mock_redis):
    fake = FakeAngel(positions=[position_row()])
    c = make_client(mock_redis, fake, contract=ANGEL_CONTRACT)
    await c.login()
    for call in (c.get_order_book, c.get_positions, c.get_funds):
        with pytest.raises(AngelCapabilityUnavailable):
            await call()
    assert await c.get_positions_or_none() is None
    assert len(fake.requests) == 1                                     # the login only


@pytest.mark.asyncio
async def test_positions_parse_with_a_verified_contract(mock_redis):
    fake = FakeAngel(positions=[position_row(net="10"), position_row(symbol="TCS-EQ", token="11536", net="-4", product="DELIVERY")])
    c = make_client(mock_redis, fake)
    await c.login()
    pos = await c.get_positions_or_none()
    assert [(p.symbol, p.quantity, p.side, p.product_type, p.instrument_id) for p in pos] == [
        ("SBIN-EQ", 10, "BUY", "INTRADAY", SBIN_TOKEN), ("TCS-EQ", -4, "SELL", "DELIVERY", "11536")]


@pytest.mark.asyncio
async def test_empty_position_list_is_flat_but_failures_are_none(mock_redis):
    fake = FakeAngel(positions=[])
    c = make_client(mock_redis, fake)
    await c.login()
    assert await c.get_positions_or_none() == []                       # genuinely flat
    fake.positions_response = (200, {"status": False, "message": "boom"})
    assert await c.get_positions_or_none() is None                     # unknown, NOT flat
    fake.positions_response = (500, {})
    assert await c.get_positions_or_none() is None
    fake.positions_response = (200, {"status": True, "data": [{"syn_sym": "SBIN-EQ"}]})   # row missing fields
    assert await c.get_positions_or_none() is None
    fake.positions_response = (200, {"status": True, "data": "not-a-list"})
    assert await c.get_positions_or_none() is None


@pytest.mark.asyncio
async def test_positions_without_a_session_are_none(mock_redis):
    c = make_client(mock_redis, FakeAngel())
    assert await c.get_positions_or_none() is None


@pytest.mark.asyncio
async def test_funds_parse_and_missing_fields_refuse(mock_redis):
    fake = FakeAngel(rms={"syn_avail": "5000.50", "syn_used": "100", "syn_coll": "250"})
    c = make_client(mock_redis, fake)
    await c.login()
    f = await c.get_funds()
    assert (f.available_margin, f.used_margin, f.collateral) == (Decimal("5000.50"), Decimal("100"), Decimal("250"))
    fake.rms = {"syn_avail": "5000"}
    with pytest.raises(AngelCapabilityUnavailable):
        await c.get_funds()
    fake.rms = {"syn_avail": "abc", "syn_used": "0", "syn_coll": "0"}
    with pytest.raises(AngelCapabilityUnavailable):
        await c.get_funds()


def test_order_row_parser_uses_sdk_keys_and_refuses_unknown_status():
    row = {"orderid": "9", "tradingsymbol": "SBIN-EQ", "symboltoken": "3045", "exchange": "NSE",
           "transactiontype": "SELL", "ordertype": "LIMIT", "producttype": "INTRADAY", "price": "800",
           "triggerprice": "0", "quantity": "10", "filledshares": "4", "unfilledshares": "6",
           "averageprice": "799.5", "status": "syn_part", "text": ""}
    rec = AngelOneClient.parse_order_row(row, SYNTHETIC_CONTRACT.status_map)
    assert (rec.broker_order_id, rec.side, rec.quantity, rec.filled_quantity, rec.status, rec.instrument_id) == \
        ("9", OrderSide.SELL, 10, 4, "PARTIAL", "3045")
    assert rec.average_price == Decimal("799.5")
    with pytest.raises(AngelCapabilityUnavailable):
        AngelOneClient.parse_order_row({**row, "status": "never-seen"}, SYNTHETIC_CONTRACT.status_map)


@pytest.mark.asyncio
async def test_order_book_lifecycle_with_a_verified_contract(mock_redis):
    fake = FakeAngel()
    rows = [
        {"orderid": "1", "tradingsymbol": "SBIN-EQ", "transactiontype": "BUY", "quantity": "10", "filledshares": "10", "status": "syn_done", "averageprice": "800"},
        {"orderid": "2", "tradingsymbol": "SBIN-EQ", "transactiontype": "BUY", "quantity": "10", "filledshares": "3", "status": "syn_part"},
        {"orderid": "3", "tradingsymbol": "SBIN-EQ", "transactiontype": "BUY", "quantity": "10", "filledshares": "0", "status": "syn_rej", "text": "RMS:Margin"},
        {"orderid": "4", "tradingsymbol": "SBIN-EQ", "transactiontype": "BUY", "quantity": "10", "filledshares": "0", "status": "syn_can"},
    ]
    fake.routes["getOrderBook"] = (200, {"status": True, "data": rows})
    c = make_client(mock_redis, fake)
    await c.login()
    book = await c.get_order_book()
    assert [(r.broker_order_id, r.status, r.filled_quantity) for r in book] == [
        ("1", "COMPLETE", 10), ("2", "PARTIAL", 3), ("3", "REJECTED", 0), ("4", "CANCELLED", 0)]
    assert book[2].rejection_reason == "RMS:Margin"
    fake.routes["getOrderBook"] = (200, {"status": True, "data": [{**rows[0], "status": "unknown-status"}]})
    with pytest.raises(AngelCapabilityUnavailable):                    # never guesses a lifecycle state
        await c.get_order_book()
