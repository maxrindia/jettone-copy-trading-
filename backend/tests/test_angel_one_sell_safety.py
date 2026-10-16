"""
SELL position safety for an Angel One Child with SELL_POSITION_SAFETY_BYPASS=false.

Real CopyEnginePipeline + real AngelOneClient + the in-memory DB; only the broker
HTTP is faked (angel_helpers.FakeAngel) and its response contract is SYNTHETIC -
see angel_helpers.py. The question answered here is whether Jettone's safety
logic holds for an Angel One Child:
  - a SELL that reduces a broker-confirmed long is copied
  - a SELL that would open or deepen a short is rejected, never partially sold
  - missing / unverified / malformed position data fails closed
  - the Master's (other broker's) instrument id never reaches Angel One
"""
import json
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.accounts import Account
from app.models.audit import AuditLog
from app.models.trades import Order
from app.services.alice_blue.types import FundsData
from app.services.angel_one.contract import ANGEL_CONTRACT
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.risk_inputs import PositionFetchError, get_available_long
from app.services.copy_engine.validator import EventValidator
from conftest import TestingSessionLocal
from test_order_handling_matrix import _event, _setup
from angel_helpers import ALICE_STYLE_TOKEN, SBIN_TOKEN, FakeAngel, make_client, position_row

CHILD_IP = "151.245.182.59"


@pytest.fixture
def angel_enabled(monkeypatch):
    monkeypatch.setattr(settings, "ANGEL_ONE_ENABLED", True)
    monkeypatch.setattr(settings, "SELL_POSITION_SAFETY_BYPASS", False)
    from angel_helpers import SYNTHETIC_CONTRACT
    monkeypatch.setattr("app.services.brokers.registry.ANGEL_CONTRACT", SYNTHETIC_CONTRACT)


async def _angel_child(db_session):
    master, child = await _setup(db_session)           # Alice Master -> Child (broker set to Angel One below)
    async with TestingSessionLocal() as s:
        row = await s.get(Account, child.id)
        row.broker = "angelone"
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


async def _reasons(child):
    async with TestingSessionLocal() as s:
        return [r.reason_code for r in (await s.execute(
            select(AuditLog).where(AuditLog.account_id == child.id))).scalars().all() if r.reason_code]


async def _all_reasons():
    async with TestingSessionLocal() as s:
        return [r.reason_code for r in (await s.execute(select(AuditLog))).scalars().all() if r.reason_code]


# ─── a valid position-reducing exit is handled correctly ────────────────────

@pytest.mark.asyncio
async def test_exit_that_reduces_a_confirmed_long_is_placed(db_session, mock_redis, angel_enabled):
    master, child = await _angel_child(db_session)
    fake = FakeAngel(positions=[position_row(net="10", product="INTRADAY")])
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "EXIT-1", qty=10))
    assert out["status"] == "SUCCESS", out
    (body,) = fake.order_bodies()
    assert body["transactiontype"] == "SELL" and body["quantity"] == "10"
    assert body["producttype"] == "INTRADAY" and body["ordertype"] == "LIMIT" and body["price"] == "800"
    assert body["symboltoken"] == SBIN_TOKEN and body["tradingsymbol"] == "SBIN-EQ"
    assert ALICE_STYLE_TOKEN not in json.dumps(body)               # the Master's broker id never reaches Angel One
    assert len(fake.calls("getPosition")) >= 1                      # the position WAS checked
    async with TestingSessionLocal() as s:
        orders = (await s.execute(select(Order).where(Order.account_id == child.id))).scalars().all()
    assert orders and {o.broker for o in orders} == {"ANGEL_ONE"}   # persisted under Angel One, not ALICE_BLUE


@pytest.mark.asyncio
async def test_partial_exit_of_a_larger_long_is_placed(db_session, mock_redis, angel_enabled):
    master, child = await _angel_child(db_session)
    fake = FakeAngel(positions=[position_row(net="25")])
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "EXIT-2", qty=10))
    assert out["status"] == "SUCCESS", out
    assert fake.order_bodies()[0]["quantity"] == "10"


