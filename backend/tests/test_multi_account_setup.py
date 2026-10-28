"""
Multi-broker account setup: several accounts, from different brokers and from the same broker, are registered, stored, shown and
reconnected independently. Offline only: fake broker transports, an in-memory database and a fake Redis. No real login, no real
broker account, and nothing here enables copy trading for any broker.

What is pinned:
  * identity is (broker, the broker's own user id, case-insensitive), not the user id alone
  * each account's credentials live in its own vault row; registering the same real account twice is refused
  * a login, a failed login or a relogin changes only the account it was for
  * the server reports each account's connection state and why a Master / Child role is blocked
"""
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.accounts import Account, BrokerCredential
from app.services.brokers import credential_vault as vault
from app.services.zerodha.client import ZerodhaClient
from conftest import TestingSessionLocal
from test_groww_foundation import TOKEN, client  # noqa: F401  (client is a fixture)
import groww_helpers as G
import zerodha_helpers as Z

TOTP = "JBSWY3DPEHPK3PXP"


@pytest.fixture
def setup_flags(monkeypatch):
    """Every non-Alice broker enabled for account SETUP only. No Child or Master trading flag is turned on."""
    for name in ("GROWW_ENABLED", "ANGEL_ONE_ENABLED", "ZERODHA_ENABLED"):
        monkeypatch.setattr(settings, name, True)
    for name in ("GROWW_CHILD_TRADING_ENABLED", "GROWW_MASTER_ENABLED", "ANGEL_ONE_CHILD_TRADING_ENABLED",
                 "ANGEL_ONE_MASTER_ENABLED", "ZERODHA_CHILD_TRADING_ENABLED", "ZERODHA_MASTER_ENABLED"):
        monkeypatch.setattr(settings, name, False)


def body(broker, uid, name=None):
    return {"account_type": "FOLLOWER", "display_name": name or uid, "alice_user_id": uid, "api_secret": "stored-in-vault",
            "broker": broker}


async def add(client, broker, uid, name=None):
    resp = await client.post("/api/v1/accounts", json=body(broker, uid, name), headers=TOKEN)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def put_creds(client, account_id, payload):
    return await client.put(f"/api/v1/accounts/{account_id}/broker-credentials", json=payload, headers=TOKEN)


async def listed(client):
    resp = await client.get("/api/v1/accounts", headers=TOKEN)
    assert resp.status_code == 200
    return {a["id"]: a for a in resp.json()}


async def vault_row(account_id):
    async with TestingSessionLocal() as s:
        return (await s.execute(select(BrokerCredential).where(BrokerCredential.account_id == uuid.UUID(str(account_id))))).scalar_one_or_none()


async def account_row(account_id):
    async with TestingSessionLocal() as s:
        return await s.get(Account, uuid.UUID(str(account_id)))


GROWW_A = {"api_key": "GRW-KEY-A", "api_secret": "GRW-SECRET-A"}
GROWW_B = {"api_key": "GRW-KEY-B", "api_secret": "GRW-SECRET-B"}


# ── identity: several accounts, across and within brokers ──────────────────────────────────────

@pytest.mark.asyncio
async def test_accounts_from_four_different_brokers_coexist_even_with_the_same_user_id_string(client, db_session, setup_flags):
    created = {b: await add(client, b, "SAME-ID", f"{b} account") for b in ("alicebluenew", "groww", "angelone", "zerodha")}
    assert len({c["id"] for c in created.values()}) == 4
    shown = await listed(client)
    for broker, c in created.items():
        assert shown[c["id"]]["broker"] == broker and shown[c["id"]]["alice_user_id"] == "SAME-ID"
    assert len(shown) == 4


