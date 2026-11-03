"""
Phase B: cross-broker F&O CHILD execution for Groww and Zerodha, on one architecture. OFFLINE: fakes only, nothing is sent to a broker.

  * capability matrix  - what each broker may do with an F&O order and how far it is evidenced; nothing but Alice Blue is VERIFIED_LIVE;
  * gates              - each broker's F&O Child gate is closed by default and opening one never opens another;
  * validation chain   - whole lots, freeze quantity, tick size: invalid orders are refused, there is NO lot cap (Alice Blue has none);
  * Groww Child        - an Alice Master's F&O order is placed at Groww with Groww's own symbol on segment FNO, then modified and cancelled;
  * Zerodha Child      - the same, through the same route policy and the same chain.

None of this is live evidence. The existing Copy Quantity settings (FIXED / MULTIPLIER) are unchanged: they size the order and this chain
validates the result.
"""
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.config import Settings, settings
from app.models.accounts import CopySetting, QuantityMode
from app.models.audit import SystemEvent
from app.models.trades import CopyTrade, Order, OrderStatus
from app.services.alice_blue.types import OrderRequest, OrderResultCategory, OrderSide, OrderType, ProductType
from app.services.brokers import fno_capabilities
from app.services.brokers.fno_capabilities import FNO_CHILD_CAPABILITIES, child_gate_open
from app.services.copy_engine import derivative_routing, fno_validation
from app.services.copy_engine.derivative_routing import ChildTerms
from app.services.groww import instruments as gi
from app.services.instruments import terms
from app.services.zerodha import instruments as zi
from conftest import TestingSessionLocal
from groww_helpers import make_client as make_groww_client
from test_alice_cross_broker_resolution import store_with
from test_groww_equity_lifecycle import CHILD_IP, groww_child, new_pipeline
from test_groww_fno_master import CE, FnoGroww, fno_store
from test_order_handling_matrix import _event
from test_zerodha_equity_lifecycle import zerodha_child, zerodha_enabled  # noqa: F401
from test_zerodha_equity_flow import StatefulKite
from zerodha_helpers import make_client as make_zerodha_client, make_store, seed_session

NIFTY_CE_TOKEN = "50001"
GROWW_SYMBOL = ZERODHA_SYMBOL = "NIFTY26NOV23500CE"


# ── the capability matrix ────────────────────────────────────────────────────────────────────────────────────────────────────

def test_only_alice_blue_is_verified_live_and_every_other_broker_is_gated_and_honest_about_it():
    alice = FNO_CHILD_CAPABILITIES["alicebluenew"]
    assert {alice.place, alice.modify, alice.cancel, alice.reconcile} == {"VERIFIED_LIVE"}
    for broker in ("groww", "zerodha"):
        cap = FNO_CHILD_CAPABILITIES[broker]
        assert {cap.place, cap.modify, cap.cancel, cap.reconcile} == {"CODE_ONLY"}, broker         # built and tested offline, never seen live
        assert cap.products == {"MIS", "NRML"} and cap.order_types == {"MARKET", "LIMIT", "SL", "SLM"}


def test_every_f_and_o_child_gate_is_closed_by_default_except_the_production_proven_one():
    s = Settings(_env_file=None)
    assert (s.GROWW_FNO_CHILD_ENABLED, s.ZERODHA_FNO_CHILD_ENABLED) == (False, False)
    assert not hasattr(s, "FNO_CHILD_MAX_LOTS")                                          # no new lot cap exists anywhere
    assert child_gate_open("alicebluenew") and not child_gate_open("groww") and not child_gate_open("zerodha")
    assert not child_gate_open("angelone") and not child_gate_open("dhan")
    assert child_gate_open(None)                                                        # a NULL stored broker is Alice Blue


def test_enabling_one_brokers_f_and_o_gate_never_enables_another(monkeypatch):
    monkeypatch.setattr(settings, "GROWW_FNO_CHILD_ENABLED", True)
    assert child_gate_open("groww") and not child_gate_open("zerodha") and not child_gate_open("angelone")
    monkeypatch.setattr(settings, "GROWW_FNO_CHILD_ENABLED", False)
    monkeypatch.setattr(settings, "ZERODHA_FNO_CHILD_ENABLED", True)
    assert child_gate_open("zerodha") and not child_gate_open("groww")
    # the other F&O switches are separate too
    monkeypatch.setattr(settings, "ZERODHA_FNO_CHILD_ENABLED", False)
    monkeypatch.setattr(settings, "GROWW_FNO_MASTER_ENABLED", True)
    monkeypatch.setattr(settings, "ZERODHA_FNO_QUANTITY_UNIT", "UNITS")
    assert not child_gate_open("groww") and not child_gate_open("zerodha")


