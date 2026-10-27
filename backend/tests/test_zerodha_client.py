"""Zerodha Kite Connect adapter - offline only (httpx.MockTransport). Never reaches api.kite.trade or any other broker."""
import hashlib
import json
from decimal import Decimal

import httpx
import pytest

from app.core.crypto import decrypt_secret
from app.services.alice_blue.instruments import InstrumentResolutionError
from app.services.alice_blue.types import OrderRequest, OrderResultCategory, OrderSide, OrderType, ProductType
from app.services.zerodha.client import (
    ZerodhaAuthError,
    ZerodhaCapabilityUnavailable,
    ZerodhaClient,
    decode_instruments_payload,
    normalize_status,
    order_tag_for,
)
from app.services.zerodha.contract import ZERODHA_CONTRACT, ZerodhaContract
from zerodha_helpers import ALICE_STYLE_TOKEN, FIXED_NOW, FakeKite, err, make_client, make_store, ok, order_row, position_row, seed_session


def req(**over):
    base = dict(symbol="SBIN", exchange="NSE", side=OrderSide.BUY, quantity=5, product_type=ProductType.CNC,
                order_type=OrderType.LIMIT, price=Decimal("800.5"), instrument_token=ALICE_STYLE_TOKEN,
                client_order_id="JT_LIM_ORDER123_acc12345")
    base.update(over)
    return OrderRequest(**base)


async def ready(redis, fake=None, **kw):
    fake = fake or FakeKite()
    c = make_client(redis, fake, **kw)
    await seed_session(redis)
    return c, fake


def test_contract_is_docs_verified_but_not_live_or_master_verified():
    assert ZERODHA_CONTRACT.docs_verified and not ZERODHA_CONTRACT.live_verified and not ZERODHA_CONTRACT.master_verified


def test_client_never_auto_logs_in_and_repr_hides_secrets(mock_redis):
    c = make_client(mock_redis, FakeKite())
    assert ZerodhaClient.AUTO_LOGIN_ON_STARTUP is False
    assert "KITE-KEY" not in repr(c) and "KITE-SECRET" not in repr(c)


def test_tag_is_deterministic_alphanumeric_and_20_chars():
    t = order_tag_for("JT_X")
    assert t == order_tag_for("JT_X") and t != order_tag_for("JT_Y")
    assert len(t) == 20 and t.isalnum()


# ── login ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_login_url_and_interactive_login(mock_redis):
    c = make_client(mock_redis, FakeKite())
    assert c.login_url() == "https://kite.zerodha.com/connect/login?v=3&api_key=KITE-KEY"
    with pytest.raises(ZerodhaAuthError):
        await c.login()


@pytest.mark.asyncio
async def test_complete_login_sends_documented_checksum_and_stores_encrypted_session(mock_redis):
    fake = FakeKite()
    c = make_client(mock_redis, fake)
    await c.complete_login("RTOKEN123")
    (r,) = fake.calls("/session/token", "POST")
    form = fake.form(r)
    assert set(form) == {"api_key", "request_token", "checksum"}
    assert form["checksum"] == hashlib.sha256(b"KITE-KEYRTOKEN123KITE-SECRET").hexdigest()
    assert "KITE-SECRET" not in r.content.decode()
    assert r.headers["X-Kite-Version"] == "3"
    stored = mock_redis._storage["zerodha:session:acc-zerodha"]
    assert "ACCESS-1" not in stored
    assert json.loads(decrypt_secret(stored))["token"] == "ACCESS-1"
    assert mock_redis._expiry["zerodha:session:acc-zerodha"] == 20.5 * 3600      # 09:30 IST -> 06:00 IST next day
    assert await c.get_session() == "ACCESS-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "a b", "x" * 300, "tok;en"])
async def test_complete_login_rejects_malformed_request_token(mock_redis, bad):
    fake = FakeKite()
    with pytest.raises(ZerodhaAuthError):
        await make_client(mock_redis, fake).complete_login(bad)
    assert not fake.requests


