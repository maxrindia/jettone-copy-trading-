"""
Broker Registry
===============
Single source of truth for which broker ids the engine can actually trade
through. The catalog (catalog.py) is UI metadata; this module decides whether
an account's broker may get a client at all, and in which role.

Fail-closed: an unknown or not-yet-supported broker id resolves to a spec
with no client, and every client-construction site must call
`require_engine_broker()` first. Nothing ever falls back to AliceBlueClient
for a non-Alice broker - that would send another broker's credentials to
Alice Blue's endpoints.

Angel One and Groww are gated twice: their flag (`ANGEL_ONE_ENABLED` /
`GROWW_ENABLED`, default false) lets an account log in at all, and `as_child`
additionally needs a verified response contract (angel_one/contract.py,
groww/contract.py). `as_master` is never true for either: Master order
detection is not implemented.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, Optional

from app.core.config import settings
from app.services.angel_one.contract import ANGEL_CONTRACT, contract_is_usable
from app.services.groww.contract import GROWW_CONTRACT, contract_is_usable as groww_contract_is_usable
from app.services.zerodha.contract import ZERODHA_CONTRACT, contract_is_usable as zerodha_contract_is_usable


class BrokerSupport(str, Enum):
    SUPPORTED = "SUPPORTED"            # login, Master detection and Child copying verified
    PARTIAL = "PARTIAL"                # some roles/capabilities verified (see capabilities)
    NOT_SUPPORTED = "NOT_SUPPORTED"    # listed for reference only; no login, no trading


@dataclass(frozen=True)
class BrokerCapabilities:
    login: bool = False
    as_master: bool = False   # order detection (WS/poll) implemented and verified
    as_child: bool = False    # place/modify/cancel/fill sync/positions/funds verified


@dataclass(frozen=True)
class BrokerSpec:
    id: str
    support: BrokerSupport
    capabilities: BrokerCapabilities
    # Value persisted in orders.broker for this broker's orders.
    order_label: Optional[str] = None
    aliases: FrozenSet[str] = field(default_factory=frozenset)
    reason: str = ""
    # Redis key prefix of this broker's cached session token.
    session_prefix: str = ""
    # Credentials live in the encrypted broker_credentials vault (not api_credentials).
    uses_vault: bool = False


class UnsupportedBrokerError(Exception):
    """Raised when an account's broker has no implemented engine client."""

    code = "UNSUPPORTED_BROKER"

    def __init__(self, broker_id: Optional[str], reason: str = ""):
        self.broker_id = broker_id
        self.reason = reason
        super().__init__(
            f"Broker '{broker_id}' is not supported for trading in Jettone"
            + (f": {reason}" if reason else ".")
        )


# Accounts created before multi-broker support store NULL or the Alice ids
# below; all of them keep resolving to Alice Blue exactly as before.
DEFAULT_BROKER_ID = "alicebluenew"

_ALICE = BrokerSpec(
    id="alicebluenew",
    support=BrokerSupport.SUPPORTED,
    capabilities=BrokerCapabilities(login=True, as_master=True, as_child=True),
    order_label="ALICE_BLUE",
    aliases=frozenset({"aliceblue", "alice_blue", "alice", "alice blue", "alice blue (ant / a3)"}),
    session_prefix="alice:session:",
)

_ANGEL_ID = "angelone"
_ANGEL_ALIASES = frozenset({"angel", "angel_one", "angel one", "angelbroking"})
_ANGEL_OFF = (
    "Angel One SmartAPI support is in development and disabled (ANGEL_ONE_ENABLED=false). "
    "Master order detection is not implemented."
)
_ANGEL_SETUP_ONLY = (
    "Credential setup and login only: Angel One cannot be a strategy Child or Master "
    "(ANGEL_ONE_CHILD_TRADING_ENABLED / ANGEL_ONE_MASTER_ENABLED are false)."
)
_ANGEL_NO_CONTRACT = (
    "Angel One login is enabled, but its order status, position and funds responses are not "
    "verified yet, so it cannot be used as a Child. Master order detection is not implemented."
)