@pytest.mark.parametrize("child,flag", [("groww", "GROWW_FNO_CHILD_ENABLED"), ("zerodha", "ZERODHA_FNO_CHILD_ENABLED")])
def test_a_closed_child_gate_refuses_the_route_even_for_a_listed_pair_with_an_exact_contract(monkeypatch, child, flag):
    from app.services.instruments.contracts import make_contract

    monkeypatch.setattr(terms, "store", store_with(loaded_at=datetime.now(timezone.utc)))
    monkeypatch.setattr(gi, "store", fno_store())
    monkeypatch.setattr(zi, "store", make_store())
    monkeypatch.setattr(settings, "CROSS_BROKER_FNO_PAIRS", f"alicebluenew>{child}")
    key = make_contract("CE", "NFO", "NIFTY", "2026-11-26", "23500").key
    closed = derivative_routing.route("alicebluenew", child, "NFO", key)
    assert closed.allowed is False and f"CROSS_BROKER_FNO_CHILD_GATE_CLOSED:{child}" in closed.skip.detail
    monkeypatch.setattr(settings, flag, True)
    opened = derivative_routing.route("alicebluenew", child, "NFO", key)
    assert opened.allowed is True and opened.child_symbol == GROWW_SYMBOL


# ── the validation chain ─────────────────────────────────────────────────────────────────────────────────────────────────────

def facts(qty, *, lot=65, freeze=None, tick=None, price=None, terms_=True):
    return fno_validation.FnoOrderFacts("groww", qty, ChildTerms(lot, freeze, tick) if terms_ else None, price)


def test_the_default_chain_allows_any_whole_number_of_lots_and_nothing_is_rounded():
    for lots in (1, 2, 5, 20):
        assert fno_validation.run_chain(facts(65 * lots)) is None                          # no cap: the Copy Quantity settings decide how many lots
    assert fno_validation.run_chain(facts(64)) == "FNO_QUANTITY_NOT_LOT_MULTIPLE"
    assert fno_validation.run_chain(facts(66)) == "FNO_QUANTITY_NOT_LOT_MULTIPLE"
    assert fno_validation.run_chain(facts(0)) == "FNO_QUANTITY_NOT_LOT_MULTIPLE"
    assert fno_validation.run_chain(facts(65, lot=None)) == "FNO_LOT_SIZE_UNKNOWN"      # a lot count cannot be established: refused, not assumed
    assert fno_validation.run_chain(facts(65, terms_=False)) == "FNO_LOT_SIZE_UNKNOWN"


def test_there_is_no_lot_cap_in_the_chain_or_in_the_settings():
    assert [c.name for c in fno_validation.DEFAULT_CHAIN] == ["lot_multiple", "freeze_quantity", "tick_size"]
    assert not hasattr(settings, "FNO_CHILD_MAX_LOTS")


def test_the_freeze_quantity_is_enforced_only_when_the_catalog_states_it_and_never_by_splitting(monkeypatch):
    assert fno_validation.run_chain(facts(65 * 28, freeze=1801)) == "FNO_FREEZE_QUANTITY_EXCEEDED"
    assert fno_validation.run_chain(facts(65 * 27, freeze=1801)) is None
    assert fno_validation.run_chain(facts(65 * 28, freeze=None)) is None                # unstated freeze quantity is not assumed (the cap bounds it)


@pytest.mark.parametrize("price,ok", [("2.35", True), ("2.30", True), ("2.33", False), ("0", True)])
def test_a_stated_limit_price_must_sit_on_the_tick_size(price, ok):
    got = fno_validation.run_chain(facts(65, tick=Decimal("0.05"), price=Decimal(price)))
    assert (got is None) is ok and (ok or got == "FNO_PRICE_NOT_ON_TICK")
    assert fno_validation.run_chain(facts(65, tick=None, price=Decimal("2.33"))) is None            # an unstated tick is not assumed
    assert fno_validation.run_chain(facts(65, tick=Decimal("0.05"), price=None)) is None            # MARKET: no price yet


