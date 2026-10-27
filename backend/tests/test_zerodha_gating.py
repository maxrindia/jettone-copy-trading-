"""Zerodha is wired in but gated off by default, rated by its documentation-only evidence, and never a supported pair."""
import pytest

from app.core.config import settings
from app.services.brokers import registry
from app.services.brokers.capabilities import Feature, Verification
from app.services.brokers.catalog import SUPPORTED_BROKERS
from app.services.brokers.credential_vault import CredentialError, VAULT_FIELDS, decrypt_payload, encrypt_payload, validate_payload
from app.services.brokers.pairs import SUPPORTED_PAIRS, check_pair
from app.services.brokers.profiles import build_profile
from app.services.zerodha.contract import ZerodhaContract


def test_default_flags_are_off_and_zerodha_cannot_trade():
    assert settings.ZERODHA_ENABLED is False and settings.ZERODHA_CHILD_TRADING_ENABLED is False and settings.ZERODHA_MASTER_ENABLED is False
    spec = registry.get_broker_spec("zerodha")
    assert spec.support == registry.BrokerSupport.NOT_SUPPORTED
    assert not registry.can_be_child("zerodha") and not registry.can_be_master("zerodha")
    with pytest.raises(registry.UnsupportedBrokerError):
        registry.require_engine_broker("zerodha")


@pytest.mark.parametrize("alias", ["Kite", "kite connect", "KiteConnect", "zerodha kite"])
def test_aliases_resolve(alias):
    assert registry.normalize_broker_id(alias) == "zerodha"


def test_enabled_alone_allows_setup_only(monkeypatch):
    monkeypatch.setattr(settings, "ZERODHA_ENABLED", True)
    spec = registry.get_broker_spec("zerodha")
    assert spec.capabilities.login and not spec.capabilities.as_child and not spec.capabilities.as_master
    assert spec.uses_vault and registry.session_key_for("zerodha", "A1") == "zerodha:session:A1"
    assert registry.persisted_order_label("zerodha") == "ZERODHA"


def test_child_gate_opens_with_its_flag_but_master_never_without_a_verified_contract(monkeypatch):
    monkeypatch.setattr(settings, "ZERODHA_ENABLED", True)
    monkeypatch.setattr(settings, "ZERODHA_CHILD_TRADING_ENABLED", True)
    monkeypatch.setattr(settings, "ZERODHA_MASTER_ENABLED", True)
    assert registry.can_be_child("zerodha")
    assert not registry.can_be_master("zerodha")                         # master_verified is False in code
    assert "not verified" in registry.get_broker_spec("zerodha").reason or "Master" in registry.get_broker_spec("zerodha").reason


def test_master_opens_only_when_the_contract_says_so(monkeypatch):
    monkeypatch.setattr(settings, "ZERODHA_ENABLED", True)
    monkeypatch.setattr(settings, "ZERODHA_MASTER_ENABLED", True)
    monkeypatch.setattr(registry, "ZERODHA_CONTRACT", ZerodhaContract(master_verified=True))
    assert registry.can_be_master("zerodha")


def test_profile_is_capped_at_docs_and_master_detection_is_unverified(monkeypatch):
    monkeypatch.setattr(registry, "ZERODHA_CONTRACT", ZerodhaContract(live_verified=True, master_verified=True))
    p = build_profile("zerodha")
    assert p.features[Feature.CHILD_PLACEMENT] == Verification.VERIFIED_DOCS            # live flag cannot raise the ceiling
    assert p.features[Feature.MASTER_DETECTION] == Verification.UNVERIFIED
    assert p.features[Feature.HOLDINGS] == Verification.NOT_SUPPORTED
    assert p.features[Feature.ORDER_LOOKUP] == Verification.VERIFIED_DOCS
    derivs = {(s.exchange, s.segment): s.verification for s in p.segments}
    assert derivs[("NFO", "OPT")] == Verification.UNVERIFIED and derivs[("NSE", "CASH")] == Verification.VERIFIED_DOCS


def test_no_zerodha_pair_is_supported():
    assert all("zerodha" not in pair for pair in SUPPORTED_PAIRS)
    for other in ("alicebluenew", "groww", "angelone", "zerodha"):
        assert not check_pair("zerodha", other).supported
        assert not check_pair(other, "zerodha").supported


def test_vault_roundtrip_hides_values():
    assert VAULT_FIELDS["zerodha"] == ("api_key", "api_secret")
    creds = validate_payload("zerodha", {"api_key": "k", "api_secret": "s"})
    assert "k" not in repr(creds) and "s" not in repr(creds).replace("ZerodhaCredentials", "")
    assert decrypt_payload("zerodha", encrypt_payload("zerodha", creds)) == creds
    with pytest.raises(CredentialError):
        validate_payload("zerodha", {"api_key": "k"})


def test_catalog_marks_zerodha_not_supported_while_disabled():
    entry = next(b for b in SUPPORTED_BROKERS if b["id"] == "zerodha")
    assert entry["id"] == "zerodha"


def test_a_groww_vault_entry_is_never_built_as_zerodha_credentials():
    from types import SimpleNamespace
    from app.services.brokers.zerodha_builder import build_zerodha_client
    blob = encrypt_payload("groww", validate_payload("groww", {"api_key": "k", "api_secret": "s"}))
    acc = SimpleNamespace(id="a1", alice_user_id="AB1234", notes=None,
                          broker_credential=SimpleNamespace(broker="groww", encrypted_payload=blob))
    with pytest.raises(CredentialError):
        build_zerodha_client(acc, None)
    acc.broker_credential = SimpleNamespace(broker="zerodha", encrypted_payload=blob)
    assert build_zerodha_client(acc, None).account_id == "a1"