def _angel_spec() -> BrokerSpec:
    if not settings.ANGEL_ONE_ENABLED:
        return BrokerSpec(
            id=_ANGEL_ID, support=BrokerSupport.NOT_SUPPORTED, capabilities=BrokerCapabilities(),
            order_label="ANGEL_ONE", aliases=_ANGEL_ALIASES, reason=_ANGEL_OFF,
            session_prefix="angel:session:", uses_vault=True,
        )
    contract_ok = contract_is_usable(ANGEL_CONTRACT)
    child_ready = bool(settings.ANGEL_ONE_CHILD_TRADING_ENABLED) and contract_ok
    master_ready = bool(settings.ANGEL_ONE_MASTER_ENABLED) and contract_ok and ANGEL_CONTRACT.master_verified
    if master_ready:
        reason = "Master" + (" and Child." if child_ready else " only.")
    elif child_ready:
        reason = "Child only. Master order detection is not enabled."
    elif not (settings.ANGEL_ONE_CHILD_TRADING_ENABLED or settings.ANGEL_ONE_MASTER_ENABLED):
        reason = _ANGEL_SETUP_ONLY
    else:
        reason = _ANGEL_NO_CONTRACT
    return BrokerSpec(
        id=_ANGEL_ID, support=BrokerSupport.PARTIAL,
        capabilities=BrokerCapabilities(login=True, as_master=master_ready, as_child=child_ready),
        order_label="ANGEL_ONE", aliases=_ANGEL_ALIASES,
        reason=reason,
        session_prefix="angel:session:", uses_vault=True,
    )


_GROWW_ID = "groww"
_GROWW_ALIASES = frozenset({"groww api", "growwapi"})
_GROWW_OFF = (
    "Groww Trading API support is in development and disabled (GROWW_ENABLED=false). "
    "Groww can only be a Child; Master order detection is not implemented."
)
_GROWW_SETUP_ONLY = (
    "Credential setup and login only: Groww cannot be a strategy Child or receive copied orders "
    "(GROWW_CHILD_TRADING_ENABLED=false). Master order detection is not implemented."
)
_GROWW_NO_CONTRACT = (
    "Groww login is enabled, but its response contract is not verified, so it cannot be used as a Child."
)


def _groww_spec() -> BrokerSpec:
    if not settings.GROWW_ENABLED:
        return BrokerSpec(
            id=_GROWW_ID, support=BrokerSupport.NOT_SUPPORTED, capabilities=BrokerCapabilities(),
            order_label="GROWW", aliases=_GROWW_ALIASES, reason=_GROWW_OFF,
            session_prefix="groww:session:", uses_vault=True,
        )
    # Setup/login need only GROWW_ENABLED. Child (copy) capability additionally needs the explicit
    # GROWW_CHILD_TRADING_ENABLED opt-in AND a usable response contract.
    child_enabled = bool(settings.GROWW_CHILD_TRADING_ENABLED)
    contract_ok = groww_contract_is_usable(GROWW_CONTRACT)
    child_ready = child_enabled and contract_ok
    # Master is a separate gate: its own flag AND the contract's master_verified (False until reviewed).
    master_ready = bool(settings.GROWW_MASTER_ENABLED) and contract_ok and GROWW_CONTRACT.master_verified
    if master_ready:
        reason = "Master" + (" and Child" if child_ready else " only") + " (equity, NSE/BSE)."
    elif child_ready:
        reason = "Child only (equity, NSE/BSE). Master order detection is not enabled."
    elif not child_enabled:
        reason = _GROWW_SETUP_ONLY
    else:
        reason = _GROWW_NO_CONTRACT
    return BrokerSpec(
        id=_GROWW_ID, support=BrokerSupport.PARTIAL,
        capabilities=BrokerCapabilities(login=True, as_master=master_ready, as_child=child_ready),
        order_label="GROWW", aliases=_GROWW_ALIASES,
        reason=reason,
        session_prefix="groww:session:", uses_vault=True,
    )