@pytest.mark.asyncio
async def test_two_accounts_on_the_same_broker_with_different_ids_both_register(client, db_session):
    a = await add(client, "alicebluenew", "ALC-ONE")
    b = await add(client, "alicebluenew", "ALC-TWO")
    assert a["id"] != b["id"]
    assert set(await listed(client)) == {a["id"], b["id"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["alc-dup", "ALC-DUP", " Alc-Dup "])
async def test_the_same_broker_and_id_is_refused_whatever_its_case_and_creates_nothing(client, db_session, variant):
    first = await add(client, "alicebluenew", "ALC-DUP")
    again = await client.post("/api/v1/accounts", json=body("alicebluenew", variant), headers=TOKEN)
    assert again.status_code == 409 and "already exists for this broker" in again.json()["detail"]
    assert set(await listed(client)) == {first["id"]}


@pytest.mark.asyncio
async def test_a_legacy_broker_alias_is_the_same_broker_for_identity(client, db_session):
    first = await add(client, "alicebluenew", "ALC-ALIAS")
    again = await client.post("/api/v1/accounts", json=body("aliceblue", "ALC-ALIAS"), headers=TOKEN)
    assert again.status_code == 409
    assert set(await listed(client)) == {first["id"]}


@pytest.mark.asyncio
async def test_registering_the_same_account_again_after_deletion_restores_it_idempotently(client, db_session):
    first = await add(client, "alicebluenew", "ALC-RESTORE")
    assert (await client.delete(f"/api/v1/accounts/{first['id']}", headers=TOKEN)).status_code == 204
    assert first["id"] not in await listed(client)
    back = await client.post("/api/v1/accounts", json=body("alicebluenew", "ALC-RESTORE"), headers=TOKEN)
    assert back.status_code == 201 and back.json()["id"] == first["id"]
    assert back.json()["session_valid"] is False                         # a restored account is not connected until it signs in


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["dhan", "fyers", "totally-unknown", "5paisa"])
async def test_unknown_or_adapterless_brokers_cannot_be_added(client, db_session, setup_flags, broker):
    resp = await client.post("/api/v1/accounts", json=body(broker, "NOPE-1"), headers=TOKEN)
    assert resp.status_code in (400, 409, 422)
    assert await listed(client) == {}


@pytest.mark.asyncio
async def test_a_broker_that_is_not_enabled_cannot_be_added(client, db_session):
    resp = await client.post("/api/v1/accounts", json=body("zerodha", "Z-OFF"), headers=TOKEN)        # ZERODHA_ENABLED is false
    assert resp.status_code in (400, 409)
    assert await listed(client) == {}


# ── credential vault isolation ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_each_account_keeps_its_own_credentials_and_saving_one_never_touches_another(client, db_session, setup_flags):
    a = await add(client, "groww", "grw-a")
    b = await add(client, "groww", "grw-b")
    assert (await put_creds(client, a["id"], GROWW_A)).status_code == 200
    before_a = (await vault_row(a["id"])).encrypted_payload
    assert (await put_creds(client, b["id"], GROWW_B)).status_code == 200
    assert (await vault_row(a["id"])).encrypted_payload == before_a                 # A's entry is byte-for-byte unchanged
    assert vault.decrypt_payload("groww", (await vault_row(a["id"])).encrypted_payload).api_key == "GRW-KEY-A"
    assert vault.decrypt_payload("groww", (await vault_row(b["id"])).encrypted_payload).api_key == "GRW-KEY-B"
    assert (await vault_row(a["id"])).account_id != (await vault_row(b["id"])).account_id


@pytest.mark.asyncio
async def test_the_same_groww_credentials_cannot_be_registered_on_two_accounts(client, db_session, setup_flags):
    a = await add(client, "groww", "grw-a")
    b = await add(client, "groww", "grw-b")
    assert (await put_creds(client, a["id"], GROWW_A)).status_code == 200
    dup = await put_creds(client, b["id"], GROWW_A)
    assert dup.status_code == 409 and "already belong to another groww account" in dup.json()["detail"]
    assert await vault_row(b["id"]) is None                                          # nothing was stored for B
    assert "GRW-KEY-A" not in dup.text and "GRW-SECRET-A" not in dup.text           # and the response leaks no secret


