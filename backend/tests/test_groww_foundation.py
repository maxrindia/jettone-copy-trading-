"""Groww foundation: feature flag, registry/catalog, encrypted vault, credential and login
APIs, Master blocking, and manager routing. Offline: no request leaves the process."""
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.api.accounts import get_runtime_client_manager
from app.core.config import Settings, settings
from app.core.database import get_db
from app.core.redis import get_redis
from app.main import app
from app.models.accounts import Account, AccountType, ApiCredential, BrokerCredential
from app.services import strategy_switch
from app.services.alice_blue.manager import AliceBlueAccountManager
from app.services.brokers import credential_vault as vault
from app.services.brokers.catalog import catalog_with_support
from app.services.brokers.groww_builder import build_groww_client
from app.services.brokers.registry import (
    BrokerSupport, can_be_child, can_be_master, get_broker_spec, normalize_broker_id, persisted_order_label,
    require_engine_broker, session_key_for, UnsupportedBrokerError,
)
from app.services.groww.client import GrowwClient
from app.services.groww.contract import GrowwContract
from conftest import TestingSessionLocal
from groww_helpers import FakeGroww, make_client
from strategy_helpers import link_strategy

TOKEN = {"X-Internal-Token": "test-internal-token"}
SECRETS = {"api_key": "GRW-KEY-SECRET-111", "api_secret": "GRW-SECRET-SECRET-222"}


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(settings, "GROWW_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_CHILD_TRADING_ENABLED", True)


# ── feature flag / registry ─────────────────────────────────────────────────

def test_groww_is_disabled_by_default():
    assert Settings.model_fields["GROWW_ENABLED"].default is False
    assert Settings.model_fields["GROWW_INSTRUMENT_MASTER_URL"].default == ""
    assert settings.GROWW_ENABLED is False                       # this test run's effective value


def test_the_flag_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("GROWW_ENABLED", "true")
    assert Settings().GROWW_ENABLED is True
    monkeypatch.setenv("GROWW_ENABLED", "false")
    assert Settings().GROWW_ENABLED is False


def test_flag_off_is_not_supported_everywhere(monkeypatch):
    monkeypatch.setattr(settings, "GROWW_ENABLED", False)
    spec = get_broker_spec("groww")
    assert spec.support == BrokerSupport.NOT_SUPPORTED and not spec.capabilities.login
    assert not can_be_child("groww") and not can_be_master("groww")
    with pytest.raises(UnsupportedBrokerError):
        require_engine_broker("groww")
    row = {r["id"]: r for r in catalog_with_support()}["groww"]
    assert row["selectable"] is False and row["capabilities"] == {"login": False, "as_master": False, "as_child": False}


def test_flag_on_makes_groww_a_child_only_broker(flag_on):
    spec = get_broker_spec("groww")
    assert spec.support == BrokerSupport.PARTIAL and spec.uses_vault is True
    assert spec.capabilities.login and spec.capabilities.as_child and not spec.capabilities.as_master
    assert can_be_child("groww") and not can_be_master("groww")
    row = {r["id"]: r for r in catalog_with_support()}["groww"]
    assert row["selectable"] is True and "Child only" in row["support_note"]


def test_flag_on_with_an_unverified_contract_allows_login_but_not_copying(flag_on, monkeypatch):
    monkeypatch.setattr("app.services.brokers.registry.GROWW_CONTRACT", GrowwContract(docs_verified=False))
    spec = get_broker_spec("groww")
    assert spec.capabilities.login and not spec.capabilities.as_child and not can_be_child("groww")


def test_angel_and_alice_are_unaffected_by_the_groww_flag(flag_on):
    assert get_broker_spec("angelone").support == BrokerSupport.NOT_SUPPORTED     # its own flag is still off
    alice = get_broker_spec(None)
    assert alice.id == "alicebluenew" and can_be_master(None) and can_be_child("alicebluenew")


