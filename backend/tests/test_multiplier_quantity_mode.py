"""
MULTIPLIER quantity mode: Child quantity = Master quantity x the Child's
strategy multiplier (StrategyMember.multiplier).

Before this mode existed the multiplier shown in the UI was dead: every Child
has a CopySetting (default FIXED, fixed_qty=1) and the pipeline only used the
multiplier when NO CopySetting existed. Live effect (2026-10-13): a Master
NIFTY order of 65 was copied as quantity 1 and rejected by Alice Blue with
EC954 "quantity should be a multiple of the lot size".

The policy is explicit and opt-in per Child - nothing here changes an
existing Child's mode, and nothing enables copying. The broker is always a
mock; no real order is ever placed.
"""
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.accounts import CopySetting, QuantityMode
from app.models.audit import SystemEvent
from app.models.strategies import StrategyMember
from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services.copy_engine.calculator import QuantityCalculator
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from conftest import TestingSessionLocal
from strategy_helpers import link_strategy
from test_strategy_master_switching import (  # noqa: F401  (api_client is a fixture)
    AUTH,
    _account,
    _child_client,
    _create_with_master,
    api_client,
)


def _calc(mode, master_qty, multiplier, *, fixed_qty=1, lot_size=1):
    setting = CopySetting(quantity_mode=mode, fixed_qty=fixed_qty)
    return QuantityCalculator.calculate_quantity(
        mode=mode, master_qty=master_qty, master_capital=Decimal("100000"),
        follower_capital=Decimal("100000"), lot_size=lot_size, settings=setting,
        reference_price=Decimal("100"), multiplier=multiplier,
    )


# ─── 1. the arithmetic ──────────────────────────────────────────────────────

def test_one_x_copies_the_master_quantity():
    assert _calc(QuantityMode.MULTIPLIER, 65, Decimal("1.00")) == 65


def test_two_x_doubles_the_master_quantity():
    assert _calc(QuantityMode.MULTIPLIER, 65, Decimal("2.00")) == 130


def test_each_partial_master_fill_is_scaled_and_the_parts_add_up():
    """MARKET fills arrive as INCREMENTAL quantities (20 then 45 of 65)."""
    parts = [_calc(QuantityMode.MULTIPLIER, q, Decimal("2")) for q in (20, 45)]
    assert parts == [40, 90] and sum(parts) == 130


def test_fractional_multiplier_floors_and_never_rounds_up():
    assert _calc(QuantityMode.MULTIPLIER, 3, Decimal("0.5")) == 1
    assert _calc(QuantityMode.MULTIPLIER, 1, Decimal("0.5")) == 0   # caller skips: ZERO_LOT_RESULT
    assert _calc(QuantityMode.MULTIPLIER, 10, Decimal("0.3")) == 3  # exact decimal, no float drift


def test_known_limitation_fractional_multiplier_is_not_lot_validated_without_lot_data():
    """The engine has no instrument lot sizes (lot_size=1 by default), so
    0.5 x 65 yields 32 - not a NIFTY lot multiple. It is NOT caught here; the
    broker rejects it. A configured lot size does floor it."""
    assert _calc(QuantityMode.MULTIPLIER, 65, Decimal("0.5")) == 32
    assert _calc(QuantityMode.MULTIPLIER, 65, Decimal("0.5"), lot_size=65) == 0
    assert _calc(QuantityMode.MULTIPLIER, 65, Decimal("2"), lot_size=65) == 130


def test_missing_or_invalid_multiplier_fails_closed_to_zero():
    assert _calc(QuantityMode.MULTIPLIER, 65, None) == 0
    assert _calc(QuantityMode.MULTIPLIER, 65, Decimal("0")) == 0
    assert _calc(QuantityMode.MULTIPLIER, 65, Decimal("-1")) == 0
    assert _calc(QuantityMode.MULTIPLIER, 65, Decimal("NaN")) == 0


def test_fixed_mode_is_unchanged_and_ignores_the_multiplier():
    assert _calc(QuantityMode.FIXED, 65, Decimal("2.00"), fixed_qty=1) == 1
    assert _calc(QuantityMode.FIXED, 65, None, fixed_qty=5) == 5


# ─── 2. through the copy pipeline ───────────────────────────────────────────