@pytest.mark.asyncio
async def test_saving_the_same_credentials_again_on_the_same_account_is_idempotent(client, db_session, setup_flags):
    a = await add(client, "groww", "grw-a")
    assert (await put_creds(client, a["id"], GROWW_A)).status_code == 200
    assert (await put_creds(client, a["id"], GROWW_A)).status_code == 200
    async with TestingSessionLocal() as s:
        rows = (await s.execute(select(BrokerCredential).where(BrokerCredential.account_id == uuid.UUID(a["id"])))).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_the_same_angel_one_totp_secret_cannot_be_registered_twice(client, db_session, setup_flags):
    a = await add(client, "angelone", "A1")
    b = await add(client, "angelone", "A2")
    ok = {"api_key": "K1", "pin": "1234", "totp_secret": TOTP}
    assert (await put_creds(client, a["id"], ok)).status_code == 200
    dup = await put_creds(client, b["id"], {"api_key": "K2", "pin": "9999", "totp_secret": TOTP.lower()})
    assert dup.status_code == 409 and TOTP not in dup.text
    assert await vault_row(b["id"]) is None


@pytest.mark.asyncio
async def test_two_kite_users_may_share_one_api_key_because_the_signed_in_user_is_what_identifies_them(client, db_session, setup_flags):
    a = await add(client, "zerodha", "AB1234")
    b = await add(client, "zerodha", "CD5678")
    shared = {"api_key": "KITE-APP-KEY", "api_secret": "KITE-APP-SECRET"}
    assert (await put_creds(client, a["id"], shared)).status_code == 200
    assert (await put_creds(client, b["id"], shared)).status_code == 200


@pytest.mark.asyncio
async def test_credentials_are_rejected_on_an_alice_account_and_for_unknown_fields(client, db_session, setup_flags):
    alice = await add(client, "alicebluenew", "ALC-V")
    assert (await put_creds(client, alice["id"], GROWW_A)).status_code == 409        # Alice Blue does not use the vault
    g = await add(client, "groww", "grw-x")
    assert (await put_creds(client, g["id"], {**GROWW_A, "pin": "1"})).status_code == 400
    assert (await put_creds(client, g["id"], {"api_key": "only-one"})).status_code == 400
    assert await vault_row(g["id"]) is None


# ── the schema change behind it ─────────────────────────────────────────────────────────────────

def test_the_identity_migration_follows_0013_is_the_single_head_and_fits_the_version_column():
    from pathlib import Path

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    backend = Path(__file__).resolve().parent.parent
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == ["0014_account_identity_by_broker"]
    assert script.get_revision("0014_account_identity_by_broker").down_revision == "0013_orders_instrument_token"
    assert all(len(r.revision) <= 32 for r in script.walk_revisions())      # alembic_version.version_num is VARCHAR(32)
    text = (backend / "alembic" / "versions" / "0014_account_identity_by_broker.py").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS broker" in text                         # no earlier migration creates accounts.broker
    assert "accounts has rows sharing" in text and "across brokers" in text  # both directions refuse rather than lose data


def test_the_model_declares_the_broker_scoped_unique_index_and_no_user_id_only_uniqueness():
    from app.models.accounts import Account

    names = {ix.name: ix for ix in Account.__table__.indexes}
    assert names["uq_accounts_broker_external_id"].unique is True
    assert Account.__table__.c.alice_user_id.unique in (None, False)


