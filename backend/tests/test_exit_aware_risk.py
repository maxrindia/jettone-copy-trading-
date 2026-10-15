"""
Exit-aware risk validation (2026-10-15 NIFTY incident).

Live evidence: the Master bought 65 NIFTY (copied to the Child), then sold 65.
The Child's copy of the SELL was rejected with INSUFFICIENT_MARGIN: RiskEngine's
margin check is side-agnostic, so a SELL that only CLOSES the Child's existing
long needed free margin equal to its full value, which the BUY had just used.

Fix: when the ONLY risk failures are margin and/or max exposure, and the
Child's broker-confirmed long for the exact instrument + product covers the
whole SELL quantity, those two checks are skipped. Anything else (no long, a
smaller long, unreadable position, a BUY, emergency stop, copy disabled, ...)
keeps the existing behaviour. SELL_POSITION_SAFETY_BYPASS stays false.
The broker is always a mock.
"""
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.accounts import Account, CopySetting
from app.models.audit import AuditLog
from app.models.trades import OrderSide as DbOrderSide
from app.services.alice_blue.types import FundsData
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.risk import RiskEngine
from app.services.copy_engine.validator import EventValidator
from conftest import TestingSessionLocal
from test_order_handling_matrix import NIFTY_TOKEN, _child, _event, _placed, _pos, _run, _setup

LOW_MARGIN = Decimal("1000")          # far below 65 x ~95-105


@pytest.fixture(autouse=True)
def _safety_on(monkeypatch):
    monkeypatch.setattr(settings, "SELL_POSITION_SAFETY_BYPASS", False)


def _funds(margin):
    return FundsData(account_id="f", available_margin=Decimal(margin), collateral=Decimal("0"),
                     used_margin=Decimal("0"), updated_at=datetime.now(timezone.utc))


def _low_margin_child(positions):
    client = _child(positions)
    client.get_funds.return_value = _funds(LOW_MARGIN)
    return client


def _incident_sell(master, order_id, order_type="LIMIT", side="SELL"):
    price, trigger = {"LIMIT": ("94.90", "0"), "MARKET": ("105.30", "0"), "SL": ("94.90", "106.10")}[order_type]
    return _event(master, order_id, side=side, order_type=order_type, product="INTRADAY", price=price,
                  trigger=trigger)


async def _risk_audits(child):
    async with TestingSessionLocal() as s:
        rows = (await s.execute(select(AuditLog).where(AuditLog.account_id == child.id,
                                                       AuditLog.event_type == "RISK_REJECTED"))).scalars().all()
    return [r.reason_code for r in rows]


# ─── pipeline end to end ────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("order_type", ["LIMIT", "MARKET", "SL"])
async def test_verified_exit_sell_is_copied_despite_low_margin(db_session, mock_redis, order_type):
    master, child = await _setup(db_session)
    client = _low_margin_child([_pos(65, "MIS")])          # Child holds the 65 it bought (intraday)
    out = await _run(mock_redis, master, child, client, _incident_sell(master, f"EXIT-{order_type}", order_type))
    assert out["status"] == "SUCCESS", out
    req = _placed(client)
    assert req.quantity == 65 and req.side.value == "SELL"
    assert await _risk_audits(child) == []


@pytest.mark.asyncio
async def test_sell_without_a_long_stays_rejected(db_session, mock_redis):
    master, child = await _setup(db_session)
    client = _low_margin_child([])
    out = await _run(mock_redis, master, child, client, _incident_sell(master, "NAKED"))
    assert out["status"] == "REJECTED"
    client.place_order.assert_not_called()
    assert await _risk_audits(child) == ["INSUFFICIENT_MARGIN"]


@pytest.mark.asyncio
async def test_partial_long_smaller_than_the_sell_stays_rejected(db_session, mock_redis):
    master, child = await _setup(db_session)
    client = _low_margin_child([_pos(30, "MIS")])
    out = await _run(mock_redis, master, child, client, _incident_sell(master, "PARTIAL"))
    assert out["status"] == "REJECTED"
    client.place_order.assert_not_called()
    assert await _risk_audits(child) == ["INSUFFICIENT_MARGIN"]