def _event(master, order_id, *, qty=65, side="BUY", order_type="MARKET", seq=None):
    is_limit = order_type == "LIMIT"
    return MasterTradeEvent(
        master_order_id=order_id, symbol="NIFTY", exchange="NFO", side=side, quantity=qty,
        price=Decimal("61.90"), limit_price=Decimal("61.90") if is_limit else Decimal("0"),
        product_type="NRML", timestamp=datetime.now(timezone.utc), account_id=str(master.id),
        order_type=order_type, event_type="LIMIT_CREATED" if is_limit else "MARKET_ACCEPTED",
        fill_sequence=1 if is_limit else (seq or qty), source="BROKER_EXTERNAL",
        status="OPEN" if is_limit else "COMPLETE", total_quantity=qty,
        filled_quantity=0 if is_limit else qty, pending_quantity=qty if is_limit else 0,
        instrument_id="44612",
    )


async def _setup(db_session, *, mode, multiplier, fixed_qty=1):
    master = await _account(db_session, "vikash")
    child = await _account(db_session, "dhines")  # CopySetting: FIXED, fixed_qty=1, copy_enabled
    await link_strategy(db_session, master, [(child, multiplier)], name="master")
    async with TestingSessionLocal() as s:
        cs = (await s.execute(select(CopySetting).where(CopySetting.follower_account_id == child.id))).scalar_one()
        cs.quantity_mode, cs.fixed_qty = mode, fixed_qty
        await s.commit()
    return master, child


def _placed_qty(client):
    call = client.place_order.call_args
    if "quantity" in call.kwargs:
        return call.kwargs["quantity"]
    for value in list(call.args) + list(call.kwargs.values()):
        if hasattr(value, "quantity"):
            return value.quantity
    raise AssertionError(f"could not find the placed quantity in {call}")


@pytest.mark.asyncio
@pytest.mark.parametrize("order_type", ["MARKET", "LIMIT"])
@pytest.mark.parametrize("multiplier,expected", [("1.00", 65), ("2.00", 130)])
async def test_multiplier_child_receives_master_qty_times_multiplier(
    db_session, mock_redis, order_type, multiplier, expected
):
    master, child = await _setup(db_session, mode=QuantityMode.MULTIPLIER, multiplier=multiplier)
    client = _child_client("BRK_1")
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_event(master, f"M_{order_type}_{expected}", order_type=order_type), clients={str(child.id): client}
    )
    assert res["results"][str(child.id)]["status"] == "SUCCESS", res
    client.place_order.assert_awaited_once()
    assert _placed_qty(client) == expected
    async with TestingSessionLocal() as s:
        row = (await s.execute(select(Order).where(Order.account_id == child.id))).scalar_one()
    assert row.quantity == expected


@pytest.mark.asyncio
async def test_fixed_child_still_copies_its_fixed_quantity_whatever_the_multiplier(db_session, mock_redis):
    """Existing Children keep FIXED until changed explicitly: the live
    behaviour (qty 1 for a Master order of 65) is preserved, not migrated."""
    master, child = await _setup(db_session, mode=QuantityMode.FIXED, multiplier="2.00", fixed_qty=1)
    client = _child_client("BRK_F")
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await pipeline.execute_replication(event=_event(master, "M_FIXED"), clients={str(child.id): client})
    assert _placed_qty(client) == 1


@pytest.mark.asyncio
async def test_repeated_partial_master_fills_each_copy_their_own_scaled_quantity(db_session, mock_redis):
    master, child = await _setup(db_session, mode=QuantityMode.MULTIPLIER, multiplier="2.00")
    client = _child_client("BRK_P")
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    quantities = []
    for incremental, cumulative in ((20, 20), (45, 65)):
        client.place_order.reset_mock()
        res = await pipeline.execute_replication(
            event=_event(master, "M_PARTIAL", qty=incremental, seq=cumulative), clients={str(child.id): client}
        )
        assert res["results"][str(child.id)]["status"] == "SUCCESS", res
        quantities.append(_placed_qty(client))
    assert quantities == [40, 90]