@pytest.mark.asyncio
async def test_failed_exchange_stores_no_session(mock_redis):
    fake = FakeKite()
    fake.session_response = err(403, "TokenException")
    c = make_client(mock_redis, fake)
    with pytest.raises(ZerodhaAuthError):
        await c.complete_login("RTOKEN123")
    assert "zerodha:session:acc-zerodha" not in mock_redis._storage


@pytest.mark.asyncio
async def test_no_session_means_nothing_is_sent(mock_redis):
    fake = FakeKite()
    c = make_client(mock_redis, fake)
    r = await c.place_order(req())
    assert r.category == OrderResultCategory.REJECTED and not fake.requests


@pytest.mark.asyncio
async def test_token_rejection_clears_session_and_is_not_retried(mock_redis):
    c, fake = await ready(mock_redis)
    fake.session_rejected = True
    r = await c.place_order(req())
    assert r.category == OrderResultCategory.REJECTED
    assert len(fake.calls("/orders/regular", "POST")) == 1
    assert "zerodha:session:acc-zerodha" not in mock_redis._storage


@pytest.mark.asyncio
async def test_logout_invalidates_remotely_and_forgets(mock_redis):
    c, fake = await ready(mock_redis)
    await c.logout()
    assert fake.calls("/session/token", "DELETE")
    assert "zerodha:session:acc-zerodha" not in mock_redis._storage


# ── placement ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_place_limit_order_uses_documented_form_and_ignores_master_token(mock_redis):
    c, fake = await ready(mock_redis)
    r = await c.place_order(req())
    assert r.category == OrderResultCategory.SUCCESS and r.broker_order_id == "240101000000099"
    (call,) = fake.calls("/orders/regular", "POST")
    assert call.headers["Authorization"] == "token KITE-KEY:ACCESS-1"
    body = fake.form(call)
    assert body == {"tradingsymbol": "SBIN", "exchange": "NSE", "transaction_type": "BUY", "order_type": "LIMIT", "quantity": "5",
                    "product": "CNC", "validity": "DAY", "tag": order_tag_for("JT_LIM_ORDER123_acc12345"), "price": "800.5"}
    assert ALICE_STYLE_TOKEN not in call.content.decode()


@pytest.mark.asyncio
async def test_market_order_sends_automatic_market_protection(mock_redis):
    c, fake = await ready(mock_redis)
    await c.place_order(req(order_type=OrderType.MARKET, price=Decimal("0")))
    body = fake.form(fake.calls("/orders/regular", "POST")[0])
    assert body["order_type"] == "MARKET" and body["market_protection"] == "-1" and "price" not in body