def test_neither_groww_nor_angel_can_ever_be_a_master(flag_on, monkeypatch):
    monkeypatch.setattr(settings, "ANGEL_ONE_ENABLED", True)
    monkeypatch.setattr(settings, "ANGEL_ONE_CHILD_TRADING_ENABLED", True)
    assert not can_be_master("groww") and not can_be_master("angelone") and can_be_master("alicebluenew")


def test_aliases_labels_and_session_keys():
    aid = uuid.uuid4()
    assert normalize_broker_id("Groww") == "groww" and normalize_broker_id("growwapi") == "groww"
    assert session_key_for("groww", aid) == f"groww:session:{aid}"
    assert persisted_order_label("groww") == "GROWW" and persisted_order_label("alicebluenew") == "ALICE_BLUE"
    assert session_key_for("angelone", aid).startswith("angel:session:")


# ── vault ───────────────────────────────────────────────────────────────────

def test_vault_roundtrip_for_groww_is_encrypted_and_redacted():
    creds = vault.validate_payload("groww", SECRETS)
    blob = vault.encrypt_payload("groww", creds)
    for secret in SECRETS.values():
        assert secret not in blob
    back = vault.decrypt_groww(blob)
    assert back == creds and back.api_key == SECRETS["api_key"] and back.api_secret == SECRETS["api_secret"]
    assert "SECRET" not in repr(creds) and "GRW" not in repr(creds)


@pytest.mark.parametrize("bad,needle", [
    ({"api_key": "VALUE-K9"}, "api_secret"), ({"api_secret": "VALUE-S9"}, "api_key"), ({}, "api_key"),
    ({"api_key": "  ", "api_secret": "VALUE-S9"}, "api_key"),
])
def test_vault_rejects_incomplete_groww_payloads_without_echoing_values(bad, needle):
    with pytest.raises(vault.CredentialError) as ei:
        vault.validate_payload("groww", bad)
    assert needle in str(ei.value)
    for v in bad.values():
        assert not v.strip() or v not in str(ei.value)


def test_vault_entries_are_broker_specific():
    blob = vault.encrypt_payload("groww", vault.validate_payload("groww", SECRETS))
    with pytest.raises(vault.CredentialError):          # a Groww entry is not valid Angel One credentials
        vault.decrypt_payload("angelone", blob)
    assert vault.describe(type("R", (), {"encrypted_payload": blob, "broker": "groww"})()) == \
        {"configured": True, "fields": ["api_key", "api_secret"]}
    with pytest.raises(vault.CredentialError):
        vault.validate_payload("zerodha", SECRETS)


def test_angel_vault_behaviour_is_unchanged():
    creds = vault.validate_angel_payload({"api_key": "k", "pin": "1", "totp_secret": "JBSWY3DPEHPK3PXP"})
    assert vault.decrypt_angel(vault.encrypt_angel(creds)) == creds
    assert vault.describe(None) == {"configured": False, "fields": []}


# ── accounts / manager ──────────────────────────────────────────────────────

async def _groww_account(db, name="grw", ip="151.245.182.70", creds=True, account_type=AccountType.FOLLOWER, active=True):
    acc = Account(id=uuid.uuid4(), account_type=account_type, display_name=name,
                  alice_user_id=f"G_{name}_{uuid.uuid4().hex[:4]}", broker="groww", is_active=active,
                  static_ip_address=ip)
    db.add(acc)
    await db.flush()
    if creds:
        db.add(BrokerCredential(account_id=acc.id, broker="groww",
                                encrypted_payload=vault.encrypt_payload("groww", vault.validate_payload("groww", SECRETS))))
    await db.commit()
    return acc


@pytest.mark.asyncio
async def test_build_groww_client_uses_vault_credentials_and_the_accounts_ip(db_session, mock_redis):
    acc = await _groww_account(db_session, ip="151.245.182.77")
    async with TestingSessionLocal() as s:
        loaded = (await s.execute(select(Account).where(Account.id == acc.id))).scalar_one()
        await s.refresh(loaded, ["broker_credential"])
        client = build_groww_client(loaded, mock_redis)
    assert isinstance(client, GrowwClient) and client.static_ip == "151.245.182.77"
    assert "SECRET" not in repr(client)


