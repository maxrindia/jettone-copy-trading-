"""
Broker-aware identity of Master order events (brokers/identity.py).

A broker's native order id is only unique within that broker, but the duplicate guard (`copy:seen:...`) and the copy idempotency key
(`derive_copy_event_id`) used to be keyed on the native id alone. This pins: (1) Alice Blue's keys are BYTE-IDENTICAL to what they always
were (golden values - a change would re-open already-seen events across a deploy); (2) any other broker's keys are scoped by broker and
Master account, so two Masters can never suppress each other's orders; (3) the scope reaches the validator, the detector's events, the
copy_trades row and restart recovery. Offline only: mock Redis, mock clients, no broker, credentials or network.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.accounts import AccountType
from app.models.trades import CopyTrade
from app.services.alice_blue.types import OrderUpdateEvent
from app.services.brokers import pairs
from app.services.brokers.identity import ALICE_BLUE, master_scope
from app.services.copy_engine.detector import MasterTradeDetector
from app.services.copy_engine.events import COPY_EVENT_NAMESPACE, MasterTradeEvent, derive_copy_event_id
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from strategy_helpers import link_strategy
from test_marketable_limit_strategy import _funds, _success_result
from test_modification_after_master_switch import _account


def legacy_key(master_order_id, follower, seq):
    """The formula exactly as it was before the scope existed."""
    return uuid.uuid5(COPY_EVENT_NAMESPACE, f"{master_order_id}:{follower}:{seq}")


# ── the scope ───────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("broker", [None, "", "  ", ALICE_BLUE, "ALICEBLUENEW", " alicebluenew "])
def test_alice_blue_and_unknown_have_the_empty_scope(broker):
    assert master_scope(broker, "acc-1") == ""


@pytest.mark.parametrize("broker", ["groww", "angelone", "zerodha", "ZERODHA", " Groww "])
def test_every_other_broker_is_scoped_by_broker_and_account(broker):
    scope = master_scope(broker, "acc-1")
    assert scope == f"{broker.strip().lower()}:acc-1:"


def test_scopes_differ_per_broker_and_per_master_account():
    scopes = {master_scope(b, a) for b in ("groww", "angelone", "zerodha") for a in ("acc-1", "acc-2")}
    assert len(scopes) == 6 and "" not in scopes


# ── copy-event idempotency key ──────────────────────────────────────────────────────────────────────────────

GOLDEN = [("26090800085772", "7f2a1c36-1111-4222-8333-444455556666", 1), ("M1", "f1", 3), ("X", "y", 100), ("ORD-9", "child", 1)]


@pytest.mark.parametrize("master_order_id,follower,seq", GOLDEN)
def test_alice_blue_copy_event_ids_are_unchanged(master_order_id, follower, seq):
    assert derive_copy_event_id(master_order_id, follower, seq) == legacy_key(master_order_id, follower, seq)
    assert derive_copy_event_id(master_order_id, follower, seq, scope="") == legacy_key(master_order_id, follower, seq)
    assert derive_copy_event_id(master_order_id, follower, seq, scope=master_scope(ALICE_BLUE, "any")) == legacy_key(master_order_id, follower, seq)


def test_a_literal_golden_value_pins_the_legacy_formula_itself():
    """Independent of any helper: if the namespace or the hashed string ever changes, this changes."""
    assert str(derive_copy_event_id("M1", "f1", 3)) == str(uuid.uuid5(uuid.uuid5(uuid.NAMESPACE_URL, "jettone:copy-engine:copy-event"), "M1:f1:3"))


def test_the_same_native_id_on_different_brokers_or_masters_gives_different_keys():
    keys = {
        derive_copy_event_id("1000", "child", 1),
        derive_copy_event_id("1000", "child", 1, scope=master_scope("groww", "m-1")),
        derive_copy_event_id("1000", "child", 1, scope=master_scope("angelone", "m-1")),
        derive_copy_event_id("1000", "child", 1, scope=master_scope("zerodha", "m-1")),
        derive_copy_event_id("1000", "child", 1, scope=master_scope("zerodha", "m-2")),
    }
    assert len(keys) == 5


def test_the_key_is_deterministic_so_a_redelivery_still_collapses():
    scope = master_scope("zerodha", "m-1")
    assert derive_copy_event_id("1000", "child", 2, scope=scope) == derive_copy_event_id("1000", "child", 2, scope=scope)


# ── the event ───────────────────────────────────────────────────────────────────────────────────────────────

def make_event(master_broker=None, account_id="m-1", order_id="1000", seq=1):
    return MasterTradeEvent(master_order_id=order_id, symbol="RELIANCE", exchange="NSE", side="BUY", quantity=10, price=Decimal("2950"),
                            product_type="MIS", timestamp=datetime.now(timezone.utc), fill_sequence=seq, account_id=account_id,
                            order_type="LIMIT", limit_price=Decimal("2950"), event_type="LIMIT_CREATED", status="OPEN",
                            master_broker=master_broker)


def test_an_event_without_a_broker_has_the_legacy_scope():
    assert make_event().master_broker is None and make_event().dedupe_scope == ""
    assert make_event(ALICE_BLUE).dedupe_scope == ""
    assert make_event("groww").dedupe_scope == "groww:m-1:"


# ── the first-line duplicate guard ──────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_alice_blue_keeps_the_exact_redis_key(mock_redis):
    result = await EventValidator(redis=mock_redis).validate(make_event(ALICE_BLUE, order_id="26090800085772", seq=2))
    assert result.is_valid
    assert await mock_redis.get("copy:seen:26090800085772:2") is not None


@pytest.mark.asyncio
async def test_two_masters_with_the_same_native_id_do_not_suppress_each_other(mock_redis):
    validator = EventValidator(redis=mock_redis)
    first = await validator.validate(make_event("groww", account_id="m-groww", order_id="1000"))
    second = await validator.validate(make_event("angelone", account_id="m-angel", order_id="1000"))
    third = await validator.validate(make_event(ALICE_BLUE, account_id="m-alice", order_id="1000"))
    assert first.is_valid and second.is_valid and third.is_valid


@pytest.mark.asyncio
async def test_a_real_redelivery_from_the_same_master_is_still_a_duplicate(mock_redis):
    validator = EventValidator(redis=mock_redis)
    assert (await validator.validate(make_event("zerodha", account_id="m-1", order_id="1000"))).is_valid
    again = await validator.validate(make_event("zerodha", account_id="m-1", order_id="1000"))
    assert not again.is_valid and again.reason_code == "DUPLICATE_EVENT"
    other_seq = await validator.validate(make_event("zerodha", account_id="m-1", order_id="1000", seq=2))
    assert other_seq.is_valid                                                                # a later fill is a new event


# ── the detector stamps the broker ──────────────────────────────────────────────────────────────────────────

def update(account_id="m-1", order_id="1000"):
    return OrderUpdateEvent(broker_order_id=order_id, client_order_id=None, symbol="RELIANCE", side="BUY", quantity=10, filled_quantity=0,
                            fill_price=Decimal("0"), status="OPEN", timestamp=datetime.now(timezone.utc), account_id=account_id,
                            product_type="MIS", order_type="LIMIT", price=Decimal("2950"))


def test_the_detector_stamps_every_event_with_its_masters_broker():
    detector = MasterTradeDetector(master_account_id="m-1")
    detector.master_broker = "zerodha"
    event = detector.process_order_update(update())
    assert event is not None and event.master_broker == "zerodha" and event.dedupe_scope == "zerodha:m-1:"


def test_a_detector_without_a_broker_emits_legacy_events():
    event = MasterTradeDetector(master_account_id="m-1").process_order_update(update())
    assert event is not None and event.master_broker is None and event.dedupe_scope == ""


def test_the_detector_still_ignores_other_accounts_events():
    detector = MasterTradeDetector(master_account_id="m-1", master_broker="groww")
    assert detector.process_order_update(update(account_id="someone-else")) is None


# ── end to end through the real pipeline ────────────────────────────────────────────────────────────────────

@pytest.fixture
def pairs_waived(monkeypatch):
    """Test only: lets non-Alice Masters route to an Alice Child so the pipeline's own keys can be observed. Nothing outside the test."""
    monkeypatch.setattr(pairs, "SUPPORTED_PAIRS", {**pairs.SUPPORTED_PAIRS, ("groww", "alicebluenew"): "test only",
                                                   ("angelone", "alicebluenew"): "test only"})
    monkeypatch.setattr(pairs, "_feature_gap", lambda *a, **k: None)
    monkeypatch.setattr(pairs, "can_be_master", lambda *a, **k: True)
    monkeypatch.setattr(pairs, "can_be_child", lambda *a, **k: True)


