"""
Observe-only shadow comparison (R4c).

A feature-flagged background job that compares the instruments of persisted Master orders against the in-memory broker
catalogs (R4b) with the R4a normalizer and comparator, and records deduplicated INSTRUMENT_SHADOW_COMPARE events for the
read-only report. It influences nothing: it makes no broker call, places no order, touches no copy-pipeline code and
reads no store but the in-memory catalogs and the `orders` / `system_events` tables.

Honesty rules
-------------
* The Master side is normalised from (exchange, symbol) ONLY. Persisted orders carry no ISIN and none is invented from a
  symbol or from any broker catalog, so a Master identity is always SYMBOL_ONLY here and can never be a definitive MATCH.
  The report therefore shows catalog coverage and candidate counts, not parity.
* Evidence level is fixed at VERIFIED_DOCS so the documented Groww profile can be evaluated.
* Deduplication evidence comes from a bounded query (today's events only). If it cannot be established, NOTHING is
  written for the cycle.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from sqlalchemy import select

from app.core.config import settings
from app.models.audit import EventSeverity, SystemEvent
from app.models.strategies import GroupStrategy
from app.models.trades import Order, OrderSource
from app.services.brokers.capabilities import Verification
from app.services.brokers.profiles import build_profile
from app.services.instruments import crosswalk_evidence, liveness
from app.services.instruments.catalogs import InstrumentCatalog, default_catalogs
from app.services.instruments.comparator import compare_instrument
from app.services.instruments.model import BrokerView, Comparison
from app.services.instruments.normalizer import normalize_equity

logger = logging.getLogger("instrument_shadow")

EVENT_TYPE = "INSTRUMENT_SHADOW_COMPARE"
MIN_EVIDENCE = Verification.VERIFIED_DOCS
INITIAL_DELAY_SECONDS = 60
MIN_INTERVAL_SECONDS = 300
MAX_WINDOW_HOURS = 168
MAX_ORDERS_CEILING = 500
DEDUPE_KEY_LIMIT = 20000          # more same-day shadow events than this: dedupe cannot be shown, so write nothing


def _session():
    # Looked up at call time so tests (and the engine) always get the live session factory.
    from app.core import database
    return database.AsyncSessionLocal()


def interval_seconds() -> int:
    return max(MIN_INTERVAL_SECONDS, int(settings.INSTRUMENT_SHADOW_INTERVAL_SECONDS))


def window_hours() -> int:
    return max(1, min(MAX_WINDOW_HOURS, int(settings.INSTRUMENT_SHADOW_WINDOW_HOURS)))


def max_orders() -> int:
    return max(1, min(MAX_ORDERS_CEILING, int(settings.INSTRUMENT_SHADOW_MAX_ORDERS)))


def comparison_payload(exchange: str, symbol: str, master, view: BrokerView, result: Comparison) -> Dict[str, Any]:
    """The sanitised evidence stored on an event: identifiers and verdicts only - no token, credential or raw response."""
    return {
        "broker_id": result.broker_id,
        "exchange": str(exchange or "").strip().upper()[:20],
        "symbol": str(symbol or "").strip().upper()[:60],
        "master_key": result.master_key,
        "confidence": result.confidence.value,
        "verdict": result.verdict.value,
        "reason": result.reason,
        "candidate_count": len(view.candidates) if view.candidates is not None else None,
        "catalog_loaded": view.candidates is not None,
        "catalog_loaded_at": view.loaded_at.isoformat() if view.loaded_at else None,
        "diffs": [{"attribute": d.attribute, "master": str(d.master), "broker": str(d.broker)} for d in result.diffs],
        "unverified_attributes": list(result.unverified_attributes),
        "min_evidence": MIN_EVIDENCE.value,
    }


def dedupe_key(payload: Dict[str, Any], day: str) -> str:
    return "|".join([
        payload["broker_id"], f"{payload['exchange']}:{payload['symbol']}", payload["master_key"] or "-",
        payload["confidence"], payload["verdict"], payload["reason"], str(payload["candidate_count"]),
        "L" if payload["catalog_loaded"] else "N", payload["catalog_loaded_at"] or "-",
        ",".join(f"{d['attribute']}={d['master']}/{d['broker']}" for d in payload["diffs"]),
        ",".join(payload["unverified_attributes"]), payload["min_evidence"],
        day,
    ])


def identity_of(payload: Dict[str, Any]) -> Tuple[str, str, str]:
    """What one report row is about: a broker's view of one Master (exchange, symbol)."""
    return (payload["broker_id"], payload["exchange"], payload["symbol"])


