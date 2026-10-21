"""
R1 capability profiles (docs/architecture/multi-broker-copy-trading, 05 §5.2).

A profile is descriptive: evidence level per feature plus the registry's current gates. These tests pin that a
profile can never claim more than the repository's evidence, that no contract boolean can inflate it, that gates
mirror the registry exactly and stay independent of evidence, and that nothing besides Alice Blue meets the
design bar for either role today. No broker is contacted.
"""
import dataclasses

import pytest

from app.core.config import settings
from app.services.brokers.capabilities import (
    ROLE_REQUIREMENTS,
    CapabilityProfile,
    Feature,
    Role,
    SegmentCaps,
    Verification,
    normalize_order_type,
)
from app.services.brokers.profiles import (
    GROWW_FEATURE_CEILING,
    GROWW_SEGMENT_CEILING,
    all_profiles,
    build_profile,
)
from app.services.brokers.registry import all_broker_specs
from app.services.groww.contract import GrowwContract
from angel_helpers import SYNTHETIC_CONTRACT

V = Verification
F = Feature
ALL_PRODUCTS = frozenset({"MIS", "CNC", "NRML"})
ALL_TYPES = frozenset({"MARKET", "LIMIT", "SL", "SLM"})


def groww_on(monkeypatch, *, child=False, master=False, contract=None):
    monkeypatch.setattr(settings, "GROWW_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_CHILD_TRADING_ENABLED", child)
    monkeypatch.setattr(settings, "GROWW_MASTER_ENABLED", master)
    if contract is not None:
        monkeypatch.setattr("app.services.brokers.registry.GROWW_CONTRACT", contract)


def all_live(gates):
    """A synthetic profile with EVERY feature live and the given gates: the baseline for negative tests."""
    return CapabilityProfile(broker_id="x", features={f: V.VERIFIED_LIVE for f in F}, gates=gates)


OPEN = {"login": True, "as_master": True, "as_child": True}


# ── vocabulary and fail-closed defaults ────────────────────────────────────────────────────

def test_only_verified_live_counts_as_live_and_ranks_are_ordered():
    assert [v for v in V if v.is_live] == [V.VERIFIED_LIVE]
    assert V.NOT_SUPPORTED.rank < V.UNVERIFIED.rank < V.VERIFIED_DOCS.rank < V.VERIFIED_LIVE.rank
    assert V.lowest(V.VERIFIED_LIVE, V.VERIFIED_DOCS) is V.VERIFIED_DOCS
    assert V.VERIFIED_DOCS.at_least(V.UNVERIFIED) and not V.UNVERIFIED.at_least(V.VERIFIED_DOCS)


def test_unlisted_feature_and_segment_are_not_supported():
    profile = CapabilityProfile(broker_id="x")
    assert all(profile.verification(f) is V.NOT_SUPPORTED for f in F)
    assert profile.segment("NSE", "CASH") is None
    assert not profile.supports("NSE", "CASH", "MIS", "LIMIT", minimum=V.UNVERIFIED)
    assert not profile.meets_design_bar(Role.CHILD) and not profile.meets_design_bar(Role.MASTER)


def test_a_profile_is_immutable_and_hashable():
    p = build_profile("alicebluenew")
    with pytest.raises(TypeError):
        p.features[F.HOLDINGS] = V.VERIFIED_LIVE                      # mapping is a read-only view
    with pytest.raises(TypeError):
        p.gates["as_child"] = False
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.broker_id = "other"
    assert hash(p) == hash(build_profile("alicebluenew")) and p == build_profile("alicebluenew")
    assert len({p, build_profile("alicebluenew")}) == 1


def test_a_profile_does_not_alias_the_dict_it_was_built_from():
    source = {F.LOGIN: V.VERIFIED_LIVE}
    profile = CapabilityProfile(broker_id="x", features=source)
    source[F.LOGIN] = V.NOT_SUPPORTED
    assert profile.verification(F.LOGIN) is V.VERIFIED_LIVE


# ── the design bar: the FULL requirement set, gates separate from evidence ────────────────

def test_the_requirement_sets_are_exactly_the_documented_ones():
    assert ROLE_REQUIREMENTS[Role.CHILD] == (F.CHILD_PLACEMENT, F.MODIFY, F.CANCEL, F.ORDER_STATUS, F.POSITIONS, F.FUNDS)
    assert ROLE_REQUIREMENTS[Role.MASTER] == (F.MASTER_DETECTION, F.ORDER_STATUS)


