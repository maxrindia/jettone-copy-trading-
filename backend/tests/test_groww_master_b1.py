"""
Groww as a MASTER, phase B1: everything that can be proven OFFLINE, with every shipped gate still closed by default.

Nothing here contacts Groww. Groww's HTTP is a stateful fake (an order the test adds to its book is what the next order-list read
reports, standing in for an order placed in the Groww app). Unlike the Child tests there is NO waiver fixture: the real role gates
(`GROWW_*` flags and `GrowwContract.master_verified`) and the real pair gate (allowlist / `CANARY_PAIRS`) decide, so these tests show
what actually has to be switched on - and that nothing is on by default.

What this does NOT show: that orders placed in the Groww app appear in Groww's order list, that a modified order keeps its id, or what an
empty list looks like. Those are live questions (phase B2); until they are answered Groww stays unfit to be enabled as a Master.
"""
import dataclasses
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from app.core.config import Settings, settings
from app.models.accounts import Account, AccountType, BrokerCredential, CopySetting, QuantityMode
from app.models.audit import SystemEvent
from app.services import strategy_switch
from app.services.alice_blue.types import OrderRequest, OrderSide, OrderType, ProductType
from app.services.brokers import pairs, registry
from app.services.brokers.pairs import SUPPORTED_PAIRS, check_pair
from app.services.copy_engine.detector import MasterTradeDetector
from app.services.instruments import terms
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.reliability.master_order_poller import BLIND_ALERT_AFTER_FAILURES, MasterOrderPoller
from app.core.crypto import encrypt_secret
from conftest import TestingSessionLocal
from groww_helpers import make_client, ok, position_row
from strategy_helpers import link_strategy
from test_groww_equity_flow import StatefulGroww
from test_alice_cross_broker_resolution import store_with
from test_groww_equity_lifecycle import CHILD_IP
from test_strategy_master_switching import _Broker, _account, _child_client, _provider

ALICE, GROWW = "alicebluenew", "groww"
BOTH_PAIRS = "groww>alicebluenew,groww>groww"


# ── gates ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def groww_roles_open(monkeypatch):
    """Every ROLE gate Groww needs to be a Master and a Child - and nothing else. `master_verified` is a code constant that only a
    reviewed supervised capture may set; the test replaces the contract object for its own duration."""
    for name in ("GROWW_ENABLED", "GROWW_CHILD_TRADING_ENABLED", "GROWW_MASTER_ENABLED"):
        monkeypatch.setattr(settings, name, True)
    monkeypatch.setattr(settings, "SELL_POSITION_SAFETY_BYPASS", False)
    monkeypatch.setattr(registry, "GROWW_CONTRACT", dataclasses.replace(registry.GROWW_CONTRACT, master_verified=True))


@pytest.fixture
def canary(monkeypatch):
    monkeypatch.setattr(settings, "CANARY_PAIRS", BOTH_PAIRS)


def test_nothing_is_on_by_default():
    s = Settings(_env_file=None)
    assert s.CANARY_PAIRS == "" and (s.GROWW_ENABLED, s.GROWW_CHILD_TRADING_ENABLED, s.GROWW_MASTER_ENABLED) == (False,) * 3
    assert s.ALICE_GROWW_CANARY_ENABLED is False and s.ENGINE_LIVE_TRADING_ENABLED is False and s.SELL_POSITION_SAFETY_BYPASS is False
    assert registry.GROWW_CONTRACT.master_verified is False and registry.listed_canary_pairs() == frozenset()
    assert set(SUPPORTED_PAIRS) == {(ALICE, ALICE)}
    assert registry.can_be_master(GROWW) is False


