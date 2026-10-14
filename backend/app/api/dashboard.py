"""Read-only aggregation for the main Dashboard.

Every trading-relevant number here is derived, never invented:
- P&L (combined, per-account, per-strategy) comes from app.services.pnl.
  PnlService, which reads each account's OWN Alice Blue order/position
  books - the same source /accounts/metrics uses. This module never
  recomputes P&L, never reads copy_trades.estimated_price, never treats
  turnover as P&L, and never touches pnl/calculator.py or pnl/service.py.
- Copy fill rate, execution impact, and orders-by-strategy come from
  Jettone's own `orders` table (SYSTEM-sourced child copies with a
  master_broker_order_id), which already carries each copy's
  broker-confirmed filled_quantity/average_price. Execution impact is
  quantity-weighted (see CopyExecution below) - an execution-quality
  measurement, never P&L.
- "Strategy performance" reflects the engine's actual selection rule in
  CopyEnginePipeline._get_eligible_followers_for_master (one master, at
  most one GroupStrategy actually copied at a time) - it does not invent
  multi-group behavior the engine does not have. Every real GroupStrategy
  is listed; only one can ever be RUNNING.

DATE RANGE: Jettone's own `orders` table has real historical rows (any
date), so copy fill rate / execution impact / orders-by-strategy genuinely
support a date range. PnlService does not: it always reads the broker's
CURRENT order/position book, which has no historical query capability this
codebase has confirmed, and there is no local P&L snapshot store. So P&L
(combined, per-account, per-strategy, and the realized-timeline chart)
is ALWAYS today's trading day, regardless of the selected range - callers
must check `range.is_today` before treating a P&L field as meaningful for
the selected range.

No POST/PUT/PATCH/DELETE, no broker calls beyond the read-only GETs
PnlService already makes (cached), no trading side effects.
"""
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.accounts import get_broker_client_provider
from app.core.config import settings
from app.core.database import get_db
from app.core.redis import get_redis
from app.core.trading_day import trading_day, trading_day_start_utc
from app.models.accounts import Account, AccountType
from app.models.audit import AuditLog, SystemEvent
from app.models.strategies import GroupStrategy, StrategyMember
from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.client import AliceBlueClient
from app.services.pnl.calculator import ZERO, RealizedPoint, money_str
from app.services.pnl.historical import (
    DailyPnl,
    HistoricalFill,
    HistoricalRealizedResult,
    compute_historical_daily,
    compute_historical_realized,
)
from app.services.pnl.service import AccountPnlSummary, AccountRealizedTimeline, PnlService
from app.services.live_trading import get_live_trading_state
from app.services.reliability.emergency_stop import EmergencyStopService, is_master_trading_enabled

logger = logging.getLogger("dashboard")

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])

# Only these events count as an "execution issue" on the dashboard.
# POSITION_MISMATCH_DETECTED is deliberately excluded: reconciliation's
# expected-position calculation has a known false-positive gap (a follower's
# own manual trade or a broker auto square-off that Jettone never recorded),
# so surfacing it here would train the user to ignore this card.
_AUDIT_ISSUE_EVENT_TYPES = ("RISK_REJECTED", "COPY_FAILED")
_SYSTEM_EVENT_ISSUE_TYPES = (
    "CANCELLATION_VERIFICATION_FAILED",
    "PNL_BROKER_MISMATCH",
    "RECONCILIATION_FAILED",
    "SQUARE_OFF_PERSISTENCE_FAILED",
)
_ISSUES_DISPLAY_LIMIT = 5  # compact alert section, not a full event log - "View all" covers the rest

_RANGE_KEYS = ("today", "yesterday", "7d", "30d", "custom")


# ── date range resolution (order-table metrics only - never P&L) ────────────

@dataclass(frozen=True)
class DateRange:
    key: str  # what was actually resolved to, e.g. falls back to "today" on bad custom input
    start: date
    end: date  # inclusive
    is_today: bool
    # Whether the range's end reaches Today - true for "today" itself and
    # for "7d"/"30d" (which always do), false for "yesterday" or a fully
    # past "custom" range. Sent to the frontend so it never has to re-derive
    # this from its own clock (a source of timezone/clock-skew drift) -
    # Unrealized P&L is only ever meaningful when this is true (see
    # pnl/historical.py: an open position's unrealized P&L is a point-in-
    # time fact as of now, and only a range extending through Today ends
    # "now").
    includes_today: bool

    def as_api_fields(self) -> Dict[str, Any]:
        return {
            "key": self.key, "start": self.start.isoformat(), "end": self.end.isoformat(),
            "is_today": self.is_today, "includes_today": self.includes_today,
        }


def _resolve_range(range_key: str, from_date: Optional[str], to_date: Optional[str], now: datetime) -> DateRange:
    today = trading_day(now)
    if range_key == "yesterday":
        d = today - timedelta(days=1)
        return DateRange("yesterday", d, d, False, includes_today=False)
    if range_key == "7d":
        return DateRange("7d", today - timedelta(days=6), today, False, includes_today=True)
    if range_key == "30d":
        return DateRange("30d", today - timedelta(days=29), today, False, includes_today=True)
    if range_key == "custom" and from_date and to_date:
        try:
            start, end = date.fromisoformat(from_date), date.fromisoformat(to_date)
            if start <= end:
                return DateRange("custom", start, end, start == today == end, includes_today=end >= today)
        except ValueError:
            pass
        # Bad/incomplete custom input fails closed to the one range that is
        # always available, rather than guessing a window.
    return DateRange("today", today, today, True, includes_today=True)


def _range_bounds_utc(rng: DateRange) -> tuple[datetime, datetime]:
    """[start, end) in UTC, end exclusive (the day after rng.end)."""
    return trading_day_start_utc(rng.start), trading_day_start_utc(rng.end + timedelta(days=1))


# ── historical realized P&L (Yesterday / 7d / 30d / past custom ranges) ─────
# See app/services/pnl/historical.py's module docstring for exactly what
# this can and cannot answer. Today's own figures never go through this
# path - they always come from the live PnlService, unchanged.