def test_build_groww_client_without_credentials_fails(mock_redis):
    class A:
        id = uuid.uuid4(); alice_user_id = "X"; broker_credential = None
    with pytest.raises(vault.CredentialError):
        build_groww_client(A(), mock_redis)


@pytest.mark.asyncio
async def test_manager_registers_groww_only_when_enabled_and_credentialed(db_session, mock_redis, monkeypatch):
    monkeypatch.setattr("app.services.alice_blue.manager.AsyncSessionLocal", TestingSessionLocal)
    monkeypatch.setattr("app.services.alice_blue.manager.get_redis", AsyncMock(return_value=mock_redis))
    ok_acc = await _groww_account(db_session, "ok")
    nocred = await _groww_account(db_session, "nocred", creds=False)
    monkeypatch.setattr(settings, "GROWW_ENABLED", False)
    mgr = AliceBlueAccountManager()
    await mgr.initialize()
    assert mgr.get_all_account_ids() == []                                    # flag off: no Groww client at all
    monkeypatch.setattr(settings, "GROWW_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_CHILD_TRADING_ENABLED", True)
    mgr = AliceBlueAccountManager()
    await mgr.initialize()
    assert mgr.get_all_account_ids() == [str(ok_acc.id)] and mgr.get_client(str(nocred.id)) is None
    assert isinstance(mgr.get_client(str(ok_acc.id)), GrowwClient)


@pytest.mark.asyncio
async def test_a_master_typed_groww_account_never_becomes_the_legacy_master(db_session, mock_redis, monkeypatch):
    monkeypatch.setattr("app.services.alice_blue.manager.AsyncSessionLocal", TestingSessionLocal)
    monkeypatch.setattr("app.services.alice_blue.manager.get_redis", AsyncMock(return_value=mock_redis))
    monkeypatch.setattr(settings, "GROWW_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_CHILD_TRADING_ENABLED", True)
    await _groww_account(db_session, "mastertyped", account_type=AccountType.MASTER)
    mgr = AliceBlueAccountManager()
    await mgr.initialize()
    assert mgr.get_master_client() is None


@pytest.mark.asyncio
async def test_startup_authentication_never_logs_in_to_groww_or_angel(mock_redis):
    """authenticate_all / reauth run at engine start; for Alice they only read Redis, but for Groww and
    Angel One a 'login' contacts the broker, so those clients are skipped (explicit login only)."""
    fake = FakeGroww()
    groww = make_client(mock_redis, fake)
    alice_like = AsyncMock()
    alice_like.get_session = AsyncMock(return_value="tok")
    mgr = AliceBlueAccountManager()
    mgr._clients = {"g": groww, "a": alice_like}
    results = await mgr.authenticate_all()
    assert results == {"a": True} and fake.requests == []
    alice_like.get_session.assert_awaited_once_with(force_refresh=True)       # Alice behaviour unchanged
    assert await mgr.reauth_failed_accounts() == {}                           # g skipped; a already authenticated
    assert fake.requests == []


@pytest.mark.asyncio
async def test_sync_account_needs_a_session_for_groww(db_session, mock_redis, flag_on):
    import json
    from app.core.crypto import encrypt_secret
    acc = await _groww_account(db_session)
    mgr = AliceBlueAccountManager()
    res = await mgr.sync_account(str(acc.id), db_session, mock_redis)
    assert res.status == "FAILED" and res.ready is False                     # no Groww session yet
    mock_redis._storage[f"groww:session:{acc.id}"] = "garbage"               # unreadable session still fails closed
    assert (await mgr.sync_account(str(acc.id), db_session, mock_redis)).status == "FAILED"
    mock_redis._storage[f"groww:session:{acc.id}"] = encrypt_secret(json.dumps({"token": "t"}))
    res = await mgr.sync_account(str(acc.id), db_session, mock_redis)
    assert res.status == "REGISTERED" and res.ready is True