@pytest.mark.parametrize("value,expected", [
    ("", set()), ("groww>alicebluenew", {(GROWW, ALICE)}), (" Groww > AliceBlueNew ", {(GROWW, ALICE)}),
    ("groww>alicebluenew,groww>groww", {(GROWW, ALICE), (GROWW, GROWW)}), ("kite>groww", {("zerodha", GROWW)}),
    ("groww", set()), (">groww", set()), ("groww>", set()), ("groww>nonsense", set()), ("nonsense>groww", set()),
    ("groww-alicebluenew", set()), ("groww>alicebluenew;groww>groww", set()), (",,", set()), ("groww>>alicebluenew", set()),
])
def test_the_canary_list_opens_only_pairs_it_names_correctly(monkeypatch, value, expected):
    monkeypatch.setattr(settings, "CANARY_PAIRS", value)
    assert registry.listed_canary_pairs() == frozenset(expected)


def test_a_listed_pair_still_needs_every_role_gate(monkeypatch, canary):
    assert check_pair(GROWW, ALICE).detail == f"MASTER_ROLE_NOT_OPEN:{GROWW}"                     # nothing else is open
    for name in ("GROWW_ENABLED", "GROWW_CHILD_TRADING_ENABLED"):
        monkeypatch.setattr(settings, name, True)
    assert check_pair(GROWW, ALICE).detail == f"MASTER_ROLE_NOT_OPEN:{GROWW}"                     # the Master flag is still off
    monkeypatch.setattr(settings, "GROWW_MASTER_ENABLED", True)
    assert check_pair(GROWW, ALICE).detail == f"MASTER_ROLE_NOT_OPEN:{GROWW}"                     # master_verified is still False in code
    monkeypatch.setattr(registry, "GROWW_CONTRACT", dataclasses.replace(registry.GROWW_CONTRACT, master_verified=True))
    decision = check_pair(GROWW, ALICE)
    assert decision.supported is True and "CANARY" in decision.detail
    assert check_pair(GROWW, GROWW).supported is True


def test_a_pair_that_is_not_listed_stays_refused_with_every_gate_open(groww_roles_open, canary):
    for master, child in ((ALICE, GROWW), ("angelone", ALICE), (GROWW, "angelone"), (GROWW, "zerodha"), (ALICE, "zerodha")):
        assert check_pair(master, child).supported is False, (master, child)


def test_without_the_canary_the_open_roles_are_still_not_enough(groww_roles_open):
    assert check_pair(GROWW, ALICE).detail == f"PAIR_NOT_ALLOWLISTED:{GROWW}->{ALICE}"
    assert check_pair(GROWW, GROWW).detail == f"PAIR_NOT_ALLOWLISTED:{GROWW}->{GROWW}"


def test_the_two_named_alice_canaries_still_work_and_still_open_only_their_pair(groww_roles_open, monkeypatch):
    monkeypatch.setattr(settings, "ALICE_GROWW_CANARY_ENABLED", True)
    assert check_pair(ALICE, GROWW).supported is True and check_pair(GROWW, ALICE).supported is False


# ── the Master switch: credentials and the live preflight, for a Groww account ───────────────────────────────────────────────

class MasterGroww(StatefulGroww):
    """A Groww account whose orders are placed OUTSIDE Jettone (as if in the Groww app): the test writes them into the book."""

    async def __call__(self, request):
        if self.session_rejected and request.url.path != "/v1/token/api/access":
            self.requests.append(request)
            return httpx.Response(401, json={"status": "FAILURE"})                              # a lapsed session on EVERY route
        return await super().__call__(request)

    def row(self, oid):
        r = super().row(oid)
        if self.book[oid]["filled"] > 0 and self.book[oid].get("avg") is not None:
            r["average_fill_price"] = float(self.book[oid]["avg"])
        return r

    def app_order(self, oid, *, status="OPEN", side="BUY", otype="LIMIT", price=800, trigger=0, qty=10, filled=0, product="MIS",
                  symbol="SBIN", exchange="NSE", avg=None):
        self.book[oid] = dict(status=status, qty=qty, filled=filled, side=side, otype=otype, price=price, trigger=trigger, symbol=symbol,
                              product=product, ref="APP" + str(oid)[-12:], exchange=exchange, avg=avg)


