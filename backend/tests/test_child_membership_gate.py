"""
Backend-authoritative Child eligibility: POST /strategies/{id}/members refuses (409) an account whose broker the
engine's CURRENT capability catalog does not allow as a Child (`as_child`), and fails closed when the broker or its
capability is unknown or missing. The UI's disabled "Add" button only mirrors this.

Existing membership rules (one strategy per account, Master never its own Child, 404s) and the Master eligibility
check are unchanged. Runs on the in-memory test database through the ASGI app: nothing here touches production data
or a broker.
"""
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.accounts import Account, AccountType, CopySetting
from app.models.strategies import StrategyAccountRole, StrategyMember
from app.services import strategy_membership as membership
from app.services.brokers.registry import BrokerCapabilities, BrokerSpec, BrokerSupport
from conftest import TestingSessionLocal
from test_strategy_master_switching import AUTH, _account, _create_with_master, api_client  # noqa: F401
from angel_helpers import SYNTHETIC_CONTRACT


async def set_broker(account, broker):
    async with TestingSessionLocal() as s:
        row = await s.get(Account, account.id)
        row.broker = broker
        await s.commit()


async def strategy_with_master(api_client, db_session, name="Gate"):
    master = await _account(db_session, f"m_{name.lower()}", AccountType.MASTER)
    sid, resp = await _create_with_master(api_client, name, master.id)
    assert resp.status_code == 200, resp.text
    return sid, master


async def add(api_client, sid, account, **extra):
    return await api_client.post(f"/api/v1/strategies/{sid}/members", json={"account_id": str(account.id), **extra}, headers=AUTH)


async def snapshot():
    """Everything a rejected add must leave exactly as it was."""
    async with TestingSessionLocal() as s:
        members = sorted((str(m.strategy_id), str(m.account_id), str(m.multiplier), m.trading_enabled)
                         for m in (await s.execute(select(StrategyMember))).scalars().all())
        roles = sorted((str(r.account_id), r.role, str(r.strategy_id)) for r in (await s.execute(select(StrategyAccountRole))).scalars().all())
        settings_rows = sorted((str(c.follower_account_id), c.copy_enabled, str(c.quantity_mode), c.fixed_qty)
                               for c in (await s.execute(select(CopySetting))).scalars().all())
        accounts = sorted((str(a.id), a.account_type.value, a.broker) for a in (await s.execute(select(Account))).scalars().all())
    return members, roles, settings_rows, accounts


# ── 1. Alice Blue is unchanged ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["alicebluenew", "", None, "Alice Blue", "aliceblue"])
async def test_an_alice_blue_child_is_accepted(api_client, db_session, broker):
    sid, _ = await strategy_with_master(api_client, db_session)
    child = await _account(db_session, "alice_child")
    await set_broker(child, broker)
    resp = await add(api_client, sid, child, multiplier="2.0")
    assert resp.status_code == 201, resp.text
    async with TestingSessionLocal() as s:
        assert (await s.execute(select(StrategyMember).where(StrategyMember.account_id == child.id))).scalar_one().multiplier == 2


# ── 2. Groww ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True], ids=["groww_off", "groww_setup_only"])
async def test_a_groww_child_is_rejected_while_the_child_gate_is_off(api_client, db_session, monkeypatch, enabled):
    monkeypatch.setattr(settings, "GROWW_ENABLED", enabled)
    monkeypatch.setattr(settings, "GROWW_CHILD_TRADING_ENABLED", False)
    sid, _ = await strategy_with_master(api_client, db_session)
    child = await _account(db_session, "groww_child")
    await set_broker(child, "groww")
    before = await snapshot()
    resp = await add(api_client, sid, child)
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "groww" in detail.lower() and "cannot be added as a strategy Child" in detail
    assert "GROWW_CHILD_TRADING_ENABLED" in detail or "disabled" in detail.lower()    # the engine's own reason, no secrets
    assert await snapshot() == before                                                # nothing was created or changed


@pytest.mark.asyncio
async def test_a_groww_child_is_accepted_only_when_the_catalog_allows_it(api_client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "GROWW_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_CHILD_TRADING_ENABLED", True)
    sid, _ = await strategy_with_master(api_client, db_session)
    child = await _account(db_session, "groww_ok")
    await set_broker(child, "groww")
    assert (await add(api_client, sid, child)).status_code == 201                    # the gate follows the catalog, not a hard-coded name


# ── 3. Angel One ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,child_flag", [(False, False), (True, False), (False, True)],
                         ids=["angel_off", "angel_setup_only", "child_flag_without_main_flag"])
async def test_an_angel_one_child_is_rejected_while_its_child_gate_is_off(api_client, db_session, monkeypatch, enabled, child_flag):
    monkeypatch.setattr(settings, "ANGEL_ONE_ENABLED", enabled)
    monkeypatch.setattr(settings, "ANGEL_ONE_CHILD_TRADING_ENABLED", child_flag)
    sid, _ = await strategy_with_master(api_client, db_session)
    child = await _account(db_session, "angel_child")
    await set_broker(child, "angelone")
    before = await snapshot()
    resp = await add(api_client, sid, child)
    assert resp.status_code == 409 and "angelone" in resp.json()["detail"].lower()
    assert await snapshot() == before


