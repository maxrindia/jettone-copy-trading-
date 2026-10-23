"""
Two independent Groww gates.

  GROWW_ENABLED               a Groww account may be added, its credentials stored, and an explicit login made.
  GROWW_CHILD_TRADING_ENABLED additionally lets it be a strategy Child (receive copied orders) - and then only
                              according to the existing response-contract and safety checks.

Offline: mocked transports, no broker contact.
"""
import pytest
from sqlalchemy import select

from app.core.config import Settings, settings
from app.models.accounts import ApiCredential, BrokerCredential
from app.models.audit import AuditLog
from app.services.brokers.guards import ensure_broker_selectable
from app.services.brokers.catalog import catalog_with_support
from app.services.brokers.registry import (
    BrokerSupport, UnsupportedBrokerError, can_be_child, can_be_master, get_broker_spec, require_engine_broker,
)
from app.services.groww.contract import GrowwContract
from conftest import TestingSessionLocal
from groww_helpers import FakeGroww, make_client
from test_groww_foundation import SECRETS, TOKEN, _groww_account, client  # noqa: F401
from test_groww_sell_safety import _buy, _groww_child, _run, CHILD_IP  # noqa: F401
from fastapi import HTTPException


def flags(monkeypatch, enabled, child):
    monkeypatch.setattr(settings, "GROWW_ENABLED", enabled)
    monkeypatch.setattr(settings, "GROWW_CHILD_TRADING_ENABLED", child)


def row(name="groww"):
    return {r["id"]: r for r in catalog_with_support()}[name]


# ── defaults ──────────────────────────────────────────────────────────────────────────────────

def test_both_flags_default_to_false_and_are_read_from_the_environment(monkeypatch):
    assert Settings.model_fields["GROWW_ENABLED"].default is False
    assert Settings.model_fields["GROWW_CHILD_TRADING_ENABLED"].default is False
    monkeypatch.setenv("GROWW_CHILD_TRADING_ENABLED", "true")
    assert Settings().GROWW_CHILD_TRADING_ENABLED is True
    monkeypatch.delenv("GROWW_CHILD_TRADING_ENABLED")
    assert Settings().GROWW_CHILD_TRADING_ENABLED is False


# ── 1. both false ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("enabled,child", [(False, False), (False, True)], ids=["both_off", "child_flag_alone"])
def test_without_groww_enabled_groww_is_not_supported_whatever_the_child_flag(monkeypatch, enabled, child):
    flags(monkeypatch, enabled, child)
    spec = get_broker_spec("groww")
    assert spec.support == BrokerSupport.NOT_SUPPORTED
    assert not (spec.capabilities.login or spec.capabilities.as_child or spec.capabilities.as_master)
    assert not can_be_child("groww") and not can_be_master("groww")
    with pytest.raises(UnsupportedBrokerError):
        require_engine_broker("groww")
    with pytest.raises(HTTPException) as ei:
        ensure_broker_selectable("groww")
    assert ei.value.status_code == 400
    r = row()
    assert r["selectable"] is False and r["capabilities"] == {"login": False, "as_master": False, "as_child": False}


# ── 2. GROWW_ENABLED only: setup and login, no Child ──────────────────────────────────────────

def test_enabled_without_the_child_flag_is_selectable_for_setup_but_cannot_be_a_child(monkeypatch):
    flags(monkeypatch, True, False)
    spec = get_broker_spec("groww")
    assert spec.support == BrokerSupport.PARTIAL and spec.uses_vault is True
    assert spec.capabilities.login is True
    assert spec.capabilities.as_child is False and spec.capabilities.as_master is False
    assert not can_be_child("groww") and not can_be_master("groww")
    assert require_engine_broker("groww").id == "groww"                      # credential / login endpoints accept it
    ensure_broker_selectable("groww")                                        # and so does account creation
    r = row()
    assert r["selectable"] is True and r["capabilities"] == {"login": True, "as_master": False, "as_child": False}
    assert "GROWW_CHILD_TRADING_ENABLED" in r["support_note"] and "setup" in r["support_note"].lower()


@pytest.mark.asyncio
async def test_setup_only_groww_can_be_added_configured_and_logged_in(client, db_session, mock_redis, monkeypatch):
    flags(monkeypatch, True, False)
    created = await client.post("/api/v1/accounts", headers=TOKEN, json={
        "account_type": "FOLLOWER", "display_name": "G", "alice_user_id": "GRW-SETUP", "api_secret": "x", "broker": "groww"})
    assert created.status_code == 201
    acc_id = created.json()["id"]
    put = await client.put(f"/api/v1/accounts/{acc_id}/broker-credentials", json=SECRETS, headers=TOKEN)
    assert put.status_code == 200 and put.json()["configured"] is True
    for secret in SECRETS.values():
        assert secret not in put.text
    async with TestingSessionLocal() as s:
        assert (await s.execute(select(ApiCredential))).scalars().all() == []
        assert (await s.execute(select(BrokerCredential))).scalar_one().broker == "groww"
    rows = {r["id"]: r for r in (await client.get("/api/v1/accounts/supported-brokers", headers=TOKEN)).json()["brokers"]}
    assert rows["groww"]["selectable"] is True and rows["groww"]["capabilities"]["as_child"] is False


