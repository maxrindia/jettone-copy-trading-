"""
audit_logs.reason_code is varchar(100). PostgreSQL rejects a longer value; SQLite does not, so the SQLite suite passed while the audit
write of an ambiguous-send outcome ("Order submission outcome unknown (...); not resent - reconcile at the broker") failed on
PostgreSQL. The pipeline now writes a short stable code (ORDER_OUTCOME_UNKNOWN) and keeps the full sanitized text in the audit row's
metadata. These tests do not rely on the database to enforce the length: they assert the stored value against the model's declared
column length, so they fail on SQLite too if the code regresses; on a PostgreSQL TEST_DATABASE_URL the database enforces it as well.
Offline only: a mocked HTTP transport, no broker, Redis, network or production access.
"""
import httpx
import pytest
from sqlalchemy import select

from app.models.audit import AuditLog
from app.models.trades import CopyTradeStatus, OrderStatus
from app.services.alice_blue.types import OrderResult, OrderResultCategory
from app.services.copy_engine.pipeline import (
    AUDIT_CODE_FAILED_SEE_DETAIL, AUDIT_CODE_OUTCOME_UNKNOWN, AUDIT_REASON_CODE_MAX, audit_reason_fields,
)
from conftest import TestingSessionLocal
from test_alice_blue_order_response_safety import AMBIGUOUS_TRANSPORTS, KINDS, run_kind
from test_alice_blue_send_findings import posts
from datetime import datetime, timezone

COLUMN_LIMIT = AuditLog.__table__.c.reason_code.type.length


def result(category, message):
    return OrderResult(category=category, client_order_id=None, broker_order_id=None, message=message, raw_response={},
                       timestamp=datetime.now(timezone.utc))


def test_the_helper_limit_matches_the_declared_column():
    assert COLUMN_LIMIT == 100 == AUDIT_REASON_CODE_MAX


# ── the pure helper ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("category", [OrderResultCategory.TIMEOUT, OrderResultCategory.UNKNOWN])
def test_an_unresolved_send_gets_the_stable_code_and_keeps_its_full_text(category):
    long_text = "Order submission outcome unknown (no usable broker order id, HTTP 200); not resent - reconcile at the broker"
    code, meta = audit_reason_fields(result(category, long_text), True)
    assert code == AUDIT_CODE_OUTCOME_UNKNOWN and len(code) <= COLUMN_LIMIT
    assert meta["category"] == category.value
    if category is OrderResultCategory.TIMEOUT:
        assert meta["detail"] == long_text                                  # the diagnostic is kept, not truncated away
    else:
        assert "outcome unknown" in meta["detail"]                          # UNKNOWN stays sanitized (no raw exception text)


def test_success_has_no_reason():
    assert audit_reason_fields(result(OrderResultCategory.SUCCESS, "ok"), False) == (None, None)


@pytest.mark.parametrize("message", ["RMS:Margin Exceeds", "x" * 100])
def test_a_definite_failure_that_fits_keeps_its_message_as_the_code_unchanged(message):
    assert audit_reason_fields(result(OrderResultCategory.REJECTED, message), False) == (message, None)


@pytest.mark.parametrize("length", [101, 250, 5000])
def test_a_definite_failure_that_does_not_fit_gets_a_stable_code_and_the_text_in_metadata(length):
    message = "Y" * length
    code, meta = audit_reason_fields(result(OrderResultCategory.REJECTED, message), False)
    assert code == AUDIT_CODE_FAILED_SEE_DETAIL and len(code) <= COLUMN_LIMIT
    assert meta["detail"] == message[:300] and meta["category"] == OrderResultCategory.REJECTED.value


@pytest.mark.parametrize("message", [None, ""])
def test_a_failure_without_a_message_has_no_code_and_no_error(message):
    assert audit_reason_fields(result(OrderResultCategory.REJECTED, message), False) == (None, None)