async def _account_historical_fills(
    db: AsyncSession, account_id: uuid.UUID, hist_start: datetime, hist_end: datetime
) -> List[HistoricalFill]:
    """Jettone's own filled `orders` rows for one account over
    [hist_start, hist_end), reduced to HistoricalFill."""
    rows = (
        await db.execute(
            select(
                Order.exchange, Order.symbol, Order.product_type,
                Order.side, Order.filled_quantity, Order.average_price, Order.created_at,
            ).where(
                Order.account_id == account_id,
                Order.created_at >= hist_start,
                Order.created_at < hist_end,
                Order.filled_quantity > 0,
            )
        )
    ).all()
    fills = [
        HistoricalFill(
            account_id=str(account_id),
            exchange=row.exchange,
            symbol=row.symbol,
            product_type=row.product_type,
            side=row.side.value if hasattr(row.side, "value") else str(row.side),
            quantity=row.filled_quantity,
            price=row.average_price,
            # created_at is a UTC-stored DateTime(timezone=True) column, but
            # some drivers (SQLite, used in tests) hand back a naive value -
            # pin it to UTC explicitly so it's never compared against or
            # combined with an aware timestamp (Today's live points) or
            # misread as local time (trading_day() calls .astimezone(), which
            # treats a naive input as system-local, not UTC).
            created_at=row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=timezone.utc),
            day=trading_day(row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=timezone.utc)),
        )
        for row in rows
    ]
    return fills


async def _account_historical_realized(
    db: AsyncSession, account_id: uuid.UUID, hist_start: datetime, hist_end: datetime
) -> HistoricalRealizedResult:
    """Realized P&L for one account over [hist_start, hist_end) - a purely
    historical, pre-Today window built from Jettone's own `orders` table.
    Empty/inverted windows (e.g. the "today" range, which has no historical
    portion at all) are a verified zero, not an error."""
    if hist_start >= hist_end:
        return HistoricalRealizedResult(ZERO, [], [])
    return compute_historical_realized(await _account_historical_fills(db, account_id, hist_start, hist_end))


async def _account_daily_pnl(
    db: AsyncSession,
    account_id: str,
    live_timelines: Dict[str, AccountRealizedTimeline],
    range_start: datetime,
    hist_end: datetime,
    today: date,
    includes_today: bool,
) -> Dict[date, DailyPnl]:
    """One account's DAILY realized P&L keyed by IST trading day: each
    completed day before Today from Jettone's own orders (isolated per day -
    see pnl/historical.py::compute_historical_daily), plus Today's own live
    realized (the broker-book figure PnlService already produces, untouched)
    when the range extends through Today. Days with no fills are absent."""
    days: Dict[date, DailyPnl] = {}
    if range_start < hist_end:
        fills = await _account_historical_fills(db, uuid.UUID(account_id), range_start, hist_end)
        days = {d.day: d for d in compute_historical_daily(fills)}
    if includes_today:
        live = live_timelines.get(account_id)
        if live is None or live.status == "UNAVAILABLE":
            days[today] = DailyPnl(today, None, "UNAVAILABLE", ["TODAY_LIVE_UNAVAILABLE"])
        elif live.points:
            days[today] = DailyPnl(
                today, live.points[-1].cumulative_realized,
                "PARTIAL" if live.status == "PARTIAL" else "OK", list(live.notes),
            )
    return days


def _live_unavailable_reason(account_ids: Sequence[str], pnl_summaries: Dict[str, AccountPnlSummary]) -> str:
    if not account_ids:
        return "No accounts configured"
    n = sum(1 for aid in account_ids if pnl_summaries[aid].total is None)
    return f"{n} account{'' if n == 1 else 's'} unavailable" if n > 0 else "Unavailable"


async def _period_realized_by_account(
    db: AsyncSession,
    account_ids: Sequence[str],
    pnl_summaries: Dict[str, AccountPnlSummary],
    range_start: datetime,
    hist_end: datetime,
    includes_today: bool,
) -> Dict[str, Optional[Decimal]]:
    """Each account's own realized P&L for the selected (non-Today) period:
    Jettone's verified historical realized over [range_start, hist_end) -
    the completed days before Today in the range - plus Today's own LIVE
    realized when the range extends through Today (7d/30d always do). Both
    contributing pieces must be available for that account's figure to be
    anything but None - never a partial sum silently missing one side."""
    result: Dict[str, Optional[Decimal]] = {}
    for aid in account_ids:
        hist = await _account_historical_realized(db, uuid.UUID(aid), range_start, hist_end)
        if not hist.available:
            result[aid] = None
            continue
        if includes_today:
            live_realized = pnl_summaries[aid].realized
            result[aid] = (hist.realized + live_realized) if live_realized is not None else None
        else:
            result[aid] = hist.realized
    return result


def _combine_range_pnl(
    account_ids: Sequence[str],
    pnl_summaries: Dict[str, AccountPnlSummary],
    per_account_realized: Dict[str, Optional[Decimal]],
    rng: DateRange,
    includes_today: bool,
) -> Dict[str, Any]:
    """The dashboard's top-level combined P&L, honoring the selected range:
      - Today: pass through pnl_summaries verbatim - unchanged from before.
      - Any other range: realized is per_account_realized as already
        computed (Jettone's own verified historical orders, plus Today's
        own live realized when the range extends through Today - see
        _period_realized_by_account; never a double count). Unrealized/
        total are only ever Today's LIVE end-of-period snapshot (an open
        position's unrealized P&L is a point-in-time fact, and a range that
        extends through Today ends right now); a range that ends before
        Today (Yesterday, a fully past custom range) has no historical
        end-of-day position or price data anywhere in this system, so
        unrealized/total stay unavailable for it.
    """
    if rng.is_today:
        all_known = bool(account_ids) and all(pnl_summaries[aid].total is not None for aid in account_ids)
        reason = None if all_known else _live_unavailable_reason(account_ids, pnl_summaries)
        return {
            "combined_realized": sum((pnl_summaries[aid].realized for aid in account_ids), ZERO) if all_known else None,
            "combined_unrealized": sum((pnl_summaries[aid].unrealized for aid in account_ids), ZERO) if all_known else None,
            "combined_total": sum((pnl_summaries[aid].total for aid in account_ids), ZERO) if all_known else None,
            "realized_unavailable_reason": reason,
            "unrealized_unavailable_reason": reason,
        }

    all_realized_known = bool(account_ids) and all(v is not None for v in per_account_realized.values())
    combined_realized = sum(per_account_realized.values(), ZERO) if all_realized_known else None
    realized_reason = None if all_realized_known else (
        "No accounts configured" if not account_ids
        else "Some accounts have unverifiable historical fills (missing price data or a suspected overnight carry)"
    )

    combined_unrealized: Optional[Decimal] = None
    unrealized_reason = "No historical end-of-day position or price data exists before Today"
    if includes_today:
        all_unrealized_known = bool(account_ids) and all(pnl_summaries[aid].unrealized is not None for aid in account_ids)
        if all_unrealized_known:
            combined_unrealized = sum((pnl_summaries[aid].unrealized for aid in account_ids), ZERO)
            unrealized_reason = None
        else:
            unrealized_reason = _live_unavailable_reason(account_ids, pnl_summaries)

    combined_total = (
        combined_realized + combined_unrealized
        if combined_realized is not None and combined_unrealized is not None
        else None
    )

    return {
        "combined_realized": combined_realized,
        "combined_unrealized": combined_unrealized,
        "combined_total": combined_total,
        "realized_unavailable_reason": realized_reason,
        "unrealized_unavailable_reason": unrealized_reason if combined_unrealized is None else None,
    }