async def _master_with_child(db_session, broker, name):
    master = await _account(db_session, f"m_{name}", AccountType.MASTER, copy_setting=False)
    child = await _account(db_session, f"c_{name}")
    master.broker = broker
    await db_session.commit()
    await link_strategy(db_session, master, [child], name=f"S_{name}")
    return master, child


def _client(order_id):
    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.get_positions_or_none.return_value = []
    client.get_positions.return_value = []
    client.place_order.return_value = _success_result(order_id)
    return client


@pytest.mark.asyncio
async def test_two_masters_on_different_brokers_with_the_same_native_order_id_are_both_copied(db_session, mock_redis, pairs_waived):
    m_groww, c_groww = await _master_with_child(db_session, "groww", "g")
    m_angel, c_angel = await _master_with_child(db_session, "angelone", "a")
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    clients = {str(c_groww.id): _client("B_G"), str(c_angel.id): _client("B_A")}
    for master, broker in ((m_groww, "groww"), (m_angel, "angelone")):
        event = make_event(broker, account_id=str(master.id), order_id="1000")      # the SAME native id
        result = await pipeline.execute_replication(event=event, clients=clients)
        assert result["status"] != "DUPLICATE_EVENT", (broker, result)
    assert clients[str(c_groww.id)].place_order.await_count == 1
    assert clients[str(c_angel.id)].place_order.await_count == 1                      # not swallowed as a "redelivery"