def test_the_chain_stops_at_the_first_refusal_and_an_error_in_a_check_is_a_refusal():
    class Boom:
        name = "boom"

        def check(self, f):
            raise RuntimeError("x")

    assert fno_validation.run_chain(facts(65), (Boom(),)) == "FNO_CHECK_ERROR"
    assert fno_validation.run_chain(facts(64, freeze=10), fno_validation.DEFAULT_CHAIN) == "FNO_QUANTITY_NOT_LOT_MULTIPLE"      # lot check runs first


# ── shared helpers for the two Children ─────────────────────────────────────────────────────────────────────────────────────

def alice_event(master, oid, *, qty=75, order_type="LIMIT", side="BUY", product="LONGTERM", price="2.35", trigger="0"):
    return _event(master, oid, side=side, order_type=order_type, product=product, exchange="NFO", symbol="NIFTY", qty=qty, price=price, trigger=trigger,
                  token=NIFTY_CE_TOKEN)


async def set_policy(child, mode, fixed=1):
    async with TestingSessionLocal() as s:
        row = (await s.execute(select(CopySetting).where(CopySetting.follower_account_id == child.id))).scalar_one()
        row.quantity_mode, row.fixed_qty = mode, fixed
        await s.commit()


async def skipped(child):
    async with TestingSessionLocal() as s:
        rows = (await s.execute(select(CopyTrade).where(CopyTrade.follower_account_id == child.id))).scalars().all()
    return [r.failure_reason for r in rows if r.failure_reason]


async def child_orders(child):
    async with TestingSessionLocal() as s:
        return (await s.execute(select(Order).where(Order.account_id == child.id))).scalars().all()


async def events():
    async with TestingSessionLocal() as s:
        return [e.event_type for e in (await s.execute(select(SystemEvent))).scalars().all()]


# ── the Groww Child ─────────────────────────────────────────────────────────────────────────────────────────────────────────

class GrowwFnoChild(FnoGroww):
    """A Groww account that takes F&O orders from Jettone: a created order lands in the FNO list, as Groww's does."""

    async def __call__(self, request):
        response = await super().__call__(request)
        if request.url.path == "/v1/order/create" and response.status_code == 200:
            self.book[f"GRW-{self.n}"]["seg"] = json.loads(request.content)["segment"]
        return response


@pytest.fixture
def groww_open(monkeypatch):
    for name in ("GROWW_ENABLED", "GROWW_CHILD_TRADING_ENABLED", "ALICE_GROWW_CANARY_ENABLED", "GROWW_FNO_CHILD_ENABLED"):
        monkeypatch.setattr(settings, name, True)
    monkeypatch.setattr(settings, "SELL_POSITION_SAFETY_BYPASS", False)
    monkeypatch.setattr(settings, "GROWW_FNO_QUANTITY_UNIT", "UNITS")
    monkeypatch.setattr(settings, "CROSS_BROKER_FNO_PAIRS", "alicebluenew>groww")
    monkeypatch.setattr(terms, "store", store_with(loaded_at=datetime.now(timezone.utc)))
    store = fno_store()
    monkeypatch.setattr(gi, "store", store)
    return store


async def groww_send(db_session, mock_redis, store, event_for, *, mode=QuantityMode.MULTIPLIER, fixed=1, fake=None, pipeline=None, child=None):
    if child is None:
        master, child = await groww_child(db_session)
        await set_policy(child, mode, fixed)
    else:
        master = None
    fake = fake or GrowwFnoChild()
    client = make_groww_client(mock_redis, fake, account_id=str(child.id), static_ip=CHILD_IP, store=store)
    await client.login()
    pipeline = pipeline or new_pipeline(mock_redis)
    return master, child, fake, client, pipeline