# ── connection state, as the server reports it ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_connection_state_follows_credentials_and_login_for_each_account_independently(client, db_session, mock_redis, setup_flags, monkeypatch):
    a = await add(client, "groww", "grw-a")
    b = await add(client, "groww", "grw-b")
    shown = await listed(client)
    assert shown[a["id"]]["connection_status"] == "NEEDS_CREDENTIALS" and shown[a["id"]]["credentials_configured"] is False
    await put_creds(client, a["id"], GROWW_A)
    shown = await listed(client)
    assert shown[a["id"]]["connection_status"] == "DISCONNECTED" and shown[a["id"]]["credentials_configured"] is True
    assert shown[b["id"]]["connection_status"] == "NEEDS_CREDENTIALS"                 # B is unaffected by A's setup

    fake = G.FakeGroww()
    monkeypatch.setattr("app.api.accounts.build_groww_client",
                        lambda acc, r, http_client=None: G.make_client(r, fake, account_id=str(acc.id)))
    login = await client.post(f"/api/v1/accounts/{a['id']}/broker-login", headers=TOKEN)
    assert login.status_code == 200 and login.json()["connection_status"] == "CONNECTED"
    shown = await listed(client)
    assert shown[a["id"]]["connection_status"] == "CONNECTED"
    assert shown[b["id"]]["connection_status"] == "NEEDS_CREDENTIALS"
    assert f"groww:session:{a['id']}" in mock_redis._storage and f"groww:session:{b['id']}" not in mock_redis._storage


@pytest.mark.asyncio
async def test_an_expired_session_is_shown_as_disconnected_not_connected(client, db_session, mock_redis, setup_flags):
    a = await add(client, "groww", "grw-a")
    await put_creds(client, a["id"], GROWW_A)
    async with TestingSessionLocal() as s:                                            # the database still says "connected" ...
        row = await s.get(Account, uuid.UUID(a["id"]))
        row.session_valid = True
        row.last_authenticated_at = datetime.now(timezone.utc)
        await s.commit()
    shown = (await listed(client))[a["id"]]                                           # ... but Redis holds no session for it
    assert shown["session_valid"] is False and shown["connection_status"] == "DISCONNECTED"
    assert (await account_row(a["id"])).session_valid is False                        # and the stale flag was corrected


@pytest.mark.asyncio
async def test_no_latency_is_invented_for_a_live_account(client, db_session, mock_redis, setup_flags, monkeypatch):
    a = await add(client, "groww", "grw-a")
    await put_creds(client, a["id"], GROWW_A)
    fake = G.FakeGroww()
    monkeypatch.setattr("app.api.accounts.build_groww_client",
                        lambda acc, r, http_client=None: G.make_client(r, fake, account_id=str(acc.id)))
    assert (await client.post(f"/api/v1/accounts/{a['id']}/broker-login", headers=TOKEN)).status_code == 200
    live = (await listed(client))[a["id"]]
    assert live["session_valid"] is True and live["ping_ms"] is None


@pytest.mark.asyncio
async def test_a_broker_that_is_switched_off_later_is_reported_not_enabled(client, db_session, setup_flags, monkeypatch):
    g = await add(client, "groww", "grw-a")
    monkeypatch.setattr(settings, "GROWW_ENABLED", False)
    shown = (await listed(client))[g["id"]]
    assert shown["connection_status"] == "BROKER_NOT_ENABLED" and "disabled" in shown["connection_detail"].lower()
    assert shown["broker_capabilities"]["login"] is False


@pytest.mark.asyncio
async def test_the_server_reports_each_accounts_trading_capability_and_why_a_role_is_blocked(client, db_session, setup_flags):
    created = {b: await add(client, b, f"cap-{b}") for b in ("alicebluenew", "groww", "angelone", "zerodha")}
    shown = await listed(client)
    alice = shown[created["alicebluenew"]["id"]]["broker_capabilities"]
    assert alice["as_master"] is True and alice["as_child"] is True
    assert alice["master_blocker"] is None and alice["child_blocker"] is None
    for broker in ("groww", "angelone", "zerodha"):
        caps = shown[created[broker]["id"]]["broker_capabilities"]
        assert caps["login"] is True                                                  # setup and sign-in are open ...
        assert caps["as_master"] is False and caps["as_child"] is False               # ... trading is not
        assert caps["master_blocker"] and caps["child_blocker"]                       # and each blocked role says why


