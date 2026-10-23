"""
R7: shadow-job liveness. Local, in-memory database only - no broker, Redis, network or production access. Liveness is its own signal:
it is never inferred from report contents, a failed cycle never looks successful, and its storage is a single row.
"""
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.core.config import Settings, settings
from app.models.audit import SystemEvent
from app.services.instruments import liveness as lv
from app.services.instruments import shadow
from conftest import TestingSessionLocal
from test_crosswalk_evidence import cats, master_order, on  # noqa: F401  (on is a fixture)
from test_instrument_api import TOKEN, client  # noqa: F401  (client is a fixture)

T0 = datetime(2026, 10, 22, 6, 0, tzinfo=timezone.utc)
INTERVAL = 900


@pytest.fixture(autouse=True)
def fresh_registry():
    lv.registry.reset()
    yield
    lv.registry.reset()


def reg(**kw):
    r = lv.LivenessRegistry()
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def derive(now, *, memory, enabled=True, persisted=None, readable=True, interval=INTERVAL):
    return lv.derive(now, enabled=enabled, interval_seconds=interval, memory=memory, persisted=persisted, persisted_readable=readable)


def running(**kw):
    base = dict(started_at=T0, interval_seconds=INTERVAL)
    base.update(kw)
    return reg(**base)


# ── the derived status ───────────────────────────────────────────────────────────────────

def test_disabled_by_configuration_even_with_old_evidence():
    out = derive(T0, memory=running(last_success_at=T0, last_completed_at=T0), enabled=False, persisted={"last_success_at": "x"})
    assert out["status"] == "DISABLED" and out["reason"] == "INSTRUMENT_SHADOW_ENABLED_IS_FALSE" and out["persisted"]["last_success_at"] == "x"


def test_enabled_but_no_job_in_this_process_is_not_started():
    out = derive(T0, memory=reg(), persisted={"last_success_at": T0.isoformat(), "last_completed_at": T0.isoformat()})
    assert out["status"] == "ENABLED_NOT_STARTED" and out["last_success_at"] is None            # history is shown only as persisted
    assert out["persisted"]["last_success_at"] == T0.isoformat()


def test_unreadable_evidence_with_no_running_job_is_unknown_not_healthy():
    out = derive(T0, memory=reg(), persisted=None, readable=False)
    assert out["status"] == "UNKNOWN" and out["reason"] == "PERSISTED_EVIDENCE_UNREADABLE"


def test_starting_then_overdue_when_the_first_cycle_never_completes():
    m = running()
    assert derive(T0 + timedelta(seconds=30), memory=m)["status"] == "STARTING"
    edge = lv.INITIAL_DELAY_SECONDS + lv.STALL_SECONDS
    assert derive(T0 + timedelta(seconds=edge), memory=m)["status"] == "STARTING"
    late = derive(T0 + timedelta(seconds=edge + 1), memory=m)
    assert late["status"] == "OVERDUE" and late["reason"] == "NO_CYCLE_COMPLETED_AFTER_START"


def test_healthy_after_a_successful_cycle_and_overdue_exactly_past_the_limit():
    done = T0 + timedelta(seconds=100)
    m = running(last_completed_at=done, last_success_at=done)
    limit = lv.OVERDUE_FACTOR * INTERVAL + lv.GRACE_SECONDS
    assert derive(done + timedelta(seconds=10), memory=m)["status"] == "HEALTHY"
    assert derive(done + timedelta(seconds=limit), memory=m)["status"] == "HEALTHY"               # the boundary is inclusive
    over = derive(done + timedelta(seconds=limit + 1), memory=m)
    assert over["status"] == "OVERDUE" and over["overdue_after_seconds"] == limit


def test_a_failed_cycle_is_failing_never_healthy_and_keeps_the_older_success():
    ok, bad = T0 + timedelta(seconds=100), T0 + timedelta(seconds=1000)
    m = running(last_completed_at=bad, last_success_at=ok, last_failure_at=bad, last_error_code="DEDUPE_UNAVAILABLE",
                consecutive_failures=2, cycles_ok=1, cycles_failed=2)
    out = derive(bad + timedelta(seconds=5), memory=m)
    assert out["status"] == "FAILING" and out["last_error_code"] == "DEDUPE_UNAVAILABLE" and out["consecutive_failures"] == 2
    assert out["last_success_at"] == ok.isoformat()


