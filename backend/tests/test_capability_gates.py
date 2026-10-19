"""
Credential setup, login, Child eligibility and Master eligibility are four separate gates.

  setup + login   <broker>_ENABLED
  Child           + <broker>_CHILD_TRADING_ENABLED   + a usable (verified) response contract
  Master          + <broker>_MASTER_ENABLED          + the contract's master_verified (set in code only after a
                                                       reviewed capture of paging, stable order ids, part-fill status)

Nothing here contacts a broker.
"""
import dataclasses

import pytest

from app.core.config import Settings, settings
from app.services.angel_one.contract import AngelContract
from app.services.brokers.registry import BrokerSupport, can_be_child, can_be_master, get_broker_spec
from app.services.groww.contract import GrowwContract
from angel_helpers import SYNTHETIC_CONTRACT


def set_flags(monkeypatch, **flags):
    for name, value in flags.items():
        monkeypatch.setattr(settings, name, value)


def use_angel_contract(monkeypatch, contract):
    monkeypatch.setattr("app.services.brokers.registry.ANGEL_CONTRACT", contract)


def use_groww_contract(monkeypatch, contract):
    monkeypatch.setattr("app.services.brokers.registry.GROWW_CONTRACT", contract)


def test_every_new_gate_defaults_to_off():
    for name in ("GROWW_MASTER_ENABLED", "ANGEL_ONE_CHILD_TRADING_ENABLED", "ANGEL_ONE_MASTER_ENABLED"):
        assert Settings.model_fields[name].default is False
    assert GrowwContract().master_verified is False and AngelContract().master_verified is False


# ── Groww ───────────────────────────────────────────────────────────────────────────────────

def test_groww_master_stays_closed_with_every_flag_on_until_the_contract_is_verified(monkeypatch):
    set_flags(monkeypatch, GROWW_ENABLED=True, GROWW_CHILD_TRADING_ENABLED=True, GROWW_MASTER_ENABLED=True)
    assert get_broker_spec("groww").capabilities.as_master is False and not can_be_master("groww")
    assert can_be_child("groww")                                                  # Child is independent of Master


@pytest.mark.parametrize("master_flag,verified,expected", [(False, False, False), (True, False, False), (False, True, False), (True, True, True)])
def test_groww_master_needs_both_its_flag_and_a_verified_contract(monkeypatch, master_flag, verified, expected):
    set_flags(monkeypatch, GROWW_ENABLED=True, GROWW_CHILD_TRADING_ENABLED=False, GROWW_MASTER_ENABLED=master_flag)
    use_groww_contract(monkeypatch, GrowwContract(master_verified=verified))
    assert can_be_master("groww") is expected
    assert not can_be_child("groww")                                              # Master never implies Child


def test_groww_master_flag_without_the_main_flag_does_nothing(monkeypatch):
    set_flags(monkeypatch, GROWW_ENABLED=False, GROWW_CHILD_TRADING_ENABLED=True, GROWW_MASTER_ENABLED=True)
    use_groww_contract(monkeypatch, GrowwContract(master_verified=True))
    spec = get_broker_spec("groww")
    assert spec.support == BrokerSupport.NOT_SUPPORTED and not spec.capabilities.login and not can_be_master("groww")


def test_groww_master_needs_a_usable_contract_even_with_flag_and_verification(monkeypatch):
    set_flags(monkeypatch, GROWW_ENABLED=True, GROWW_MASTER_ENABLED=True)
    use_groww_contract(monkeypatch, GrowwContract(docs_verified=False, master_verified=True))
    assert not can_be_master("groww")


# ── Angel One ───────────────────────────────────────────────────────────────────────────────

def test_angel_enabled_alone_is_setup_and_login_only(monkeypatch):
    set_flags(monkeypatch, ANGEL_ONE_ENABLED=True, ANGEL_ONE_CHILD_TRADING_ENABLED=False, ANGEL_ONE_MASTER_ENABLED=False)
    use_angel_contract(monkeypatch, SYNTHETIC_CONTRACT)                              # even a verified contract
    spec = get_broker_spec("angelone")
    assert spec.support == BrokerSupport.PARTIAL and spec.capabilities.login
    assert not spec.capabilities.as_child and not spec.capabilities.as_master
    assert "setup and login only" in spec.reason.lower()


def test_angel_stays_closed_with_every_flag_on_while_its_contract_is_unverified(monkeypatch):
    set_flags(monkeypatch, ANGEL_ONE_ENABLED=True, ANGEL_ONE_CHILD_TRADING_ENABLED=True, ANGEL_ONE_MASTER_ENABLED=True)
    spec = get_broker_spec("angelone")                                                # the real, empty, production contract
    assert spec.capabilities.login and not spec.capabilities.as_child and not spec.capabilities.as_master
    assert "not verified" in spec.reason


@pytest.mark.parametrize("child_flag,master_flag,master_verified,exp_child,exp_master", [
    (True, False, False, True, False),
    (False, True, True, False, True),
    (True, True, False, True, False),          # flag on, contract not master-verified
    (True, True, True, True, True),
    (False, False, True, False, False),
])
def test_angel_child_and_master_are_independent_gates(monkeypatch, child_flag, master_flag, master_verified, exp_child, exp_master):
    set_flags(monkeypatch, ANGEL_ONE_ENABLED=True, ANGEL_ONE_CHILD_TRADING_ENABLED=child_flag, ANGEL_ONE_MASTER_ENABLED=master_flag)
    use_angel_contract(monkeypatch, dataclasses.replace(SYNTHETIC_CONTRACT, master_verified=master_verified))
    assert can_be_child("angelone") is exp_child
    assert can_be_master("angelone") is exp_master


def test_alice_blue_is_unaffected_by_any_gate(monkeypatch):
    set_flags(monkeypatch, GROWW_ENABLED=False, ANGEL_ONE_ENABLED=False)
    assert can_be_master("alicebluenew") and can_be_child("alicebluenew")
    set_flags(monkeypatch, GROWW_ENABLED=True, GROWW_MASTER_ENABLED=True, ANGEL_ONE_ENABLED=True, ANGEL_ONE_MASTER_ENABLED=True)
    assert can_be_master("alicebluenew") and can_be_child("alicebluenew")