@pytest.mark.asyncio
async def test_sell_is_sized_by_the_multiplier_but_still_needs_the_childs_real_position(
    db_session, mock_redis, monkeypatch
):
    """A square-off is NOT assumed to equal the earlier BUY: the SELL is
    Master SELL qty x multiplier, and the existing position-safety gate then
    rejects the WHOLE copy when the Child's broker position does not cover
    it (no partial sell, no short)."""
    from app.core.config import settings as app_settings
    monkeypatch.setattr(app_settings, "SELL_POSITION_SAFETY_BYPASS", False)
    master, child = await _setup(db_session, mode=QuantityMode.MULTIPLIER, multiplier="2.00")
    client = _child_client("BRK_S")          # broker reports no positions at all
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_event(master, "M_SELL", side="SELL"), clients={str(child.id): client}
    )
    assert res["results"][str(child.id)]["status"] == "REJECTED", res
    client.place_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("already_filled,expected_remaining", [(0, 200), (130, 70)])
async def test_master_quantity_modification_resizes_the_child_by_the_multiplier(
    db_session, mock_redis, already_filled, expected_remaining
):
    """Master raises its order from 65 to 100 at 2x: the Child's new target
    is 200, and only what the Child has NOT already filled is sent to the
    broker (the filled part is never touched)."""
    from test_modification_after_master_switch import _client as _modify_client

    master, child = await _setup(db_session, mode=QuantityMode.MULTIPLIER, multiplier="2.00")
    async with TestingSessionLocal() as s:
        s.add(Order(internal_order_id="JT_CH_M", broker_order_id="CH_M", account_id=child.id,
                    master_broker_order_id="M_MOD", source=OrderSource.SYSTEM, exchange="NFO", symbol="NIFTY",
                    side=OrderSide.BUY, order_type="LIMIT", product_type="NRML", quantity=130,
                    filled_quantity=already_filled, pending_quantity=130 - already_filled,
                    price=Decimal("61.90"), status=OrderStatus.PARTIAL if already_filled else OrderStatus.OPEN))
        await s.commit()
    client = _modify_client("CH_M")
    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await pipeline.handle_master_modification(
        master_order_id="M_MOD", new_price=Decimal("62.00"), new_quantity=100,
        clients={str(child.id): client}, master_account_id=str(master.id),
    )
    client.modify_order.assert_awaited_once()
    assert client.modify_order.call_args.kwargs["quantity"] == expected_remaining


# ─── 3. the explicit policy endpoint ────────────────────────────────────────

async def _strategy_with_child(api_client, db_session):
    master = await _account(db_session, "m")
    child = await _account(db_session, "c")
    sid, resp = await _create_with_master(api_client, "S", master.id)
    assert resp.status_code == 200, resp.text
    resp = await api_client.post(f"/api/v1/strategies/{sid}/members",
                                 json={"account_id": str(child.id), "multiplier": "2.00"}, headers=AUTH)
    assert resp.status_code == 201
    return sid, master, child


async def _setting(account_id):
    async with TestingSessionLocal() as s:
        return (await s.execute(select(CopySetting).where(CopySetting.follower_account_id == account_id))).scalar_one()


@pytest.mark.asyncio
async def test_adding_a_child_does_not_change_its_existing_quantity_mode(api_client, db_session):
    _, _, child = await _strategy_with_child(api_client, db_session)
    cs = await _setting(child.id)
    assert cs.quantity_mode == QuantityMode.FIXED and cs.fixed_qty == 1


