"""
Alice Blue Master -> Groww Child in OBSERVE-ONLY terms: what stops a Groww Child order today, and what the rest of the path would do.

Offline: the in-memory DB, mock Redis, the real CopyEnginePipeline and the real GrowwClient whose HTTP is faked (groww_helpers.FakeGroww).
No broker, network or production access. Passing here is CODE-LEVEL evidence only: it shows the gates fail closed and the Groww adapter
refuses bad orders before anything is built. It is not evidence that Groww accepts an order.

The engine-level observe-only stop (Live Trading OFF -> no new order reaches the pipeline at all) is pinned separately by
test_live_trading_toggle.py::test_engine_copies_nothing_new_while_off.
"""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.audit import AuditLog
from app.services.alice_blue.types import OrderUpdateEvent
from app.services.copy_engine import child_preview as P
from app.services.copy_engine.detector import MasterTradeDetector
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.groww.instruments import GrowwInstrumentStore
from app.services.strategy_membership import MembershipConflict, assert_broker_can_be_child, assert_pair_supported
from conftest import TestingSessionLocal
from groww_helpers import ALICE_STYLE_TOKEN, CSV_HEADER, CSV_ROWS, FakeGroww, make_client, make_store
from test_groww_sell_safety import CHILD_IP, _groww_child
from test_order_handling_matrix import _event

CREATE = "/v1/order/create"


@pytest.fixture
def production_groww_flags(monkeypatch):
    """Exactly production's Groww flags: setup and login allowed, Child trading off."""
    monkeypatch.setattr(settings, "GROWW_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_CHILD_TRADING_ENABLED", False)
    monkeypatch.setattr(settings, "SELL_POSITION_SAFETY_BYPASS", False)


@pytest.fixture
def groww_child_flag_on(monkeypatch):
    monkeypatch.setattr(settings, "GROWW_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_CHILD_TRADING_ENABLED", True)
    monkeypatch.setattr(settings, "SELL_POSITION_SAFETY_BYPASS", False)


def entry(master, oid, *, side="BUY", order_type="LIMIT", symbol="SBIN", exchange="NSE", qty=10, price="800", trigger="0",
          product="INTRADAY"):
    return _event(master, oid, side=side, order_type=order_type, product=product, exchange=exchange, symbol=symbol, qty=qty,
                  price=price, trigger=trigger, token=ALICE_STYLE_TOKEN)


async def run(mock_redis, master, child, fake, event, *, store=None, pipe=None):
    client = make_client(mock_redis, fake, account_id=str(child.id), static_ip=CHILD_IP, store=store)
    await client.login()
    pipe = pipe or CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipe.execute_replication(event=event, clients={str(child.id): client})
    return (res.get("results") or {}).get(str(child.id)), res


async def audit_codes():
    async with TestingSessionLocal() as s:
        return [r.reason_code for r in (await s.execute(select(AuditLog))).scalars().all() if r.reason_code]


def ambiguous_store():
    s = GrowwInstrumentStore()
    dup = "NSE,3046,SBIN,NSE-SBIN2,State Bank Duplicate,EQ,CASH,EQ,INE062A01021,,,,,1,0.05,100000,False,True,True"
    s.load_csv_text("\n".join([CSV_HEADER] + CSV_ROWS + [dup]))
    return s


# ── with production's settings, a Groww Child cannot be added or copied to ───────────────────────────────────────────────────

def test_production_flags_refuse_a_groww_child_at_membership(production_groww_flags):
    with pytest.raises(MembershipConflict) as exc:
        assert_broker_can_be_child("groww")
    assert exc.value.code == "CHILD_BROKER_UNSUPPORTED"


def test_even_with_the_child_flag_on_the_closed_pair_refuses_membership(groww_child_flag_on):
    assert_broker_can_be_child("groww")                                          # the role gate alone would allow it ...
    with pytest.raises(MembershipConflict) as exc:
        assert_pair_supported(SimpleNamespace(broker="alicebluenew"), "groww")    # ... the pair allowlist does not
    assert exc.value.code == "BROKER_PAIR_NOT_SUPPORTED"