_ZERODHA_ID = "zerodha"
_ZERODHA_ALIASES = frozenset({"kite", "kite connect", "kiteconnect", "zerodha kite"})
_ZERODHA_OFF = (
    "Zerodha Kite Connect support is built from the documentation only and disabled (ZERODHA_ENABLED=false)."
)
_ZERODHA_SETUP_ONLY = (
    "Credential setup and interactive login only: Zerodha cannot be a strategy Child or Master "
    "(ZERODHA_CHILD_TRADING_ENABLED / ZERODHA_MASTER_ENABLED are false)."
)
_ZERODHA_NO_CONTRACT = "Zerodha login is enabled, but its response contract is not usable, so it cannot be used as a Child."


def _zerodha_spec() -> BrokerSpec:
    if not settings.ZERODHA_ENABLED:
        return BrokerSpec(
            id=_ZERODHA_ID, support=BrokerSupport.NOT_SUPPORTED, capabilities=BrokerCapabilities(),
            order_label="ZERODHA", aliases=_ZERODHA_ALIASES, reason=_ZERODHA_OFF,
            session_prefix="zerodha:session:", uses_vault=True,
        )
    contract_ok = zerodha_contract_is_usable(ZERODHA_CONTRACT)
    child_ready = bool(settings.ZERODHA_CHILD_TRADING_ENABLED) and contract_ok
    master_ready = bool(settings.ZERODHA_MASTER_ENABLED) and contract_ok and ZERODHA_CONTRACT.master_verified
    if master_ready:
        reason = "Master" + (" and Child" if child_ready else " only") + " (equity and F&O, NSE/BSE/NFO)."
    elif child_ready:
        reason = "Child only (NSE/BSE/NFO). Master order detection is not verified."
    elif not settings.ZERODHA_CHILD_TRADING_ENABLED and not settings.ZERODHA_MASTER_ENABLED:
        reason = _ZERODHA_SETUP_ONLY
    else:
        reason = _ZERODHA_NO_CONTRACT if not contract_ok else (
            "Zerodha Master detection is not verified: Kite's documentation does not say whether orders placed outside the API key "
            "appear in the order book."
        )
    return BrokerSpec(
        id=_ZERODHA_ID, support=BrokerSupport.PARTIAL,
        capabilities=BrokerCapabilities(login=True, as_master=master_ready, as_child=child_ready),
        order_label="ZERODHA", aliases=_ZERODHA_ALIASES, reason=reason,
        session_prefix="zerodha:session:", uses_vault=True,
    )


_NOT_YET = (
    "flattrade", "dhan", "fyers", "shoonya", "upstox",
    "kotakneo", "5paisa", "icicidirect", "hdfcsky",
)

_STATIC: Dict[str, BrokerSpec] = {_ALICE.id: _ALICE}
for _bid in _NOT_YET:
    _STATIC[_bid] = BrokerSpec(
        id=_bid,
        support=BrokerSupport.NOT_SUPPORTED,
        capabilities=BrokerCapabilities(),
        reason="No adapter is implemented for this broker yet.",
    )

_ALIASES: Dict[str, str] = {_ANGEL_ID: _ANGEL_ID, _GROWW_ID: _GROWW_ID, _ZERODHA_ID: _ZERODHA_ID}
for _a in _ANGEL_ALIASES:
    _ALIASES[_a] = _ANGEL_ID
for _a in _GROWW_ALIASES:
    _ALIASES[_a] = _GROWW_ID
for _a in _ZERODHA_ALIASES:
    _ALIASES[_a] = _ZERODHA_ID
for _spec in _STATIC.values():
    _ALIASES[_spec.id] = _spec.id
    for _a in _spec.aliases:
        _ALIASES[_a] = _spec.id


