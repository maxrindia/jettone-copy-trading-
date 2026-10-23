"""
R4i: the NSE crosswalk as shadow-only evidence. Synthetic fixture and local files only - no broker, Redis, network or
production access. A candidate ISIN is a MASTER_TOKEN_LINK_UNVERIFIED lead: it must never become a match, an identity, or
anything the planner or the copy pipeline can see.
"""
import ast
import csv
import hashlib
import io
import socket
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.core.config import Settings, settings
from app.core.database import get_db
from app.core.redis import get_redis
from app.main import app
from app.models.audit import SystemEvent
from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services.angel_one.instruments import AngelInstrumentStore
from app.services.instruments import crosswalk_evidence as ce
from app.services.instruments import nse_crosswalk as cwm
from app.services.instruments import shadow
from app.services.instruments.catalogs import AliceCatalog, AngelCatalog, GrowwCatalog
from conftest import TestingSessionLocal
from test_instrument_catalogs import groww_row, groww_store

APP = Path(__file__).resolve().parent.parent / "app"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "synthetic_nse_cm_security.csv"
SNAP = date(2026, 10, 22)                                              # a Thursday
NOW = datetime(2026, 10, 22, 6, 0, tzinfo=timezone.utc)                # 11:30 IST on the snapshot day
TOKEN = {"X-Internal-Token": "test-internal-token"}
LINK = "MASTER_TOKEN_LINK_UNVERIFIED"