@pytest.mark.asyncio
async def test_equity_alice_suffix_is_stripped_and_unknown_symbol_is_not_sent(mock_redis):
    c, fake = await ready(mock_redis)
    assert (await c.place_order(req(symbol="SBIN-EQ"))).category == OrderResultCategory.SUCCESS
    r = await c.place_order(req(symbol="NOSUCH"))
    assert r.category == OrderResultCategory.REJECTED and r.message.startswith("Not sent")
    assert len(fake.calls("/orders/regular", "POST")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("over", [
    dict(product_type=ProductType.NRML),                                   # NRML is not an equity-cash product here
    dict(order_type=OrderType.LIMIT, price=Decimal("0")),
    dict(order_type=OrderType.SL, price=Decimal("10"), trigger_price=Decimal("0")),
    dict(quantity=0),
    dict(exchange="MCX", symbol="GOLDM"),
])
async def test_invalid_orders_are_refused_before_any_request(mock_redis, over):
    c, fake = await ready(mock_redis)
    r = await c.place_order(req(**over))
    assert r.category == OrderResultCategory.REJECTED and not fake.requests


@pytest.mark.asyncio
async def test_derivative_requires_lot_multiple_and_exact_tradingsymbol(mock_redis):
    c, fake = await ready(mock_redis)
    bad = await c.place_order(req(symbol="NIFTY26NOV23500CE", exchange="NFO", product_type=ProductType.NRML, quantity=80))
    assert bad.category == OrderResultCategory.REJECTED and "lot" in bad.message
    good = await c.place_order(req(symbol="NIFTY26NOV23500CE", exchange="NFO", product_type=ProductType.NRML, quantity=150))
    assert good.category == OrderResultCategory.SUCCESS
    # another broker's contract spelling is never translated
    foreign = await c.place_order(req(symbol="NIFTY", exchange="NFO", product_type=ProductType.NRML, quantity=75))
    assert foreign.category == OrderResultCategory.REJECTED


@pytest.mark.asyncio
async def test_tag_to_client_order_id_is_persisted_before_the_send(mock_redis):
    c, fake = await ready(mock_redis)
    seen = {}

    original = fake.__call__

    async def spy(request):
        if request.url.path == "/orders/regular":
            seen["mapped"] = await mock_redis.get(f"zerodha:tag:acc-zerodha:{order_tag_for('JT_LIM_ORDER123_acc12345')}")
        return await original(request)

    c._http = httpx.AsyncClient(transport=httpx.MockTransport(spy))
    await c.place_order(req())
    assert seen["mapped"] in ("JT_LIM_ORDER123_acc12345", b"JT_LIM_ORDER123_acc12345")


@pytest.mark.asyncio
async def test_4xx_is_a_definitive_rejection_with_documented_error_type(mock_redis):
    c, fake = await ready(mock_redis)
    fake.place_response = err(400, "MarginException", "Insufficient funds")
    r = await c.place_order(req())
    assert r.category == OrderResultCategory.REJECTED and "MarginException" in r.message


@pytest.mark.asyncio
async def test_429_is_a_rejection_not_a_timeout(mock_redis):
    c, fake = await ready(mock_redis)
    fake.place_response = err(429, "NetworkException", "Too many requests")
    assert (await c.place_order(req())).category == OrderResultCategory.REJECTED


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_5xx_is_ambiguous_timeout_and_never_resent(mock_redis, status):
    c, fake = await ready(mock_redis)
    fake.place_response = err(status, "NetworkException", "OMS down")
    r = await c.place_order(req())
    assert r.category == OrderResultCategory.TIMEOUT
    assert len(fake.calls("/orders/regular", "POST")) == 1
    assert len(fake.calls("/orders", "GET")) == 1                          # exactly one read-only tag lookup


@pytest.mark.asyncio
async def test_ambiguous_send_found_by_tag_becomes_success(mock_redis):
    c, fake = await ready(mock_redis)
    fake.place_response = err(502, "NetworkException")
    tag = order_tag_for("JT_LIM_ORDER123_acc12345")
    fake.orders_response = ok([order_row(oid="777", tag=tag)])
    r = await c.place_order(req())
    assert r.category == OrderResultCategory.SUCCESS and r.broker_order_id == "777"
    assert len(fake.calls("/orders/regular", "POST")) == 1


@pytest.mark.asyncio
async def test_read_timeout_after_send_is_ambiguous(mock_redis):
    c, fake = await ready(mock_redis)
    fake.place_exception = httpx.ReadTimeout("slow")
    r = await c.place_order(req())
    assert r.category == OrderResultCategory.TIMEOUT
    assert len(fake.calls("/orders/regular", "POST")) == 1


@pytest.mark.asyncio
async def test_connect_error_is_provably_not_sent(mock_redis):
    c, fake = await ready(mock_redis)
    fake.place_exception = httpx.ConnectError("refused")
    r = await c.place_order(req())
    assert r.category == OrderResultCategory.NETWORK_ERROR


@pytest.mark.asyncio
async def test_success_without_order_id_is_ambiguous(mock_redis):
    c, fake = await ready(mock_redis)
    fake.place_response = ok({})
    assert (await c.place_order(req())).category == OrderResultCategory.TIMEOUT


# ── modify / cancel ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_modify_untouched_open_order(mock_redis):
    c, fake = await ready(mock_redis)
    r = await c.modify_order("240101000000001", 8, Decimal("801"))
    assert r.category == OrderResultCategory.SUCCESS
    body = fake.form(fake.calls("/orders/regular/240101000000001", "PUT")[0])
    assert body == {"order_type": "LIMIT", "quantity": "8", "validity": "DAY", "price": "801"}


@pytest.mark.asyncio
async def test_modify_refuses_part_filled_and_non_open_orders(mock_redis):
    c, fake = await ready(mock_redis)
    fake.history_response = ok([order_row(status="OPEN", filled=3)])
    r = await c.modify_order("240101000000001", 8, Decimal("801"))
    assert r.category == OrderResultCategory.REJECTED and "part-filled" in r.message
    fake.history_response = ok([order_row(status="COMPLETE", filled=10)])
    assert (await c.modify_order("240101000000001", 8, Decimal("801"))).category == OrderResultCategory.REJECTED
    assert not fake.calls("/orders/regular/", "PUT")


@pytest.mark.asyncio
async def test_modify_5xx_is_ambiguous_not_a_rejection(mock_redis):
    c, fake = await ready(mock_redis)
    fake.modify_response = err(504, "NetworkException")
    assert (await c.modify_order("240101000000001", 8, Decimal("801"))).category == OrderResultCategory.TIMEOUT
    assert len(fake.calls("/orders/regular/", "PUT")) == 1


@pytest.mark.asyncio
async def test_cancel_outcomes(mock_redis):
    c, fake = await ready(mock_redis)
    assert (await c.cancel_order("240101000000001")).category == OrderResultCategory.SUCCESS
    fake.cancel_response = err(400, "OrderException", "already cancelled")
    assert (await c.cancel_order("240101000000001")).category == OrderResultCategory.REJECTED
    fake.cancel_response = err(503, "NetworkException")
    assert (await c.cancel_order("240101000000001")).category == OrderResultCategory.TIMEOUT


# ── status normalisation ────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,qty,filled,expected", [
    ("OPEN", 10, 0, "OPEN"), ("OPEN", 10, 4, "PARTIAL"), ("COMPLETE", 10, 10, "COMPLETE"), ("CANCELLED", 10, 0, "CANCELLED"),
    ("REJECTED", 10, 0, "REJECTED"), ("TRIGGER PENDING", 10, 0, "TRIGGER_PENDING"),
    ("VALIDATION PENDING", 10, 0, "VALIDATION PENDING"), ("SOMETHING NEW", 10, 0, "SOMETHING NEW"),
])
def test_normalize_status(raw, qty, filled, expected):
    assert normalize_status(raw, qty, filled) == expected