@pytest.mark.asyncio
async def test_angel_one_stays_rejected_with_every_flag_on_while_its_contract_is_unverified(api_client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "ANGEL_ONE_ENABLED", True)
    monkeypatch.setattr(settings, "ANGEL_ONE_CHILD_TRADING_ENABLED", True)
    monkeypatch.setattr(settings, "ANGEL_ONE_MASTER_ENABLED", True)
    sid, _ = await strategy_with_master(api_client, db_session)
    child = await _account(db_session, "angel_unverified")
    await set_broker(child, "angelone")
    resp = await add(api_client, sid, child)
    assert resp.status_code == 409 and "not verified" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_angel_one_is_accepted_when_flag_and_verified_contract_both_allow_it(api_client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "ANGEL_ONE_ENABLED", True)
    monkeypatch.setattr(settings, "ANGEL_ONE_CHILD_TRADING_ENABLED", True)
    monkeypatch.setattr("app.services.brokers.registry.ANGEL_CONTRACT", SYNTHETIC_CONTRACT)
    sid, _ = await strategy_with_master(api_client, db_session)
    child = await _account(db_session, "angel_ok")
    await set_broker(child, "angelone")
    assert (await add(api_client, sid, child)).status_code == 201


# ── 4. unknown / missing capability fails closed ──────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["robinhood", "made-up-broker", "zerodha", "dhan"])
async def test_an_unknown_or_unsupported_broker_is_rejected(api_client, db_session, broker):
    sid, _ = await strategy_with_master(api_client, db_session)
    child = await _account(db_session, "mystery")
    await set_broker(child, broker)
    before = await snapshot()
    resp = await add(api_client, sid, child)
    assert resp.status_code == 409
    assert await snapshot() == before


@pytest.mark.parametrize("spec", [
    None,
    SimpleNamespace(id="x", reason="r"),                                                           # no capabilities attribute at all
    SimpleNamespace(id="x", reason="r", capabilities=None),
    SimpleNamespace(id="x", reason="r", capabilities=SimpleNamespace()),                          # capabilities without as_child
    SimpleNamespace(id="x", reason="r", capabilities=SimpleNamespace(as_child="yes")),            # truthy but not literally True
    BrokerSpec(id="x", support=BrokerSupport.PARTIAL, capabilities=BrokerCapabilities(login=True, as_child=False),
               order_label="X", aliases=frozenset(), reason=""),
], ids=["no_spec", "no_attr", "none", "no_as_child", "truthy_string", "as_child_false"])
def test_a_missing_or_malformed_capability_fails_closed(monkeypatch, spec):
    monkeypatch.setattr(membership, "get_broker_spec", lambda broker: spec)
    with pytest.raises(membership.MembershipConflict) as ei:
        membership.assert_broker_can_be_child("x")
    assert ei.value.code == "CHILD_BROKER_UNSUPPORTED"


def test_only_a_literal_true_capability_passes(monkeypatch):
    ok = BrokerSpec(id="x", support=BrokerSupport.PARTIAL, capabilities=BrokerCapabilities(login=True, as_child=True),
                    order_label="X", aliases=frozenset(), reason="")
    monkeypatch.setattr(membership, "get_broker_spec", lambda broker: ok)
    membership.assert_broker_can_be_child("x")                                                    # does not raise


def test_the_error_names_the_broker_and_carries_nothing_secret():
    with pytest.raises(membership.MembershipConflict) as ei:
        membership.assert_broker_can_be_child("groww")
    msg = ei.value.message
    assert "groww" in msg and len(msg) < 400
    for forbidden in ("token", "secret", "password", "key="):
        assert forbidden not in msg.lower()


# ── 5. existing rules unchanged ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_account_still_cannot_join_two_strategies(api_client, db_session):
    sid1, _ = await strategy_with_master(api_client, db_session, "One")
    sid2, _ = await strategy_with_master(api_client, db_session, "Two")
    child = await _account(db_session, "shared")
    assert (await add(api_client, sid1, child)).status_code == 201
    resp = await add(api_client, sid2, child)
    assert resp.status_code == 409 and "One" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_a_master_still_cannot_be_its_own_child(api_client, db_session):
    sid, master = await strategy_with_master(api_client, db_session)
    assert (await add(api_client, sid, master)).status_code == 409


@pytest.mark.asyncio
async def test_missing_strategy_and_missing_account_are_still_404(api_client, db_session):
    import uuid
    sid, _ = await strategy_with_master(api_client, db_session)
    ghost = SimpleNamespace(id=uuid.uuid4())
    child = await _account(db_session, "real")
    assert (await add(api_client, str(uuid.uuid4()), child)).status_code == 404
    assert (await add(api_client, sid, ghost)).status_code == 404


@pytest.mark.asyncio
async def test_the_master_eligibility_check_is_unchanged(api_client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "GROWW_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_CHILD_TRADING_ENABLED", True)         # Child-capable, still never a Master
    groww = await _account(db_session, "groww_master", AccountType.FOLLOWER)
    await set_broker(groww, "groww")
    sid, resp = await _create_with_master(api_client, "GrowwMaster", groww.id)
    assert resp.status_code == 409 and "can only be a Child account" in resp.text


@pytest.mark.asyncio
async def test_a_rejected_add_never_reaches_the_availability_check(api_client, db_session, monkeypatch):
    """The broker is checked first, so an unsupported broker is reported as such rather than as 'already assigned'."""
    sid, _ = await strategy_with_master(api_client, db_session)
    child = await _account(db_session, "order_of_checks")
    await set_broker(child, "groww")

    async def must_not_run(*a, **k):
        raise AssertionError("availability was checked for a broker that cannot be a Child")
    monkeypatch.setattr(membership, "assert_account_available", must_not_run)
    resp = await add(api_client, sid, child)
    assert resp.status_code == 409 and "cannot be added as a strategy Child" in resp.json()["detail"]
