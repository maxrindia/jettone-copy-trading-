"""
Identity assurance and connection-state accuracy for account setup. Offline only: fake broker transports, an in-memory database and
a fake Redis; nothing here contacts a broker or changes any trading gate.

* A login that succeeds proves the credentials worked, not WHICH broker account they belong to. Each broker reports how far Jettone can
  confirm that, and Groww (whose token response carries no identity) says so wherever the account is shown. The statement is
  informational: it never adds or removes a Master / Child role.
* When the session store cannot be reached, the connection state is UNKNOWN. It is never shown as connected, and it is never written
  to the database as a sign-out.
"""
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.services.brokers.account_status import broker_capabilities, connection_status, identity_assurance
from test_groww_foundation import TOKEN, client  # noqa: F401  (client is a fixture)
from test_multi_account_setup import (  # noqa: F401
    GROWW_A, G, Z, account_row, add, kite_login, listed, put_creds, setup_flags, zerodha_client_for,
)


# ── identity assurance ───────────────────────────────────────────────────────────────────────────

def test_each_brokers_identity_assurance_is_reported_and_only_zerodha_is_confirmed():
    assert identity_assurance("zerodha")["level"] == "CONFIRMED"
    assert identity_assurance("alicebluenew")["level"] == "CREDENTIAL_BOUND"
    assert identity_assurance("angelone")["level"] == "CREDENTIAL_BOUND"
    assert identity_assurance("groww")["level"] == "NOT_AVAILABLE"
    for unknown in ("dhan", "totally-unknown", "", None):
        assert identity_assurance(unknown)["level"] in ("NOT_AVAILABLE", "CREDENTIAL_BOUND")   # None / "" resolve to Alice Blue
    assert identity_assurance("dhan")["level"] == "NOT_AVAILABLE"                              # never assumed confirmed
    assert broker_capabilities("groww")["identity"]["level"] == "NOT_AVAILABLE"
    assert broker_capabilities("not-a-broker")["identity"]["level"] == "NOT_AVAILABLE"


def test_nothing_but_zerodha_claims_the_identity_was_confirmed_and_the_notes_make_no_provider_claims_beyond_jettones_own_use():
    for broker in ("alicebluenew", "angelone", "groww"):
        assert identity_assurance(broker)["level"] != "CONFIRMED"
    assert "does not use the login response" in identity_assurance("alicebluenew")["note"]
    assert "does not use the login response" in identity_assurance("angelone")["note"]


@pytest.mark.asyncio
async def test_a_connected_groww_account_says_its_identity_could_not_be_confirmed(client, db_session, mock_redis, setup_flags, monkeypatch):
    g = await add(client, "groww", "label-says-this-is-me")
    await put_creds(client, g["id"], GROWW_A)
    fake = G.FakeGroww()
    monkeypatch.setattr("app.api.accounts.build_groww_client",
                        lambda acc, r, http_client=None: G.make_client(r, fake, account_id=str(acc.id)))
    login = await client.post(f"/api/v1/accounts/{g['id']}/broker-login", headers=TOKEN)
    assert login.status_code == 200
    shown = (await listed(client))[g["id"]]
    assert shown["connection_status"] == "CONNECTED"                                       # authentication did succeed ...
    detail = shown["connection_detail"].lower()
    assert "cannot confirm" in detail and "belongs to the account named here" in detail    # ... and the limitation is stated
    assert shown["broker_capabilities"]["identity"]["level"] == "NOT_AVAILABLE"
    assert login.json()["connection_detail"] == shown["connection_detail"]                 # the login response says the same
    for secret in ("GRW-KEY-A", "GRW-SECRET-A"):
        assert secret not in login.text and secret not in str(shown)


@pytest.mark.asyncio
async def test_a_signed_out_groww_account_is_not_described_as_signed_in(client, db_session, setup_flags):
    g = await add(client, "groww", "grw-out")
    await put_creds(client, g["id"], GROWW_A)
    shown = (await listed(client))[g["id"]]
    assert shown["connection_status"] == "DISCONNECTED" and "Signed in" not in shown["connection_detail"]


@pytest.mark.asyncio
async def test_a_connected_zerodha_account_is_not_given_the_groww_limitation(client, db_session, mock_redis, setup_flags, monkeypatch):
    z = await add(client, "zerodha", "AB1234")
    await put_creds(client, z["id"], {"api_key": "KITE-KEY", "api_secret": "KITE-SECRET"})
    fake = Z.FakeKite()
    fake.session_response = Z.ok({"access_token": "ACCESS-Z", "user_id": "AB1234"})
    monkeypatch.setattr("app.api.accounts.build_zerodha_client", zerodha_client_for(mock_redis, fake))
    assert (await kite_login(client, z["id"])).status_code == 200
    shown = (await listed(client))[z["id"]]
    assert shown["connection_status"] == "CONNECTED" and shown["connection_detail"] == "Signed in."
    assert shown["broker_capabilities"]["identity"]["level"] == "CONFIRMED"