@pytest.mark.parametrize("role", list(Role))
def test_complete_live_evidence_with_an_open_gate_meets_the_bar(role):
    p = all_live(OPEN)
    assert p.meets_design_bar(role) and p.missing_for_design_bar(role) == []


@pytest.mark.parametrize("role", list(Role))
def test_a_closed_gate_blocks_the_bar_whatever_the_evidence(role):
    p = all_live({"login": True, "as_master": False, "as_child": False})
    assert p.missing_for_design_bar(role) == [] and not p.meets_design_bar(role)


@pytest.mark.parametrize("role,feature", [(r, f) for r in Role for f in ROLE_REQUIREMENTS[r]],
                         ids=lambda v: v.value)
@pytest.mark.parametrize("level", [V.VERIFIED_DOCS, V.UNVERIFIED, V.NOT_SUPPORTED], ids=lambda v: v.value)
def test_one_required_feature_that_is_not_live_blocks_the_role(role, feature, level):
    features = {f: V.VERIFIED_LIVE for f in F}
    features[feature] = level
    p = CapabilityProfile(broker_id="x", features=features, gates=OPEN)
    assert p.missing_for_design_bar(role) == [feature]
    assert not p.meets_design_bar(role)


@pytest.mark.parametrize("role", list(Role))
def test_a_required_feature_that_is_absent_blocks_the_role(role):
    for feature in ROLE_REQUIREMENTS[role]:
        features = {f: V.VERIFIED_LIVE for f in F if f is not feature}
        p = CapabilityProfile(broker_id="x", features=features, gates=OPEN)
        assert not p.meets_design_bar(role), feature


def test_features_a_role_does_not_need_do_not_block_it():
    features = {f: V.VERIFIED_LIVE for f in F}
    features[F.HOLDINGS] = V.NOT_SUPPORTED          # no role requires holdings
    features[F.CLIENT_REFERENCE] = V.NOT_SUPPORTED
    features[F.MASTER_DETECTION] = V.UNVERIFIED     # Master-only
    p = CapabilityProfile(broker_id="x", features=features, gates=OPEN)
    assert p.meets_design_bar(Role.CHILD) and not p.meets_design_bar(Role.MASTER)


# ── Alice Blue ─────────────────────────────────────────────────────────────────────────────

def test_alice_profile_matches_the_evidence_in_the_repository():
    p = build_profile("alicebluenew")
    for feature in (F.LOGIN, F.MASTER_DETECTION, F.CHILD_PLACEMENT, F.MODIFY, F.CANCEL, F.ORDER_STATUS,
                    F.POSITIONS, F.FUNDS):
        assert p.verification(feature) is V.VERIFIED_LIVE, feature
    assert p.verification(F.HOLDINGS) is V.NOT_SUPPORTED
    assert p.verification(F.ORDER_LOOKUP) is V.NOT_SUPPORTED                    # order book read, not a lookup
    assert p.verification(F.CLIENT_REFERENCE) is V.NOT_SUPPORTED                # no order tag is sent
    assert p.segment("NSE", "CASH").verification is V.VERIFIED_LIVE
    assert p.segment("NFO", "OPT").verification is V.VERIFIED_LIVE              # confirmed live 2026-10-14
    assert p.segment("NFO", "FUT").verification is V.UNVERIFIED                 # never exercised
    assert p.segment("BSE", "CASH").verification is V.UNVERIFIED
    assert p.segment("MCX", "COM") is None
    assert p.meets_design_bar(Role.MASTER) and p.meets_design_bar(Role.CHILD)


@pytest.mark.parametrize("stored", [None, "", "alicebluenew", "aliceblue", "Alice Blue"])
def test_legacy_and_alias_values_resolve_to_the_alice_profile(stored):
    assert build_profile(stored).broker_id == "alicebluenew"


def test_unknown_broker_has_no_profile():
    assert build_profile("not-a-broker") is None


# ── supports(): strict by default, canonical values only ──────────────────────────────────

