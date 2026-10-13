"""Multi-strategy, strategy-specific Master selection and safe Master switching.

Covers:
- membership rules: one strategy per account, one Master per strategy,
  enforced by the API and by the database itself
- the Master-switch preflight: every blocker, broker verification failure
  (fail closed), and that a blocked switch changes nothing and never
  cancels/places/modifies anything
- strict strategy routing: no matching strategy -> no copy (audited), a
  disabled strategy -> no copy (audited), two strategies with distinct
  Masters/Children never cross-copy
- runtime: one monitor per strategy Master, switching without a restart,
  the activation boundary, events from an old Master after a switch
- manual Place Order uses the selected strategy's Master
- the shared/exclusive switch gate

Brokers are always doubles. ENGINE_LIVE_TRADING_ENABLED is only patched on
inside individual tests; no real broker call is possible.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.strategies import get_switch_client_provider
from app.core.crypto import encrypt_secret
from app.core.database import get_db
from app.core.redis import get_redis
from app.main import app
from app.models.accounts import Account, AccountType, ApiCredential, CopySetting, QuantityMode
from app.models.audit import AuditLog, SystemEvent
from app.models.reconciliation import ReconciliationRecord
from app.models.strategies import GroupStrategy, StrategyAccountRole, StrategyMember
from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderSide, OrderSource, OrderStatus
from app.services import strategy_membership as membership
from app.services import strategy_switch
from app.services.alice_blue.types import (
    FundsData,
    OrderResult,
    OrderResultCategory,
    OrderUpdateEvent,
    Position,
)
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.runtime.engine_loop import EngineLoop
from conftest import TestingSessionLocal
from strategy_helpers import link_strategy

AUTH = {"X-Internal-Token": "test-internal-token"}


# ─── fixtures / builders ────────────────────────────────────────────────────

async def _account(session, name, account_type=AccountType.FOLLOWER, *, credential=True, is_active=True,
                   copy_setting=True):
    acc = Account(
        id=uuid.uuid4(), account_type=account_type, display_name=name,
        alice_user_id=f"AB_{name.upper()}_{uuid.uuid4().hex[:4]}", is_active=is_active,
    )
    session.add(acc)
    await session.flush()
    if credential:
        session.add(ApiCredential(account_id=acc.id, encrypted_api_secret=encrypt_secret("secret")))
    if copy_setting:
        session.add(CopySetting(
            follower_account_id=acc.id, copy_enabled=True, quantity_mode=QuantityMode.FIXED, fixed_qty=1,
        ))
    await session.commit()
    await session.refresh(acc)
    return acc


class _Broker:
    """Read-only broker double for preflight verification. Every write
    method is an AsyncMock so tests can prove none was ever called."""

    def __init__(self, book=None, positions=None, book_error=None, positions_error=None):
        self._book = [] if book is None else book
        self._positions = [] if positions is None else positions
        self._book_error = book_error
        self._positions_error = positions_error
        self.cancel_order = AsyncMock()
        self.place_order = AsyncMock()
        self.modify_order = AsyncMock()

    async def get_order_book_raw(self):
        if self._book_error:
            raise self._book_error
        return self._book

    async def get_positions_or_none(self):
        if self._positions_error:
            raise self._positions_error
        return self._positions


def _provider(brokers):
    return lambda account_id: brokers.get(str(account_id))


def _pos(symbol="RELIANCE-EQ", qty=10, product="MIS"):
    return Position(symbol=symbol, quantity=qty, side="BUY" if qty > 0 else "SELL",
                    average_price=Decimal("100"), account_id="x", exchange="NSE", product_type=product)


def _order(account_id, *, source=OrderSource.BROKER_EXTERNAL, status=OrderStatus.OPEN, broker_order_id=None,
           side=OrderSide.BUY, product="MIS", qty=10, filled=0, master_broker_order_id=None, symbol="RELIANCE"):
    boid = broker_order_id or f"B{uuid.uuid4().hex[:10]}"
    return Order(
        id=uuid.uuid4(), internal_order_id=f"I_{boid}", broker_order_id=boid, account_id=account_id,
        source=source, exchange="NSE", symbol=symbol, side=side, order_type="LIMIT", product_type=product,
        quantity=qty, filled_quantity=filled, pending_quantity=qty - filled, price=Decimal("100"),
        status=status, master_broker_order_id=master_broker_order_id,
    )


@pytest_asyncio.fixture
async def trio(db_session):
    """Dhinesh (created as MASTER) is Master of 'Intraday', Vikash its Child;
    Arun is unassigned."""
    dhinesh = await _account(db_session, "dhinesh", AccountType.MASTER, copy_setting=False)
    vikash = await _account(db_session, "vikash")
    arun = await _account(db_session, "arun")
    strategy = await link_strategy(db_session, dhinesh, [vikash], name="Intraday")
    brokers = {str(a.id): _Broker() for a in (dhinesh, vikash, arun)}
    return SimpleNamespace(dhinesh=dhinesh, vikash=vikash, arun=arun, strategy=strategy, brokers=brokers)


@pytest_asyncio.fixture
async def api_client(mock_redis, db_session):
    async def override_db():
        async with TestingSessionLocal() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    brokers = {}
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_redis] = lambda: mock_redis
    app.dependency_overrides[get_switch_client_provider] = lambda: _provider(brokers)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.brokers = brokers
        yield client
    app.dependency_overrides.clear()


async def _roles(session):
    rows = (await session.execute(select(StrategyAccountRole))).scalars().all()
    return {(r.account_id, r.role, r.strategy_id) for r in rows}


async def _create_with_master(api_client, name, master_id):
    """POST /strategies (no Master) then assign one through switch-master -
    the only way a Master is set. The Master gets a clean broker double."""
    resp = await api_client.post("/api/v1/strategies", json={"name": name}, headers=AUTH)
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]
    api_client.brokers.setdefault(str(master_id), _Broker())
    resp = await api_client.post(f"/api/v1/strategies/{sid}/switch-master",
                                 json={"new_master_account_id": str(master_id)}, headers=AUTH)
    return sid, resp


# ─── 1. membership rules ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_account_cannot_join_a_second_strategy(api_client, db_session, trio):
    other = await _account(db_session, "kumar", AccountType.FOLLOWER)
    normal_id, resp = await _create_with_master(api_client, "Normal", other.id)
    assert resp.status_code == 200, resp.text

    # Vikash is already Intraday's Child.
    resp = await api_client.post(f"/api/v1/strategies/{normal_id}/members",
                                 json={"account_id": str(trio.vikash.id)}, headers=AUTH)
    assert resp.status_code == 409
    assert "Intraday" in resp.json()["detail"]

    # Dhinesh is Intraday's Master: cannot be Normal's Child either.
    resp = await api_client.post(f"/api/v1/strategies/{normal_id}/members",
                                 json={"account_id": str(trio.dhinesh.id)}, headers=AUTH)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_master_cannot_be_added_as_its_own_child(api_client, trio):
    resp = await api_client.post(f"/api/v1/strategies/{trio.strategy.id}/members",
                                 json={"account_id": str(trio.dhinesh.id)}, headers=AUTH)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_never_sets_a_master_without_the_switch_preflight(api_client, trio):
    resp = await api_client.post("/api/v1/strategies", json={"name": "Dup", "master_account_id": str(trio.arun.id)},
                                 headers=AUTH)
    assert resp.status_code == 409
    assert "switch-master" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_assigning_an_account_already_in_a_strategy_as_master_is_rejected(api_client, trio):
    for acc in (trio.dhinesh, trio.vikash):
        _sid, resp = await _create_with_master(api_client, "Dup", acc.id)
        assert resp.status_code == 409, resp.text
        assert "Intraday" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_deleting_a_strategy_releases_its_accounts(api_client, db_session, trio):
    resp = await api_client.delete(f"/api/v1/strategies/{trio.strategy.id}", headers=AUTH)
    assert resp.status_code == 204
    new_id, resp = await _create_with_master(api_client, "Again", trio.vikash.id)
    assert resp.status_code == 200, resp.text
    resp = await api_client.post(f"/api/v1/strategies/{new_id}/members",
                                 json={"account_id": str(trio.dhinesh.id)}, headers=AUTH)
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_generic_put_cannot_change_the_master(api_client, trio):
    resp = await api_client.put(f"/api/v1/strategies/{trio.strategy.id}",
                                json={"master_account_id": str(trio.arun.id)}, headers=AUTH)
    assert resp.status_code == 409
    assert "switch-master" in resp.json()["detail"]
    # Re-sending the current Master (as the UI's full-form saves do) is fine.
    resp = await api_client.put(f"/api/v1/strategies/{trio.strategy.id}",
                                json={"master_account_id": str(trio.dhinesh.id), "name": "Intraday 2"}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Intraday 2"


@pytest.mark.asyncio
async def test_trading_toggle_is_the_single_switch_for_copying_to_a_child(api_client, db_session):
    """Adding a Child with Trading ON creates/updates its CopySetting with
    copy_enabled ON (no hidden second gate -> no FOLLOWER_PAUSED), and the
    Trading toggle keeps it in step afterwards. Other CopySetting fields are
    never touched."""
    master = await _account(db_session, "m1", AccountType.FOLLOWER)
    bare = await _account(db_session, "bare", AccountType.MASTER, copy_setting=False)
    paused = await _account(db_session, "paused")  # has a CopySetting, copy_enabled True, fixed_qty=1
    sid, resp = await _create_with_master(api_client, "S", master.id)
    assert resp.status_code == 200, resp.text
    for acc in (bare, paused):
        resp = await api_client.post(f"/api/v1/strategies/{sid}/members", json={"account_id": str(acc.id)},
                                     headers=AUTH)
        assert resp.status_code == 201

    async def settings_map():
        async with TestingSessionLocal() as s:
            return {c.follower_account_id: c for c in (await s.execute(select(CopySetting))).scalars().all()}

    cs = await settings_map()
    assert cs[bare.id].copy_enabled is True and cs[bare.id].fixed_qty == 1  # created, ON with the toggle
    assert cs[paused.id].copy_enabled is True

    resp = await api_client.put(f"/api/v1/strategies/{sid}/members/{bare.id}",
                                json={"trading_enabled": False}, headers=AUTH)
    assert resp.status_code == 200
    assert (await settings_map())[bare.id].copy_enabled is False   # toggle OFF -> copying OFF
    resp = await api_client.put(f"/api/v1/strategies/{sid}/members/{bare.id}",
                                json={"trading_enabled": True}, headers=AUTH)
    assert (await settings_map())[bare.id].copy_enabled is True    # toggle ON -> copying ON
    resp = await api_client.put(f"/api/v1/strategies/{sid}/members/{bare.id}",
                                json={"multiplier": "2.00"}, headers=AUTH)
    assert resp.status_code == 200
    assert (await settings_map())[bare.id].copy_enabled is True    # unrelated edit leaves it alone


@pytest.mark.asyncio
async def test_added_child_with_trading_off_does_not_copy(api_client, db_session):
    master = await _account(db_session, "m2")
    child = await _account(db_session, "off", copy_setting=False)
    sid, resp = await _create_with_master(api_client, "S2", master.id)
    resp = await api_client.post(f"/api/v1/strategies/{sid}/members",
                                 json={"account_id": str(child.id), "trading_enabled": False}, headers=AUTH)
    assert resp.status_code == 201
    async with TestingSessionLocal() as s:
        cs = (await s.execute(select(CopySetting).where(CopySetting.follower_account_id == child.id))).scalar_one()
    assert cs.copy_enabled is False


@pytest.mark.asyncio
async def test_prod_scenario_new_master_order_copies_to_child_added_via_api(api_client, db_session, mock_redis):
    """The case that failed live: Vikash (Master) places an order, Dhines
    (a Child created as account_type MASTER with no CopySetting, added with
    Trading ON) must receive the copy - not FOLLOWER_PAUSED."""
    vikash = await _account(db_session, "vikash")
    dhines = await _account(db_session, "dhines", AccountType.MASTER, copy_setting=False)
    sid, resp = await _create_with_master(api_client, "master", vikash.id)
    assert resp.status_code == 200, resp.text
    resp = await api_client.post(f"/api/v1/strategies/{sid}/members", json={"account_id": str(dhines.id)},
                                 headers=AUTH)
    assert resp.status_code == 201
    client = _child_client("BRK_DHINES")
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=_limit_event(vikash, "ORD_V"), clients={str(dhines.id): client})
    assert res["results"][str(dhines.id)]["status"] == "SUCCESS", res
    client.place_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_removing_a_child_with_a_working_copied_order_is_blocked(api_client, db_session, trio):
    db_session.add(_order(trio.vikash.id, source=OrderSource.SYSTEM, status=OrderStatus.OPEN))
    await db_session.commit()
    resp = await api_client.delete(f"/api/v1/strategies/{trio.strategy.id}/members/{trio.vikash.id}", headers=AUTH)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_database_enforces_one_strategy_per_account_and_one_master(db_session, trio):
    strategy_id, vikash_id, arun_id = trio.strategy.id, trio.vikash.id, trio.arun.id
    other_id = uuid.uuid4()
    db_session.add(GroupStrategy(id=other_id, name="Other"))
    await db_session.commit()

    db_session.add(StrategyAccountRole(strategy_id=other_id, account_id=vikash_id, role="CHILD"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()

    db_session.add(StrategyAccountRole(strategy_id=strategy_id, account_id=arun_id, role="MASTER"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


# ─── 2. preflight + switch ─────────────────────────────────────────────────

async def _preflight(db_session, trio, new_master, action="CHILD"):
    return await strategy_switch.run_preflight(
        db_session, trio.strategy.id, new_master.id, action, _provider(trio.brokers)
    )


def _codes(preflight):
    return {(b.code, b.account_id) for b in preflight.blockers}


@pytest.mark.asyncio
async def test_clean_switch_to_child_with_old_master_kept_as_child(db_session, trio):
    pre = await _preflight(db_session, trio, trio.vikash)
    assert pre.allowed and pre.blockers == []
    assert {a["role"] for a in pre.checked_accounts} == {"CURRENT_MASTER", "NEW_MASTER"}

    synced = AsyncMock()
    await strategy_switch.execute_switch(
        db_session, trio.strategy.id, trio.vikash.id, "CHILD", _provider(trio.brokers), after_commit=synced,
    )
    synced.assert_awaited_once()

    async with TestingSessionLocal() as s:
        strat = (await s.execute(select(GroupStrategy).where(GroupStrategy.id == trio.strategy.id))).scalar_one()
        members = (await s.execute(select(StrategyMember).where(StrategyMember.strategy_id == strat.id))).scalars().all()
        cs = (await s.execute(select(CopySetting).where(CopySetting.follower_account_id == trio.dhinesh.id))).scalar_one()
        roles = await _roles(s)
    assert strat.master_account_id == trio.vikash.id
    assert {m.account_id: m.trading_enabled for m in members} == {trio.dhinesh.id: True}  # Trading ON
    assert cs.copy_enabled is True
    assert roles == {(trio.vikash.id, "MASTER", strat.id), (trio.dhinesh.id, "CHILD", strat.id)}


@pytest.mark.asyncio
async def test_switch_with_remove_frees_old_master_for_another_strategy(api_client, db_session, trio):
    await strategy_switch.execute_switch(db_session, trio.strategy.id, trio.arun.id, "REMOVE", _provider(trio.brokers))
    async with TestingSessionLocal() as s:
        roles = await _roles(s)
    assert (trio.dhinesh.id, "MASTER", trio.strategy.id) not in roles
    assert all(acc != trio.dhinesh.id for acc, _r, _s in roles)
    _sid, resp = await _create_with_master(api_client, "Normal", trio.dhinesh.id)
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_switch_back_and_forth_dhinesh_vikash_dhinesh(db_session, trio):
    provider = _provider(trio.brokers)
    await strategy_switch.execute_switch(db_session, trio.strategy.id, trio.vikash.id, "CHILD", provider)
    await strategy_switch.execute_switch(db_session, trio.strategy.id, trio.dhinesh.id, "CHILD", provider)
    async with TestingSessionLocal() as s:
        strat = (await s.execute(select(GroupStrategy).where(GroupStrategy.id == trio.strategy.id))).scalar_one()
        roles = await _roles(s)
    assert strat.master_account_id == trio.dhinesh.id
    assert roles == {(trio.dhinesh.id, "MASTER", strat.id), (trio.vikash.id, "CHILD", strat.id)}


@pytest.mark.asyncio
async def test_switch_request_validation(db_session, trio):
    other = await link_strategy(db_session, await _account(db_session, "kumar"), [], name="Normal")
    inactive = await _account(db_session, "sleepy", is_active=False)
    no_cred = await _account(db_session, "nocred", credential=False)
    cases = [
        (trio.dhinesh.id, "CHILD", 400, "SAME_MASTER"),
        (other.master_account_id, "CHILD", 409, "ACCOUNT_ALREADY_ASSIGNED"),
        (inactive.id, "CHILD", 409, "NEW_MASTER_NOT_ELIGIBLE"),
        (no_cred.id, "CHILD", 409, "NEW_MASTER_NOT_ELIGIBLE"),
        (trio.arun.id, "KEEP", 422, "INVALID_OLD_MASTER_ACTION"),
    ]
    for new_id, action, code, err in cases:
        with pytest.raises(strategy_switch.SwitchValidationError) as exc:
            await strategy_switch.run_preflight(db_session, trio.strategy.id, new_id, action, _provider(trio.brokers))
        assert (exc.value.status_code, exc.value.code) == (code, err)


@pytest.mark.asyncio
async def test_blocker_pending_master_orders_in_db_and_at_broker(db_session, trio):
    db_session.add(_order(trio.dhinesh.id, status=OrderStatus.PARTIAL, filled=4))
    await db_session.commit()
    trio.brokers[str(trio.vikash.id)]._book = [{"brokerOrderId": "X1", "orderStatus": "OPEN", "tradingSymbol": "ITC"}]
    pre = await _preflight(db_session, trio, trio.vikash)
    assert not pre.allowed
    assert ("PENDING_MASTER_ORDERS", str(trio.dhinesh.id)) in _codes(pre)
    # The NEW Master's working order at the broker blocks too: it would be
    # baselined and never copied.
    assert ("PENDING_MASTER_ORDERS", str(trio.vikash.id)) in _codes(pre)


@pytest.mark.asyncio
async def test_unknown_broker_status_counts_as_working(db_session, trio):
    trio.brokers[str(trio.dhinesh.id)]._book = [{"brokerOrderId": "X2", "orderStatus": "AMO REQ RECEIVED"}]
    pre = await _preflight(db_session, trio, trio.vikash)
    assert ("PENDING_MASTER_ORDERS", str(trio.dhinesh.id)) in _codes(pre)


@pytest.mark.asyncio
async def test_blocker_pending_and_partially_filled_child_orders(db_session, trio):
    db_session.add(_order(trio.vikash.id, source=OrderSource.SYSTEM, status=OrderStatus.PARTIAL, filled=3,
                          broker_order_id="C1", master_broker_order_id="M1"))
    await db_session.commit()
    pre = await _preflight(db_session, trio, trio.arun)
    blocker = next(b for b in pre.blockers if b.code == "PENDING_CHILD_ORDERS")
    assert blocker.account_id == str(trio.vikash.id) and blocker.role == "CHILD"
    assert "1 partially filled" in blocker.message


@pytest.mark.asyncio
async def test_blocker_child_order_still_working_at_broker_after_db_says_done(db_session, trio):
    db_session.add(_order(trio.vikash.id, source=OrderSource.SYSTEM, status=OrderStatus.COMPLETE, filled=10,
                          broker_order_id="C2", master_broker_order_id="M2"))
    await db_session.commit()
    trio.brokers[str(trio.vikash.id)]._book = [
        {"brokerOrderId": "C2", "orderStatus": "OPEN"},
        {"brokerOrderId": "OWN_MANUAL", "orderStatus": "OPEN"},  # not a Jettone copy: ignored
    ]
    pre = await _preflight(db_session, trio, trio.arun)
    child = [b for b in pre.blockers if b.code == "PENDING_CHILD_ORDERS"]
    assert len(child) == 1 and child[0].count == 1 and child[0].items[0].startswith("C2")


@pytest.mark.asyncio
async def test_copy_in_flight_blocks_but_reconciliation_only_warns(db_session, trio):
    db_session.add(CopyTrade(
        id=uuid.uuid4(), master_order_id="M3", follower_account_id=trio.vikash.id, copy_event_id=uuid.uuid4(),
        symbol="ITC", exchange="NSE", side=OrderSide.BUY, quantity=1, product_type="MIS",
        status=CopyTradeStatus.SUBMITTED,
    ))
    db_session.add(CopyTrade(
        id=uuid.uuid4(), master_order_id="M4", follower_account_id=trio.vikash.id, copy_event_id=uuid.uuid4(),
        symbol="ITC", exchange="NSE", side=OrderSide.BUY, quantity=1, product_type="MIS",
        status=CopyTradeStatus.RECONCILIATION_REQUIRED,
    ))
    await db_session.commit()
    pre = await _preflight(db_session, trio, trio.arun)
    assert ("COPY_IN_FLIGHT", str(trio.vikash.id)) in _codes(pre)  # a placement still running: real, blocks
    assert ("RECONCILIATION_UNRESOLVED", str(trio.vikash.id)) not in _codes(pre)
    assert [w.code for w in pre.warnings] == ["RECONCILIATION_UNRESOLVED"]  # reconciliation: advisory only


@pytest.mark.asyncio
async def test_old_reconciliation_items_warn_but_do_not_block(db_session, trio):
    rec = ReconciliationRecord(account_id=trio.vikash.id, symbol="ITC", expected_qty=1, actual_qty=0, discrepancy_qty=1)
    db_session.add(rec)
    await db_session.commit()
    rec.created_at = datetime.now(timezone.utc) - timedelta(days=3)
    await db_session.commit()
    pre = await _preflight(db_session, trio, trio.arun)
    assert pre.allowed
    assert [w.code for w in pre.warnings] == ["RECONCILIATION_HISTORY"]


@pytest.mark.asyncio
async def test_open_positions_block_and_nothing_is_squared_off_or_cancelled(db_session, trio):
    trio.brokers[str(trio.vikash.id)]._positions = [_pos(qty=10), _pos(symbol="ITC-EQ", qty=0)]
    with pytest.raises(strategy_switch.SwitchBlocked) as exc:
        await strategy_switch.execute_switch(db_session, trio.strategy.id, trio.arun.id, "CHILD",
                                             _provider(trio.brokers))
    blockers = exc.value.preflight.blockers
    assert [(b.code, b.count) for b in blockers] == [("OPEN_POSITIONS", 1)]  # flat ITC row ignored
    for broker in trio.brokers.values():
        broker.place_order.assert_not_called()
        broker.cancel_order.assert_not_called()
        broker.modify_order.assert_not_called()
    async with TestingSessionLocal() as s:
        strat = (await s.execute(select(GroupStrategy).where(GroupStrategy.id == trio.strategy.id))).scalar_one()
        assert strat.master_account_id == trio.dhinesh.id  # unchanged
        assert await _roles(s) == {(trio.dhinesh.id, "MASTER", strat.id), (trio.vikash.id, "CHILD", strat.id)}


@pytest.mark.asyncio
async def test_carry_forward_quantity_in_records_only_warns(db_session, trio):
    db_session.add(_order(trio.vikash.id, source=OrderSource.SYSTEM, status=OrderStatus.COMPLETE, filled=5,
                          product="CNC", broker_order_id="C5"))
    db_session.add(_order(trio.vikash.id, source=OrderSource.SYSTEM, status=OrderStatus.COMPLETE, filled=5,
                          product="MIS", broker_order_id="C6"))  # intraday: not a carry position
    await db_session.commit()
    pre = await _preflight(db_session, trio, trio.arun)
    assert pre.allowed and pre.blockers == []   # records-based totals are advisory
    carry = [w for w in pre.warnings if w.code == "CARRY_POSITION_UNVERIFIED"]
    assert len(carry) == 1 and carry[0].items == ["RELIANCE CNC net +5"]
    assert pre.limitations  # the unverified-holdings limitation is always stated


@pytest.mark.asyncio
async def test_closed_carry_quantity_does_not_block(db_session, trio):
    db_session.add(_order(trio.vikash.id, source=OrderSource.SYSTEM, status=OrderStatus.COMPLETE, filled=5,
                          product="LONGTERM", broker_order_id="C7"))
    db_session.add(_order(trio.vikash.id, source=OrderSource.SYSTEM, status=OrderStatus.COMPLETE, filled=5,
                          product="CNC", side=OrderSide.SELL, broker_order_id="C8"))
    await db_session.commit()
    pre = await _preflight(db_session, trio, trio.arun)
    assert pre.allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["no_client", "book_none", "book_raises", "positions_none", "positions_raise"])
async def test_broker_verification_failure_fails_closed(db_session, trio, failure):
    key = str(trio.vikash.id)
    if failure == "no_client":
        trio.brokers.pop(key)
    elif failure == "book_none":
        trio.brokers[key]._book = None
        trio.brokers[key].get_order_book_raw = AsyncMock(return_value=None)
    elif failure == "book_raises":
        trio.brokers[key]._book_error = RuntimeError("timeout")
    elif failure == "positions_none":
        trio.brokers[key].get_positions_or_none = AsyncMock(return_value=None)
    else:
        trio.brokers[key]._positions_error = RuntimeError("502")
    pre = await _preflight(db_session, trio, trio.arun)
    assert not pre.allowed
    assert ("BROKER_VERIFICATION_UNAVAILABLE", key) in _codes(pre)


@pytest.mark.asyncio
async def test_preflight_and_switch_endpoints(api_client, trio):
    for acc in (trio.dhinesh, trio.vikash, trio.arun):
        api_client.brokers[str(acc.id)] = _Broker()
    api_client.brokers[str(trio.vikash.id)]._positions = [_pos(qty=-5)]

    resp = await api_client.get(
        f"/api/v1/strategies/{trio.strategy.id}/switch-master/preflight",
        params={"new_master_account_id": str(trio.arun.id), "old_master_action": "CHILD"}, headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["allowed"] is False
    assert body["blockers"][0]["code"] == "OPEN_POSITIONS"
    assert body["blockers"][0]["account_name"] == "vikash"

    resp = await api_client.post(f"/api/v1/strategies/{trio.strategy.id}/switch-master",
                                 json={"new_master_account_id": str(trio.arun.id), "old_master_action": "CHILD"},
                                 headers=AUTH)
    assert resp.status_code == 409
    assert "open position" in resp.json()["detail"]

    api_client.brokers[str(trio.vikash.id)]._positions = []
    resp = await api_client.post(f"/api/v1/strategies/{trio.strategy.id}/switch-master",
                                 json={"new_master_account_id": str(trio.arun.id), "old_master_action": "CHILD"},
                                 headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["strategy"]["master_account_id"] == str(trio.arun.id)
    assert resp.json()["monitor_ready"] is False  # no engine running in tests


# ─── 3. strategy routing ───────────────────────────────────────────────────

def _funds():
    return FundsData(account_id="f", available_margin=Decimal("1000000"), collateral=Decimal("0"),
                     used_margin=Decimal("0"), updated_at=datetime.now(timezone.utc))


def _child_client(boid):
    c = AsyncMock()
    c.get_funds.return_value = _funds()
    c.get_positions.return_value = []
    c.get_positions_or_none.return_value = []
    c.place_order.return_value = OrderResult(
        category=OrderResultCategory.SUCCESS, client_order_id=None, broker_order_id=boid, message="ok",
        raw_response={}, timestamp=datetime.now(timezone.utc),
    )
    return c


def _limit_event(master, order_id):
    return MasterTradeEvent(
        master_order_id=order_id, symbol="RELIANCE", exchange="NSE", side="BUY", quantity=10,
        price=Decimal("100.00"), limit_price=Decimal("100.00"), product_type="MIS",
        timestamp=datetime.now(timezone.utc), account_id=str(master.id), order_type="LIMIT",
        event_type="LIMIT_CREATED", status="OPEN", total_quantity=10, pending_quantity=10,
    )


@pytest.mark.asyncio
async def test_two_strategies_with_distinct_masters_never_cross_copy(db_session, mock_redis):
    arun = await _account(db_session, "arun")
    dhinesh = await _account(db_session, "dhinesh")
    n1, n2 = await _account(db_session, "n1"), await _account(db_session, "n2")
    i1 = await _account(db_session, "i1")
    await link_strategy(db_session, arun, [n1, n2], name="Normal")
    await link_strategy(db_session, dhinesh, [i1], name="Intraday")
    clients = {str(a.id): _child_client(f"BRK_{a.display_name}") for a in (arun, dhinesh, n1, n2, i1)}
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))

    # stagger_ms: same reason as test_scale_load - two concurrent child
    # placements on the test suite's single shared SQLite connection can
    # collide ("cannot commit transaction"); Postgres has no such limit.
    res_normal = await pipeline.execute_replication(event=_limit_event(arun, "ORD_ARUN"), clients=clients,
                                                    stagger_ms=200)
    assert set(res_normal["results"]) == {str(n1.id), str(n2.id)}
    res_intraday = await pipeline.execute_replication(event=_limit_event(dhinesh, "ORD_DHINESH"), clients=clients)
    assert set(res_intraday["results"]) == {str(i1.id)}

    for acc in (n1, n2, i1):
        assert clients[str(acc.id)].place_order.await_count == 1, (res_normal, res_intraday)
    clients[str(arun.id)].place_order.assert_not_called()
    clients[str(dhinesh.id)].place_order.assert_not_called()


@pytest.mark.asyncio
async def test_no_matching_strategy_copies_nothing_and_is_audited(db_session, mock_redis):
    lone = await _account(db_session, "lone", AccountType.MASTER)
    follower = await _account(db_session, "follower")  # active FOLLOWER the old fallback would have used
    client = _child_client("BRK_X")
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))

    res = await pipeline.execute_replication(event=_limit_event(lone, "ORD_LONE"), clients={str(follower.id): client})
    assert res == {"status": "no_followers"}
    client.place_order.assert_not_called()
    audits = (await db_session.execute(select(AuditLog))).scalars().all()
    assert [(a.event_type, a.reason_code, a.master_order_id) for a in audits] == [
        ("COPY_NOT_ROUTED", "NO_MATCHING_STRATEGY", "ORD_LONE")
    ]


@pytest.mark.asyncio
async def test_disabled_strategy_copies_nothing_and_is_audited(db_session, mock_redis):
    master = await _account(db_session, "m")
    child = await _account(db_session, "c")
    await link_strategy(db_session, master, [child], trading_enabled=False)
    client = _child_client("BRK_Y")
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=_limit_event(master, "ORD_OFF"), clients={str(child.id): client})
    assert res == {"status": "no_followers"}
    client.place_order.assert_not_called()
    events = (await db_session.execute(select(SystemEvent.event_type))).scalars().all()
    assert "STRATEGY_TRADING_DISABLED" in events


@pytest.mark.asyncio
async def test_switch_never_reverses_copy_direction(db_session, mock_redis, trio):
    """The bug that started this: after making Vikash the Master, an order
    from Dhinesh (now a Child) must reach nobody - not Vikash."""
    await strategy_switch.execute_switch(db_session, trio.strategy.id, trio.vikash.id, "CHILD",
                                         _provider(trio.brokers))
    clients = {str(trio.vikash.id): _child_client("BRK_V"), str(trio.dhinesh.id): _child_client("BRK_D")}
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))

    res = await pipeline.execute_replication(event=_limit_event(trio.dhinesh, "ORD_FROM_OLD"), clients=clients)
    assert res == {"status": "no_followers"}
    clients[str(trio.vikash.id)].place_order.assert_not_called()

    # The old Master was kept as a Child with Trading ON, so the new Master's
    # order reaches it straight away - the direction is Vikash -> Dhinesh only.
    res = await pipeline.execute_replication(event=_limit_event(trio.vikash, "ORD_FROM_NEW_1"), clients=clients)
    assert set(res["results"]) == {str(trio.dhinesh.id)}
    clients[str(trio.dhinesh.id)].place_order.assert_awaited_once()
    clients[str(trio.vikash.id)].place_order.assert_not_called()

    # ...and switching the Child's Trading toggle OFF stops it.
    member = (await db_session.execute(select(StrategyMember).where(
        StrategyMember.account_id == trio.dhinesh.id))).scalar_one()
    member.trading_enabled = False
    await db_session.commit()
    res = await pipeline.execute_replication(event=_limit_event(trio.vikash, "ORD_FROM_NEW_2"), clients=clients)
    assert res == {"status": "no_followers"}
    clients[str(trio.dhinesh.id)].place_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_modification_of_existing_child_keeps_copy_setting_when_strategy_disabled(db_session, mock_redis):
    """Managing an already-copied order is unchanged by strict routing: with
    the strategy's trading switched off, a master modification still resolves
    the child's CopySetting (FIXED qty) rather than falling to 1.0x."""
    master = await _account(db_session, "m", copy_setting=False)
    child = await _account(db_session, "c", copy_setting=False)
    db_session.add(CopySetting(follower_account_id=child.id, copy_enabled=True, quantity_mode=QuantityMode.FIXED,
                               fixed_qty=7))
    await link_strategy(db_session, master, [child], trading_enabled=False)
    db_session.add(_order(child.id, source=OrderSource.SYSTEM, status=OrderStatus.OPEN, qty=7,
                          broker_order_id="CH_MOD", master_broker_order_id="ORD_MOD"))
    await db_session.commit()
    client = _child_client("CH_MOD")
    client.modify_order.return_value = OrderResult(
        category=OrderResultCategory.SUCCESS, client_order_id=None, broker_order_id="CH_MOD", message="ok",
        raw_response={}, timestamp=datetime.now(timezone.utc),
    )
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await pipeline.handle_master_modification(
        master_order_id="ORD_MOD", new_price=Decimal("101"), new_quantity=50,
        clients={str(child.id): client}, master_account_id=str(master.id),
    )
    client.modify_order.assert_awaited_once()
    assert client.modify_order.call_args.kwargs["quantity"] == 7


