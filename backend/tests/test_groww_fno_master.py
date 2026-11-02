"""
Phase A: a Groww MASTER's F&O orders, copied to an Alice Blue CHILD. OFFLINE: fakes only, no broker contacted.

What is proven here (code-level, with documentation-shaped rows - NOT live evidence):
  * every gate is closed by default and each one on its own opens nothing (GROWW_FNO_MASTER_ENABLED, GROWW_FNO_QUANTITY_UNIT,
    CROSS_BROKER_FNO_PAIRS), and the cash path is byte-for-byte what it was;
  * a Groww F&O order becomes a broker-neutral record with a canonical contract key, exchange NFO and the quantity in UNITS;
  * the Alice Child receives ITS OWN token, resolved from an exact row of ITS OWN contract master - never Groww's id - and the original
    `CROSS_BROKER_DERIVATIVE_UNMAPPED` refusal is what every unlisted, unresolved, ambiguous or unreadable case still gets;
  * modifications of a routed F&O order reach the Alice Child.

What it does NOT show: that Groww's F&O order list looks like this (none has been captured), what Groww's F&O `quantity` counts, or which
products / exchange Groww reports for F&O. Those stay closed gates until a reviewed live capture sets them.
"""
import dataclasses
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

from app.core.config import Settings, settings
from app.models.trades import CopyTrade
from app.services.alice_blue.types import OrderResultCategory
from app.services.copy_engine import derivative_routing
from app.services.groww import segments
from app.services.groww.client import GrowwError
from app.services.groww.instruments import GrowwInstrumentStore
from app.services.instruments import terms
from app.services.instruments.contracts import contract_from_key, make_contract
from app.services.reliability.master_order_poller import MasterOrderPoller
from conftest import TestingSessionLocal
from groww_helpers import CSV_HEADER, CSV_ROWS
from test_alice_cross_broker_resolution import store_with
from test_groww_master_b1 import Chain, MasterGroww, canary, groww_roles_open  # noqa: F401  (fixtures are used by name)

ALICE_NFO_TOKENS = {"CE": "50001", "PE": "50003", "FUT": "50005"}      # the NIFTY 26-Nov-2026 rows of the Alice contract master fixture
FNO_ROWS = [
    "NSE,1,NIFTY26NOV23500CE,x,,CE,FNO,,,NIFTY,26000,2026-11-26,23500,75,0.05,1800,1,1,1",
    "NSE,2,NIFTY26NOV23500PE,x,,PE,FNO,,,NIFTY,26000,2026-11-26,23500,75,0.05,1800,1,1,1",
    "NSE,3,NIFTY26NOVFUT,x,,FUT,FNO,,,NIFTY,26000,2026-11-26,,75,0.05,1800,1,1,1",
    "NSE,4,NIFTY26NOV24000CE,x,,CE,FNO,,,NIFTY,26000,2026-11-26,24000,75,0.05,1800,1,1,1",         # a strike Alice's master does not list
    "NSE,5,BANKNIFTY26NOV51500CE,x,,CE,FNO,,,BANKNIFTY,26009,2026-11-26,51500,35,0.05,600,1,1,1",   # Alice's master lists it TWICE
]
CE, PE, FUT, MISSING, AMBIGUOUS = (r.split(",")[2] for r in FNO_ROWS)


def fno_store() -> GrowwInstrumentStore:
    store = GrowwInstrumentStore()
    store.load_csv_text("\n".join([CSV_HEADER] + CSV_ROWS + FNO_ROWS))
    return store


class FnoGroww(MasterGroww):
    """A Groww account whose F&O orders are placed OUTSIDE Jettone: the order list answers per segment, as Groww's does."""

    fno_error = False

    def fno_order(self, oid, trading_symbol, *, side="BUY", otype="LIMIT", price=100, qty=75, filled=0, status="OPEN", product="NRML",
                  exchange="NSE", avg=None):
        self.book[oid] = dict(status=status, qty=qty, filled=filled, side=side, otype=otype, price=price, trigger=0, symbol=trading_symbol,
                              product=product, ref="APP" + str(oid)[-12:], exchange=exchange, avg=avg, seg="FNO")

    def row(self, oid):
        r = super().row(oid)
        r["segment"] = self.book[oid].get("seg", "CASH")
        return r

    async def __call__(self, request):
        if request.url.path == "/v1/order/list" and self.list_override is None:
            self.requests.append(request)
            seg = request.url.params.get("segment")
            if seg == "FNO" and self.fno_error:
                return httpx.Response(500, json={"status": "FAILURE"})
            return httpx.Response(200, json={"status": "SUCCESS", "payload": {
                "order_list": [self.row(o) for o in self.book if self.book[o].get("seg", "CASH") == seg]}})
        return await super().__call__(request)

    def segments_read(self):
        return [r.url.params.get("segment") for r in self.requests if r.url.path == "/v1/order/list"]