def test_supports_is_strict_about_product_order_type_and_unsupported_segments():
    caps = SegmentCaps("NSE", "CASH", frozenset({"MIS"}), frozenset({"LIMIT"}), V.VERIFIED_LIVE)
    dead = SegmentCaps("NFO", "OPT", frozenset({"MIS"}), frozenset({"LIMIT"}), V.NOT_SUPPORTED)
    profile = CapabilityProfile(broker_id="x", segments=(caps, dead))
    assert profile.supports("nse", " cash ", "mis", "limit")                   # case and spaces normalised
    assert not profile.supports("NSE", "CASH", "CNC", "LIMIT")                  # product not listed
    assert not profile.supports("NSE", "CASH", "MIS", "SLM")                    # order type not listed
    assert not profile.supports("NFO", "OPT", "MIS", "LIMIT", minimum=V.UNVERIFIED)   # NOT_SUPPORTED never
    assert not profile.supports("BSE", "CASH", "MIS", "LIMIT")                  # unlisted
    assert not profile.supports("NSE", "CASH", "", None)


def test_unverified_and_docs_level_segments_need_the_caller_to_say_so():
    alice = build_profile("alicebluenew")
    assert alice.supports("NFO", "OPT", "NRML", "LIMIT")                        # live
    assert not alice.supports("NFO", "FUT", "NRML", "LIMIT")                    # UNVERIFIED -> refused by default
    assert alice.supports("NFO", "FUT", "NRML", "LIMIT", minimum=V.UNVERIFIED)  # only when explicitly accepted
    assert not alice.supports("NFO", "FUT", "NRML", "LIMIT", minimum=V.VERIFIED_DOCS)


def test_docs_level_groww_segment_is_refused_by_default_and_allowed_at_docs_level(monkeypatch):
    groww_on(monkeypatch)
    p = build_profile("groww")
    assert not p.supports("NSE", "CASH", "MIS", "LIMIT")
    assert p.supports("NSE", "CASH", "MIS", "LIMIT", minimum=V.VERIFIED_DOCS)
    assert not p.supports("NFO", "OPT", "NRML", "LIMIT", minimum=V.UNVERIFIED)  # F&O is not listed at all


@pytest.mark.parametrize("raw,canonical", [
    ("MARKET", "MARKET"), ("mkt", "MARKET"), ("LIMIT", "LIMIT"), ("L", "LIMIT"), ("SL", "SL"),
    ("SL-M", "SLM"), ("SLM", "SLM"), ("sl_m", "SLM"), (" sl-m ", "SLM"), ("BRACKET", None), (None, None), ("", None),
])
def test_order_type_normalisation(raw, canonical):
    assert normalize_order_type(raw) == canonical


def test_the_real_order_type_enum_normalises():
    from app.services.alice_blue.types import OrderType
    assert normalize_order_type(OrderType.SL_M) == "SLM" and OrderType.SL_M.value == "SL-M"
    assert [normalize_order_type(t) for t in (OrderType.MARKET, OrderType.LIMIT, OrderType.SL)] == ["MARKET", "LIMIT", "SL"]


def test_real_world_values_need_supports_order_and_fail_the_canonical_check():
    """`supports` takes canonical values; LONGTERM / SL-M are what actually arrive."""
    alice = build_profile("alicebluenew")
    assert not alice.supports("NSE", "CASH", "LONGTERM", "LIMIT")                # not canonical -> refused
    assert not alice.supports("NSE", "CASH", "MIS", "SL-M")
    assert alice.supports_order("NSE", "CASH", "LONGTERM", "LIMIT")              # LONGTERM -> CNC
    assert alice.supports_order("NSE", "CASH", "INTRADAY", "SL-M")               # INTRADAY -> MIS, SL-M -> SLM


def test_supports_order_accepts_the_real_enums():
    from app.services.alice_blue.types import OrderType, ProductType
    alice = build_profile("alicebluenew")
    assert alice.supports_order("NSE", "CASH", ProductType.CNC, OrderType.SL_M)
    assert alice.supports_order("NSE", "CASH", ProductType.MIS, OrderType.MARKET)


def test_supports_order_folds_carry_products_on_derivative_exchanges_only():
    alice = build_profile("alicebluenew")
    assert alice.supports_order("NFO", "OPT", "LONGTERM", "LIMIT")               # CNC folds into NRML on NFO
    narrow = CapabilityProfile(broker_id="x", segments=(
        SegmentCaps("NFO", "OPT", frozenset({"NRML"}), ALL_TYPES, V.VERIFIED_LIVE),
        SegmentCaps("NSE", "CASH", frozenset({"NRML"}), ALL_TYPES, V.VERIFIED_LIVE),
    ))
    assert narrow.supports_order("NFO", "OPT", "CNC", "LIMIT")                   # folded
    assert not narrow.supports_order("NSE", "CASH", "CNC", "LIMIT")              # equity stays strict


