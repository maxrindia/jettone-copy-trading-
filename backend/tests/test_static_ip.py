"""Static IP expiry tracking: identity, status, counting, and the account API.

Nothing here touches a broker: the account endpoints run against the in-memory
test database with an isolated runtime manager (no clients are built for these
accounts because none has a session).
"""
from datetime import date
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.api.accounts import get_runtime_client_manager
from app.core.database import get_db
from app.core.redis import get_redis
from app.main import app
from app.models.accounts import Account
from app.services.alice_blue.manager import AliceBlueAccountManager
from app.services.static_ip import (
    ACTIVE,
    EXPIRED,
    UNTRACKED,
    account_static_ip_host,
    extract_proxy_url,
    normalize_host,
    static_ip_status,
    summarize_static_ips,
)
from conftest import TestingSessionLocal

TOKEN = {"X-Internal-Token": "test-internal-token"}
TODAY = date(2026, 10, 8)


def acct(host=None, expiry=None, scheme="https://u:p@", port=""):
    notes = "Broker: alicebluenew" + (f", ProxyURL: {scheme}{host}{port}" if host else "")
    return SimpleNamespace(notes=notes, proxy_expires_on=expiry)


# ── identity ────────────────────────────────────────────────────────────────

def test_same_host_regardless_of_credentials_scheme_port_and_case():
    variants = [
        "178.18.252.165",
        "178.18.252.165:443",
        "https://user:pass@178.18.252.165:443",
        "http://u:p@178.18.252.165:8080/",
    ]
    assert {normalize_host(v) for v in variants} == {"178.18.252.165"}
    assert normalize_host("https://U:P@DC-Mum-007.StaticIP.in:8443") == "dc-mum-007.staticip.in"


def test_credentials_with_special_characters_do_not_leak_into_the_host():
    assert normalize_host("https://us%40er:p%40ss%3Aword@host.example.com:443") == "host.example.com"


def test_no_proxy_means_no_host():
    assert extract_proxy_url("Broker: alicebluenew, APIKey: x") is None
    assert account_static_ip_host(None) is None
    assert account_static_ip_host("") is None


def test_legacy_bare_url_in_notes_is_still_a_proxy():
    assert account_static_ip_host("https://u:p@legacy.example.com") == "legacy.example.com"


# ── expiry status (date only, last valid day inclusive) ─────────────────────

def test_status_active_expired_untracked():
    notes = "ProxyURL: https://u:p@h.example.com"
    assert static_ip_status(notes, date(2026, 10, 15), TODAY) == ACTIVE
    assert static_ip_status(notes, date(2026, 10, 1), TODAY) == EXPIRED
    assert static_ip_status(notes, None, TODAY) == UNTRACKED


def test_expiry_day_itself_is_still_active_and_next_day_is_expired():
    notes = "ProxyURL: https://u:p@h.example.com"
    assert static_ip_status(notes, TODAY, TODAY) == ACTIVE
    assert static_ip_status(notes, date(2026, 10, 7), TODAY) == EXPIRED


def test_no_static_ip_has_no_status_even_with_a_stray_expiry():
    assert static_ip_status("Broker: alicebluenew", date(2020, 1, 1), TODAY) is None


# ── counting ────────────────────────────────────────────────────────────────

def test_spec_example_counts_one():
    accounts = [
        acct("178.18.252.165", date(2026, 10, 15)),
        acct("178.18.252.165", date(2026, 10, 20)),
        acct("dc-mum-007.staticip.in", date(2026, 10, 1)),
    ]
    s = summarize_static_ips(accounts, TODAY)
    assert (s.available, s.active, s.expired, s.untracked) == (1, 1, 1, 0)


def test_same_host_different_ports_and_case_counts_once():
    s = summarize_static_ips(
        [acct("A.example.com", date(2026, 11, 1), port=":443"), acct("a.example.com", date(2026, 11, 1), port=":8080")],
        TODAY,
    )
    assert s.available == 1


