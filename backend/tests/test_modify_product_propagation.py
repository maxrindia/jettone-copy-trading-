"""
Modify-order product propagation. A Child order's persisted product is the Master event's product string. All three pipeline paths
that modify a Child order (master modification, pending-SL modification, SL-trigger conversion) now pass the broker product derived
from it, so a client that needs the product (Angel One) gets the order's own, and one that does not (Alice Blue, Groww) is unaffected.
Missing or unknown products are passed through as None, never defaulted: the client then refuses before sending. Offline only:
mock clients and fake transports, no broker, Redis, network or production access. Cross-broker pairs stay blocked (R5 unchanged).
"""
import ast
import json
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.accounts import AccountType
from app.services.alice_blue.types import OrderResult, OrderResultCategory, OrderSide as BrokerOrderSide, OrderType, ProductType
from app.services.brokers import pairs
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import (
    CopyEnginePipeline, broker_product_for_modify, compute_marketable_limit_price,
)
from app.services.copy_engine.validator import EventValidator
from strategy_helpers import SAMPLE_MASTER_ACCOUNT_ID, link_strategy
from angel_helpers import FakeAngel
from angel_helpers import make_client as make_angel_client
from test_adapter_conformance import AliceHarness, GrowwHarness
from test_marketable_limit_strategy import _funds, _limit_event, _market_event, _success_result
from test_modification_after_master_switch import _account, _child_order, _client, _pipeline
from test_order_handling_matrix import _event
from test_sl_trigger_immediate_execution import _child as _sl_child
from test_sl_trigger_immediate_execution import _client as _sl_client
from test_sl_trigger_immediate_execution import _fill_event, _record

APP = Path(__file__).resolve().parent.parent / "app"
KNOWN = [("MIS", ProductType.MIS), ("INTRADAY", ProductType.MIS), ("mis", ProductType.MIS), ("CNC", ProductType.CNC),
         ("LONGTERM", ProductType.CNC), ("NRML", ProductType.NRML), ("MTF", ProductType.NRML)]
# Values whose product on the wire depends on WHICH placement path sent the order, which is not persisted: never mapped.
PATH_DEPENDENT = ["DELIVERY", "CARRYFORWARD", "MARGIN"]


# ── the mapping ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("stored,expected", KNOWN)
def test_known_stored_products_map_to_the_broker_product(stored, expected):
    assert broker_product_for_modify(stored) is expected


@pytest.mark.parametrize("stored", [None, "", " ", "WEIRD", "BO", "CO", " CNC", "CNC ", 0, 7, "MIS;DROP"] + PATH_DEPENDENT)
def test_missing_unknown_or_path_dependent_stored_products_are_never_defaulted(stored):
    assert broker_product_for_modify(stored) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", PATH_DEPENDENT)
async def test_the_placement_paths_really_disagree_about_these_products(db_session, mock_redis, sample_accounts, sample_strategy, stored):
    """Pins the finding that makes them unmappable: LIMIT sends CNC / NRML / NRML, MARKET sends MIS. If placement is ever aligned this
    fails, and the values can then be mapped (and moved out of PATH_DEPENDENT)."""
    follower = sample_accounts["follower1"]
    sent = {}
    for kind, event in (("limit", _limit_event(master_order_id=f"MP_L_{stored}")), ("market", _market_event(master_order_id=f"MP_M_{stored}"))):
        client = AsyncMock()
        client.get_funds.return_value = _funds()
        client.get_positions_or_none.return_value = []
        client.get_positions.return_value = []
        client.place_order.return_value = _success_result(f"B_{kind}_{stored}")
        await CopyEnginePipeline(validator=EventValidator(redis=mock_redis)).execute_replication(
            event=replace(event, product_type=stored), clients={str(follower.id): client})
        sent[kind] = client.place_order.call_args.args[0].product_type
    assert sent["limit"] is not sent["market"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["limit", "market", "sl"])