@pytest.fixture
def fno_gates(groww_roles_open, canary, monkeypatch):
    """Everything an F&O copy needs: the role gates, a FRESH Alice contract master, the F&O flag, an explicit quantity unit and the pair."""
    monkeypatch.setattr(terms, "store", store_with(loaded_at=datetime.now(timezone.utc)))
    monkeypatch.setattr(settings, "GROWW_FNO_MASTER_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_FNO_QUANTITY_UNIT", "UNITS")
    monkeypatch.setattr(settings, "CROSS_BROKER_FNO_PAIRS", "groww>alicebluenew")


async def fno_chain(db_session, redis):
    c = await Chain().build(db_session, redis)
    c.master_fake = FnoGroww()
    c.master_client._http = httpx.AsyncClient(transport=httpx.MockTransport(c.master_fake))
    c.master_client._instruments = fno_store()
    c.poller = MasterOrderPoller(master_account_id=str(c.master.id), client_provider=lambda _i: c.master_client, on_order_update=c.on_event,
                                 pipeline=c.pipeline, check_interval_seconds=5)                  # a fresh poller: its first read is the baseline
    await c.poller.poll_once()
    c.events.clear()
    c.results.clear()
    return c


async def copies_for(account):
    async with TestingSessionLocal() as s:
        return (await s.execute(select(CopyTrade).where(CopyTrade.follower_account_id == account.id))).scalars().all()


# ── gates: closed by default, and each one alone opens nothing ───────────────────────────────────────────────────────────────

def test_every_f_and_o_gate_is_closed_by_default():
    s = Settings(_env_file=None)
    assert s.GROWW_FNO_MASTER_ENABLED is False and s.GROWW_FNO_QUANTITY_UNIT == "" and s.CROSS_BROKER_FNO_PAIRS == ""
    assert [p.name for p in segments.enabled_policies()] == ["CASH"]


@pytest.mark.parametrize("value,expected", [
    ("", set()), ("groww>alicebluenew", {("groww", "alicebluenew")}), (" Groww > AliceBlueNew ", {("groww", "alicebluenew")}),
    ("groww", set()), ("groww>nonsense", set()), (">alicebluenew", set()), ("groww>alicebluenew;x", set()), ("kite>alicebluenew", {("zerodha", "alicebluenew")}),
])
def test_the_pair_list_opens_only_pairs_it_names_correctly(monkeypatch, value, expected):
    from app.services.brokers.registry import listed_fno_pairs

    monkeypatch.setattr(settings, "CROSS_BROKER_FNO_PAIRS", value)
    assert listed_fno_pairs() == frozenset(expected)


@pytest.mark.asyncio
async def test_with_the_defaults_the_f_and_o_list_is_never_even_read(db_session, mock_redis, groww_roles_open, canary, monkeypatch):
    monkeypatch.setattr(terms, "store", store_with(loaded_at=datetime.now(timezone.utc)))
    c = await fno_chain(db_session, mock_redis)
    c.master_fake.fno_order("F-1", CE)
    await c.poll()
    assert set(c.master_fake.segments_read()) == {"CASH"} and c.events == [] and c.placed_on_alice() == []


@pytest.mark.asyncio
async def test_the_f_and_o_flag_alone_reads_the_list_but_copies_nothing_without_a_quantity_unit(db_session, mock_redis, fno_gates, monkeypatch):
    monkeypatch.setattr(settings, "GROWW_FNO_QUANTITY_UNIT", "")
    c = await fno_chain(db_session, mock_redis)
    c.master_fake.fno_order("F-1", CE)
    await c.poll()
    assert "FNO" in c.master_fake.segments_read() and c.events == [] and c.placed_on_alice() == []
    records = await c.master_client.get_order_book()
    assert [r.status for r in records] == ["UNSUPPORTED_FNO_QUANTITY_UNIT_UNSET"]


