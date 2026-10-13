"""
Regression: options (F&O) entry copied to a Child with qty 1 -> Alice Blue EC954.

Live evidence (2026-10-13): Master order NIFTY 65 was copied to the Child as
quantity 1 and rejected with "'quantity' should be a multiple of the lot size".
Until 2026-10-12 every NIFTY copy went out as 65 and filled. What changed:

  A Child that had NO CopySetting (an account created as MASTER, later made a
  Child) sized its copy with the strategy multiplier (Master qty x 1.0 = 65).
  Master switching started auto-creating a CopySetting for such a Child
  (migration 0008 backfill / strategy_membership.sync_copy_enabled) with the
  account-creation defaults quantity_mode=FIXED, fixed_qty=1 - and a
  CopySetting makes the pipeline ignore the multiplier, so the Child went from
  65 to 1.

The auto-created CopySetting must keep the quantity the Child had without one:
Master qty x the Child's strategy multiplier (MULTIPLIER mode).

The broker is always a mock; no order is ever placed.
"""
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.accounts import AccountType, CopySetting, QuantityMode
from app.services import strategy_membership, strategy_switch
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from conftest import TestingSessionLocal
from strategy_helpers import link_strategy
from test_multiplier_quantity_mode import _event, _placed_qty
from test_strategy_master_switching import (  # noqa: F401  (fixtures)
    AUTH,
    _account,
    _child_client,
    _create_with_master,
    _provider,
    api_client,
    trio,
)


async def _setting(account_id):
    async with TestingSessionLocal() as s:
        return (await s.execute(select(CopySetting).where(CopySetting.follower_account_id == account_id))).scalar_one()


# ─── the legacy behaviour the regression must not change ────────────────────

@pytest.mark.asyncio
async def test_child_without_a_copy_setting_copies_master_qty_times_multiplier(db_session, mock_redis):
    """The behaviour that worked: no CopySetting -> 65 x 1.0 = 65."""
    master = await _account(db_session, "vikash")
    child = await _account(db_session, "dhines", AccountType.MASTER, copy_setting=False)
    await link_strategy(db_session, master, [(child, "1.0")], name="legacy")
    client = _child_client("BRK_LEGACY")
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=_event(master, "M_LEGACY"), clients={str(child.id): client})
    assert res["results"][str(child.id)]["status"] == "SUCCESS", res
    assert _placed_qty(client) == 65


# ─── the regression: adding a Child through the API ─────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("multiplier,expected", [("1.00", 65), ("2.00", 130)])
async def test_child_added_via_api_without_a_copy_setting_still_gets_master_qty(
    api_client, db_session, mock_redis, multiplier, expected
):
    master = await _account(db_session, "vikash")
    child = await _account(db_session, "dhines", AccountType.MASTER, copy_setting=False)
    sid, resp = await _create_with_master(api_client, "master", master.id)
    assert resp.status_code == 200, resp.text
    resp = await api_client.post(
        f"/api/v1/strategies/{sid}/members",
        json={"account_id": str(child.id), "multiplier": multiplier},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text

    created = await _setting(child.id)
    assert created.quantity_mode == QuantityMode.MULTIPLIER, (
        "an auto-created CopySetting must size by the strategy multiplier, not FIXED 1"
    )
    assert created.copy_enabled is True

    client = _child_client("BRK_API")
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(event=_event(master, "M_API"), clients={str(child.id): client})
    assert res["results"][str(child.id)]["status"] == "SUCCESS", res
    assert _placed_qty(client) == expected, f"NIFTY 65 at {multiplier}x must be copied as {expected}"


# ─── the regression: old Master kept as a Child by a Master switch ──────────

@pytest.mark.asyncio
async def test_old_master_kept_as_child_after_switch_copies_master_qty(db_session, mock_redis, trio):
    """Dhinesh (Master, no CopySetting) is switched out and kept as a Child;
    Vikash becomes Master. The 2026-10-13 NIFTY orders were exactly this."""
    assert (await db_session.execute(
        select(CopySetting).where(CopySetting.follower_account_id == trio.dhinesh.id)
    )).scalar_one_or_none() is None

    await strategy_switch.execute_switch(
        db_session, trio.strategy.id, trio.vikash.id, "CHILD", _provider(trio.brokers)
    )

    created = await _setting(trio.dhinesh.id)
    assert created.quantity_mode == QuantityMode.MULTIPLIER
    assert created.copy_enabled is True

    client = _child_client("BRK_SWITCH")
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_event(trio.vikash, "M_SWITCH"), clients={str(trio.dhinesh.id): client}
    )
    assert res["results"][str(trio.dhinesh.id)]["status"] == "SUCCESS", res
    assert _placed_qty(client) == 65


# ─── safety: nothing that already has settings is touched ───────────────────

@pytest.mark.asyncio
async def test_an_existing_copy_setting_keeps_its_quantity_policy(db_session):
    child = await _account(db_session, "configured")  # CopySetting: FIXED, fixed_qty=1
    async with TestingSessionLocal() as s:
        cs = (await s.execute(select(CopySetting).where(CopySetting.follower_account_id == child.id))).scalar_one()
        cs.quantity_mode, cs.fixed_qty, cs.copy_enabled = QuantityMode.FIXED, 5, False
        await s.commit()

    async with TestingSessionLocal() as s:
        created = await strategy_membership.sync_copy_enabled(s, child.id, True)
        await s.commit()
    assert created is False

    cs = await _setting(child.id)
    assert (cs.quantity_mode, cs.fixed_qty, cs.copy_enabled) == (QuantityMode.FIXED, 5, True)


@pytest.mark.asyncio
async def test_auto_created_copy_setting_keeps_fixed_qty_available_and_copying_follows_the_toggle(db_session):
    child = await _account(db_session, "bare", AccountType.MASTER, copy_setting=False)
    async with TestingSessionLocal() as s:
        assert await strategy_membership.sync_copy_enabled(s, child.id, False) is True
        await s.commit()
    cs = await _setting(child.id)
    assert cs.copy_enabled is False          # Trading OFF -> never enables copying on its own
    assert cs.quantity_mode == QuantityMode.MULTIPLIER
    assert cs.fixed_qty == 1                 # still there if the user switches the policy to FIXED
    assert cs.max_trade_value is None and cs.max_daily_loss is None and cs.max_exposure is None
