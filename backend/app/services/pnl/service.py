"""Per-account gross P&L for the Demat and group-strategy cards.

Read-only. Each account's P&L is computed by Jettone from that account's OWN
Alice Blue order book (the fill ledger) and position book (LTP for open
positions), then cross-checked against - never replaced by - the broker's
realizedPnl / unrealizedPnl / MtoM.

Jettone's orders and copy_trades tables are deliberately not read: they miss
fills Jettone did not place (a follower's own manual trades, broker auto
square-off), record Jettone's own child square-offs at average_price 0, and
copy_trades carries master-derived prices. None of them is a fill ledger.

No state is kept between calls - every result is rebuilt from the broker
books, so it is unaffected by restarts. Broker reads are cached in Redis for
PNL_BROKER_CACHE_TTL_SECONDS per account and book.
"""
import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence

from sqlalchemy import select

from app.core import database as _database
from app.core.config import settings
from app.core.trading_day import trading_day
from app.models.audit import SystemEvent
from app.services.monitoring.notifier import emit_system_event
from app.services.pnl.broker_data import BrokerPosition, parse_order_book_fills, parse_positions
from app.services.pnl.calculator import (
    CHECK_MISMATCH,
    BrokerCheck,
    ComponentCheck,
    RealizedPoint,
    compute_pnl,
    compute_realized_timeline,
    money_str,
    reconcile_with_broker,
)

logger = logging.getLogger("pnl")

PNL_BASIS_GROSS = "GROSS"
PNL_STATUS_OK = "OK"
PNL_STATUS_PARTIAL = "PARTIAL"
PNL_STATUS_UNAVAILABLE = "UNAVAILABLE"

BROKER_CACHE_PREFIX = "pnl:broker:"
MISMATCH_EVENT_TYPE = "PNL_BROKER_MISMATCH"
MISMATCH_ALERT_KEY_PREFIX = "pnl:mismatch_alerted:"
MISMATCH_ALERT_KEY_TTL_SECONDS = 2 * 24 * 3600

ClientProvider = Callable[[str], Optional[Any]]


def _component_fields(check: ComponentCheck) -> Dict[str, Optional[str]]:
    return {
        "jettone": money_str(check.jettone),
        "broker": money_str(check.broker),
        "diff": money_str(check.diff),
        "status": check.status,
    }


@dataclass
class AccountPnlSummary:
    account_id: str
    trading_day: date
    as_of: datetime
    status: str
    realized: Optional[Decimal]
    unrealized: Optional[Decimal]
    total: Optional[Decimal]
    open_positions_count: Optional[int]
    broker_check: BrokerCheck
    notes: List[str] = field(default_factory=list)

    def as_api_fields(self) -> Dict[str, Any]:
        """Response fields; the only place money is rounded (2-decimal strings)."""
        return {
            "realized_pnl": money_str(self.realized),
            "unrealized_pnl": money_str(self.unrealized),
            "total_pnl": money_str(self.total),
            "pnl_basis": PNL_BASIS_GROSS,
            "pnl_status": self.status,
            "pnl_notes": list(self.notes),
            "open_positions_count": self.open_positions_count,
            "broker_check": {
                "status": self.broker_check.status,
                "tolerance": money_str(self.broker_check.tolerance),
                "realized": _component_fields(self.broker_check.realized),
                "unrealized": _component_fields(self.broker_check.unrealized),
                "total": _component_fields(self.broker_check.total),
            },
            "trading_day": self.trading_day.isoformat(),
            "as_of": self.as_of.isoformat(),
        }


@dataclass
class AccountRealizedTimeline:
    """Dashboard chart data: this account's cumulative realized P&L today,
    one point per fill. Built from the same broker order-book rows (and the
    same Redis cache) as AccountPnlSummary - never a second, different
    reading of the account's fills."""
    account_id: str
    trading_day: date
    status: str  # OK (every fill usable) / PARTIAL (some skipped, see notes) / UNAVAILABLE
    points: List[RealizedPoint]
    notes: List[str]

    def as_api_fields(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "points": [
                {
                    "time": p.time.isoformat(),
                    "broker_order_id": p.broker_order_id,
                    "symbol": p.symbol,
                    "side": p.side,
                    "quantity": p.quantity,
                    "cumulative_realized": money_str(p.cumulative_realized),
                }
                for p in self.points
            ],
            "notes": list(self.notes),
        }