def get_live_master_id() -> Optional[str]:
    """The account the running engine currently treats as master (the same
    identity CopyEnginePipeline copies from) - or None if no master client
    is live. A dependency so tests can override it without a real engine."""
    from app.services.alice_blue.manager import account_manager

    client = account_manager.get_master_client()
    return client.account_id if client else None


def _audit_issue_message(row: AuditLog) -> str:
    parts = [row.event_type]
    if row.symbol:
        detail = row.symbol
        if row.side:
            detail += f" {row.side}"
        if row.quantity is not None:
            detail += f" x{row.quantity}"
        parts.append(detail)
    if row.reason_code:
        parts.append(row.reason_code)
    return " — ".join(parts)


async def _collect_issues(db: AsyncSession, range_start: datetime, range_end: datetime) -> List[Dict[str, Any]]:
    """AuditLog rows have no resolution concept (a pure append-only trail),
    so they are scoped to the selected date range. SystemEvent rows DO carry
    `resolved` (the same active/historical distinction /system/events
    already uses) - only unresolved ones count as a "current" issue,
    regardless of range, since a still-open problem from yesterday is still
    active today."""
    audit_rows = (
        await db.execute(
            select(AuditLog)
            .where(
                AuditLog.created_at >= range_start,
                AuditLog.created_at < range_end,
                AuditLog.event_type.in_(_AUDIT_ISSUE_EVENT_TYPES),
            )
            .order_by(AuditLog.created_at.desc())
        )
    ).scalars().all()
    system_rows = (
        await db.execute(
            select(SystemEvent)
            .where(
                SystemEvent.resolved.is_(False),
                SystemEvent.event_type.in_(_SYSTEM_EVENT_ISSUE_TYPES),
            )
            .order_by(SystemEvent.created_at.desc())
        )
    ).scalars().all()

    items = [
        {
            "id": str(r.id), "kind": "AUDIT", "type": r.event_type, "severity": "WARNING",
            "message": _audit_issue_message(r),
            "account_id": str(r.account_id) if r.account_id else None,
            "created_at": r.created_at.isoformat(),
        }
        for r in audit_rows
    ] + [
        {
            "id": str(r.id), "kind": "SYSTEM_EVENT", "type": r.event_type, "severity": r.severity.value,
            "message": r.message,
            "account_id": str(r.account_id) if r.account_id else None,
            "created_at": r.created_at.isoformat(),
        }
        for r in system_rows
    ]
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items


# ── copy fill rate / execution impact, global AND per-strategy ──────────────

@dataclass
class StrategyExecution:
    """Same shape as the global copy-execution numbers, scoped to one
    strategy's own copies (by Order.group_strategy_id) within the selected
    date range."""
    considered_orders: int = 0
    filled_orders: int = 0
    working_orders: int = 0
    rejected_orders: int = 0
    target_quantity: int = 0
    filled_quantity: int = 0
    impact_total: Decimal = ZERO
    impact_quantity: int = 0  # denominator for average_per_share; 0 means "no priced fills"

    def as_api_fields(self) -> Dict[str, Any]:
        return {
            "considered_orders": self.considered_orders,
            "filled_orders": self.filled_orders,
            "working_orders": self.working_orders,
            "rejected_orders": self.rejected_orders,
            "target_quantity": self.target_quantity,
            "filled_quantity": self.filled_quantity,
            "rate_percent": (
                round(self.filled_quantity / self.target_quantity * 100, 1) if self.target_quantity > 0 else None
            ),
            "execution_impact": money_str(self.impact_total) if self.impact_quantity > 0 else None,
            "average_per_share": (
                money_str(self.impact_total / self.impact_quantity) if self.impact_quantity > 0 else None
            ),
        }


@dataclass
class CopyExecution:
    """Copy fill rate and execution-impact inputs, both built from Jettone's
    own `orders` table - the child copies' broker-confirmed fills. This is
    an execution-QUALITY measurement, never P&L: it is not derived from, and
    never feeds, any P&L figure.

    Execution impact is quantity-weighted: a 1,000-share fill must move the
    total by 1,000x what a 1-share fill at the same per-share edge does, so
    the KPI reflects real rupee impact rather than one price difference per
    trade regardless of size."""
    filled_quantity: int
    target_quantity: int
    filled_orders: int
    working_orders: int
    rejected_orders: int
    considered_orders: int
    impact_total: Optional[Decimal]
    total_filled_quantity: int
    average_edge_per_share: Optional[Decimal]
    edge_items: List[Dict[str, Any]]
    # Raw (unrounded) per-account impact sums, keyed by account_id string -
    # strategy P&L-adjacent aggregation sums these directly and rounds once,
    # rather than re-parsing already-rounded per-item strings (double-round).
    impact_total_by_account: Dict[str, Decimal]
    # Per-strategy breakdown, keyed by group_strategy_id string, or the
    # literal "unassigned" for real copies with no strategy link (the
    # engine's documented fallback-mode: copies to all active followers when
    # no GroupStrategy exists for the master - see
    # CopyEnginePipeline._get_eligible_followers_for_master).
    by_strategy: Dict[str, StrategyExecution] = field(default_factory=dict)


