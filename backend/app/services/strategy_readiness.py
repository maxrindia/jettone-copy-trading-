"""
Strategy readiness diagnostics (R6): which Master -> Child broker pairs the order-time gate (R5) will accept, and how much of the
Master's recent instrument activity has any mapping evidence (R4i).

Read-only and additive. It reuses `brokers.pairs.check_pair` - there is no second broker-support policy here - and the R4i
crosswalk evidence. It makes no broker, Redis or network call, writes nothing, changes no role and submits nothing. The output
carries broker ids, display names, reason codes and counts only: no credential, session value or account id is added.

What "ready" means here
-----------------------
`broker_pairs_ready` says only that every trading-enabled Child's broker pair is on the support allowlist. It is NOT a claim that
orders will be placed: the engine may be observe-only, a session may have expired, risk may reject. `observe_only` reports the
live-trading switch so the two are never confused. A Child whose pair is unsupported is shown as such even though it is a member
of the strategy: membership is not execution support.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.trades import Order, OrderSource
from app.services.brokers.pairs import BROKER_PAIR_NOT_SUPPORTED, check_pair
from app.services.brokers.registry import normalize_broker_id
from app.services.instruments import crosswalk_evidence

MAX_HOURS = 168
DEFAULT_HOURS = 24
MAX_ORDERS = 500

# What an operator should understand from the first part of a PairDecision.detail. Never a secret; broker ids come from the
# registry's canonical names or are truncated by check_pair.
_REQUIREMENT_TEXT = {
    "MASTER_BROKER_UNKNOWN": "The Master's broker could not be determined, so no pair can be checked.",
    "CHILD_BROKER_UNKNOWN": "The Child's broker is not recognised.",
    "PAIR_NOT_ALLOWLISTED": "This Master -> Child broker combination is not on the supported list.",
    "MASTER_ROLE_NOT_OPEN": "The Master broker's Master role is not open.",
    "CHILD_ROLE_NOT_OPEN": "The Child broker's Child role is not open.",
    "EVIDENCE_BELOW_LIVE": "The pair lacks live-verified evidence for a required capability.",
    "NO_PROFILE": "No capability profile exists for a broker of this pair.",
    "PAIR_CHECK_ERROR": "The pair check failed, so the pair is treated as unsupported.",
}


def _canonical(broker: Optional[str]) -> Optional[str]:
    return normalize_broker_id(broker) if broker is not None else None


def pair_readiness(master_broker: Optional[str], child_broker: Optional[str]) -> Dict[str, Any]:
    """The R5 decision for one pair, in a response-friendly shape. `supported` is True/False; `None` is never returned for a
    pair that was checked (an unknown Master is unsupported, as at order time)."""
    decision = check_pair(master_broker, child_broker)
    requirement = None if decision.supported else decision.detail.split(":", 1)[0]
    return {
        "master_broker": _canonical(master_broker),
        "child_broker": normalize_broker_id(child_broker),       # a NULL stored broker is Alice Blue, as everywhere else
        "supported": decision.supported,
        "code": decision.code,
        "requirement": requirement,
        "detail": decision.detail,
        "message": (
            "Orders for this Child are copied: the broker pair is supported."
            if decision.supported else
            f"{BROKER_PAIR_NOT_SUPPORTED}: orders will NOT be copied to this Child. "
            + _REQUIREMENT_TEXT.get(requirement or "", "The broker pair is not supported.")
        ),
    }


def no_master_pair(child_broker: Optional[str]) -> Dict[str, Any]:
    """A strategy without a Master has no pair to check: reported as unknown, never as supported."""
    return {
        "master_broker": None, "child_broker": normalize_broker_id(child_broker), "supported": None, "code": None,
        "requirement": "NO_MASTER", "detail": "NO_MASTER",
        "message": "The strategy has no Master yet, so the broker pair cannot be checked.",
    }


def _warning(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    return {"code": code, "message": message, **extra}


MAPPING_WARNINGS = {
    "CROSSWALK_UNAVAILABLE": ("MAPPING_SOURCE_UNAVAILABLE", "The instrument snapshot is not configured or cannot be used, so these instruments have no mapping evidence."),
    "STALE_SNAPSHOT": ("MAPPING_SNAPSHOT_STALE", "The instrument snapshot is stale, so these instruments have no usable mapping evidence."),
    "CONFLICT": ("MAPPING_CONFLICT", "The snapshot holds conflicting rows for these instruments."),
    "CANDIDATE": ("MAPPING_IDENTITY_UNVERIFIED", "These instruments have a candidate ISIN, but the Master token link is unverified; none is a verified identity."),
    "NO_MASTER_TOKEN": ("MAPPING_NO_MASTER_TOKEN", "These Master orders carry no instrument token, so nothing can be looked up."),
    "UNMAPPED": ("MAPPING_NOT_FOUND", "No snapshot row was found for these instruments."),
    "EXCHANGE_NOT_COVERED": ("MAPPING_EXCHANGE_NOT_COVERED", "These instruments are not on NSE cash equity (BSE and derivatives stay unmapped)."),
    "UNSUPPORTED_SERIES": ("MAPPING_UNSUPPORTED_SERIES", "These instruments are in a series that is not supported."),
    "DELETED": ("MAPPING_DELETED", "The snapshot marks these instruments deleted."),
    "INVALID_ISIN": ("MAPPING_INVALID_ISIN", "The snapshot has no valid ISIN for these instruments."),
    "SYMBOL_DISAGREES": ("MAPPING_SYMBOL_DISAGREES", "The order's symbol differs from the snapshot's for these instruments."),
}


def coverage_warnings(coverage: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Machine-readable warnings for a coverage summary. Always explicit: an unusable source or an empty window is a warning
    of its own, never silence."""
    out: List[Dict[str, Any]] = []
    status = coverage.get("status")
    if status == "NO_MASTER":
        return [_warning("MAPPING_COVERAGE_UNKNOWN", "The strategy has no Master, so there are no Master instruments to assess.")]
    if status == "NO_RECENT_ORDERS":
        out.append(_warning("MAPPING_COVERAGE_UNKNOWN", "The Master placed no orders in the window, so coverage cannot be assessed. "
                                                         "This is not a finding that every instrument is covered."))
    if coverage.get("truncated"):
        out.append(_warning("MAPPING_COVERAGE_TRUNCATED", f"Only the newest {MAX_ORDERS} Master orders in the window were assessed."))
    for state, count in (coverage.get("by_state") or {}).items():
        entry = MAPPING_WARNINGS.get(state)
        if entry and count:
            code, text = entry
            out.append(_warning(code, text, count=count))
    snap = coverage.get("snapshot") or {}
    if snap.get("stale") and "STALE_SNAPSHOT" not in (coverage.get("by_state") or {}):
        out.append(_warning("MAPPING_SNAPSHOT_STALE", "The instrument snapshot is stale."))
    return out