@pytest.mark.asyncio
async def test_setting_an_account_up_enables_no_copy_trading_gate(client, db_session, setup_flags):
    from app.services.brokers.pairs import SUPPORTED_PAIRS
    from app.services.brokers.registry import can_be_child, can_be_master
    for broker in ("groww", "angelone", "zerodha"):
        acc = await add(client, broker, f"gate-{broker}")
        await put_creds(client, acc["id"], {"api_key": "k", "api_secret": "s", "pin": "1", "totp_secret": TOTP}
                        if False else ({"api_key": "k1", "pin": "1", "totp_secret": TOTP} if broker == "angelone"
                                       else {"api_key": f"k-{broker}", "api_secret": "s"}))
        assert not can_be_master(broker) and not can_be_child(broker)
    assert list(SUPPORTED_PAIRS) == [("alicebluenew", "alicebluenew")]


# ── login, cancelled login, invalid credentials, relogin: only the account it is for changes ─────

def zerodha_client_for(redis, fake):
    def build(acc, r, http_client=None):
        return ZerodhaClient(
            account_id=str(acc.id), label=acc.alice_user_id, api_key="KITE-KEY", api_secret="KITE-SECRET", redis=r,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake)), clock=lambda: Z.FIXED_NOW,
            instruments=Z.make_store(),
        )
    return build


async def kite_login(client, account_id, token="RTOKEN123"):
    return await client.post(f"/api/v1/accounts/{account_id}/broker-login-complete", json={"request_token": token}, headers=TOKEN)


@pytest.mark.asyncio
async def test_zerodha_login_connects_only_the_signed_in_kite_user_and_not_a_sibling(client, db_session, mock_redis, setup_flags, monkeypatch):
    a = await add(client, "zerodha", "AB1234")
    b = await add(client, "zerodha", "CD5678")
    for acc in (a, b):
        await put_creds(client, acc["id"], {"api_key": "KITE-KEY", "api_secret": "KITE-SECRET"})
    fake = Z.FakeKite()
    fake.session_response = Z.ok({"access_token": "ACCESS-A", "user_id": "AB1234"})
    monkeypatch.setattr("app.api.accounts.build_zerodha_client", zerodha_client_for(mock_redis, fake))

    resp = await kite_login(client, a["id"])
    assert resp.status_code == 200 and resp.json()["connection_status"] == "CONNECTED"
    assert f"zerodha:session:{a['id']}" in mock_redis._storage
    assert f"zerodha:session:{b['id']}" not in mock_redis._storage
    shown = await listed(client)
    assert shown[a["id"]]["session_valid"] is True and shown[b["id"]]["session_valid"] is False
    assert "ACCESS-A" not in resp.text and "RTOKEN123" not in resp.text and "KITE-SECRET" not in resp.text


@pytest.mark.asyncio
async def test_a_kite_login_for_the_wrong_user_is_refused_and_connects_nothing(client, db_session, mock_redis, setup_flags, monkeypatch):
    a = await add(client, "zerodha", "AB1234")
    b = await add(client, "zerodha", "CD5678")
    for acc in (a, b):
        await put_creds(client, acc["id"], {"api_key": "KITE-KEY", "api_secret": "KITE-SECRET"})
    fake = Z.FakeKite()
    fake.session_response = Z.ok({"access_token": "ACCESS-B", "user_id": "CD5678"})        # the browser was signed in as B ...
    monkeypatch.setattr("app.api.accounts.build_zerodha_client", zerodha_client_for(mock_redis, fake))
    resp = await kite_login(client, a["id"])                                                 # ... while connecting A
    assert resp.status_code == 401 and "different user" in resp.json()["detail"]
    assert not any(k.startswith("zerodha:session:") for k in mock_redis._storage)
    shown = await listed(client)
    assert shown[a["id"]]["session_valid"] is False and shown[b["id"]]["session_valid"] is False
    assert "ACCESS-B" not in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"request_token": ""}, {"request_token": 123}, {"request_token": "bad token!"}])