# ── Master blocking ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["groww", "angelone"])
async def test_a_child_only_broker_cannot_become_a_strategy_master(db_session, monkeypatch, broker):
    monkeypatch.setattr(settings, "GROWW_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_CHILD_TRADING_ENABLED", True)
    monkeypatch.setattr(settings, "ANGEL_ONE_ENABLED", True)
    monkeypatch.setattr(settings, "ANGEL_ONE_CHILD_TRADING_ENABLED", True)
    from test_strategy_master_switching import _account
    old_master = await _account(db_session, "oldmaster")
    child = await _account(db_session, "child")
    strategy = await link_strategy(db_session, old_master, [child], name="S")
    cand = await _account(db_session, "cand")
    cand.broker = broker                       # same session as run_preflight (no stale identity-map copy)
    await db_session.commit()
    with pytest.raises(strategy_switch.SwitchValidationError) as ei:
        await strategy_switch.run_preflight(db_session, strategy.id, cand.id, "REMOVE", lambda aid: None)
    assert ei.value.code == "NEW_MASTER_BROKER_UNSUPPORTED" and ei.value.status_code == 409
    assert "Child account" in ei.value.message


@pytest.mark.asyncio
async def test_alice_blue_can_still_become_a_master(db_session):
    from test_strategy_master_switching import _account
    old_master = await _account(db_session, "oldmaster")
    child = await _account(db_session, "child")
    strategy = await link_strategy(db_session, old_master, [child], name="S")
    cand = await _account(db_session, "cand")
    try:
        await strategy_switch.run_preflight(db_session, strategy.id, cand.id, "REMOVE", lambda aid: None)
    except strategy_switch.SwitchValidationError as exc:
        assert exc.code != "NEW_MASTER_BROKER_UNSUPPORTED"


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
    manager = AliceBlueAccountManager()
    app.dependency_overrides[get_runtime_client_manager] = lambda: manager
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _no_secrets(text: str):
    for s in SECRETS.values():
        assert s not in text, "a secret leaked into a response"


@pytest.mark.asyncio
async def test_supported_brokers_endpoint_reflects_the_groww_flag(client, monkeypatch):
    monkeypatch.setattr(settings, "GROWW_ENABLED", False)
    rows = {r["id"]: r for r in (await client.get("/api/v1/accounts/supported-brokers", headers=TOKEN)).json()["brokers"]}
    assert rows["groww"]["support"] == "NOT_SUPPORTED" and rows["groww"]["selectable"] is False
    monkeypatch.setattr(settings, "GROWW_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_CHILD_TRADING_ENABLED", True)
    rows = {r["id"]: r for r in (await client.get("/api/v1/accounts/supported-brokers", headers=TOKEN)).json()["brokers"]}
    assert rows["groww"]["support"] == "PARTIAL" and rows["groww"]["selectable"] is True
    assert rows["alicebluenew"]["support"] == "SUPPORTED"


@pytest.mark.asyncio
async def test_a_disabled_groww_cannot_be_added_or_configured(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "GROWW_ENABLED", False)
    resp = await client.post("/api/v1/accounts", headers=TOKEN, json={
        "account_type": "FOLLOWER", "display_name": "G", "alice_user_id": "GRW-OFF", "api_secret": "x", "broker": "groww"})
    assert resp.status_code == 400
    acc = await _groww_account(db_session, creds=False)
    for call in (client.put(f"/api/v1/accounts/{acc.id}/broker-credentials", json=SECRETS, headers=TOKEN),
                 client.post(f"/api/v1/accounts/{acc.id}/broker-login", headers=TOKEN)):
        r = await call
        assert r.status_code == 409
        _no_secrets(r.text)