async def make_groww(db_session, name, *, vault=True, account_type=AccountType.FOLLOWER):
    acc = await _account(db_session, name, account_type, credential=False, copy_setting=account_type != AccountType.MASTER)
    async with TestingSessionLocal() as s:
        row = await s.get(Account, acc.id)
        row.broker, row.static_ip_address = GROWW, CHILD_IP
        if vault:
            s.add(BrokerCredential(account_id=acc.id, broker=GROWW, encrypted_payload=encrypt_secret("{}"), key_version=1))
        await s.commit()
    return acc


async def alice_strategy(db_session, redis, *, groww_child=False):
    """An Alice Master with one Alice Child (and, optionally, a Groww Child), plus a Groww account that is not yet in any strategy."""
    old = await _account(db_session, "old-master", AccountType.MASTER, copy_setting=False)
    child = await _account(db_session, "alice-child")
    children = [child]
    gchild = None
    if groww_child:
        gchild = await make_groww(db_session, "groww-child")
        children.append(gchild)
    new = await make_groww(db_session, "groww-master")
    strategy = await link_strategy(db_session, old, children, name=f"S-{uuid.uuid4().hex[:6]}")
    fake = MasterGroww()
    client = make_client(redis, fake, account_id=str(new.id))
    await client.login()
    brokers = {str(old.id): _Broker(), str(child.id): _Broker(), str(new.id): client}
    child_fake = None
    if gchild is not None:
        child_fake = MasterGroww()
        gclient = make_client(redis, child_fake, account_id=str(gchild.id), static_ip=CHILD_IP)
        await gclient.login()
        brokers[str(gchild.id)] = gclient
    return SimpleNamespace(old=old, child=child, gchild=gchild, new=new, strategy=strategy, fake=fake, child_fake=child_fake, brokers=brokers,
                           strategy_id=strategy.id, new_id=new.id)


async def preflight(db_session, sc, new=None):
    strategy_id, new_id = sc.strategy_id, (new.id if new is not None else sc.new_id)      # read before the objects are expired
    db_session.expire_all()                                   # the accounts were edited through other sessions, as in production
    return await strategy_switch.run_preflight(db_session, strategy_id, new_id, "CHILD", _provider(sc.brokers))


def codes(result):
    return {b.code for b in result.blockers}


@pytest.mark.asyncio
async def test_a_flat_groww_master_with_a_supported_pair_passes_the_preflight(db_session, mock_redis, groww_roles_open, canary):
    sc = await alice_strategy(db_session, mock_redis)
    result = await preflight(db_session, sc)
    assert result.allowed is True and codes(result) == set()


@pytest.mark.asyncio
async def test_a_working_groww_order_blocks_the_switch_and_is_named_groww(db_session, mock_redis, groww_roles_open, canary):
    sc = await alice_strategy(db_session, mock_redis)
    sc.fake.app_order("GAPP-1")
    result = await preflight(db_session, sc)
    blocker = next(b for b in result.blockers if b.code == "PENDING_MASTER_ORDERS")
    assert "Groww" in blocker.message and "Alice Blue" not in blocker.message and result.allowed is False


@pytest.mark.asyncio
async def test_a_finished_groww_order_does_not_block(db_session, mock_redis, groww_roles_open, canary):
    sc = await alice_strategy(db_session, mock_redis)
    sc.fake.app_order("GAPP-1", status="EXECUTED", filled=10, avg=800)
    sc.fake.app_order("GAPP-2", status="CANCELLED")
    assert (await preflight(db_session, sc)).allowed is True


@pytest.mark.asyncio
async def test_an_open_groww_intraday_position_is_a_warning_as_it_is_for_alice(db_session, mock_redis, groww_roles_open, canary):
    sc = await alice_strategy(db_session, mock_redis)
    sc.fake.positions_response = ok({"positions": [position_row(symbol="SBIN", exchange="NSE", product="MIS", qty=5)]})
    result = await preflight(db_session, sc)
    warning = next(w for w in result.warnings if w.code == "INTRADAY_POSITIONS_OPEN")
    assert "Alice Blue" not in warning.message and "OPEN_POSITIONS" not in codes(result) and result.allowed is True