async def _compute_copy_execution(db: AsyncSession, range_start: datetime, range_end: datetime) -> CopyExecution:
    child_orders = (
        await db.execute(
            select(Order).where(
                Order.created_at >= range_start,
                Order.created_at < range_end,
                Order.source == OrderSource.SYSTEM,
                Order.master_broker_order_id.isnot(None),
            )
        )
    ).scalars().all()

    by_strategy: Dict[str, StrategyExecution] = {}

    def strategy_bucket(child: Order) -> StrategyExecution:
        key = str(child.group_strategy_id) if child.group_strategy_id else "unassigned"
        bucket = by_strategy.get(key)
        if bucket is None:
            bucket = by_strategy[key] = StrategyExecution()
        return bucket

    target_quantity = filled_quantity = 0
    filled_orders = working_orders = rejected_orders = 0
    for child in child_orders:
        bucket = strategy_bucket(child)
        target_quantity += child.quantity
        filled_quantity += child.filled_quantity
        bucket.considered_orders += 1
        bucket.target_quantity += child.quantity
        bucket.filled_quantity += child.filled_quantity
        if child.status == OrderStatus.COMPLETE:
            filled_orders += 1
            bucket.filled_orders += 1
        elif child.status in (OrderStatus.OPEN, OrderStatus.PARTIAL, OrderStatus.PENDING):
            working_orders += 1
            bucket.working_orders += 1
        elif child.status in (OrderStatus.REJECTED, OrderStatus.FAILED, OrderStatus.CANCELLED):
            rejected_orders += 1
            bucket.rejected_orders += 1

    # Pair each filled copy with the master order it copied, by broker order
    # id, to compare real execution prices on both sides. broker_order_id is
    # only unique per (broker, account_id) - see Order's own
    # uq_orders_broker_account_broker_order constraint - so a follower's own
    # order id could coincidentally collide with the master's. Scoping this
    # lookup to MASTER-type accounts (the actual master identity, not merely
    # "not a SYSTEM copy") rules that out: only a master account's order can
    # ever be bound as "the master fill". This matches on account TYPE, not
    # today's specific live master id, since a child copy's
    # master_broker_order_id may reference whichever master account was
    # live when that copy was made.
    master_broker_ids = {
        o.master_broker_order_id
        for o in child_orders
        if o.filled_quantity > 0 and o.average_price and o.average_price > 0
    }
    masters_by_broker_id: Dict[str, Order] = {}
    if master_broker_ids:
        master_rows = (
            await db.execute(
                select(Order)
                .join(Account, Account.id == Order.account_id)
                .where(Order.broker_order_id.in_(master_broker_ids), Account.account_type == AccountType.MASTER)
            )
        ).scalars().all()
        masters_by_broker_id = {m.broker_order_id: m for m in master_rows}

    impact_total = ZERO
    total_filled_quantity = 0
    edge_items: List[Dict[str, Any]] = []
    impact_total_by_account: Dict[str, Decimal] = {}
    for child in child_orders:
        if child.filled_quantity <= 0 or not child.average_price or child.average_price <= 0:
            continue
        master = masters_by_broker_id.get(child.master_broker_order_id)
        if master is None or not master.average_price or master.average_price <= 0:
            continue
        quantity = child.filled_quantity
        # Per-share edge - BUY: paying less than the master is better; SELL:
        # receiving more is better - then weighted by how many shares were
        # actually copied, so the total is a real rupee impact.
        edge_per_share = (
            master.average_price - child.average_price
            if child.side == OrderSide.BUY
            else child.average_price - master.average_price
        )
        impact = edge_per_share * quantity
        impact_total += impact
        total_filled_quantity += quantity
        account_id_str = str(child.account_id)
        impact_total_by_account[account_id_str] = impact_total_by_account.get(account_id_str, ZERO) + impact
        bucket = strategy_bucket(child)
        bucket.impact_total += impact
        bucket.impact_quantity += quantity
        strategy_id = str(child.group_strategy_id) if child.group_strategy_id else None
        item = {
            "symbol": child.symbol,
            "side": child.side.value,
            "time": child.created_at.isoformat(),
            "quantity": quantity,
            "master_price": money_str(master.average_price),
            "child_price": money_str(child.average_price),
            "edge_per_share": money_str(edge_per_share),
            "impact": money_str(impact),
            "child_account_id": account_id_str,
            "group_strategy_id": strategy_id,
        }
        edge_items.append(item)

    edge_items.sort(key=lambda x: x["time"])

    return CopyExecution(
        filled_quantity=filled_quantity,
        target_quantity=target_quantity,
        filled_orders=filled_orders,
        working_orders=working_orders,
        rejected_orders=rejected_orders,
        considered_orders=len(child_orders),
        impact_total=impact_total if edge_items else None,
        total_filled_quantity=total_filled_quantity,
        average_edge_per_share=(impact_total / total_filled_quantity) if edge_items else None,
        edge_items=edge_items,
        impact_total_by_account=impact_total_by_account,
        by_strategy=by_strategy,
    )


def _edge_direction(impact_total: Optional[Decimal]) -> Optional[str]:
    if impact_total is None:
        return None
    if impact_total > 0:
        return "children_better"
    if impact_total < 0:
        return "children_worse"
    return "even"


# ── strategy performance (every real strategy, not just the running one) ────

async def _load_strategies(db: AsyncSession) -> List[GroupStrategy]:
    return (
        await db.execute(
            select(GroupStrategy)
            .where(GroupStrategy.deleted_at.is_(None))
            .options(
                selectinload(GroupStrategy.members).selectinload(StrategyMember.account),
                selectinload(GroupStrategy.master_account),
            )
            .order_by(GroupStrategy.created_at.asc())
        )
    ).scalars().all()


def _running_candidates(strategies: Sequence[GroupStrategy], live_master_id: Optional[str]) -> List[GroupStrategy]:
    # Mirrors CopyEnginePipeline._get_eligible_followers_for_master's actual
    # selection: the engine copies from at most one trading_enabled strategy
    # per master, with no defined tie-break when more than one qualifies.
    return [s for s in strategies if s.trading_enabled and live_master_id and str(s.master_account_id) == live_master_id]


def _strategy_status(s: GroupStrategy, is_the_picked_running_one: bool) -> str:
    if is_the_picked_running_one:
        return "RUNNING"
    if not s.trading_enabled:
        return "DISABLED"
    return "NOT_RUNNING"


def _not_running_reason(s: GroupStrategy, live_master_id: Optional[str], status: str) -> Optional[str]:
    if status == "RUNNING":
        return None
    if status == "DISABLED":
        return "Trading disabled for this strategy"
    if not live_master_id:
        return "No live master session"
    if s.master_account_id is None:
        return "No master account assigned to this strategy"
    if str(s.master_account_id) != live_master_id:
        return "Master account for this strategy is not the currently live master"
    return "Another strategy for this master is running instead (the engine runs one strategy per master)"