# ── the segment policy: a raw F&O row becomes a record carrying a canonical contract ─────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("symbol,kind,expiry,strike", [(CE, "CE", "2026-11-26", "23500"), (PE, "PE", "2026-11-26", "23500"), (FUT, "FUT", "2026-11-26", None)])
async def test_an_f_and_o_row_becomes_a_record_with_the_canonical_contract_exchange_nfo_and_units(db_session, mock_redis, fno_gates, symbol, kind, expiry, strike):
    c = await fno_chain(db_session, mock_redis)
    c.master_fake.fno_order("F-1", symbol, qty=150)
    (rec,) = await c.master_client.get_order_book()
    assert rec.exchange == "NFO" and rec.quantity == 150 and rec.symbol == symbol and rec.status == "OPEN" and rec.instrument_id is None
    assert rec.contract_key == make_contract(kind, "NFO", "NIFTY", expiry, strike).key
    assert contract_from_key(rec.contract_key) == make_contract(kind, "NFO", "NIFTY", expiry, strike)


@pytest.mark.asyncio
async def test_a_lots_unit_is_converted_to_units_with_the_instruments_lot_size(db_session, mock_redis, fno_gates, monkeypatch):
    monkeypatch.setattr(settings, "GROWW_FNO_QUANTITY_UNIT", "lots")
    c = await fno_chain(db_session, mock_redis)
    c.master_fake.fno_order("F-1", CE, qty=2, filled=1, status="OPEN")
    (rec,) = await c.master_client.get_order_book()
    assert (rec.quantity, rec.filled_quantity) == (150, 75)


@pytest.mark.asyncio
@pytest.mark.parametrize("change,reason", [
    (dict(qty=70), "QUANTITY_NOT_LOT_MULTIPLE"), (dict(product="CNC"), "PRODUCT_NOT_FNO"), (dict(exchange="BSE"), "EXCHANGE_NOT_NSE"),
])
async def test_a_row_it_cannot_prove_it_understands_is_unsupported_and_never_dispatched(db_session, mock_redis, fno_gates, change, reason):
    c = await fno_chain(db_session, mock_redis)
    c.master_fake.fno_order("F-1", CE, **change)
    (rec,) = await c.master_client.get_order_book()
    assert rec.status == "UNSUPPORTED_FNO_" + reason and rec.contract_key is None
    await c.poll()
    assert c.events == [] and c.placed_on_alice() == []


@pytest.mark.asyncio
async def test_an_f_and_o_instrument_the_catalog_does_not_know_is_unsupported(db_session, mock_redis, fno_gates):
    c = await fno_chain(db_session, mock_redis)
    c.master_fake.fno_order("F-1", "NIFTY26NOV99999CE")
    (rec,) = await c.master_client.get_order_book()
    assert rec.status == "UNSUPPORTED_FNO_INSTRUMENT_UNKNOWN"


@pytest.mark.asyncio
async def test_an_unreadable_f_and_o_list_fails_the_whole_read_it_never_reads_as_empty(db_session, mock_redis, fno_gates):
    c = await fno_chain(db_session, mock_redis)
    c.master_fake.fno_error = True
    with pytest.raises(GrowwError):
        await c.master_client.get_order_book()


# ── end to end: Groww Master F&O -> Alice Child ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("symbol,kind", [(CE, "CE"), (PE, "PE"), (FUT, "FUT")])
async def test_a_groww_f_and_o_order_is_copied_to_the_alice_child_with_alices_own_token(db_session, mock_redis, fno_gates, symbol, kind):
    c = await fno_chain(db_session, mock_redis)
    c.master_fake.fno_order("F-1", symbol, qty=75, price=100)
    await c.poll()
    (req,) = c.placed_on_alice()
    assert req.exchange == "NFO" and req.instrument_token == ALICE_NFO_TOKENS[kind] and req.quantity == 75      # Alice's token, units
    assert req.symbol == symbol or req.symbol                                                                    # the symbol is for display only
    assert c.child_fake.bodies("/v1/order/create") == []                                                        # the Groww Child never sends an F&O order
    assert c.events == ["LIMIT_CREATED"]


