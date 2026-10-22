"""
R4c: the read-only instrument endpoints. Fixtures only - no broker, Redis or network, and nothing is written.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app.core.config import settings
from app.core.database import get_db
from app.core.redis import get_redis
from app.main import app
from app.models.accounts import Account
from app.models.audit import SystemEvent
from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services.groww import instruments as groww_instruments
from app.services.instruments import report, shadow
from app.services.instruments.catalogs import AliceCatalog, AngelCatalog, GrowwCatalog
from app.services.angel_one.instruments import AngelInstrumentStore
from conftest import TestingSessionLocal
from test_instrument_catalogs import INFY, REL, groww_row, groww_store

TOKEN = {"X-Internal-Token": "test-internal-token"}
PATHS = ["/api/v1/instruments/compare?exchange=NSE&symbol=RELIANCE", "/api/v1/instruments/shadow-report"]


@pytest_asyncio.fixture
async def client(mock_redis, db_session):
    async def override_db():
        async with TestingSessionLocal() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_redis] = lambda: mock_redis
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def groww_loaded(monkeypatch):
    """The live Groww store replaced by a loaded fixture whose snapshot time is 'just now' (the endpoints use the clock)."""
    store = groww_store(groww_row(), groww_row(symbol="INFY", isin=INFY))
    store._snapshot_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    monkeypatch.setattr(groww_instruments, "store", store)
    return store


async def count(model):
    async with TestingSessionLocal() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def seed_events(db, *, symbol="RELIANCE", now=None, groww=None):
    now = now or datetime.now(timezone.utc)
    master = (await db.execute(select(Account).limit(1))).scalars().first()
    db.add(Order(id=uuid.uuid4(), internal_order_id=f"EXT_{uuid.uuid4().hex[:8]}", account_id=master.id,
                 source=OrderSource.BROKER_EXTERNAL, exchange="NSE", symbol=symbol, side=OrderSide.BUY, order_type="LIMIT",
                 product_type="MIS", quantity=1, filled_quantity=0, pending_quantity=1, price=Decimal("1"),
                 status=OrderStatus.OPEN, created_at=now - timedelta(minutes=30)))
    await db.commit()
    return await shadow.run_once(now, catalogs=[AliceCatalog(), AngelCatalog(AngelInstrumentStore()),
                                                GrowwCatalog(groww or groww_store(groww_row()))])


# ── authentication ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_both_endpoints_require_the_internal_token(client, path):
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers={"X-Internal-Token": "wrong"})).status_code == 401
    assert (await client.get(path, headers=TOKEN)).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PATHS)
async def test_an_unset_server_token_closes_the_endpoints(client, monkeypatch, path):
    monkeypatch.setattr(settings, "INTERNAL_API_TOKEN", "")
    for headers in ({}, TOKEN, {"X-Internal-Token": ""}):
        assert (await client.get(path, headers=headers)).status_code == 503


@pytest.mark.asyncio
async def test_only_get_is_offered(client):
    for path in ("/api/v1/instruments/compare", "/api/v1/instruments/shadow-report"):
        for method in ("post", "put", "delete", "patch"):
            assert (await getattr(client, method)(path, headers=TOKEN)).status_code == 405


# ── compare ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_explicit_isin_can_produce_a_real_match_and_writes_nothing(client, groww_loaded, db_session):
    events_before, orders_before = await count(SystemEvent), await count(Order)
    r = await client.get(f"/api/v1/instruments/compare?exchange=nse&symbol=reliance-eq&isin={REL.lower()}", headers=TOKEN)
    assert r.status_code == 200 and r.json()["success"] is True
    data = r.json()["data"]
    assert data["input"] == {"exchange": "NSE", "symbol": "RELIANCE-EQ", "isin": REL}
    assert data["normalization"]["key"] == f"EQ|NSE|ISIN|{REL}" and data["min_evidence"] == "VERIFIED_DOCS"
    res = {x["broker_id"]: x for x in data["results"]}
    assert list(res) == ["alicebluenew", "angelone", "groww"]
    assert res["groww"]["verdict"] == "MATCH" and res["groww"]["group"] == "matched"
    assert res["groww"]["matched"]["isin"] == REL and "token" not in res["groww"]["matched"]
    assert res["alicebluenew"]["verdict"] == "CATALOG_NOT_LOADED"
    assert res["angelone"]["catalog_loaded"] is False                     # Angel's catalog is not loaded either
    assert await count(SystemEvent) == events_before and await count(Order) == orders_before


@pytest.mark.asyncio
async def test_without_an_isin_nothing_is_definitive(client, groww_loaded):
    data = (await client.get("/api/v1/instruments/compare?exchange=NSE&symbol=RELIANCE", headers=TOKEN)).json()["data"]
    groww = next(x for x in data["results"] if x["broker_id"] == "groww")
    assert (groww["verdict"], groww["reason"], groww["group"]) == ("UNMAPPED", "SYMBOL_ONLY_NOT_DEFINITIVE", "identity_unconfirmed")
    assert groww["candidate_count"] == 1 and data["normalization"]["confidence"] == "SYMBOL_ONLY"


@pytest.mark.asyncio
async def test_invalid_conflicting_and_derivative_inputs_are_unmapped_not_errors(client, groww_loaded):
    async def groww_of(query):
        data = (await client.get(f"/api/v1/instruments/compare?{query}", headers=TOKEN)).json()["data"]
        return next(x for x in data["results"] if x["broker_id"] == "groww")

    assert (await groww_of("exchange=NSE&symbol=RELIANCE&isin=INE002A01019"))["reason"] == "MALFORMED_ISIN"
    conflict = await groww_of("exchange=NSE&symbol=RELIANCE&isin=INE040A01034")      # a valid ISIN the store does not list for it
    shared = await groww_of(f"exchange=NSE&symbol=RELIANCE&isin={INFY}")             # INFY's ISIN, but RELIANCE shares the symbol
    assert (shared["verdict"], shared["reason"]) == ("AMBIGUOUS", "SYMBOL_SHARED_WITH_OTHER_ISIN")
    assert (conflict["verdict"], conflict["reason"], conflict["group"]) == ("UNMAPPED", "ISIN_CONFLICT", "identity_conflict")
    assert (await groww_of("exchange=NFO&symbol=NIFTY%2024500%20CE"))["reason"] == "DERIVATIVES_NOT_SUPPORTED_YET"
    assert (await groww_of("exchange=NSE&symbol=NIFTY"))["reason"] == "NOT_AN_EQUITY_INDEX"


@pytest.mark.asyncio
async def test_an_unloaded_groww_catalog_is_reported_as_not_loaded(client, monkeypatch):
    monkeypatch.setattr(groww_instruments, "store", groww_instruments.GrowwInstrumentStore())
    data = (await client.get(f"/api/v1/instruments/compare?exchange=NSE&symbol=RELIANCE&isin={REL}", headers=TOKEN)).json()["data"]
    assert {x["broker_id"]: x["verdict"] for x in data["results"]}["groww"] == "CATALOG_NOT_LOADED"


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["symbol=RELIANCE", "exchange=NSE", "exchange=&symbol=X", f"exchange=NSE&symbol={'A' * 61}",
                                   f"exchange={'N' * 21}&symbol=X", "exchange=NSE&symbol=X&isin=INE002A010180"])
async def test_compare_validates_its_parameters(client, query):
    assert (await client.get(f"/api/v1/instruments/compare?{query}", headers=TOKEN)).status_code == 422


# ── shadow report ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_empty_report_is_honest_about_coverage(client, groww_loaded):
    data = (await client.get("/api/v1/instruments/shadow-report", headers=TOKEN)).json()["data"]
    assert data["enabled"] is False and data["event_count"] == 0 and data["rows"] == [] and data["groups"] == {}
    assert data["truncated"] is False and data["window_hours"] == 24 and data["min_evidence"] == "VERIFIED_DOCS"
    states = {c["broker_id"]: c for c in data["catalogs"]}
    assert states["alicebluenew"]["loaded"] is False and "not loaded" in states["alicebluenew"]["note"]
    assert states["angelone"]["loaded"] is False and "no production loader" in states["angelone"]["note"].lower()
    assert states["groww"]["loaded"] is True and "equity" in states["groww"]["note"].lower()
    assert any("no ISIN" in n and "MATCH" in n for n in data["notes"])
    assert set(data["by_broker"]) == {"alicebluenew", "angelone", "groww"}


@pytest.mark.asyncio
async def test_the_report_reflects_the_enabled_flag(client, monkeypatch):
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_ENABLED", True)
    assert (await client.get("/api/v1/instruments/shadow-report", headers=TOKEN)).json()["data"]["enabled"] is True


@pytest.mark.asyncio
async def test_the_report_never_claims_parity_for_symbol_only_masters(client, db_session, sample_accounts, sample_strategy, groww_loaded, monkeypatch):
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_ENABLED", True)
    result = await seed_events(db_session, groww=groww_loaded)
    assert result.written == 3
    events_before = await count(SystemEvent)
    data = (await client.get("/api/v1/instruments/shadow-report", headers=TOKEN)).json()["data"]
    assert await count(SystemEvent) == events_before                      # reading writes nothing
    assert data["event_count"] == 3 and data["instrument_count"] == 1
    assert "matched" not in data["groups"] and "MATCH" not in data["by_verdict"]
    assert data["groups"] == {"catalog_not_loaded": 1, "identity_unconfirmed": 1, "segment_not_supported": 1}
    groww = data["by_broker"]["groww"]
    assert groww["instruments"] == 1 and groww["with_candidates"] == 1 and groww["groups"] == {"identity_unconfirmed": 1}
    assert data["by_broker"]["alicebluenew"]["groups"] == {"catalog_not_loaded": 1}
    assert all(set(r) >= {"broker_id", "verdict", "reason", "group", "candidate_count"} for r in data["rows"])
    assert "token" not in str(data).lower().replace("tokens", "")


@pytest.mark.asyncio
async def test_aggregates_use_the_latest_result_per_instrument_and_rows_list_the_history(client, db_session, sample_accounts, sample_strategy, groww_loaded, monkeypatch):
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_ENABLED", True)
    now = datetime.now(timezone.utc)
    await seed_events(db_session, now=now - timedelta(minutes=20), groww=groww_loaded)
    await shadow.run_once(now - timedelta(minutes=10), catalogs=[AliceCatalog(), AngelCatalog(AngelInstrumentStore()),
                          GrowwCatalog(groww_store(groww_row(), groww_row(isin=INFY)))])       # Groww now ambiguous
    data = (await client.get("/api/v1/instruments/shadow-report", headers=TOKEN)).json()["data"]
    assert data["event_count"] == 4 and data["instrument_count"] == 1
    assert data["by_broker"]["groww"]["groups"] == {"ambiguous": 1}
    assert data["by_broker"]["groww"]["instruments"] == 1
    assert [r["created_at"] for r in data["rows"]] == sorted((r["created_at"] for r in data["rows"]), reverse=True)


@pytest.mark.asyncio
async def test_the_window_excludes_old_events(client, db_session, sample_accounts, sample_strategy, groww_loaded, monkeypatch):
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_ENABLED", True)
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_WINDOW_HOURS", 400)
    await seed_events(db_session, now=datetime.now(timezone.utc) - timedelta(days=5), groww=groww_loaded)
    assert (await client.get("/api/v1/instruments/shadow-report?hours=24", headers=TOKEN)).json()["data"]["event_count"] == 0
    wide = (await client.get("/api/v1/instruments/shadow-report?hours=168", headers=TOKEN)).json()["data"]
    assert wide["event_count"] == 3 and wide["window_hours"] == 168


@pytest.mark.asyncio
async def test_hours_limit_and_offset_are_bounded_not_rejected(client):
    async def params(query):
        r = await client.get(f"/api/v1/instruments/shadow-report?{query}", headers=TOKEN)
        assert r.status_code == 200
        d = r.json()["data"]
        return d["window_hours"], d["pagination"]["limit"], d["pagination"]["offset"]

    assert await params("hours=100000&limit=100000&offset=999999999") == (168, 500, 100000)
    assert await params("hours=0&limit=0&offset=-5") == (1, 1, 0)
    assert await params("") == (24, 100, 0)
    for bad in ("hours=abc", "limit=1.5", "offset=x"):
        assert (await client.get(f"/api/v1/instruments/shadow-report?{bad}", headers=TOKEN)).status_code == 422


@pytest.mark.asyncio
async def test_pagination_pages_through_the_events(client, db_session, sample_accounts, sample_strategy, groww_loaded, monkeypatch):
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_ENABLED", True)
    now = datetime.now(timezone.utc)
    for i, sym in enumerate(("RELIANCE", "INFY")):
        await seed_events(db_session, symbol=sym, now=now - timedelta(minutes=10 - i), groww=groww_loaded)
    first = (await client.get("/api/v1/instruments/shadow-report?limit=4", headers=TOKEN)).json()["data"]
    second = (await client.get("/api/v1/instruments/shadow-report?limit=4&offset=4", headers=TOKEN)).json()["data"]
    assert first["pagination"] == {"limit": 4, "offset": 0, "returned": 4, "total": 6}
    assert second["pagination"]["returned"] == 2
    keys = [(r["created_at"], r["broker_id"], r["symbol"]) for r in first["rows"] + second["rows"]]
    assert len(set(keys)) == 6
    assert first["event_count"] == second["event_count"] == 6 and first["groups"] == second["groups"]   # aggregates ignore paging


@pytest.mark.asyncio
async def test_the_report_cap_marks_truncation(client, db_session, sample_accounts, sample_strategy, groww_loaded, monkeypatch):
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_ENABLED", True)
    await seed_events(db_session, groww=groww_loaded)
    monkeypatch.setattr(report, "REPORT_EVENT_CAP", 2)
    data = (await client.get("/api/v1/instruments/shadow-report", headers=TOKEN)).json()["data"]
    assert data["truncated"] is True and data["event_count"] == 2


@pytest.mark.asyncio
async def test_malformed_event_metadata_is_skipped_not_fatal(client, db_session):
    async with TestingSessionLocal() as s:
        s.add(SystemEvent(event_type=shadow.EVENT_TYPE, message="bad", metadata_payload=None))
        await s.commit()
    r = await client.get("/api/v1/instruments/shadow-report", headers=TOKEN)
    assert r.status_code == 200 and r.json()["data"]["event_count"] == 0


# ── grouping ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("verdict,reason,group", [
    ("MATCH", "ISIN_MATCH", "matched"),
    ("ATTRIBUTE_DIFF", "ATTRIBUTE_DIFF", "attribute_mismatch"),
    ("UNMAPPED", "ISIN_CONFLICT", "identity_conflict"),
    ("UNMAPPED", "SYMBOL_ONLY_NOT_DEFINITIVE", "identity_unconfirmed"),
    ("UNMAPPED", "CANDIDATE_LACKS_ISIN", "identity_unconfirmed"),
    ("UNMAPPED", "NOT_FOUND", "unmapped"),
    ("UNMAPPED", "DERIVATIVES_NOT_SUPPORTED_YET", "unmapped"),
    ("AMBIGUOUS", "MULTIPLE_CANDIDATES_FOR_ISIN", "ambiguous"),
    ("CATALOG_NOT_LOADED", "CATALOG_NOT_LOADED", "catalog_not_loaded"),
    ("MASTER_DATA_STALE", "LOAD_TIME_UNKNOWN", "catalog_stale"),
    ("SEGMENT_NOT_SUPPORTED", "NSE_CASH_NOT_SUPPORTED_AT_VERIFIED_DOCS", "segment_not_supported"),
    ("SOMETHING_NEW", "X", "unmapped"),
])
def test_existing_verdicts_are_grouped_for_the_report(verdict, reason, group):
    assert report.group_of(verdict, reason) == group


def test_a_job_event_with_an_attribute_difference_is_reported_as_a_mismatch():
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    payload = {"broker_id": "groww", "exchange": "NSE", "symbol": "X", "verdict": "ATTRIBUTE_DIFF", "reason": "ATTRIBUTE_DIFF",
               "candidate_count": 1, "catalog_loaded": True, "diffs": [{"attribute": "lot_size", "master": "1", "broker": "5"}]}
    data = report.build_report([(now, payload)], hours=24, limit=10, offset=0, truncated=False, now=now)
    assert data["groups"] == {"attribute_mismatch": 1} and data["rows"][0]["diffs"][0]["attribute"] == "lot_size"