@pytest.mark.parametrize("product,order_type", [("BRACKET", "LIMIT"), ("MIS", "ICEBERG"), (None, "LIMIT"), ("MIS", None)])
def test_supports_order_fails_closed_on_unknown_values(product, order_type):
    assert not build_profile("alicebluenew").supports_order("NSE", "CASH", product, order_type, minimum=V.UNVERIFIED)


def test_the_unverified_segment_check_survives_normalisation():
    alice = build_profile("alicebluenew")
    assert not alice.supports_order("NFO", "FUT", "LONGTERM", "SL-M")
    assert alice.supports_order("NFO", "FUT", "LONGTERM", "SL-M", minimum=V.UNVERIFIED)


# ── Groww: evidence is capped per feature, independent of the gates ───────────────────────

def test_groww_disabled_keeps_its_evidence_but_every_gate_is_closed(monkeypatch):
    monkeypatch.setattr(settings, "GROWW_ENABLED", False)
    p = build_profile("groww")
    assert p.gates == {"login": False, "as_master": False, "as_child": False}
    assert p.verification(F.CHILD_PLACEMENT) is V.VERIFIED_DOCS
    assert not p.meets_design_bar(Role.CHILD)


def test_groww_child_on_documentation_alone_opens_the_gate_but_does_not_meet_the_design_bar(monkeypatch):
    """Conflict between code and design, pinned: the registry opens the Child gate for a docs-verified contract,
    whereas the architecture requires VERIFIED_LIVE. R1 reports it; it does not change the registry."""
    groww_on(monkeypatch, child=True)
    p = build_profile("groww")
    assert p.gate_open(Role.CHILD) is True
    assert p.role_verification(Role.CHILD) is V.VERIFIED_DOCS
    assert p.meets_design_bar(Role.CHILD) is False
    assert set(p.missing_for_design_bar(Role.CHILD)) == set(ROLE_REQUIREMENTS[Role.CHILD])


def test_groww_login_is_the_only_live_verified_capability(monkeypatch):
    groww_on(monkeypatch)
    p = build_profile("groww")
    assert {f for f in F if p.verification(f) is V.VERIFIED_LIVE} == {F.LOGIN}   # token call observed 2026-10-19
    assert p.verification(F.HOLDINGS) is V.NOT_SUPPORTED and p.verification(F.ORDER_LOOKUP) is V.NOT_SUPPORTED
    assert p.segment("NSE", "CASH").verification is V.VERIFIED_DOCS
    assert p.segment("NFO", "OPT") is None


@pytest.mark.parametrize("master_verified", [False, True])
@pytest.mark.parametrize("flags", [dict(child=False), dict(child=True, master=True)], ids=["flags_off", "flags_on"])
def test_no_groww_contract_boolean_can_inflate_the_evidence(monkeypatch, master_verified, flags):
    """`live_verified` says the RESPONSE shapes were seen on a real account. It must not turn placement, modify,
    cancel, order book, positions, funds, the client reference, any segment, or Master detection into
    VERIFIED_LIVE."""
    groww_on(monkeypatch, **flags, contract=GrowwContract(live_verified=True, docs_verified=True, master_verified=master_verified))
    p = build_profile("groww")
    live = {f for f in F if p.verification(f) is V.VERIFIED_LIVE}
    assert live == {F.LOGIN}
    for feature, ceiling in GROWW_FEATURE_CEILING.items():
        assert p.verification(feature).rank <= ceiling.rank, feature
    for (exchange, segment), ceiling in GROWW_SEGMENT_CEILING.items():
        assert p.segment(exchange, segment).verification.rank <= ceiling.rank
    assert p.verification(F.MASTER_DETECTION) is V.UNVERIFIED
    assert not p.meets_design_bar(Role.CHILD) and not p.meets_design_bar(Role.MASTER)