def compare_pair(exchange: str, symbol: str, catalogs: Sequence[InstrumentCatalog], now: datetime) -> List[Dict[str, Any]]:
    """One payload per broker for a Master (exchange, symbol). Pure apart from reading the in-memory catalogs."""
    master = normalize_equity(exchange, symbol, None)           # never an ISIN: none is persisted, none is invented
    out = []
    for catalog in sorted(catalogs, key=lambda c: c.broker_id):
        view = catalog.view_for(master.instrument) if master.instrument else BrokerView(catalog.broker_id)
        result = compare_instrument(master, view, build_profile(catalog.broker_id), now=now, minimum=MIN_EVIDENCE)
        out.append(comparison_payload(exchange, symbol, master, view, result))
    return out


@dataclass
class ShadowResult:
    enabled: bool = True
    examined_orders: int = 0
    instruments: int = 0
    compared: int = 0
    written: int = 0
    duplicates: int = 0
    error: Optional[str] = None
    keys: List[str] = field(default_factory=list)
    crosswalk_written: int = 0
    crosswalk_duplicates: int = 0
    crosswalk_error: Optional[str] = None


async def _recent_master_rows(since: datetime, limit: int) -> List[Tuple[Any, Any, Any]]:
    """The newest `limit` persisted orders placed on the Master account of a non-deleted strategy (never a copy-engine
    order, never an order on a Child account) inside the window: (exchange, symbol, instrument_token), newest first."""
    masters = select(GroupStrategy.master_account_id).where(
        GroupStrategy.deleted_at.is_(None), GroupStrategy.master_account_id.is_not(None)
    )
    async with _session() as session:
        return list((await session.execute(
            select(Order.exchange, Order.symbol, Order.instrument_token)
            .where(Order.account_id.in_(masters), Order.source != OrderSource.SYSTEM, Order.created_at >= since)
            .order_by(Order.created_at.desc())
            .limit(limit)
        )).all())


async def _recent_master_pairs(since: datetime, limit: int) -> Tuple[int, List[Tuple[str, str]]]:
    """The recent Master orders reduced to distinct (exchange, symbol) pairs, newest first."""
    rows = await _recent_master_rows(since, limit)
    seen: Set[Tuple[str, str]] = set()
    pairs: List[Tuple[str, str]] = []
    for exchange, symbol, _token in rows:
        pair = (str(exchange or "").strip().upper(), str(symbol or "").strip().upper())
        if pair not in seen:
            seen.add(pair)
            pairs.append(pair)
    return len(rows), pairs


def _distinct_subjects(rows: Sequence[Tuple[Any, Any, Any]]) -> List[Tuple[str, str, Optional[str]]]:
    """Distinct (exchange, symbol, token) triples, newest first. A missing token stays None: it is never filled in."""
    seen: Set[Tuple[str, str, Optional[str]]] = set()
    out: List[Tuple[str, str, Optional[str]]] = []
    for exchange, symbol, token in rows:
        triple = (str(exchange or "").strip().upper(), str(symbol or "").strip().upper(),
                  str(token).strip() if token not in (None, "") else None)
        if triple not in seen:
            seen.add(triple)
            out.append(triple)
    return out


async def _todays_latest_keys(session, day_start: datetime) -> Dict[Tuple[str, str, str], str]:
    """The most recent dedupe key per (broker, exchange, symbol) among today's events. A result is written when it differs
    from the LATEST one, not merely when it is new today: a state that reverts to an earlier one must be recorded again,
    or the report would keep showing the intermediate state. Bounded to today's events of this type."""
    rows = (await session.execute(
        select(SystemEvent.created_at, SystemEvent.metadata_payload)
        .where(SystemEvent.event_type == EVENT_TYPE, SystemEvent.created_at >= day_start)
        .order_by(SystemEvent.created_at.asc())
        .limit(DEDUPE_KEY_LIMIT + 1)
    )).all()
    if len(rows) > DEDUPE_KEY_LIMIT:
        raise RuntimeError("too many same-day shadow events to establish deduplication")
    latest: Dict[Tuple[str, str, str], str] = {}
    for _, payload in rows:                                  # ascending: a later event overwrites an earlier one
        p = payload or {}
        if p.get("dedupe_key") and p.get("broker_id"):
            latest[identity_of(p)] = p["dedupe_key"]
    return latest


