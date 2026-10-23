"""Angel One foundation: encrypted credential vault, migration 0012, credential /
login APIs, broker-neutral manager routing and the flag-aware registry.
Offline: no request leaves the process (FakeAngel / mock Redis / in-memory DB)."""
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.api.accounts import get_runtime_client_manager
from app.core.config import settings
from app.core.crypto import encrypt_secret
from app.core.database import get_db
from app.core.redis import get_redis
from app.main import app
from app.models.accounts import Account, AccountType, ApiCredential, BrokerCredential
from app.services.alice_blue.manager import AliceBlueAccountManager
from app.services.angel_one.client import AngelOneClient
from app.services.angel_one.contract import ANGEL_CONTRACT
from app.services.brokers import credential_vault as vault
from app.services.brokers.angel_builder import accounts_sharing_static_ip, build_angel_client
from app.services.brokers.catalog import catalog_with_support
from app.services.brokers.registry import (
    BrokerSupport, can_be_child, get_broker_spec, persisted_order_label, require_engine_broker,
    session_key_for, UnsupportedBrokerError,
)
from conftest import TestingSessionLocal
from angel_helpers import SYNTHETIC_CONTRACT, FakeAngel, make_client

TOKEN = {"X-Internal-Token": "test-internal-token"}
SECRETS = {"api_key": "AK-SECRET-111", "pin": "9876", "totp_secret": "JBSWY3DPEHPK3PXP"}


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(settings, "ANGEL_ONE_ENABLED", True)
    monkeypatch.setattr(settings, "ANGEL_ONE_CHILD_TRADING_ENABLED", True)


@pytest.fixture
def verified(monkeypatch):
    monkeypatch.setattr("app.services.brokers.registry.ANGEL_CONTRACT", SYNTHETIC_CONTRACT)


# ── registry (flag + contract aware) ────────────────────────────────────────

def test_flag_off_is_not_supported_everywhere(monkeypatch):
    monkeypatch.setattr(settings, "ANGEL_ONE_ENABLED", False)
    spec = get_broker_spec("angelone")
    assert spec.support == BrokerSupport.NOT_SUPPORTED and not spec.capabilities.login
    assert not can_be_child("angelone")
    with pytest.raises(UnsupportedBrokerError):
        require_engine_broker("angelone")
    row = {r["id"]: r for r in catalog_with_support()}["angelone"]
    assert row["selectable"] is False


def test_flag_on_but_unverified_contract_allows_login_not_copying(flag_on):
    assert ANGEL_CONTRACT.verified is False
    spec = get_broker_spec("angelone")
    assert spec.support == BrokerSupport.PARTIAL
    assert spec.capabilities.login is True and spec.capabilities.as_child is False
    assert spec.capabilities.as_master is False
    assert require_engine_broker("angelone").id == "angelone"
    assert can_be_child("angelone") is False
    row = {r["id"]: r for r in catalog_with_support()}["angelone"]
    assert row["selectable"] is True and row["capabilities"] == {"login": True, "as_master": False, "as_child": False}
    assert "not verified" in row["support_note"]


def test_flag_on_and_verified_contract_is_child_only(flag_on, verified):
    spec = get_broker_spec("angelone")
    assert spec.capabilities.as_child is True and spec.capabilities.as_master is False
    assert can_be_child("angelone") is True


def test_alice_blue_is_unchanged_by_the_angel_flag(flag_on):
    spec = get_broker_spec(None)
    assert spec.id == "alicebluenew" and spec.support == BrokerSupport.SUPPORTED
    assert spec.capabilities.as_master and spec.capabilities.as_child and can_be_child("alicebluenew")


def test_session_keys_and_order_labels():
    aid = uuid.uuid4()
    assert session_key_for(None, aid) == f"alice:session:{aid}"
    assert session_key_for("alicebluenew", aid) == f"alice:session:{aid}"
    assert session_key_for("angelone", aid) == f"angel:session:{aid}"
    assert session_key_for("who-knows", aid) == f"alice:session:{aid}"        # legacy default
    assert persisted_order_label(None) == "ALICE_BLUE"
    assert persisted_order_label("alicebluenew") == "ALICE_BLUE"
    assert persisted_order_label("angelone") == "ANGEL_ONE"
    assert persisted_order_label("who-knows") == "ALICE_BLUE"


# ── vault ───────────────────────────────────────────────────────────────────

def test_vault_roundtrip_and_ciphertext_has_no_plaintext():
    creds = vault.validate_angel_payload(SECRETS)
    blob = vault.encrypt_angel(creds)
    for secret in SECRETS.values():
        assert secret not in blob
    assert vault.decrypt_angel(blob) == creds
    assert "AK-SECRET" not in repr(creds) and "9876" not in repr(creds)