@pytest.mark.asyncio
async def test_production_flags_route_no_order_to_an_existing_groww_member(db_session, mock_redis, production_groww_flags):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    out, res = await run(mock_redis, master, child, fake, entry(master, "OBS-ROUTE"))
    assert out is None                                                           # never routed, so no per-Child result
    assert fake.bodies(CREATE) == []
    assert "CHILD_BROKER_NOT_READY" in await audit_codes()                       # the reason is recorded


@pytest.mark.asyncio
async def test_child_flag_on_but_pair_closed_skips_with_a_recorded_reason(db_session, mock_redis, groww_child_flag_on):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    out, _ = await run(mock_redis, master, child, fake, entry(master, "OBS-PAIR"))
    assert out["status"] == "SKIPPED" and out["reason"] == "BROKER_PAIR_NOT_SUPPORTED", out
    assert fake.bodies(CREATE) == []
    assert "BROKER_PAIR_NOT_SUPPORTED" in await audit_codes()


# ── with the pair waived for the test only: the Groww adapter still refuses bad orders before building a request ────────────

pair_waived = pytest.mark.usefixtures("cross_broker_pairs_enabled", "groww_child_flag_on")


@pair_waived
@pytest.mark.asyncio
@pytest.mark.parametrize("symbol,store,why", [
    ("UNKNOWNCO", None, "not in Groww's catalog"),
    ("SBIN", "ambiguous", "two Groww rows match"),
    ("SBIN", "empty", "Groww's catalog is not loaded"),
])
async def test_missing_or_ambiguous_groww_mapping_sends_nothing(db_session, mock_redis, symbol, store, why):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    s = {"ambiguous": ambiguous_store(), "empty": GrowwInstrumentStore(), None: make_store()}[store]
    out, _ = await run(mock_redis, master, child, fake, entry(master, f"OBS-MAP-{why[:6]}", symbol=symbol), store=s)
    assert out["status"] == "REJECTED", (why, out)
    assert fake.bodies(CREATE) == [], why


@pair_waived
@pytest.mark.asyncio
async def test_a_derivative_never_reaches_groww(db_session, mock_redis):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    out, _ = await run(mock_redis, master, child, fake,
                       entry(master, "OBS-NFO", symbol="NIFTY26OCT23500CE", exchange="NFO", qty=75, product="NRML"))
    assert out["status"] == "SKIPPED" and out["reason"] == "INSTRUMENT_UNMAPPED", out
    assert fake.bodies(CREATE) == []


@pair_waived
@pytest.mark.asyncio
@pytest.mark.parametrize("symbol,qty,side,why", [
    ("SMALLFRZ", 150, "BUY", "above Groww's freeze quantity"),
    ("NOSELL", 10, "SELL", "Groww does not allow SELL for it"),
    ("UNREADABLE", 10, "BUY", "unreadable buy/sell flags"),
])
async def test_an_order_the_groww_adapter_refuses_sends_nothing(db_session, mock_redis, symbol, qty, side, why):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    out, _ = await run(mock_redis, master, child, fake, entry(master, f"OBS-ADP-{symbol}", symbol=symbol, qty=qty, side=side))
    assert out["status"] == "REJECTED", (why, out)
    assert fake.bodies(CREATE) == [], why


@pair_waived
@pytest.mark.asyncio
async def test_a_repeated_master_event_creates_one_groww_order_at_most(db_session, mock_redis):
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    pipe = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    event = entry(master, "OBS-DUP")
    await run(mock_redis, master, child, fake, event, pipe=pipe)
    await run(mock_redis, master, child, fake, event, pipe=pipe)
    bodies = fake.bodies(CREATE)
    assert len(bodies) == 1                                                       # the first event created exactly one order
    assert all(ALICE_STYLE_TOKEN not in str(b) for b in bodies)                   # the Master's id never reaches Groww


def test_an_unknown_master_status_is_not_turned_into_an_event():
    update = OrderUpdateEvent(broker_order_id="AB-1", client_order_id=None, symbol="SBIN", side="BUY", quantity=10, filled_quantity=0,
                              fill_price=Decimal("0"), status="SOMETHING_NEW", timestamp=datetime.now(timezone.utc), account_id="m-1",
                              product_type="MIS", order_type="LIMIT", price=Decimal("800"))
    assert MasterTradeDetector(master_account_id="m-1", master_broker="alicebluenew").process_order_update(update) is None