@pytest.mark.parametrize("raw,qty,filled", [("COMPLETE", 10, 4), ("OPEN", 10, 10), ("TRIGGER PENDING", 10, 2)])
def test_inconsistent_status_and_fill_is_refused(raw, qty, filled):
    with pytest.raises(ZerodhaCapabilityUnavailable):
        normalize_status(raw, qty, filled)


# ── order book / reads ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_order_book_parses_and_restores_client_order_id(mock_redis):
    c, fake = await ready(mock_redis)
    tag = order_tag_for("JT_LIM_ORDER123_acc12345")
    await mock_redis.set(f"zerodha:tag:acc-zerodha:{tag}", "JT_LIM_ORDER123_acc12345")
    fake.orders_response = ok([order_row(oid="1", tag=tag, filled=4, status="OPEN"), order_row(oid="2", status="COMPLETE", filled=10, tag="manual")])
    a, b = await c.get_order_book()
    assert a.status == "PARTIAL" and a.filled_quantity == 4 and a.client_order_id == "JT_LIM_ORDER123_acc12345"
    assert b.status == "COMPLETE" and b.average_price == Decimal("800") and b.client_order_id == "manual"
    assert a.instrument_id is None and a.order_type == "LIMIT" and a.side == OrderSide.BUY


@pytest.mark.asyncio
async def test_order_book_empty_is_an_empty_list_and_garbage_raises(mock_redis):
    c, fake = await ready(mock_redis)
    assert await c.get_order_book() == []
    fake.orders_response = ok({"unexpected": 1})
    with pytest.raises(ZerodhaCapabilityUnavailable):
        await c.get_order_book()
    fake.orders_response = (500, {"status": "error", "message": "x", "error_type": "GeneralException"})
    with pytest.raises(Exception):
        await c.get_order_book()