ALPHA, ALPHA_ISIN = "9000003", "ZZSYN001A014"                          # SYNALPHA, series EQ
SME, BOND, COLA_D1, PLACEHOLDER, DELETED = "9000008", "9000009", "9000011", "9000001", "9000002"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    local = {"127.0.0.1", "::1", "localhost", None, ""}               # the event loop's own self-pipe uses loopback
    real_connect, real_getaddrinfo = socket.socket.connect, socket.getaddrinfo

    def connect(self, address, *a, **k):
        if (address[0] if isinstance(address, tuple) else address) not in local:
            raise AssertionError("network access attempted")
        return real_connect(self, address, *a, **k)

    def getaddrinfo(host, *a, **k):
        if host not in local:
            raise AssertionError("network access attempted")
        return real_getaddrinfo(host, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    ce.clear_cache()
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", "")
    yield
    ce.clear_cache()


def make_file(tmp_path, snapshot=SNAP, rows=None, name=None):
    """A copy of the synthetic fixture whose UpdDt agrees with `snapshot`, named like the official file."""
    with open(FIXTURE, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        header, data = reader.fieldnames, list(reader)
    upd = str(int((datetime(snapshot.year, snapshot.month, snapshot.day, 12) - datetime(1980, 1, 1)).total_seconds()))
    for r in data:
        r["UpdDt"] = upd
    data = rows(data) if rows else data
    out = io.StringIO(newline="")
    w = csv.DictWriter(out, fieldnames=header, lineterminator="\n")
    w.writeheader()
    w.writerows(data)
    path = tmp_path / (name or f"NSE_CM_security_{snapshot.strftime('%d%m%Y')}.csv")
    path.write_bytes(out.getvalue().encode("utf-8"))
    return path


def evaluate(path, symbol, token, exchange="NSE", now=NOW):
    cw, err = ce.get_crosswalk(str(path))
    return ce.evaluate(exchange, symbol, token, cw, err, now)


# ── configuration ────────────────────────────────────────────────────────────────────────

def test_defaults_are_off():
    s = Settings(_env_file=None)
    assert s.INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH == "" and s.INSTRUMENT_SHADOW_ENABLED is False
    assert s.CANONICAL_MAPPING_ENFORCED is False


def test_not_configured_reads_nothing():
    assert ce.is_configured() is False
    assert ce.get_crosswalk() == (None, ce.NOT_CONFIGURED)


# ── candidate lookup and provenance ──────────────────────────────────────────────────────

def test_a_candidate_is_always_unverified_and_carries_provenance(tmp_path):
    path = make_file(tmp_path)
    p = evaluate(path, "SYNALPHA", ALPHA)
    assert p["state"] == "CANDIDATE" and p["candidate_isin"] == ALPHA_ISIN and p["reason"] == "TOKEN_LINK_UNVERIFIED"
    assert p["link_status"] == LINK == ce.LINK_STATUS
    snap = p["snapshot"]
    assert snap["source_filename"] == "NSE_CM_security_22102026.csv" and snap["snapshot_date"] == "2026-10-22"
    assert snap["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest() == snap["content_sha256"]
    assert snap["age_trading_days"] == 0 and snap["stale"] is False
    assert (p["crosswalk_series"], p["crosswalk_symbol"], p["master_token"]) == ("EQ", "SYNALPHA", ALPHA)


def test_a_dash_eq_master_symbol_is_normalised_but_still_needs_the_token(tmp_path):
    path = make_file(tmp_path)
    assert evaluate(path, "synalpha-eq", ALPHA)["state"] == "CANDIDATE"


@pytest.mark.parametrize("symbol,token,state,reason", [
    ("SYNALPHA", "9999999", "UNMAPPED", "TOKEN_NOT_IN_SNAPSHOT"),
    ("SYNALPHA", "ABC", "UNMAPPED", "MALFORMED_TOKEN"),
    ("SYNDUMMY", PLACEHOLDER, "INVALID_ISIN", "PLACEHOLDER_OR_MALFORMED_ISIN"),
    ("SYNDELETED", DELETED, "DELETED", "DELFLG_Y"),
    ("SYNBOND", BOND, "UNSUPPORTED_SERIES", "SERIES_GB"),
    ("SYNCOLA", COLA_D1, "UNSUPPORTED_SERIES", "SERIES_D1"),
    ("SYNBETA", ALPHA, "SYMBOL_DISAGREES", "ORDER_SYMBOL_DIFFERS_FROM_SNAPSHOT"),
    ("SYNALPHA", None, "NO_MASTER_TOKEN", "ORDER_HAS_NO_INSTRUMENT_TOKEN"),
    ("", ALPHA, "MASTER_SYMBOL_UNUSABLE", "MISSING_SYMBOL"),
])
def test_every_non_candidate_state_carries_no_isin(tmp_path, symbol, token, state, reason):
    p = evaluate(make_file(tmp_path), symbol, token)
    assert (p["state"], p["reason"]) == (state, reason)
    assert p["candidate_isin"] is None and p["link_status"] == LINK


def test_a_symbol_alone_never_finds_a_record(tmp_path):
    path = make_file(tmp_path)
    p = evaluate(path, "SYNALPHA", None)                               # the exact snapshot symbol, but no token
    assert p["state"] == "NO_MASTER_TOKEN" and p["candidate_isin"] is None
    wrong = evaluate(path, "SYNALPHA", "9000004")                      # token of another instrument (SYNBETA)
    assert wrong["state"] == "SYMBOL_DISAGREES" and wrong["candidate_isin"] is None


def test_a_symbol_shared_by_two_series_follows_the_token_only(tmp_path):
    path = make_file(tmp_path)
    eq, d1 = evaluate(path, "SYNCOLA", "9000010"), evaluate(path, "SYNCOLA", COLA_D1)
    assert eq["state"] == "CANDIDATE" and eq["candidate_isin"] == "ZZSYN008A019"
    assert d1["state"] == "UNSUPPORTED_SERIES" and d1["candidate_isin"] is None


@pytest.mark.parametrize("exchange", ["BSE", "NFO", "BFO", "MCX", "", "XYZ"])
def test_only_nse_is_covered(tmp_path, exchange):
    p = evaluate(make_file(tmp_path), "SYNALPHA", ALPHA, exchange=exchange)
    assert p["state"] == "EXCHANGE_NOT_COVERED" and p["candidate_isin"] is None


def test_series_support_is_an_explicit_list():
    assert ce.SUPPORTED_SERIES == {"EQ", "BE", "BZ", "SM", "ST"}


def test_sme_series_is_supported(tmp_path):
    assert evaluate(make_file(tmp_path), "SYNSME", SME)["state"] == "CANDIDATE"


# ── freshness, conflicts, unusable files ─────────────────────────────────────────────────

def test_a_stale_snapshot_gives_no_candidate(tmp_path):
    path = make_file(tmp_path)
    monday = datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc)         # two weekdays after Thursday
    p = evaluate(path, "SYNALPHA", ALPHA, now=monday)
    assert p["state"] == "STALE_SNAPSHOT" and p["candidate_isin"] is None
    assert p["snapshot"]["stale"] is True and p["snapshot"]["age_trading_days"] == 2
    friday = datetime(2026, 10, 23, 6, 0, tzinfo=timezone.utc)
    assert evaluate(path, "SYNALPHA", ALPHA, now=friday)["state"] == "CANDIDATE"


def test_a_future_dated_snapshot_gives_no_candidate(tmp_path):
    p = evaluate(make_file(tmp_path), "SYNALPHA", ALPHA, now=NOW - timedelta(days=3))
    assert p["state"] == "STALE_SNAPSHOT" and p["reason"] == "FUTURE_SNAPSHOT" and p["candidate_isin"] is None


def test_conflicting_rows_give_no_winner(tmp_path):
    def dup(rows):
        twin = dict(next(r for r in rows if r["FinInstrmId"] == ALPHA))
        twin["ISIN"] = "ZZSYN002A012"
        return rows + [twin]
    p = evaluate(make_file(tmp_path, rows=dup), "SYNALPHA", ALPHA)
    assert p["state"] == "CONFLICT" and p["conflicting_rows"] == 2 and p["candidate_isin"] is None


def test_unusable_files_fail_closed_with_a_code_only(tmp_path):
    missing = tmp_path / "NSE_CM_security_22102026.csv"
    assert ce.get_crosswalk(str(missing)) == (None, "FILE_UNUSABLE")
    bad = tmp_path / "NSE_CM_security_21102026.csv"
    bad.write_text("FinInstrmId,TckrSymb\n1,A\n", encoding="utf-8")
    cw, err = ce.get_crosswalk(str(bad))
    assert cw is None and err == "MISSING_REQUIRED_COLUMNS"
    nodate = tmp_path / "securities.csv"
    nodate.write_bytes(make_file(tmp_path).read_bytes())
    assert ce.get_crosswalk(str(nodate))[0] is None
    assert ce.get_crosswalk("https://nsearchives.nseindia.com/content/cm/NSE_CM_security_22102026.csv.gz") == (None, "FILE_UNUSABLE")
    p = ce.evaluate("NSE", "SYNALPHA", ALPHA, None, "MISSING_REQUIRED_COLUMNS", NOW)
    assert p["state"] == "CROSSWALK_UNAVAILABLE" and p["reason"] == "MISSING_REQUIRED_COLUMNS" and p["snapshot"] is None
    assert str(tmp_path) not in str(p)


def test_the_file_is_parsed_once_per_version(tmp_path, monkeypatch):
    path = make_file(tmp_path)
    calls = []
    real = ce.load_nse_security_master
    monkeypatch.setattr(ce, "load_nse_security_master", lambda p: calls.append(p) or real(p))
    a, b = ce.get_crosswalk(str(path)), ce.get_crosswalk(str(path))
    assert a[0] is b[0] and len(calls) == 1
    path.write_bytes(path.read_bytes() + b"\n")                        # a new version (size changes) is re-read
    assert ce.get_crosswalk(str(path))[0] is not a[0] and len(calls) == 2


def test_dedupe_key_changes_with_state_snapshot_and_day(tmp_path):
    path = make_file(tmp_path)
    p = evaluate(path, "SYNALPHA", ALPHA)
    stale = evaluate(path, "SYNALPHA", ALPHA, now=datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc))
    assert ce.dedupe_key(p, "d1") != ce.dedupe_key(stale, "d1") != ce.dedupe_key(p, "d2")
    assert ce.dedupe_key(p, "d1") == ce.dedupe_key(evaluate(path, "SYNALPHA", ALPHA), "d1")


# ── the shadow job ───────────────────────────────────────────────────────────────────────

@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_ENABLED", True)
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_MAX_ORDERS", 500)
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_WINDOW_HOURS", 24)


def cats():
    return [AliceCatalog(), AngelCatalog(AngelInstrumentStore()), GrowwCatalog(groww_store(groww_row()))]


async def master_order(db, account, symbol, token, exchange="NSE", now=NOW):
    row = Order(id=uuid.uuid4(), internal_order_id=f"EXT_{uuid.uuid4().hex[:10]}", broker_order_id=uuid.uuid4().hex[:8],
                account_id=account.id, source=OrderSource.BROKER_EXTERNAL, exchange=exchange, symbol=symbol, side=OrderSide.BUY,
                order_type="LIMIT", product_type="MIS", quantity=1, filled_quantity=0, pending_quantity=1,
                price=Decimal("1"), status=OrderStatus.OPEN, created_at=now - timedelta(hours=1), instrument_token=token)
    db.add(row)
    await db.commit()


async def events(event_type):
    async with TestingSessionLocal() as s:
        return [e.metadata_payload for e in (await s.execute(select(SystemEvent).where(SystemEvent.event_type == event_type))).scalars()]


@pytest.mark.asyncio
async def test_off_by_default_nothing_is_read_or_written(db_session, sample_accounts, sample_strategy, on, monkeypatch):
    await master_order(db_session, sample_accounts["master"], "SYNALPHA", ALPHA)
    monkeypatch.setattr(ce, "get_crosswalk", lambda *a: (_ for _ in ()).throw(AssertionError("must not be read")))
    result = await shadow.run_once(NOW, catalogs=cats())
    assert result.error is None and result.crosswalk_error is None
    assert result.crosswalk_written == 0 and result.crosswalk_duplicates == 0 and await events(ce.EVENT_TYPE) == []
    assert result.written > 0                                          # the existing comparison is unchanged


@pytest.mark.asyncio
async def test_the_shadow_flag_off_means_no_crosswalk_work_even_when_a_path_is_set(db_session, sample_accounts, sample_strategy, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_ENABLED", False)
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", str(make_file(tmp_path)))
    await master_order(db_session, sample_accounts["master"], "SYNALPHA", ALPHA)
    result = await shadow.run_once(NOW, catalogs=cats())
    assert result.enabled is False and await events(ce.EVENT_TYPE) == []


@pytest.mark.asyncio
async def test_candidates_are_recorded_deduplicated_and_never_change_the_comparison(db_session, sample_accounts, sample_strategy, on, tmp_path, monkeypatch):
    master = sample_accounts["master"]
    await master_order(db_session, master, "SYNALPHA", ALPHA)
    await master_order(db_session, master, "SYNALPHA", None)                          # same symbol, no token
    await master_order(db_session, master, "SYNBOND", BOND)
    await master_order(db_session, master, "TCS", "11536", exchange="BSE")
    await master_order(db_session, master, "NIFTY", "74056", exchange="NFO")
    baseline = await shadow.run_once(NOW, catalogs=cats())
    base_compare = sorted((p["broker_id"], p["symbol"], p["verdict"], p["reason"]) for p in await events(shadow.EVENT_TYPE))

    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", str(make_file(tmp_path)))
    first = await shadow.run_once(NOW + timedelta(minutes=1), catalogs=cats())
    assert first.error is None and first.crosswalk_error is None and first.crosswalk_written == 5 and first.crosswalk_duplicates == 0
    by_state = {(p["symbol"], p["master_token"]): p for p in await events(ce.EVENT_TYPE)}
    assert by_state[("SYNALPHA", ALPHA)]["state"] == "CANDIDATE" and by_state[("SYNALPHA", ALPHA)]["candidate_isin"] == ALPHA_ISIN
    assert by_state[("SYNALPHA", None)]["state"] == "NO_MASTER_TOKEN" and by_state[("SYNALPHA", None)]["candidate_isin"] is None
    assert by_state[("SYNBOND", BOND)]["state"] == "UNSUPPORTED_SERIES"
    assert by_state[("TCS", "11536")]["state"] == "EXCHANGE_NOT_COVERED" and by_state[("NIFTY", "74056")]["state"] == "EXCHANGE_NOT_COVERED"
    assert {p["link_status"] for p in by_state.values()} == {LINK}
    assert all(p["dedupe_key"] for p in by_state.values())

    again = await shadow.run_once(NOW + timedelta(minutes=2), catalogs=cats())
    assert again.crosswalk_written == 0 and again.crosswalk_duplicates == 5

    after_compare = sorted((p["broker_id"], p["symbol"], p["verdict"], p["reason"]) for p in await events(shadow.EVENT_TYPE))
    assert after_compare == base_compare and baseline.written == len(base_compare)    # the crosswalk changed no comparison
    assert all(v != "MATCH" for _, _, v, _ in after_compare)


@pytest.mark.asyncio
async def test_a_changed_state_is_recorded_again(db_session, sample_accounts, sample_strategy, on, tmp_path, monkeypatch):
    await master_order(db_session, sample_accounts["master"], "SYNALPHA", ALPHA)
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", str(make_file(tmp_path)))
    await shadow.run_once(NOW, catalogs=cats())
    monday = datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc)
    await master_order(db_session, sample_accounts["master"], "SYNALPHA", ALPHA, now=monday)
    later = await shadow.run_once(monday, catalogs=cats())
    states = [p["state"] for p in await events(ce.EVENT_TYPE)]
    assert later.crosswalk_written == 1 and states == ["CANDIDATE", "STALE_SNAPSHOT"]


@pytest.mark.asyncio
async def test_a_replaced_snapshot_with_the_same_answer_is_recorded_again_for_provenance(db_session, sample_accounts, sample_strategy, on, tmp_path, monkeypatch):
    """Same date, same candidate, different file bytes: the provenance (SHA-256) differs, so the change must be visible."""
    await master_order(db_session, sample_accounts["master"], "SYNALPHA", ALPHA)
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", str(make_file(tmp_path)))
    first = await shadow.run_once(NOW, catalogs=cats())
    make_file(tmp_path, rows=lambda rows: [r for r in rows if r["FinInstrmId"] != "9000014"])      # drops an unrelated row
    second = await shadow.run_once(NOW + timedelta(minutes=1), catalogs=cats())
    assert first.crosswalk_written == 1 and second.crosswalk_written == 1 and second.crosswalk_duplicates == 0
    a, b = await events(ce.EVENT_TYPE)
    assert a["state"] == b["state"] == "CANDIDATE" and a["snapshot"]["sha256"] != b["snapshot"]["sha256"]


@pytest.mark.asyncio
async def test_system_orders_are_not_subjects(db_session, sample_accounts, sample_strategy, on, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", str(make_file(tmp_path)))
    row = Order(id=uuid.uuid4(), internal_order_id="SYS_1", broker_order_id="b1", account_id=sample_accounts["master"].id,
                source=OrderSource.SYSTEM, exchange="NSE", symbol="SYNALPHA", side=OrderSide.BUY, order_type="LIMIT",
                product_type="MIS", quantity=1, filled_quantity=0, pending_quantity=1, price=Decimal("1"),
                status=OrderStatus.OPEN, created_at=NOW - timedelta(hours=1), instrument_token=ALPHA)
    db_session.add(row)
    await db_session.commit()
    await shadow.run_once(NOW, catalogs=cats())
    assert await events(ce.EVENT_TYPE) == []


@pytest.mark.asyncio
async def test_a_missing_file_records_unavailable_and_does_not_break_the_cycle(db_session, sample_accounts, sample_strategy, on, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", str(tmp_path / "NSE_CM_security_22102026.csv"))
    await master_order(db_session, sample_accounts["master"], "SYNALPHA", ALPHA)
    result = await shadow.run_once(NOW, catalogs=cats())
    assert result.error is None and result.crosswalk_error is None and result.written > 0
    (payload,) = await events(ce.EVENT_TYPE)
    assert payload["state"] == "CROSSWALK_UNAVAILABLE" and payload["candidate_isin"] is None and payload["snapshot"] is None
    assert str(tmp_path) not in str(payload)


@pytest.mark.asyncio
async def test_unreadable_crosswalk_dedupe_writes_no_crosswalk_events_but_keeps_the_comparison(db_session, sample_accounts, sample_strategy, on, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", str(make_file(tmp_path)))
    await master_order(db_session, sample_accounts["master"], "SYNALPHA", ALPHA)

    async def broken(session, day_start):
        raise RuntimeError("query failed")

    monkeypatch.setattr(shadow, "_todays_latest_crosswalk_keys", broken)
    result = await shadow.run_once(NOW, catalogs=cats())
    assert result.crosswalk_error == "DEDUPE_UNAVAILABLE: RuntimeError" and result.crosswalk_written == 0
    assert await events(ce.EVENT_TYPE) == [] and result.written > 0 and result.error is None


@pytest.mark.asyncio
async def test_a_crosswalk_step_failure_is_contained(db_session, sample_accounts, sample_strategy, on, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", str(make_file(tmp_path)))
    await master_order(db_session, sample_accounts["master"], "SYNALPHA", ALPHA)

    def boom(*a, **k):
        raise RuntimeError("evaluation bug")

    monkeypatch.setattr(ce, "evaluate", boom)
    result = await shadow.run_once(NOW, catalogs=cats())
    assert result.crosswalk_error == "RuntimeError" and result.error is None and result.written > 0   # the comparison is unaffected
    assert await events(ce.EVENT_TYPE) == []


@pytest.mark.asyncio
async def test_child_account_and_system_orders_are_not_subjects(db_session, sample_accounts, sample_strategy, on, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", str(make_file(tmp_path)))
    await master_order(db_session, sample_accounts["follower1"], "SYNALPHA", ALPHA)
    await shadow.run_once(NOW, catalogs=cats())
    assert await events(ce.EVENT_TYPE) == []


# ── the report ───────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client(mock_redis, db_session):
    async def override_db():
        async with TestingSessionLocal() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_redis] = lambda: mock_redis
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def count(model):
    async with TestingSessionLocal() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


@pytest.mark.asyncio
async def test_the_report_shows_the_crosswalk_section_read_only(client, db_session, sample_accounts, sample_strategy, on, tmp_path, monkeypatch):
    ist_today = datetime.now(timezone.utc).astimezone(cwm.IST).date()
    path = make_file(tmp_path, snapshot=ist_today)
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", str(path))
    now = datetime.now(timezone.utc)
    await master_order(db_session, sample_accounts["master"], "SYNALPHA", ALPHA, now=now)
    await master_order(db_session, sample_accounts["master"], "SYNBOND", BOND, now=now)
    result = await shadow.run_once(now, catalogs=cats())
    assert result.crosswalk_written == 2

    before = await count(SystemEvent)
    data = (await client.get("/api/v1/instruments/shadow-report", headers=TOKEN)).json()["data"]
    assert await count(SystemEvent) == before                                   # reading writes nothing
    cw = data["crosswalk"]
    assert cw["configured"] is True and cw["link_status"] == LINK and cw["candidates"] == 1 and cw["instrument_count"] == 2
    assert cw["by_state"] == {"CANDIDATE": 1, "UNSUPPORTED_SERIES": 1} and cw["load_error"] is None
    assert cw["live_snapshot"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert cw["live_snapshot"]["snapshot_date"] == ist_today.isoformat() and cw["live_snapshot"]["stale"] is False
    assert {r["link_status"] for r in cw["rows"]} == {LINK}
    assert "matched" not in data["groups"] and "MATCH" not in data["by_verdict"]       # a candidate is never parity
    assert "unverified" in cw["note"].lower() or LINK.lower() in cw["note"].lower()


@pytest.mark.asyncio
async def test_the_report_without_a_configured_file(client, db_session, sample_accounts, sample_strategy):
    cw = (await client.get("/api/v1/instruments/shadow-report", headers=TOKEN)).json()["data"]["crosswalk"]
    assert cw["configured"] is False and cw["live_snapshot"] is None and cw["load_error"] is None
    assert cw["rows"] == [] and cw["candidates"] == 0 and cw["instrument_count"] == 0


@pytest.mark.asyncio
async def test_the_report_with_an_unusable_file_says_why_without_leaking_the_path(client, db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", str(tmp_path / "NSE_CM_security_22102026.csv"))
    cw = (await client.get("/api/v1/instruments/shadow-report", headers=TOKEN)).json()["data"]["crosswalk"]
    assert cw["configured"] is True and cw["load_error"] == "FILE_UNUSABLE" and cw["live_snapshot"] is None
    assert str(tmp_path) not in str(cw)


def test_a_stored_link_status_is_never_trusted_by_the_report():
    forged = {"exchange": "NSE", "symbol": "X", "master_token": "5", "state": "CANDIDATE", "reason": "R", "link_status": "VERIFIED",
              "candidate_isin": "ZZSYN001A014"}
    section = ce.build_section([(NOW, forged)], NOW)
    assert {r["link_status"] for r in section["rows"]} == {LINK}


# ── isolation ────────────────────────────────────────────────────────────────────────────

def test_the_evidence_module_has_no_network_redis_db_or_order_dependency():
    tree = ast.parse((APP / "services" / "instruments" / "crosswalk_evidence.py").read_text(encoding="utf-8"))
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.level == 0:
            imported.add(n.module or "")
    banned = ("redis", "httpx", "requests", "urllib", "socket", "sqlalchemy", "aiohttp", "websockets")
    assert not [m for m in imported if m.split(".")[0] in banned]
    assert not [m for m in imported if any(w in m for w in ("planner", "pipeline", "copy_engine", "alice_blue", "groww", "angel_one", "brokers"))]
    called = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not called & {"place_order", "modify_order", "cancel_order", "urlopen", "request", "post"}


def test_the_planner_pipeline_and_orders_never_see_the_crosswalk():
    for rel in ("services/instruments/planner.py", "services/copy_engine/pipeline.py", "services/copy_engine/calculator.py",
                "services/alice_blue/client.py", "models/trades.py"):
        text = (APP / rel).read_text(encoding="utf-8").lower()
        assert "crosswalk" not in text, rel