@pytest.mark.parametrize("bad,needle", [
    ({"api_key": "k", "pin": "1"}, "totp_secret"),
    ({"api_key": "", "pin": "1", "totp_secret": "JBSWY3DPEHPK3PXP"}, "api_key"),
    ({"api_key": "k", "pin": "1", "totp_secret": "not base32 !!!"}, "base32"),
])
def test_vault_rejects_bad_payloads_without_echoing_values(bad, needle):
    with pytest.raises(vault.CredentialError) as ei:
        vault.validate_angel_payload(bad)
    assert needle in str(ei.value)
    for v in bad.values():
        assert not v or v not in str(ei.value) or v == needle


def test_vault_describe_never_returns_values():
    class Row:
        encrypted_payload = vault.encrypt_angel(vault.validate_angel_payload(SECRETS))
    out = vault.describe(Row())
    assert out == {"configured": True, "fields": ["api_key", "pin", "totp_secret"]}
    assert vault.describe(None) == {"configured": False, "fields": []}

    class Broken:
        encrypted_payload = "garbage"
    assert vault.describe(Broken())["configured"] is False


# ── migration ───────────────────────────────────────────────────────────────

def test_migration_0012_is_additive_and_the_chain_has_one_head():
    from alembic.script import ScriptDirectory
    root = Path(__file__).resolve().parent.parent / "alembic"
    script = ScriptDirectory(str(root))
    # 0012 was the head when it was written; later additive migrations may follow it, but the history must stay linear
    # (one head) and 0012 must still be part of it.
    assert len(script.get_heads()) == 1
    assert "0012_broker_credentials" in {r.revision for r in script.walk_revisions()}
    rev = script.get_revision("0012_broker_credentials")
    assert rev.down_revision == "0011_account_static_ip_address"
    src = (root / "versions" / "0012_broker_credentials.py").read_text(encoding="utf-8")
    up = src.split("def upgrade", 1)[1].split("def downgrade", 1)[0].upper()
    assert "CREATE TABLE IF NOT EXISTS BROKER_CREDENTIALS" in up
    import re
    for forbidden in (r"\bDROP\b", r"\bALTER\s+TABLE\b", r"\bDELETE\s+FROM\b", r"\bUPDATE\s+\w+\s+SET\b",
                      r"\bTRUNCATE\b", r"\bINSERT\s+INTO\b"):
        assert not re.search(forbidden, up), forbidden
    # every model column is in the migration SQL
    for col in BrokerCredential.__table__.columns:
        assert col.name in src


# ── manager routing ─────────────────────────────────────────────────────────

async def _angel_account(db, name="ang", ip="151.245.182.10", creds=True, broker="angelone", active=True):
    acc = Account(id=uuid.uuid4(), account_type=AccountType.FOLLOWER, display_name=name,
                  alice_user_id=f"A_{name}_{uuid.uuid4().hex[:4]}", broker=broker, is_active=active, static_ip_address=ip)
    db.add(acc)
    await db.flush()
    if creds:
        db.add(BrokerCredential(account_id=acc.id, broker="angelone",
                                encrypted_payload=vault.encrypt_angel(vault.validate_angel_payload(SECRETS))))
    await db.commit()
    return acc


@pytest.mark.asyncio
async def test_build_angel_client_uses_vault_credentials_and_account_ip(db_session, mock_redis):
    acc = await _angel_account(db_session, ip="151.245.182.77")
    async with TestingSessionLocal() as s:
        loaded = (await s.execute(select(Account).where(Account.id == acc.id))).scalar_one()
        await s.refresh(loaded, ["broker_credential"])
        client = build_angel_client(loaded, mock_redis)
    assert isinstance(client, AngelOneClient)
    assert client.static_ip == "151.245.182.77" and client.user_id == loaded.alice_user_id
    assert "9876" not in repr(client)


def test_build_angel_client_without_credentials_fails(mock_redis):
    class A:
        id = uuid.uuid4(); alice_user_id = "X"; broker_credential = None
    with pytest.raises(vault.CredentialError):
        build_angel_client(A(), mock_redis)


def test_accounts_sharing_a_static_ip_are_detected():
    mk = lambda ip: type("A", (), {"id": uuid.uuid4(), "static_ip_address": ip})()
    a, b, c, d = mk("1.1.1.1"), mk("1.1.1.1"), mk("2.2.2.2"), mk(None)
    assert set(accounts_sharing_static_ip([a, b, c, d])) == {str(a.id), str(b.id)}