@pytest.mark.asyncio
async def test_a_position_that_cannot_be_classified_blocks_and_names_groww(db_session, mock_redis, groww_roles_open, canary):
    sc = await alice_strategy(db_session, mock_redis)
    sc.fake.positions_response = ok({"positions": [position_row(symbol="SBIN", exchange="NSE", product="NRML", qty=5)]})
    result = await preflight(db_session, sc)
    blocker = next(b for b in result.blockers if b.code == "OPEN_POSITIONS")
    assert "Groww" in blocker.message and "Alice Blue" not in blocker.message


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [(500, {"status": "FAILURE"}), (200, {"status": "SUCCESS", "payload": {"unexpected": []}}),
                                      (200, {"status": "SUCCESS", "payload": {"order_list": [{"groww_order_id": str(i)} for i in range(100)]}})],
                         ids=["http-error", "no-order-list", "full-page-that-never-advances"])
async def test_an_unreadable_groww_book_blocks_and_is_never_read_as_nothing_open(db_session, mock_redis, groww_roles_open, canary, override):
    sc = await alice_strategy(db_session, mock_redis)
    sc.fake.list_override = override
    result = await preflight(db_session, sc)
    blocker = next(b for b in result.blockers if b.code == "BROKER_VERIFICATION_UNAVAILABLE")
    assert "Groww order book could not be fetched" in blocker.message and result.allowed is False


@pytest.mark.asyncio
async def test_an_unreadable_groww_position_book_blocks(db_session, mock_redis, groww_roles_open, canary):
    sc = await alice_strategy(db_session, mock_redis)
    sc.fake.positions_response = (200, {"status": "SUCCESS", "payload": {}})              # no positions array: "unknown", never "flat"
    result = await preflight(db_session, sc)
    assert any(b.code == "BROKER_VERIFICATION_UNAVAILABLE" and "Groww position book" in b.message for b in result.blockers)


@pytest.mark.asyncio
async def test_without_the_canary_the_children_are_blocked_as_an_unsupported_pair(db_session, mock_redis, groww_roles_open):
    sc = await alice_strategy(db_session, mock_redis)
    result = await preflight(db_session, sc)
    assert "BROKER_PAIR_NOT_SUPPORTED" in codes(result) and result.allowed is False


@pytest.mark.asyncio
async def test_with_the_role_gates_closed_a_groww_account_cannot_be_the_new_master(db_session, mock_redis, canary):
    sc = await alice_strategy(db_session, mock_redis)
    with pytest.raises(strategy_switch.SwitchValidationError) as err:
        await preflight(db_session, sc)
    assert err.value.code == "NEW_MASTER_BROKER_UNSUPPORTED"


@pytest.mark.asyncio
async def test_a_groww_master_needs_its_vault_credential_not_an_alice_one(db_session, mock_redis, groww_roles_open, canary):
    sc = await alice_strategy(db_session, mock_redis)
    bare = await make_groww(db_session, "groww-no-credentials", vault=False)
    sc.brokers[str(bare.id)] = sc.brokers[str(sc.new.id)]
    with pytest.raises(strategy_switch.SwitchValidationError) as err:
        await preflight(db_session, sc, new=bare)
    assert err.value.code == "NEW_MASTER_NOT_ELIGIBLE"
    assert (await preflight(db_session, sc)).allowed is True                                # the account WITH a vault row is eligible


@pytest.mark.asyncio
async def test_an_alice_account_still_needs_its_alice_credential(db_session, mock_redis, groww_roles_open):
    sc = await alice_strategy(db_session, mock_redis)
    bare_alice = await _account(db_session, "alice-no-credential", credential=False, copy_setting=False)
    with pytest.raises(strategy_switch.SwitchValidationError) as err:
        await preflight(db_session, sc, new=bare_alice)
    assert err.value.code == "NEW_MASTER_NOT_ELIGIBLE"