async def test_a_cancelled_or_malformed_kite_login_changes_nothing(client, db_session, mock_redis, setup_flags, monkeypatch, payload):
    a = await add(client, "zerodha", "AB1234")
    await put_creds(client, a["id"], {"api_key": "KITE-KEY", "api_secret": "KITE-SECRET"})
    fake = Z.FakeKite()
    monkeypatch.setattr("app.api.accounts.build_zerodha_client", zerodha_client_for(mock_redis, fake))
    resp = await client.post(f"/api/v1/accounts/{a['id']}/broker-login-complete", json=payload, headers=TOKEN)
    assert resp.status_code in (400, 401)
    assert not fake.requests                                                                  # nothing was sent to Zerodha
    assert (await account_row(a["id"])).session_valid is False
    assert not any(k.startswith("zerodha:session:") for k in mock_redis._storage)


@pytest.mark.asyncio
async def test_kite_rejecting_the_request_token_does_not_connect_and_marks_only_that_account_offline(client, db_session, mock_redis, setup_flags, monkeypatch):
    a = await add(client, "zerodha", "AB1234")
    b = await add(client, "zerodha", "CD5678")
    for acc in (a, b):
        await put_creds(client, acc["id"], {"api_key": "KITE-KEY", "api_secret": "KITE-SECRET"})
    ok_fake = Z.FakeKite()
    ok_fake.session_response = Z.ok({"access_token": "ACCESS-B", "user_id": "CD5678"})
    monkeypatch.setattr("app.api.accounts.build_zerodha_client", zerodha_client_for(mock_redis, ok_fake))
    assert (await kite_login(client, b["id"])).status_code == 200                             # B is connected first

    bad = Z.FakeKite()
    bad.session_response = Z.err(403, "TokenException")
    monkeypatch.setattr("app.api.accounts.build_zerodha_client", zerodha_client_for(mock_redis, bad))
    resp = await kite_login(client, a["id"])
    assert resp.status_code == 401
    shown = await listed(client)
    assert shown[a["id"]]["connection_status"] == "DISCONNECTED"
    assert shown[b["id"]]["connection_status"] == "CONNECTED"                                 # B's live session is untouched
    assert f"zerodha:session:{b['id']}" in mock_redis._storage


@pytest.mark.asyncio
async def test_relogin_of_one_account_never_disturbs_a_sibling_session_or_vault(client, db_session, mock_redis, setup_flags, monkeypatch):
    a = await add(client, "zerodha", "AB1234")
    b = await add(client, "zerodha", "CD5678")
    for acc in (a, b):
        await put_creds(client, acc["id"], {"api_key": "KITE-KEY", "api_secret": "KITE-SECRET"})
    fake = Z.FakeKite()
    fake.session_response = Z.ok({"access_token": "ACCESS-B", "user_id": "CD5678"})
    monkeypatch.setattr("app.api.accounts.build_zerodha_client", zerodha_client_for(mock_redis, fake))
    assert (await kite_login(client, b["id"])).status_code == 200
    b_session = mock_redis._storage[f"zerodha:session:{b['id']}"]
    b_vault = (await vault_row(b["id"])).encrypted_payload

    fake_a = Z.FakeKite()
    fake_a.session_response = Z.ok({"access_token": "ACCESS-A", "user_id": "AB1234"})
    monkeypatch.setattr("app.api.accounts.build_zerodha_client", zerodha_client_for(mock_redis, fake_a))
    assert (await kite_login(client, a["id"])).status_code == 200                            # A signs in (and may sign in again)
    assert mock_redis._storage[f"zerodha:session:{b['id']}"] == b_session                    # B's session is byte-for-byte unchanged
    assert (await vault_row(b["id"])).encrypted_payload == b_vault
    assert (await account_row(b["id"])).last_authenticated_at is not None


