"""
Copy-pipeline behaviour for a Groww Child with SELL_POSITION_SAFETY_BYPASS=false.

Real CopyEnginePipeline + real GrowwClient + the in-memory DB; only the broker
HTTP is faked (groww_helpers.FakeGroww, shaped from Groww's documentation).
  - a SELL that reduces a broker-confirmed long is copied
  - a SELL that would open or deepen a short is rejected whole
  - missing / unverified / malformed position data fails closed
  - the Master's (Alice Blue) instrument id never reaches Groww
  - margin, emergency stop, idempotency, strategy isolation, static IP and the
    feature flag all still apply
"""
import json

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.accounts import Account
from app.models.audit import AuditLog
from app.models.trades import Order
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.risk_inputs import PositionFetchError, get_available_long
from app.services.copy_engine.validator import EventValidator
from app.services.groww.contract import GrowwContract
from conftest import TestingSessionLocal
from test_order_handling_matrix import _event, _setup
from groww_helpers import ALICE_STYLE_TOKEN, FakeGroww, make_client, ok, position_row

CHILD_IP = "151.245.182.60"


@pytest.fixture
def groww_enabled(monkeypatch):
    monkeypatch.setattr(settings, "GROWW_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_CHILD_TRADING_ENABLED", True)
    monkeypatch.setattr(settings, "SELL_POSITION_SAFETY_BYPASS", False)


async def _groww_child(db_session):
    master, child = await _setup(db_session)           # Alice Master -> Child (broker set to Groww below)
    async with TestingSessionLocal() as s:
        row = await s.get(Account, child.id)
        row.broker = "groww"
        row.static_ip_address = CHILD_IP
        await s.commit()
    return master, child


def _sell(master, oid, *, qty=10, product="INTRADAY", symbol="SBIN", exchange="NSE", order_type="LIMIT", price="800"):
    return _event(master, oid, side="SELL", order_type=order_type, product=product, exchange=exchange,
                  symbol=symbol, qty=qty, price=price, token=ALICE_STYLE_TOKEN)


def _buy(master, oid, *, qty=10):
    return _event(master, oid, side="BUY", order_type="LIMIT", product="INTRADAY", exchange="NSE",
                  symbol="SBIN", qty=qty, price="800", token=ALICE_STYLE_TOKEN)


async def _run(mock_redis, master, child, fake, event, *, client=None):
    client = client or make_client(mock_redis, fake, account_id=str(child.id), static_ip=CHILD_IP)
    await client.login()
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=event, clients={str(child.id): client})
    return (res.get("results") or {}).get(str(child.id)), client


async def _reasons(child=None):
    async with TestingSessionLocal() as s:
        q = select(AuditLog) if child is None else select(AuditLog).where(AuditLog.account_id == child.id)
        return [r.reason_code for r in (await s.execute(q)).scalars().all() if r.reason_code]


def _positions(*rows):
    return ok({"positions": list(rows)})


# ─── a valid position-reducing exit is copied ───────────────────────────────

@pytest.mark.asyncio
async def test_exit_that_reduces_a_confirmed_long_is_placed(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    fake.positions_response = _positions(position_row(qty=10, product="MIS"))
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "EXIT-1", qty=10))
    assert out["status"] == "SUCCESS", out
    (body,) = fake.bodies("/v1/order/create")
    assert (body["transaction_type"], body["quantity"], body["product"], body["order_type"], body["price"]) == \
        ("SELL", 10, "MIS", "LIMIT", 800.0)
    assert body["trading_symbol"] == "SBIN" and body["exchange"] == "NSE" and body["segment"] == "CASH"
    assert ALICE_STYLE_TOKEN not in json.dumps(body)                      # the Master's broker id never reaches Groww
    assert fake.calls("/v1/positions/user")                                # the position WAS checked
    async with TestingSessionLocal() as s:
        orders = (await s.execute(select(Order).where(Order.account_id == child.id))).scalars().all()
    assert orders and {o.broker for o in orders} == {"GROWW"}              # persisted under Groww, not ALICE_BLUE


@pytest.mark.asyncio
async def test_partial_exit_of_a_larger_long_is_placed(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    fake.positions_response = _positions(position_row(qty=25))
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "EXIT-2", qty=10))
    assert out["status"] == "SUCCESS", out
    assert fake.bodies("/v1/order/create")[0]["quantity"] == 10