@pytest.mark.asyncio
@pytest.mark.parametrize("child_flag,master_flag", [(False, False), (True, False), (True, True)])
async def test_the_identity_limitation_never_changes_what_roles_groww_may_have(client, db_session, setup_flags, monkeypatch, child_flag, master_flag):
    from app.services.brokers.pairs import SUPPORTED_PAIRS
    from app.services.brokers.registry import can_be_child, can_be_master
    monkeypatch.setattr(settings, "GROWW_CHILD_TRADING_ENABLED", child_flag)
    monkeypatch.setattr(settings, "GROWW_MASTER_ENABLED", master_flag)
    g = await add(client, "groww", "grw-roles")
    caps = (await listed(client))[g["id"]]["broker_capabilities"]
    assert caps["as_child"] is can_be_child("groww") and caps["as_master"] is can_be_master("groww")   # exactly the registry's answer
    assert caps["as_master"] is False                                                                 # Groww is never a Master here
    assert caps["identity"]["level"] == "NOT_AVAILABLE"
    assert list(SUPPORTED_PAIRS) == [("alicebluenew", "alicebluenew")]                                # and no pair is enabled


# ── connection-state accuracy when the session store is unreachable ─────────────────────────────

def test_an_unreachable_session_store_is_unknown_never_connected():
    status, detail = connection_status("alicebluenew", True, True, False)
    assert status == "STATUS_UNKNOWN" and "session store" in detail
    assert connection_status("groww", True, True, False)[0] != "CONNECTED"
    assert connection_status("totally-unknown", True, True, False)[0] == "UNSUPPORTED_BROKER"      # the more specific state wins
    assert connection_status("alicebluenew", True, True, None)[0] == "CONNECTED"                   # "not looked up" is not "unknown"
    assert connection_status("alicebluenew", True, True, True)[0] == "CONNECTED"


@pytest.mark.asyncio
async def test_when_redis_fails_the_list_still_loads_reports_unknown_and_does_not_sign_the_account_out(client, db_session, mock_redis, setup_flags, monkeypatch):
    g = await add(client, "groww", "grw-redis")
    await put_creds(client, g["id"], GROWW_A)
    fake = G.FakeGroww()
    monkeypatch.setattr("app.api.accounts.build_groww_client",
                        lambda acc, r, http_client=None: G.make_client(r, fake, account_id=str(acc.id)))
    assert (await client.post(f"/api/v1/accounts/{g['id']}/broker-login", headers=TOKEN)).status_code == 200
    stamp = (await account_row(g["id"])).last_authenticated_at
    assert stamp is not None

    working_get = mock_redis.get
    mock_redis.get = AsyncMock(side_effect=ConnectionError("redis down"))
    try:
        resp = await client.get("/api/v1/accounts", headers=TOKEN)
    finally:
        mock_redis.get = working_get
    assert resp.status_code == 200                                                        # the page still loads
    shown = {a["id"]: a for a in resp.json()}[g["id"]]
    assert shown["connection_status"] == "STATUS_UNKNOWN" and shown["session_valid"] is False
    assert "session store" in shown["connection_detail"] and shown["session_store_available"] is False
    assert "redis down" not in resp.text                                                  # no internal error text is exposed

    row = await account_row(g["id"])                                                      # nothing was written: still signed in
    assert row.session_valid is True and row.last_authenticated_at == stamp

    back = (await listed(client))[g["id"]]                                                # Redis recovered: connected again, no re-login
    assert back["connection_status"] == "CONNECTED" and back["session_store_available"] is None


@pytest.mark.asyncio
async def test_a_redis_failure_does_not_affect_accounts_that_were_never_signed_in(client, db_session, mock_redis, setup_flags):
    g = await add(client, "groww", "grw-never")
    await put_creds(client, g["id"], GROWW_A)
    working_get = mock_redis.get
    mock_redis.get = AsyncMock(side_effect=ConnectionError("redis down"))
    try:
        resp = await client.get("/api/v1/accounts", headers=TOKEN)
    finally:
        mock_redis.get = working_get
    assert resp.status_code == 200
    assert {a["id"]: a for a in resp.json()}[g["id"]]["connection_status"] == "DISCONNECTED"   # no session to check, so no doubt