@pytest.mark.asyncio
async def test_a_long_in_another_product_does_not_count_as_an_exit(db_session, mock_redis):
    master, child = await _setup(db_session)
    client = _low_margin_child([_pos(65, "NRML")])     # carry holding; the SELL is intraday
    out = await _run(mock_redis, master, child, client, _incident_sell(master, "OTHER-PRODUCT"))
    assert out["status"] == "REJECTED"
    client.place_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unavailable", "raises"])
async def test_position_lookup_failure_fails_closed(db_session, mock_redis, failure):
    master, child = await _setup(db_session)
    client = _low_margin_child([_pos(65, "MIS")])
    if failure == "unavailable":
        client.get_positions_or_none.return_value = None
    else:
        client.get_positions_or_none.side_effect = RuntimeError("broker timeout")
    out = await _run(mock_redis, master, child, client, _incident_sell(master, f"POS-{failure}"))
    assert out["status"] == "REJECTED"
    client.place_order.assert_not_called()
    assert await _risk_audits(child) == ["INSUFFICIENT_MARGIN"]


@pytest.mark.asyncio
async def test_low_margin_buy_is_still_rejected_even_with_a_long(db_session, mock_redis):
    master, child = await _setup(db_session)
    client = _low_margin_child([_pos(65, "MIS")])
    out = await _run(mock_redis, master, child, client, _incident_sell(master, "BUY-LOW", side="BUY"))
    assert out["status"] == "REJECTED"
    client.place_order.assert_not_called()
    assert await _risk_audits(child) == ["INSUFFICIENT_MARGIN"]
    client.get_positions_or_none.assert_not_called()       # a BUY never triggers the exit lookup


@pytest.mark.asyncio
async def test_emergency_stop_still_blocks_a_verified_exit(db_session, mock_redis):
    master, child = await _setup(db_session)
    async with TestingSessionLocal() as s:
        (await s.get(Account, child.id)).is_emergency_stopped = True
        await s.commit()
    client = _low_margin_child([_pos(65, "MIS")])
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=_incident_sell(master, "ESTOP"), clients={str(child.id): client})
    # blocked before or by the risk engine - never placed, never exit-skipped
    client.place_order.assert_not_called()
    child_result = (res.get("results") or {}).get(str(child.id))
    assert child_result is None or child_result["status"] in ("REJECTED", "SKIPPED"), res


@pytest.mark.asyncio
async def test_copy_disabled_still_blocks_a_verified_exit(db_session, mock_redis):
    master, child = await _setup(db_session)
    async with TestingSessionLocal() as s:
        cs = (await s.execute(select(CopySetting).where(CopySetting.follower_account_id == child.id))).scalar_one()
        cs.copy_enabled = False
        await s.commit()
    client = _low_margin_child([_pos(65, "MIS")])
    out = await _run(mock_redis, master, child, client, _incident_sell(master, "PAUSED"))
    assert out["status"] in ("REJECTED", "SKIPPED"), out
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_sufficient_margin_sell_makes_no_extra_position_read_for_risk(db_session, mock_redis):
    """Ordinary copies pay no extra broker call: the exit lookup only runs
    after margin/exposure actually failed (the SELL safety gate still reads
    positions once, as before)."""
    master, child = await _setup(db_session)
    client = _child([_pos(65, "MIS")])                  # plenty of margin
    out = await _run(mock_redis, master, child, client, _incident_sell(master, "RICH"))
    assert out["status"] == "SUCCESS", out
    assert client.get_positions_or_none.await_count == 1


# ─── helper used by the modify / reprice / SL-trigger paths ─────────────────

