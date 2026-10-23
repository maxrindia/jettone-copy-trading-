"""
Shadow-job liveness (R7).

Why this exists
---------------
A report that holds events proves nothing about the job: they may be old, and an empty report may mean the job is down or that
the Master simply did not trade. Liveness is therefore its own signal, kept apart from the data events:

* IN MEMORY, for this process: when the job started, when each cycle began and ended, whether it succeeded, and a stable error
  code if it failed. The job runs inside the single-worker engine process, so this is accurate for "right now".
* ONE persisted row (`system_events`, type INSTRUMENT_SHADOW_LIVENESS), updated in place after every completed cycle, so the last
  success and last failure survive a restart. It is a single row that is overwritten, never appended to: its size is bounded no
  matter how long the job runs, and it needs no migration. Report queries select other event types, so it never mixes with data.

The status is DERIVED when asked, from the state, the row and the clock:

  DISABLED             the flag is off (any persisted history is still shown)
  ENABLED_NOT_STARTED  the flag is on but this process has no running job (startup never reached it, or it failed to start)
  STARTING             started, first cycle not yet due
  HEALTHY              the last cycle succeeded and is recent
  FAILING              the last cycle failed (a stable code says why); an older success may exist
  OVERDUE              no cycle has completed within the allowed time
  STALLED_IN_CYCLE     a cycle began and has not ended for too long
  STOPPED              the job was stopped in this process (shutdown)
  UNKNOWN              the evidence is missing or inconsistent (unreadable row, clock skew)

A cycle is a success only when neither the comparison step nor the crosswalk step reported an error. Codes are stable identifiers
(an exception class name or a fixed word): no message, path, credential or file content ever reaches this signal.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Optional

from sqlalchemy import select

from app.models.audit import EventSeverity, SystemEvent

EVENT_TYPE = "INSTRUMENT_SHADOW_LIVENESS"
OVERDUE_FACTOR = 3               # no completed cycle for this many intervals ...
GRACE_SECONDS = 60               # ... plus this, and the job is overdue
STALL_SECONDS = 600              # one cycle is bounded work (at most 500 orders); this long inside one is a stall
INITIAL_DELAY_SECONDS = 60       # mirrors shadow.INITIAL_DELAY_SECONDS (the first cycle runs after this delay)
CLOCK_SKEW_SECONDS = 120         # a recorded time this far in the future is not trustworthy
PAYLOAD_VERSION = 1


class Status(str, Enum):
    DISABLED = "DISABLED"
    ENABLED_NOT_STARTED = "ENABLED_NOT_STARTED"
    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    FAILING = "FAILING"
    OVERDUE = "OVERDUE"
    STALLED_IN_CYCLE = "STALLED_IN_CYCLE"
    STOPPED = "STOPPED"
    UNKNOWN = "UNKNOWN"


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _parse(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def error_code(raw: Optional[str], prefix: str = "") -> Optional[str]:
    """A stable code from a cycle's error string ('DEDUPE_UNAVAILABLE: RuntimeError' -> 'DEDUPE_UNAVAILABLE'). Nothing else survives."""
    if not raw:
        return None
    head = re.sub(r"[^A-Za-z0-9_]", "", str(raw).split(":", 1)[0].strip())[:60] or "ERROR"
    return f"{prefix}{head}"


@dataclass
class LivenessRegistry:
    """The job's own account of itself, for this process. Written only by the shadow job; read by the API."""
    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    interval_seconds: int = 0
    cycle_started_at: Optional[datetime] = None
    last_completed_at: Optional[datetime] = None
    last_success_at: Optional[datetime] = None
    last_failure_at: Optional[datetime] = None
    last_error_code: Optional[str] = None
    consecutive_failures: int = 0
    cycles_ok: int = 0
    cycles_failed: int = 0
    last_counts: Dict[str, int] = field(default_factory=dict)

    def reset(self) -> None:
        self.__init__()                                              # type: ignore[misc]

    def mark_started(self, now: datetime, interval_seconds: int) -> None:
        self.reset()
        self.started_at, self.interval_seconds = now, int(interval_seconds)

    def mark_stopped(self, now: datetime) -> None:
        if self.started_at is not None:
            self.stopped_at = now
            self.cycle_started_at = None

    def cycle_begin(self, now: datetime) -> None:
        self.cycle_started_at = now

    def cycle_end(self, now: datetime, *, code: Optional[str], counts: Optional[Dict[str, int]] = None) -> None:
        self.cycle_started_at = None
        self.last_completed_at = now
        if counts is not None:
            self.last_counts = {k: int(v) for k, v in counts.items()}
        if code is None:
            self.last_success_at, self.last_error_code, self.consecutive_failures = now, None, 0
            self.cycles_ok += 1
        else:
            self.last_failure_at, self.last_error_code = now, code
            self.consecutive_failures += 1
            self.cycles_failed += 1

    def payload(self, now: datetime) -> Dict[str, Any]:
        """What is persisted: timestamps, counters and a code. No path, message or credential."""
        return {
            "v": PAYLOAD_VERSION, "updated_at": _iso(now), "started_at": _iso(self.started_at), "stopped_at": _iso(self.stopped_at),
            "interval_seconds": self.interval_seconds, "last_completed_at": _iso(self.last_completed_at),
            "last_success_at": _iso(self.last_success_at), "last_failure_at": _iso(self.last_failure_at),
            "last_error_code": self.last_error_code, "consecutive_failures": self.consecutive_failures,
            "cycles_ok": self.cycles_ok, "cycles_failed": self.cycles_failed, "last_counts": dict(self.last_counts),
        }


registry = LivenessRegistry()