async def _todays_latest_crosswalk_keys(session, day_start: datetime) -> Dict[Tuple[str, str, str], str]:
    """Like _todays_latest_keys, for INSTRUMENT_SHADOW_CROSSWALK events: the latest dedupe key per (exchange, symbol, token)."""
    rows = (await session.execute(
        select(SystemEvent.metadata_payload)
        .where(SystemEvent.event_type == crosswalk_evidence.EVENT_TYPE, SystemEvent.created_at >= day_start)
        .order_by(SystemEvent.created_at.asc())
        .limit(DEDUPE_KEY_LIMIT + 1)
    )).all()
    if len(rows) > DEDUPE_KEY_LIMIT:
        raise RuntimeError("too many same-day crosswalk events to establish deduplication")
    latest: Dict[Tuple[str, str, str], str] = {}
    for (payload,) in rows:
        p = payload or {}
        if p.get("dedupe_key") and p.get("exchange") and p.get("symbol"):
            latest[crosswalk_evidence.identity_of(p)] = p["dedupe_key"]
    return latest


async def _record_crosswalk(now: datetime, result: ShadowResult, day: str, day_start: datetime) -> None:
    """R4i: record, per Master (exchange, symbol, token), what the configured local NSE snapshot says. Shadow-only and
    separate from the catalog comparison: a failure here is reported in `crosswalk_error` and cannot undo or block it."""
    try:
        rows = await _recent_master_rows(now - timedelta(hours=window_hours()), max_orders())
        crosswalk, error = await asyncio.to_thread(crosswalk_evidence.get_crosswalk)       # local file only, cached
        payloads = [crosswalk_evidence.evaluate(ex, sym, tok, crosswalk, error, now) for ex, sym, tok in _distinct_subjects(rows)]
        async with _session() as session:
            try:
                latest = await _todays_latest_crosswalk_keys(session, day_start)
            except Exception as exc:
                result.crosswalk_error = f"DEDUPE_UNAVAILABLE: {type(exc).__name__}"
                logger.error(f"[InstrumentShadow] crosswalk deduplication unavailable; nothing written: {exc}")
                return
            for payload in payloads:
                key = crosswalk_evidence.dedupe_key(payload, day)
                ident = crosswalk_evidence.identity_of(payload)
                if latest.get(ident) == key:
                    result.crosswalk_duplicates += 1
                    continue
                latest[ident] = key
                session.add(SystemEvent(
                    event_type=crosswalk_evidence.EVENT_TYPE, severity=EventSeverity.INFO,
                    message=f"NSE:{payload['symbol']} token {payload['master_token'] or '-'} -> {payload['state']}"
                            f" ({payload['reason']}; {payload['link_status']})",
                    metadata_payload={**payload, "dedupe_key": key, "day": day},
                    created_at=now,
                ))
                result.crosswalk_written += 1
            if result.crosswalk_written:
                await session.commit()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        result.crosswalk_error = f"{type(exc).__name__}"
        logger.error(f"[InstrumentShadow] crosswalk step failed: {exc}", exc_info=True)


