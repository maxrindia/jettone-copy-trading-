"""
R4c: the observe-only shadow job. Fixtures and mocks only - no broker, Redis or network. The job reads persisted Master
orders and the in-memory catalogs and writes only INSTRUMENT_SHADOW_COMPARE events.
"""
import ast
import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.config import Settings, settings
from app.models.audit import SystemEvent
from app.models.strategies import GroupStrategy
from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services.brokers.capabilities import Verification
from app.services.instruments import catalogs as catalogs_module
from app.services.instruments import report as report_module
from app.services.instruments import shadow
from app.services.instruments.catalogs import AliceCatalog, AngelCatalog, GrowwCatalog
from app.services.angel_one.instruments import AngelInstrumentStore
from conftest import TestingSessionLocal
from test_instrument_catalogs import REL, INFY, groww_row, groww_store

NOW = datetime(2026, 10, 22, 6, 0, tzinfo=timezone.utc)      # the Groww fixture store loaded two hours before this


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_ENABLED", True)
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_MAX_ORDERS", 500)
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_WINDOW_HOURS", 24)


def cats(groww=None, angel=None):
    return [AliceCatalog(), AngelCatalog(angel or AngelInstrumentStore()), GrowwCatalog(groww or groww_store(groww_row()))]


async def master_order(db, account, symbol="RELIANCE", exchange="NSE", source=OrderSource.BROKER_EXTERNAL, age_hours=1.0, now=NOW):
    row = Order(id=uuid.uuid4(), internal_order_id=f"EXT_{uuid.uuid4().hex[:10]}", broker_order_id=uuid.uuid4().hex[:8],
                account_id=account.id, source=source, exchange=exchange, symbol=symbol, side=OrderSide.BUY,
                order_type="LIMIT", product_type="MIS", quantity=10, filled_quantity=0, pending_quantity=10,
                price=Decimal("100"), status=OrderStatus.OPEN, created_at=now - timedelta(hours=age_hours))
    db.add(row)
    await db.commit()
    return row


async def events(event_type=shadow.EVENT_TYPE):
    async with TestingSessionLocal() as s:
        return (await s.execute(select(SystemEvent).where(SystemEvent.event_type == event_type))).scalars().all()


def by(events_, broker, symbol):
    return [e.metadata_payload for e in events_ if e.metadata_payload["broker_id"] == broker and e.metadata_payload["symbol"] == symbol]


# ── flag, lifecycle ──────────────────────────────────────────────────────────────────────

def test_configuration_defaults_and_bounds(monkeypatch):
    s = Settings(_env_file=None)
    assert (s.INSTRUMENT_SHADOW_ENABLED, s.INSTRUMENT_SHADOW_INTERVAL_SECONDS, s.INSTRUMENT_SHADOW_WINDOW_HOURS,
            s.INSTRUMENT_SHADOW_MAX_ORDERS) == (False, 900, 24, 500)
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_INTERVAL_SECONDS", 5)
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_WINDOW_HOURS", 100000)
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_MAX_ORDERS", 10 ** 9)
    assert (shadow.interval_seconds(), shadow.window_hours(), shadow.max_orders()) == (300, 168, 500)
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_WINDOW_HOURS", 0)
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_MAX_ORDERS", 0)
    assert (shadow.window_hours(), shadow.max_orders()) == (1, 1)


@pytest.mark.asyncio
async def test_flag_off_creates_no_task_and_writes_no_events(db_session, sample_accounts, sample_strategy, monkeypatch):
    assert settings.INSTRUMENT_SHADOW_ENABLED is False
    await master_order(db_session, sample_accounts["master"])

    async def boom(*a, **k):
        raise AssertionError("the database must not be read when the flag is off")

    monkeypatch.setattr(shadow, "_recent_master_pairs", boom)
    result = await shadow.run_once(NOW, catalogs=cats())
    assert result.enabled is False and result.written == 0 and result.error is None
    job = shadow.ShadowJob(initial_delay=0.01, interval=0.01)
    await job.start()
    assert job._task is None and job.running is False
    await asyncio.sleep(0.05)
    await job.stop()
    assert await events() == []


