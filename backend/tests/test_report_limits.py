"""
R7: explicit truncation and clamp metadata on the shadow report and the strategy readiness coverage. A report that covers less than
it appears to must say so, which limit was reached, and must never look like a complete observation. Local database only.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.config import settings
from app.models.accounts import AccountType
from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services import strategy_readiness as sr
from app.services.instruments import crosswalk_evidence as ce
from app.services.instruments import report
from app.services.instruments import shadow
from conftest import TestingSessionLocal  # noqa: F401
from strategy_helpers import link_strategy
from test_crosswalk_evidence import make_file
from test_instrument_api import TOKEN, client, groww_loaded, seed_events  # noqa: F401  (fixtures)
from test_snapshot_rotation import D
from test_strategy_master_switching import AUTH, _account, api_client  # noqa: F401  (fixture)

NOW = datetime(2026, 10, 22, 6, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    ce.clear_cache()
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", "")
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_ENABLED", True)
    yield
    ce.clear_cache()


def comparison_event(i):
    return (NOW - timedelta(minutes=i), {
        "broker_id": "groww", "exchange": "NSE", "symbol": f"SYM{i}", "master_key": f"EQ|NSE|SYM|SYM{i}",
        "confidence": "SYMBOL_ONLY", "verdict": "UNMAPPED", "reason": "SYMBOL_ONLY_NOT_DEFINITIVE",
        "candidate_count": 1, "catalog_loaded": True, "catalog_loaded_at": None, "diffs": [], "unverified_attributes": [],
    })


def build(events=(), **kw):
    args = dict(hours=24, limit=100, offset=0, truncated=False, now=NOW)
    args.update(kw)
    return report.build_report(list(events), **args)


# ── comparison events ────────────────────────────────────────────────────────────────────

def test_the_comparison_event_cap_is_reported_and_marks_the_report_incomplete():
    data = build([comparison_event(i) for i in range(5)], truncated=True)
    ce_ = data["limits"]["comparison_events"]
    assert ce_ == {"read": 5, "cap": report.REPORT_EVENT_CAP, "truncated": True}
    assert data["truncated"] is True                                          # the existing field is unchanged
    assert data["complete"] is False and "COMPARISON_EVENT_CAP_REACHED" in data["incomplete_reasons"]


def test_a_report_under_every_cap_is_complete():
    data = build([comparison_event(i) for i in range(5)])
    assert data["complete"] is True and data["incomplete_reasons"] == []
    assert data["limits"]["comparison_events"]["truncated"] is False and data["limits"]["window"]["clamped"] is False


@pytest.mark.parametrize("event_cap,instruments,truncated", [(5, 1, False), (3, 1, False), (6, 2, False), (5, 2, True), (2, 2, True)])
@pytest.mark.asyncio
async def test_the_cap_boundary_through_the_api(client, db_session, sample_accounts, sample_strategy, groww_loaded, monkeypatch,
                                                event_cap, instruments, truncated):
    """Each seeded instrument writes three comparison events (one per broker): exactly the cap is complete, one over is truncated."""
    monkeypatch.setattr(report, "REPORT_EVENT_CAP", event_cap)
    for symbol in ("RELIANCE", "INFY")[:instruments]:
        await seed_events(db_session, symbol=symbol, groww=groww_loaded)
    data = (await client.get("/api/v1/instruments/shadow-report", headers=TOKEN)).json()["data"]
    assert data["limits"]["comparison_events"] == {"read": min(3 * instruments, event_cap), "cap": event_cap, "truncated": truncated}
    assert data["truncated"] is truncated and data["complete"] is (not truncated)


# ── crosswalk events ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cap,count,truncated", [(5, 4, False), (5, 5, False), (5, 6, True), (2, 6, True)])
@pytest.mark.asyncio
async def test_the_crosswalk_event_cap_now_has_a_flag(client, db_session, sample_accounts, sample_strategy, groww_loaded, tmp_path,
                                                      monkeypatch, cap, count, truncated):
    monkeypatch.setattr(settings, "INSTRUMENT_SHADOW_NSE_CROSSWALK_PATH", str(make_file(tmp_path, snapshot=datetime.now(timezone.utc).astimezone(
        __import__("app.services.instruments.nse_crosswalk", fromlist=["IST"]).IST).date())))
    master = sample_accounts["master"]
    for i in range(count):
        db_session.add(Order(id=uuid.uuid4(), internal_order_id=f"EXT_{i}", broker_order_id=f"b{i}", account_id=master.id,
                             source=OrderSource.BROKER_EXTERNAL, exchange="NSE", symbol=f"SYN{i}", side=OrderSide.BUY,
                             order_type="LIMIT", product_type="MIS", quantity=1, filled_quantity=0, pending_quantity=1,
                             price=Decimal("1"), status=OrderStatus.OPEN, created_at=datetime.now(timezone.utc) - timedelta(minutes=30),
                             instrument_token=str(9000100 + i)))
    await db_session.commit()
    result = await shadow.run_once(datetime.now(timezone.utc), catalogs=[])
    assert result.crosswalk_written == count
    monkeypatch.setattr(report, "REPORT_EVENT_CAP", cap)
    data = (await client.get("/api/v1/instruments/shadow-report", headers=TOKEN)).json()["data"]
    cw = data["limits"]["crosswalk_events"]
    assert cw["cap"] == cap and cw["truncated"] is truncated and cw["read"] == min(count, cap)
    assert data["crosswalk"]["events_truncated"] is truncated and data["crosswalk"]["events_read"] == min(count, cap)
    assert ("CROSSWALK_EVENT_CAP_REACHED" in data["incomplete_reasons"]) is truncated and data["complete"] is (not truncated)


def test_crosswalk_rows_cap_is_separate_from_completeness():
    events = [(NOW, {"exchange": "NSE", "symbol": f"S{i}", "master_token": str(i + 1), "state": "UNMAPPED", "reason": "X",
                     "candidate_isin": None, "crosswalk_series": None, "crosswalk_symbol": None, "conflicting_rows": 0, "snapshot": None})
              for i in range(ce.REPORT_ROW_CAP + 1)]
    data = build(crosswalk_events=events)
    rows = data["limits"]["crosswalk_rows"]
    assert rows == {"cap": ce.REPORT_ROW_CAP, "total": ce.REPORT_ROW_CAP + 1, "truncated": True}
    assert len(data["crosswalk"]["rows"]) == ce.REPORT_ROW_CAP and data["crosswalk"]["by_state"] == {"UNMAPPED": ce.REPORT_ROW_CAP + 1}
    assert data["complete"] is True                                           # counts cover every instrument; only the row list is short


# ── window and page clamps ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("asked,applied,clamped", [(24, 24, False), (168, 168, False), (169, 168, True), (100000, 168, True), (0, 1, True), (-5, 1, True), (1, 1, False)])
@pytest.mark.asyncio
async def test_window_clamps_are_reported(client, asked, applied, clamped):
    data = (await client.get(f"/api/v1/instruments/shadow-report?hours={asked}", headers=TOKEN)).json()["data"]
    w = data["limits"]["window"]
    assert w == {"requested_hours": asked, "applied_hours": applied, "max_hours": 168, "clamped": clamped} and data["window_hours"] == applied
    assert ("WINDOW_CLAMPED" in data["incomplete_reasons"]) is clamped and data["complete"] is (not clamped)


@pytest.mark.parametrize("limit,offset,lim_applied,off_applied,lim_c,off_c", [
    (100, 0, 100, 0, False, False), (500, 0, 500, 0, False, False), (501, 0, 500, 0, True, False), (0, 0, 1, 0, True, False),
    (10, -1, 10, 0, False, True), (10, 100000, 10, 100000, False, False), (10, 100001, 10, 100000, False, True)])
@pytest.mark.asyncio
async def test_page_clamps_and_has_more_are_reported(client, limit, offset, lim_applied, off_applied, lim_c, off_c):
    data = (await client.get(f"/api/v1/instruments/shadow-report?limit={limit}&offset={offset}", headers=TOKEN)).json()["data"]
    p = data["limits"]["page"]
    assert (p["limit_applied"], p["offset_applied"], p["limit_clamped"], p["offset_clamped"]) == (lim_applied, off_applied, lim_c, off_c)
    assert p["limit_requested"] == limit and p["offset_requested"] == offset and p["max_limit"] == 500 and p["max_offset"] == 100000
    assert data["pagination"]["limit"] == lim_applied and data["pagination"]["offset"] == off_applied      # existing semantics intact
    assert data["complete"] is True                                           # paging alone never makes a report incomplete


@pytest.mark.asyncio
async def test_has_more_follows_the_page(client, db_session, sample_accounts, sample_strategy, groww_loaded):
    await seed_events(db_session, groww=groww_loaded)                          # 3 events
    first = (await client.get("/api/v1/instruments/shadow-report?limit=2&offset=0", headers=TOKEN)).json()["data"]
    last = (await client.get("/api/v1/instruments/shadow-report?limit=2&offset=2", headers=TOKEN)).json()["data"]
    assert first["limits"]["page"]["has_more"] is True and first["limits"]["page"]["returned"] == 2
    assert last["limits"]["page"]["has_more"] is False and last["limits"]["page"]["returned"] == 1
    assert set(first) >= {"enabled", "generated_at", "window_hours", "event_count", "truncated", "groups", "rows", "pagination"}


def test_the_old_shape_is_unchanged_apart_from_additions():
    data = build([comparison_event(1)])
    legacy = {"enabled", "generated_at", "window_hours", "min_evidence", "event_count", "instrument_count", "truncated", "groups",
              "by_verdict", "by_reason", "by_broker", "catalogs", "notes", "pagination", "rows", "crosswalk"}
    assert legacy <= set(data) and {"limits", "complete", "incomplete_reasons", "liveness"} <= set(data)


# ── strategy readiness coverage ──────────────────────────────────────────────────────────

async def master_with_orders(db_session, count, hours=1.0):
    master = await _account(db_session, "master", AccountType.MASTER, copy_setting=False)
    strategy = await link_strategy(db_session, master, [], name="lim")
    for i in range(count):
        db_session.add(Order(id=uuid.uuid4(), internal_order_id=f"EXT_{i}", broker_order_id=f"b{i}", account_id=master.id,
                             source=OrderSource.BROKER_EXTERNAL, exchange="NSE", symbol=f"SYN{i}", side=OrderSide.BUY, order_type="LIMIT",
                             product_type="MIS", quantity=1, filled_quantity=0, pending_quantity=1, price=Decimal("1"),
                             status=OrderStatus.OPEN, created_at=datetime.now(timezone.utc) - timedelta(hours=hours),
                             instrument_token=str(9000100 + i)))
    await db_session.commit()
    return strategy


@pytest.mark.parametrize("orders,cap,truncated", [(2, 3, False), (3, 3, False), (4, 3, True)])
@pytest.mark.asyncio
async def test_the_order_cap_is_reported_for_readiness(api_client, db_session, monkeypatch, orders, cap, truncated):
    monkeypatch.setattr(sr, "MAX_ORDERS", cap)
    strategy = await master_with_orders(db_session, orders)
    cov = (await api_client.get(f"/api/v1/strategies/{strategy.id}/readiness", headers=AUTH)).json()["mapping_coverage"]
    assert cov["limits"]["orders"] == {"cap": cap, "read": min(orders, cap), "truncated": truncated} and cov["truncated"] is truncated
    assert cov["complete"] is (not truncated) and (("ORDER_CAP_REACHED" in cov["incomplete_reasons"]) is truncated)


@pytest.mark.parametrize("asked,applied,clamped", [(24, 24, False), (168, 168, False), (169, 168, True), (0, 1, True)])
@pytest.mark.asyncio
async def test_the_readiness_window_clamp_is_reported(api_client, db_session, asked, applied, clamped):
    strategy = await master_with_orders(db_session, 1)
    cov = (await api_client.get(f"/api/v1/strategies/{strategy.id}/readiness", params={"hours": asked}, headers=AUTH)).json()["mapping_coverage"]
    assert cov["limits"]["window"] == {"requested_hours": asked, "applied_hours": applied, "max_hours": 168, "clamped": clamped}
    assert cov["window_hours"] == applied and cov["complete"] is (not clamped)


@pytest.mark.asyncio
async def test_an_empty_window_is_never_complete(api_client, db_session):
    strategy = await master_with_orders(db_session, 1, hours=72)
    cov = (await api_client.get(f"/api/v1/strategies/{strategy.id}/readiness", headers=AUTH)).json()["mapping_coverage"]
    assert cov["status"] == "NO_RECENT_ORDERS" and cov["complete"] is False and "NO_RECENT_ORDERS" in cov["incomplete_reasons"]
