"""
Cross-broker DERIVATIVE routing: one policy, asked by every place that used to refuse a cross-broker F&O order outright.
========================================================================================================================
Until now `planner.cross_broker_derivative_skip` refused every derivative order copied to a Child on a DIFFERENT broker, because no broker's
derivative symbol or token is an identity at another broker. That refusal stays the DEFAULT. This module is the single place that can lift it,
for a pair the owner listed (CROSS_BROKER_FNO_PAIRS) and only when the order's contract resolves EXACTLY in the Child's OWN catalog:

    Master adapter     builds CanonicalContract.key (underlying + expiry + strike + CE/PE/FUT) from ITS instrument master   -> event.contract_key
    this policy        pair listed?  contract readable?  Child has a resolver?  exactly one row in the Child's catalog?
    Child              receives ITS OWN token from that catalog row - the Master broker's token never crosses brokers

Open/closed: a Child broker becomes routable by registering a resolver in `CHILD_RESOLVERS` (a Strategy per broker); the pipeline, the
planner and the preview do not change. Today only Alice Blue has one, because only its derivative resolution reads a verified-complete
catalog (instruments/contracts.py). Zerodha, Groww and Angel One Children are refused with the same reason code as before.

Everything fails closed: an unlisted pair, an unreadable key, a Child without a resolver, an unloaded / stale / ambiguous catalog or a token
that is not numeric keeps the original `INSTRUMENT_UNMAPPED` skip (reason `CROSS_BROKER_DERIVATIVE_UNMAPPED:...` or `CROSS_BROKER_FNO_*`).
This policy never opens a role gate or a pair gate and never decides Live Trading; those still apply to the order afterwards.
"""
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from app.services.brokers.registry import listed_fno_pairs, normalize_broker_id
from app.services.instruments.contracts import (
    CanonicalContract, ContractResolution, ResolutionVerdict, contract_from_key, resolve_for_broker,
)
from app.services.instruments.planner import (
    ALICE_ID, EXCHANGE_SEGMENTS, INSTRUMENT_UNMAPPED, PlannerSkip, cross_broker_derivative_skip,
)

Resolver = Callable[[CanonicalContract], ContractResolution]


def _resolve_alice(contract: CanonicalContract) -> ContractResolution:
    return resolve_for_broker(ALICE_ID, contract)


# Child broker -> how a canonical contract is resolved in that broker's own catalog. One entry = one more routable Child.
CHILD_RESOLVERS: Dict[str, Resolver] = {ALICE_ID: _resolve_alice}


@dataclass(frozen=True)
class RouteDecision:
    allowed: bool
    skip: Optional[PlannerSkip] = None
    child_token: Optional[str] = None            # the Child's OWN token (meaningful only at the Child's broker)
    child_symbol: Optional[str] = None


def is_cash(exchange: Any) -> bool:
    return EXCHANGE_SEGMENTS.get(str(exchange or "").strip().upper()) == ("CASH",)


def _refuse(detail: str) -> RouteDecision:
    return RouteDecision(False, PlannerSkip(INSTRUMENT_UNMAPPED, detail[:200]))


def route(master_broker: Optional[str], child_broker: Optional[str], exchange: Any, contract_key: Optional[str]) -> RouteDecision:
    """Whether a derivative order of `master_broker` may be copied to a Child of `child_broker`, and with which Child token. Cash equity
    and same-broker orders are not routed here (the callers never ask); asked anyway, they are allowed with no token."""
    try:
        master = normalize_broker_id(master_broker) if master_broker else None
        child = normalize_broker_id(child_broker)
        if master is not None and master == child or is_cash(exchange):
            return RouteDecision(True)
        if master is None or child is None or (master, child) not in listed_fno_pairs():
            original = cross_broker_derivative_skip(master_broker=master, child_broker=child, exchange=exchange)
            return RouteDecision(original is None, original)                            # the original answer, byte for byte
        contract = contract_from_key(contract_key)
        if contract is None:
            return _refuse(f"CROSS_BROKER_FNO_CONTRACT_UNAVAILABLE:{master}->{child}")
        resolver = CHILD_RESOLVERS.get(child)
        if resolver is None:
            return _refuse(f"CROSS_BROKER_FNO_CHILD_NOT_SUPPORTED:{child}")
        resolution = resolver(contract)
        token = str(resolution.broker_token or "")
        if resolution.verdict is not ResolutionVerdict.RESOLVED_UNVERIFIED or not token.isdigit():
            return _refuse(f"CROSS_BROKER_FNO_{resolution.verdict.value}:{resolution.reason}")
        return RouteDecision(True, None, token, resolution.broker_symbol)
    except Exception as exc:                                                              # an error is never a route
        return RouteDecision(False, PlannerSkip(INSTRUMENT_UNMAPPED, f"CROSS_BROKER_FNO_ROUTE_ERROR:{type(exc).__name__}"))


def skip_for(master_broker: Optional[str], child_broker: Optional[str], exchange: Any, contract_key: Optional[str]) -> Optional[PlannerSkip]:
    """The planner/preview question: None when the order may be copied to this Child, else why not. Same-broker and cash orders: None."""
    decision = route(master_broker, child_broker, exchange, contract_key)
    return None if decision.allowed else decision.skip
