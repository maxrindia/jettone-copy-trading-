"""
Runtime broker-client registry sync: a follower activated or deactivated
after the engine started must be reflected in AliceBlueAccountManager's
registry (and so in replication) without a restart.

No live trading and no real broker calls - clients are AsyncMock doubles,
or real AliceBlueClient instances whose only I/O here is a Redis session
read against the in-memory mock_redis fixture.
"""
import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.core.crypto import encrypt_secret
from app.models.accounts import Account, AccountType, ApiCredential
from app.models.audit import SystemEvent
from app.services.alice_blue.manager import AliceBlueAccountManager
from app.services.alice_blue.types import OrderResultCategory
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from conftest import TestingSessionLocal
from tests.test_sell_position_safety import _buy_market_event, _client
from strategy_helpers import link_strategy


async def _add_account(db_session, name, account_type, is_active=True):
    acc = Account(
        id=uuid.uuid4(),
        account_type=account_type,
        display_name=name,
        alice_user_id=f"AB_{name.upper()}",
        is_active=is_active,
    )
    db_session.add(acc)
    await db_session.flush()
    db_session.add(ApiCredential(account_id=acc.id, encrypted_api_secret=encrypt_secret("secret")))
    await db_session.commit()
    await db_session.refresh(acc)
    return acc


async def _set_active(db_session, acc, is_active):
    acc.is_active = is_active
    await db_session.commit()


@pytest.fixture
def patched_manager_io(monkeypatch, mock_redis):
    monkeypatch.setattr("app.services.alice_blue.manager.AsyncSessionLocal", TestingSessionLocal)
    monkeypatch.setattr("app.services.alice_blue.manager.get_redis", AsyncMock(return_value=mock_redis))


@pytest.fixture
def fake_client_factory(monkeypatch, mock_redis):
    """Replaces AliceBlueClient construction inside the manager with broker
    doubles, one per account id, and counts constructions."""
    made = {}

    def factory(account_id, **kwargs):
        c = _client(mock_redis)
        c.account_id = account_id
        made.setdefault(account_id, []).append(c)
        return c

    monkeypatch.setattr("app.services.alice_blue.manager.AliceBlueClient", MagicMock(side_effect=factory))
    return made


# 1, 2, 10. Startup behavior

@pytest.mark.asyncio
async def test_startup_loads_active_follower_and_master(db_session, patched_manager_io):
    master = await _add_account(db_session, "dhinesh", AccountType.MASTER)
    vikash = await _add_account(db_session, "vikash", AccountType.FOLLOWER)

    mgr = AliceBlueAccountManager()
    await mgr.initialize()

    assert mgr._master_account_id == str(master.id)
    assert mgr.get_master_client() is not None
    # No Redis session exists: startup still registers the client, as before.
    assert set(mgr.get_follower_clients_map()) == {str(vikash.id)}


@pytest.mark.asyncio
async def test_startup_skips_inactive_follower(db_session, patched_manager_io):
    await _add_account(db_session, "dhinesh", AccountType.MASTER)
    vikash = await _add_account(db_session, "vikash", AccountType.FOLLOWER, is_active=False)

    mgr = AliceBlueAccountManager()
    await mgr.initialize()

    assert mgr.get_client(str(vikash.id)) is None
    assert mgr.get_follower_clients_map() == {}


# 3, 4. Activation after startup

@pytest.mark.asyncio
async def test_activation_after_startup_registers_client(db_session, mock_redis, patched_manager_io):
    await _add_account(db_session, "dhinesh", AccountType.MASTER)
    vikash = await _add_account(db_session, "vikash", AccountType.FOLLOWER, is_active=False)
    mgr = AliceBlueAccountManager()
    await mgr.initialize()

    await _set_active(db_session, vikash, True)
    await mock_redis.set(f"alice:session:{vikash.id}", "SESSION_JWT")
    result = await mgr.sync_account(str(vikash.id), db=db_session, redis=mock_redis)

    assert result.status == "REGISTERED" and result.ready
    assert mgr.get_client(str(vikash.id)) is not None
    assert str(vikash.id) in mgr.get_follower_clients_map()