async def coverage_for_master(
    db: AsyncSession, master_account_id: Any, *, now: Optional[datetime] = None, hours: int = DEFAULT_HOURS,
) -> Dict[str, Any]:
    """Mapping coverage of the instruments the Master placed in the window. Reads `orders` only, plus the local snapshot (cached,
    no network). It does not need the shadow job and does not depend on INSTRUMENT_SHADOW_ENABLED."""
    now = now or datetime.now(timezone.utc)
    requested = int(hours)
    hours = max(1, min(MAX_HOURS, requested))
    base = {"window_hours": hours, "max_orders": MAX_ORDERS}
    window = {"requested_hours": requested, "applied_hours": hours, "max_hours": MAX_HOURS, "clamped": requested != hours}
    if master_account_id is None:
        return {**base, "status": "NO_MASTER", "limits": {"window": window, "orders": {"cap": MAX_ORDERS, "read": 0, "truncated": False}},
                "complete": False, "incomplete_reasons": ["NO_MASTER"], **crosswalk_evidence.coverage_summary([], now),
                "total_instruments": 0}
    rows = (await db.execute(
        select(Order.exchange, Order.symbol, Order.instrument_token)
        .where(Order.account_id == master_account_id, Order.source != OrderSource.SYSTEM,
               Order.created_at >= now - timedelta(hours=hours))
        .order_by(Order.created_at.desc())
        .limit(MAX_ORDERS + 1)
    )).all()
    truncated = len(rows) > MAX_ORDERS
    summary = crosswalk_evidence.coverage_summary(rows[:MAX_ORDERS], now)
    status = "AVAILABLE" if summary["total_instruments"] else "NO_RECENT_ORDERS"
    reasons = (["ORDER_CAP_REACHED"] if truncated else []) + (["WINDOW_CLAMPED"] if window["clamped"] else []) \
        + (["NO_RECENT_ORDERS"] if status == "NO_RECENT_ORDERS" else [])
    limits = {"window": window, "orders": {"cap": MAX_ORDERS, "read": min(len(rows), MAX_ORDERS), "truncated": truncated}}
    return {**base, "status": status, "truncated": truncated, "limits": limits, "complete": not reasons,
            "incomplete_reasons": reasons, **summary}


