"""Broker registry: selection, fail-closed refusal of unsupported brokers, and
Alice Blue backward compatibility. No broker is contacted."""
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.api.accounts import get_runtime_client_manager
from app.core.crypto import encrypt_secret
from app.core.database import get_db
from app.core.redis import get_redis
from app.main import app
from app.models.accounts import Account, AccountType, ApiCredential
from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.manager import AliceBlueAccountManager
from app.services.brokers.catalog import SUPPORTED_BROKERS, catalog_with_support
from app.services.brokers.registry import (
    BrokerSupport,
    UnsupportedBrokerError,
    get_broker_spec,
    is_alice_blue,
    normalize_broker_id,
    order_label_for,
    require_alice_blue,
    require_engine_broker,
)
from conftest import TestingSessionLocal

TOKEN = {"X-Internal-Token": "test-internal-token"}


# ── registry ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [None, "", "alicebluenew", "AliceBlue", "Alice Blue", " alice_blue "])
def test_legacy_and_alias_values_resolve_to_alice_blue(value):
    assert normalize_broker_id(value) == "alicebluenew"
    assert is_alice_blue(value)
    assert require_engine_broker(value).support == BrokerSupport.SUPPORTED
    assert order_label_for(value) == "ALICE_BLUE"  # unchanged persisted label


def test_unknown_broker_is_refused():
    assert normalize_broker_id("robinhood") is None
    with pytest.raises(UnsupportedBrokerError):
        require_engine_broker("robinhood")


@pytest.mark.parametrize("bid", [b["id"] for b in SUPPORTED_BROKERS if b["id"] != "alicebluenew"])
def test_every_non_alice_catalog_broker_is_not_supported_and_has_no_client(bid):
    spec = get_broker_spec(bid)
    assert spec is not None and spec.support == BrokerSupport.NOT_SUPPORTED
    assert not (spec.capabilities.login or spec.capabilities.as_master or spec.capabilities.as_child)
    with pytest.raises(UnsupportedBrokerError):
        require_engine_broker(bid)
    with pytest.raises(UnsupportedBrokerError):
        order_label_for(bid)


def test_angel_one_is_not_advertised_as_master_or_child():
    spec = get_broker_spec("angelone")
    assert spec.support == BrokerSupport.NOT_SUPPORTED
    assert spec.capabilities.as_master is False and spec.capabilities.as_child is False
    assert "Master order detection is not implemented" in spec.reason


def test_require_alice_blue_refuses_angel():
    with pytest.raises(UnsupportedBrokerError):
        require_alice_blue("angelone")


def test_catalog_annotation_marks_only_alice_selectable():
    rows = {r["id"]: r for r in catalog_with_support()}
    assert len(rows) == len(SUPPORTED_BROKERS)
    assert rows["alicebluenew"]["selectable"] is True
    assert rows["alicebluenew"]["capabilities"] == {"login": True, "as_master": True, "as_child": True}
    for bid, r in rows.items():
        if bid != "alicebluenew":
            assert r["support"] == "NOT_SUPPORTED" and r["selectable"] is False, bid
            assert r["support_note"]


# ── dispatch: the registry + manager are the only path, and there is no Alice Blue fallback ──

def test_the_dead_factory_and_base_class_are_gone():
    import importlib.util
    assert importlib.util.find_spec("app.services.brokers.factory") is None
    assert importlib.util.find_spec("app.services.brokers.base") is None
    import app.services.brokers as pkg
    assert not hasattr(pkg, "create_broker_client") and not hasattr(pkg, "BaseBrokerClient")


@pytest.mark.parametrize("bid", ["angelone", "zerodha", "groww", "unknown-broker"])
def test_dispatch_never_falls_back_to_alice(mock_redis, monkeypatch, bid):
    """With the broker flags off every non-Alice broker is refused before ANY client - least of all an
    Alice Blue one - is constructed."""
    def must_not_be_built(*a, **k):
        raise AssertionError("an AliceBlueClient was constructed for a non-Alice account")
    monkeypatch.setattr(AliceBlueClient, "__init__", must_not_be_built)
    with pytest.raises(UnsupportedBrokerError):
        AliceBlueAccountManager._build_client(_acc(bid), mock_redis)


def test_dispatch_builds_each_enabled_broker_its_own_client_and_never_alice(mock_redis, monkeypatch):
    from app.core.config import settings
    from app.services.angel_one.client import AngelOneClient
    from app.services.groww.client import GrowwClient
    from app.services.brokers import credential_vault as vault
    monkeypatch.setattr(settings, "ANGEL_ONE_ENABLED", True)
    monkeypatch.setattr(settings, "ANGEL_ONE_CHILD_TRADING_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_CHILD_TRADING_ENABLED", True)

    def acc(broker, payload_broker, creds):
        a = _acc(broker)
        a.broker_credential = SimpleNamespace(encrypted_payload=vault.encrypt_payload(payload_broker, vault.validate_payload(payload_broker, creds)))
        a.static_ip_address = "10.0.0.1"
        a.proxy_expires_on = None
        return a
    assert isinstance(AliceBlueAccountManager._build_client(acc("groww", "groww", {"api_key": "k", "api_secret": "s"}), mock_redis), GrowwClient)
    assert isinstance(AliceBlueAccountManager._build_client(
        acc("angelone", "angelone", {"api_key": "k", "pin": "1", "totp_secret": "JBSWY3DPEHPK3PXP"}), mock_redis), AngelOneClient)
    assert isinstance(AliceBlueAccountManager._build_client(_acc("alicebluenew"), mock_redis), AliceBlueClient)
    assert isinstance(AliceBlueAccountManager._build_client(_acc(None), mock_redis), AliceBlueClient)


# ── manager: unsupported accounts never get a client ────────────────────────