def test_null_expiry_is_available_but_untracked_never_expired():
    s = summarize_static_ips([acct("a.example.com", None), acct("b.example.com", date(2026, 10, 9))], TODAY)
    assert (s.available, s.active, s.untracked, s.expired) == (2, 1, 1, 0)


def test_expired_excluded_and_accounts_without_static_ip_ignored():
    s = summarize_static_ips([acct("a.example.com", date(2026, 1, 1)), acct(None, None), acct(None, date(2020, 1, 1))], TODAY)
    assert (s.available, s.expired) == (0, 1)


def test_host_valid_on_any_account_stays_available():
    s = summarize_static_ips([acct("a.example.com", date(2026, 1, 1)), acct("a.example.com", date(2026, 12, 1))], TODAY)
    assert (s.available, s.expired) == (1, 0)


def test_no_accounts_is_zero():
    assert summarize_static_ips([], TODAY).available == 0


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


def payload(user_id, host=None, expiry=None, **extra):
    body = {
        "account_type": "FOLLOWER",
        "display_name": f"Acct {user_id}",
        "alice_user_id": user_id,
        "api_secret": "secret",
        "notes": "Broker: alicebluenew" + (f", ProxyURL: https://u:p@{host}" if host else ""),
    }
    if expiry is not None:
        body["proxy_expires_on"] = expiry
    body.update(extra)
    return body


@pytest.mark.asyncio
async def test_create_account_with_expiry_returns_status_and_host(client):
    resp = await client.post("/api/v1/accounts", json=payload("SIP1", "Host.Example.com:8443", "2099-01-01"), headers=TOKEN)
    assert resp.status_code == 201
    body = resp.json()
    assert body["proxy_expires_on"] == "2099-01-01"
    assert body["static_ip_host"] == "host.example.com"
    assert body["static_ip_status"] == "ACTIVE"
    assert "u:p" not in resp.text.replace(body["notes"], "")  # credentials only appear in the notes field already stored


@pytest.mark.asyncio
async def test_past_expiry_reports_expired(client):
    resp = await client.post("/api/v1/accounts", json=payload("SIP2", "old.example.com", "2020-01-01"), headers=TOKEN)
    assert resp.json()["static_ip_status"] == "EXPIRED"


@pytest.mark.asyncio
async def test_account_without_expiry_stays_valid_and_untracked(client):
    resp = await client.post("/api/v1/accounts", json=payload("SIP3", "new.example.com"), headers=TOKEN)
    assert resp.status_code == 201
    body = resp.json()
    assert body["proxy_expires_on"] is None
    assert body["static_ip_status"] == "UNTRACKED"


@pytest.mark.asyncio
async def test_account_without_static_ip_has_no_status(client):
    resp = await client.post("/api/v1/accounts", json=payload("SIP4"), headers=TOKEN)
    body = resp.json()
    assert body["static_ip_host"] is None and body["static_ip_status"] is None


@pytest.mark.asyncio
async def test_existing_account_without_the_field_still_lists(client, db_session):
    db_session.add(Account(account_type="FOLLOWER", display_name="Legacy", alice_user_id="LEGACY1", notes="Broker: alicebluenew"))
    await db_session.commit()
    resp = await client.get("/api/v1/accounts", headers=TOKEN)
    assert resp.status_code == 200
    row = next(a for a in resp.json() if a["alice_user_id"] == "LEGACY1")
    assert row["proxy_expires_on"] is None and row["static_ip_status"] is None


@pytest.mark.asyncio
async def test_renewal_updates_the_expiry_without_creating_another_account(client):
    created = (await client.post("/api/v1/accounts", json=payload("SIP5", "renew.example.com", "2020-01-01"), headers=TOKEN)).json()
    assert created["static_ip_status"] == "EXPIRED"
    resp = await client.put(f"/api/v1/accounts/{created['id']}", json={"proxy_expires_on": "2099-02-01"}, headers=TOKEN)
    assert resp.status_code == 200
    assert resp.json()["proxy_expires_on"] == "2099-02-01"
    assert resp.json()["static_ip_status"] == "ACTIVE"
    accounts = (await client.get("/api/v1/accounts", headers=TOKEN)).json()
    assert len([a for a in accounts if a["alice_user_id"] == "SIP5"]) == 1