@pytest.mark.asyncio
async def test_creating_a_groww_account_stores_no_api_credential(client, db_session, flag_on):
    resp = await client.post("/api/v1/accounts", headers=TOKEN, json={
        "account_type": "FOLLOWER", "display_name": "G", "alice_user_id": "GRW-NEW",
        "api_secret": "should-not-be-stored", "broker": "groww"})
    assert resp.status_code == 201
    async with TestingSessionLocal() as s:
        assert (await s.execute(select(ApiCredential))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["groww", "angelone"])
async def test_a_master_account_cannot_be_created_on_a_child_only_broker(client, flag_on, monkeypatch, broker):
    monkeypatch.setattr(settings, "ANGEL_ONE_ENABLED", True)
    monkeypatch.setattr(settings, "ANGEL_ONE_CHILD_TRADING_ENABLED", True)
    resp = await client.post("/api/v1/accounts", headers=TOKEN, json={
        "account_type": "MASTER", "display_name": "M", "alice_user_id": f"M-{broker}", "api_secret": "x", "broker": broker})
    assert resp.status_code == 400 and "Child" in resp.text
    async with TestingSessionLocal() as s:
        assert (await s.execute(select(Account))).scalars().all() == []


@pytest.mark.asyncio
async def test_a_master_account_can_still_be_created_on_alice_blue(client, flag_on):
    resp = await client.post("/api/v1/accounts", headers=TOKEN, json={
        "account_type": "MASTER", "display_name": "M", "alice_user_id": "ALC-M", "api_secret": "x", "broker": "alicebluenew"})
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_groww_credentials_are_stored_encrypted_and_never_returned(client, db_session, flag_on):
    acc = await _groww_account(db_session, creds=False)
    resp = await client.put(f"/api/v1/accounts/{acc.id}/broker-credentials", json=SECRETS, headers=TOKEN)
    assert resp.status_code == 200 and resp.json() == {"configured": True, "fields": ["api_key", "api_secret"]}
    _no_secrets(resp.text)
    got = await client.get(f"/api/v1/accounts/{acc.id}/broker-credentials", headers=TOKEN)
    assert got.json()["configured"] is True
    _no_secrets(got.text)
    _no_secrets((await client.get(f"/api/v1/accounts/{acc.id}", headers=TOKEN)).text)
    async with TestingSessionLocal() as s:
        row = (await s.execute(select(BrokerCredential).where(BrokerCredential.account_id == acc.id))).scalar_one()
        assert row.broker == "groww"
        _no_secrets(row.encrypted_payload)
        assert vault.decrypt_groww(row.encrypted_payload).api_secret == SECRETS["api_secret"]


@pytest.mark.asyncio
async def test_invalid_groww_credential_payloads_do_not_echo_secrets(client, db_session, flag_on):
    acc = await _groww_account(db_session, creds=False)
    for bad in ({"api_key": SECRETS["api_key"]}, {**SECRETS, "pin": "1234"}, {**SECRETS, "totp_secret": "x"}):
        r = await client.put(f"/api/v1/accounts/{acc.id}/broker-credentials", json=bad, headers=TOKEN)
        assert r.status_code == 400
        _no_secrets(r.text)
    async with TestingSessionLocal() as s:
        assert (await s.execute(select(BrokerCredential))).scalars().all() == []


@pytest.mark.asyncio
async def test_alice_accounts_have_no_vault(client, db_session, flag_on):
    acc = Account(account_type=AccountType.FOLLOWER, display_name="A", alice_user_id="ALC-V", broker="alicebluenew", is_active=True)
    db_session.add(acc)
    await db_session.commit()
    for call in (client.put(f"/api/v1/accounts/{acc.id}/broker-credentials", json=SECRETS, headers=TOKEN),
                 client.post(f"/api/v1/accounts/{acc.id}/broker-login", headers=TOKEN)):
        assert (await call).status_code == 409