@pytest.mark.asyncio
async def test_activated_client_is_used_by_replication(db_session, mock_redis, patched_manager_io, fake_client_factory):
    master = await _add_account(db_session, "dhinesh", AccountType.MASTER)
    vikash = await _add_account(db_session, "vikash", AccountType.FOLLOWER, is_active=False)
    await link_strategy(db_session, master, [vikash])
    mgr = AliceBlueAccountManager()
    await mgr.initialize()

    await _set_active(db_session, vikash, True)
    result = await mgr.sync_account(str(vikash.id), db=db_session, redis=mock_redis)
    assert result.status == "REGISTERED"

    event = _buy_market_event(quantity=2)
    event.account_id = str(master.id)
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=event, clients=mgr.get_follower_clients_map())

    registered = fake_client_factory[str(vikash.id)][-1]
    assert res["results"][str(vikash.id)]["status"] == OrderResultCategory.SUCCESS.value
    registered.place_order.assert_awaited_once()
    assert registered.place_order.call_args.args[0].quantity == 2


# 5. Authentication failure

@pytest.mark.asyncio
async def test_activation_without_session_is_not_registered(db_session, mock_redis, patched_manager_io):
    await _add_account(db_session, "dhinesh", AccountType.MASTER)
    vikash = await _add_account(db_session, "vikash", AccountType.FOLLOWER, is_active=False)
    mgr = AliceBlueAccountManager()
    await mgr.initialize()

    await _set_active(db_session, vikash, True)
    result = await mgr.sync_account(str(vikash.id), db=db_session, redis=mock_redis)

    assert result.status == "FAILED" and not result.ready
    assert "No active Alice Blue session" in result.detail
    assert mgr.get_client(str(vikash.id)) is None
    assert mgr.get_follower_clients_map() == {}


# 6, 7. Deactivation

@pytest.mark.asyncio
async def test_deactivation_removes_client(db_session, mock_redis, patched_manager_io):
    await _add_account(db_session, "dhinesh", AccountType.MASTER)
    vikash = await _add_account(db_session, "vikash", AccountType.FOLLOWER)
    mgr = AliceBlueAccountManager()
    await mgr.initialize()
    assert mgr.get_client(str(vikash.id)) is not None

    await _set_active(db_session, vikash, False)
    result = await mgr.sync_account(str(vikash.id), db=db_session, redis=mock_redis)

    assert result.status == "REMOVED" and not result.ready
    assert mgr.get_client(str(vikash.id)) is None
    assert str(vikash.id) not in mgr._last_auth_status
    # Credentials are not touched.
    cred = (await db_session.execute(select(ApiCredential).where(ApiCredential.account_id == vikash.id))).scalar_one()
    assert cred.encrypted_api_secret


@pytest.mark.asyncio
async def test_deactivated_follower_is_not_selected_for_replication(db_session, mock_redis, patched_manager_io, fake_client_factory):
    master = await _add_account(db_session, "dhinesh", AccountType.MASTER)
    vikash = await _add_account(db_session, "vikash", AccountType.FOLLOWER)
    mgr = AliceBlueAccountManager()
    await mgr.initialize()
    old_client = mgr.get_client(str(vikash.id))

    await _set_active(db_session, vikash, False)
    await mgr.sync_account(str(vikash.id), db=db_session, redis=mock_redis)

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    eligible, _ = await pipeline._get_eligible_followers_for_master(str(master.id))
    assert str(vikash.id) not in {str(a.id) for a, _m in eligible}

    event = _buy_market_event(quantity=1)
    event.account_id = str(master.id)
    res = await pipeline.execute_replication(event=event, clients=mgr.get_follower_clients_map())
    assert str(vikash.id) not in res.get("results", {})
    old_client.place_order.assert_not_called()


# 8. Master is never changed by follower sync

@pytest.mark.asyncio
async def test_master_client_unchanged_by_follower_sync(db_session, mock_redis, patched_manager_io):
    master = await _add_account(db_session, "dhinesh", AccountType.MASTER)
    vikash = await _add_account(db_session, "vikash", AccountType.FOLLOWER, is_active=False)
    mgr = AliceBlueAccountManager()
    await mgr.initialize()
    master_client = mgr.get_master_client()

    await mock_redis.set(f"alice:session:{vikash.id}", "SESSION_JWT")
    await _set_active(db_session, vikash, True)
    await mgr.sync_account(str(vikash.id), db=db_session, redis=mock_redis)
    await _set_active(db_session, vikash, False)
    await mgr.sync_account(str(vikash.id), db=db_session, redis=mock_redis)

    assert mgr.get_master_client() is master_client
    assert mgr._master_account_id == str(master.id)

    # Even deactivating the master in the DB never removes it at runtime.
    await _set_active(db_session, master, False)
    result = await mgr.sync_account(str(master.id), db=db_session, redis=mock_redis)
    assert result.status == "MASTER_UNCHANGED"
    assert mgr.get_master_client() is master_client
    assert str(master.id) not in mgr.get_follower_clients_map()


# 9. Concurrency