@pytest.mark.asyncio
async def test_job_runs_in_the_background_and_shuts_down_cleanly(on, monkeypatch):
    # run_once is stubbed: this test is about the task lifecycle, and a real background query would share the test
    # database's single connection with the test itself. run_once is exercised against the database elsewhere.
    calls = []
    catalogs = cats()

    async def stub(now=None, *, catalogs=None):
        calls.append(catalogs)
        return shadow.ShadowResult()

    monkeypatch.setattr(shadow, "run_once", stub)
    job = shadow.ShadowJob(initial_delay=0.01, interval=0.02, catalogs=catalogs)
    await job.start()
    assert job.running
    await job.start()                                       # a second start is a no-op
    for _ in range(100):
        if len(calls) >= 3:
            break
        await asyncio.sleep(0.02)
    assert len(calls) >= 3 and all(c is catalogs for c in calls)
    await job.stop()
    assert job.running is False and job._task is None
    seen = len(calls)
    await asyncio.sleep(0.1)
    assert len(calls) == seen                               # nothing runs after stop
    await job.stop()                                        # idempotent


@pytest.mark.asyncio
async def test_stop_during_the_start_up_delay_cancels_without_a_cycle(on, monkeypatch):
    calls = []

    async def stub(*a, **k):
        calls.append(1)
        return shadow.ShadowResult()

    monkeypatch.setattr(shadow, "run_once", stub)
    job = shadow.ShadowJob(initial_delay=60, catalogs=cats())
    await job.start()
    assert job.running
    await asyncio.wait_for(job.stop(), timeout=2)
    assert job.running is False and calls == []


@pytest.mark.asyncio
async def test_a_failing_cycle_is_survived_and_the_next_one_runs(on, monkeypatch):
    calls = []

    async def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return shadow.ShadowResult()

    monkeypatch.setattr(shadow, "run_once", flaky)
    job = shadow.ShadowJob(initial_delay=0, interval=0.01)
    await job.start()
    for _ in range(100):
        if len(calls) >= 3:
            break
        await asyncio.sleep(0.02)
    assert len(calls) >= 3 and job.running
    await job.stop()