async def run_once(
    now: Optional[datetime] = None,
    *,
    catalogs: Optional[Sequence[InstrumentCatalog]] = None,
) -> ShadowResult:
    """One comparison cycle. A no-op when the flag is off. Never raises; a failure is returned in `error`."""
    if not settings.INSTRUMENT_SHADOW_ENABLED:
        return ShadowResult(enabled=False)
    now = now or datetime.now(timezone.utc)
    result = ShadowResult()
    try:
        catalogs = list(catalogs) if catalogs is not None else list(default_catalogs())
        examined, pairs = await _recent_master_pairs(now - timedelta(hours=window_hours()), max_orders())
        result.examined_orders, result.instruments = examined, len(pairs)
        day = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        day_start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        payloads: List[Dict[str, Any]] = []
        for exchange, symbol in pairs:
            payloads.extend(compare_pair(exchange, symbol, catalogs, now))
        result.compared = len(payloads)
        async with _session() as session:
            try:
                latest = await _todays_latest_keys(session, day_start)
            except Exception as exc:
                result.error = f"DEDUPE_UNAVAILABLE: {type(exc).__name__}"
                logger.error(f"[InstrumentShadow] deduplication evidence unavailable; nothing written this cycle: {exc}")
                return result
            for payload in payloads:
                key = dedupe_key(payload, day)
                if latest.get(identity_of(payload)) == key:
                    result.duplicates += 1
                    continue
                latest[identity_of(payload)] = key
                session.add(SystemEvent(
                    event_type=EVENT_TYPE, severity=EventSeverity.INFO,
                    message=f"{payload['broker_id']} {payload['exchange']}:{payload['symbol']} -> {payload['verdict']}"
                            f" ({payload['reason']})",
                    metadata_payload={**payload, "dedupe_key": key, "day": day},
                    created_at=now,
                ))
                result.written += 1
                result.keys.append(key)
            if result.written:
                await session.commit()
        if crosswalk_evidence.is_configured():                  # R4i: nothing at all happens while no file is configured
            await _record_crosswalk(now, result, day, day_start)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        result.error = f"{type(exc).__name__}"
        logger.error(f"[InstrumentShadow] cycle failed: {exc}", exc_info=True)
    return result


class ShadowJob:
    """Owns the one background task. Nothing is created unless INSTRUMENT_SHADOW_ENABLED is true."""

    def __init__(self, *, initial_delay: float = INITIAL_DELAY_SECONDS, interval: Optional[float] = None,
                 catalogs: Optional[Sequence[InstrumentCatalog]] = None) -> None:
        self._initial_delay = initial_delay
        self._interval = interval
        self._catalogs = catalogs
        self._task: Optional[asyncio.Task] = None
        self._persist_task: Optional[asyncio.Task] = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not settings.INSTRUMENT_SHADOW_ENABLED or self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="instrument-shadow")
        interval = self._interval if self._interval is not None else interval_seconds()
        liveness.registry.mark_started(datetime.now(timezone.utc), int(interval))
        logger.info("[InstrumentShadow] enabled; first cycle after the start-up delay (observe-only).")

    def _schedule_persist(self) -> None:
        """At most one persistence attempt in flight, so a slow database cannot pile up tasks or hold back a cycle."""
        if self._persist_task is None or self._persist_task.done():
            self._persist_task = asyncio.create_task(self._persist_liveness(), name="instrument-shadow-liveness")

    async def _persist_liveness(self, timeout: float = 10.0) -> None:
        """Best effort and bounded: the single liveness row is evidence, never a dependency of the job or of shutdown."""
        now = datetime.now(timezone.utc)
        try:
            saved = await asyncio.wait_for(liveness.persist(liveness.registry.payload(now)), timeout=timeout)
        except asyncio.TimeoutError:
            saved = False
        if not saved:
            logger.warning("[InstrumentShadow] liveness evidence could not be persisted.")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        liveness.registry.mark_stopped(datetime.now(timezone.utc))
        pending, self._persist_task = self._persist_task, None
        if pending is not None and not pending.done():
            pending.cancel()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:                       # a crashed loop must not break shutdown
            logger.error(f"[InstrumentShadow] stopped after an error: {exc}")
        await self._persist_liveness(timeout=1.0)

    async def _loop(self) -> None:
        await asyncio.sleep(self._initial_delay)
        while True:
            liveness.registry.cycle_begin(datetime.now(timezone.utc))
            try:
                outcome = await run_once(catalogs=self._catalogs)
                if outcome.error:
                    logger.warning(f"[InstrumentShadow] cycle ended with {outcome.error}")
                liveness.registry.cycle_end(datetime.now(timezone.utc), code=liveness.cycle_code(outcome),
                                            counts=liveness.cycle_counts(outcome))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[InstrumentShadow] cycle failed: {exc}", exc_info=True)
                liveness.registry.cycle_end(datetime.now(timezone.utc), code=liveness.error_code(type(exc).__name__, "CYCLE_"))
            self._schedule_persist()                   # never delays the next cycle
            await asyncio.sleep(self._interval if self._interval is not None else interval_seconds())