@pytest.mark.asyncio
async def test_broker_login_sets_the_groww_session(client, db_session, mock_redis, flag_on, monkeypatch):
    acc = await _groww_account(db_session)
    fake = FakeGroww()
    monkeypatch.setattr("app.api.accounts.build_groww_client",
                        lambda a, r, http_client=None: make_client(r, fake, account_id=str(a.id)))
    resp = await client.post(f"/api/v1/accounts/{acc.id}/broker-login", headers=TOKEN)
    assert resp.status_code == 200 and resp.json()["session_valid"] is True
    assert f"groww:session:{acc.id}" in mock_redis._storage and f"alice:session:{acc.id}" not in mock_redis._storage
    assert len(fake.calls("/v1/token/api/access")) == 1
    _no_secrets(resp.text)


@pytest.mark.asyncio
async def test_broker_login_failure_is_a_401_that_mentions_the_daily_approval(client, db_session, mock_redis, flag_on, monkeypatch):
    acc = await _groww_account(db_session)
    fake = FakeGroww()
    fake.token_response = (401, {"status": "FAILURE"})
    monkeypatch.setattr("app.api.accounts.build_groww_client",
                        lambda a, r, http_client=None: make_client(r, fake, account_id=str(a.id)))
    resp = await client.post(f"/api/v1/accounts/{acc.id}/broker-login", headers=TOKEN)
    assert resp.status_code == 401 and "approval" in resp.text
    _no_secrets(resp.text)
    assert f"groww:session:{acc.id}" not in mock_redis._storage


@pytest.mark.asyncio
async def test_broker_login_without_credentials_is_409(client, db_session, flag_on):
    acc = await _groww_account(db_session, creds=False)
    assert (await client.post(f"/api/v1/accounts/{acc.id}/broker-login", headers=TOKEN)).status_code == 409


@pytest.mark.asyncio
async def test_the_list_reads_the_groww_session_key(client, db_session, mock_redis, flag_on, monkeypatch):
    acc = await _groww_account(db_session)
    fake = FakeGroww()
    monkeypatch.setattr("app.api.accounts.build_groww_client",
                        lambda a, r, http_client=None: make_client(r, fake, account_id=str(a.id)))
    await client.post(f"/api/v1/accounts/{acc.id}/broker-login", headers=TOKEN)
    listing = await client.get("/api/v1/accounts", headers=TOKEN)
    assert [a["session_valid"] for a in listing.json() if a["id"] == str(acc.id)] == [True]


@pytest.mark.asyncio
async def test_the_angel_login_route_still_works_for_angel(client, db_session, mock_redis, monkeypatch):
    """The generalised login route keeps /angel-login for Angel One accounts."""
    from angel_helpers import FakeAngel, make_client as angel_client
    monkeypatch.setattr(settings, "ANGEL_ONE_ENABLED", True)
    monkeypatch.setattr(settings, "ANGEL_ONE_CHILD_TRADING_ENABLED", True)
    acc = Account(id=uuid.uuid4(), account_type=AccountType.FOLLOWER, display_name="ang", alice_user_id="ANG-X",
                  broker="angelone", is_active=True, static_ip_address="10.0.0.9")
    db_session.add(acc)
    await db_session.flush()
    db_session.add(BrokerCredential(account_id=acc.id, broker="angelone", encrypted_payload=vault.encrypt_angel(
        vault.validate_angel_payload({"api_key": "k", "pin": "1", "totp_secret": "JBSWY3DPEHPK3PXP"}))))
    await db_session.commit()
    fake = FakeAngel()
    monkeypatch.setattr("app.api.accounts.build_angel_client",
                        lambda a, r, http_client=None: angel_client(r, fake, account_id=str(a.id)))
    resp = await client.post(f"/api/v1/accounts/{acc.id}/angel-login", headers=TOKEN)
    assert resp.status_code == 200 and f"angel:session:{acc.id}" in mock_redis._storage