def test_the_ceiling_table_is_pinned():
    """Raising a ceiling is a deliberate edit that must come with that feature's live evidence."""
    assert {f: v for f, v in GROWW_FEATURE_CEILING.items() if v is V.VERIFIED_LIVE} == {F.LOGIN: V.VERIFIED_LIVE}
    assert GROWW_FEATURE_CEILING[F.MASTER_DETECTION] is V.UNVERIFIED
    assert {GROWW_FEATURE_CEILING[f] for f in (F.CHILD_PLACEMENT, F.MODIFY, F.CANCEL)} == {V.VERIFIED_DOCS}
    assert set(GROWW_SEGMENT_CEILING.values()) == {V.VERIFIED_DOCS}


def test_an_unusable_groww_contract_lowers_the_evidence_below_its_ceiling(monkeypatch):
    groww_on(monkeypatch, child=True, contract=GrowwContract(docs_verified=False))
    p = build_profile("groww")
    assert p.role_verification(Role.CHILD) is V.UNVERIFIED and p.gate_open(Role.CHILD) is False
    assert p.verification(F.LOGIN) is V.VERIFIED_LIVE                             # independent of the contract


# ── Angel One ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("contract_verified", [False, True])
def test_angel_one_is_never_rated_above_unverified(monkeypatch, contract_verified):
    for name in ("ANGEL_ONE_ENABLED", "ANGEL_ONE_CHILD_TRADING_ENABLED", "ANGEL_ONE_MASTER_ENABLED"):
        monkeypatch.setattr(settings, name, True)
    if contract_verified:
        monkeypatch.setattr("app.services.brokers.registry.ANGEL_CONTRACT", SYNTHETIC_CONTRACT)
    p = build_profile("angelone")
    assert max(p.verification(f).rank for f in F) <= V.UNVERIFIED.rank
    assert all(c.verification is V.UNVERIFIED for c in p.segments)
    assert not p.meets_design_bar(Role.CHILD) and not p.meets_design_bar(Role.MASTER)
    assert p.verification(F.LOGIN) is V.UNVERIFIED


# ── brokers without an adapter ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("broker", ["zerodha", "5paisa", "dhan", "fyers"])
def test_brokers_without_an_adapter_support_nothing(broker):
    p = build_profile(broker)
    assert p is not None and p.segments == ()
    assert all(p.verification(f) is V.NOT_SUPPORTED for f in F)
    assert p.gates == {"login": False, "as_master": False, "as_child": False}
    assert not p.supports("NSE", "CASH", "MIS", "LIMIT", minimum=V.UNVERIFIED)


# ── invariants across the whole registry ───────────────────────────────────────────────────

def test_gates_mirror_the_registry_for_every_broker(monkeypatch):
    groww_on(monkeypatch, child=True)
    for broker_id, spec in all_broker_specs().items():
        p = build_profile(broker_id)
        c = spec.capabilities
        assert p.gates == {"login": c.login, "as_master": c.as_master, "as_child": c.as_child}, broker_id
        assert p.gate_open(Role.MASTER) is c.as_master and p.gate_open(Role.CHILD) is c.as_child


def test_flipping_flags_changes_gates_and_never_the_evidence(monkeypatch):
    """Gates and evidence are independent inputs."""
    def snapshot():
        return {b: (dict(p.features), p.segments) for b, p in all_profiles().items()}

    monkeypatch.setattr(settings, "GROWW_ENABLED", False)
    for name in ("ANGEL_ONE_ENABLED", "ANGEL_ONE_CHILD_TRADING_ENABLED", "ANGEL_ONE_MASTER_ENABLED"):
        monkeypatch.setattr(settings, name, False)
    before = snapshot()
    groww_on(monkeypatch, child=True, master=True)
    for name in ("ANGEL_ONE_ENABLED", "ANGEL_ONE_CHILD_TRADING_ENABLED", "ANGEL_ONE_MASTER_ENABLED"):
        monkeypatch.setattr(settings, name, True)
    assert snapshot() == before


def test_only_alice_blue_meets_the_design_bar_today(monkeypatch):
    groww_on(monkeypatch, child=True, master=True)
    for name in ("ANGEL_ONE_ENABLED", "ANGEL_ONE_CHILD_TRADING_ENABLED", "ANGEL_ONE_MASTER_ENABLED"):
        monkeypatch.setattr(settings, name, True)
    meets = {b: [r.value for r in Role if p.meets_design_bar(r)] for b, p in all_profiles().items()}
    assert meets["alicebluenew"] == ["MASTER", "CHILD"]
    assert all(roles == [] for b, roles in meets.items() if b != "alicebluenew"), meets