# ─── a SELL that would open unintended short exposure is rejected ───────────

@pytest.mark.asyncio
async def test_sell_when_flat_is_rejected(db_session, mock_redis, angel_enabled):
    master, child = await _angel_child(db_session)
    fake = FakeAngel(positions=[])
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "SHORT-1"))
    assert out["status"] == "REJECTED"
    assert fake.order_bodies() == []
    assert "INSUFFICIENT_LONG_POSITION" in await _reasons(child)


@pytest.mark.asyncio
async def test_sell_larger_than_the_long_is_rejected_whole(db_session, mock_redis, angel_enabled):
    master, child = await _angel_child(db_session)
    fake = FakeAngel(positions=[position_row(net="5")])
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "SHORT-2", qty=10))
    assert out["status"] == "REJECTED"
    assert fake.order_bodies() == []                                # never a partial sell
    assert "INSUFFICIENT_LONG_POSITION" in await _reasons(child)


@pytest.mark.asyncio
async def test_sell_when_already_short_is_rejected(db_session, mock_redis, angel_enabled):
    master, child = await _angel_child(db_session)
    fake = FakeAngel(positions=[position_row(net="-3")])
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "SHORT-3", qty=1))
    assert out["status"] == "REJECTED" and fake.order_bodies() == []


@pytest.mark.asyncio
async def test_a_long_in_another_product_does_not_cover_the_sell(db_session, mock_redis, angel_enabled):
    master, child = await _angel_child(db_session)
    fake = FakeAngel(positions=[position_row(net="10", product="DELIVERY")])    # CNC holding
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "PROD-1", product="INTRADAY"))
    assert out["status"] == "REJECTED" and fake.order_bodies() == []


@pytest.mark.asyncio
async def test_a_long_in_another_instrument_does_not_cover_the_sell(db_session, mock_redis, angel_enabled):
    master, child = await _angel_child(db_session)
    fake = FakeAngel(positions=[position_row(symbol="TCS-EQ", token="11536", net="10")])
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "INSTR-1"))
    assert out["status"] == "REJECTED" and fake.order_bodies() == []


@pytest.mark.asyncio
async def test_matching_is_by_angel_token_even_if_the_symbol_spelling_differs(db_session, mock_redis, angel_enabled):
    master, child = await _angel_child(db_session)
    fake = FakeAngel(positions=[position_row(symbol="SBIN", token=SBIN_TOKEN, net="10")])
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "TOK-1"))
    assert out["status"] == "SUCCESS", out


# ─── missing / unverified / malformed position data fails safely ────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    (200, {"status": False, "message": "boom"}),                 # broker error
    (500, {}),                                                   # server error
    (200, {"status": True, "data": "garbage"}),                 # wrong shape
    (200, {"status": True, "data": [{"syn_sym": "SBIN-EQ"}]}),  # row missing fields
    (403, {"error_type": "TokenException"}),                     # session rejected
], ids=["status_false", "http_500", "not_a_list", "missing_fields", "token_exception"])
async def test_unestablishable_position_rejects_the_sell(db_session, mock_redis, angel_enabled, response):
    master, child = await _angel_child(db_session)
    fake = FakeAngel(positions=[position_row()])
    fake.positions_response = response
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "FAIL-1"))
    assert out["status"] == "REJECTED"
    assert fake.order_bodies() == []
    # Rejected by the exposure input check or by the SELL position gate - both fail closed.
    assert {"POSITION_FETCH_FAILED", "RISK_INPUTS_UNAVAILABLE"} & set(await _reasons(child))