# ─── 4. runtime monitors ───────────────────────────────────────────────────

class _FakePoller:
    instances = []

    def __init__(self, master_account_id, **kwargs):
        self.master_account_id = master_account_id
        self.started = False
        self.stopped = False
        self.is_seeded = True
        _FakePoller.instances.append(self)

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


class _FakeWebSocketClient:
    def __init__(self, client):
        self.on_order_update = None

    async def connect_and_listen(self):
        return
        yield  # pragma: no cover

    async def close(self):
        pass


def _engine_patches(legacy_master_id, detectors, pipeline_instance, all_clients=None):
    fake_am = MagicMock()
    fake_am.initialize = AsyncMock()
    fake_am.authenticate_all = AsyncMock(return_value={})
    fake_am.get_master_client = MagicMock(
        return_value=SimpleNamespace(account_id=legacy_master_id) if legacy_master_id else None
    )
    fake_am.get_client = MagicMock(return_value=None)
    fake_am.get_clients_map_excluding = MagicMock(
        side_effect=lambda acc: {k: v for k, v in (all_clients or {}).items() if k != acc}
    )

    def detector_factory(master_account_id):
        d = MagicMock()
        d.account = master_account_id
        d.process_order_update = MagicMock(return_value=None)
        detectors[master_account_id] = d
        return d

    _FakePoller.instances = []
    return patch.multiple(
        "app.services.runtime.engine_loop",
        account_manager=fake_am,
        MasterTradeDetector=MagicMock(side_effect=detector_factory),
        CopyEnginePipeline=MagicMock(return_value=pipeline_instance),
        AliceBlueWebSocketClient=_FakeWebSocketClient,
        load_and_cache_contract_master=AsyncMock(),
        ReconciliationEngine=MagicMock(run_crash_recovery=AsyncMock(return_value=0)),
        StaleOrderSweeper=MagicMock(return_value=MagicMock(start=AsyncMock(), stop=AsyncMock())),
        FollowerFillSyncer=MagicMock(return_value=MagicMock(start=AsyncMock(), stop=AsyncMock())),
        MasterOrderPoller=_FakePoller,
    )