def test_a_cycle_that_never_ends_is_stalled():
    m = running(last_completed_at=T0, last_success_at=T0, cycle_started_at=T0 + timedelta(seconds=100))
    assert derive(T0 + timedelta(seconds=100 + lv.STALL_SECONDS), memory=m)["status"] == "HEALTHY"
    stalled = derive(T0 + timedelta(seconds=100 + lv.STALL_SECONDS + 1), memory=m)
    assert stalled["status"] == "STALLED_IN_CYCLE" and stalled["in_cycle_since"]


def test_stopped_and_clock_skew():
    assert derive(T0, memory=running(stopped_at=T0 + timedelta(seconds=1)))["status"] == "STOPPED"
    future = running(last_completed_at=T0 + timedelta(seconds=lv.CLOCK_SKEW_SECONDS + 5), last_success_at=T0)
    assert derive(T0, memory=future)["status"] == "UNKNOWN"


# ── the registry and codes ───────────────────────────────────────────────────────────────

def test_cycle_outcomes_update_counters_and_a_failure_is_never_a_success():
    r = running()
    r.cycle_begin(T0)
    r.cycle_end(T0 + timedelta(seconds=1), code=None, counts={"written": 3})
    r.cycle_begin(T0 + timedelta(seconds=2))
    r.cycle_end(T0 + timedelta(seconds=3), code="BOOM")
    assert (r.cycles_ok, r.cycles_failed, r.consecutive_failures) == (1, 1, 1)
    assert r.last_success_at == T0 + timedelta(seconds=1) and r.last_failure_at == T0 + timedelta(seconds=3) and r.cycle_started_at is None
    r.cycle_end(T0 + timedelta(seconds=9), code=None)
    assert r.consecutive_failures == 0 and r.last_error_code is None


def test_codes_are_stable_identifiers_with_no_message_or_path():
    assert lv.error_code("DEDUPE_UNAVAILABLE: RuntimeError") == "DEDUPE_UNAVAILABLE"
    assert lv.error_code("RuntimeError") == "RuntimeError"
    assert "secret" not in (lv.error_code("OSError C:\\secret\\path: x") or "") and "\\" not in (lv.error_code("a\\b: c") or "")
    assert lv.error_code("CrosswalkErr", "CROSSWALK_") == "CROSSWALK_CrosswalkErr" and lv.error_code(None) is None and lv.error_code("") is None
    ok = SimpleNamespace(error=None, crosswalk_error=None)
    assert lv.cycle_code(ok) is None
    assert lv.cycle_code(SimpleNamespace(error="DEDUPE_UNAVAILABLE: X", crosswalk_error=None)) == "DEDUPE_UNAVAILABLE"
    assert lv.cycle_code(SimpleNamespace(error=None, crosswalk_error="DEDUPE_UNAVAILABLE: X")) == "CROSSWALK_DEDUPE_UNAVAILABLE"


def test_the_persisted_payload_is_timestamps_counters_and_codes_only():
    r = running(last_completed_at=T0, last_success_at=T0, last_error_code="BOOM")
    payload = r.payload(T0)
    assert set(payload) == {"v", "updated_at", "started_at", "stopped_at", "interval_seconds", "last_completed_at", "last_success_at",
                            "last_failure_at", "last_error_code", "consecutive_failures", "cycles_ok", "cycles_failed", "last_counts"}
    assert not any(w in json.dumps(payload).lower() for w in ("path", "token", "secret", "password"))


# ── persistence: one row ─────────────────────────────────────────────────────────────────

async def liveness_rows():
    async with TestingSessionLocal() as s:
        return (await s.execute(select(SystemEvent).where(SystemEvent.event_type == lv.EVENT_TYPE))).scalars().all()


@pytest.mark.asyncio
async def test_storage_is_a_single_row_however_many_cycles_run(db_session):
    r = running()
    for i in range(40):
        r.cycle_end(T0 + timedelta(seconds=i), code=None if i % 2 == 0 else "BOOM")
        assert await lv.persist(r.payload(T0 + timedelta(seconds=i))) is True
    rows = await liveness_rows()
    assert len(rows) == 1 and rows[0].metadata_payload["cycles_ok"] == 20 and rows[0].metadata_payload["cycles_failed"] == 20