@pytest.mark.asyncio
@pytest.mark.parametrize("patch", [
    dict(quantity=None), dict(quantity="abc"), dict(filled_quantity=-1), dict(filled_quantity=99), dict(transaction_type="HOLD"),
    dict(order_type="WEIRD"), dict(order_id=None), dict(tradingsymbol=""), dict(price="NaN"),
])
async def test_malformed_rows_fail_the_whole_read(mock_redis, patch):
    c, fake = await ready(mock_redis)
    row = order_row()
    row.update(patch)
    fake.orders_response = ok([row])
    with pytest.raises(ZerodhaCapabilityUnavailable):
        await c.get_order_book()


@pytest.mark.asyncio
@pytest.mark.parametrize("over,expected", [
    (dict(variety="amo"), "UNSUPPORTED_VARIETY_AMO"), (dict(validity="IOC"), "UNSUPPORTED_VALIDITY_IOC"),
    (dict(exchange="MCX"), "UNSUPPORTED_EXCHANGE_MCX"),
])
async def test_unsupported_variety_validity_exchange_surface_as_unmapped_status(mock_redis, over, expected):
    c, fake = await ready(mock_redis)
    fake.orders_response = ok([order_row(**over)])
    (rec,) = await c.get_order_book()
    assert rec.status == expected


@pytest.mark.asyncio
async def test_get_order_uses_last_history_entry_and_404_is_none(mock_redis):
    c, fake = await ready(mock_redis)
    fake.history_response = ok([order_row(oid="9", status="OPEN PENDING"), order_row(oid="9", status="OPEN", filled=2)])
    rec = await c.get_order("9")
    assert rec.status == "PARTIAL" and rec.filled_quantity == 2
    fake.history_response = err(404, "OrderException")
    assert await c.get_order("9") is None
    fake.history_response = ok([order_row(oid="OTHER")])
    with pytest.raises(ZerodhaCapabilityUnavailable):
        await c.get_order("9")


@pytest.mark.asyncio
async def test_find_by_tag_none_one_and_duplicate(mock_redis):
    c, fake = await ready(mock_redis)
    assert await c.find_by_client_reference("JTnone") is None
    fake.orders_response = ok([order_row(oid="1", tag="JTx"), order_row(oid="2", tag="other")])
    assert (await c.find_by_client_reference("JTx")).broker_order_id == "1"
    fake.orders_response = ok([order_row(oid="1", tag="JTx"), order_row(oid="2", tag="JTx")])
    with pytest.raises(ZerodhaCapabilityUnavailable):
        await c.find_by_client_reference("JTx")


@pytest.mark.asyncio
async def test_unreadable_unrelated_row_does_not_hide_a_tag_match(mock_redis):
    c, fake = await ready(mock_redis)
    bad = order_row(oid="2", tag="zzz")
    bad["quantity"] = None
    fake.orders_response = ok([bad, order_row(oid="1", tag="JTx")])
    assert (await c.find_by_client_reference("JTx")).broker_order_id == "1"


@pytest.mark.asyncio
async def test_unusable_contract_fails_every_read_closed(mock_redis):
    c, _ = await ready(mock_redis, contract=ZerodhaContract(docs_verified=False))
    for call in (c.get_order_book, c.get_positions, c.get_funds):
        with pytest.raises(ZerodhaCapabilityUnavailable):
            await call()