@pytest.mark.asyncio
async def test_a_delivery_exit_against_a_cnc_position(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    fake.positions_response = _positions(position_row(qty=10, product="CNC"))
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "CNC-1", product="LONGTERM"))
    assert out["status"] == "SUCCESS", out
    assert fake.bodies("/v1/order/create")[0]["product"] == "CNC"


# ─── a SELL that would open unintended short exposure is rejected ───────────

@pytest.mark.asyncio
async def test_sell_when_flat_is_rejected(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "SHORT-1"))
    assert out["status"] == "REJECTED" and fake.bodies("/v1/order/create") == []
    assert "INSUFFICIENT_LONG_POSITION" in await _reasons(child)


@pytest.mark.asyncio
async def test_sell_larger_than_the_long_is_rejected_whole(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    fake.positions_response = _positions(position_row(qty=5))
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "SHORT-2", qty=10))
    assert out["status"] == "REJECTED" and fake.bodies("/v1/order/create") == []     # never a partial sell
    assert "INSUFFICIENT_LONG_POSITION" in await _reasons(child)


@pytest.mark.asyncio
async def test_sell_when_already_short_is_rejected(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    fake.positions_response = _positions(position_row(qty=-3))
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "SHORT-3", qty=1))
    assert out["status"] == "REJECTED" and fake.bodies("/v1/order/create") == []


@pytest.mark.asyncio
async def test_a_long_in_another_product_does_not_cover_the_sell(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    fake.positions_response = _positions(position_row(qty=10, product="CNC"))
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "PROD-1", product="INTRADAY"))
    assert out["status"] == "REJECTED" and fake.bodies("/v1/order/create") == []


@pytest.mark.asyncio
async def test_a_long_in_another_instrument_does_not_cover_the_sell(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    fake.positions_response = _positions(position_row("TCS", isin="INE467B01029", qty=10))
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "INSTR-1"))
    assert out["status"] == "REJECTED" and fake.bodies("/v1/order/create") == []


@pytest.mark.asyncio
async def test_a_long_on_another_exchange_does_not_cover_the_sell(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    fake.positions_response = _positions(position_row(exchange="BSE", qty=10))         # same ISIN, other exchange
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "EXCH-1"))
    assert out["status"] == "REJECTED" and fake.bodies("/v1/order/create") == []


# ─── missing / unverified / malformed position data fails safely ────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    (200, {"status": "FAILURE"}),
    (500, {}),
    (200, {"status": "SUCCESS", "payload": {}}),                              # undocumented empty shape: unknown, not flat
    (200, {"status": "SUCCESS", "payload": {"positions": "garbage"}}),
    (200, {"status": "SUCCESS", "payload": {"positions": [{"trading_symbol": "SBIN"}]}}),
    (401, {"status": "FAILURE"}),                                              # session rejected
], ids=["failure", "http_500", "no_positions_array", "not_a_list", "missing_fields", "token_rejected"])
async def test_unestablishable_position_rejects_the_sell(db_session, mock_redis, groww_enabled, response):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    fake.positions_response = response
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "FAIL-1"))
    assert out["status"] == "REJECTED" and fake.bodies("/v1/order/create") == []
    # Rejected by the exposure input check or by the SELL position gate - both fail closed.
    assert {"POSITION_FETCH_FAILED", "RISK_INPUTS_UNAVAILABLE"} & set(await _reasons(child))


@pytest.mark.asyncio
async def test_unrecognised_product_on_the_matching_instrument_rejects(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    fake.positions_response = _positions(position_row(qty=10, product="SOMETHING_NEW"))
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "PROD-2"))
    assert out["status"] == "REJECTED" and fake.bodies("/v1/order/create") == []
    assert "UNKNOWN_PRODUCT_TYPE" in await _reasons(child)


@pytest.mark.asyncio
async def test_instrument_not_in_the_groww_master_rejects(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "NOINSTR-1", symbol="NOSUCHSTOCK"))
    assert out["status"] == "REJECTED" and fake.bodies("/v1/order/create") == []
    assert "INSTRUMENT_RESOLUTION_FAILED" in await _reasons(child)


@pytest.mark.asyncio
async def test_derivative_sell_is_rejected_fail_closed(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    out, _ = await _run(mock_redis, master, child, fake,
                        _sell(master, "FNO-1", symbol="NIFTY26OCT23500CE", exchange="NFO", product="LONGTERM", qty=75))
    assert out["status"] == "REJECTED" and fake.bodies("/v1/order/create") == []


@pytest.mark.asyncio
async def test_a_symbol_groww_does_not_allow_selling_is_not_placed(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    fake.positions_response = _positions(position_row("NOSELL", isin="INE000X00001", qty=10))
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "NOSELL-1", symbol="NOSELL"))
    assert out["status"] != "SUCCESS" and fake.bodies("/v1/order/create") == []