# ── the read-only preview ─────────────────────────────────────────────────────────────────────────────────────────────────────

MASTER = SimpleNamespace(id="00000000-0000-0000-0000-0000000000aa")


def preview(event, **kw):
    kw.setdefault("groww_store", make_store())
    return P.preview_child_order(event, master_broker="alicebluenew", child_broker="groww", **kw)


def test_with_production_flags_the_preview_lists_both_closed_gates_and_still_checks_the_order(production_groww_flags):
    got = preview(entry(MASTER, "PV-1"))
    assert got.would_send is False
    assert {"CHILD_BROKER_NOT_READY", "BROKER_PAIR_NOT_SUPPORTED"} <= set(got.codes)
    assert got.order_ok_if_enabled is True                                       # nothing about the ORDER is wrong
    assert got.child_order_type == "LIMIT" and got.child_quantity == 10
    assert got.order_body["trading_symbol"] == "SBIN" and got.order_body["product"] == "MIS"
    assert got.order_body["order_type"] == "LIMIT" and got.order_body["order_reference_id"] == "PREVIEW"
    assert ALICE_STYLE_TOKEN not in str(got.order_body)


@pytest.mark.parametrize("event_kw,store,code", [
    (dict(symbol="UNKNOWNCO"), None, "INSTRUMENT_UNMAPPED"),
    ({}, "ambiguous", "INSTRUMENT_UNMAPPED"),
    ({}, "empty", "INSTRUMENT_UNMAPPED"),
    (dict(symbol="NIFTY26OCT23500CE", exchange="NFO", qty=75, product="NRML"), None, "INSTRUMENT_UNMAPPED"),
    (dict(symbol="SMALLFRZ", qty=150), None, "ADAPTER_REFUSED"),
    (dict(symbol="NOSELL", side="SELL"), None, "ADAPTER_REFUSED"),
    (dict(symbol="UNREADABLE"), None, "ADAPTER_REFUSED"),
    (dict(product="NRML"), None, "ADAPTER_REFUSED"),                            # Groww equity takes CNC / MIS only
    (dict(order_type="SL", trigger="0"), None, "ADAPTER_REFUSED"),              # an SL needs a trigger
])
def test_the_preview_fails_closed_on_every_order_problem(production_groww_flags, event_kw, store, code):
    s = {"ambiguous": ambiguous_store(), "empty": GrowwInstrumentStore(), None: make_store()}[store]
    got = preview(entry(MASTER, "PV-BAD", **event_kw), groww_store=s)
    assert code in got.codes and got.order_ok_if_enabled is False and got.would_send is False, got
    assert got.order_body is None


def test_a_zero_quantity_and_a_non_entry_event_are_refused(production_groww_flags):
    assert "ZERO_LOT_RESULT" in preview(entry(MASTER, "PV-Z"), multiplier=Decimal("0")).codes
    modified = entry(MASTER, "PV-M")
    modified.event_type = "ORDER_MODIFIED"
    assert "UNSUPPORTED_EVENT" in preview(modified).codes


def test_an_sl_entry_is_previewed_as_a_native_sl(production_groww_flags):
    got = preview(entry(MASTER, "PV-SL", order_type="SL", price="800", trigger="795"))
    assert got.child_order_type == "SL" and got.order_body["order_type"] == "SL" and got.order_body["trigger_price"] == 795.0


def test_the_preview_never_touches_the_network(production_groww_flags, monkeypatch):
    import httpx

    def boom(*a, **k):
        raise AssertionError("the preview must not send anything")
    monkeypatch.setattr(httpx.AsyncClient, "send", boom)
    monkeypatch.setattr(httpx.Client, "send", boom)
    assert preview(entry(MASTER, "PV-NET")).would_send is False


def test_alice_to_alice_previews_with_no_blockers():
    got = P.preview_child_order(entry(MASTER, "PV-AA"), master_broker="alicebluenew", child_broker="alicebluenew")
    assert got.blockers == () and got.would_send is False