@pytest.mark.asyncio
async def test_manager_initialize_registers_angel_only_when_enabled_and_credentialed(db_session, mock_redis, monkeypatch):
    monkeypatch.setattr("app.services.alice_blue.manager.AsyncSessionLocal", TestingSessionLocal)
    from unittest.mock import AsyncMock
    monkeypatch.setattr("app.services.alice_blue.manager.get_redis", AsyncMock(return_value=mock_redis))
    ok = await _angel_account(db_session, "ok", ip="10.0.0.1")
    nocred = await _angel_account(db_session, "nocred", ip="10.0.0.2", creds=False)
    shared1 = await _angel_account(db_session, "s1", ip="10.0.0.3")
    shared2 = await _angel_account(db_session, "s2", ip="10.0.0.3")

    monkeypatch.setattr(settings, "ANGEL_ONE_ENABLED", False)
    mgr = AliceBlueAccountManager()
    await mgr.initialize()
    assert mgr.get_all_account_ids() == []                                   # flag off: no Angel client at all

    monkeypatch.setattr(settings, "ANGEL_ONE_ENABLED", True)
    monkeypatch.setattr(settings, "ANGEL_ONE_CHILD_TRADING_ENABLED", True)
    mgr = AliceBlueAccountManager()
    await mgr.initialize()
    assert mgr.get_all_account_ids() == [str(ok.id)]                         # no credentials / shared IP: skipped
    assert isinstance(mgr.get_client(str(ok.id)), AngelOneClient)
    for skipped in (nocred, shared1, shared2):
        assert mgr.get_client(str(skipped.id)) is None


@pytest.mark.asyncio
async def test_sync_account_needs_a_session_for_angel(db_session, mock_redis, flag_on):
    acc = await _angel_account(db_session)
    mgr = AliceBlueAccountManager()
    res = await mgr.sync_account(str(acc.id), db_session, mock_redis)
    assert res.status == "FAILED" and res.ready is False                     # no Angel session yet
    mock_redis._storage[f"angel:session:{acc.id}"] = "garbage"               # unreadable session: still fails closed
    res = await mgr.sync_account(str(acc.id), db_session, mock_redis)
    assert res.status == "FAILED"
    import json
    mock_redis._storage[f"angel:session:{acc.id}"] = encrypt_secret(json.dumps({"jwt": "j"}))
    res = await mgr.sync_account(str(acc.id), db_session, mock_redis)
    assert res.status == "REGISTERED" and res.ready is True


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
async def test_credentials_endpoint_is_closed_when_the_flag_is_off(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "ANGEL_ONE_ENABLED", False)
    acc = await _angel_account(db_session, creds=False)
    resp = await client.put(f"/api/v1/accounts/{acc.id}/broker-credentials", json=SECRETS, headers=TOKEN)
    assert resp.status_code == 409
    _no_secrets(resp.text)


@pytest.mark.asyncio
async def test_credentials_are_stored_encrypted_and_never_returned(client, db_session, flag_on):
    acc = await _angel_account(db_session, creds=False)
    resp = await client.put(f"/api/v1/accounts/{acc.id}/broker-credentials", json=SECRETS, headers=TOKEN)
    assert resp.status_code == 200 and resp.json() == {"configured": True, "fields": ["api_key", "pin", "totp_secret"]}
    _no_secrets(resp.text)
    got = await client.get(f"/api/v1/accounts/{acc.id}/broker-credentials", headers=TOKEN)
    assert got.json()["configured"] is True
    _no_secrets(got.text)
    detail = await client.get(f"/api/v1/accounts/{acc.id}", headers=TOKEN)
    _no_secrets(detail.text)
    async with TestingSessionLocal() as s:
        row = (await s.execute(select(BrokerCredential).where(BrokerCredential.account_id == acc.id))).scalar_one()
        assert row.broker == "angelone"
        _no_secrets(row.encrypted_payload)
        assert vault.decrypt_angel(row.encrypted_payload).pin == "9876"


@pytest.mark.asyncio
async def test_invalid_credential_payloads_do_not_echo_secrets(client, db_session, flag_on):
    acc = await _angel_account(db_session, creds=False)
    resp = await client.put(f"/api/v1/accounts/{acc.id}/broker-credentials",
                            json={"api_key": SECRETS["api_key"], "pin": SECRETS["pin"]}, headers=TOKEN)
    assert resp.status_code == 400 and "totp_secret" in resp.text
    _no_secrets(resp.text)
    resp = await client.put(f"/api/v1/accounts/{acc.id}/broker-credentials", json={**SECRETS, "extra": "x"}, headers=TOKEN)
    assert resp.status_code == 400
    _no_secrets(resp.text)
    async with TestingSessionLocal() as s:
        assert (await s.execute(select(BrokerCredential))).scalars().all() == []