@pytest.mark.asyncio
async def test_run_once_never_raises(db_session, on, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(shadow, "_recent_master_pairs", boom)
    result = await shadow.run_once(NOW, catalogs=cats())
    assert result.error == "RuntimeError" and result.written == 0


# ── which orders, bounded ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_only_master_orders_inside_the_window_and_the_bound_are_examined(db_session, sample_accounts, sample_strategy, on, monkeypatch):
    m, child = sample_accounts["master"], sample_accounts["follower1"]
    await master_order(db_session, m, "RELIANCE", age_hours=1)
    await master_order(db_session, m, "RELIANCE", age_hours=2)                        # duplicate pair
    await master_order(db_session, m, "INFY", source=OrderSource.INTERNAL_UI, age_hours=3)
    await master_order(db_session, child, "TCS", source=OrderSource.SYSTEM, age_hours=1)   # a copy-engine order: never examined
    await master_order(db_session, child, "WIPRO", source=OrderSource.INTERNAL_UI, age_hours=1)   # a UI order on a Child account
    await master_order(db_session, m, "SBIN", age_hours=30)                           # outside the 24 h window
    result = await shadow.run_once(NOW, catalogs=cats())
    assert result.examined_orders == 3 and result.instruments == 2 and result.compared == 6
    symbols = {e.metadata_payload["symbol"] for e in await events()}
    assert symbols == {"RELIANCE", "INFY"}

    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_MAX_ORDERS", 1)
    again = await shadow.run_once(NOW + timedelta(days=1), catalogs=cats())
    assert again.examined_orders == 0                                                 # window moved: nothing recent
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_WINDOW_HOURS", 168)
    bounded = await shadow.run_once(NOW, catalogs=cats())
    assert bounded.examined_orders == 1 and bounded.instruments == 1


# ── what is recorded ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_results_are_honest_for_every_broker(db_session, sample_accounts, sample_strategy, on):
    m = sample_accounts["master"]
    await master_order(db_session, m, "RELIANCE-EQ")
    await shadow.run_once(NOW, catalogs=cats())
    ev = await events()
    (alice,), (angel,), (groww,) = by(ev, "alicebluenew", "RELIANCE-EQ"), by(ev, "angelone", "RELIANCE-EQ"), by(ev, "groww", "RELIANCE-EQ")
    assert (alice["verdict"], alice["catalog_loaded"], alice["candidate_count"]) == ("CATALOG_NOT_LOADED", False, None)
    # Angel's profile is only UNVERIFIED, below the fixed VERIFIED_DOCS level, so the profile verdict comes first; the
    # event still records that its catalog is not loaded.
    assert (angel["verdict"], angel["catalog_loaded"], angel["candidate_count"]) == ("SEGMENT_NOT_SUPPORTED", False, None)
    assert (groww["verdict"], groww["reason"], groww["confidence"]) == ("UNMAPPED", "SYMBOL_ONLY_NOT_DEFINITIVE", "SYMBOL_ONLY")
    assert groww["candidate_count"] == 1 and groww["catalog_loaded"] and groww["master_key"] == "EQ|NSE|SYM|RELIANCE"
    assert all(p["verdict"] != "MATCH" for p in (alice, angel, groww)) and all(p["min_evidence"] == "VERIFIED_DOCS" for p in (alice, angel, groww))


@pytest.mark.asyncio
async def test_the_job_uses_verified_docs_and_never_supplies_an_isin(db_session, sample_accounts, sample_strategy, on, monkeypatch):
    await master_order(db_session, sample_accounts["master"])
    seen = []
    real = shadow.compare_instrument

    def spy(master, view, profile, **kw):
        seen.append((master.confidence.value, kw.get("minimum"), kw.get("master_details")))
        return real(master, view, profile, **kw)

    monkeypatch.setattr(shadow, "compare_instrument", spy)
    await shadow.run_once(NOW, catalogs=cats())
    assert seen and {s[0] for s in seen} == {"SYMBOL_ONLY"} and {s[1] for s in seen} == {Verification.VERIFIED_DOCS}
    assert shadow.MIN_EVIDENCE is Verification.VERIFIED_DOCS


@pytest.mark.asyncio
async def test_groww_is_evaluated_at_docs_level_not_rejected_as_unsupported(db_session, sample_accounts, sample_strategy, on):
    await master_order(db_session, sample_accounts["master"])
    await shadow.run_once(NOW, catalogs=cats())
    (groww,) = by(await events(), "groww", "RELIANCE")
    assert groww["verdict"] != "SEGMENT_NOT_SUPPORTED"


@pytest.mark.asyncio
async def test_ambiguous_stale_derivative_and_unmappable_masters(db_session, sample_accounts, sample_strategy, on):
    m = sample_accounts["master"]
    dup = groww_store(groww_row(), groww_row(isin=INFY))
    await master_order(db_session, m, "RELIANCE")
    await master_order(db_session, m, "NIFTY 24500 CE", exchange="NFO")
    await master_order(db_session, m, "NIFTY", exchange="NSE")
    await shadow.run_once(NOW, catalogs=cats(groww=dup))
    ev = await events()
    assert by(ev, "groww", "RELIANCE")[0]["verdict"] == "AMBIGUOUS"
    nfo = by(ev, "groww", "NIFTY 24500 CE")[0]
    assert (nfo["verdict"], nfo["reason"]) == ("UNMAPPED", "DERIVATIVES_NOT_SUPPORTED_YET") and nfo["master_key"] is None
    assert by(ev, "groww", "NIFTY")[0]["reason"] == "NOT_AN_EQUITY_INDEX"
    later = NOW + timedelta(hours=30)                         # the same catalog is now older than 24 hours
    await master_order(db_session, m, "RELIANCE", age_hours=1, now=later)
    await shadow.run_once(later, catalogs=cats(groww=dup))
    stale = [p for p in by(await events(), "groww", "RELIANCE") if p["verdict"] == "MASTER_DATA_STALE"]
    assert len(stale) == 1 and stale[0]["reason"] == "CATALOG_OLDER_THAN_LIMIT"


@pytest.mark.asyncio
async def test_a_complete_but_unloaded_or_missing_instrument_is_reported_not_found(db_session, sample_accounts, sample_strategy, on):
    await master_order(db_session, sample_accounts["master"], "ZZZTOP")
    await shadow.run_once(NOW, catalogs=cats())
    (groww,) = by(await events(), "groww", "ZZZTOP")
    assert (groww["verdict"], groww["reason"], groww["candidate_count"]) == ("UNMAPPED", "NOT_FOUND", 0)


@pytest.mark.asyncio
async def test_events_carry_no_token_or_credential(db_session, sample_accounts, sample_strategy, on, tmp_path):
    angel = AngelInstrumentStore(clock=lambda: NOW - timedelta(hours=1))
    path = tmp_path / "scrip.json"
    path.write_text(json.dumps([{"token": "TOK-SECRET-9", "symbol": "RELIANCE-EQ", "name": "RELIANCE", "exch_seg": "NSE"}]))
    angel.load_file(str(path))
    await master_order(db_session, sample_accounts["master"])
    await shadow.run_once(NOW, catalogs=cats(angel=angel))
    ev = await events()
    text = json.dumps([e.metadata_payload for e in ev]) + " ".join(e.message for e in ev)
    assert "TOK-SECRET-9" not in text and "token" not in text.lower()
    (angel_p,) = by(ev, "angelone", "RELIANCE")
    assert angel_p["candidate_count"] == 1 and angel_p["verdict"] == "SEGMENT_NOT_SUPPORTED" and angel_p["catalog_loaded"]


# ── deduplication ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_repeat_run_writes_no_duplicates(db_session, sample_accounts, sample_strategy, on):
    await master_order(db_session, sample_accounts["master"])
    first = await shadow.run_once(NOW, catalogs=cats())
    assert first.written == 3 and first.duplicates == 0
    for later in (NOW, NOW + timedelta(minutes=15), NOW + timedelta(hours=3)):
        again = await shadow.run_once(later, catalogs=cats())
        assert again.written == 0 and again.duplicates == 3 and again.error is None
    assert len(await events()) == 3


@pytest.mark.asyncio
async def test_a_changed_result_writes_exactly_one_new_event(db_session, sample_accounts, sample_strategy, on):
    await master_order(db_session, sample_accounts["master"])
    await shadow.run_once(NOW, catalogs=cats())
    changed = cats(groww=groww_store(groww_row(), groww_row(isin=INFY)))                 # Groww now lists two candidates
    result = await shadow.run_once(NOW + timedelta(minutes=15), catalogs=changed)
    assert result.written == 1 and result.duplicates == 2
    ev = by(await events(), "groww", "RELIANCE")
    assert sorted(p["verdict"] for p in ev) == ["AMBIGUOUS", "UNMAPPED"]


@pytest.mark.asyncio
async def test_the_next_day_starts_a_new_dedupe_window(db_session, sample_accounts, sample_strategy, on, monkeypatch):
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_WINDOW_HOURS", 48)
    await master_order(db_session, sample_accounts["master"])
    await shadow.run_once(NOW, catalogs=cats())
    day2 = await shadow.run_once(NOW + timedelta(days=1), catalogs=cats())
    assert day2.written == 3 and len(await events()) == 6


@pytest.mark.asyncio
async def test_failure_to_read_the_dedupe_keys_writes_nothing(db_session, sample_accounts, sample_strategy, on, monkeypatch):
    await master_order(db_session, sample_accounts["master"])

    async def broken(session, day_start):
        raise RuntimeError("query failed")

    monkeypatch.setattr(shadow, "_todays_latest_keys", broken)
    result = await shadow.run_once(NOW, catalogs=cats())
    assert result.error == "DEDUPE_UNAVAILABLE: RuntimeError" and result.written == 0 and await events() == []


@pytest.mark.asyncio
async def test_too_many_same_day_events_means_dedupe_cannot_be_shown(db_session, sample_accounts, sample_strategy, on, monkeypatch):
    await master_order(db_session, sample_accounts["master"])
    await shadow.run_once(NOW, catalogs=cats())
    monkeypatch.setattr(shadow, "DEDUPE_KEY_LIMIT", 2)
    result = await shadow.run_once(NOW, catalogs=cats(groww=groww_store(groww_row(), groww_row(isin=INFY))))
    assert result.error.startswith("DEDUPE_UNAVAILABLE") and result.written == 0 and len(await events()) == 3


@pytest.mark.asyncio
async def test_the_dedupe_query_is_bounded_to_the_current_day(db_session, sample_accounts, sample_strategy, on):
    async with TestingSessionLocal() as s:
        s.add(SystemEvent(event_type=shadow.EVENT_TYPE, message="old", created_at=NOW - timedelta(days=3),
                          metadata_payload={"dedupe_key": "yesterday-ish"}))
        s.add(SystemEvent(event_type="OTHER_EVENT", message="x", created_at=NOW, metadata_payload={"dedupe_key": "x"}))
        await s.commit()
    async with TestingSessionLocal() as s:
        keys = await shadow._todays_latest_keys(s, NOW.replace(hour=0, minute=0))
    assert keys == {}


# ── isolation ────────────────────────────────────────────────────────────────────────────

def _imports(path):
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    names, calls = [], set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names += [f"{node.module}.{a.name}" for a in node.names]
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            calls.add(node.func.attr)
    return " ".join(names).lower(), calls


@pytest.mark.parametrize("module", [shadow, report_module, catalogs_module])
def test_the_observe_only_code_imports_no_client_redis_http_or_pipeline(module):
    names, calls = _imports(module.__file__)
    for banned in ("redis", "httpx", "requests", "pipeline", "alice_blue.client", "angel_one.client", "groww.client",
                   "engine_loop", "manager", "websocket"):
        assert banned not in names, (module.__name__, banned)
    assert not calls & {"place_order", "modify_order", "cancel_order", "get_order_book", "get_positions", "get_funds"}


def test_api_module_is_read_only():
    import app.api.instruments as api
    names, calls = _imports(api.__file__)
    for banned in ("redis", "httpx", "pipeline", "client", "manager"):
        assert banned not in names, banned
    src = Path(api.__file__).read_text(encoding="utf-8")
    assert "@router.get" in src and "@router.post" not in src and "@router.put" not in src and "@router.delete" not in src
    assert not calls & {"add", "commit", "flush", "delete", "execute_write"}


def test_nothing_in_the_trading_path_imports_the_instruments_package_except_the_planner():
    """R4c kept the whole instruments package out of the trading path. R4f wires in exactly ONE module: the pipeline imports
    `app.services.instruments.planner` for the per-order checks (off unless CANONICAL_MAPPING_ENFORCED). The shadow job,
    report, catalogs, comparator and models stay out of every trading module."""
    root = Path(__file__).resolve().parent.parent / "app"
    # Plus the OBSERVE-only lot / freeze terms module (pure: no network, database or broker import - asserted in
    # test_quantity_observe.py): the pipeline reads it to log would-be rejections, and the Alice contract-master loader fills it.
    allowed = {
        "app/services/copy_engine/pipeline.py": ("from app.services.instruments.planner import",
                                                 "from app.services.instruments.terms import"),
        "app/services/alice_blue/instruments.py": ("from app.services.instruments.terms import",),
        # The read-only Child preview (never sends or writes) calls the same planner functions the pipeline applies before a send.
        "app/services/copy_engine/child_preview.py": ("from app.services.instruments.planner import",),
    }
    for sub in ("services/copy_engine", "services/reliability", "services/runtime", "services/alice_blue",
                "services/groww", "services/angel_one", "services/brokers"):
        for path in (root / sub).rglob("*.py"):
            rel = path.relative_to(root.parent).as_posix()
            for line in path.read_text(encoding="utf-8").splitlines():
                if "services.instruments" in line:
                    assert line.strip().startswith(allowed.get(rel, ())), (rel, line)


def test_the_lifespan_starts_and_stops_the_job_around_the_engine():
    src = (Path(__file__).resolve().parent.parent / "app" / "main.py").read_text(encoding="utf-8")
    assert src.index("engine_loop.start()") < src.index("shadow_job.start()")
    assert src.index("shadow_job.stop()") < src.index("engine_loop.stop()")


@pytest.mark.asyncio
async def test_the_job_writes_only_shadow_events_and_changes_no_order(db_session, sample_accounts, sample_strategy, on):
    row_id = (await master_order(db_session, sample_accounts["master"])).id
    before = (await db_session.execute(select(func.count()).select_from(Order))).scalar_one()
    await shadow.run_once(NOW, catalogs=cats())
    db_session.expire_all()
    assert (await db_session.execute(select(func.count()).select_from(Order))).scalar_one() == before
    types = {e.event_type for e in (await db_session.execute(select(SystemEvent))).scalars().all()}
    assert types == {shadow.EVENT_TYPE}
    assert (await db_session.get(Order, row_id)).status == OrderStatus.OPEN


@pytest.mark.asyncio
async def test_only_orders_on_the_master_of_a_live_strategy_are_examined(db_session, sample_accounts, sample_strategy, on):
    m, child = sample_accounts["master"], sample_accounts["follower1"]
    await master_order(db_session, m, "RELIANCE")
    await master_order(db_session, child, "INFY", source=OrderSource.BROKER_EXTERNAL)     # not a strategy Master
    first = await shadow.run_once(NOW, catalogs=cats())
    assert first.instruments == 1
    strategy = await db_session.get(GroupStrategy, sample_strategy.id)
    strategy.deleted_at = NOW
    await db_session.commit()
    after = await shadow.run_once(NOW + timedelta(minutes=5), catalogs=cats())
    assert after.examined_orders == 0 and after.written == 0                                # its Master no longer counts


@pytest.mark.asyncio
async def test_a_result_that_reverts_to_an_earlier_state_is_recorded_again(db_session, sample_accounts, sample_strategy, on):
    await master_order(db_session, sample_accounts["master"])
    plain, doubled = cats(), cats(groww=groww_store(groww_row(), groww_row(isin=INFY)))
    assert (await shadow.run_once(NOW, catalogs=plain)).written == 3                        # A
    assert (await shadow.run_once(NOW + timedelta(minutes=1), catalogs=doubled)).written == 1   # B
    back = await shadow.run_once(NOW + timedelta(minutes=2), catalogs=plain)                # A again
    assert back.written == 1 and back.duplicates == 2
    again = await shadow.run_once(NOW + timedelta(minutes=3), catalogs=plain)
    assert again.written == 0
    groww = sorted(by(await events(), "groww", "RELIANCE"), key=lambda p: p["verdict"])
    assert [p["verdict"] for p in groww] == ["AMBIGUOUS", "UNMAPPED", "UNMAPPED"]
    # the report's current state is the latest event, A - not the intermediate B
    async with TestingSessionLocal() as s:
        rows = (await s.execute(select(SystemEvent.created_at, SystemEvent.metadata_payload)
                                .where(SystemEvent.event_type == shadow.EVENT_TYPE)
                                .order_by(SystemEvent.created_at.desc()))).all()
    data = report_module.build_report(rows, hours=24, limit=50, offset=0, truncated=False, now=NOW)
    assert data["by_broker"]["groww"]["groups"] == {"identity_unconfirmed": 1}


@pytest.mark.asyncio
async def test_two_spellings_of_one_symbol_are_both_reported(db_session, sample_accounts, sample_strategy, on):
    await master_order(db_session, sample_accounts["master"], "RELIANCE")
    await master_order(db_session, sample_accounts["master"], "RELIANCE-EQ")
    result = await shadow.run_once(NOW, catalogs=cats())
    assert result.instruments == 2 and result.written == 6
    assert {e.metadata_payload["symbol"] for e in await events()} == {"RELIANCE", "RELIANCE-EQ"}


BASE_PAYLOAD = {
    "broker_id": "groww", "exchange": "NSE", "symbol": "RELIANCE", "master_key": "EQ|NSE|SYM|RELIANCE",
    "confidence": "SYMBOL_ONLY", "verdict": "UNMAPPED", "reason": "SYMBOL_ONLY_NOT_DEFINITIVE", "candidate_count": 1,
    "catalog_loaded": True, "catalog_loaded_at": "2026-10-22T04:00:00+00:00", "diffs": [], "unverified_attributes": [],
    "min_evidence": "VERIFIED_DOCS",
}


@pytest.mark.parametrize("field,value", [
    ("broker_id", "groww2"), ("exchange", "BSE"), ("symbol", "OTHER"), ("master_key", "EQ|NSE|SYM|OTHER"),
    ("confidence", "ISIN"), ("verdict", "MATCH"), ("reason", "X"), ("candidate_count", 9), ("catalog_loaded", False),
    ("catalog_loaded_at", "2026-01-01T00:00:00+00:00"), ("diffs", [{"attribute": "lot_size", "master": "1", "broker": "5"}]),
    ("unverified_attributes", ["lot_size"]), ("min_evidence", "VERIFIED_LIVE"),
])
def test_every_reported_field_is_part_of_the_dedupe_key(field, value):
    base = BASE_PAYLOAD
    assert shadow.dedupe_key({**base, field: value}, "2026-10-22") != shadow.dedupe_key(base, "2026-10-22")
    assert shadow.dedupe_key(base, "2026-10-22") == shadow.dedupe_key(dict(base), "2026-10-22")
    assert shadow.dedupe_key(base, "2026-10-23") != shadow.dedupe_key(base, "2026-10-22")


def _fake_lifespan_environment(monkeypatch, order):
    """The real lifespan, with the engine, Redis and the database replaced by fakes."""
    import app.main as main

    class FakeEngine:
        def __init__(self, redis):
            pass

        async def start(self):
            order.append("engine.start")

        async def stop(self):
            order.append("engine.stop")

    class FakeConn:
        async def execute(self, *a, **k):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeDbEngine:
        def begin(self):
            return FakeConn()

    class FakeRedis:
        async def ping(self):
            return True

    async def fake_get_redis():
        return FakeRedis()

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(main, "EngineLoop", FakeEngine)
    monkeypatch.setattr(main, "get_redis", fake_get_redis)
    monkeypatch.setattr(main, "close_redis", noop)
    monkeypatch.setattr(main, "validate_encryption_key", lambda: None)
    monkeypatch.setattr(main, "find_placeholder_secrets", lambda s: [])
    monkeypatch.setattr(settings, "MASTER_CAPITAL", Decimal("1000"))
    monkeypatch.setattr("app.core.database.engine", FakeDbEngine())
    monkeypatch.setattr("app.services.groww.instruments.load_instruments_if_enabled", noop)
    return main


@pytest.mark.asyncio
async def test_the_lifespan_starts_the_job_after_the_engine_and_stops_it_first(monkeypatch):
    order = []
    main = _fake_lifespan_environment(monkeypatch, order)

    class FakeJob:
        async def start(self):
            order.append("job.start")

        async def stop(self):
            order.append("job.stop")

    monkeypatch.setattr(shadow, "ShadowJob", FakeJob)
    async with main.lifespan(main.app):
        assert order == ["engine.start", "job.start"]
    assert order == ["engine.start", "job.start", "job.stop", "engine.stop"]


@pytest.mark.asyncio
async def test_the_real_job_creates_no_task_in_the_lifespan_when_the_flag_is_off(monkeypatch):
    main = _fake_lifespan_environment(monkeypatch, [])
    assert settings.INSTRUMENT_SHADOW_ENABLED is False
    async with main.lifespan(main.app):
        assert "instrument-shadow" not in {t.get_name() for t in asyncio.all_tasks()}


@pytest.mark.asyncio
async def test_the_real_job_runs_inside_the_lifespan_when_the_flag_is_on_and_is_cancelled_at_shutdown(monkeypatch):
    main = _fake_lifespan_environment(monkeypatch, [])
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_ENABLED", True)
    async with main.lifespan(main.app):
        names = {t.get_name() for t in asyncio.all_tasks()}
        assert "instrument-shadow" in names
    await asyncio.sleep(0)
    assert "instrument-shadow" not in {t.get_name() for t in asyncio.all_tasks()}


@pytest.mark.asyncio
async def test_a_copy_engine_order_is_never_examined_even_on_a_master_account(db_session, sample_accounts, sample_strategy, on):
    m = sample_accounts["master"]
    await master_order(db_session, m, "RELIANCE")
    await master_order(db_session, m, "TCS", source=OrderSource.SYSTEM)
    result = await shadow.run_once(NOW, catalogs=cats())
    assert result.examined_orders == 1 and {e.metadata_payload["symbol"] for e in await events()} == {"RELIANCE"}