@pytest.mark.asyncio
async def test_a_groww_child_in_the_strategy_is_now_verified_not_blanket_blocked(db_session, mock_redis, groww_roles_open, monkeypatch):
    """A strategy with a Groww Child used to be blocked on every switch, because only Alice's raw order book could be read."""
    monkeypatch.setattr(settings, "ALICE_GROWW_CANARY_ENABLED", True)
    sc = await alice_strategy(db_session, mock_redis, groww_child=True)
    arun = await _account(db_session, "arun")
    sc.brokers[str(arun.id)] = _Broker()
    result = await preflight(db_session, sc, new=arun)
    assert "BROKER_VERIFICATION_UNAVAILABLE" not in codes(result) and result.allowed is True


# ── a Master that cannot be read is reported, once ───────────────────────────────────────────────────────────────────────────

async def system_events():
    async with TestingSessionLocal() as s:
        return [e.event_type for e in (await s.execute(select(SystemEvent))).scalars().all()]


async def make_master_poller(db_session, redis, fake=None):
    master = await make_groww(db_session, "groww-poll-master", account_type=AccountType.MASTER)
    fake = fake or MasterGroww()
    client = make_client(redis, fake, account_id=str(master.id))
    await client.login()
    poller = MasterOrderPoller(master_account_id=str(master.id), client_provider=lambda _id: client, on_order_update=lambda e: None,
                               pipeline=CopyEnginePipeline(validator=EventValidator(redis=redis)), check_interval_seconds=5)
    return master, fake, client, poller


@pytest.mark.asyncio
async def test_a_master_that_stays_unreadable_is_alerted_once_and_recovery_is_reported(db_session, mock_redis):
    master, fake, client, poller = await make_master_poller(db_session, mock_redis)
    await poller.poll_once()                                                                  # a good first read: seeded
    fake.list_override = (500, {"status": "FAILURE"})                                         # Groww starts refusing the order list
    for _ in range(BLIND_ALERT_AFTER_FAILURES - 1):
        await poller.poll_once()
    assert "MASTER_MONITOR_BLIND" not in await system_events()                                # not yet
    for _ in range(4):
        await poller.poll_once()
    assert (await system_events()).count("MASTER_MONITOR_BLIND") == 1                         # once, however long it lasts
    fake.list_override = None
    await poller.poll_once()
    assert (await system_events()).count("MASTER_MONITOR_RECOVERED") == 1


@pytest.mark.asyncio
async def test_a_lapsed_groww_session_is_alerted_as_the_master_not_being_watched(db_session, mock_redis):
    master, fake, client, poller = await make_master_poller(db_session, mock_redis)
    await poller.poll_once()
    fake.session_rejected = True                                                              # the daily session ends: every read is refused
    for _ in range(BLIND_ALERT_AFTER_FAILURES):
        await poller.poll_once()
    async with TestingSessionLocal() as s:
        events = (await s.execute(select(SystemEvent).where(SystemEvent.event_type == "MASTER_MONITOR_BLIND"))).scalars().all()
    assert len(events) == 1 and events[0].metadata_payload["reason"] == "GrowwAuthError"


@pytest.mark.asyncio
async def test_a_truncated_order_list_is_alerted_with_its_own_reason(db_session, mock_redis):
    # the fake returns the same 100 rows for every page, so the read is refused as a full page that never advances
    master, fake, client, poller = await make_master_poller(db_session, mock_redis)
    await poller.poll_once()
    fake.list_override = (200, {"status": "SUCCESS", "payload": {"order_list": [{"groww_order_id": str(i)} for i in range(100)]}})
    for _ in range(BLIND_ALERT_AFTER_FAILURES):
        await poller.poll_once()
    async with TestingSessionLocal() as s:
        events = (await s.execute(select(SystemEvent).where(SystemEvent.event_type == "MASTER_MONITOR_BLIND"))).scalars().all()
    assert len(events) == 1 and events[0].metadata_payload["reason"] == "GrowwOrderListTruncated"


