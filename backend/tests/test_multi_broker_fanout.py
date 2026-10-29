"""
One Master entry fans out to EVERY eligible Child of the strategy, whatever the Child's (or the Master's) broker.

Fixtures and mocks only - no broker, Redis, network or production access. These tests exercise the engine's routing, per-Child pair
check, per-Child isolation and idempotency with every Master -> Child broker combination. They are CODE-LEVEL evidence: the pair
gate is waived inside each test by the `all_pairs_open_for_test` fixture (the shipped allowlist, role flags and evidence ceilings
are untouched and still refuse every non-Alice pair - see test_broker_pair_gate.py), and a mocked client is not proof that a real
broker accepts the order. Real-broker verification is a separate, supervised step.
"""
import itertools
import socket

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.accounts import Account
from app.models.trades import CopyTrade, CopyTradeStatus, Order
from app.services.brokers import pairs
from app.services.copy_engine import pipeline as pipeline_module
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from conftest import TestingSessionLocal
from strategy_helpers import link_strategy
from test_order_handling_matrix import _event
from test_strategy_master_switching import _account, _child_client

BROKERS = ("alicebluenew", "groww", "zerodha", "angelone")
ALL_PAIRS = list(itertools.product(BROKERS, BROKERS))
ORDERS = [("LIMIT", "0"), ("MARKET", "0"), ("SL", "790")]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    local = {"127.0.0.1", "::1", "localhost", None, ""}
    real_connect, real_getaddrinfo = socket.socket.connect, socket.getaddrinfo

    def connect(self, address, *a, **k):
        if (address[0] if isinstance(address, tuple) else address) not in local:
            raise AssertionError("network access attempted")
        return real_connect(self, address, *a, **k)

    def getaddrinfo(host, *a, **k):
        if host not in local:
            raise AssertionError("network access attempted")
        return real_getaddrinfo(host, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


ALICE_SBIN_TOKEN = "3045"          # Alice Blue's own token for NSE SBIN-EQ in the test contract master below (never the Master's 12345)
ALICE_NSE_MASTER = (
    "Exch,Exchange Segment,Group Name,Symbol,Token,Instrument Type,Instrument Name,Formatted Ins Name,Trading Symbol,Lot Size,Tick Size\n"
    f"NSE,nse_cm,EQ,SBIN,{ALICE_SBIN_TOKEN},0,STATE BANK OF INDIA,SBIN-EQ,SBIN-EQ,1,0.05\n"
)


@pytest.fixture(autouse=True)
def alice_contract_master(monkeypatch):
    """A fresh, just-loaded Alice NSE contract master for each test: an Alice Child of another broker's Master is given Alice's own
    token from it (planner.alice_cross_broker_token) and is skipped when it is missing."""
    from app.services.instruments import terms
    fresh = terms.TermsStore()
    fresh.load_alice("NSE", ALICE_NSE_MASTER)
    monkeypatch.setattr(terms, "store", fresh)
    return fresh


@pytest.fixture
def all_pairs_open_for_test(monkeypatch):
    """TEST ONLY: list all 16 pairs and open every role gate, for the duration of one test."""
    for name in ("GROWW_ENABLED", "GROWW_CHILD_TRADING_ENABLED", "GROWW_MASTER_ENABLED",
                 "ANGEL_ONE_ENABLED", "ANGEL_ONE_CHILD_TRADING_ENABLED", "ANGEL_ONE_MASTER_ENABLED",
                 "ZERODHA_ENABLED", "ZERODHA_CHILD_TRADING_ENABLED", "ZERODHA_MASTER_ENABLED"):
        monkeypatch.setattr(settings, name, True)
    monkeypatch.setattr(settings, "SELL_POSITION_SAFETY_BYPASS", False)
    monkeypatch.setattr(pairs, "SUPPORTED_PAIRS", {pair: "test only" for pair in ALL_PAIRS})
    monkeypatch.setattr(pairs, "_feature_gap", lambda *a, **k: None)
    monkeypatch.setattr(pairs, "can_be_master", lambda *a, **k: True)
    monkeypatch.setattr(pairs, "can_be_child", lambda *a, **k: True)
    monkeypatch.setattr(pipeline_module, "can_be_child", lambda *a, **k: True)       # the routing-time role gate, waived here only


async def set_broker(account, broker):
    async with TestingSessionLocal() as s:
        (await s.get(Account, account.id)).broker = broker
        await s.commit()


async def build_strategy(db_session, master_broker, child_brokers, name):
    master = await _account(db_session, "master")
    await set_broker(master, master_broker)
    children = []
    for i, broker in enumerate(child_brokers):
        child = await _account(db_session, f"child{i}")
        await set_broker(child, broker)
        children.append(child)
    await link_strategy(db_session, master, children, name=name)
    return master, children


def entry(master, oid, order_type, trigger):
    return _event(master, oid, side="BUY", order_type=order_type, product="INTRADAY", exchange="NSE", symbol="SBIN",
                  qty=10, price="800", trigger=trigger, token="12345")


async def copies_of(account):
    async with TestingSessionLocal() as s:
        return (await s.execute(select(CopyTrade).where(CopyTrade.follower_account_id == account.id))).scalars().all()


async def orders_of(account):
    async with TestingSessionLocal() as s:
        return (await s.execute(select(Order).where(Order.account_id == account.id))).scalars().all()


def pipeline(mock_redis):
    return CopyEnginePipeline(validator=EventValidator(redis=mock_redis))


@pytest.mark.asyncio
@pytest.mark.parametrize("master_broker", BROKERS)
@pytest.mark.parametrize("order_type,trigger", ORDERS)
async def test_one_master_entry_reaches_every_child_on_every_broker(
        db_session, mock_redis, all_pairs_open_for_test, master_broker, order_type, trigger):
    """Two Alice Blue Children, two Groww, one Zerodha, one Angel One: one Master entry, six Child orders."""
    child_brokers = ["alicebluenew", "alicebluenew", "groww", "groww", "zerodha", "angelone"]
    master, children = await build_strategy(db_session, master_broker, child_brokers, f"fan-{master_broker}-{order_type}")
    clients = {str(c.id): _child_client(f"BRK_{i}") for i, c in enumerate(children)}
    res = await pipeline(mock_redis).execute_replication(
        event=entry(master, f"FAN-{master_broker}-{order_type}", order_type, trigger), clients=clients)

    for child in children:
        assert res["results"][str(child.id)]["status"] == "SUCCESS", (child.broker, res["results"][str(child.id)])
        assert clients[str(child.id)].place_order.await_count == 1
        assert len(await copies_of(child)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("master_broker,child_broker", ALL_PAIRS)
async def test_every_master_child_combination_copies_with_the_pair_waived(
        db_session, mock_redis, all_pairs_open_for_test, master_broker, child_broker):
    master, children = await build_strategy(db_session, master_broker, [child_broker], f"pair-{master_broker}-{child_broker}")
    clients = {str(children[0].id): _child_client("BRK_X")}
    res = await pipeline(mock_redis).execute_replication(
        event=entry(master, f"PAIR-{master_broker}-{child_broker}", "LIMIT", "0"), clients=clients)
    assert res["results"][str(children[0].id)]["status"] == "SUCCESS", res["results"]
    assert clients[str(children[0].id)].place_order.await_count == 1


# ── a Master broker's own instrument id is never sent to another broker as that broker's token ──────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("master_broker,child_broker", [p for p in ALL_PAIRS])
@pytest.mark.parametrize("order_type,trigger", ORDERS)
async def test_a_foreign_instrument_token_never_reaches_another_brokers_child(
        db_session, mock_redis, all_pairs_open_for_test, master_broker, child_broker, order_type, trigger):
    master, children = await build_strategy(db_session, master_broker, [child_broker], f"tok-{master_broker}-{child_broker}-{order_type}")
    child = children[0]
    client = _child_client("BRK_T")
    await pipeline(mock_redis).execute_replication(
        event=entry(master, f"TOK-{master_broker}-{child_broker}-{order_type}", order_type, trigger),
        clients={str(child.id): client})
    request = client.place_order.await_args.args[0]
    if master_broker == child_broker:
        assert request.instrument_token == "12345"          # same broker: the Master's token is native to the Child
    elif child_broker == "alicebluenew":
        assert request.instrument_token == ALICE_SBIN_TOKEN  # Alice's own token from its contract master, never the Master's
    else:
        assert request.instrument_token is None, (master_broker, child_broker, request.instrument_token)


# ── one Child's failure never stops the others, and a repeated event never doubles an order ───────────────────────────────────

from datetime import datetime, timezone                                                     # noqa: E402

from app.services.alice_blue.types import OrderResult, OrderResultCategory                    # noqa: E402
from test_order_handling_matrix import _pos                                                  # noqa: E402

MIXED = ["alicebluenew", "groww", "zerodha", "angelone", "alicebluenew"]


def _result(category, boid=None, message="x"):
    return OrderResult(category=category, client_order_id=None, broker_order_id=boid, message=message, raw_response={},
                       timestamp=datetime.now(timezone.utc))


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", range(len(MIXED)))
async def test_one_child_rejected_by_its_broker_does_not_stop_the_others(db_session, mock_redis, all_pairs_open_for_test, failing):
    master, children = await build_strategy(db_session, "alicebluenew", MIXED, f"reject-{failing}")
    clients = {str(c.id): _child_client(f"BRK_{i}") for i, c in enumerate(children)}
    clients[str(children[failing].id)].place_order.return_value = _result(OrderResultCategory.REJECTED, None, "RMS: insufficient")
    res = await pipeline(mock_redis).execute_replication(event=entry(master, f"REJ-{failing}", "LIMIT", "0"), clients=clients)

    for i, child in enumerate(children):
        assert clients[str(child.id)].place_order.await_count == 1                          # every Child was attempted
        expected = "REJECTED" if i == failing else "SUCCESS"
        assert res["results"][str(child.id)]["status"] == expected, (i, MIXED[i], res["results"][str(child.id)])
    failed_copies = await copies_of(children[failing])
    assert len(failed_copies) == 1 and failed_copies[0].status != CopyTradeStatus.SUCCESS           # recorded, not silently skipped


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", range(len(MIXED)))
async def test_an_ambiguous_send_is_recorded_for_that_child_only_and_is_never_resent(
        db_session, mock_redis, all_pairs_open_for_test, failing):
    master, children = await build_strategy(db_session, "alicebluenew", MIXED, f"timeout-{failing}")
    clients = {str(c.id): _child_client(f"BRK_{i}") for i, c in enumerate(children)}
    clients[str(children[failing].id)].place_order.return_value = _result(OrderResultCategory.TIMEOUT, None, "no answer")
    res = await pipeline(mock_redis).execute_replication(event=entry(master, f"TMO-{failing}", "MARKET", "0"), clients=clients)

    for i, child in enumerate(children):
        assert clients[str(child.id)].place_order.await_count == 1, (i, "an ambiguous send must not be sent again")
        status = res["results"][str(child.id)]["status"]
        assert (status == "TIMEOUT") if i == failing else (status == "SUCCESS"), (i, status)


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type,trigger", ORDERS)
async def test_the_same_master_event_twice_places_one_order_per_child(
        db_session, mock_redis, all_pairs_open_for_test, order_type, trigger):
    master, children = await build_strategy(db_session, "alicebluenew", MIXED, f"dup-{order_type}")
    clients = {str(c.id): _child_client(f"BRK_{i}") for i, c in enumerate(children)}
    pipe = pipeline(mock_redis)
    event = entry(master, f"DUP-{order_type}", order_type, trigger)
    await pipe.execute_replication(event=event, clients=clients)
    await pipe.execute_replication(event=event, clients=clients)
    for child in children:
        assert clients[str(child.id)].place_order.await_count == 1
        assert len(await copies_of(child)) == 1


@pytest.mark.asyncio
async def test_a_child_whose_funds_cannot_be_read_is_not_copied_and_the_others_are(db_session, mock_redis, all_pairs_open_for_test):
    """Fail closed per Child: unreadable funds mean no order for that Child, never a guess, and no effect on its siblings."""
    master, children = await build_strategy(db_session, "alicebluenew", MIXED, "funds")
    clients = {str(c.id): _child_client(f"BRK_{i}") for i, c in enumerate(children)}
    clients[str(children[2].id)].get_funds.return_value = None                              # the Zerodha Child
    res = await pipeline(mock_redis).execute_replication(event=entry(master, "FUNDS-1", "MARKET", "0"), clients=clients)

    assert clients[str(children[2].id)].place_order.await_count == 0
    assert res["results"][str(children[2].id)]["status"] != "SUCCESS"
    for i in (0, 1, 3, 4):
        assert clients[str(children[i].id)].place_order.await_count == 1
        assert res["results"][str(children[i].id)]["status"] == "SUCCESS"


@pytest.mark.asyncio
async def test_a_child_over_its_risk_limit_is_rejected_and_the_others_are_copied(db_session, mock_redis, all_pairs_open_for_test):
    master, children = await build_strategy(db_session, "alicebluenew", MIXED, "risk")
    clients = {str(c.id): _child_client(f"BRK_{i}") for i, c in enumerate(children)}
    broke = clients[str(children[1].id)]                                                    # the Groww Child has no margin
    funds = broke.get_funds.return_value
    broke.get_funds.return_value = type(funds)(**{**funds.__dict__, "available_margin": 0}) if hasattr(funds, "__dict__") else funds
    res = await pipeline(mock_redis).execute_replication(event=entry(master, "RISK-1", "MARKET", "0"), clients=clients)

    assert broke.place_order.await_count == 0 and res["results"][str(children[1].id)]["status"] != "SUCCESS"
    for i in (0, 2, 3, 4):
        assert res["results"][str(children[i].id)]["status"] == "SUCCESS"


@pytest.mark.asyncio
async def test_an_exit_is_blocked_for_every_child_that_holds_nothing_whatever_its_broker(db_session, mock_redis, all_pairs_open_for_test):
    """A SELL copy needs a long position on the CHILD's own broker; with none reported it is refused for each broker alike."""
    master, children = await build_strategy(db_session, "alicebluenew", MIXED, "exit-flat")
    clients = {str(c.id): _child_client(f"BRK_{i}") for i, c in enumerate(children)}
    event = _event(master, "EXIT-1", side="SELL", order_type="MARKET", product="INTRADAY", exchange="NSE", symbol="SBIN",
                   qty=10, price="800", trigger="0", token="12345")
    res = await pipeline(mock_redis).execute_replication(event=event, clients=clients)
    for child in children:
        assert clients[str(child.id)].place_order.await_count == 0
        assert res["results"][str(child.id)]["status"] != "SUCCESS"


# ── a Master cancel or modify reaches every Child's still-pending order, on every broker ─────────────────────────────────────

@pytest.mark.asyncio
async def test_a_master_cancel_reaches_every_childs_pending_order_across_brokers(db_session, mock_redis, all_pairs_open_for_test):
    master, children = await build_strategy(db_session, "alicebluenew", MIXED, "cancel")
    clients = {str(c.id): _child_client(f"BRK_{i}") for i, c in enumerate(children)}
    pipe = pipeline(mock_redis)
    await pipe.execute_replication(event=entry(master, "CXL-1", "LIMIT", "0"), clients=clients)
    for c in clients.values():
        c.cancel_order.return_value = _result(OrderResultCategory.SUCCESS, None, "cancelled")
    await pipe.handle_master_cancellation("CXL-1", clients)
    for child in children:
        assert clients[str(child.id)].cancel_order.await_count == 1, child.broker


@pytest.mark.asyncio
async def test_one_childs_failed_cancel_does_not_stop_the_other_cancels(db_session, mock_redis, all_pairs_open_for_test):
    master, children = await build_strategy(db_session, "alicebluenew", MIXED, "cancel-fail")
    clients = {str(c.id): _child_client(f"BRK_{i}") for i, c in enumerate(children)}
    pipe = pipeline(mock_redis)
    await pipe.execute_replication(event=entry(master, "CXL-2", "LIMIT", "0"), clients=clients)
    for c in clients.values():
        c.cancel_order.return_value = _result(OrderResultCategory.SUCCESS, None, "cancelled")
    clients[str(children[1].id)].cancel_order.return_value = _result(OrderResultCategory.REJECTED, None, "already executed")
    await pipe.handle_master_cancellation("CXL-2", clients)
    for child in children:
        assert clients[str(child.id)].cancel_order.await_count == 1


@pytest.mark.asyncio
async def test_a_master_modification_reaches_every_childs_pending_order_across_brokers(db_session, mock_redis, all_pairs_open_for_test):
    from decimal import Decimal
    master, children = await build_strategy(db_session, "alicebluenew", MIXED, "modify")
    clients = {str(c.id): _child_client(f"BRK_{i}") for i, c in enumerate(children)}
    pipe = pipeline(mock_redis)
    await pipe.execute_replication(event=entry(master, "MOD-1", "LIMIT", "0"), clients=clients)
    for c in clients.values():
        c.modify_order.return_value = _result(OrderResultCategory.SUCCESS, None, "modified")
    await pipe.handle_master_modification("MOD-1", Decimal("801"), 10, clients, master_account_id=str(master.id))
    for child in children:
        assert clients[str(child.id)].modify_order.await_count == 1, child.broker


# ── the matrix names every missing requirement and keeps code completeness apart from real-broker verification ─────────────────

def test_pair_requirements_lists_every_gap_not_just_the_first():
    from app.services.brokers.pairs import pair_requirements
    missing = pair_requirements("alicebluenew", "zerodha")
    kinds = {m["requirement"] for m in missing}
    assert {"PAIR_NOT_ALLOWLISTED", "CHILD_ROLE_NOT_OPEN", "EVIDENCE_BELOW_LIVE"} <= kinds
    assert any("zerodha" in m["detail"] and "placement" in m["detail"].lower() or "PLACEMENT" in m["detail"].upper() for m in missing)
    assert pair_requirements("alicebluenew", "alicebluenew") == ()
    assert pair_requirements("nonsense", "groww")[0]["requirement"] == "BROKER_UNKNOWN"


@pytest.mark.asyncio
async def test_the_matrix_separates_code_completeness_from_verification():
    from app.api.brokers import get_broker_matrix
    cells = (await get_broker_matrix())["data"]["matrix"]
    assert len(cells) == 16 and all(c["code_complete"] is True for c in cells)
    verified = {(c["master"], c["child"]) for c in cells if c["real_broker_verified"]}
    assert verified == {("alicebluenew", "alicebluenew")}                         # nothing else is claimed as verified
    for c in cells:
        assert c["supported"] == c["real_broker_verified"]
        assert (c["missing"] == []) == c["supported"]