@pytest.mark.asyncio
async def test_a_market_style_groww_f_and_o_fill_is_copied_once(db_session, mock_redis, fno_gates):
    c = await fno_chain(db_session, mock_redis)
    c.master_fake.fno_order("F-1", CE, otype="MARKET", price=0, qty=75, filled=75, status="EXECUTED", avg=101.5)
    await c.poll()
    await c.poll()
    assert len(c.placed_on_alice()) == 1 and c.placed_on_alice()[0].instrument_token == "50001"


@pytest.mark.asyncio
async def test_a_modification_of_a_routed_f_and_o_order_reaches_the_alice_child(db_session, mock_redis, fno_gates):
    c = await fno_chain(db_session, mock_redis)
    c.master_fake.fno_order("F-1", CE, price=100)
    await c.poll()
    assert len(c.placed_on_alice()) == 1
    c.master_fake.book["F-1"]["price"] = 101
    await c.poll()
    assert c.alice_client.modify_order.await_count == 1
    call = c.alice_client.modify_order.await_args
    assert Decimal(str(call.kwargs["price"])) == Decimal("101") and call.kwargs["exchange"] == "NFO"


@pytest.mark.asyncio
async def test_a_cancellation_of_a_routed_f_and_o_order_reaches_the_alice_child(db_session, mock_redis, fno_gates):
    c = await fno_chain(db_session, mock_redis)
    c.master_fake.fno_order("F-1", CE)
    await c.poll()
    c.master_fake.book["F-1"]["status"] = "CANCELLED"
    await c.poll()
    assert c.alice_client.cancel_order.await_count == 1


@pytest.mark.asyncio
async def test_cash_orders_still_copy_with_the_f_and_o_gates_open(db_session, mock_redis, fno_gates):
    c = await fno_chain(db_session, mock_redis)
    c.master_fake.app_order("G-1", status="OPEN", price=800)
    await c.poll()
    (req,) = c.placed_on_alice()
    assert req.exchange == "NSE" and req.quantity == 10


# ── every refusal keeps the original, fail-closed skip ───────────────────────────────────────────────────────────────────────

async def skipped_reason(c):
    rows = await copies_for(c.alice)
    return [r.failure_reason for r in rows if r.failure_reason]


@pytest.mark.asyncio
async def test_without_the_pair_listed_the_original_refusal_stands(db_session, mock_redis, fno_gates, monkeypatch):
    monkeypatch.setattr(settings, "CROSS_BROKER_FNO_PAIRS", "")
    c = await fno_chain(db_session, mock_redis)
    c.master_fake.fno_order("F-1", CE)
    await c.poll()
    assert c.placed_on_alice() == [] and await skipped_reason(c) == ["INSTRUMENT_UNMAPPED: CROSS_BROKER_DERIVATIVE_UNMAPPED:groww->alicebluenew:NFO"]


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol,verdict", [(MISSING, "UNMAPPED"), (AMBIGUOUS, "AMBIGUOUS")])
async def test_a_contract_that_does_not_resolve_exactly_in_alices_master_is_never_traded(db_session, mock_redis, fno_gates, symbol, verdict):
    c = await fno_chain(db_session, mock_redis)
    c.master_fake.fno_order("F-1", symbol, qty=75 if symbol != AMBIGUOUS else 35)
    await c.poll()
    (reason,) = await skipped_reason(c)
    assert c.placed_on_alice() == [] and reason.startswith(f"INSTRUMENT_UNMAPPED: CROSS_BROKER_FNO_{verdict}")


@pytest.mark.asyncio
@pytest.mark.parametrize("store_kwargs,verdict", [
    (dict(loaded_at=datetime(2020, 1, 1, tzinfo=timezone.utc)), "STALE"), (dict(nfo=False, loaded_at=datetime.now(timezone.utc)), "CATALOG_NOT_LOADED"),
])
async def test_a_stale_or_unloaded_alice_catalog_blocks_the_copy(db_session, mock_redis, fno_gates, monkeypatch, store_kwargs, verdict):
    monkeypatch.setattr(terms, "store", store_with(**store_kwargs))
    c = await fno_chain(db_session, mock_redis)
    c.master_fake.fno_order("F-1", CE)
    await c.poll()
    (reason,) = await skipped_reason(c)
    assert c.placed_on_alice() == [] and f"CROSS_BROKER_FNO_{verdict}" in reason


# ── the route policy itself ──────────────────────────────────────────────────────────────────────────────────────────────────

KEY = make_contract("CE", "NFO", "NIFTY", "2026-11-26", "23500").key