@pytest.mark.asyncio
async def test_renewing_a_shared_ip_updates_every_account_using_it(client):
    a = (await client.post("/api/v1/accounts", json=payload("SIP6A", "shared.example.com", "2020-01-01"), headers=TOKEN)).json()
    b = (await client.post("/api/v1/accounts", json=payload("SIP6B", "SHARED.example.com:8443", "2020-01-01"), headers=TOKEN)).json()
    other = (await client.post("/api/v1/accounts", json=payload("SIP6C", "different.example.com", "2020-01-01"), headers=TOKEN)).json()
    await client.put(f"/api/v1/accounts/{a['id']}", json={"proxy_expires_on": "2099-03-01"}, headers=TOKEN)
    rows = {r["alice_user_id"]: r for r in (await client.get("/api/v1/accounts", headers=TOKEN)).json()}
    assert rows["SIP6A"]["proxy_expires_on"] == rows["SIP6B"]["proxy_expires_on"] == "2099-03-01"
    assert rows["SIP6C"]["proxy_expires_on"] == "2020-01-01"  # a different IP is untouched


@pytest.mark.asyncio
async def test_clearing_the_expiry_returns_to_untracked(client):
    created = (await client.post("/api/v1/accounts", json=payload("SIP7", "clear.example.com", "2099-01-01"), headers=TOKEN)).json()
    resp = await client.put(f"/api/v1/accounts/{created['id']}", json={"proxy_expires_on": None}, headers=TOKEN)
    assert resp.json()["proxy_expires_on"] is None
    assert resp.json()["static_ip_status"] == "UNTRACKED"


@pytest.mark.asyncio
async def test_updating_other_fields_does_not_touch_expiry(client):
    created = (await client.post("/api/v1/accounts", json=payload("SIP8", "keep.example.com", "2099-01-01"), headers=TOKEN)).json()
    resp = await client.put(f"/api/v1/accounts/{created['id']}", json={"display_name": "Renamed"}, headers=TOKEN)
    assert resp.json()["proxy_expires_on"] == "2099-01-01"


@pytest.mark.asyncio
async def test_invalid_expiry_is_rejected(client):
    resp = await client.post("/api/v1/accounts", json=payload("SIP9", "x.example.com", "not-a-date"), headers=TOKEN)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_kpi_endpoint_counts_distinct_non_expired_hosts(client):
    await client.post("/api/v1/accounts", json=payload("K1", "178.18.252.165", "2099-10-15"), headers=TOKEN)
    await client.post("/api/v1/accounts", json=payload("K2", "178.18.252.165:443", "2099-10-20"), headers=TOKEN)
    await client.post("/api/v1/accounts", json=payload("K3", "dc-mum-007.staticip.in", "2020-10-01"), headers=TOKEN)
    await client.post("/api/v1/accounts", json=payload("K4", "untracked.example.com"), headers=TOKEN)
    await client.post("/api/v1/accounts", json=payload("K5"), headers=TOKEN)
    resp = await client.get("/api/v1/system/static-ip", headers=TOKEN)
    assert resp.status_code == 200
    body = resp.json()
    assert body["active_count"] == 1
    assert body["expired_count"] == 1
    assert body["untracked_count"] == 1
    assert body["available_count"] == 2


@pytest.mark.asyncio
async def test_soft_deleted_account_is_not_counted(client, db_session):
    created = (await client.post("/api/v1/accounts", json=payload("K6", "gone.example.com", "2099-01-01"), headers=TOKEN)).json()
    await client.delete(f"/api/v1/accounts/{created['id']}", headers=TOKEN)
    body = (await client.get("/api/v1/system/static-ip", headers=TOKEN)).json()
    assert body["available_count"] == 0