@pytest.mark.asyncio
async def test_unrecognised_product_on_the_matching_instrument_rejects(db_session, mock_redis, angel_enabled):
    master, child = await _angel_child(db_session)
    fake = FakeAngel(positions=[position_row(net="10", product="SOMETHING_NEW")])
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "PROD-2"))
    assert out["status"] == "REJECTED" and fake.order_bodies() == []
    assert "UNKNOWN_PRODUCT_TYPE" in await _reasons(child)


@pytest.mark.asyncio
async def test_instrument_not_in_the_angel_master_rejects(db_session, mock_redis, angel_enabled):
    master, child = await _angel_child(db_session)
    fake = FakeAngel(positions=[position_row()])
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "NOINSTR-1", symbol="NOSUCHSTOCK"))
    assert out["status"] == "REJECTED" and fake.order_bodies() == []
    assert "INSTRUMENT_RESOLUTION_FAILED" in await _reasons(child)


@pytest.mark.asyncio
async def test_derivative_sell_is_rejected_fail_closed(db_session, mock_redis, angel_enabled):
    master, child = await _angel_child(db_session)
    fake = FakeAngel(positions=[position_row(symbol="NIFTY26OCT23500CE", token="26000", exchange="NFO", product="CARRYFORWARD", net="65")])
    out, _ = await _run(mock_redis, master, child,
                        fake, _sell(master, "FNO-1", symbol="NIFTY26OCT23500CE", exchange="NFO", product="LONGTERM", qty=65))
    assert out["status"] == "REJECTED" and fake.order_bodies() == []


@pytest.mark.asyncio
async def test_unverified_contract_makes_the_child_ineligible(db_session, mock_redis, monkeypatch):
    """The production default: the flag may be on, but with no verified contract the
    Angel One account is never copied to - no position or order request at all."""
    monkeypatch.setattr(settings, "ANGEL_ONE_ENABLED", True)
    monkeypatch.setattr(settings, "SELL_POSITION_SAFETY_BYPASS", False)
    assert ANGEL_CONTRACT.verified is False
    master, child = await _angel_child(db_session)
    fake = FakeAngel(positions=[position_row()])
    out, client = await _run(mock_redis, master, child, fake, _sell(master, "NOCONTRACT-1"))
    assert out is None                                                     # no result: not an eligible child
    assert fake.order_bodies() == [] and fake.calls("getPosition") == []
    assert "CHILD_BROKER_NOT_READY" in await _all_reasons()


@pytest.mark.asyncio
async def test_flag_off_makes_the_child_ineligible(db_session, mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "ANGEL_ONE_ENABLED", False)
    monkeypatch.setattr(settings, "SELL_POSITION_SAFETY_BYPASS", False)
    from angel_helpers import SYNTHETIC_CONTRACT
    monkeypatch.setattr("app.services.brokers.registry.ANGEL_CONTRACT", SYNTHETIC_CONTRACT)
    master, child = await _angel_child(db_session)
    fake = FakeAngel(positions=[position_row()])
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "FLAGOFF-1"))
    assert out is None and fake.order_bodies() == []


@pytest.mark.asyncio
async def test_get_available_long_fails_closed_on_an_unverified_client(mock_redis):
    """The safety lookup itself, independent of routing: an Angel client whose
    contract is unverified can never report a position (so never 'flat')."""
    fake = FakeAngel(positions=[position_row()])
    client = make_client(mock_redis, fake, contract=ANGEL_CONTRACT)
    await client.login()
    with pytest.raises(PositionFetchError):
        await get_available_long(client, "NSE", "SBIN", "INTRADAY", provided_token=ALICE_STYLE_TOKEN)


@pytest.mark.asyncio
async def test_get_available_long_counts_only_the_matching_long(mock_redis):
    fake = FakeAngel(positions=[
        position_row(net="10"), position_row(net="4", product="DELIVERY"),
        position_row(symbol="TCS-EQ", token="11536", net="99"),
    ])
    client = make_client(mock_redis, fake)
    await client.login()
    assert await get_available_long(client, "NSE", "SBIN", "MIS", provided_token=ALICE_STYLE_TOKEN) == 10
    assert await get_available_long(client, "NSE", "SBIN", "CNC", provided_token=ALICE_STYLE_TOKEN) == 4