@pytest.mark.asyncio
async def test_an_unverified_contract_makes_the_child_ineligible(db_session, mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "GROWW_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_CHILD_TRADING_ENABLED", True)
    monkeypatch.setattr(settings, "SELL_POSITION_SAFETY_BYPASS", False)
    monkeypatch.setattr("app.services.brokers.registry.GROWW_CONTRACT", GrowwContract(docs_verified=False))
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "NOCONTRACT-1"))
    assert out is None and fake.bodies("/v1/order/create") == [] and fake.calls("/v1/positions/user") == []
    assert "CHILD_BROKER_NOT_READY" in await _reasons()


@pytest.mark.asyncio
async def test_flag_off_makes_the_child_ineligible(db_session, mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "GROWW_ENABLED", False)
    monkeypatch.setattr(settings, "SELL_POSITION_SAFETY_BYPASS", False)
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    fake.positions_response = _positions(position_row(qty=10))
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "FLAGOFF-1"))
    assert out is None and fake.bodies("/v1/order/create") == []
    assert "CHILD_BROKER_NOT_READY" in await _reasons()


@pytest.mark.asyncio
async def test_get_available_long_fails_closed_on_an_unverified_client(mock_redis):
    fake = FakeGroww()
    client = make_client(mock_redis, fake, contract=GrowwContract(docs_verified=False))
    await client.login()
    with pytest.raises(PositionFetchError):
        await get_available_long(client, "NSE", "SBIN", "INTRADAY", provided_token=ALICE_STYLE_TOKEN)


@pytest.mark.asyncio
async def test_get_available_long_counts_only_the_matching_long(mock_redis):
    fake = FakeGroww()
    fake.positions_response = _positions(
        position_row(qty=10), position_row(qty=4, product="CNC"), position_row("TCS", isin="INE467B01029", qty=99),
        position_row(exchange="BSE", qty=77))
    client = make_client(mock_redis, fake)
    await client.login()
    assert await get_available_long(client, "NSE", "SBIN", "MIS", provided_token=ALICE_STYLE_TOKEN) == 10
    assert await get_available_long(client, "NSE", "SBIN", "CNC", provided_token=ALICE_STYLE_TOKEN) == 4


# ─── nothing else in the safety stack is loosened ───────────────────────────

@pytest.mark.asyncio
async def test_buy_is_unaffected_by_the_sell_gate(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    fake.positions_response = _positions()
    out, _ = await _run(mock_redis, master, child, fake, _buy(master, "BUY-1"))
    assert out["status"] == "SUCCESS", out
    assert fake.bodies("/v1/order/create")[0]["transaction_type"] == "BUY"


@pytest.mark.asyncio
async def test_low_margin_still_rejects_a_buy(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    fake.positions_response = _positions()
    fake.margin_response = ok({"clear_cash": 10.0, "net_margin_used": 0.0, "collateral_available": 0.0})
    out, _ = await _run(mock_redis, master, child, fake, _buy(master, "MARGIN-1"))
    assert out["status"] == "REJECTED" and fake.bodies("/v1/order/create") == []
    assert "INSUFFICIENT_MARGIN" in await _reasons(child)


@pytest.mark.asyncio
async def test_verified_exit_with_low_margin_is_still_copied(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    fake.positions_response = _positions(position_row(qty=10))
    fake.margin_response = ok({"clear_cash": 10.0, "net_margin_used": 0.0, "collateral_available": 0.0})
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "EXITLOW-1"))
    assert out["status"] == "SUCCESS", out


@pytest.mark.asyncio
async def test_unavailable_funds_reject_the_copy(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    fake.positions_response = _positions()
    fake.margin_response = ok({"clear_cash": 1000000.0})                      # required fields missing
    out, _ = await _run(mock_redis, master, child, fake, _buy(master, "NOFUNDS-1"))
    assert out["status"] == "REJECTED" and fake.bodies("/v1/order/create") == []


@pytest.mark.asyncio
async def test_duplicate_master_event_places_once(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    fake.positions_response = _positions(position_row(qty=10))
    client = make_client(mock_redis, fake, account_id=str(child.id), static_ip=CHILD_IP)
    await client.login()
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    event = _sell(master, "DUP-1")
    await pipeline.execute_replication(event=event, clients={str(child.id): client})
    await pipeline.execute_replication(event=event, clients={str(child.id): client})
    assert len(fake.bodies("/v1/order/create")) == 1


@pytest.mark.asyncio
async def test_emergency_stop_still_blocks(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    async with TestingSessionLocal() as s:
        (await s.get(Account, child.id)).is_emergency_stopped = True
        await s.commit()
    fake = FakeGroww()
    fake.positions_response = _positions(position_row(qty=10))
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "ESTOP-1"))
    assert out is None and fake.bodies("/v1/order/create") == []


@pytest.mark.asyncio
async def test_missing_static_ip_rejects_without_an_order_request(db_session, mock_redis, groww_enabled):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    fake.positions_response = _positions(position_row(qty=10))
    client = make_client(mock_redis, fake, account_id=str(child.id), static_ip=None)
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "NOIP-1"), client=client)
    assert out["status"] != "SUCCESS" and fake.bodies("/v1/order/create") == []