def cycle_counts(result: Any) -> Dict[str, int]:
    return {name: int(getattr(result, name, 0) or 0) for name in (
        "examined_orders", "instruments", "compared", "written", "duplicates", "crosswalk_written", "crosswalk_duplicates")}


def cycle_code(result: Any) -> Optional[str]:
    """None for a successful cycle. A comparison error wins; a crosswalk-step error is prefixed so the two are told apart."""
    return error_code(getattr(result, "error", None)) or error_code(getattr(result, "crosswalk_error", None), "CROSSWALK_")


# ── persistence: one row, updated in place ───────────────────────────────────────────────────────

def _session():
    from app.core import database
    return database.AsyncSessionLocal()


async def persist(payload: Dict[str, Any]) -> bool:
    """Overwrite the single liveness row (creating it once). Best effort: False on any failure, which never affects the job."""
    try:
        async with _session() as session:
            rows = (await session.execute(
                select(SystemEvent).where(SystemEvent.event_type == EVENT_TYPE).order_by(SystemEvent.created_at.asc())
            )).scalars().all()
            if not rows:
                session.add(SystemEvent(event_type=EVENT_TYPE, severity=EventSeverity.INFO,
                                        message="Instrument shadow job liveness (single row, updated in place).",
                                        metadata_payload=payload))
            else:
                rows[0].metadata_payload = dict(payload)
                for extra in rows[1:]:                               # never more than one row
                    await session.delete(extra)
            await session.commit()
        return True
    except Exception:
        return False


async def load_persisted(session) -> tuple:
    """(payload or None, readable). `readable` is False when the row could not be read, which is different from "no row"."""
    try:
        row = (await session.execute(
            select(SystemEvent.metadata_payload).where(SystemEvent.event_type == EVENT_TYPE).order_by(SystemEvent.created_at.asc()).limit(1)
        )).first()
    except Exception:
        return None, False
    return (dict(row[0]) if row and isinstance(row[0], dict) else None), True


# ── the derived status ───────────────────────────────────────────────────────────────────────────

def _view(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    p = payload or {}
    return {k: p.get(k) for k in ("started_at", "stopped_at", "last_completed_at", "last_success_at", "last_failure_at",
                                  "last_error_code", "consecutive_failures", "cycles_ok", "cycles_failed", "last_counts", "updated_at")}


def derive(
    now: datetime, *, enabled: bool, interval_seconds: int, memory: LivenessRegistry,
    persisted: Optional[Dict[str, Any]] = None, persisted_readable: bool = True,
) -> Dict[str, Any]:
    """Pure: the liveness status from this process's registry, the persisted row and the clock. Never infers health from data."""
    interval = max(1, int(interval_seconds))
    overdue_after = OVERDUE_FACTOR * interval + GRACE_SECONDS
    out: Dict[str, Any] = {
        "enabled": bool(enabled), "evaluated_at": _iso(now), "interval_seconds": interval,
        "overdue_after_seconds": overdue_after, "stall_after_seconds": STALL_SECONDS,
        "persisted": _view(persisted), "persisted_readable": persisted_readable,
    }

    def result(status: Status, reason: str, **extra: Any) -> Dict[str, Any]:
        base = {**out, "status": status.value, "reason": reason}
        base.update({
            "started_at": _iso(memory.started_at) if memory.started_at else None,
            "last_completed_at": _iso(memory.last_completed_at), "last_success_at": _iso(memory.last_success_at),
            "last_failure_at": _iso(memory.last_failure_at), "last_error_code": memory.last_error_code,
            "consecutive_failures": memory.consecutive_failures, "cycles_ok": memory.cycles_ok, "cycles_failed": memory.cycles_failed,
            "last_counts": dict(memory.last_counts),
        })
        base.update(extra)
        return base

    if not enabled:
        return result(Status.DISABLED, "INSTRUMENT_SHADOW_ENABLED_IS_FALSE")
    recorded = [t for t in (memory.started_at, memory.last_completed_at, memory.cycle_started_at,
                            _parse((persisted or {}).get("last_completed_at"))) if t is not None]
    if any(t > now + timedelta(seconds=CLOCK_SKEW_SECONDS) for t in recorded):
        return result(Status.UNKNOWN, "CLOCK_SKEW")
    if memory.started_at is None:
        if not persisted_readable:
            return result(Status.UNKNOWN, "PERSISTED_EVIDENCE_UNREADABLE")
        return result(Status.ENABLED_NOT_STARTED, "NO_RUNNING_JOB_IN_THIS_PROCESS")
    if memory.stopped_at is not None and memory.stopped_at >= memory.started_at:
        return result(Status.STOPPED, "JOB_STOPPED")
    if memory.cycle_started_at is not None and (now - memory.cycle_started_at).total_seconds() > STALL_SECONDS:
        return result(Status.STALLED_IN_CYCLE, "CYCLE_RUNNING_TOO_LONG", in_cycle_since=_iso(memory.cycle_started_at))
    if memory.last_completed_at is None:
        due = memory.started_at + timedelta(seconds=INITIAL_DELAY_SECONDS + STALL_SECONDS)
        if now <= due:
            return result(Status.STARTING, "FIRST_CYCLE_NOT_YET_DUE")
        return result(Status.OVERDUE, "NO_CYCLE_COMPLETED_AFTER_START")
    age = (now - memory.last_completed_at).total_seconds()
    if age > overdue_after:
        return result(Status.OVERDUE, "NO_CYCLE_COMPLETED_RECENTLY", seconds_since_last_cycle=int(age))
    if memory.last_error_code is not None:
        return result(Status.FAILING, "LAST_CYCLE_FAILED", seconds_since_last_cycle=int(age))
    return result(Status.HEALTHY, "LAST_CYCLE_SUCCEEDED", seconds_since_last_cycle=int(age))