def _strategy_pnl(
    strategy: GroupStrategy,
    pnl_summaries: Dict[str, AccountPnlSummary],
    per_account_realized: Dict[str, Optional[Decimal]],
    is_today: bool,
) -> Dict[str, Any]:
    """Master + follower P&L for this strategy's own linked accounts.

    Today: real per-account PnlService totals (realized+unrealized), summed
    as raw Decimal and rounded once - unchanged from before.

    Any other range: master_pnl/followers_pnl/total_pnl (all gross totals)
    stay unavailable - unrealized has no historical data source (see
    pnl/historical.py) - but realized_pnl exposes the combined REALIZED
    figure from per_account_realized (Jettone's own verified historical
    orders, plus Today's own live realized when the range extends through
    Today), so the Strategy Performance table can show a real, honestly-
    labeled number instead of a blanket "unavailable"."""
    if is_today:
        master_summary = pnl_summaries.get(str(strategy.master_account_id)) if strategy.master_account_id else None
        member_totals: List[Optional[Decimal]] = [
            pnl_summaries[str(m.account_id)].total if str(m.account_id) in pnl_summaries else None
            for m in strategy.members
        ]
        followers_pnl = (
            money_str(sum(member_totals, ZERO)) if member_totals and all(v is not None for v in member_totals) else None
        )
        master_pnl = money_str(master_summary.total) if master_summary and master_summary.total is not None else None
        total_pnl = (
            money_str(master_summary.total + sum((v for v in member_totals), ZERO))
            if master_summary and master_summary.total is not None and member_totals and all(v is not None for v in member_totals)
            else None
        )
        return {
            "master_pnl": master_pnl, "followers_pnl": followers_pnl, "total_pnl": total_pnl,
            "realized_pnl": None, "unavailable_reason": None, "partial_note": None,
        }

    # Master-anchored: the master's own verified realized figure is this
    # strategy's primary number - what the admin actually entered - so it
    # must never be withheld just because a follower's copy has an old,
    # unrelated unresolved position. A follower whose historical fills can't
    # be verified is excluded from the sum (never guessed as zero) and named
    # in `partial_note`, so the total is always either fully accurate or
    # explicitly marked as missing a named piece - never silently wrong.
    master_id = str(strategy.master_account_id) if strategy.master_account_id else None
    if not master_id:
        return {
            "master_pnl": None, "followers_pnl": None, "total_pnl": None, "realized_pnl": None,
            "unavailable_reason": "No master account linked to this strategy", "partial_note": None,
        }
    if per_account_realized.get(master_id) is None:
        return {
            "master_pnl": None, "followers_pnl": None, "total_pnl": None, "realized_pnl": None,
            "unavailable_reason": "Master account has unverifiable historical fills (missing price data or a suspected overnight carry)",
            "partial_note": None,
        }

    included = [master_id]
    excluded_names: List[str] = []
    for m in strategy.members:
        fid = str(m.account_id)
        if per_account_realized.get(fid) is None:
            excluded_names.append(m.account.display_name if m.account else fid[:8])
        else:
            included.append(fid)

    realized_total = sum((per_account_realized[aid] for aid in included), ZERO)
    partial_note = (
        f"Excludes {', '.join(excluded_names)} - unverifiable historical fills (missing price data or a suspected overnight carry)"
        if excluded_names else None
    )
    return {
        "master_pnl": None, "followers_pnl": None, "total_pnl": None,
        "realized_pnl": money_str(realized_total), "unavailable_reason": None,
        "partial_note": partial_note,
    }


def _build_strategy_performance(
    strategies: Sequence[GroupStrategy],
    live_master_id: Optional[str],
    pnl_summaries: Dict[str, AccountPnlSummary],
    per_account_realized: Dict[str, Optional[Decimal]],
    copy_execution: CopyExecution,
    is_today: bool,
) -> Dict[str, Any]:
    running_candidates = _running_candidates(strategies, live_master_id)
    running = running_candidates[0] if running_candidates else None
    ambiguous = len(running_candidates) > 1

    rows: List[Dict[str, Any]] = []
    for s in strategies:
        is_running = running is not None and s.id == running.id
        status = _strategy_status(s, is_running)
        exec_stats = copy_execution.by_strategy.get(str(s.id), StrategyExecution())
        rows.append(
            {
                "id": str(s.id),
                "name": s.name,
                "status": status,
                "status_reason": _not_running_reason(s, live_master_id, status),
                "master": {
                    "account_id": str(s.master_account_id) if s.master_account_id else None,
                    "display_name": s.master_account.display_name if s.master_account else None,
                },
                "followers": [
                    {
                        "account_id": str(m.account_id),
                        "display_name": m.account.display_name if m.account else None,
                        "multiplier": str(m.multiplier),
                        "trading_enabled": m.trading_enabled,
                    }
                    for m in s.members
                ],
                "follower_count": len(s.members),
                "pnl": _strategy_pnl(s, pnl_summaries, per_account_realized, is_today),
                "execution": exec_stats.as_api_fields(),
            }
        )

    return {"list": rows, "live_master_id": live_master_id, "ambiguous": ambiguous}


async def _orders_by_strategy(
    db: AsyncSession, range_start: datetime, range_end: datetime, strategies: Sequence[GroupStrategy]
) -> List[Dict[str, Any]]:
    """Real copy/order activity grouped by strategy for the selected range -
    counts child SYSTEM copies (the same population copy_fill/execution
    impact use), from Jettone's own `orders` table.

    Every real, configured GroupStrategy is always included, at count 0 if
    it had no copy activity in the selected range - the same strategy list
    Strategy Performance uses, so a strategy never silently disappears from
    this chart just because nothing was copied for it in this window."""
    rows = (
        await db.execute(
            select(Order.group_strategy_id, func.count().label("cnt"))
            .where(
                Order.created_at >= range_start,
                Order.created_at < range_end,
                Order.source == OrderSource.SYSTEM,
                Order.master_broker_order_id.isnot(None),
            )
            .group_by(Order.group_strategy_id)
        )
    ).all()
    counts_by_id: Dict[Optional[str], int] = {
        (str(row.group_strategy_id) if row.group_strategy_id else None): int(row.cnt) for row in rows
    }
    result = [
        {
            "strategy_id": str(s.id),
            "strategy_name": s.name,
            "count": counts_by_id.get(str(s.id), 0),
        }
        for s in strategies
    ]
    unassigned_count = counts_by_id.get(None, 0)
    if unassigned_count > 0:
        result.append({"strategy_id": None, "strategy_name": "Unassigned", "count": unassigned_count})
    result.sort(key=lambda r: r["count"], reverse=True)
    return result