@pytest.mark.asyncio
async def test_policy_is_switched_only_by_an_explicit_request_and_is_audited(api_client, db_session):
    sid, _, child = await _strategy_with_child(api_client, db_session)
    url = f"/api/v1/strategies/{sid}/members/{child.id}/quantity-policy"
    resp = await api_client.put(url, json={"quantity_mode": "MULTIPLIER"}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert resp.json()["quantity_mode"] == "MULTIPLIER" and Decimal(resp.json()["multiplier"]) == Decimal("2.00")
    cs = await _setting(child.id)
    assert cs.quantity_mode == QuantityMode.MULTIPLIER and cs.fixed_qty == 1  # fixed_qty kept for switching back
    async with TestingSessionLocal() as s:
        event = (await s.execute(select(SystemEvent).where(
            SystemEvent.event_type == "CHILD_QUANTITY_POLICY_CHANGED"))).scalar_one()
    assert "FIXED" in event.message and "MULTIPLIER" in event.message

    resp = await api_client.put(url, json={"quantity_mode": "FIXED", "fixed_qty": 65}, headers=AUTH)
    assert resp.status_code == 200
    cs = await _setting(child.id)
    assert cs.quantity_mode == QuantityMode.FIXED and cs.fixed_qty == 65


@pytest.mark.asyncio
async def test_policy_change_can_set_the_multiplier_and_validates_input(api_client, db_session):
    sid, _, child = await _strategy_with_child(api_client, db_session)
    url = f"/api/v1/strategies/{sid}/members/{child.id}/quantity-policy"
    resp = await api_client.put(url, json={"quantity_mode": "MULTIPLIER", "multiplier": "3"}, headers=AUTH)
    assert resp.status_code == 200
    async with TestingSessionLocal() as s:
        member = (await s.execute(select(StrategyMember).where(StrategyMember.account_id == child.id))).scalar_one()
    assert member.multiplier == Decimal("3")
    for bad in ({"quantity_mode": "RATIO"}, {"quantity_mode": "MULTIPLIER", "multiplier": "0"},
                {"quantity_mode": "FIXED", "fixed_qty": 0}, {}):
        assert (await api_client.put(url, json=bad, headers=AUTH)).status_code == 422, bad


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_policy_change_never_turns_copying_on_or_off(api_client, db_session, enabled):
    sid, _, child = await _strategy_with_child(api_client, db_session)
    resp = await api_client.put(f"/api/v1/strategies/{sid}/members/{child.id}",
                                json={"trading_enabled": enabled}, headers=AUTH)
    assert resp.status_code == 200
    resp = await api_client.put(f"/api/v1/strategies/{sid}/members/{child.id}/quantity-policy",
                                json={"quantity_mode": "MULTIPLIER"}, headers=AUTH)
    assert resp.status_code == 200
    assert (await _setting(child.id)).copy_enabled is enabled
    async with TestingSessionLocal() as s:
        member = (await s.execute(select(StrategyMember).where(StrategyMember.account_id == child.id))).scalar_one()
    assert member.trading_enabled is enabled


@pytest.mark.asyncio
async def test_policy_change_is_refused_while_the_child_has_a_working_copied_order(api_client, db_session):
    sid, _, child = await _strategy_with_child(api_client, db_session)
    async with TestingSessionLocal() as s:
        s.add(Order(internal_order_id="INT_W", broker_order_id="B_W", account_id=child.id, source=OrderSource.SYSTEM,
                    symbol="NIFTY", exchange="NFO", side=OrderSide.BUY, order_type="LIMIT", product_type="NRML",
                    quantity=1, pending_quantity=1, status=OrderStatus.OPEN))
        await s.commit()
    resp = await api_client.put(f"/api/v1/strategies/{sid}/members/{child.id}/quantity-policy",
                                json={"quantity_mode": "MULTIPLIER"}, headers=AUTH)
    assert resp.status_code == 409
    assert (await _setting(child.id)).quantity_mode == QuantityMode.FIXED


@pytest.mark.asyncio
async def test_unknown_member_is_rejected_without_creating_a_copy_setting(api_client, db_session):
    sid, _, _ = await _strategy_with_child(api_client, db_session)
    stranger = await _account(db_session, "x", copy_setting=False)
    resp = await api_client.put(f"/api/v1/strategies/{sid}/members/{stranger.id}/quantity-policy",
                                json={"quantity_mode": "MULTIPLIER"}, headers=AUTH)
    assert resp.status_code == 404
    async with TestingSessionLocal() as s:
        assert (await s.execute(select(CopySetting).where(
            CopySetting.follower_account_id == stranger.id))).scalar_one_or_none() is None


# ─── 4. the migration only adds the enum value ──────────────────────────────

def test_migration_0009_adds_the_value_and_touches_no_rows():
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "0009_quantity_mode_multiplier.py"
    source = path.read_text()
    assert "down_revision: Union[str, None] = '0008_strategy_account_roles'" in source
    assert "ADD VALUE IF NOT EXISTS 'MULTIPLIER'" in source
    upgrade_body = source.split("def upgrade()")[1].split("def downgrade()")[0].upper()
    for forbidden in ("UPDATE ", "INSERT ", "DELETE ", "COPY_ENABLED"):
        assert forbidden not in upgrade_body