def _risk_kwargs(account, cs, *, quantity=65, price=Decimal("105.30"), margin=LOW_MARGIN):
    return dict(account=account, settings=cs, symbol="NIFTY", quantity=quantity, price=price,
                funds=_funds(margin), daily_loss_today=Decimal("0"), open_exposure=Decimal("0"),
                follower_runtime_paused=False, global_emergency_stop=False)


def _acct(**kw):
    a = Account(display_name="c", alice_user_id="x", account_type="FOLLOWER")
    a.is_emergency_stopped = kw.get("estop", False)
    return a


def _cs(**kw):
    return CopySetting(copy_enabled=kw.get("enabled", True), max_exposure=kw.get("max_exposure"),
                       max_trade_value=kw.get("max_trade_value"), max_daily_loss=kw.get("max_daily_loss"),
                       allowed_symbols=None)


def _client(positions):
    c = AsyncMock()
    c.get_positions_or_none.return_value = positions
    return c


async def _exit_aware(client, side, **risk):
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=None))
    return await pipeline._evaluate_risk_exit_aware(
        client=client, side=side, exchange="NFO", product_type="INTRADAY", instrument_token=NIFTY_TOKEN, **risk
    )


@pytest.mark.asyncio
async def test_helper_accepts_the_db_order_side_enum_used_by_modify_paths():
    res = await _exit_aware(_client([_pos(65, "MIS")]), DbOrderSide.SELL, **_risk_kwargs(_acct(), _cs()))
    assert res.passed, res.failures


@pytest.mark.asyncio
async def test_helper_exposure_breach_on_a_verified_exit_passes():
    res = await _exit_aware(_client([_pos(65, "MIS")]), "SELL",
                            **_risk_kwargs(_acct(), _cs(max_exposure=Decimal("100")), margin=Decimal("1000000")))
    assert res.passed, res.failures


@pytest.mark.asyncio
async def test_helper_never_skips_other_failures():
    # margin fails AND max trade value fails: not exit-skippable as a whole -> unchanged result
    res = await _exit_aware(_client([_pos(65, "MIS")]), "SELL",
                            **_risk_kwargs(_acct(), _cs(max_trade_value=Decimal("10"))))
    assert not res.passed
    assert set(res.failures) == {"INSUFFICIENT_MARGIN", "MAX_TRADE_VALUE_BREACHED"}


@pytest.mark.asyncio
async def test_helper_daily_loss_still_blocks():
    risk = _risk_kwargs(_acct(), _cs(max_daily_loss=Decimal("100")))
    risk["daily_loss_today"] = Decimal("500")
    res = await _exit_aware(_client([_pos(65, "MIS")]), "SELL", **risk)
    assert "MAX_DAILY_LOSS_BREACHED" in res.failures


@pytest.mark.asyncio
async def test_helper_without_a_client_fails_closed():
    res = await _exit_aware(None, "SELL", **_risk_kwargs(_acct(), _cs()))
    assert res.failures == ["INSUFFICIENT_MARGIN"]


# ─── RiskEngine unit ────────────────────────────────────────────────────────

def test_reduces_position_skips_only_margin_and_exposure():
    acct, cs = _acct(estop=True), _cs(enabled=False, max_exposure=Decimal("1"), max_trade_value=Decimal("1"))
    kw = _risk_kwargs(acct, cs)
    plain = RiskEngine.evaluate(**kw)
    exit_ = RiskEngine.evaluate(**kw, reduces_position=True)
    assert {"INSUFFICIENT_MARGIN", "MAX_EXPOSURE_BREACHED"} <= set(plain.failures)
    assert "INSUFFICIENT_MARGIN" not in exit_.failures and "MAX_EXPOSURE_BREACHED" not in exit_.failures
    assert {"FOLLOWER_EMERGENCY_STOP", "FOLLOWER_PAUSED", "MAX_TRADE_VALUE_BREACHED"} <= set(exit_.failures)


def test_default_is_unchanged():
    kw = _risk_kwargs(_acct(), _cs())
    assert RiskEngine.evaluate(**kw).failures == ["INSUFFICIENT_MARGIN"]