async def test_the_modify_mapping_agrees_with_what_placement_sent(db_session, mock_redis, sample_accounts, sample_strategy, kind):
    """Drift guard: for every known product, the product placement put on the wire equals what a later modify will name."""
    follower = sample_accounts["follower1"]
    master = SimpleNamespace(id=uuid.UUID(SAMPLE_MASTER_ACCOUNT_ID))
    for stored, expected in KNOWN:
        oid = f"M_{kind}_{stored}"
        base = {"limit": lambda: _limit_event(master_order_id=oid), "market": lambda: _market_event(master_order_id=oid),
                "sl": lambda: _event(master, oid, side="BUY", order_type="SL", product="MIS", exchange="NSE", symbol="RELIANCE",
                                     qty=10, price="100", trigger="99", token="2885")}[kind]()
        client = AsyncMock()
        client.get_funds.return_value = _funds()
        client.get_positions_or_none.return_value = []
        client.get_positions.return_value = []
        client.place_order.return_value = _success_result(f"B_{kind}_{stored}")
        await CopyEnginePipeline(validator=EventValidator(redis=mock_redis)).execute_replication(
            event=replace(base, product_type=stored), clients={str(follower.id): client})
        sent = client.place_order.call_args.args[0]
        assert sent.product_type is expected and expected is broker_product_for_modify(stored), (kind, stored)


# ── the three pipeline call sites pass the persisted product ──────────────────────────────────────

def modify_kwargs(client):
    client.modify_order.assert_awaited_once()
    return client.modify_order.call_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("stored,expected", KNOWN + [("WEIRD", None), ("", None), ("DELIVERY", None)])
async def test_master_modification_passes_the_persisted_product(db_session, mock_redis, stored, expected):
    master = await _account(db_session, "vikash")
    child = await _account(db_session, "dhinesh")
    await link_strategy(db_session, master, [child])
    row = _child_order(child.id, "ORD_M1", "CH_1")
    row.product_type = stored
    db_session.add(row)
    await db_session.commit()
    client = _client("CH_1")
    res = await _pipeline(mock_redis).handle_master_modification(
        master_order_id="ORD_M1", new_price=Decimal("101"), new_quantity=7, clients={str(child.id): client},
        master_account_id=str(master.id),
    )
    assert res["modifications"][str(child.id)] == "MODIFIED"
    assert modify_kwargs(client)["product_type"] is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("stored,expected", [("MIS", ProductType.MIS), ("LONGTERM", ProductType.CNC), ("NRML", ProductType.NRML),
                                             ("WEIRD", None)])
async def test_pending_sl_modification_passes_the_persisted_product(db_session, mock_redis, sample_accounts, sample_strategy, stored, expected):
    follower = sample_accounts["follower1"]
    row = await _sl_child(db_session, follower.id, "M_PSL", "C_PSL", order_type="SL")
    row.product_type = stored
    await db_session.merge(row)
    await db_session.commit()
    client = AsyncMock()
    client.modify_order.return_value = OrderResult(category=OrderResultCategory.SUCCESS, client_order_id=None, broker_order_id="C_PSL",
                                                   message="ok", raw_response={}, timestamp=datetime.now(timezone.utc))
    event = MasterTradeEvent(master_order_id="M_PSL", symbol="ITC", exchange="NSE", side="BUY", quantity=1, price=Decimal("266.00"),
                             limit_price=Decimal("266.00"), trigger_price=Decimal("265.50"), product_type="MIS",
                             timestamp=datetime.now(timezone.utc), account_id=SAMPLE_MASTER_ACCOUNT_ID, order_type="SL",
                             event_type="SL_PENDING_MODIFIED", status="TRIGGER_PENDING", total_quantity=1, pending_quantity=1)
    res = await _pipeline(mock_redis).handle_pending_sl_modification(event=event, clients={str(follower.id): client})
    assert res["modifications"][str(follower.id)] == "MODIFIED"
    assert modify_kwargs(client)["product_type"] is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("stored,expected", [("MIS", ProductType.MIS), ("LONGTERM", ProductType.CNC), ("MTF", ProductType.NRML),
                                             ("WEIRD", None)])
