"""
OBSERVE-mode quantity validation at the pipeline's final-quantity points (placement: LIMIT / SL / MARKET; modification: pending-SL
modify, SL-trigger conversion, master modify). The observation logs and counts what the pure lot / freeze validator would say and
changes NOTHING: every result, every broker payload and every sibling Child outcome is identical to a control run in which the
observer is replaced by a no-op. Offline only: mock clients, an in-memory terms store, no broker / Redis / network / production.
"""
import ast
import logging
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.services.alice_blue.types import OrderResult, OrderResultCategory
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline, compute_marketable_limit_price
from app.services.copy_engine.validator import EventValidator
from app.services.instruments import terms as T
from app.services.instruments.terms import ALICE, InstrumentTerms, Knowledge
from strategy_helpers import SAMPLE_MASTER_ACCOUNT_ID, link_strategy
from test_marketable_limit_strategy import _funds, _limit_event, _market_event, _success_result
from test_modification_after_master_switch import _account, _child_order, _client, _pipeline
from test_order_handling_matrix import _event
from test_sl_trigger_immediate_execution import _child as _sl_child
from test_sl_trigger_immediate_execution import _client as _sl_client
from test_sl_trigger_immediate_execution import _fill_event, _record

APP = Path(__file__).resolve().parent.parent / "app"
TOKEN = "2885"
NOW = datetime.now(timezone.utc)


def put_terms(lot=7, freeze=None, exchange="NSE", token=TOKEN, **kw):
    """A fresh in-store entry. freeze=None -> UNKNOWN (the only state any real source gives today)."""
    base = dict(broker=ALICE, exchange=exchange, token=token, lot_size=lot, lot_state=Knowledge.KNOWN, source="test",
                loaded_at=datetime.now(timezone.utc))
    if freeze:
        base.update(freeze_quantity=freeze, freeze_state=Knowledge.KNOWN)
    base.update(kw)
    t = InstrumentTerms(**base)
    T.store._by_exchange.setdefault((ALICE, exchange), {})[t.key] = t


@pytest.fixture(autouse=True)
def clean_observation_state():
    T.store.clear()
    T.observation_counts.clear()
    yield
    T.store.clear()
    T.observation_counts.clear()


@pytest.fixture
def observed(monkeypatch):
    """Records every real observation call (it still runs the real code)."""
    calls = []
    real = CopyEnginePipeline._observe_child_quantity

    async def spy(self, **kw):
        calls.append(kw)
        return await real(self, **kw)
    monkeypatch.setattr(CopyEnginePipeline, "_observe_child_quantity", spy)
    return calls


def silence(monkeypatch):
    monkeypatch.setattr(CopyEnginePipeline, "_observe_child_quantity", AsyncMock(return_value=None))


def raising(monkeypatch):
    def boom(**kw):
        raise RuntimeError("terms backend exploded")
    monkeypatch.setattr("app.services.copy_engine.pipeline.observe_quantity", boom)


def wire(order):
    """The parts of a placed order that go on the wire (identity of the payload, not generated ids)."""
    return (order.symbol, order.exchange, str(order.side), str(order.order_type), str(order.product_type), order.quantity,
            order.price, getattr(order, "trigger_price", None))


def norm(result):
    out = dict(result or {})
    return {k: out.get(k) for k in ("status", "reason", "reasons") if k in out}


# ── placement ────────────────────────────────────────────────────────────────────────────────────

def make_event(kind, oid, master_id=SAMPLE_MASTER_ACCOUNT_ID):
    master = SimpleNamespace(id=uuid.UUID(str(master_id)))
    if kind == "limit":
        return replace(_limit_event(master_order_id=oid), instrument_id=TOKEN, account_id=str(master_id))
    if kind == "market":
        return replace(_market_event(master_order_id=oid), instrument_id=TOKEN, account_id=str(master_id))
    return _event(master, oid, side="BUY", order_type="SL", product="MIS", exchange="NSE", symbol="RELIANCE", qty=10, price="100",
                  trigger="99", token=TOKEN)


async def place(kind, oid, db, redis, accounts, followers=("follower1",), master_id=SAMPLE_MASTER_ACCOUNT_ID):
    clients = {}
    for name in followers:
        c = AsyncMock()
        c.get_funds.return_value = _funds()
        c.get_positions_or_none.return_value = []
        c.get_positions.return_value = []
        c.place_order.return_value = _success_result(f"B_{oid}_{name}")
        clients[str(accounts[name].id)] = c
    res = await CopyEnginePipeline(validator=EventValidator(redis=redis)).execute_replication(event=make_event(kind, oid, master_id), clients=clients)
    return res, {cid: [wire(call.args[0]) for call in c.place_order.call_args_list] for cid, c in clients.items()}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,point", [("limit", "PLACE_LIMIT"), ("market", "PLACE_MARKET"), ("sl", "PLACE_SL")])