# ── positions / funds ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_positions_use_net_and_carry_no_foreign_instrument_id(mock_redis):
    c, fake = await ready(mock_redis)
    fake.positions_response = ok({"net": [position_row(qty=10), position_row(symbol="TCS", qty=-3)], "day": []})
    long_, short = await c.get_positions()
    assert (long_.symbol, long_.quantity, long_.side, long_.product_type, long_.exchange) == ("SBIN", 10, "BUY", "CNC", "NSE")
    assert short.quantity == -3 and short.side == "SELL" and long_.instrument_id is None


@pytest.mark.asyncio
async def test_positions_unknown_is_none_never_flat(mock_redis):
    c, fake = await ready(mock_redis)
    fake.positions_response = ok({"day": []})
    assert await c.get_positions_or_none() is None
    fake.positions_response = ok({"net": [position_row(), position_row()]})        # duplicate instrument+product
    assert await c.get_positions_or_none() is None
    fake.positions_response = ok({"net": []})
    assert await c.get_positions_or_none() == []
    fake.session_rejected = True
    assert await c.get_positions_or_none() is None


@pytest.mark.asyncio
async def test_funds_mapping_and_refusal(mock_redis):
    c, fake = await ready(mock_redis)
    f = await c.get_funds()
    assert (f.available_margin, f.collateral, f.used_margin) == (Decimal("90000.5"), Decimal("5000"), Decimal("10000.0"))
    fake.margins_response = ok({"available": {"live_balance": None, "collateral": 0}, "utilised": {"debits": 0}})
    with pytest.raises(ZerodhaCapabilityUnavailable):
        await c.get_funds()


# ── instruments ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_instrument_returns_zerodha_token_and_symbol(mock_redis):
    c, _ = await ready(mock_redis)
    assert await c.resolve_instrument("NSE", "SBIN-EQ") == ("779521", "SBIN")
    with pytest.raises(InstrumentResolutionError):
        await c.resolve_instrument("NSE", "NOSUCH")
    with pytest.raises(InstrumentResolutionError):
        await c.resolve_instrument("MCX", "GOLDM")


def test_instrument_dump_decoding_handles_gzip_and_plain():
    import gzip
    text = "a,b\n1,2\n"
    assert decode_instruments_payload(gzip.compress(text.encode())) == text
    assert decode_instruments_payload(text.encode()) == text


def test_store_lookup_rules():
    s = make_store()
    assert s.resolve("BSE", "SBIN").instrument_token == "128028676"
    assert s.resolve("NFO", "NIFTY26NOVFUT").lot_size == 75
    assert s.resolve("NFO", "NIFTY26NOV23500CE").freeze_quantity == 0
    from datetime import date
    assert s.find_contract("NFO", "NIFTY", "CE", date(2026, 11, 26), Decimal("23500")).tradingsymbol == "NIFTY26NOV23500CE"
    with pytest.raises(InstrumentResolutionError):
        s.find_contract("NFO", "NIFTY", "FUT", date(2026, 11, 27))
    with pytest.raises(InstrumentResolutionError):
        s.find_contract("NFO", "NIFTY", "CE", date(2026, 11, 26))
    assert s.skipped == 1                                                   # the MCX row


# ── order updates (pure parsing) ────────────────────────────────────────────

def test_postback_checksum_is_verified_and_constant_time():
    from app.services.zerodha.updates import ZerodhaUpdateError, postback_checksum, verify_postback
    row = order_row(oid="55")
    good = dict(row, checksum=postback_checksum("55", row["order_timestamp"], "KITE-SECRET"))
    assert "checksum" not in verify_postback(good, "KITE-SECRET")
    with pytest.raises(ZerodhaUpdateError):
        verify_postback(good, "WRONG")
    with pytest.raises(ZerodhaUpdateError):
        verify_postback(dict(good, order_id="56"), "KITE-SECRET")
    for bad in (None, [], {}, dict(row)):
        with pytest.raises(ZerodhaUpdateError):
            verify_postback(bad, "KITE-SECRET")