def test_the_route_policy_is_the_original_answer_unless_the_pair_is_listed(monkeypatch):
    for master, child in (("groww", "alicebluenew"), ("alicebluenew", "groww"), ("zerodha", "alicebluenew"), ("angelone", "groww")):
        decision = derivative_routing.route(master, child, "NFO", KEY)
        assert decision.allowed is False and decision.skip.detail == f"CROSS_BROKER_DERIVATIVE_UNMAPPED:{master}->{child}:NFO"
    assert derivative_routing.route("alicebluenew", "alicebluenew", "NFO", None).allowed is True                  # same broker: unchanged
    assert derivative_routing.route("groww", "alicebluenew", "NSE", None).allowed is True                         # cash is not this policy's business
    assert derivative_routing.route(None, "groww", "NFO", KEY).allowed is True                                    # unknown Master: the pair gate refuses it elsewhere


def test_a_listed_pair_needs_a_readable_contract_and_a_child_with_a_resolver(monkeypatch):
    monkeypatch.setattr(settings, "CROSS_BROKER_FNO_PAIRS", "groww>alicebluenew,groww>angelone,groww>groww")
    monkeypatch.setattr(terms, "store", store_with(loaded_at=datetime.now(timezone.utc)))
    ok = derivative_routing.route("groww", "alicebluenew", "NFO", KEY)
    assert ok.allowed and ok.child_token == "50001" and ok.skip is None
    for key in (None, "", "garbage", "CE|NFO|NIFTY|2026-11-26", "EQ|NSE|SYM|SBIN"):
        d = derivative_routing.route("groww", "alicebluenew", "NFO", key)
        assert d.allowed is False and "CROSS_BROKER_FNO_CONTRACT_UNAVAILABLE" in d.skip.detail
    for child in ("angelone", "groww"):                                   # listed, but no resolver is registered for the Child
        d = derivative_routing.route("groww", child, "NFO", KEY)
        assert d.allowed is (child == "groww") and (child == "groww" or "CHILD_NOT_SUPPORTED" in d.skip.detail)


def test_a_resolver_error_or_a_non_numeric_token_is_never_a_route(monkeypatch):
    monkeypatch.setattr(settings, "CROSS_BROKER_FNO_PAIRS", "groww>alicebluenew")

    def boom(_contract):
        raise RuntimeError("x")

    monkeypatch.setitem(derivative_routing.CHILD_RESOLVERS, "alicebluenew", boom)
    d = derivative_routing.route("groww", "alicebluenew", "NFO", KEY)
    assert d.allowed is False and "CROSS_BROKER_FNO_ROUTE_ERROR" in d.skip.detail

    from app.services.instruments.contracts import ContractResolution, ResolutionVerdict

    monkeypatch.setitem(derivative_routing.CHILD_RESOLVERS, "alicebluenew",
                        lambda c: ContractResolution("alicebluenew", ResolutionVerdict.RESOLVED_UNVERIFIED, "x", c.key, "S", "UNRESOLVED:abc"))
    assert derivative_routing.route("groww", "alicebluenew", "NFO", KEY).allowed is False


def test_a_new_child_broker_is_routable_by_registering_one_resolver(monkeypatch):
    """Open/closed: the planner, the pipeline and the preview are not edited to add a Child broker; one resolver is registered."""
    from app.services.instruments.contracts import ContractResolution, ResolutionVerdict

    monkeypatch.setattr(settings, "CROSS_BROKER_FNO_PAIRS", "groww>angelone")
    assert derivative_routing.route("groww", "angelone", "NFO", KEY).allowed is False
    monkeypatch.setitem(derivative_routing.CHILD_RESOLVERS, "angelone",
                        lambda c: ContractResolution("angelone", ResolutionVerdict.RESOLVED_UNVERIFIED, "x", c.key, "NIFTY26NOV23500CE", "12345"))
    d = derivative_routing.route("groww", "angelone", "NFO", KEY)
    assert d.allowed is True and d.child_token == "12345"


# ── the quantity unit can be INFERRED per order (AUTO), the exchange is tolerant, and one odd row cannot blind the book ──────────