# ── end to end through the real Alice client, the pipeline and the audit table ────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("name", list(AMBIGUOUS_TRANSPORTS))
async def test_an_ambiguous_send_writes_its_audit_row_stays_unresolved_and_posts_once(
        db_session, mock_redis, sample_accounts, sample_strategy, kind, name):
    mid = f"AUD_{kind}_{name}"
    http, outcome, trade, order = await run_kind(db_session, mock_redis, sample_accounts, kind, mid, **AMBIGUOUS_TRANSPORTS[name])
    # exactly one placement POST and the outcome is still unresolved
    assert len(posts(http)) == 1
    assert outcome["status"] == "TIMEOUT"
    assert trade.status == CopyTradeStatus.TIMEOUT and order.status == OrderStatus.PENDING and order.broker_order_id is None
    async with TestingSessionLocal() as s:
        all_rows = (await s.execute(select(AuditLog).where(AuditLog.master_order_id == mid))).scalars().all()
    assert all(r.reason_code is None or len(r.reason_code) <= COLUMN_LIMIT for r in all_rows)
    if kind == "SL":
        # The SL placement path writes no per-Child COPY_FAILED audit row (only the fan-out start / summary rows); that is
        # existing behaviour and unchanged here. Nothing it writes may exceed the column.
        return
    # LIMIT / MARKET: the audit row WAS written (before the fix PostgreSQL raised and no COPY_FAILED row existed), with a short code
    rows = [r for r in all_rows if r.event_type == "COPY_FAILED"]
    assert len(rows) == 1
    row = rows[0]
    assert row.reason_code == AUDIT_CODE_OUTCOME_UNKNOWN
    assert len(row.reason_code) <= COLUMN_LIMIT                              # asserted here, so SQLite catches a regression too
    assert row.metadata_payload["detail"].startswith("Order submission outcome unknown") or "unknown" in row.metadata_payload["detail"].lower()
    assert row.metadata_payload["category"] in ("TIMEOUT", "UNKNOWN")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_no_audit_reason_in_any_ambiguous_or_failed_path_exceeds_the_column(db_session, mock_redis, sample_accounts, sample_strategy, kind):
    mid = f"AUD_LEN_{kind}"
    await run_kind(db_session, mock_redis, sample_accounts, kind, mid, side_effect=httpx.ReadError("reset"))
    async with TestingSessionLocal() as s:
        rows = (await s.execute(select(AuditLog).where(AuditLog.master_order_id == mid))).scalars().all()
    assert rows and all(r.reason_code is None or len(r.reason_code) <= COLUMN_LIMIT for r in rows)


# ── the one consumer that shows reason_code to the operator ──────────────────────────────────────

def test_dashboard_issue_text_still_shows_the_detail_for_the_new_codes_and_is_unchanged_otherwise():
    from app.api.dashboard import _audit_issue_message
    unknown = AuditLog(event_type="COPY_FAILED", symbol="RELIANCE", side="BUY", quantity=10, reason_code=AUDIT_CODE_OUTCOME_UNKNOWN,
                       metadata_payload={"detail": "Order submission outcome unknown (HTTP 503); not resent - reconcile at the broker"})
    assert _audit_issue_message(unknown) == (
        "COPY_FAILED — RELIANCE BUY x10 — ORDER_OUTCOME_UNKNOWN — Order submission outcome unknown (HTTP 503); not resent - reconcile at the broker")
    # other audit rows that happen to carry a metadata "detail" (e.g. pair-gate skips) are NOT changed
    skip = AuditLog(event_type="COPY_SKIPPED", symbol="RELIANCE", reason_code="BROKER_PAIR_NOT_SUPPORTED",
                    metadata_payload={"detail": "alicebluenew -> angelone", "child_broker": "angelone"})
    assert _audit_issue_message(skip) == "COPY_SKIPPED — RELIANCE — BROKER_PAIR_NOT_SUPPORTED"
    plain = AuditLog(event_type="COPY_FAILED", symbol="X", reason_code="RMS:Margin Exceeds", metadata_payload=None)
    assert _audit_issue_message(plain) == "COPY_FAILED — X — RMS:Margin Exceeds"