@pytest.mark.asyncio
async def test_extra_rows_are_removed_so_the_row_count_stays_bounded(db_session):
    async with TestingSessionLocal() as s:
        for i in range(3):
            s.add(SystemEvent(event_type=lv.EVENT_TYPE, message="x", metadata_payload={"v": 1, "i": i}))
        await s.commit()
    assert await lv.persist(running().payload(T0)) is True
    assert len(await liveness_rows()) == 1


@pytest.mark.asyncio
async def test_a_persistence_failure_is_reported_not_raised(db_session, monkeypatch):
    def boom():
        raise RuntimeError("database down")
    monkeypatch.setattr(lv, "_session", boom)
    assert await lv.persist(running().payload(T0)) is False
    payload, readable = await lv.load_persisted(SimpleNamespace(execute=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))))
    assert payload is None and readable is False


@pytest.mark.asyncio
async def test_no_row_is_readable_and_empty_not_unreadable(db_session):
    async with TestingSessionLocal() as s:
        assert await lv.load_persisted(s) == (None, True)


# ── the job ──────────────────────────────────────────────────────────────────────────────

async def wait_for(predicate, timeout=3.0):
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.mark.asyncio
async def test_a_running_job_reports_healthy_with_persisted_evidence(db_session, sample_accounts, sample_strategy, on):
    await master_order(db_session, sample_accounts["master"], "SYNALPHA", "9000003")
    job = shadow.ShadowJob(initial_delay=0, interval=0.05, catalogs=cats())
    await job.start()
    assert await wait_for(lambda: lv.registry.cycles_ok >= 2)
    out = derive(datetime.now(timezone.utc), memory=lv.registry, interval=900)
    assert out["status"] == "HEALTHY" and out["cycles_ok"] >= 2 and out["last_error_code"] is None
    await job.stop()
    # The in-memory test database gives each concurrent connection its own empty database, so the job's background write cannot be
    # relied on here; persisting the same registry sequentially checks what the job would store.
    await job._persist_liveness()
    rows = await liveness_rows()
    assert len(rows) == 1 and rows[0].metadata_payload["last_success_at"] and rows[0].metadata_payload["cycles_ok"] >= 2
    assert derive(datetime.now(timezone.utc), memory=lv.registry)["status"] == "STOPPED"


@pytest.mark.asyncio
async def test_a_failing_cycle_never_looks_successful(db_session, on, monkeypatch):
    async def failing(*a, **k):
        return shadow.ShadowResult(error="DEDUPE_UNAVAILABLE: RuntimeError")
    monkeypatch.setattr(shadow, "run_once", failing)
    job = shadow.ShadowJob(initial_delay=0, interval=0.05)
    await job.start()
    assert await wait_for(lambda: lv.registry.cycles_failed >= 2)
    out = derive(datetime.now(timezone.utc), memory=lv.registry)
    assert out["status"] == "FAILING" and out["last_error_code"] == "DEDUPE_UNAVAILABLE" and out["last_success_at"] is None
    assert out["cycles_ok"] == 0
    await job.stop()


@pytest.mark.asyncio
async def test_a_crosswalk_step_failure_is_a_failed_cycle_with_its_own_prefix(db_session, on, monkeypatch):
    async def partly(*a, **k):
        return shadow.ShadowResult(crosswalk_error="DEDUPE_UNAVAILABLE: RuntimeError")
    monkeypatch.setattr(shadow, "run_once", partly)
    job = shadow.ShadowJob(initial_delay=0, interval=0.05)
    await job.start()
    assert await wait_for(lambda: lv.registry.cycles_failed >= 1)
    assert lv.registry.last_error_code == "CROSSWALK_DEDUPE_UNAVAILABLE" and lv.registry.cycles_ok == 0
    await job.stop()


@pytest.mark.asyncio
async def test_an_exception_in_a_cycle_is_a_failure_and_the_next_cycle_runs(db_session, on, monkeypatch):
    calls = []

    async def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("secret detail C:\\private\\path")
        return shadow.ShadowResult()
    monkeypatch.setattr(shadow, "run_once", flaky)
    job = shadow.ShadowJob(initial_delay=0, interval=0.05)
    await job.start()
    assert await wait_for(lambda: lv.registry.cycles_ok >= 1)
    assert lv.registry.cycles_failed == 1 and lv.registry.last_error_code is None            # recovered
    assert "private" not in json.dumps(lv.registry.payload(T0))
    await job.stop()