@pytest.mark.parametrize("qty,lot,freeze,expected", [
    (1, 75, 1800, "LOTS"), (24, 75, 1800, "LOTS"), (25, 75, 1800, None), (70, 75, 1800, None), (75, 75, 1800, "UNITS"), (150, 75, 1800, "UNITS"),
    (75, 75, 0, None), (1, 25, 1000, "LOTS"), (25, 25, 1000, None), (50, 25, 1000, "UNITS"), (1, 35, 601, "LOTS"), (35, 35, 601, "UNITS"),
    (0, 75, 1800, None), (-75, 75, 1800, None), (75, 0, 1800, None),
])
def test_the_quantity_unit_is_inferred_only_when_it_is_unambiguous(qty, lot, freeze, expected):
    assert segments.infer_quantity_unit(qty, lot, freeze) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("qty,expected_units", [(1, 75), (2, 150), (75, 75), (150, 150)])
async def test_auto_reads_one_lot_as_lots_and_a_lot_multiple_as_units(db_session, mock_redis, fno_gates, monkeypatch, qty, expected_units):
    monkeypatch.setattr(settings, "GROWW_FNO_QUANTITY_UNIT", "AUTO")
    c = await fno_chain(db_session, mock_redis)
    c.master_fake.fno_order("F-1", CE, qty=qty)
    await c.poll()
    (req,) = c.placed_on_alice()
    assert req.quantity == expected_units and req.instrument_token == "50001"


@pytest.mark.asyncio
async def test_auto_refuses_an_order_whose_unit_is_not_unambiguous(db_session, mock_redis, fno_gates, monkeypatch):
    monkeypatch.setattr(settings, "GROWW_FNO_QUANTITY_UNIT", "AUTO")
    c = await fno_chain(db_session, mock_redis)
    c.master_fake.fno_order("F-1", CE, qty=70)
    (rec,) = await c.master_client.get_order_book()
    assert rec.status == "UNSUPPORTED_FNO_QUANTITY_UNIT_AMBIGUOUS"
    await c.poll()
    assert c.placed_on_alice() == []


@pytest.mark.asyncio
async def test_an_nfo_exchange_row_is_read_like_an_nse_one(db_session, mock_redis, fno_gates):
    c = await fno_chain(db_session, mock_redis)
    c.master_fake.fno_order("F-1", CE, exchange="NFO")
    await c.poll()
    assert len(c.placed_on_alice()) == 1


@pytest.mark.asyncio
async def test_one_malformed_f_and_o_row_is_skipped_and_logged_with_its_fields_the_cash_book_is_still_read(db_session, mock_redis, fno_gates, caplog):
    c = await fno_chain(db_session, mock_redis)
    c.master_fake.app_order("G-1", status="OPEN", price=800)
    c.master_fake.fno_order("F-1", CE, status="SOME_NEW_STATUS")
    with caplog.at_level("WARNING", logger="groww_client"):
        records = await c.master_client.get_order_book()
    assert [r.exchange for r in records] == ["NSE"]                                    # the cash order is there; the odd F&O row is not
    assert any("a row was skipped" in m and "SOME_NEW_STATUS" in m and "NIFTY26NOV23500CE" in m for m in caplog.messages)
    await c.poll()
    assert len(c.placed_on_alice()) == 1 and c.placed_on_alice()[0].exchange == "NSE"   # and it still copies


@pytest.mark.asyncio
async def test_an_unsupported_f_and_o_order_is_logged_once_with_the_raw_values(db_session, mock_redis, fno_gates, caplog):
    c = await fno_chain(db_session, mock_redis)
    c.master_fake.fno_order("LOGONCE-9", CE, product="CNC")                 # a fresh id: the once-per-order memory is process-wide
    with caplog.at_level("WARNING", logger="groww_segments"):
        await c.master_client.get_order_book()
        await c.master_client.get_order_book()
    hits = [m for m in caplog.messages if "PRODUCT_NOT_FNO" in m]
    assert len(hits) == 1 and "'product': 'CNC'" in hits[0] and "'exchange': 'NSE'" in hits[0]


# ── lot-based F&O sizing: "one lot" or "a multiple of the Master's lots", chosen per Child ───────────────────────────────────────

from app.models.accounts import CopySetting, QuantityMode                                    # noqa: E402
from app.services.copy_engine.calculator import QuantityCalculator                          # noqa: E402


def setting(mode, fixed=1):
    return CopySetting(follower_account_id=None, copy_enabled=True, quantity_mode=mode, fixed_qty=fixed)