@pytest.mark.asyncio
async def test_zerodha_has_no_server_side_login_and_says_how_to_connect(client, db_session, setup_flags):
    a = await add(client, "zerodha", "AB1234")
    await put_creds(client, a["id"], {"api_key": "KITE-KEY", "api_secret": "KITE-SECRET"})
    resp = await client.post(f"/api/v1/accounts/{a['id']}/broker-login", headers=TOKEN)
    assert resp.status_code == 409 and "interactive" in resp.json()["detail"]
    assert (await account_row(a["id"])).session_valid is False


@pytest.mark.asyncio
async def test_a_groww_login_failure_marks_only_that_account_and_leaks_no_secret(client, db_session, mock_redis, setup_flags, monkeypatch):
    a = await add(client, "groww", "grw-a")
    b = await add(client, "groww", "grw-b")
    await put_creds(client, a["id"], GROWW_A)
    await put_creds(client, b["id"], GROWW_B)
    good = G.FakeGroww()
    monkeypatch.setattr("app.api.accounts.build_groww_client",
                        lambda acc, r, http_client=None: G.make_client(r, good, account_id=str(acc.id)))
    assert (await client.post(f"/api/v1/accounts/{b['id']}/broker-login", headers=TOKEN)).status_code == 200
    bad = G.FakeGroww()
    bad.token_response = (401, {"status": "FAILURE"})
    monkeypatch.setattr("app.api.accounts.build_groww_client",
                        lambda acc, r, http_client=None: G.make_client(r, bad, account_id=str(acc.id)))
    resp = await client.post(f"/api/v1/accounts/{a['id']}/broker-login", headers=TOKEN)
    assert resp.status_code == 401
    for secret in ("GRW-KEY-A", "GRW-SECRET-A", "GRW-KEY-B", "GRW-SECRET-B"):
        assert secret not in resp.text
    shown = await listed(client)
    assert shown[a["id"]]["session_valid"] is False and shown[b["id"]]["session_valid"] is True


@pytest.mark.asyncio
async def test_a_login_for_an_account_without_credentials_does_not_connect_it(client, db_session, setup_flags):
    a = await add(client, "groww", "grw-a")
    resp = await client.post(f"/api/v1/accounts/{a['id']}/broker-login", headers=TOKEN)
    assert resp.status_code == 409
    assert (await account_row(a["id"])).session_valid is False


# ── Alice Blue: the OAuth callback and the Alice-only bookkeeping ───────────────────────────────

class _FakeAlice:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def authenticate_with_auth_code(self, code):
        return "alice-session"


@pytest.mark.asyncio
async def test_the_alice_callback_resolves_the_alice_account_even_when_another_broker_shares_the_id(client, db_session, mock_redis, setup_flags, monkeypatch):
    alice = await add(client, "alicebluenew", "SHARED-1")
    groww = await add(client, "groww", "SHARED-1")
    monkeypatch.setattr("app.api.accounts.AliceBlueClient", _FakeAlice)
    resp = await client.post("/api/v1/accounts/callback", json={"auth_code": "code", "returned_user_id": "SHARED-1"}, headers=TOKEN)
    # by id alone the request names no account, so it needs a reconnect intent; with one set for the Alice account it resolves to it
    assert resp.status_code == 400
    assert (await client.post(f"/api/v1/accounts/{alice['id']}/reconnect-intent", headers=TOKEN)).status_code == 204
    resp = await client.post("/api/v1/accounts/callback", json={"auth_code": "code", "returned_user_id": "SHARED-1"}, headers=TOKEN)
    assert resp.status_code == 200
    assert (await account_row(alice["id"])).session_valid is True
    assert (await account_row(groww["id"])).session_valid is False