@pytest.mark.asyncio
async def test_new_credentials_invalidate_the_session(client, db_session, flag_on):
    acc = await _angel_account(db_session)
    async with TestingSessionLocal() as s:
        row = await s.get(Account, acc.id)
        row.session_valid = True
        row.last_authenticated_at = datetime.now(timezone.utc)
        await s.commit()
    resp = await client.put(f"/api/v1/accounts/{acc.id}/broker-credentials", json=SECRETS, headers=TOKEN)
    assert resp.status_code == 200
    async with TestingSessionLocal() as s:
        row = await s.get(Account, acc.id)
        assert row.session_valid is False and row.last_authenticated_at is None


@pytest.mark.asyncio
async def test_alice_accounts_have_no_vault_endpoint(client, db_session, flag_on):
    acc = Account(account_type=AccountType.FOLLOWER, display_name="A", alice_user_id="ALC-V", broker="alicebluenew", is_active=True)
    db_session.add(acc)
    await db_session.commit()
    for call in (client.put(f"/api/v1/accounts/{acc.id}/broker-credentials", json=SECRETS, headers=TOKEN),
                 client.get(f"/api/v1/accounts/{acc.id}/broker-credentials", headers=TOKEN),
                 client.post(f"/api/v1/accounts/{acc.id}/angel-login", headers=TOKEN)):
        assert (await call).status_code == 409


@pytest.mark.asyncio
async def test_creating_an_angel_account_stores_no_api_credential(client, db_session, flag_on):
    resp = await client.post("/api/v1/accounts", headers=TOKEN, json={
        "account_type": "FOLLOWER", "display_name": "Angel 1", "alice_user_id": "ANG-NEW",
        "api_secret": "should-not-be-stored", "broker": "angelone"})
    assert resp.status_code == 201
    async with TestingSessionLocal() as s:
        assert (await s.execute(select(ApiCredential))).scalars().all() == []


@pytest.mark.asyncio
async def test_creating_an_alice_account_still_stores_its_api_credential(client, db_session, flag_on):
    resp = await client.post("/api/v1/accounts", headers=TOKEN, json={
        "account_type": "FOLLOWER", "display_name": "Alice 1", "alice_user_id": "ALC-NEW",
        "api_secret": "alice-secret", "broker": "alicebluenew"})
    assert resp.status_code == 201
    async with TestingSessionLocal() as s:
        assert len((await s.execute(select(ApiCredential))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_angel_login_sets_the_session(client, db_session, mock_redis, flag_on, monkeypatch):
    acc = await _angel_account(db_session)
    fake = FakeAngel()
    monkeypatch.setattr("app.api.accounts.build_angel_client",
                        lambda a, r, http_client=None: make_client(r, fake, account_id=str(a.id)))
    resp = await client.post(f"/api/v1/accounts/{acc.id}/angel-login", headers=TOKEN)
    assert resp.status_code == 200 and resp.json()["session_valid"] is True
    assert f"angel:session:{acc.id}" in mock_redis._storage
    assert f"alice:session:{acc.id}" not in mock_redis._storage
    assert len(fake.calls("loginByPassword")) == 1
    _no_secrets(resp.text)
    # the account list reads the Angel session key, not Alice's
    listing = await client.get("/api/v1/accounts", headers=TOKEN)
    assert [a["session_valid"] for a in listing.json() if a["id"] == str(acc.id)] == [True]


@pytest.mark.asyncio
async def test_angel_login_failure_is_a_401_without_secrets(client, db_session, mock_redis, flag_on, monkeypatch):
    acc = await _angel_account(db_session)
    fake = FakeAngel()
    fake.routes["loginByPassword"] = (200, {"status": False, "message": "Invalid totp", "errorcode": "AB1050"})
    monkeypatch.setattr("app.api.accounts.build_angel_client",
                        lambda a, r, http_client=None: make_client(r, fake, account_id=str(a.id)))
    resp = await client.post(f"/api/v1/accounts/{acc.id}/angel-login", headers=TOKEN)
    assert resp.status_code == 401 and "Invalid totp" in resp.text
    _no_secrets(resp.text)
    assert f"angel:session:{acc.id}" not in mock_redis._storage


@pytest.mark.asyncio
async def test_angel_login_without_credentials_is_409(client, db_session, flag_on):
    acc = await _angel_account(db_session, creds=False)
    resp = await client.post(f"/api/v1/accounts/{acc.id}/angel-login", headers=TOKEN)
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_angel_login_is_closed_when_the_flag_is_off(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "ANGEL_ONE_ENABLED", False)
    acc = await _angel_account(db_session)
    resp = await client.post(f"/api/v1/accounts/{acc.id}/angel-login", headers=TOKEN)
    assert resp.status_code == 409