@pytest.mark.asyncio
async def test_setup_only_login_works_through_the_endpoint(client, db_session, mock_redis, monkeypatch):
    flags(monkeypatch, True, False)
    acc = await _groww_account(db_session)
    fake = FakeGroww()
    monkeypatch.setattr("app.api.accounts.build_groww_client",
                        lambda a, r, http_client=None: make_client(r, fake, account_id=str(a.id)))
    resp = await client.post(f"/api/v1/accounts/{acc.id}/broker-login", headers=TOKEN)
    assert resp.status_code == 200 and resp.json()["session_valid"] is True
    assert [r.url.path for r in fake.requests] == ["/v1/token/api/access"]       # a token request only


@pytest.mark.asyncio
async def test_setup_only_groww_receives_no_copied_orders(db_session, mock_redis, monkeypatch):
    flags(monkeypatch, True, False)
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    out, _ = await _run(mock_redis, master, child, fake, _buy(master, "GATE-1"))
    assert out is None or out.get("status") != "SUCCESS"
    assert fake.calls("/v1/order/create") == []                                  # no order request of any kind
    async with TestingSessionLocal() as s:
        reasons = [a.reason_code for a in (await s.execute(select(AuditLog))).scalars().all()]
    assert "CHILD_BROKER_NOT_READY" in reasons


# ── 3. both flags: Child capability follows the existing contract and safety checks ──────────

def test_both_flags_expose_child_capability_only_for_a_usable_contract(monkeypatch):
    flags(monkeypatch, True, True)
    spec = get_broker_spec("groww")
    assert spec.support == BrokerSupport.PARTIAL and spec.capabilities.login and spec.capabilities.as_child
    assert not spec.capabilities.as_master and can_be_child("groww") and not can_be_master("groww")
    assert "Child only" in row()["support_note"]
    monkeypatch.setattr("app.services.brokers.registry.GROWW_CONTRACT", GrowwContract(docs_verified=False))
    spec = get_broker_spec("groww")
    assert spec.capabilities.login and not spec.capabilities.as_child and not can_be_child("groww")
    assert "contract is not verified" in spec.reason


@pytest.mark.asyncio
async def test_both_flags_still_apply_the_normal_child_safety_checks(db_session, mock_redis, monkeypatch, cross_broker_pairs_enabled):
    flags(monkeypatch, True, True)
    monkeypatch.setattr(settings, "SELL_POSITION_SAFETY_BYPASS", False)
    master, child = await _groww_child(db_session)
    fake = FakeGroww()
    out, _ = await _run(mock_redis, master, child, fake, _buy(master, "GATE-2"))
    assert out["status"] == "SUCCESS" and len(fake.calls("/v1/order/create")) == 1


# ── the other brokers are unaffected ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("child", [False, True])
def test_the_groww_child_flag_does_not_change_alice_or_angel(monkeypatch, child):
    monkeypatch.setattr(settings, "GROWW_CHILD_TRADING_ENABLED", child)
    monkeypatch.setattr(settings, "GROWW_ENABLED", True)
    monkeypatch.setattr(settings, "ANGEL_ONE_ENABLED", False)
    assert row("angelone")["selectable"] is False
    alice = row("alicebluenew")
    assert alice["support"] == "SUPPORTED" and alice["capabilities"] == {"login": True, "as_master": True, "as_child": True}
    assert can_be_master("alicebluenew") and can_be_child("alicebluenew")


@pytest.mark.parametrize("child", [False, True])
def test_angel_one_never_becomes_a_child_while_its_contract_is_unverified(monkeypatch, child):
    from app.services.angel_one.contract import ANGEL_CONTRACT
    assert ANGEL_CONTRACT.verified is False
    monkeypatch.setattr(settings, "ANGEL_ONE_ENABLED", True)
    monkeypatch.setattr(settings, "ANGEL_ONE_CHILD_TRADING_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_CHILD_TRADING_ENABLED", child)           # irrelevant to Angel One
    spec = get_broker_spec("angelone")
    assert spec.capabilities.login is True and spec.capabilities.as_child is False and not can_be_child("angelone")
    assert row("angelone")["capabilities"]["as_child"] is False