@pytest.mark.asyncio
async def test_the_alice_callback_never_authenticates_another_brokers_account(client, db_session, setup_flags, monkeypatch):
    groww = await add(client, "groww", "SHARED-2")
    monkeypatch.setattr("app.api.accounts.AliceBlueClient", _FakeAlice)
    resp = await client.post("/api/v1/accounts/callback",
                             json={"auth_code": "code", "account_id": groww["id"], "returned_user_id": "SHARED-2"}, headers=TOKEN)
    assert resp.status_code == 409
    assert (await account_row(groww["id"])).session_valid is False


@pytest.mark.asyncio
async def test_a_target_user_id_that_belongs_only_to_another_broker_matches_no_alice_account(client, db_session, setup_flags, monkeypatch):
    await add(client, "groww", "ONLY-GROWW")
    monkeypatch.setattr("app.api.accounts.AliceBlueClient", _FakeAlice)
    resp = await client.post("/api/v1/accounts/callback", json={"auth_code": "code", "target_user_id": "ONLY-GROWW"}, headers=TOKEN)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reconnect_intent_is_alice_only_and_sign_out_does_not_clear_another_brokers_intent(client, db_session, mock_redis, setup_flags):
    alice = await add(client, "alicebluenew", "SHARED-3")
    groww = await add(client, "groww", "SHARED-3")
    assert (await client.post(f"/api/v1/accounts/{groww['id']}/reconnect-intent", headers=TOKEN)).status_code == 409
    assert (await client.post(f"/api/v1/accounts/{alice['id']}/reconnect-intent", headers=TOKEN)).status_code == 204
    assert (await client.post(f"/api/v1/accounts/{groww['id']}/sign-out", headers=TOKEN)).status_code == 200
    assert "alice:reconnect_intent:SHARED-3" in mock_redis._storage                          # Groww's sign-out left Alice's intent alone
    assert (await client.post(f"/api/v1/accounts/{alice['id']}/sign-out", headers=TOKEN)).status_code == 200
    assert "alice:reconnect_intent:SHARED-3" not in mock_redis._storage


# ── removing an account affects only that account ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_deleting_one_account_clears_only_its_own_session_and_keeps_siblings_connected(client, db_session, mock_redis, setup_flags, monkeypatch):
    a = await add(client, "groww", "grw-a")
    b = await add(client, "groww", "grw-b")
    await put_creds(client, a["id"], GROWW_A)
    await put_creds(client, b["id"], GROWW_B)
    fake = G.FakeGroww()
    monkeypatch.setattr("app.api.accounts.build_groww_client",
                        lambda acc, r, http_client=None: G.make_client(r, fake, account_id=str(acc.id)))
    for acc in (a, b):
        assert (await client.post(f"/api/v1/accounts/{acc['id']}/broker-login", headers=TOKEN)).status_code == 200
    b_vault = (await vault_row(b["id"])).encrypted_payload

    assert (await client.delete(f"/api/v1/accounts/{a['id']}", headers=TOKEN)).status_code == 204
    assert f"groww:session:{a['id']}" not in mock_redis._storage
    assert f"groww:session:{b['id']}" in mock_redis._storage
    shown = await listed(client)
    assert a["id"] not in shown and shown[b["id"]]["connection_status"] == "CONNECTED"
    assert (await vault_row(b["id"])).encrypted_payload == b_vault
    assert (await vault_row(a["id"])) is not None                                             # A's credentials stay so it can be restored


@pytest.mark.asyncio
async def test_deleting_an_unknown_account_changes_nothing(client, db_session, setup_flags):
    a = await add(client, "groww", "grw-a")
    assert (await client.delete(f"/api/v1/accounts/{uuid.uuid4()}", headers=TOKEN)).status_code == 404
    assert set(await listed(client)) == {a["id"]}