@pytest.mark.asyncio
async def test_an_empty_book_that_cannot_be_proven_is_never_a_blind_alert(db_session, mock_redis, monkeypatch):
    """Alice Blue's reader returns [] both for a failure and for a genuinely empty book, so its empty reads are UNKNOWN. A flat Alice
    Master reads that way on every poll and must never be reported as unwatched."""
    from app.services.brokers import order_book_read as obr
    from app.services.reliability import master_order_poller as mop

    master, fake, client, poller = await make_master_poller(db_session, mock_redis)
    await poller.poll_once()                                                                  # seeded from a proven read
    unknown = obr.OrderBookRead(obr.OrderBookState.UNKNOWN, (), obr.EMPTY_NOT_PROVEN, "", False)

    async def not_provable(_client, checked=False):
        return unknown

    monkeypatch.setattr(mop, "read_order_book", not_provable)
    for _ in range(BLIND_ALERT_AFTER_FAILURES * 3):
        await poller.poll_once()
    assert "MASTER_MONITOR_BLIND" not in await system_events()


@pytest.mark.asyncio
async def test_one_failed_poll_in_a_healthy_run_alerts_nothing(db_session, mock_redis):
    master, fake, client, poller = await make_master_poller(db_session, mock_redis)
    await poller.poll_once()
    fake.list_override = (500, {"status": "FAILURE"})
    await poller.poll_once()
    fake.list_override = None
    for _ in range(BLIND_ALERT_AFTER_FAILURES * 2):
        await poller.poll_once()
    assert "MASTER_MONITOR_BLIND" not in await system_events()


# ── end to end: a Groww Master's order -> detector -> pipeline -> Alice and Groww Children ──────────────────────────────────

async def set_multiplier_mode(account):
    async with TestingSessionLocal() as s:
        setting = (await s.execute(select(CopySetting).where(CopySetting.follower_account_id == account.id))).scalar_one()
        setting.quantity_mode = QuantityMode.MULTIPLIER
        await s.commit()


class Chain:
    """One Groww Master (orders written into its fake book), one Alice Child (a mock) and one Groww Child (real adapter, fake HTTP)."""

    async def build(self, db_session, redis, positions_for_alice=None):
        self.master = await make_groww(db_session, "groww-master-e2e", account_type=AccountType.MASTER)
        self.alice = await _account(db_session, "alice-child-e2e")
        self.groww = await make_groww(db_session, "groww-child-e2e")
        for child in (self.alice, self.groww):
            await set_multiplier_mode(child)
        await link_strategy(db_session, self.master, [self.alice, self.groww], name=f"E2E-{uuid.uuid4().hex[:6]}")
        self.master_fake, self.child_fake = MasterGroww(), MasterGroww()
        self.master_client = make_client(redis, self.master_fake, account_id=str(self.master.id))
        self.groww_client = make_client(redis, self.child_fake, account_id=str(self.groww.id), static_ip=CHILD_IP)
        await self.master_client.login()
        await self.groww_client.login()
        self.alice_client = _child_client("ALICE-CHILD-1")
        self.alice_client.cancel_order.return_value = self.alice_client.place_order.return_value
        self.alice_client.modify_order.return_value = self.alice_client.place_order.return_value
        self.child_fake.positions_response = ok({"positions": [position_row(symbol="SBIN", exchange="NSE", product="MIS", qty=50)]})
        self.pipeline = CopyEnginePipeline(validator=EventValidator(redis=redis))
        self.detector = MasterTradeDetector(master_account_id=str(self.master.id))
        self.detector.master_broker = GROWW
        self.clients = {str(self.alice.id): self.alice_client, str(self.groww.id): self.groww_client}
        self.poller = MasterOrderPoller(master_account_id=str(self.master.id), client_provider=lambda _id: self.master_client,
                                        on_order_update=self.on_event, pipeline=self.pipeline, check_interval_seconds=5)
        self.events, self.results = [], []
        await self.poller.poll_once()                                                         # the baseline: nothing is copied
        return self

    async def on_event(self, event):
        trade = self.detector.process_order_update(event)
        if trade is None:
            return
        self.events.append(trade.event_type)
        self.results.append(await self.pipeline.execute_replication(event=trade, clients=self.clients))

    async def poll(self):
        return await self.poller.poll_once()

    def placed_on_alice(self):
        return [c.args[0] for c in self.alice_client.place_order.await_args_list]