async def test_placement_decision_and_payload_are_identical_with_observation_on_a_would_be_rejection(
        db_session, mock_redis, sample_accounts, sample_strategy, monkeypatch, observed, kind, point):
    with monkeypatch.context() as mp:
        silence(mp)
        ctrl_res, ctrl_wire = await place(kind, f"CTRL_{kind}", db_session, mock_redis, sample_accounts)
    T.store.clear()
    put_terms(lot=7, freeze=5)                                   # 10 is not a multiple of 7 and exceeds a freeze of 5: would reject
    res, wired = await place(kind, f"OBS_{kind}", db_session, mock_redis, sample_accounts)
    assert norm(res) == norm(ctrl_res)
    assert [list(v) for v in wired.values()] == [list(v) for v in ctrl_wire.values()]
    assert any(v for v in wired.values()), "the order must still be sent"
    assert T.observation_counts[(point, "REJECT")] == 1
    assert T.observation_counts[(point, T.NOT_LOT_MULTIPLE)] == 1 and T.observation_counts[(point, T.EXCEEDS_FREEZE)] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["limit", "market", "sl"])
@pytest.mark.parametrize("scenario", ["empty_store", "unknown_freeze_lot_multiple", "stale", "conflict", "invalid"])
async def test_placement_is_unchanged_whatever_the_metadata_state(db_session, mock_redis, sample_accounts, sample_strategy, monkeypatch,
                                                                   observed, kind, scenario):
    with monkeypatch.context() as mp:
        silence(mp)
        ctrl_res, ctrl_wire = await place(kind, f"C2_{kind}_{scenario}", db_session, mock_redis, sample_accounts)
    T.store.clear()
    if scenario == "unknown_freeze_lot_multiple":
        put_terms(lot=5)
    elif scenario == "stale":
        put_terms(lot=5, freeze=100, loaded_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    elif scenario == "conflict":
        put_terms(lot=None, lot_state=Knowledge.UNKNOWN, status=T.TermsStatus.CONFLICT, issues=("DUPLICATE_TOKEN_DIFFERING_ROWS",))
    elif scenario == "invalid":
        put_terms(lot=None, lot_state=Knowledge.INVALID, status=T.TermsStatus.INVALID, issues=("LOT_NON_POSITIVE",))
    res, wired = await place(kind, f"O2_{kind}_{scenario}", db_session, mock_redis, sample_accounts)
    assert norm(res) == norm(ctrl_res)
    assert [list(v) for v in wired.values()] == [list(v) for v in ctrl_wire.values()]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["limit", "market", "sl"])
async def test_a_failing_observer_cannot_change_or_break_an_order(db_session, mock_redis, sample_accounts, sample_strategy, monkeypatch, kind):
    with monkeypatch.context() as mp:
        silence(mp)
        ctrl_res, ctrl_wire = await place(kind, f"C3_{kind}", db_session, mock_redis, sample_accounts)
    with monkeypatch.context() as mp:
        raising(mp)
        res, wired = await place(kind, f"O3_{kind}", db_session, mock_redis, sample_accounts)
    assert norm(res) == norm(ctrl_res)
    assert [list(v) for v in wired.values()] == [list(v) for v in ctrl_wire.values()]
    assert not T.observation_counts


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["limit", "market", "sl"])
async def test_sibling_children_are_unaffected_and_each_is_observed_on_its_own_quantity(db_session, mock_redis, monkeypatch, observed, kind):
    master = await _account(db_session, "vikash")
    c1 = await _account(db_session, "dhinesh")
    c2 = await _account(db_session, "arun")
    await link_strategy(db_session, master, [c1, c2])
    accounts = {"c1": c1, "c2": c2}
    with monkeypatch.context() as mp:
        silence(mp)
        ctrl_res, ctrl_wire = await place(kind, f"C4_{kind}", db_session, mock_redis, accounts, ("c1", "c2"), master.id)
    put_terms(lot=7, freeze=5)
    res, wired = await place(kind, f"O4_{kind}", db_session, mock_redis, accounts, ("c1", "c2"), master.id)
    assert norm(res) == norm(ctrl_res)
    assert sorted(map(tuple, wired.values())) == sorted(map(tuple, ctrl_wire.values()))
    assert all(len(v) == 1 for v in wired.values()), "exactly one transmission per Child"
    assert {str(c["child_account_id"]) for c in observed} == {str(c1.id), str(c2.id)}