@pytest.mark.asyncio
async def test_a_cross_broker_pair_only_talks_to_groww(db_session, mock_redis, groww_enabled):
    """Alice Master event -> Groww Child: every request the Child client made went to api.groww.in
    (FakeGroww asserts the host on each request)."""
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    fake.positions_response = _positions(position_row(qty=10))
    await _run(mock_redis, master, child, fake, _sell(master, "XB-1"))
    assert fake.requests and all(r.url.host == "api.groww.in" for r in fake.requests)


@pytest.mark.asyncio
async def test_a_market_master_order_is_copied_as_a_limit_not_a_groww_market(db_session, mock_redis, groww_enabled):
    """The existing MARKET handling is unchanged: the pipeline converts a MARKET fill into a marketable LIMIT
    for every Child, which Groww's documented LIMIT type carries."""
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    fake.positions_response = _positions()
    event = _event(master, "MKT-1", side="BUY", order_type="MARKET", product="INTRADAY", exchange="NSE",
                   symbol="SBIN", qty=10, price="800", token=ALICE_STYLE_TOKEN)
    out, _ = await _run(mock_redis, master, child, fake, event)
    assert out["status"] == "SUCCESS", out
    assert fake.bodies("/v1/order/create")[0]["order_type"] == "LIMIT"


# ─── a non-object row anywhere in the positions array makes the whole book unknown ──────────

BAD_ROWS = [("string", "SBIN"), ("none", None), ("int", 5), ("bool", True), ("list", [1, 2]), ("float", 1.5)]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [r for _, r in BAD_ROWS], ids=[n for n, _ in BAD_ROWS])
@pytest.mark.parametrize("position", ["first", "last", "only"])
async def test_a_non_object_position_row_is_a_typed_error_never_a_partial_or_flat_book(mock_redis, bad, position):
    from app.services.groww.client import GrowwCapabilityUnavailable
    rows = {"first": [bad, position_row()], "last": [position_row(), bad], "only": [bad]}[position]
    fake = FakeGroww()
    fake.positions_response = _positions(*rows)
    client = make_client(mock_redis, fake)
    await client.login()
    with pytest.raises(GrowwCapabilityUnavailable, match="not an object"):         # typed, not AttributeError
        await client.get_positions()
    assert await client.get_positions_or_none() is None                            # unknown, never []


@pytest.mark.asyncio
async def test_well_formed_books_are_unaffected(mock_redis):
    fake = FakeGroww()
    client = make_client(mock_redis, fake)
    await client.login()
    fake.positions_response = _positions()
    assert await client.get_positions() == [] and await client.get_positions_or_none() == []     # a documented-shape empty book is flat
    fake.positions_response = _positions(position_row(qty=10), position_row(symbol="X", segment="FNO"))
    got = await client.get_positions()
    assert [(p.symbol, p.quantity) for p in got] == [("SBIN", 10)]                 # other segments still skipped


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [r for _, r in BAD_ROWS], ids=[n for n, _ in BAD_ROWS])
async def test_sell_stays_blocked_when_the_book_has_a_non_object_row_even_beside_a_covering_long(db_session, mock_redis, groww_enabled, bad):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    fake.positions_response = _positions(position_row(qty=10, product="MIS"), bad)    # a valid covering long is present
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "BADROW-1", qty=10))
    assert out["status"] == "REJECTED" and fake.bodies("/v1/order/create") == []
    assert {"POSITION_FETCH_FAILED", "RISK_INPUTS_UNAVAILABLE"} & set(await _reasons(child))