@pytest.mark.asyncio
async def test_the_copy_trade_row_carries_the_scoped_key_and_a_redelivery_still_sends_once(db_session, mock_redis, pairs_waived):
    master, child = await _master_with_child(db_session, "groww", "g")
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    client = _client("B_1")
    event = make_event("groww", account_id=str(master.id), order_id="GRW-77")
    await pipeline.execute_replication(event=event, clients={str(child.id): client})
    await pipeline.execute_replication(event=event, clients={str(child.id): client})            # the same event redelivered
    assert client.place_order.await_count == 1
    expected = derive_copy_event_id("GRW-77", str(child.id), 1, scope=f"groww:{master.id}:")
    rows = (await db_session.execute(select(CopyTrade).where(CopyTrade.master_order_id == "GRW-77"))).scalars().all()
    assert [r.copy_event_id for r in rows] == [expected]
    assert expected != legacy_key("GRW-77", str(child.id), 1)
    assert await mock_redis.get(f"copy:seen:groww:{master.id}:GRW-77:1") is not None


@pytest.mark.asyncio
async def test_a_pre_send_rejection_row_carries_the_same_scoped_key(db_session, mock_redis, pairs_waived):
    """The Child has no client, so it is recorded as REJECTED before anything is sent (_record_child_result): that row must be keyed
    exactly like the sent ones, or a later retry of the same event could not be matched to it."""
    master, child = await _master_with_child(db_session, "groww", "g")
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await pipeline.execute_replication(event=make_event("groww", account_id=str(master.id), order_id="PRE-1"), clients={})
    rows = (await db_session.execute(select(CopyTrade).where(CopyTrade.master_order_id == "PRE-1"))).scalars().all()
    assert len(rows) == 1 and rows[0].status.value == "REJECTED"
    assert rows[0].copy_event_id == derive_copy_event_id("PRE-1", str(child.id), 1, scope=f"groww:{master.id}:")


@pytest.mark.asyncio
async def test_an_alice_blue_master_still_writes_the_legacy_key_through_the_pipeline(db_session, mock_redis):
    master = await _account(db_session, "m_alice", AccountType.MASTER, copy_setting=False)
    child = await _account(db_session, "c_alice")
    await link_strategy(db_session, master, [child], name="S_alice")
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    client = _client("B_1")
    event = make_event(None, account_id=str(master.id), order_id="26090800085772")
    await pipeline.execute_replication(event=event, clients={str(child.id): client})
    rows = (await db_session.execute(select(CopyTrade).where(CopyTrade.master_order_id == "26090800085772"))).scalars().all()
    assert [r.copy_event_id for r in rows] == [legacy_key("26090800085772", str(child.id), 1)]
    assert await mock_redis.get("copy:seen:26090800085772:1") is not None


def test_every_derive_copy_event_id_call_in_the_pipeline_carries_a_scope():
    """A new call site that forgets the scope would silently reopen the collision for non-Alice Masters."""
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "app" / "services"
    for rel in ("copy_engine/pipeline.py", "reliability/restart_recovery.py"):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == "derive_copy_event_id"]
        assert calls, rel
        for call in calls:
            assert any(k.arg == "scope" for k in call.keywords), (rel, call.lineno)