def _acc(broker):
    return SimpleNamespace(
        id=uuid.uuid4(), alice_user_id="U1", broker=broker, notes=None,
        api_credential=SimpleNamespace(encrypted_api_secret=encrypt_secret("secret")),
    )


def test_manager_build_client_refuses_non_alice_before_decrypting(mock_redis, monkeypatch):
    decrypted = []
    monkeypatch.setattr("app.services.alice_blue.manager.decrypt_secret", lambda c: decrypted.append(c) or "x")
    with pytest.raises(UnsupportedBrokerError):
        AliceBlueAccountManager._build_client(_acc("angelone"), mock_redis)
    assert decrypted == []  # credentials were never touched


def test_manager_build_client_alice_unchanged(mock_redis):
    for broker in (None, "alicebluenew"):
        c = AliceBlueAccountManager._build_client(_acc(broker), mock_redis)
        assert isinstance(c, AliceBlueClient) and c.api_secret == "secret"


@pytest.mark.asyncio
async def test_initialize_skips_angel_account_and_keeps_alice(db_session, mock_redis, monkeypatch):
    alice = Account(account_type=AccountType.FOLLOWER, display_name="A", alice_user_id="ALC1", is_active=True, broker="alicebluenew")
    angel = Account(account_type=AccountType.FOLLOWER, display_name="B", alice_user_id="ANG1", is_active=True, broker="angelone")
    db_session.add_all([alice, angel])
    await db_session.flush()
    db_session.add_all([
        ApiCredential(account_id=alice.id, encrypted_api_secret=encrypt_secret("s1")),
        ApiCredential(account_id=angel.id, encrypted_api_secret=encrypt_secret("s2")),
    ])
    await db_session.commit()
    monkeypatch.setattr("app.services.alice_blue.manager.AsyncSessionLocal", TestingSessionLocal)
    monkeypatch.setattr("app.services.alice_blue.manager.get_redis", AsyncMock(return_value=mock_redis))

    mgr = AliceBlueAccountManager()
    await mgr.initialize()
    assert mgr.get_client(str(alice.id)) is not None
    assert mgr.get_client(str(angel.id)) is None


@pytest.mark.asyncio
async def test_sync_account_fails_for_angel(db_session, mock_redis):
    angel = Account(account_type=AccountType.FOLLOWER, display_name="B", alice_user_id="ANG2", is_active=True, broker="angelone")
    db_session.add(angel)
    await db_session.flush()
    db_session.add(ApiCredential(account_id=angel.id, encrypted_api_secret=encrypt_secret("s")))
    await db_session.commit()
    mgr = AliceBlueAccountManager()
    res = await mgr.sync_account(str(angel.id), db_session, mock_redis)
    assert res.status == "FAILED" and res.ready is False
    assert "not supported" in res.detail
    assert mock_redis.get.await_count == 0  # no session lookup, no broker call


# ── API ─────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client(mock_redis, db_session):
    async def override_db():
        async with TestingSessionLocal() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_redis] = lambda: mock_redis
    app.dependency_overrides[get_runtime_client_manager] = lambda: AliceBlueAccountManager()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _payload(uid, broker):
    return {"account_type": "FOLLOWER", "display_name": uid, "alice_user_id": uid, "api_secret": "s", "broker": broker}


@pytest.mark.asyncio
async def test_supported_brokers_endpoint_reports_status(client):
    resp = await client.get("/api/v1/accounts/supported-brokers", headers=TOKEN)
    assert resp.status_code == 200
    rows = {r["id"]: r for r in resp.json()["brokers"]}
    assert rows["alicebluenew"]["support"] == "SUPPORTED"
    assert rows["angelone"]["support"] == "NOT_SUPPORTED" and rows["angelone"]["selectable"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["angelone", "groww", "made-up"])
async def test_create_account_rejects_unsupported_broker(client, broker):
    resp = await client.post("/api/v1/accounts", json=_payload("X" + broker[:4], broker), headers=TOKEN)
    assert resp.status_code == 400
    async with TestingSessionLocal() as s:
        assert (await s.execute(select(Account))).scalars().all() == []


@pytest.mark.asyncio
async def test_create_alice_account_still_works(client):
    resp = await client.post("/api/v1/accounts", json=_payload("ALC9", "alicebluenew"), headers=TOKEN)
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_update_cannot_switch_account_to_unsupported_broker(client):
    created = (await client.post("/api/v1/accounts", json=_payload("ALC8", "alicebluenew"), headers=TOKEN)).json()
    resp = await client.put(f"/api/v1/accounts/{created['id']}", json={"broker": "angelone"}, headers=TOKEN)
    assert resp.status_code == 409          # a broker can never be changed after creation (see test_account_broker_immutable)
    ok = await client.put(f"/api/v1/accounts/{created['id']}", json={"broker": "alicebluenew", "display_name": "n"}, headers=TOKEN)
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_broker_endpoints_refuse_existing_non_alice_account(client, db_session):
    # An account stored with a non-Alice broker (e.g. legacy data) must never
    # get an AliceBlueClient from the direct-construction API paths.
    acc = Account(account_type=AccountType.FOLLOWER, display_name="G", alice_user_id="GRW1", is_active=True, broker="groww")
    db_session.add(acc)
    await db_session.flush()
    db_session.add(ApiCredential(account_id=acc.id, encrypted_api_secret=encrypt_secret("s")))
    await db_session.commit()
    for path in (f"/api/v1/broker/funds/{acc.id}", f"/api/v1/broker/positions/{acc.id}"):
        resp = await client.get(path, headers=TOKEN)
        assert resp.status_code == 409, path
    resp = await client.post(f"/api/v1/broker/authenticate/{acc.id}", headers=TOKEN)
    assert resp.status_code == 409