@pytest.mark.asyncio
async def test_an_alice_f_and_o_order_is_placed_at_groww_with_groww_symbol_segment_fno_and_the_carry_product(db_session, mock_redis, groww_open):
    master, child, fake, client, pipeline = await groww_send(db_session, mock_redis, groww_open, None)
    res = await pipeline.execute_replication(event=alice_event(master, "G-1", qty=75), clients={str(child.id): client})
    assert res["results"][str(child.id)]["status"] == "SUCCESS", res
    (body,) = fake.bodies("/v1/order/create")
    assert (body["trading_symbol"], body["exchange"], body["segment"], body["product"], body["order_type"], body["transaction_type"], body["quantity"]) == \
           (GROWW_SYMBOL, "NSE", "FNO", "NRML", "LIMIT", "BUY", 75)
    assert body["price"] == 2.35 and body["order_reference_id"].startswith("JT") and NIFTY_CE_TOKEN not in json.dumps(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("product,expected", [("LONGTERM", "NRML"), ("INTRADAY", "MIS")])
async def test_the_groww_products_follow_the_masters_carry_or_intraday(db_session, mock_redis, groww_open, product, expected):
    master, child, fake, client, pipeline = await groww_send(db_session, mock_redis, groww_open, None)
    await pipeline.execute_replication(event=alice_event(master, "G-2", product=product), clients={str(child.id): client})
    assert fake.bodies("/v1/order/create")[0]["product"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,fixed,master_qty,sent", [(QuantityMode.FIXED, 75, 75, 75), (QuantityMode.FIXED, 75, 750, 75), (QuantityMode.MULTIPLIER, 1, 75, 75)])
async def test_the_existing_copy_quantity_settings_size_the_groww_order_in_units_exactly_as_for_cash(db_session, mock_redis, groww_open, mode, fixed, master_qty, sent):
    master, child, fake, client, pipeline = await groww_send(db_session, mock_redis, groww_open, None, mode=mode, fixed=fixed)
    await pipeline.execute_replication(event=alice_event(master, "G-3", qty=master_qty), clients={str(child.id): client})
    assert [b["quantity"] for b in fake.bodies("/v1/order/create")] == [sent]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,fixed,master_qty", [(QuantityMode.FIXED, 150, 75), (QuantityMode.MULTIPLIER, 1, 150)])
async def test_a_larger_order_is_sized_by_the_copy_quantity_settings_and_sent_to_groww_like_it_is_to_alice(db_session, mock_redis, groww_open, mode, fixed, master_qty):
    master, child, fake, client, pipeline = await groww_send(db_session, mock_redis, groww_open, None, mode=mode, fixed=fixed)
    await pipeline.execute_replication(event=alice_event(master, "G-4", qty=master_qty), clients={str(child.id): client})
    assert [b["quantity"] for b in fake.bodies("/v1/order/create")] == [150]                # 2 lots: FIXED 150 (units) or MULTIPLIER on a 2-lot Master order


@pytest.mark.asyncio
async def test_the_groww_adapter_still_refuses_what_is_invalid_not_what_is_large(db_session, mock_redis, groww_open):
    client = make_groww_client(mock_redis, GrowwFnoChild(), account_id="acc-g", static_ip=CHILD_IP, store=groww_open)
    await client.login()

    def request(qty):
        return OrderRequest(symbol=GROWW_SYMBOL, exchange="NFO", side=OrderSide.BUY, quantity=qty, product_type=ProductType.NRML, order_type=OrderType.LIMIT,
                            price=Decimal("2.35"), client_order_id=f"JT_X_{qty}")

    assert (await client.place_order(request(150))).category == OrderResultCategory.SUCCESS             # 2 lots: no cap
    assert "whole number of lots" in (await client.place_order(request(74))).message                     # not a lot multiple: refused, never rounded
    assert "freeze quantity" in (await client.place_order(request(75 * 25))).message                      # above the exchange freeze quantity (1800): refused, never split


@pytest.mark.asyncio
async def test_the_groww_adapter_refuses_without_its_gate_or_a_configured_unit_or_with_a_foreign_symbol(db_session, mock_redis, groww_open, monkeypatch):
    client = make_groww_client(mock_redis, GrowwFnoChild(), account_id="acc-g", static_ip=CHILD_IP, store=groww_open)
    await client.login()

    def request(symbol=GROWW_SYMBOL, qty=75):
        return OrderRequest(symbol=symbol, exchange="NFO", side=OrderSide.BUY, quantity=qty, product_type=ProductType.NRML, order_type=OrderType.LIMIT,
                            price=Decimal("2.35"), client_order_id="JT_X_2")

    monkeypatch.setattr(settings, "GROWW_FNO_QUANTITY_UNIT", "")
    assert "not configured" in (await client.place_order(request())).message
    monkeypatch.setattr(settings, "GROWW_FNO_QUANTITY_UNIT", "UNITS")
    assert (await client.place_order(request("NIFTY"))).category == OrderResultCategory.REJECTED                       # Alice's bare symbol is not Groww's
    monkeypatch.setattr(settings, "GROWW_FNO_CHILD_ENABLED", False)
    assert "not enabled" in (await client.place_order(request())).message


@pytest.mark.asyncio
async def test_an_explicit_lots_unit_sends_the_lot_count_to_groww(db_session, mock_redis, groww_open, monkeypatch):
    monkeypatch.setattr(settings, "GROWW_FNO_QUANTITY_UNIT", "LOTS")
    master, child, fake, client, pipeline = await groww_send(db_session, mock_redis, groww_open, None)
    await pipeline.execute_replication(event=alice_event(master, "G-5", qty=75), clients={str(child.id): client})
    assert [b["quantity"] for b in fake.bodies("/v1/order/create")] == [1]


@pytest.mark.asyncio
async def test_a_contract_groww_does_not_hold_exactly_is_never_traded_and_the_pair_must_be_listed(db_session, mock_redis, groww_open, monkeypatch):
    master, child, fake, client, pipeline = await groww_send(db_session, mock_redis, groww_open, None)
    import dataclasses

    await pipeline.execute_replication(event=dataclasses.replace(alice_event(master, "G-6"), instrument_id="50004"),
                                       clients={str(child.id): client})                              # NIFTY 23600 CE: in Alice's master, not in Groww's
    assert fake.bodies("/v1/order/create") == [] and any("CROSS_BROKER_FNO_UNMAPPED" in r for r in await skipped(child))
    monkeypatch.setattr(settings, "CROSS_BROKER_FNO_PAIRS", "")
    await pipeline.execute_replication(event=alice_event(master, "G-7"), clients={str(child.id): client})
    assert fake.bodies("/v1/order/create") == [] and any("CROSS_BROKER_DERIVATIVE_UNMAPPED" in r for r in await skipped(child))


@pytest.mark.asyncio
async def test_the_same_event_twice_creates_one_groww_order(db_session, mock_redis, groww_open):
    master, child, fake, client, pipeline = await groww_send(db_session, mock_redis, groww_open, None)
    event = alice_event(master, "G-8")
    await pipeline.execute_replication(event=event, clients={str(child.id): client})
    await new_pipeline(mock_redis).execute_replication(event=event, clients={str(child.id): client})     # even from a fresh process
    assert len(fake.bodies("/v1/order/create")) == 1 and len(fake.book) == 1


@pytest.mark.asyncio
async def test_a_price_change_and_a_cancel_reach_the_groww_order_on_segment_fno(db_session, mock_redis, groww_open):
    import dataclasses

    master, child, fake, client, pipeline = await groww_send(db_session, mock_redis, groww_open, None)
    created = alice_event(master, "G-9")
    await pipeline.execute_replication(event=created, clients={str(child.id): client})
    await pipeline.execute_replication(event=dataclasses.replace(created, event_type="ORDER_MODIFIED", price=Decimal("2.40"), limit_price=Decimal("2.40")),
                                       clients={str(child.id): client})
    (mod,) = fake.bodies("/v1/order/modify")
    assert (mod["segment"], mod["price"], mod["quantity"], mod["order_type"]) == ("FNO", 2.4, 75, "LIMIT")
    await pipeline.execute_replication(event=dataclasses.replace(created, event_type="ORDER_CANCELLED", status="CANCELLED"), clients={str(child.id): client})
    (cancel,) = fake.bodies("/v1/order/cancel")
    assert cancel["segment"] == "FNO" and fake.book[fake.only]["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_a_part_filled_groww_f_and_o_order_is_not_modified_but_the_rest_can_be_cancelled(db_session, mock_redis, groww_open):
    import dataclasses

    master, child, fake, client, pipeline = await groww_send(db_session, mock_redis, groww_open, None)
    created = alice_event(master, "G-10")
    await pipeline.execute_replication(event=created, clients={str(child.id): client})
    fake.book[fake.only]["filled"] = 10
    await pipeline.execute_replication(event=dataclasses.replace(created, event_type="ORDER_MODIFIED", price=Decimal("2.40"), limit_price=Decimal("2.40")),
                                       clients={str(child.id): client})
    assert fake.bodies("/v1/order/modify") == [] and "CHILD_MODIFICATION_FAILED" in await events()
    await pipeline.execute_replication(event=dataclasses.replace(created, event_type="ORDER_CANCELLED", status="CANCELLED"), clients={str(child.id): client})
    assert len(fake.bodies("/v1/order/cancel")) == 1


@pytest.mark.asyncio
async def test_the_groww_child_fill_sync_reads_its_own_f_and_o_order_book(db_session, mock_redis, groww_open):
    master, child, fake, client, pipeline = await groww_send(db_session, mock_redis, groww_open, None)
    await pipeline.execute_replication(event=alice_event(master, "G-11"), clients={str(child.id): client})
    fake.fill(fake.only, 75)
    (record,) = [r for r in await client.get_order_book() if r.exchange == "NFO"]
    assert (record.status, record.filled_quantity, record.quantity, record.symbol) == ("COMPLETE", 75, 75, GROWW_SYMBOL)
    assert {r.url.params.get("segment") for r in fake.requests if r.url.path == "/v1/order/list"} == {"CASH", "FNO"}
    assert (await client.get_order(fake.only)).status == "COMPLETE"


@pytest.mark.asyncio
async def test_cash_equity_to_groww_is_unchanged_with_every_f_and_o_gate_open(db_session, mock_redis, groww_open):
    from test_groww_equity_lifecycle import holds, master_event

    master, child, fake, client, pipeline = await groww_send(db_session, mock_redis, groww_open, None)
    fake.positions_response = holds("NSE", "MIS")
    res = await pipeline.execute_replication(event=master_event(master, "C-1", side="BUY", order_type="LIMIT", product="INTRADAY", exchange="NSE"),
                                             clients={str(child.id): client})
    (body,) = fake.bodies("/v1/order/create")
    assert res["results"][str(child.id)]["status"] == "SUCCESS" and (body["segment"], body["exchange"], body["trading_symbol"]) == ("CASH", "NSE", "SBIN")


@pytest.mark.asyncio
async def test_a_closed_groww_gate_blocks_the_copy_while_zerodha_stays_independent(db_session, mock_redis, groww_open, monkeypatch):
    monkeypatch.setattr(settings, "GROWW_FNO_CHILD_ENABLED", False)
    master, child, fake, client, pipeline = await groww_send(db_session, mock_redis, groww_open, None)
    await pipeline.execute_replication(event=alice_event(master, "G-12"), clients={str(child.id): client})
    assert fake.bodies("/v1/order/create") == [] and any("CHILD_GATE_CLOSED:groww" in r for r in await skipped(child))
    assert not child_gate_open("zerodha")


# ── the Zerodha Child, through the same route policy and chain ──────────────────────────────────────────────────────────────

@pytest.fixture
def zerodha_open(zerodha_enabled, monkeypatch):
    monkeypatch.setattr(terms, "store", store_with(loaded_at=datetime.now(timezone.utc)))
    store = make_store()
    monkeypatch.setattr(zi, "store", store)
    monkeypatch.setattr(settings, "CROSS_BROKER_FNO_PAIRS", "alicebluenew>zerodha")
    monkeypatch.setattr(settings, "ZERODHA_FNO_CHILD_ENABLED", True)
    return store


async def zerodha_setup(db_session, mock_redis, store, mode=QuantityMode.MULTIPLIER, fixed=1):
    master, child = await zerodha_child(db_session)
    await set_policy(child, mode, fixed)
    fake = StatefulKite()
    client = make_zerodha_client(mock_redis, fake, account_id=str(child.id), store=store)
    await seed_session(mock_redis, account_id=str(child.id))
    return master, child, fake, client, new_pipeline(mock_redis)


@pytest.mark.asyncio
async def test_the_zerodha_child_is_sized_by_the_copy_quantity_settings_with_no_extra_cap(db_session, mock_redis, zerodha_open):
    master, child, fake, client, pipeline = await zerodha_setup(db_session, mock_redis, zerodha_open)
    await pipeline.execute_replication(event=alice_event(master, "Z-1", qty=75), clients={str(child.id): client})
    assert [f["quantity"] for f in fake.forms("POST")] == ["75"]
    master2, child2, fake2, client2, pipeline2 = await zerodha_setup(db_session, mock_redis, zerodha_open, QuantityMode.FIXED, 150)
    await pipeline2.execute_replication(event=alice_event(master2, "Z-2", qty=75), clients={str(child2.id): client2})
    assert [f["quantity"] for f in fake2.forms("POST")] == ["150"]


@pytest.mark.asyncio
async def test_a_closed_zerodha_gate_blocks_the_copy_even_though_groww_s_gate_is_open(db_session, mock_redis, zerodha_open, monkeypatch):
    monkeypatch.setattr(settings, "ZERODHA_FNO_CHILD_ENABLED", False)
    monkeypatch.setattr(settings, "GROWW_FNO_CHILD_ENABLED", True)
    master, child, fake, client, pipeline = await zerodha_setup(db_session, mock_redis, zerodha_open)
    await pipeline.execute_replication(event=alice_event(master, "Z-3"), clients={str(child.id): client})
    assert fake.forms("POST") == [] and any("CHILD_GATE_CLOSED:zerodha" in r for r in await skipped(child))


@pytest.mark.asyncio
async def test_a_price_change_and_a_cancel_reach_the_zerodha_order(db_session, mock_redis, zerodha_open):
    import dataclasses

    master, child, fake, client, pipeline = await zerodha_setup(db_session, mock_redis, zerodha_open)
    created = alice_event(master, "Z-4")
    await pipeline.execute_replication(event=created, clients={str(child.id): client})
    await pipeline.execute_replication(event=dataclasses.replace(created, event_type="ORDER_MODIFIED", price=Decimal("2.40"), limit_price=Decimal("2.40")),
                                       clients={str(child.id): client})
    (mod,) = fake.forms("PUT")
    assert mod["price"] == "2.4" and mod["quantity"] == "75" and fake.book[fake.only]["price"] == Decimal("2.4")
    await pipeline.execute_replication(event=dataclasses.replace(created, event_type="ORDER_CANCELLED", status="CANCELLED"), clients={str(child.id): client})
    assert len(fake.forms("DELETE")) == 1 and fake.book[fake.only]["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_a_part_filled_zerodha_f_and_o_order_is_not_modified(db_session, mock_redis, zerodha_open):
    import dataclasses

    master, child, fake, client, pipeline = await zerodha_setup(db_session, mock_redis, zerodha_open)
    created = alice_event(master, "Z-5")
    await pipeline.execute_replication(event=created, clients={str(child.id): client})
    fake.fill(fake.only, 10)
    await pipeline.execute_replication(event=dataclasses.replace(created, event_type="ORDER_MODIFIED", price=Decimal("2.40"), limit_price=Decimal("2.40")),
                                       clients={str(child.id): client})
    assert fake.forms("PUT") == [] and "CHILD_MODIFICATION_FAILED" in await events()


@pytest.mark.asyncio
async def test_the_same_event_twice_creates_one_zerodha_order(db_session, mock_redis, zerodha_open):
    master, child, fake, client, pipeline = await zerodha_setup(db_session, mock_redis, zerodha_open)
    event = alice_event(master, "Z-6")
    await pipeline.execute_replication(event=event, clients={str(child.id): client})
    await new_pipeline(mock_redis).execute_replication(event=event, clients={str(child.id): client})
    assert len(fake.forms("POST")) == 1


# ── every order type goes through the same chain (MARKET and SL take different pipeline paths from LIMIT) ───────────────────────

ORDER_TYPES = [("LIMIT", "0"), ("MARKET", "0"), ("SL", "2.30"), ("SLM", "2.30")]


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type,trigger", ORDER_TYPES)
async def test_two_lots_of_every_order_type_reach_the_groww_child(db_session, mock_redis, groww_open, order_type, trigger):
    master, child, fake, client, pipeline = await groww_send(db_session, mock_redis, groww_open, None)
    res = await pipeline.execute_replication(event=alice_event(master, f"GT-{order_type}-2", qty=150, order_type=order_type, trigger=trigger),
                                             clients={str(child.id): client})
    assert res["results"][str(child.id)]["status"] == "SUCCESS" and [b["quantity"] for b in fake.bodies("/v1/order/create")] == [150]


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type,trigger", ORDER_TYPES)
async def test_one_lot_of_every_order_type_reaches_groww_and_zerodha(db_session, mock_redis, groww_open, order_type, trigger):
    master, child, fake, client, pipeline = await groww_send(db_session, mock_redis, groww_open, None)
    res = await pipeline.execute_replication(event=alice_event(master, f"GT-{order_type}-1", qty=75, order_type=order_type, trigger=trigger),
                                             clients={str(child.id): client})
    (body,) = fake.bodies("/v1/order/create")
    assert res["results"][str(child.id)]["status"] == "SUCCESS" and body["segment"] == "FNO" and body["quantity"] == 75
    assert body["order_type"] == {"LIMIT": "LIMIT", "MARKET": "LIMIT", "SL": "SL", "SLM": "SL_M"}[order_type]       # a MARKET copy is a marketable LIMIT


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type,trigger", ORDER_TYPES)
async def test_two_lots_of_every_order_type_reach_the_zerodha_child(db_session, mock_redis, zerodha_open, order_type, trigger):
    master, child, fake, client, pipeline = await zerodha_setup(db_session, mock_redis, zerodha_open)
    res = await pipeline.execute_replication(event=alice_event(master, f"ZT-{order_type}-2", qty=150, order_type=order_type, trigger=trigger),
                                             clients={str(child.id): client})
    assert res["results"][str(child.id)]["status"] == "SUCCESS" and [f["quantity"] for f in fake.forms("POST")] == ["150"]

# ── Copy Quantity is never adjusted: an invalid F&O quantity is refused with its reason ─────────────────────────────────────────

async def set_member_multiplier(child, value):
    from app.models.strategies import StrategyMember
    async with TestingSessionLocal() as s:
        m = (await s.execute(select(StrategyMember).where(StrategyMember.account_id == child.id))).scalar_one()
        m.multiplier = Decimal(value)
        await s.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("fixed", [1, 74, 76, 100])
async def test_a_fixed_quantity_that_is_not_a_whole_number_of_lots_is_refused_never_rounded_for_groww(db_session, mock_redis, groww_open, fixed):
    master, child, fake, client, pipeline = await groww_send(db_session, mock_redis, groww_open, None, mode=QuantityMode.FIXED, fixed=fixed)
    await pipeline.execute_replication(event=alice_event(master, f"FX-{fixed}", qty=75), clients={str(child.id): client})
    assert fake.bodies("/v1/order/create") == [] and any("FNO_QUANTITY_NOT_LOT_MULTIPLE" in r for r in await skipped(child))


@pytest.mark.asyncio
@pytest.mark.parametrize("mult,master_qty", [("0.5", 75), ("0.75", 150), ("1.5", 75), ("1.01", 150)])
async def test_a_multiplier_that_gives_a_fractional_lot_is_refused_never_floored_for_groww(db_session, mock_redis, groww_open, mult, master_qty):
    master, child, fake, client, pipeline = await groww_send(db_session, mock_redis, groww_open, None, mode=QuantityMode.MULTIPLIER)
    await set_member_multiplier(child, mult)
    await pipeline.execute_replication(event=alice_event(master, f"MX-{mult}", qty=master_qty), clients={str(child.id): client})
    assert fake.bodies("/v1/order/create") == [] and any("FNO_QUANTITY_NOT_LOT_MULTIPLE" in r for r in await skipped(child))


@pytest.mark.asyncio
async def test_a_multiplier_that_gives_whole_lots_is_sent_exactly(db_session, mock_redis, groww_open):
    master, child, fake, client, pipeline = await groww_send(db_session, mock_redis, groww_open, None, mode=QuantityMode.MULTIPLIER)
    await set_member_multiplier(child, "1.5")
    await pipeline.execute_replication(event=alice_event(master, "MX-ok", qty=150), clients={str(child.id): client})
    assert [b["quantity"] for b in fake.bodies("/v1/order/create")] == [225]                  # 2 lots x 1.5 = 3 lots


@pytest.mark.asyncio
@pytest.mark.parametrize("fixed", [1, 74])
async def test_a_fixed_quantity_that_is_not_a_whole_number_of_lots_is_refused_for_zerodha(db_session, mock_redis, zerodha_open, fixed):
    master, child, fake, client, pipeline = await zerodha_setup(db_session, mock_redis, zerodha_open, QuantityMode.FIXED, fixed)
    await pipeline.execute_replication(event=alice_event(master, f"ZFX-{fixed}", qty=75), clients={str(child.id): client})
    assert fake.forms("POST") == [] and any("FNO_QUANTITY_NOT_LOT_MULTIPLE" in r for r in await skipped(child))


@pytest.mark.asyncio
async def test_a_fractional_lot_multiplier_is_refused_for_zerodha(db_session, mock_redis, zerodha_open):
    master, child, fake, client, pipeline = await zerodha_setup(db_session, mock_redis, zerodha_open)
    await set_member_multiplier(child, "0.75")
    await pipeline.execute_replication(event=alice_event(master, "ZMX-1", qty=150), clients={str(child.id): client})
    assert fake.forms("POST") == [] and any("FNO_QUANTITY_NOT_LOT_MULTIPLE" in r for r in await skipped(child))