def _ws_event(account_id, boid="B1"):
    return OrderUpdateEvent(
        broker_order_id=boid, client_order_id=None, symbol="RELIANCE", side="BUY", quantity=10,
        filled_quantity=0, fill_price=Decimal("0"), status="OPEN", timestamp=datetime.now(timezone.utc),
        account_id=str(account_id), product_type="MIS", order_type="LIMIT", price=Decimal("100"),
        trigger_price=Decimal("0"), rejection_reason=None, exchange="NSE",
    )


async def _drain():
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending)


@pytest.mark.asyncio
async def test_every_strategy_master_is_monitored_and_switching_resyncs_only_that_one(db_session, mock_redis):
    legacy = await _account(db_session, "legacy", AccountType.MASTER)
    arun = await _account(db_session, "arun")
    dhinesh = await _account(db_session, "dhinesh")
    vikash = await _account(db_session, "vikash")
    child_n = await _account(db_session, "childn")
    await link_strategy(db_session, arun, [child_n], name="Normal")
    intraday = await link_strategy(db_session, dhinesh, [vikash], name="Intraday")
    legacy_id, arun_id, dhinesh_id, vikash_id = (str(a.id) for a in (legacy, arun, dhinesh, vikash))
    intraday_id = intraday.id

    detectors = {}
    with _engine_patches(legacy_id, detectors, MagicMock(save_or_update_order=AsyncMock())):
        loop = EngineLoop(redis_client=mock_redis)
        await loop._run()
        assert loop.monitored_master_ids() == {legacy_id, arun_id, dhinesh_id}
        arun_poller = loop._monitors[arun_id].poller

        await strategy_switch.execute_switch(
            db_session, intraday_id, uuid.UUID(vikash_id), "CHILD", lambda _a: _Broker(),
            after_commit=lambda pf: loop.sync_master_monitors(seed_timeout_seconds=0, activations=pf.activations()),
        )
        assert loop.monitored_master_ids() == {legacy_id, arun_id, vikash_id}
        assert loop._monitors[arun_id].poller is arun_poller and not arun_poller.stopped
        dhinesh_poller = next(p for p in _FakePoller.instances if p.master_account_id == dhinesh_id)
        assert dhinesh_poller.stopped
        assert loop.is_master_monitor_ready(vikash_id)
        await loop.stop()