@pytest.mark.asyncio
async def test_concurrent_activations_register_one_client(db_session, mock_redis, patched_manager_io, fake_client_factory):
    await _add_account(db_session, "dhinesh", AccountType.MASTER)
    vikash = await _add_account(db_session, "vikash", AccountType.FOLLOWER, is_active=False)
    mgr = AliceBlueAccountManager()
    await mgr.initialize()
    await _set_active(db_session, vikash, True)

    results = await asyncio.gather(
        mgr.sync_account(str(vikash.id), db=db_session, redis=mock_redis),
        mgr.sync_account(str(vikash.id), db=db_session, redis=mock_redis),
    )

    assert sorted(r.status for r in results) == ["ALREADY_REGISTERED", "REGISTERED"]
    assert len(fake_client_factory[str(vikash.id)]) == 1
    assert mgr.get_client(str(vikash.id)) is fake_client_factory[str(vikash.id)][0]


# 11. A client that genuinely cannot be initialized still fails closed

@pytest.mark.asyncio
async def test_unregistered_active_follower_still_fails_closed_with_funds_fetch_failed(db_session, mock_redis, patched_manager_io):
    master = await _add_account(db_session, "dhinesh", AccountType.MASTER)
    vikash = await _add_account(db_session, "vikash", AccountType.FOLLOWER, is_active=False)
    await link_strategy(db_session, master, [vikash])
    mgr = AliceBlueAccountManager()
    await mgr.initialize()

    await _set_active(db_session, vikash, True)
    result = await mgr.sync_account(str(vikash.id), db=db_session, redis=mock_redis)
    assert result.status == "FAILED"

    event = _buy_market_event(quantity=1)
    event.account_id = str(master.id)
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=event, clients=mgr.get_follower_clients_map())

    assert res["results"][str(vikash.id)] == {"status": "REJECTED", "reasons": ["FUNDS_FETCH_FAILED"]}


# API: PUT /accounts/{id} drives the same sync and exposes the outcome

@pytest.mark.asyncio
async def test_put_activation_registers_and_deactivation_removes(db_session, mock_redis):
    import httpx
    from app.api.accounts import get_runtime_client_manager
    from app.core.database import get_db
    from app.core.redis import get_redis
    from app.main import app

    vikash = await _add_account(db_session, "vikash", AccountType.FOLLOWER, is_active=False)
    await mock_redis.set(f"alice:session:{vikash.id}", "SESSION_JWT")
    mgr = AliceBlueAccountManager()

    async def override_db():
        async with TestingSessionLocal() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_redis] = lambda: mock_redis
    app.dependency_overrides[get_runtime_client_manager] = lambda: mgr
    headers = {"X-Internal-Token": "test-internal-token"}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            on = await api.put(f"/api/v1/accounts/{vikash.id}", json={"is_active": True}, headers=headers)
            assert on.status_code == 200
            assert on.json()["runtime_client_status"] == "REGISTERED"
            assert mgr.get_client(str(vikash.id)) is not None

            off = await api.put(f"/api/v1/accounts/{vikash.id}", json={"is_active": False}, headers=headers)
            assert off.status_code == 200
            assert off.json()["runtime_client_status"] == "REMOVED"
            assert off.json()["is_active"] is False
            assert mgr.get_client(str(vikash.id)) is None

            other = await api.put(f"/api/v1/accounts/{vikash.id}", json={"display_name": "vikash 2"}, headers=headers)
            assert other.json()["runtime_client_status"] is None
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_put_activation_failure_is_reported_not_silent(db_session, mock_redis):
    import httpx
    from app.api.accounts import get_runtime_client_manager
    from app.core.database import get_db
    from app.core.redis import get_redis
    from app.main import app

    vikash = await _add_account(db_session, "vikash", AccountType.FOLLOWER, is_active=False)
    mgr = AliceBlueAccountManager()

    async def override_db():
        async with TestingSessionLocal() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_redis] = lambda: mock_redis
    app.dependency_overrides[get_runtime_client_manager] = lambda: mgr
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            resp = await api.put(
                f"/api/v1/accounts/{vikash.id}", json={"is_active": True},
                headers={"X-Internal-Token": "test-internal-token"},
            )
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["is_active"] is True
    assert resp.json()["runtime_client_status"] == "FAILED"
    assert mgr.get_client(str(vikash.id)) is None

    async with TestingSessionLocal() as s:
        events = (await s.execute(select(SystemEvent).where(SystemEvent.event_type == "RUNTIME_CLIENT_UNAVAILABLE"))).scalars().all()
    assert len(events) == 1
    assert str(events[0].account_id) == str(vikash.id)