@pytest.mark.asyncio
async def test_a_slow_or_hung_database_never_delays_cycles_or_shutdown(on, monkeypatch):
    in_flight, peak = [0], [0]

    async def hung(payload):
        in_flight[0] += 1
        peak[0] = max(peak[0], in_flight[0])
        try:
            await asyncio.sleep(3600)
        finally:
            in_flight[0] -= 1
        return True

    async def stub(*a, **k):
        return shadow.ShadowResult()

    monkeypatch.setattr(lv, "persist", hung)
    monkeypatch.setattr(shadow, "run_once", stub)
    job = shadow.ShadowJob(initial_delay=0, interval=0.02)
    await job.start()
    assert await wait_for(lambda: lv.registry.cycles_ok >= 5)                # cycles keep running while persistence hangs
    assert peak[0] == 1                                                      # at most one attempt in flight: no pile-up
    await asyncio.wait_for(job.stop(), timeout=3)
    assert job.running is False


@pytest.mark.asyncio
async def test_a_disabled_job_creates_no_state_and_no_row(db_session, monkeypatch):
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_ENABLED", False)
    job = shadow.ShadowJob(initial_delay=0, interval=0.05)
    await job.start()
    await asyncio.sleep(0.1)
    assert lv.registry.started_at is None and job.running is False and await liveness_rows() == []


# ── the API ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_liveness_endpoint_states_and_no_secret_leakage(client, db_session, monkeypatch, tmp_path):
    body = (await client.get("/api/v1/instruments/shadow-liveness", headers=TOKEN)).json()["data"]
    assert body["status"] == "DISABLED" and body["crosswalk_snapshot"]["configured"] is False

    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_ENABLED", True)
    body = (await client.get("/api/v1/instruments/shadow-liveness", headers=TOKEN)).json()["data"]
    assert body["status"] == "ENABLED_NOT_STARTED"                           # enabled, nothing running: not "healthy"

    from test_crosswalk_evidence import make_file
    from test_snapshot_rotation import D
    make_file(tmp_path, snapshot=D)
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", str(tmp_path))
    text = json.dumps((await client.get("/api/v1/instruments/shadow-liveness", headers=TOKEN)).json())
    assert str(tmp_path) not in text and "token" not in text.lower().replace("master_token_link_unverified", "")
    from app.services.instruments import crosswalk_evidence as ce
    ce.clear_cache()


@pytest.mark.asyncio
async def test_an_empty_report_says_nothing_about_the_job_and_the_report_carries_its_own_liveness(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_ENABLED", True)
    data = (await client.get("/api/v1/instruments/shadow-report", headers=TOKEN)).json()["data"]
    assert data["event_count"] == 0 and data["liveness"]["status"] == "ENABLED_NOT_STARTED"
    assert data["liveness"]["status"] != "HEALTHY"
    rows = await liveness_rows()
    assert rows == []


@pytest.mark.asyncio
async def test_the_liveness_row_never_appears_in_report_data(client, db_session, monkeypatch):
    assert await lv.persist(running(last_completed_at=T0, last_success_at=T0).payload(T0)) is True
    data = (await client.get("/api/v1/instruments/shadow-report?hours=168", headers=TOKEN)).json()["data"]
    assert data["event_count"] == 0 and data["crosswalk"]["events_read"] == 0


def test_flags_remain_off_by_default():
    s = Settings(_env_file=None)
    assert s.INSTRUMENT_SHADOW_ENABLED is False and s.CANONICAL_MAPPING_ENFORCED is False and s.ENGINE_LIVE_TRADING_ENABLED is False


def test_liveness_module_is_isolated_from_the_trading_path_and_the_network():
    import ast
    from pathlib import Path
    app = Path(__file__).resolve().parent.parent / "app"
    tree = ast.parse((app / "services" / "instruments" / "liveness.py").read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 0}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not [m for m in imported if m and any(w in m for w in ("copy_engine", "alice_blue", "groww", "angel_one", "brokers", "redis", "httpx", "socket"))]
    for rel in ("services/copy_engine/pipeline.py", "services/brokers/pairs.py"):
        text = (app / rel).read_text(encoding="utf-8").lower()
        assert "liveness" not in text and "crosswalk" not in text, rel