@pytest.mark.asyncio
async def test_legacy_master_is_not_monitored_while_it_is_a_strategy_child(db_session, mock_redis):
    legacy = await _account(db_session, "dhinesh", AccountType.MASTER)
    vikash = await _account(db_session, "vikash")
    await link_strategy(db_session, vikash, [legacy], name="Intraday")
    detectors = {}
    pipeline_instance = MagicMock(save_or_update_order=AsyncMock())
    with _engine_patches(str(legacy.id), detectors, pipeline_instance):
        loop = EngineLoop(redis_client=mock_redis)
        await loop._run()
        assert loop.monitored_master_ids() == {str(vikash.id)}
        # Its own order stream is Jettone's copies: not even ingested as a Master's.
        loop._ws_client.on_order_update(_ws_event(legacy.id))
        await _drain()
        pipeline_instance.save_or_update_order.assert_not_called()
        await loop.stop()


@pytest.mark.asyncio
async def test_old_master_events_after_switch_are_not_replicated_new_master_events_are(
    db_session, mock_redis, monkeypatch, trio
):
    monkeypatch.setattr("app.services.runtime.engine_loop.settings.ENGINE_LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr("app.services.runtime.engine_loop.is_master_trading_enabled", AsyncMock(return_value=True))
    detectors = {}
    trade = MagicMock(event_type="LIMIT_CREATED", master_order_id="X", symbol="RELIANCE", side="BUY", quantity=10)
    pipeline_instance = MagicMock(save_or_update_order=AsyncMock(),
                                  execute_replication=AsyncMock(return_value={"status": "completed"}))
    all_clients = {str(trio.dhinesh.id): object(), str(trio.vikash.id): object()}
    with _engine_patches(str(trio.dhinesh.id), detectors, pipeline_instance, all_clients):
        loop = EngineLoop(redis_client=mock_redis)
        await loop._run()
        old_detector = detectors[str(trio.dhinesh.id)]
        old_detector.process_order_update.return_value = trade

        # An event for the old Master arrives while the switch holds the gate...
        async with membership.accounts_switch_lock([str(trio.dhinesh.id), str(trio.vikash.id)]):
            loop._ws_client.on_order_update(_ws_event(trio.dhinesh.id, "DURING"))
            await asyncio.sleep(0.05)
            old_detector.process_order_update.assert_not_called()  # held back by the gate
            # ...the switch commits and monitoring re-syncs inside the gate.
            async with TestingSessionLocal() as s:
                strat = await s.get(GroupStrategy, trio.strategy.id)
                await s.execute(StrategyAccountRole.__table__.delete())
                await s.execute(StrategyMember.__table__.delete())
                strat.master_account_id = trio.vikash.id
                s.add(StrategyMember(strategy_id=strat.id, account_id=trio.dhinesh.id))
                await s.commit()
            await loop.sync_master_monitors(seed_timeout_seconds=0)
        await _drain()
        old_detector.process_order_update.assert_not_called()
        pipeline_instance.execute_replication.assert_not_called()

        new_detector = detectors[str(trio.vikash.id)]
        new_detector.process_order_update.return_value = trade
        loop._monitors[str(trio.vikash.id)].poller.is_seeded = True
        # Events reach the new Master's monitor through the same callback the poller uses.
        loop._on_order_update(_ws_event(trio.vikash.id, "AFTER"))
        await _drain()
        new_detector.process_order_update.assert_called_once()
        call = pipeline_instance.execute_replication.call_args
        assert set(call.kwargs["clients"]) == {str(trio.dhinesh.id)}  # never the source itself
        await loop.stop()


@pytest.mark.asyncio
async def test_new_master_activation_boundary_and_jettone_orders_not_recopied(db_session, mock_redis, trio):
    """Real poller: everything already on the new Master's order book at
    activation (including Jettone's own child copies from when it was a
    Child) is persisted but never dispatched."""
    from app.services.alice_blue.types import OrderRecord
    from app.services.reliability.master_order_poller import MasterOrderPoller

    def rec(boid, status="OPEN"):
        return OrderRecord(broker_order_id=boid, client_order_id=None, symbol="ITC", exchange="NSE",
                           side=OrderSide.BUY, quantity=1, filled_quantity=0, average_price=Decimal("0"),
                           status=status, order_timestamp=datetime.now(timezone.utc), order_type="LIMIT",
                           price=Decimal("400"))

    db_session.add(_order(trio.vikash.id, source=OrderSource.SYSTEM, status=OrderStatus.COMPLETE, filled=1,
                          broker_order_id="JT_COPY", master_broker_order_id="OLD"))
    await db_session.commit()
    book = [rec("PRE_EXISTING"), rec("JT_COPY", "COMPLETE")]
    client = MagicMock(get_order_book=AsyncMock(side_effect=lambda: list(book)))
    dispatched = []
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    poller = MasterOrderPoller(master_account_id=str(trio.vikash.id), client_provider=lambda _a: client,
                               on_order_update=dispatched.append, pipeline=pipeline)
    assert not poller.is_seeded
    await poller.poll_once()
    assert poller.is_seeded and dispatched == []

    book.append(rec("NEW_AFTER_ACTIVATION"))
    book[1] = rec("JT_COPY", "CANCELLED")  # a change on Jettone's own order: still never dispatched
    await poller.poll_once()
    assert [e.broker_order_id for e in dispatched] == ["NEW_AFTER_ACTIVATION"]


# ─── 5. manual Place Order ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_manual_order_uses_the_selected_strategys_master(db_session, mock_redis, monkeypatch, trio):
    from app.api import broker as broker_api

    # Arun (account_type FOLLOWER) becomes Intraday's Master; Dhinesh (the
    # oldest account_type MASTER) moves to 'Normal' and must NOT be used for
    # Intraday's orders.
    child_id = (await _account(db_session, "kumar")).id
    strategy_id, arun_id, dhinesh_id = trio.strategy.id, trio.arun.id, trio.dhinesh.id
    await strategy_switch.execute_switch(db_session, strategy_id, arun_id, "REMOVE", _provider(trio.brokers))
    dhinesh = await db_session.get(Account, dhinesh_id, populate_existing=True)
    child = await db_session.get(Account, child_id, populate_existing=True)
    await link_strategy(db_session, dhinesh, [child], name="Normal")

    clients = {str(arun_id): MagicMock(name="arun"), str(dhinesh_id): MagicMock(name="dhinesh")}
    monkeypatch.setattr("app.services.alice_blue.manager.account_manager._clients", clients)
    monkeypatch.setattr("app.services.runtime.hooks.is_master_monitor_ready", lambda acc: True)

    master, client = await broker_api._load_master_client(db_session, mock_redis, strategy_id)
    assert master.id == arun_id and client is clients[str(arun_id)]


@pytest.mark.asyncio
async def test_manual_order_fails_closed(db_session, mock_redis, monkeypatch, trio):
    from app.api import broker as broker_api

    monkeypatch.setattr("app.services.alice_blue.manager.account_manager._clients", {})
    with pytest.raises(HTTPException) as exc:  # engine has no session for the Master
        await broker_api._load_master_client(db_session, mock_redis, trio.strategy.id)
    assert exc.value.status_code == 503

    monkeypatch.setattr("app.services.alice_blue.manager.account_manager._clients",
                        {str(trio.dhinesh.id): MagicMock()})
    monkeypatch.setattr("app.services.runtime.hooks.is_master_monitor_ready", lambda acc: False)
    with pytest.raises(HTTPException) as exc:  # Master monitor not active yet
        await broker_api._load_master_client(db_session, mock_redis, trio.strategy.id)
    assert exc.value.status_code == 409 and "monitoring" in exc.value.detail

    monkeypatch.setattr("app.services.runtime.hooks.is_master_monitor_ready", lambda acc: True)
    trio.strategy.trading_enabled = False
    await db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await broker_api._load_master_client(db_session, mock_redis, trio.strategy.id)
    assert exc.value.status_code == 409 and "disabled" in exc.value.detail

    with pytest.raises(HTTPException) as exc:
        await broker_api._load_master_client(db_session, mock_redis, uuid.uuid4())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_manual_limit_order_places_on_strategy_master_and_copies_to_its_children(
    db_session, mock_redis, monkeypatch, trio
):
    from app.api.broker import ManualOrderRequest, place_manual_master_order
    from app.services.alice_blue.types import OrderSide as BrokerSide, OrderType

    monkeypatch.setattr("app.api.broker.settings.ENGINE_LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr("app.api.broker.is_master_trading_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr("app.services.runtime.hooks.is_master_monitor_ready", lambda acc: True)
    outsider = await _account(db_session, "outsider")
    master_client = _child_client("BRK_MASTER")
    vikash_client = _child_client("BRK_VIKASH")
    outsider_client = _child_client("BRK_OUT")
    monkeypatch.setattr("app.services.alice_blue.manager.account_manager._clients", {
        str(trio.dhinesh.id): master_client, str(trio.vikash.id): vikash_client, str(outsider.id): outsider_client,
    })

    resp = await place_manual_master_order(
        payload=ManualOrderRequest(strategy_id=trio.strategy.id, symbol="RELIANCE", side=BrokerSide.BUY,
                                   quantity=10, product_type="MIS", order_type=OrderType.LIMIT,
                                   price=Decimal("100")),
        db=db_session, redis=mock_redis,
    )
    assert resp["placed"] is True
    master_client.place_order.assert_awaited_once()
    vikash_client.place_order.assert_awaited_once()
    outsider_client.place_order.assert_not_called()
    rows = (await db_session.execute(select(Order).where(Order.source == OrderSource.INTERNAL_UI))).scalars().all()
    assert [r.account_id for r in rows] == [trio.dhinesh.id]


# ─── 6. gate + stop provider ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_switch_gate_waits_for_running_orders_and_holds_new_ones():
    acc = str(uuid.uuid4())
    log = []
    release_order = asyncio.Event()

    async def order_task(name, wait=None):
        async with membership.account_order_gate(acc):
            log.append(f"{name}:in")
            if wait:
                await wait.wait()
            log.append(f"{name}:out")

    first = asyncio.create_task(order_task("o1", release_order))
    await asyncio.sleep(0)
    second = asyncio.create_task(order_task("o2"))  # concurrent orders still run together
    await asyncio.sleep(0.01)
    assert log == ["o1:in", "o2:in", "o2:out"]

    async def switch():
        async with membership.accounts_switch_lock([acc]):
            log.append("switch")

    sw = asyncio.create_task(switch())
    await asyncio.sleep(0.01)
    late = asyncio.create_task(order_task("o3"))
    await asyncio.sleep(0.01)
    assert "switch" not in log and "o3:in" not in log  # waits for o1; o3 waits for the switch
    release_order.set()
    await asyncio.gather(first, second, sw, late)
    assert log.index("o1:out") < log.index("switch") < log.index("o3:in")


@pytest.mark.asyncio
async def test_pause_of_an_account_created_as_master_is_honored_when_it_is_a_child(mock_redis, monkeypatch):
    from app.services.reliability.stop_provider import build_stop_provider

    legacy_master = str(uuid.uuid4())
    monkeypatch.setattr("app.services.alice_blue.manager.account_manager._clients", {legacy_master: object()})
    monkeypatch.setattr("app.services.alice_blue.manager.account_manager._master_account_id", legacy_master)
    with patch("app.services.reliability.stop_provider.EmergencyStopService") as svc:
        svc.return_value.is_global_active = AsyncMock(return_value=False)
        svc.return_value.is_follower_paused = AsyncMock(side_effect=lambda acc: acc == legacy_master)
        provider = build_stop_provider(mock_redis, account_ids=None)
        _, stopped = await provider()
    assert stopped == {legacy_master}


# ─── 7. historical reconciliation ─────────────────────────────────────────

async def _old_unresolved_copy(db_session, account_id, product):
    trade = CopyTrade(
        id=uuid.uuid4(), master_order_id=f"OLD_{product}", follower_account_id=account_id,
        copy_event_id=uuid.uuid4(), symbol="ITC", exchange="NSE", side=OrderSide.BUY, quantity=1,
        product_type=product, status=CopyTradeStatus.RECONCILIATION_REQUIRED,
    )
    db_session.add(trade)
    await db_session.commit()
    trade.created_at = datetime.now(timezone.utc) - timedelta(days=5)
    await db_session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("product", ["CNC", "LONGTERM", "NRML", "WEIRD_PRODUCT", "MIS", "INTRADAY"])
async def test_earlier_day_reconciliation_copy_never_blocks_whatever_the_product(db_session, trio, product):
    await _old_unresolved_copy(db_session, trio.vikash.id, product)
    pre = await _preflight(db_session, trio, trio.arun)
    assert pre.allowed and pre.blockers == []
    assert [w.code for w in pre.warnings] == ["RECONCILIATION_HISTORY"]


@pytest.mark.asyncio
async def test_unresolved_reconciliation_does_not_hide_a_real_live_blocker(db_session, trio):
    """Reconciliation noise is advisory, but a live open position still blocks."""
    await _old_unresolved_copy(db_session, trio.vikash.id, "CNC")
    trio.brokers[str(trio.dhinesh.id)]._positions = [_pos(qty=1)]
    pre = await _preflight(db_session, trio, trio.arun)
    assert not pre.allowed
    assert {b.code for b in pre.blockers} == {"OPEN_POSITIONS"}


# ─── 8. broker state unreadable: acknowledged override ─────────────────────

async def _unreadable(trio, *keys):
    for key in keys:
        trio.brokers[str(key)]._book_error = RuntimeError("Alice Blue unreachable")


@pytest.mark.asyncio
async def test_only_unverifiable_blockers_can_be_overridden(db_session, trio):
    await _unreadable(trio, trio.dhinesh.id, trio.arun.id)
    pre = await _preflight(db_session, trio, trio.arun)
    assert not pre.allowed and pre.can_override
    assert {b.code for b in pre.blockers} == {"BROKER_VERIFICATION_UNAVAILABLE"}
    assert "can_override" in pre.to_dict()


@pytest.mark.asyncio
async def test_real_blockers_are_never_overridable(db_session, trio):
    await _unreadable(trio, trio.arun.id)                      # unverifiable...
    trio.brokers[str(trio.vikash.id)]._positions = [_pos(qty=3)]  # ...plus a REAL open position
    pre = await _preflight(db_session, trio, trio.arun)
    assert not pre.allowed and not pre.can_override
    with pytest.raises(strategy_switch.SwitchBlocked):
        await strategy_switch.execute_switch(db_session, trio.strategy.id, trio.arun.id, "CHILD",
                                             _provider(trio.brokers), acknowledge_unverified=True)


@pytest.mark.asyncio
async def test_unverifiable_without_acknowledgement_still_blocks(db_session, trio):
    await _unreadable(trio, trio.dhinesh.id, trio.arun.id)
    with pytest.raises(strategy_switch.SwitchBlocked):
        await strategy_switch.execute_switch(db_session, trio.strategy.id, trio.arun.id, "CHILD",
                                             _provider(trio.brokers))
    async with TestingSessionLocal() as s:
        strat = (await s.execute(select(GroupStrategy).where(GroupStrategy.id == trio.strategy.id))).scalar_one()
    assert strat.master_account_id == trio.dhinesh.id  # unchanged


@pytest.mark.asyncio
async def test_acknowledged_override_switches_and_is_audited(db_session, trio):
    await _unreadable(trio, trio.dhinesh.id, trio.arun.id)
    pre = await strategy_switch.execute_switch(db_session, trio.strategy.id, trio.arun.id, "CHILD",
                                               _provider(trio.brokers), acknowledge_unverified=True)
    assert pre.can_override
    async with TestingSessionLocal() as s:
        strat = (await s.execute(select(GroupStrategy).where(GroupStrategy.id == trio.strategy.id))).scalar_one()
        event = (await s.execute(select(SystemEvent).where(
            SystemEvent.event_type == "STRATEGY_MASTER_SWITCHED"))).scalar_one()
    assert strat.master_account_id == trio.arun.id
    assert event.metadata_payload["unverified_override"] is True
    assert "UNVERIFIED" in event.message
    for broker in trio.brokers.values():  # still never cancels / places / modifies
        broker.place_order.assert_not_called()
        broker.cancel_order.assert_not_called()
        broker.modify_order.assert_not_called()


@pytest.mark.asyncio
async def test_normal_switch_is_not_marked_as_override(db_session, trio):
    await strategy_switch.execute_switch(db_session, trio.strategy.id, trio.arun.id, "CHILD",
                                         _provider(trio.brokers), acknowledge_unverified=True)
    async with TestingSessionLocal() as s:
        event = (await s.execute(select(SystemEvent).where(
            SystemEvent.event_type == "STRATEGY_MASTER_SWITCHED"))).scalar_one()
    assert event.metadata_payload["unverified_override"] is False


@pytest.mark.asyncio
async def test_switch_endpoint_accepts_the_acknowledgement(api_client, trio):
    for acc in (trio.dhinesh, trio.vikash, trio.arun):
        api_client.brokers[str(acc.id)] = _Broker()
    api_client.brokers[str(trio.arun.id)]._book_error = RuntimeError("down")
    body = {"new_master_account_id": str(trio.arun.id), "old_master_action": "CHILD"}
    resp = await api_client.post(f"/api/v1/strategies/{trio.strategy.id}/switch-master", json=body, headers=AUTH)
    assert resp.status_code == 409
    resp = await api_client.post(f"/api/v1/strategies/{trio.strategy.id}/switch-master",
                                 json={**body, "acknowledge_unverified": True}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["strategy"]["master_account_id"] == str(trio.arun.id)