async def test_sl_trigger_conversion_passes_the_persisted_product(db_session, mock_redis, sample_accounts, sample_strategy, stored, expected):
    follower = sample_accounts["follower1"]
    fill = Decimal("265.20")
    target = compute_marketable_limit_price("BUY", fill)
    row = await _sl_child(db_session, follower.id, "M_TRG", "C_TRG", order_type="SL")
    row.product_type = stored
    await db_session.merge(row)
    await db_session.commit()
    client = _sl_client([
        [_record("C_TRG", "SL", "TRIGGER_PENDING", Decimal("265.00"), Decimal("264.90"), side=BrokerOrderSide.BUY)],
        [_record("C_TRG", "LIMIT", "OPEN", target, side=BrokerOrderSide.BUY)],
    ])
    res = await _pipeline(mock_redis).execute_replication(
        event=_fill_event("M_TRG", side="BUY", fill=fill, reported_type="MARKET"), clients={str(follower.id): client})
    assert res["executions"][str(follower.id)].startswith("MODIFIED")
    assert modify_kwargs(client)["product_type"] is expected


def test_every_modify_call_in_the_pipeline_names_the_product():
    tree = ast.parse((APP / "services" / "copy_engine" / "pipeline.py").read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "modify_order"]
    assert len(calls) == 3
    for call in calls:
        kw = {k.arg: k.value for k in call.keywords}
        assert isinstance(kw.get("product_type"), ast.Call) and kw["product_type"].func.id == "broker_product_for_modify"


# ── Angel One through the real client and a fake transport ───────────────────────────────────────

def angel_ok(fake):
    fake.routes["modifyOrder"] = (200, {"status": True, "message": "SUCCESS", "data": {"orderid": "A1"}})


async def angel_child(db_session, mock_redis, stored_product):
    master = await _account(db_session, "alice_master", AccountType.MASTER, copy_setting=False)
    child = await _account(db_session, "angel_child")
    await link_strategy(db_session, master, [child])
    row = _child_order(child.id, "ORD_ANG", "A1")
    row.product_type = stored_product
    row.symbol = "SBIN"                                                     # an instrument in the fake Angel One store
    db_session.add(row)
    await db_session.commit()
    fake = FakeAngel()
    angel_ok(fake)
    client = make_angel_client(mock_redis, fake)
    await client.login()
    return master, child, fake, client


@pytest.mark.asyncio
@pytest.mark.parametrize("stored,wire", [("MIS", "INTRADAY"), ("INTRADAY", "INTRADAY"), ("LONGTERM", "DELIVERY"), ("CNC", "DELIVERY"),
                                         ("NRML", "CARRYFORWARD"), ("MTF", "CARRYFORWARD")])
async def test_an_angel_one_child_is_modified_with_its_own_persisted_product(db_session, mock_redis, stored, wire):
    master, child, fake, client = await angel_child(db_session, mock_redis, stored)
    res = await _pipeline(mock_redis).handle_master_modification(
        master_order_id="ORD_ANG", new_price=Decimal("101"), new_quantity=7, clients={str(child.id): client},
        master_account_id=str(master.id))
    assert res["modifications"][str(child.id)] == "MODIFIED"
    (sent,) = fake.calls("modifyOrder")
    body = json.loads(sent.content)
    assert body["producttype"] == wire and body["orderid"] == "A1"          # the order's own product, never another holding's


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", ["WEIRD", "", "BO", "DELIVERY", "CARRYFORWARD", "MARGIN"])
async def test_a_missing_unknown_or_ambiguous_product_fails_closed_and_sends_no_modify_request(db_session, mock_redis, stored):
    master, child, fake, client = await angel_child(db_session, mock_redis, stored)
    res = await _pipeline(mock_redis).handle_master_modification(
        master_order_id="ORD_ANG", new_price=Decimal("101"), new_quantity=7, clients={str(child.id): client},
        master_account_id=str(master.id))
    assert res["modifications"][str(child.id)] != "MODIFIED" and "product" in res["modifications"][str(child.id)].lower()
    assert fake.calls("modifyOrder") == []                                  # nothing was transmitted
    from sqlalchemy import select
    from app.models.trades import Order
    db_session.expire_all()
    order = (await db_session.execute(select(Order).where(Order.broker_order_id == "A1"))).scalar_one()
    assert order.price == Decimal("100.00") and order.quantity == 7        # the Child order row was not rewritten