@pytest.mark.parametrize("mode,fixed,master,mult,lot,expected", [
    (QuantityMode.FIXED, 1, 65, "1", 65, 65),          # one lot, whatever the Master traded
    (QuantityMode.FIXED, 1, 650, "1", 65, 65),
    (QuantityMode.FIXED, 2, 65, "1", 65, 130),         # two lots
    (QuantityMode.FIXED, 0, 65, "1", 65, 0),           # zero lots = skip
    (QuantityMode.MULTIPLIER, 1, 65, "1", 65, 65),     # the Master's own lots
    (QuantityMode.MULTIPLIER, 1, 130, "2", 65, 260),   # twice the Master's lots
    (QuantityMode.MULTIPLIER, 1, 65, "3", 65, 195),
    (QuantityMode.MULTIPLIER, 1, 65, "0.5", 65, 0),    # less than one lot: never rounded up
    (QuantityMode.MULTIPLIER, 1, 130, "1.5", 65, 195),   # exactly 3 lots
])
def test_derivative_sizing_is_in_whole_lots(mode, fixed, master, mult, lot, expected):
    got = QuantityCalculator.calculate_quantity(mode=mode, master_qty=master, master_capital=Decimal("1"), follower_capital=Decimal("1"), lot_size=1,
                                                settings=setting(mode, fixed), multiplier=Decimal(mult), derivative_lot=lot)
    assert got == expected and got % lot == 0


@pytest.mark.parametrize("mode,fixed,master,mult,expected", [
    (QuantityMode.FIXED, 1, 10, "1", 1), (QuantityMode.FIXED, 5, 10, "1", 5), (QuantityMode.MULTIPLIER, 1, 10, "2", 20),
])
def test_cash_sizing_is_exactly_what_it_was(mode, fixed, master, mult, expected):
    for lot in (None, 1):
        assert QuantityCalculator.calculate_quantity(mode=mode, master_qty=master, master_capital=Decimal("1"), follower_capital=Decimal("1"), lot_size=1,
                                                     settings=setting(mode, fixed), multiplier=Decimal(mult), derivative_lot=lot) == expected


async def alice_policy(c, mode, fixed=1):
    async with TestingSessionLocal() as s:
        row = (await s.execute(select(CopySetting).where(CopySetting.follower_account_id == c.alice.id))).scalar_one()
        row.quantity_mode, row.fixed_qty = mode, fixed
        await s.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,fixed,master_units,expected", [
    (QuantityMode.FIXED, 1, 75, 75),        # "one lot": the Child gets one lot even though FIXED 1 used to mean one UNIT (and was rejected)
    (QuantityMode.FIXED, 2, 75, 150),
    (QuantityMode.MULTIPLIER, 1, 150, 150),
])
async def test_the_alice_child_is_sized_in_lots_for_a_groww_f_and_o_order(db_session, mock_redis, fno_gates, mode, fixed, master_units, expected):
    c = await fno_chain(db_session, mock_redis)
    await alice_policy(c, mode, fixed)
    c.master_fake.fno_order("F-1", CE, qty=master_units)
    await c.poll()
    (req,) = c.placed_on_alice()
    assert req.quantity == expected and req.quantity % 75 == 0 and req.instrument_token == "50001"


@pytest.mark.asyncio
async def test_less_than_one_lot_is_skipped_never_rounded_up_and_cash_is_still_one_unit(db_session, mock_redis, fno_gates):
    c = await fno_chain(db_session, mock_redis)
    await alice_policy(c, QuantityMode.FIXED, 1)
    async with TestingSessionLocal() as s:                                   # a fractional multiplier on the strategy member
        from app.models.strategies import StrategyMember
        m = (await s.execute(select(StrategyMember).where(StrategyMember.account_id == c.alice.id))).scalar_one()
        m.multiplier = Decimal("0.5")
        await s.commit()
    await alice_policy(c, QuantityMode.MULTIPLIER)
    c.master_fake.fno_order("F-1", CE, qty=75)
    await c.poll()
    assert c.placed_on_alice() == []                                         # 0.5 lot is below one lot: ZERO_LOT_RESULT
    await alice_policy(c, QuantityMode.FIXED, 1)
    c.master_fake.app_order("G-1", status="OPEN", price=800)
    await c.poll()
    (req,) = c.placed_on_alice()
    assert req.exchange == "NSE" and req.quantity == 1                       # cash: FIXED 1 is still one share