@pytest.mark.asyncio
async def test_replaying_an_event_still_transmits_once_with_observation_on(db_session, mock_redis, sample_accounts, sample_strategy, observed):
    put_terms(lot=7, freeze=5)
    follower = sample_accounts["follower1"]
    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.get_positions_or_none.return_value = []
    client.get_positions.return_value = []
    client.place_order.return_value = _success_result("B_REPLAY")
    pipe = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    event = make_event("limit", "REPLAY_1")
    await pipe.execute_replication(event=event, clients={str(follower.id): client})
    await pipe.execute_replication(event=event, clients={str(follower.id): client})
    assert client.place_order.await_count == 1


# ── verdict honesty ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_freeze_is_logged_as_unverified_never_as_safe(db_session, mock_redis, sample_accounts, sample_strategy, caplog):
    put_terms(lot=5)                                               # lot known and the quantity is a multiple; freeze unknown
    caplog.set_level(logging.INFO, logger="copy_engine_pipeline")
    await place("limit", "HONEST_1", db_session, mock_redis, sample_accounts)
    lines = [r.getMessage() for r in caplog.records if "QUANTITY_OBSERVE" in r.getMessage()]
    assert lines and all("verified_safe=False" in m and "UNKNOWN" in m and T.FREEZE_UNKNOWN in m for m in lines)
    assert not any("verified_safe=True" in m or "ALLOW" in m for m in lines)
    assert T.observation_counts[("PLACE_LIMIT", "UNKNOWN")] == 1 and T.observation_counts[("PLACE_LIMIT", "ALLOW")] == 0


@pytest.mark.asyncio
async def test_missing_metadata_is_reported_as_unknown_not_safe(db_session, mock_redis, sample_accounts, sample_strategy, caplog):
    caplog.set_level(logging.INFO, logger="copy_engine_pipeline")
    await place("market", "HONEST_2", db_session, mock_redis, sample_accounts)
    lines = [r.getMessage() for r in caplog.records if "QUANTITY_OBSERVE" in r.getMessage()]
    assert lines and all("verified_safe=False" in m and T.TERMS_MISSING in m for m in lines)
    assert all(r.levelno >= logging.WARNING for r in caplog.records if "QUANTITY_OBSERVE" in r.getMessage())


@pytest.mark.asyncio
async def test_only_fully_established_terms_log_allow(db_session, mock_redis, sample_accounts, sample_strategy, caplog):
    put_terms(lot=5, freeze=100)                                   # both known, quantity 10 satisfies both
    caplog.set_level(logging.INFO, logger="copy_engine_pipeline")
    await place("limit", "HONEST_3", db_session, mock_redis, sample_accounts)
    assert T.observation_counts[("PLACE_LIMIT", "ALLOW")] == 1


# ── modification paths ───────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_master_modification_observes_without_changing_the_modify_payload(db_session, mock_redis, observed):
    master = await _account(db_session, "vikash")
    child = await _account(db_session, "dhinesh")
    await link_strategy(db_session, master, [child])
    row = _child_order(child.id, "ORD_MM2", "CH_MM2")
    row.instrument_token = TOKEN
    db_session.add(row)
    await db_session.commit()
    put_terms(lot=7, freeze=5, exchange=row.exchange)
    client = _client("CH_MM2")
    res = await _pipeline(mock_redis).handle_master_modification(
        master_order_id="ORD_MM2", new_price=Decimal("101"), new_quantity=7, clients={str(child.id): client},
        master_account_id=str(master.id))
    assert res["modifications"][str(child.id)] == "MODIFIED"
    client.modify_order.assert_awaited_once()
    assert client.modify_order.call_args.kwargs["quantity"] == 7              # the engine's own quantity, not a corrected one
    assert [o["point"] for o in observed] == ["MODIFY_MASTER"]
    assert observed[0]["quantity"] == 7 and observed[0]["token"] == TOKEN
    assert T.observation_counts[("MODIFY_MASTER", "REJECT")] == 1