@pytest.mark.asyncio
async def test_one_childs_failed_modification_does_not_block_another(db_session, mock_redis):
    master = await _account(db_session, "alice_master", AccountType.MASTER, copy_setting=False)
    angel = await _account(db_session, "angel_child")
    alice = await _account(db_session, "alice_child")
    await link_strategy(db_session, master, [angel, alice])
    bad = _child_order(angel.id, "ORD_X", "A1")
    bad.symbol = "SBIN"
    bad.product_type = "WEIRD"
    good = _child_order(alice.id, "ORD_X", "AL1")
    db_session.add_all([bad, good])
    await db_session.commit()
    fake = FakeAngel()
    angel_ok(fake)
    angel_client = make_angel_client(mock_redis, fake)
    await angel_client.login()
    alice_client = _client("AL1")
    res = await _pipeline(mock_redis).handle_master_modification(
        master_order_id="ORD_X", new_price=Decimal("101"), new_quantity=7,
        clients={str(angel.id): angel_client, str(alice.id): alice_client}, master_account_id=str(master.id))
    assert res["modifications"][str(alice.id)] == "MODIFIED" and res["modifications"][str(angel.id)] != "MODIFIED"
    alice_client.modify_order.assert_awaited_once()
    assert fake.calls("modifyOrder") == []


# ── Alice Blue and Groww wire payloads are unchanged ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_alice_blue_modify_payload_is_identical_whatever_product_is_named(mock_redis):
    bodies = []
    for kwargs in ({}, {"product_type": None}, {"product_type": ProductType.CNC}, {"product_type": ProductType.NRML}):
        h = await AliceHarness().build(mock_redis)
        h.modify_ok()
        await h.client.modify_order("B1", quantity=5, price=Decimal("101"), order_type=OrderType.LIMIT, symbol="RELIANCE",
                                    exchange="NSE", **kwargs)
        (call,) = h.modify_calls()
        bodies.append(call.content)
    assert len(set(bodies)) == 1                                           # byte-identical: the product is ignored, as before


@pytest.mark.asyncio
async def test_groww_modify_payload_is_identical_whatever_product_is_named(mock_redis):
    from groww_helpers import ok, order_row
    bodies = []
    for kwargs in ({}, {"product_type": None}, {"product_type": ProductType.CNC}, {"product_type": ProductType.NRML}):
        h = await GrowwHarness().build(mock_redis)
        h.fake.detail_response = ok(order_row(status="OPEN", filled=0, product="MIS"))
        res = await h.client.modify_order("GRW1", quantity=5, price=Decimal("101"), order_type=OrderType.LIMIT, symbol="SBIN",
                                          exchange="NSE", **kwargs)
        assert res.category == OrderResultCategory.SUCCESS
        (call,) = h.modify_calls()
        bodies.append(call.content)
    assert len(set(bodies)) == 1


def test_no_pair_was_enabled_and_no_default_changed():
    from app.core.config import Settings
    assert set(pairs.SUPPORTED_PAIRS) == {("alicebluenew", "alicebluenew")}
    s = Settings(_env_file=None)
    assert s.ENGINE_LIVE_TRADING_ENABLED is False and s.SELL_POSITION_SAFETY_BYPASS is False