@pytest.fixture
def chain_gates(groww_roles_open, canary, monkeypatch):
    """The gates, plus a FRESH Alice contract master: an Alice Child of another broker's Master takes its token only from an exact row of
    Alice's own catalog (the engine loads it at startup) and is refused without one."""
    monkeypatch.setattr(terms, "store", store_with(loaded_at=datetime.now(timezone.utc)))


@pytest.mark.asyncio
async def test_a_groww_limit_order_is_copied_to_an_alice_and_a_groww_child_once(db_session, mock_redis, chain_gates):
    c = await Chain().build(db_session, mock_redis)
    c.master_fake.app_order("GAPP-1", price=800, qty=10, product="MIS")
    assert await c.poll() == 1
    assert await c.poll() == 0                                                                # a repeated poll copies nothing again
    (alice_req,) = c.placed_on_alice()
    assert (alice_req.symbol, alice_req.exchange, alice_req.side, alice_req.quantity, alice_req.order_type) == ("SBIN", "NSE", OrderSide.BUY, 10, OrderType.LIMIT)
    assert alice_req.price == Decimal("800.00") and alice_req.product_type == ProductType.MIS
    assert alice_req.instrument_token == "3045"                                                # Alice's OWN token for SBIN, from Alice's catalog
    (gbody,) = c.child_fake.bodies("/v1/order/create")
    assert gbody["transaction_type"] == "BUY" and gbody["quantity"] == 10 and gbody["order_type"] == "LIMIT" and gbody["price"] == 800.0
    assert gbody["product"] == "MIS" and gbody["trading_symbol"] == "SBIN"
    assert c.events == ["LIMIT_CREATED"]
    assert not c.master_fake.bodies("/v1/order/create") and not c.master_fake.bodies("/v1/order/modify")   # the Master is only ever READ


@pytest.mark.asyncio
async def test_orders_that_existed_before_a_restart_are_baselined_and_never_copied(db_session, mock_redis, chain_gates):
    c = await Chain().build(db_session, mock_redis)
    c.master_fake.app_order("OLD-1", price=800)                                                # placed before the engine restarted
    restarted = MasterOrderPoller(master_account_id=str(c.master.id), client_provider=lambda _id: c.master_client, on_order_update=c.on_event,
                                  pipeline=c.pipeline, check_interval_seconds=5)               # a new poller: its first read is a baseline
    assert await restarted.poll_once() == 0 and await restarted.poll_once() == 0
    assert not c.placed_on_alice() and not c.child_fake.bodies("/v1/order/create")


@pytest.mark.asyncio
async def test_a_groww_market_buy_and_its_sell_exit_are_copied(db_session, mock_redis, chain_gates):
    c = await Chain().build(db_session, mock_redis)
    c.master_fake.app_order("GBUY", status="EXECUTED", otype="MARKET", price=0, qty=10, filled=10, avg=802.5, side="BUY")
    await c.poll()
    assert [b["order_type"] for b in c.child_fake.bodies("/v1/order/create")] == ["LIMIT"]
    assert c.child_fake.bodies("/v1/order/create")[0]["price"] == 802.5                        # a MARKET fill goes out as a LIMIT at its price
    c.master_fake.app_order("GSELL", status="EXECUTED", otype="MARKET", price=0, qty=10, filled=10, avg=803.0, side="SELL")
    await c.poll()
    sells = [b for b in c.child_fake.bodies("/v1/order/create") if b["transaction_type"] == "SELL"]
    assert len(sells) == 1 and sells[0]["price"] == 803.0
    assert len(c.child_fake.bodies("/v1/order/create")) == 2                                   # one BUY, one SELL: no duplicates