def _open_positions_count(positions: Optional[Sequence[BrokerPosition]]) -> Optional[int]:
    """POS: broker position rows with netQuantity != 0."""
    if positions is None or any(p.net_quantity is None for p in positions):
        return None
    return sum(1 for p in positions if p.net_quantity != 0)


class PnlService:
    def __init__(
        self,
        redis: Any,
        client_provider: ClientProvider,
        now: Optional[Callable[[], datetime]] = None,
        cache_ttl_seconds: Optional[int] = None,
        fetch_timeout_seconds: float = 8.0,
    ):
        self._redis = redis
        self._client_provider = client_provider
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._cache_ttl = (
            cache_ttl_seconds if cache_ttl_seconds is not None else settings.PNL_BROKER_CACHE_TTL_SECONDS
        )
        self._fetch_timeout = fetch_timeout_seconds

    async def summarize_accounts(self, account_ids: Iterable[Any]) -> Dict[str, AccountPnlSummary]:
        now = self._now()
        day = trading_day(now)
        ids = [str(account_id) for account_id in account_ids]
        summaries = await asyncio.gather(*(self._summarize(account_id, day, now) for account_id in ids))
        return dict(zip(ids, summaries))

    async def _summarize(self, account_id: str, day: date, now: datetime) -> AccountPnlSummary:
        client = self._client_provider(account_id)
        if client is None:
            return self._unavailable(account_id, day, now, None, "NO_BROKER_CLIENT")

        order_rows, position_rows = await asyncio.gather(
            self._read_broker_rows(account_id, "orderbook", client.get_order_book_raw),
            self._read_broker_rows(account_id, "positions", client.get_positions_raw),
        )
        positions = parse_positions(position_rows) if position_rows is not None else None
        if order_rows is None:
            # Never fall back to Jettone's own tables - they are not a fill ledger.
            return self._unavailable(account_id, day, now, positions, "BROKER_ORDER_BOOK_UNAVAILABLE")

        fills, issues = parse_order_book_fills(order_rows)
        result = compute_pnl(fills, positions, day, account_notes=issues)
        summary = AccountPnlSummary(
            account_id=account_id,
            trading_day=day,
            as_of=now,
            status=PNL_STATUS_OK if result.fully_verified else PNL_STATUS_PARTIAL,
            realized=result.realized,
            unrealized=result.unrealized,
            total=result.total,
            open_positions_count=_open_positions_count(positions),
            broker_check=reconcile_with_broker(result, positions),
            notes=result.notes,
        )
        if summary.broker_check.status == CHECK_MISMATCH:
            await self._alert_mismatch_once(summary)
        return summary

    @staticmethod
    def _unavailable(
        account_id: str,
        day: date,
        now: datetime,
        positions: Optional[Sequence[BrokerPosition]],
        note: str,
    ) -> AccountPnlSummary:
        return AccountPnlSummary(
            account_id=account_id,
            trading_day=day,
            as_of=now,
            status=PNL_STATUS_UNAVAILABLE,
            realized=None,
            unrealized=None,
            total=None,
            open_positions_count=_open_positions_count(positions),
            broker_check=reconcile_with_broker(None, positions),
            notes=[note],
        )

    async def realized_timelines(self, account_ids: Iterable[Any]) -> Dict[str, AccountRealizedTimeline]:
        """Per-account cumulative realized P&L today, for the 'Realized P&L
        Today' dashboard chart. Reuses the same cached order-book read as
        summarize_accounts (same Redis key), so calling both within one
        cache TTL window costs no extra broker request."""
        now = self._now()
        day = trading_day(now)
        ids = [str(account_id) for account_id in account_ids]
        timelines = await asyncio.gather(*(self._timeline_for(account_id, day) for account_id in ids))
        return dict(zip(ids, timelines))

    async def _timeline_for(self, account_id: str, day: date) -> AccountRealizedTimeline:
        client = self._client_provider(account_id)
        if client is None:
            return AccountRealizedTimeline(account_id, day, PNL_STATUS_UNAVAILABLE, [], ["NO_BROKER_CLIENT"])

        order_rows = await self._read_broker_rows(account_id, "orderbook", client.get_order_book_raw)
        if order_rows is None:
            return AccountRealizedTimeline(account_id, day, PNL_STATUS_UNAVAILABLE, [], ["BROKER_ORDER_BOOK_UNAVAILABLE"])

        fills, issues = parse_order_book_fills(order_rows)
        points, timeline_notes = compute_realized_timeline(fills, day)
        notes = list(dict.fromkeys(issues + timeline_notes))
        status = PNL_STATUS_OK if not notes else PNL_STATUS_PARTIAL
        return AccountRealizedTimeline(account_id, day, status, points, notes)

    # ── broker reads (Redis-cached) ─────────────────────────────────────
    async def _read_broker_rows(
        self,
        account_id: str,
        kind: str,
        fetch: Callable[[], Awaitable[Optional[List[Dict[str, Any]]]]],
    ) -> Optional[List[Dict[str, Any]]]:
        """Rows of one broker book, or None when it could not be read. A
        failure is never cached, and never turned into an empty book."""
        cache_key = f"{BROKER_CACHE_PREFIX}{kind}:{account_id}"
        cached = await self._cache_get(cache_key)
        if cached is not None:
            return cached
        try:
            rows = await asyncio.wait_for(fetch(), timeout=self._fetch_timeout)
        except Exception as exc:
            logger.warning(f"[PnL] Broker {kind} read failed for account {account_id}: {type(exc).__name__}: {exc}")
            return None
        if rows is None:
            return None
        await self._cache_set(cache_key, rows)
        return rows

    async def _cache_get(self, key: str) -> Optional[List[Dict[str, Any]]]:
        if self._redis is None:
            return None
        try:
            raw = await self._redis.get(key)
            if not raw:
                return None
            data = json.loads(raw)
            return data if isinstance(data, list) else None
        except Exception as exc:
            logger.warning(f"[PnL] Cache read failed for {key}: {exc}")
            return None

    async def _cache_set(self, key: str, rows: List[Dict[str, Any]]) -> None:
        if self._redis is None or self._cache_ttl <= 0:
            return
        try:
            await self._redis.set(key, json.dumps(rows), ex=self._cache_ttl)
        except Exception as exc:
            logger.warning(f"[PnL] Cache write failed for {key}: {exc}")

    # ── broker mismatch alert (at most one per account per trading day) ─
    async def _alert_mismatch_once(self, summary: AccountPnlSummary) -> None:
        try:
            account_uuid = uuid.UUID(summary.account_id)
        except ValueError:
            return
        day_iso = summary.trading_day.isoformat()

        if self._redis is not None:
            try:
                acquired = await self._redis.set(
                    f"{MISMATCH_ALERT_KEY_PREFIX}{summary.account_id}:{day_iso}",
                    "1",
                    ex=MISMATCH_ALERT_KEY_TTL_SECONDS,
                    nx=True,
                )
                if not acquired:
                    return
            except Exception as exc:
                logger.warning(f"[PnL] Mismatch alert lock unavailable, using DB check only: {exc}")

        check = summary.broker_check
        mismatched = [
            f"{name} Jettone {money_str(c.jettone)} vs Alice Blue {money_str(c.broker)}"
            for name, c in (("realized", check.realized), ("unrealized", check.unrealized), ("total", check.total))
            if c.status == CHECK_MISMATCH
        ]
        message = (
            f"Gross P&L for account {summary.account_id} on {day_iso} differs from Alice Blue beyond "
            f"tolerance ({'; '.join(mismatched)}). The card keeps showing Jettone's own calculation; "
            "verify the account's fills."
        )
        metadata = {
            "trading_day": day_iso,
            "tolerance": money_str(check.tolerance),
            "realized": _component_fields(check.realized),
            "unrealized": _component_fields(check.unrealized),
            "total": _component_fields(check.total),
        }

        try:
            async with _database.AsyncSessionLocal() as session:
                # Restart-safe backstop for the Redis lock.
                stmt = (
                    select(SystemEvent)
                    .where(
                        SystemEvent.event_type == MISMATCH_EVENT_TYPE,
                        SystemEvent.account_id == account_uuid,
                    )
                    .order_by(SystemEvent.created_at.desc())
                    .limit(20)
                )
                for event in (await session.execute(stmt)).scalars():
                    if (event.metadata_payload or {}).get("trading_day") == day_iso:
                        return
                await emit_system_event(
                    session,
                    event_type=MISMATCH_EVENT_TYPE,
                    severity="WARNING",
                    message=message,
                    account_id=account_uuid,
                    metadata_payload=metadata,
                )
        except Exception as exc:
            logger.error(f"[PnL] Failed to record P&L mismatch event for {summary.account_id}: {exc}")
