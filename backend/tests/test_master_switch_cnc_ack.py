"""
Master switch with open CNC / intraday positions.

CNC (delivery) positions no longer block a switch: they stay open in Alice
Blue (Jettone never closes or transfers them), so the switch needs the user's
explicit acknowledgement, enforced by the engine. Intraday positions are
informational only. Carry (NRML) and unclassifiable products still block, an
unreadable position book still fails closed, and every pending-order safeguard
is unchanged. The broker is always a mock.
"""
import pytest
from sqlalchemy import select

from app.models.strategies import GroupStrategy
from app.services import strategy_switch
from conftest import TestingSessionLocal
from test_strategy_master_switching import (  # noqa: F401  (api_client and trio are fixtures)
    AUTH,
    _Broker,
    _codes,
    _pos,
    _preflight,
    _provider,
    api_client,
    trio,
)

URL = "/api/v1/strategies/{sid}/switch-master"


async def _master(strategy_id):
    async with TestingSessionLocal() as s:
        row = (await s.execute(select(GroupStrategy).where(GroupStrategy.id == strategy_id))).scalar_one()
        return row.master_account_id


def _cnc(trio, qty=10):
    trio.brokers[str(trio.vikash.id)]._positions = [_pos(qty=qty, product="CNC")]


# ─── preflight ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cnc_position_is_a_warning_that_requires_acknowledgement(db_session, trio):
    _cnc(trio)
    pre = await _preflight(db_session, trio, trio.arun)
    assert pre.allowed and pre.blockers == []
    assert pre.requires_cnc_acknowledgement is True
    cnc = [w for w in pre.warnings if w.code == "CNC_POSITIONS_OPEN"]
    assert len(cnc) == 1 and cnc[0].count == 1 and cnc[0].account_name == "vikash"
    assert "RELIANCE-EQ" in cnc[0].items[0]


@pytest.mark.asyncio
async def test_intraday_only_is_informational_and_needs_no_acknowledgement(db_session, trio):
    trio.brokers[str(trio.vikash.id)]._positions = [_pos(qty=5, product="MIS")]
    pre = await _preflight(db_session, trio, trio.arun)
    assert pre.allowed and pre.requires_cnc_acknowledgement is False
    assert [w.code for w in pre.warnings if w.code.endswith("POSITIONS_OPEN")] == ["INTRADAY_POSITIONS_OPEN"]


@pytest.mark.asyncio
async def test_no_positions_is_unchanged(db_session, trio):
    pre = await _preflight(db_session, trio, trio.arun)
    assert pre.allowed and pre.requires_cnc_acknowledgement is False
    assert not [w for w in pre.warnings if w.code.endswith("POSITIONS_OPEN")]


@pytest.mark.asyncio
@pytest.mark.parametrize("product", ["NRML", "WEIRD_PRODUCT"])
async def test_carry_and_unknown_products_still_block(db_session, trio, product):
    trio.brokers[str(trio.vikash.id)]._positions = [_pos(qty=2, product=product)]
    pre = await _preflight(db_session, trio, trio.arun)
    assert not pre.allowed
    assert {b.code for b in pre.blockers} == {"OPEN_POSITIONS"}


@pytest.mark.asyncio
async def test_a_blocking_position_alongside_cnc_still_blocks(db_session, trio):
    trio.brokers[str(trio.vikash.id)]._positions = [
        _pos(qty=10, product="CNC"), _pos(symbol="X", qty=1, product="NRML"),
    ]
    pre = await _preflight(db_session, trio, trio.arun)
    assert not pre.allowed
    assert [(b.code, b.count) for b in pre.blockers] == [("OPEN_POSITIONS", 1)]


@pytest.mark.asyncio
async def test_position_lookup_failure_fails_closed(db_session, trio):
    trio.brokers[str(trio.vikash.id)]._positions_error = RuntimeError("502")
    pre = await _preflight(db_session, trio, trio.arun)
    assert not pre.allowed
    assert ("BROKER_VERIFICATION_UNAVAILABLE", str(trio.vikash.id)) in _codes(pre)
    assert pre.requires_cnc_acknowledgement is False   # unknown is never read as "no positions" or "CNC only"
    with pytest.raises(strategy_switch.SwitchBlocked):
        await strategy_switch.execute_switch(db_session, trio.strategy.id, trio.arun.id, "CHILD",
                                             _provider(trio.brokers), acknowledge_cnc_positions=True)
    assert await _master(trio.strategy.id) == trio.dhinesh.id


# ─── execute (engine-enforced) ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_switch_without_acknowledgement_is_refused_and_changes_nothing(db_session, trio):
    _cnc(trio)
    with pytest.raises(strategy_switch.SwitchValidationError) as exc:
        await strategy_switch.execute_switch(db_session, trio.strategy.id, trio.arun.id, "CHILD",
                                             _provider(trio.brokers))
    assert exc.value.code == "CNC_ACKNOWLEDGEMENT_REQUIRED" and exc.value.status_code == 409
    assert await _master(trio.strategy.id) == trio.dhinesh.id
    for broker in trio.brokers.values():
        broker.place_order.assert_not_called()
        broker.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_switch_with_acknowledgement_completes_and_squares_nothing_off(db_session, trio):
    _cnc(trio)
    await strategy_switch.execute_switch(db_session, trio.strategy.id, trio.arun.id, "CHILD",
                                         _provider(trio.brokers), acknowledge_cnc_positions=True)
    assert await _master(trio.strategy.id) == trio.arun.id
    for broker in trio.brokers.values():
        broker.place_order.assert_not_called()
        broker.cancel_order.assert_not_called()
        broker.modify_order.assert_not_called()


@pytest.mark.asyncio
async def test_acknowledgement_is_not_needed_without_cnc(db_session, trio):
    await strategy_switch.execute_switch(db_session, trio.strategy.id, trio.arun.id, "CHILD",
                                         _provider(trio.brokers))
    assert await _master(trio.strategy.id) == trio.arun.id


# ─── HTTP API ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_endpoint_requires_the_acknowledgement_flag(api_client, trio):
    for acc in (trio.dhinesh, trio.vikash, trio.arun):
        api_client.brokers[str(acc.id)] = _Broker()
    api_client.brokers[str(trio.vikash.id)]._positions = [_pos(qty=10, product="CNC")]
    url = URL.format(sid=trio.strategy.id)

    pre = await api_client.get(
        f"{url}/preflight",
        params={"new_master_account_id": str(trio.arun.id), "old_master_action": "CHILD"},
        headers=AUTH,
    )
    assert pre.status_code == 200
    body = pre.json()
    assert body["allowed"] is True and body["requires_cnc_acknowledgement"] is True
    assert any(w["code"] == "CNC_POSITIONS_OPEN" for w in body["warnings"])

    payload = {"new_master_account_id": str(trio.arun.id), "old_master_action": "CHILD"}
    refused = await api_client.post(url, json=payload, headers=AUTH)
    assert refused.status_code == 409 and "CNC" in refused.text
    assert await _master(trio.strategy.id) == trio.dhinesh.id

    ok = await api_client.post(url, json={**payload, "acknowledge_cnc_positions": True}, headers=AUTH)
    assert ok.status_code == 200, ok.text
    assert ok.json()["switched"] is True
    assert await _master(trio.strategy.id) == trio.arun.id