def normalize_broker_id(broker_id: Optional[str]) -> Optional[str]:
    """Canonical id for a stored broker value. NULL/empty -> Alice Blue (the
    column's historical default); an unrecognised value -> None."""
    raw = (broker_id or "").strip().lower()
    if not raw:
        return DEFAULT_BROKER_ID
    return _ALIASES.get(raw)


def get_broker_spec(broker_id: Optional[str]) -> Optional[BrokerSpec]:
    """The broker's CURRENT spec (flags and contract state applied)."""
    canonical = normalize_broker_id(broker_id)
    if canonical is None:
        return None
    if canonical == _ANGEL_ID:
        return _angel_spec()
    if canonical == _GROWW_ID:
        return _groww_spec()
    if canonical == _ZERODHA_ID:
        return _zerodha_spec()
    return _STATIC.get(canonical)


def all_broker_specs() -> Dict[str, BrokerSpec]:
    out = dict(_STATIC)
    out[_ANGEL_ID] = _angel_spec()
    out[_GROWW_ID] = _groww_spec()
    out[_ZERODHA_ID] = _zerodha_spec()
    return out


def is_alice_blue(broker_id: Optional[str]) -> bool:
    return normalize_broker_id(broker_id) == _ALICE.id


def require_engine_broker(broker_id: Optional[str]) -> BrokerSpec:
    """The spec for a broker the engine can build a client for, or
    UnsupportedBrokerError."""
    spec = get_broker_spec(broker_id)
    if spec is None:
        raise UnsupportedBrokerError(broker_id, "unknown broker id")
    if not spec.capabilities.login:
        raise UnsupportedBrokerError(broker_id, spec.reason)
    return spec


def require_alice_blue(broker_id: Optional[str]) -> BrokerSpec:
    """Guard for code paths that construct an AliceBlueClient directly."""
    spec = require_engine_broker(broker_id)
    if spec.id != _ALICE.id:
        raise UnsupportedBrokerError(broker_id, "this operation is implemented for Alice Blue only")
    return spec


def can_be_child(broker_id: Optional[str]) -> bool:
    """True only when the broker may receive copied orders. Unknown -> False."""
    spec = get_broker_spec(broker_id)
    return bool(spec and spec.capabilities.login and spec.capabilities.as_child)


def can_be_master(broker_id: Optional[str]) -> bool:
    """True only when the broker's orders can be detected and copied from. Today
    that is Alice Blue alone; Angel One and Groww are Child-only."""
    spec = get_broker_spec(broker_id)
    return bool(spec and spec.capabilities.login and spec.capabilities.as_master)


def order_label_for(broker_id: Optional[str]) -> str:
    """orders.broker value for an account's broker. Fails closed for brokers
    that cannot trade (their orders must never be persisted as placed)."""
    spec = require_engine_broker(broker_id)
    assert spec.order_label
    return spec.order_label


def persisted_order_label(broker_id: Optional[str]) -> str:
    """orders.broker label for persistence. Never raises: legacy/unknown values
    keep the historical 'ALICE_BLUE' label so existing rows and lookups are
    unchanged; only a recognised non-Alice broker gets its own label."""
    spec = get_broker_spec(broker_id)
    return spec.order_label if spec and spec.order_label else _ALICE.order_label


def session_key_for(broker_id: Optional[str], account_id) -> str:
    """Redis key of the account's cached broker session. Unknown/legacy -> Alice."""
    spec = get_broker_spec(broker_id)
    prefix = spec.session_prefix if spec and spec.session_prefix else _ALICE.session_prefix
    return f"{prefix}{account_id}"


def canary_pair_open(master: str, child: str) -> bool:
    """The owner-set canary for the one pair alicebluenew -> groww. Off unless ALICE_GROWW_CANARY_ENABLED; it never opens any other pair."""
    return bool(settings.ALICE_GROWW_CANARY_ENABLED) and (master, child) == ("alicebluenew", "groww")