@pytest.mark.asyncio
async def test_stream_frames_other_than_orders_are_ignored_and_orders_use_the_strict_parser(mock_redis):
    from app.services.zerodha.updates import ZerodhaUpdateError, order_from_stream_message, record_from_update
    c, _ = await ready(mock_redis)
    assert order_from_stream_message({"type": "message", "data": {}}) is None and order_from_stream_message([1]) is None
    with pytest.raises(ZerodhaUpdateError):
        order_from_stream_message({"type": "order", "data": "x"})
    rec = record_from_update(c, order_from_stream_message({"type": "order", "data": order_row(status="OPEN", filled=2)}))
    assert rec.status == "PARTIAL"
    bad = order_row()
    bad["quantity"] = None
    with pytest.raises(ZerodhaCapabilityUnavailable):
        record_from_update(c, bad)


# ── safety review additions ─────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("n,body", [
    (1, ok({"access_token": "ACCESS-1", "user_id": "ZZ9999"})), (2, ok({"access_token": "ACCESS-1"})),
    (3, ok({"access_token": "ACCESS-1", "user_id": None})),
])
async def test_login_for_a_different_kite_user_or_without_user_id_is_refused_and_stores_nothing(mock_redis, n, body):
    fake = FakeKite()
    fake.session_response = body
    c = make_client(mock_redis, fake, account_id=f"acc-{n}")
    with pytest.raises(ZerodhaAuthError):
        await c.complete_login("RTOKEN123")
    assert f"zerodha:session:acc-{n}" not in mock_redis._storage


@pytest.mark.asyncio
async def test_login_user_match_is_case_insensitive(mock_redis):
    fake = FakeKite()
    fake.session_response = ok({"access_token": "ACCESS-1", "user_id": "ab1234"})
    await make_client(mock_redis, fake).complete_login("RTOKEN123")
    assert "zerodha:session:acc-zerodha" in mock_redis._storage


@pytest.mark.asyncio
async def test_a_session_is_stored_only_under_its_own_account_key(mock_redis):
    c_a = make_client(mock_redis, FakeKite(), account_id="acc-A")
    await c_a.complete_login("RTOKEN123")
    c_b = make_client(mock_redis, FakeKite(), account_id="acc-B")
    with pytest.raises(ZerodhaAuthError):
        await c_b.get_session()


@pytest.mark.asyncio
async def test_ambiguous_send_found_rejected_or_cancelled_is_not_reported_as_success(mock_redis):
    for status in ("REJECTED", "CANCELLED"):
        c, fake = await ready(mock_redis)
        fake.place_response = err(502, "NetworkException")
        fake.orders_response = ok([order_row(oid="777", tag=order_tag_for("JT_LIM_ORDER123_acc12345"), status=status)])
        r = await c.place_order(req())
        assert r.category == OrderResultCategory.REJECTED and status in r.message
        assert len(fake.calls("/orders/regular", "POST")) == 1


@pytest.mark.asyncio
async def test_inconclusive_lookup_is_never_success(mock_redis):
    c, fake = await ready(mock_redis)
    fake.place_response = err(504, "NetworkException")
    for orders in (ok({"unexpected": 1}), ok([order_row(oid="1", tag="other")]), err(500, "GeneralException")):
        fake.orders_response = orders
        assert (await c.place_order(req())).category == OrderResultCategory.TIMEOUT


@pytest.mark.asyncio
async def test_no_secret_or_token_appears_in_results_or_logs(mock_redis, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    c, fake = await ready(mock_redis)
    fake.place_response = err(400, "InputException", "bad")
    results = [await c.place_order(req()), await c.cancel_order("1"), await c.modify_order("1", 5, Decimal("1"))]
    fake.place_response = err(502, "NetworkException")
    results.append(await c.place_order(req()))
    fake.session_rejected = True
    results.append(await c.place_order(req()))
    blob = repr(results) + caplog.text
    for secret in ("KITE-SECRET", "ACCESS-1", "RTOKEN123"):
        assert secret not in blob
    c2 = make_client(mock_redis, FakeKite())
    with caplog.at_level(logging.DEBUG):
        await c2.complete_login("RTOKEN123")
    assert "RTOKEN123" not in caplog.text and "KITE-SECRET" not in caplog.text