def master_broker_for(master_account: Any) -> Optional[str]:
    """The Master's canonical broker id as the order-time lookup establishes it: NULL means Alice Blue, an unrecognised value is
    None (unknown, which fails closed), and no Master account is None too (callers pass has_master separately)."""
    if master_account is None:
        return None
    return normalize_broker_id(getattr(master_account, "broker", None))


def children_readiness(master_broker: Optional[str], members: Sequence[Any], *, has_master: bool = True) -> List[Dict[str, Any]]:
    """One row per member: display name, broker, whether it is trading-enabled and its pair decision. No ids. With a Master whose
    broker cannot be established (`master_broker` None) every pair is unsupported, exactly as at order time."""
    rows = []
    for m in members:
        account = getattr(m, "account", None)
        broker = getattr(account, "broker", None)
        pair = pair_readiness(master_broker, broker) if has_master else no_master_pair(broker)
        rows.append({
            "display_name": getattr(account, "display_name", None),
            "broker": pair["child_broker"],
            "trading_enabled": bool(getattr(m, "trading_enabled", False)),
            "broker_pair": pair,
        })
    return rows


def strategy_readiness(strategy: Any) -> Dict[str, Any]:
    """Pair readiness of a loaded strategy (members and master_account loaded). Pure: no database, broker or network access."""
    master = getattr(strategy, "master_account", None)
    master_broker = getattr(master, "broker", None) if master is not None else None
    effective_master = master_broker_for(master)
    members = list(getattr(strategy, "members", []) or [])
    children = children_readiness(effective_master, members, has_master=master is not None)
    enabled = [c for c in children if c["trading_enabled"]]
    unsupported = [c for c in enabled if c["broker_pair"]["supported"] is not True]
    warnings: List[Dict[str, Any]] = []
    if master is None:
        warnings.append(_warning("NO_MASTER", "The strategy has no Master, so nothing is copied."))
    for c in unsupported:
        if master is not None:
            warnings.append(_warning(
                BROKER_PAIR_NOT_SUPPORTED,
                f"{c['display_name']}: {c['broker_pair']['message']}",
                child=c["display_name"], child_broker=c["broker"], requirement=c["broker_pair"]["requirement"],
            ))
    if master is not None and not enabled:
        warnings.append(_warning("NO_TRADING_ENABLED_CHILDREN", "No Child is trading-enabled, so nothing is copied."))
    ready = master is not None and bool(enabled) and not unsupported
    return {
        "master": None if master is None else {"display_name": master.display_name, "broker": effective_master},
        "children": children,
        "summary": {
            "children_total": len(children),
            "children_trading_enabled": len(enabled),
            "children_pair_supported": sum(1 for c in enabled if c["broker_pair"]["supported"] is True),
            "children_pair_unsupported": len(unsupported) if master is not None else 0,
        },
        "broker_pairs_ready": ready,
        "observe_only": not bool(settings.ENGINE_LIVE_TRADING_ENABLED),
        "warnings": warnings,
        "note": ("broker_pairs_ready means only that every trading-enabled Child's broker pair is supported. It does not mean orders "
                 "will be placed: live trading, sessions, risk and instrument checks still apply."),
    }


def switch_pair_warnings(new_master_broker: Optional[str], children: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Non-blocking Master-switch warnings: Children whose pair would be unsupported under the NEW Master's broker. `children`
    are {"name": str, "role": str, "broker": str|None}."""
    out = []
    for child in children:
        pair = pair_readiness(new_master_broker, child.get("broker"))
        if pair["supported"] is not True:
            out.append({**child, "pair": pair})
    return out