@pytest.mark.asyncio
async def test_pending_sl_modification_observes_without_changing_the_modify_payload(db_session, mock_redis, sample_accounts, sample_strategy, observed):
    follower = sample_accounts["follower1"]
    row = await _sl_child(db_session, follower.id, "M_OPSL", "C_OPSL", order_type="SL")
    row.instrument_token = TOKEN
    await db_session.merge(row)
    await db_session.commit()
    put_terms(lot=7, freeze=5, exchange=row.exchange)
    client = AsyncMock()
    client.modify_order.return_value = OrderResult(category=OrderResultCategory.SUCCESS, client_order_id=None, broker_order_id="C_OPSL",
                                                   message="ok", raw_response={}, timestamp=datetime.now(timezone.utc))
    event = MasterTradeEvent(master_order_id="M_OPSL", symbol="ITC", exchange="NSE", side="BUY", quantity=1, price=Decimal("266.00"),
                             limit_price=Decimal("266.00"), trigger_price=Decimal("265.50"), product_type="MIS",
                             timestamp=datetime.now(timezone.utc), account_id=SAMPLE_MASTER_ACCOUNT_ID, order_type="SL",
                             event_type="SL_PENDING_MODIFIED", status="TRIGGER_PENDING", total_quantity=1, pending_quantity=1)
    res = await _pipeline(mock_redis).handle_pending_sl_modification(event=event, clients={str(follower.id): client})
    assert res["modifications"][str(follower.id)] == "MODIFIED"
    client.modify_order.assert_awaited_once()
    assert [o["point"] for o in observed] == ["MODIFY_PENDING_SL"]
    assert observed[0]["quantity"] == client.modify_order.call_args.kwargs["quantity"]
    assert T.observation_counts[("MODIFY_PENDING_SL", "REJECT")] == 1


@pytest.mark.asyncio
async def test_sl_trigger_conversion_observes_without_changing_the_modify_payload(db_session, mock_redis, sample_accounts, sample_strategy, observed):
    follower = sample_accounts["follower1"]
    row = await _sl_child(db_session, follower.id, "M_OSLT", "C_OSLT", order_type="SL", quantity=10)
    row.instrument_token = TOKEN
    await db_session.merge(row)
    await db_session.commit()
    put_terms(lot=7, freeze=5, exchange=row.exchange)
    fill = Decimal("265.20")
    target = compute_marketable_limit_price("BUY", fill)
    client = _sl_client([
        [_record("C_OSLT", "SL", "TRIGGER_PENDING", Decimal("265.00"), Decimal("264.90"), quantity=10)],
        [_record("C_OSLT", "LIMIT", "OPEN", target, quantity=10)],
    ])
    event = replace(_fill_event("M_OSLT", fill=fill), account_id=SAMPLE_MASTER_ACCOUNT_ID)
    res = await _pipeline(mock_redis).execute_replication(event=event, clients={str(follower.id): client})
    client.modify_order.assert_awaited_once()
    assert [o["point"] for o in observed] == ["SL_TRIGGER_CONVERSION"]
    assert observed[0]["quantity"] == client.modify_order.call_args.kwargs["quantity"] == 10
    assert T.observation_counts[("SL_TRIGGER_CONVERSION", "REJECT")] == 1
    assert res["executions"][str(follower.id)].startswith("MODIFIED")


# ── structure ────────────────────────────────────────────────────────────────────────────────────

def test_default_mode_is_observe_and_no_decision_code_reads_the_mode():
    assert settings.QUANTITY_VALIDATION_MODE == "OBSERVE"
    readers = []
    for path in APP.rglob("*.py"):
        if "QUANTITY_VALIDATION_MODE" in path.read_text(encoding="utf-8"):
            readers.append(path.relative_to(APP).as_posix())
    assert readers == ["core/config.py"]


def test_the_terms_module_imports_no_network_database_or_broker_code():
    tree = ast.parse((APP / "services/instruments/terms.py").read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods.add((node.module or "").split(".")[0])
    assert not mods & {"httpx", "requests", "aiohttp", "socket", "sqlalchemy", "redis", "app"}


def test_every_observation_point_is_observe_only_in_the_pipeline_source():
    src = (APP / "services/copy_engine/pipeline.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "_observe_child_quantity"]
    points = sorted(k.value.value for c in calls for k in c.keywords if k.arg == "point")
    assert points == ["MODIFY_MASTER", "MODIFY_PENDING_SL", "PLACE_LIMIT", "PLACE_MARKET", "PLACE_SL", "SL_TRIGGER_CONVERSION"]
    for c in calls:                                                     # a bare awaited statement: its value is never used
        parent_is_expr_await = any(isinstance(p, ast.Expr) and isinstance(p.value, ast.Await) and p.value.value is c
                                   for p in ast.walk(tree))
        assert parent_is_expr_await