@pytest.mark.asyncio
async def test_a_groww_master_modify_and_cancel_reach_both_children(db_session, mock_redis, chain_gates):
    c = await Chain().build(db_session, mock_redis)
    c.master_fake.app_order("GAPP-1", price=800)
    await c.poll()
    c.master_fake.book["GAPP-1"]["price"] = 801                                                # the owner changes the price in the Groww app
    await c.poll()
    assert len(c.child_fake.bodies("/v1/order/modify")) == 1 and c.child_fake.bodies("/v1/order/modify")[0]["price"] == 801.0
    assert c.alice_client.modify_order.await_count == 1
    c.master_fake.book["GAPP-1"]["status"] = "CANCELLED"                                       # then cancels it
    await c.poll()
    assert len(c.child_fake.bodies("/v1/order/cancel")) == 1 and c.alice_client.cancel_order.await_count == 1
    assert len(c.child_fake.bodies("/v1/order/create")) == 1                                   # modified and cancelled in place, never re-created


@pytest.mark.asyncio
async def test_a_part_filled_then_completed_groww_master_order_creates_no_new_child_order(db_session, mock_redis, chain_gates):
    c = await Chain().build(db_session, mock_redis)
    c.master_fake.app_order("GAPP-1", price=800)
    await c.poll()
    c.master_fake.fill("GAPP-1", 4)
    await c.poll()
    c.master_fake.fill("GAPP-1", 10)
    await c.poll()
    assert len(c.child_fake.bodies("/v1/order/create")) == 1 and len(c.placed_on_alice()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type", ["SL", "SL_M"])
async def test_a_groww_master_stop_loss_is_copied_as_a_stop_loss_and_follows_its_cancel(db_session, mock_redis, chain_gates, order_type):
    c = await Chain().build(db_session, mock_redis)
    c.master_fake.app_order("GSL-1", status="TRIGGER_PENDING", otype=order_type, price=0 if order_type == "SL_M" else 799, trigger=795, side="BUY")
    await c.poll()
    (body,) = c.child_fake.bodies("/v1/order/create")
    assert body["order_type"] == order_type and body["trigger_price"] == 795.0 and ("price" in body) == (order_type == "SL")
    c.master_fake.book["GSL-1"]["status"] = "CANCELLED"
    await c.poll()
    assert len(c.child_fake.bodies("/v1/order/cancel")) == 1


@pytest.mark.asyncio
async def test_one_child_failing_does_not_stop_the_other(db_session, mock_redis, chain_gates):
    c = await Chain().build(db_session, mock_redis)
    c.child_fake.create_exception = httpx.ReadTimeout("no answer")                            # the Groww Child's send is ambiguous
    c.master_fake.app_order("GAPP-1", price=800)
    await c.poll()
    assert len(c.placed_on_alice()) == 1                                                       # the Alice Child was still copied
    assert len(c.child_fake.bodies("/v1/order/create")) == 1                                   # and the ambiguous send was not repeated
    await c.poll()
    assert len(c.child_fake.bodies("/v1/order/create")) == 1


@pytest.mark.asyncio
async def test_without_the_canary_a_groww_master_copies_nothing(db_session, mock_redis, groww_roles_open):
    c = await Chain().build(db_session, mock_redis)
    c.master_fake.app_order("GAPP-1", price=800)
    await c.poll()
    assert not c.placed_on_alice() and not c.child_fake.bodies("/v1/order/create")             # every role gate open, still no pair


@pytest.mark.asyncio
async def test_with_the_master_role_closed_nothing_is_copied_even_with_the_canary(db_session, mock_redis, canary, monkeypatch):
    for name in ("GROWW_ENABLED", "GROWW_CHILD_TRADING_ENABLED"):
        monkeypatch.setattr(settings, name, True)
    c = await Chain().build(db_session, mock_redis)
    c.master_fake.app_order("GAPP-1", price=800)
    await c.poll()
    assert not c.placed_on_alice() and not c.child_fake.bodies("/v1/order/create")