# ─── nothing else in the safety stack is loosened ───────────────────────────

@pytest.mark.asyncio
async def test_buy_is_unaffected_by_the_sell_gate(db_session, mock_redis, angel_enabled):
    master, child = await _angel_child(db_session)
    fake = FakeAngel(positions=[])
    out, _ = await _run(mock_redis, master, child, fake, _buy(master, "BUY-1"))
    assert out["status"] == "SUCCESS", out
    assert fake.order_bodies()[0]["transactiontype"] == "BUY"


@pytest.mark.asyncio
async def test_low_margin_still_rejects_a_buy(db_session, mock_redis, angel_enabled):
    master, child = await _angel_child(db_session)
    fake = FakeAngel(rms={"syn_avail": "10", "syn_used": "0", "syn_coll": "0"})
    out, _ = await _run(mock_redis, master, child, fake, _buy(master, "MARGIN-1"))
    assert out["status"] == "REJECTED" and fake.order_bodies() == []
    assert "INSUFFICIENT_MARGIN" in await _reasons(child)


@pytest.mark.asyncio
async def test_verified_exit_with_low_margin_is_still_copied(db_session, mock_redis, angel_enabled):
    master, child = await _angel_child(db_session)
    fake = FakeAngel(positions=[position_row(net="10")], rms={"syn_avail": "10", "syn_used": "0", "syn_coll": "0"})
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "EXITLOW-1"))
    assert out["status"] == "SUCCESS", out


@pytest.mark.asyncio
async def test_unavailable_funds_reject_the_copy(db_session, mock_redis, angel_enabled):
    master, child = await _angel_child(db_session)
    fake = FakeAngel(positions=[position_row()])
    fake.rms = {}                                                         # RMS gives none of the expected fields
    out, _ = await _run(mock_redis, master, child, fake, _buy(master, "NOFUNDS-1"))
    assert out["status"] == "REJECTED" and fake.order_bodies() == []


@pytest.mark.asyncio
async def test_duplicate_master_event_places_once(db_session, mock_redis, angel_enabled):
    master, child = await _angel_child(db_session)
    fake = FakeAngel(positions=[position_row(net="10")])
    client = make_client(mock_redis, fake, account_id=str(child.id), static_ip=CHILD_IP)
    await client.login()
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    event = _sell(master, "DUP-1")
    await pipeline.execute_replication(event=event, clients={str(child.id): client})
    await pipeline.execute_replication(event=event, clients={str(child.id): client})
    assert len(fake.order_bodies()) == 1


@pytest.mark.asyncio
async def test_missing_static_ip_rejects_without_an_order_request(db_session, mock_redis, angel_enabled):
    master, child = await _angel_child(db_session)
    fake = FakeAngel(positions=[position_row(net="10")])
    client = make_client(mock_redis, fake, account_id=str(child.id), static_ip=None)
    out, _ = await _run(mock_redis, master, child, fake, _sell(master, "NOIP-1"), client=client)
    assert out["status"] != "SUCCESS" and fake.order_bodies() == []


@pytest.mark.asyncio
async def test_a_cross_broker_pair_never_calls_alice_endpoints(db_session, mock_redis, angel_enabled):
    """Alice Master event -> Angel Child: every HTTP request the Child client made
    went to apiconnect.angelone.in (FakeAngel asserts the host on each request)."""
    master, child = await _angel_child(db_session)
    fake = FakeAngel(positions=[position_row(net="10")])
    await _run(mock_redis, master, child, fake, _sell(master, "XB-1"))
    assert fake.requests and all(r.url.host == "apiconnect.angelone.in" for r in fake.requests)