def _real_trading_impact(
    strategies: Sequence[GroupStrategy],
    live_master_id: Optional[str],
    pnl_summaries: Dict[str, AccountPnlSummary],
    per_account_realized: Dict[str, Optional[Decimal]],
    copy_execution: CopyExecution,
    is_today: bool,
) -> Optional[Dict[str, Any]]:
    """Master performance -> copy execution -> follower result, for the
    strategy the engine is actually running right now. None when nothing is
    running (never a fabricated stand-in)."""
    running_candidates = _running_candidates(strategies, live_master_id)
    running = running_candidates[0] if running_candidates else None
    if running is None:
        return None

    pnl = _strategy_pnl(running, pnl_summaries, per_account_realized, is_today)
    exec_stats = copy_execution.by_strategy.get(str(running.id), StrategyExecution())
    return {
        "strategy_id": str(running.id),
        "strategy_name": running.name,
        "master_pnl": pnl["master_pnl"],
        "followers_pnl": pnl["followers_pnl"],
        "pnl_unavailable_reason": pnl["unavailable_reason"],
        "copy_execution_impact": exec_stats.as_api_fields()["execution_impact"],
        "copied_orders": exec_stats.considered_orders,
        "filled_copies": exec_stats.filled_orders,
        "failed_missed_copies": exec_stats.rejected_orders,
        "working_copies": exec_stats.working_orders,
    }


