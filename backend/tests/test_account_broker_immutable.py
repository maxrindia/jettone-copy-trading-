"""An account's broker is fixed at creation. A PUT that tries to change it is rejected (409) and changes
nothing; an UNCHANGED broker value (the edit form re-sends it on every save) and every ordinary update
still work, and the account's credentials / session / runtime client are left exactly as they were."""
import uuid

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.accounts import Account, ApiCredential, BrokerCredential
from app.services.alice_blue.manager import AliceBlueAccountManager
from conftest import TestingSessionLocal
from test_groww_foundation import TOKEN, client, flag_on, _groww_account  # noqa: F401


def alice_payload(uid, **extra):
    body = {"account_type": "FOLLOWER", "display_name": uid, "alice_user_id": uid, "api_secret": "alice-secret",
            "broker": "alicebluenew"}
    body.update(extra)
    return body


async def snapshot(account_id):
    account_id = uuid.UUID(str(account_id))
    async with TestingSessionLocal() as s:
        acc = await s.get(Account, account_id)
        api = (await s.execute(select(ApiCredential).where(ApiCredential.account_id == account_id))).scalar_one_or_none()
        vault = (await s.execute(select(BrokerCredential).where(BrokerCredential.account_id == account_id))).scalar_one_or_none()
        return (acc.broker, acc.display_name, acc.session_valid, acc.is_active,
                api.encrypted_api_secret if api else None, vault.encrypted_payload if vault else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["groww", "angelone", "zerodha", "dhan", "unknown-broker"])
async def test_changing_an_alice_account_to_another_broker_is_rejected_and_changes_nothing(client, db_session, flag_on, monkeypatch, target):
    monkeypatch.setattr(settings, "ANGEL_ONE_ENABLED", True)
    created = (await client.post("/api/v1/accounts", json=alice_payload("ALC-IMM"), headers=TOKEN)).json()
    before = await snapshot(created["id"])
    resp = await client.put(f"/api/v1/accounts/{created['id']}", json={"broker": target, "display_name": "renamed"}, headers=TOKEN)
    assert resp.status_code == 409 and "cannot be changed" in resp.json()["detail"]
    assert await snapshot(created["id"]) == before                         # not even the display_name in the same request


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["alicebluenew", "angelone", "zerodha"])
async def test_changing_a_groww_account_is_rejected_and_keeps_its_vault_credentials(client, db_session, flag_on, monkeypatch, target):
    monkeypatch.setattr(settings, "ANGEL_ONE_ENABLED", True)
    acc = await _groww_account(db_session)
    before = await snapshot(acc.id)
    resp = await client.put(f"/api/v1/accounts/{acc.id}", json={"broker": target}, headers=TOKEN)
    assert resp.status_code == 409
    after = await snapshot(acc.id)
    assert after == before and after[0] == "groww" and after[5] is not None    # vault entry intact, broker unchanged


@pytest.mark.asyncio
@pytest.mark.parametrize("same", ["alicebluenew", "aliceblue", "Alice Blue", "ALICE_BLUE", "", None])
async def test_an_unchanged_broker_value_is_accepted_and_ordinary_fields_still_update(client, db_session, same):
    created = (await client.post("/api/v1/accounts", json=alice_payload("ALC-SAME"), headers=TOKEN)).json()
    before = await snapshot(created["id"])
    body = {"display_name": "Renamed", "notes": "Broker: alicebluenew", "static_ip_address": "151.245.182.10",
            "proxy_expires_on": "2099-01-01"}
    if same is not None:
        body["broker"] = same
    resp = await client.put(f"/api/v1/accounts/{created['id']}", json=body, headers=TOKEN)
    assert resp.status_code == 200
    out = resp.json()
    assert out["display_name"] == "Renamed" and out["static_ip_address"] == "151.245.182.10"
    after = await snapshot(created["id"])
    assert after[0] == before[0]                                           # the stored broker value is not rewritten
    assert after[4] == before[4]                                           # nor is the credential touched


@pytest.mark.asyncio
async def test_an_unchanged_groww_broker_is_accepted(client, db_session, flag_on):
    acc = await _groww_account(db_session)
    before = await snapshot(acc.id)
    resp = await client.put(f"/api/v1/accounts/{acc.id}", json={"broker": "groww", "display_name": "My Groww"}, headers=TOKEN)
    assert resp.status_code == 200 and resp.json()["display_name"] == "My Groww"
    after = await snapshot(acc.id)
    assert after[0] == "groww" and after[5] == before[5]


@pytest.mark.asyncio
async def test_a_rejected_change_leaves_the_runtime_client_registered(db_session, mock_redis, monkeypatch):
    """The guard runs before anything reaches the runtime manager, so the registered client survives."""
    import httpx
    from app.api.accounts import get_runtime_client_manager
    from app.core.database import get_db
    from app.core.redis import get_redis
    from app.main import app

    async def override_db():
        async with TestingSessionLocal() as session:
            yield session

    manager = AliceBlueAccountManager()
    sentinel = object()
    manager._clients["placeholder"] = sentinel
    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_redis] = lambda: mock_redis
    app.dependency_overrides[get_runtime_client_manager] = lambda: manager
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
            created = (await c.post("/api/v1/accounts", json=alice_payload("ALC-RT"), headers=TOKEN)).json()
            manager._clients[created["id"]] = sentinel
            resp = await c.put(f"/api/v1/accounts/{created['id']}", json={"broker": "groww", "is_active": False}, headers=TOKEN)
            assert resp.status_code == 409
            assert manager._clients[created["id"]] is sentinel                # still registered
            assert (await snapshot(created["id"]))[3] is True                 # and still active
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_a_deleted_account_can_only_come_back_on_its_original_broker(client, db_session, flag_on):
    created = (await client.post("/api/v1/accounts", json=alice_payload("ALC-DEL"), headers=TOKEN)).json()
    assert (await client.delete(f"/api/v1/accounts/{created['id']}", headers=TOKEN)).status_code == 204
    wrong = await client.post("/api/v1/accounts", json=alice_payload("ALC-DEL", broker="groww"), headers=TOKEN)
    assert wrong.status_code == 409 and "different broker" in wrong.json()["detail"]
    async with TestingSessionLocal() as s:                                    # still deleted, still Alice Blue
        row = (await s.execute(select(Account).where(Account.id == uuid.UUID(created["id"])))).scalar_one()
        assert row.deleted_at is not None and row.broker == "alicebluenew"
    same = await client.post("/api/v1/accounts", json=alice_payload("ALC-DEL"), headers=TOKEN)
    assert same.status_code == 201 and same.json()["id"] == created["id"]


@pytest.mark.asyncio
async def test_other_update_validation_is_unchanged(client, db_session):
    created = (await client.post("/api/v1/accounts", json=alice_payload("ALC-VAL"), headers=TOKEN)).json()
    bad_ip = await client.put(f"/api/v1/accounts/{created['id']}", json={"static_ip_address": "not-an-ip"}, headers=TOKEN)
    assert bad_ip.status_code == 422
    missing = await client.put("/api/v1/accounts/00000000-0000-4000-8000-000000000000", json={"display_name": "x"}, headers=TOKEN)
    assert missing.status_code == 404