@router.get("/summary")
async def get_dashboard_summary(
    db: AsyncSession = Depends(get_db),
    redis: Any = Depends(get_redis),
    client_provider: Callable[[str], Optional[AliceBlueClient]] = Depends(get_broker_client_provider),
    live_master_id: Optional[str] = Depends(get_live_master_id),
    range: str = Query("today", pattern="^(today|yesterday|7d|30d|custom)$"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    """
    One read-only payload for the main Dashboard. Two independent time
    scopes:
      - `platform`, `status` are always CURRENT state - never affected by
        the selected range.
      - `pnl`, `strategies[].pnl`, `real_trading_impact.*_pnl` are always
        TODAY's trading day - PnlService has no historical capability (see
        module docstring); `range.is_today` tells the caller when a
        non-today range was requested, so those fields can be labeled
        unavailable-for-range instead of silently showing today's numbers.
      - `copy_fill`, `execution_edge`, `orders_by_strategy`, and
        `strategies[].execution` DO honor the selected range - they come
        from Jettone's own `orders` table, which has real historical rows.
    Combined P&L sums each DISTINCT account's own total_pnl exactly once
    (see PnlService/accounts/metrics) - never a strategy's member accounts,
    and never twice for an account in more than one strategy.
    """
    now = datetime.now(timezone.utc)
    today = trading_day(now)
    today_start = trading_day_start_utc(today)
    rng = _resolve_range(range, from_date, to_date, now)
    range_start, range_end = _range_bounds_utc(rng)

    accounts = (
        await db.execute(
            select(Account)
            .where(Account.deleted_at.is_(None))
            .order_by(Account.account_type.asc(), Account.created_at.asc())
        )
    ).scalars().all()
    account_ids = [str(a.id) for a in accounts]

    pnl_summaries = await PnlService(
        redis=redis, client_provider=client_provider, now=lambda: now
    ).summarize_accounts(account_ids)

    # Per-account historical+live realized - computed once and reused by
    # the combined "pnl" figures below, Strategy Performance, and Real
    # Trading Impact, so each account's historical fills are only queried
    # once regardless of how many places reference it. See
    # _period_realized_by_account / pnl/historical.py for exactly what this
    # can and cannot answer for a non-Today range.
    includes_today = rng.includes_today
    per_account_realized: Dict[str, Optional[Decimal]] = (
        {aid: pnl_summaries[aid].realized for aid in account_ids}
        if rng.is_today
        else await _period_realized_by_account(
            db, account_ids, pnl_summaries, range_start, min(range_end, today_start), includes_today
        )
    )

    # ── Combined P&L for the selected range - see _combine_range_pnl for
    # exactly what Today vs. a historical range each can and cannot answer.
    range_pnl = _combine_range_pnl(account_ids, pnl_summaries, per_account_realized, rng, includes_today)
    combined_realized = range_pnl["combined_realized"]
    combined_unrealized = range_pnl["combined_unrealized"]
    combined_total = range_pnl["combined_total"]
    accounts_unavailable = sum(1 for aid in account_ids if pnl_summaries[aid].total is None)

    all_positions_known = bool(account_ids) and all(
        pnl_summaries[aid].open_positions_count is not None for aid in account_ids
    )
    combined_open_positions = (
        sum(pnl_summaries[aid].open_positions_count for aid in account_ids) if all_positions_known else None
    )

    accounts_payload = [
        {
            "account_id": str(a.id),
            "display_name": a.display_name,
            "account_type": a.account_type.value,
            "is_active": a.is_active,
            "session_valid": a.session_valid,
            **pnl_summaries[str(a.id)].as_api_fields(),
        }
        for a in accounts
    ]

    master_account = next((a for a in accounts if str(a.id) == live_master_id), None)
    if master_account is None:
        # No live session yet, but still show whichever MASTER account is
        # configured so the status strip has something honest to display.
        master_account = next((a for a in accounts if a.account_type == AccountType.MASTER), None)
    followers = [a for a in accounts if a.account_type == AccountType.FOLLOWER]
    connected_followers = sum(1 for a in followers if a.is_active and a.session_valid)
    connected_accounts_total = sum(1 for a in accounts if a.is_active and a.session_valid)

    copy_execution = await _compute_copy_execution(db, range_start, range_end)
    fill_rate_percent = (
        round(copy_execution.filled_quantity / copy_execution.target_quantity * 100, 1)
        if copy_execution.target_quantity > 0
        else None
    )

    strategies = await _load_strategies(db)
    running_candidates = _running_candidates(strategies, live_master_id)

    issues = await _collect_issues(db, range_start, range_end)
    strategy_performance = _build_strategy_performance(
        strategies, live_master_id, pnl_summaries, per_account_realized, copy_execution, rng.is_today
    )
    orders_by_strategy = await _orders_by_strategy(db, range_start, range_end, strategies)
    real_trading_impact = _real_trading_impact(
        strategies, live_master_id, pnl_summaries, per_account_realized, copy_execution, rng.is_today
    )

    return {
        "trading_day": today.isoformat(),
        "as_of": now.isoformat(),
        "range": rng.as_api_fields(),
        # ── Row 1: platform state - NEVER affected by `range` ───────────────
        "platform": {
            "demat_accounts": {"total": len(accounts), "connected": connected_accounts_total},
            "static_ip": None,  # served by the existing GET /system/static-ip - not duplicated here
            "strategies": {"total": len(strategies), "active": len(running_candidates)},
        },
        "status": {
            "live_trading_enabled": (await get_live_trading_state(db)).enabled,
            "master_trading_enabled": await is_master_trading_enabled(redis),
            "kill_switch_active": await EmergencyStopService(redis).is_global_active(),
            "master": (
                None
                if master_account is None
                else {
                    "account_id": str(master_account.id),
                    "display_name": master_account.display_name,
                    "alice_user_id": master_account.alice_user_id,
                    "session_valid": master_account.session_valid,
                    "is_live": str(master_account.id) == live_master_id,
                }
            ),
            "children": {"total": len(followers), "connected": connected_followers},
        },
        # ── Row 2 + Real Trading Impact: combined_realized now honors the
        # selected range (Today's live figure, or Jettone's own verified
        # historical orders plus Today's live figure when the range extends
        # through Today - see _combine_range_pnl). combined_unrealized/total
        # are only ever Today's live snapshot, since no historical end-of-
        # day position or price data exists anywhere in this system - the
        # *_unavailable_reason fields say exactly why when null. ─────────
        "pnl": {
            "combined_realized": money_str(combined_realized),
            "combined_unrealized": money_str(combined_unrealized),
            "combined_total": money_str(combined_total),
            "pnl_basis": "GROSS",
            "accounts_total": len(account_ids),
            "accounts_unavailable": accounts_unavailable,
            "accounts": accounts_payload,
            "realized_unavailable_reason": range_pnl["realized_unavailable_reason"],
            "unrealized_unavailable_reason": range_pnl["unrealized_unavailable_reason"],
        },
        "open_positions": {
            "total": combined_open_positions,
            "accounts_total": len(account_ids),
        },
        "copy_fill": {
            "filled_quantity": copy_execution.filled_quantity,
            "target_quantity": copy_execution.target_quantity,
            "rate_percent": fill_rate_percent,
            "filled_orders": copy_execution.filled_orders,
            "working_orders": copy_execution.working_orders,
            "rejected_orders": copy_execution.rejected_orders,
            "considered_orders": copy_execution.considered_orders,
        },
        "execution_edge": {
            "total": money_str(copy_execution.impact_total),
            "average_per_share": money_str(copy_execution.average_edge_per_share),
            "total_quantity": copy_execution.total_filled_quantity,
            "sample_count": len(copy_execution.edge_items),
            "direction": _edge_direction(copy_execution.impact_total),
            "items": copy_execution.edge_items[-30:],
        },
        "issues": {"count": len(issues), "items": issues[:_ISSUES_DISPLAY_LIMIT]},
        "strategies": strategy_performance,
        "orders_by_strategy": orders_by_strategy,
        "real_trading_impact": real_trading_impact,
    }


def _combine_strategy_series(
    master_id: Optional[str], follower_ids: Sequence[str], timelines: Dict[str, AccountRealizedTimeline]
) -> Dict[str, Any]:
    """Merge the master's + any verifiable followers' raw-Decimal realized-
    P&L points into ONE combined step series for a strategy - RAW Decimal
    values summed at each shared time checkpoint, rounded exactly once per
    output point. Never a client-side re-sum of already-rounded strings
    (that would double-round, the same class of bug already fixed for the
    strategy P&L KPI).

    Master-anchored, mirroring _strategy_pnl: the master's own curve is what
    "my daily entries" means for this strategy, so it is never blanked out
    just because a follower has an unrelated unresolved position. A follower
    whose own timeline can't be verified is left out of the merge (never
    guessed/interpolated) and flips status to PARTIAL rather than
    UNAVAILABLE, so the chart still shows the master's real curve."""
    if not master_id or master_id not in timelines or timelines[master_id].status == "UNAVAILABLE":
        return {"status": "UNAVAILABLE", "points": []}

    relevant = [timelines[master_id]]
    excluded_a_follower = False
    for fid in follower_ids:
        t = timelines.get(fid)
        if t is None or t.status == "UNAVAILABLE":
            excluded_a_follower = True
            continue
        relevant.append(t)

    per_account_by_time = [{p.time: p.cumulative_realized for p in t.points} for t in relevant]
    all_times = sorted({t for d in per_account_by_time for t in d})
    running = [ZERO] * len(relevant)
    points = []
    for t in all_times:
        for i, d in enumerate(per_account_by_time):
            if t in d:
                running[i] = d[t]
        points.append({"time": t.isoformat(), "cumulative_realized": money_str(sum(running, ZERO))})

    status = "PARTIAL" if excluded_a_follower or any(t.status == "PARTIAL" for t in relevant) else "OK"
    return {"status": status, "points": points}


def _combine_strategy_daily(
    master_id: Optional[str], follower_ids: Sequence[str], daily_by_account: Dict[str, Dict[date, DailyPnl]]
) -> Dict[str, Any]:
    """Per-day (NOT cumulative) P&L for one strategy: master + verifiable
    followers summed within each trading day, in raw Decimal and rounded
    exactly once. Each day is independent - a day that can't be costed is
    reported with its reasons and a null value, and never touches any other
    day.

    Master-anchored like _strategy_pnl: a master day that can't be costed is
    UNAVAILABLE for the strategy; a follower day that can't be costed is
    left out of that day's sum (never guessed as 0) and makes the day
    PARTIAL. An account that is both master and member counts once.
    `unavailable_reason` is set only when no day has a usable value at all.
    """
    if not master_id or master_id not in daily_by_account:
        return {"daily": [], "unavailable_reason": "No master account linked to this strategy"}

    followers: List[str] = []
    for fid in follower_ids:
        if fid != master_id and fid not in followers and fid in daily_by_account:
            followers.append(fid)

    all_days = sorted({d for aid in [master_id, *followers] for d in daily_by_account[aid]})
    daily: List[Dict[str, Any]] = []
    for day in all_days:
        reasons: List[str] = []
        m = daily_by_account[master_id].get(day)
        if m is not None and m.daily_pnl is None:
            daily.append({
                "date": day.isoformat(), "daily_pnl": None, "status": "UNAVAILABLE",
                "reasons": [f"MASTER:{r}" for r in m.reasons],
            })
            continue
        total = ZERO
        partial = False
        if m is not None:
            total += m.daily_pnl
            partial = m.status == "PARTIAL"
            reasons += [f"MASTER:{r}" for r in m.reasons]
        for fid in followers:
            f = daily_by_account[fid].get(day)
            if f is None:
                continue
            if f.daily_pnl is None:
                partial = True
                reasons += [f"FOLLOWER_EXCLUDED:{fid[:8]}:{r}" for r in f.reasons]
                continue
            total += f.daily_pnl
            if f.status == "PARTIAL":
                partial = True
                reasons += [f"FOLLOWER:{fid[:8]}:{r}" for r in f.reasons]
        daily.append({
            "date": day.isoformat(), "daily_pnl": money_str(total),
            "status": "PARTIAL" if partial else "OK", "reasons": reasons,
        })

    unavailable_reason = None
    if daily and all(d["daily_pnl"] is None for d in daily):
        first = next((r for d in daily for r in d["reasons"]), "")
        unavailable_reason = f"No trading day in this range has a verifiable P&L ({first})" if first else "No verifiable P&L"
    return {"daily": daily, "unavailable_reason": unavailable_reason}


async def _account_period_timeline(
    db: AsyncSession,
    account_id: str,
    live_timelines: Dict[str, AccountRealizedTimeline],
    range_start: datetime,
    hist_end: datetime,
    includes_today: bool,
) -> AccountRealizedTimeline:
    """One account's cumulative realized P&L across the selected (non-Today)
    period: Jettone's own verified historical points over
    [range_start, hist_end), continued by Today's own live points (offset so
    the curve keeps accumulating, never restarting at zero) when the range
    extends through Today. UNAVAILABLE end to end whenever either
    contributing piece is - never a partial curve missing one side."""
    day = trading_day(range_start)
    hist = await _account_historical_realized(db, uuid.UUID(account_id), range_start, hist_end)
    if not hist.available:
        return AccountRealizedTimeline(account_id, day, "UNAVAILABLE", [], hist.notes)

    hist_points = [
        RealizedPoint(time=p.time, broker_order_id="", symbol=p.symbol, side=p.side, quantity=p.quantity,
                       cumulative_realized=p.cumulative_realized)
        for p in hist.points
    ]
    if not includes_today:
        return AccountRealizedTimeline(account_id, day, "OK", hist_points, [])

    live = live_timelines.get(account_id)
    if live is None or live.status == "UNAVAILABLE":
        return AccountRealizedTimeline(account_id, day, "UNAVAILABLE", [], ["TODAY_LIVE_UNAVAILABLE"])

    offset = hist.realized
    combined_points = hist_points + [
        RealizedPoint(time=p.time, broker_order_id=p.broker_order_id, symbol=p.symbol, side=p.side,
                       quantity=p.quantity, cumulative_realized=p.cumulative_realized + offset)
        for p in live.points
    ]
    status = "PARTIAL" if live.status == "PARTIAL" else "OK"
    return AccountRealizedTimeline(account_id, day, status, combined_points, list(live.notes))


@router.get("/realized-timeline")
async def get_dashboard_realized_timeline(
    db: AsyncSession = Depends(get_db),
    redis: Any = Depends(get_redis),
    client_provider: Callable[[str], Optional[AliceBlueClient]] = Depends(get_broker_client_provider),
    range: str = Query("today", pattern="^(today|yesterday|7d|30d|custom)$"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
):
    """
    Cumulative REALIZED P&L per STRATEGY (master + its followers' points
    combined), for the Strategy P&L chart:
      - Today: unchanged from before - Today's own live broker fills.
      - Any other range: Jettone's own verified historical orders (see
        pnl/historical.py), continued by Today's own live points when the
        range extends through Today (7d/30d always do). UNAVAILABLE for a
        strategy whenever any of its accounts' fills can't be verified -
        never a partial or fabricated curve. There is still no historical
        LTP/position-snapshot data anywhere in this system, so this chart -
        like the P&L cards - only ever shows REALIZED, never unrealized.
    """
    now = datetime.now(timezone.utc)
    today = trading_day(now)
    today_start = trading_day_start_utc(today)
    rng = _resolve_range(range, from_date, to_date, now)
    range_start, range_end = _range_bounds_utc(rng)

    accounts = (
        await db.execute(select(Account).where(Account.deleted_at.is_(None)))
    ).scalars().all()
    account_ids = [str(a.id) for a in accounts]

    live_timelines = await PnlService(
        redis=redis, client_provider=client_provider, now=lambda: now
    ).realized_timelines(account_ids)

    if rng.is_today:
        timelines = live_timelines
    else:
        includes_today = rng.includes_today
        hist_end = min(range_end, today_start)
        timelines = {
            aid: await _account_period_timeline(db, aid, live_timelines, range_start, hist_end, includes_today)
            for aid in account_ids
        }

    # Additive per-day view (`daily`): each trading day's own P&L, isolated
    # per day. `points`/`status` above are untouched for the main dashboard.
    daily_hist_end = min(range_end, today_start)
    daily_by_account = {
        aid: await _account_daily_pnl(db, aid, live_timelines, range_start, daily_hist_end, today, rng.includes_today)
        for aid in account_ids
    }

    strategies = await _load_strategies(db)
    strategies_payload = {}
    for s in strategies:
        member_ids = [str(m.account_id) for m in s.members]
        master_id = str(s.master_account_id) if s.master_account_id else None
        series = _combine_strategy_series(master_id, member_ids, timelines)
        daily = _combine_strategy_daily(master_id, member_ids, daily_by_account)
        strategies_payload[str(s.id)] = {"name": s.name, **series, **daily}

    return {
        "trading_day": today.isoformat(),
        "as_of": now.isoformat(),
        "range": rng.as_api_fields(),
        "strategies": strategies_payload,
    }
