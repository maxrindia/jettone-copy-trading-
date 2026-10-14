"""
Live Trading ON/OFF - the owner's runtime switch (UI), persisted and enforced
by the backend.

State rules: a stored value is authoritative; with nothing stored the
ENGINE_LIVE_TRADING_ENABLED environment value is the default; an unreadable
state is OFF. OFF blocks every NEW broker order (engine copies and the manual
order endpoint); master cancellations still propagate. No broker is contacted.
"""
import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.audit import SystemEvent
from app.models.platform_settings import PlatformSetting
from app.services import live_trading
from app.services.alice_blue.types import OrderUpdateEvent
from app.services.copy_engine.events import MasterTradeEvent
from app.services.live_trading import (
    LIVE_TRADING_KEY,
    event_allowed_when_live_off,
    get_live_trading_state,
    set_live_trading,
)
from app.services.runtime.engine_loop import EngineLoop
from conftest import TestingSessionLocal
from test_engine_loop import _FakeMasterClient, _FakeWebSocketClient
from test_strategy_master_switching import AUTH, api_client  # noqa: F401  (api_client is a fixture)


async def _stored():
    async with TestingSessionLocal() as s:
        row = (await s.execute(select(PlatformSetting).where(PlatformSetting.key == LIVE_TRADING_KEY))).scalar_one_or_none()
        return None if row is None else row.value


# ─── state resolution ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_default_with_nothing_stored_is_off(db_session, monkeypatch):
    monkeypatch.setattr(settings, "ENGINE_LIVE_TRADING_ENABLED", False)
    state = await get_live_trading_state(db_session)
    assert (state.enabled, state.source) == (False, "env_default")


@pytest.mark.asyncio
async def test_environment_value_is_only_the_default(db_session, monkeypatch):
    monkeypatch.setattr(settings, "ENGINE_LIVE_TRADING_ENABLED", True)
    assert (await get_live_trading_state(db_session)).enabled is True       # nothing stored -> env default
    await set_live_trading(db_session, False, actor="ui", reason="test")
    state = await get_live_trading_state(db_session)
    assert (state.enabled, state.source) == (False, "runtime")              # stored OFF beats env ON


@pytest.mark.asyncio
async def test_ui_can_turn_it_on_without_touching_the_environment(db_session, monkeypatch):
    monkeypatch.setattr(settings, "ENGINE_LIVE_TRADING_ENABLED", False)
    state = await set_live_trading(db_session, True, actor="ui", reason="test")
    assert (state.enabled, state.source, state.updated_by) == (True, "runtime", "ui")


@pytest.mark.asyncio
async def test_toggle_persists_in_the_database_and_is_audited(db_session, monkeypatch):
    monkeypatch.setattr(settings, "ENGINE_LIVE_TRADING_ENABLED", False)
    await set_live_trading(db_session, True, actor="ui", reason="small test")
    assert await _stored() == "true"                       # a brand-new session sees it (= survives a restart)
    await set_live_trading(db_session, False, actor="ui", reason="done")
    assert await _stored() == "false"
    async with TestingSessionLocal() as s:
        events = (await s.execute(select(SystemEvent).where(SystemEvent.event_type == "LIVE_TRADING_TOGGLED")
                                  .order_by(SystemEvent.created_at))).scalars().all()
    assert [e.metadata_payload["enabled"] for e in events] == [True, False]
    assert events[0].metadata_payload["previous"] is None and events[1].metadata_payload["previous"] is True
    assert "small test" in events[0].message


@pytest.mark.asyncio
async def test_unreadable_state_is_off(monkeypatch):
    monkeypatch.setattr(settings, "ENGINE_LIVE_TRADING_ENABLED", True)
    broken = MagicMock()
    broken.execute = AsyncMock(side_effect=RuntimeError("db down"))
    state = await get_live_trading_state(broken)
    assert (state.enabled, state.source) == (False, "error")
    monkeypatch.setattr(live_trading, "AsyncSessionLocal", MagicMock(side_effect=RuntimeError("no session")))
    assert await live_trading.is_live_trading_enabled() is False


def test_only_cancellations_pass_while_off():
    assert event_allowed_when_live_off("ORDER_CANCELLED") is True
    for new_exposure in ("LIMIT_CREATED", "MARKET_ACCEPTED", "ORDER_FILLED", "ORDER_MODIFIED",
                         "SL_PENDING_CREATED", "SL_PENDING_MODIFIED", "ORDER_REJECTED", ""):
        assert event_allowed_when_live_off(new_exposure) is False, new_exposure


# ─── API ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_api_turning_on_requires_confirm_and_persists(api_client, monkeypatch):
    monkeypatch.setattr(settings, "ENGINE_LIVE_TRADING_ENABLED", False)
    url = "/api/v1/reliability/live-trading"
    r = await api_client.get(url, headers=AUTH)
    assert r.status_code == 200 and r.json()["data"]["enabled"] is False

    r = await api_client.put(url, json={"enabled": True}, headers=AUTH)          # no confirm
    assert r.status_code == 422
    assert await _stored() is None                                              # nothing changed

    r = await api_client.put(url, json={"enabled": True, "confirm": True, "reason": "approved test"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["data"]["enabled"] is True
    assert (await api_client.get(url, headers=AUTH)).json()["data"]["enabled"] is True
    assert await _stored() == "true"

    r = await api_client.put(url, json={"enabled": False}, headers=AUTH)        # turning OFF needs no confirm
    assert r.status_code == 200 and r.json()["data"]["enabled"] is False
    assert await _stored() == "false"


@pytest.mark.asyncio
async def test_manual_order_endpoint_enforces_the_state(api_client, monkeypatch):
    monkeypatch.setattr(settings, "ENGINE_LIVE_TRADING_ENABLED", False)
    body = {"strategy_id": str(uuid.uuid4()), "symbol": "NIFTY", "exchange": "NFO", "side": "BUY",
            "quantity": 65, "product_type": "NRML", "order_type": "MARKET"}
    r = await api_client.post("/api/v1/broker/manual-order", json=body, headers=AUTH)
    assert r.status_code == 409 and "Live Trading is OFF" in r.text

    await api_client.put("/api/v1/reliability/live-trading", json={"enabled": True, "confirm": True}, headers=AUTH)
    r = await api_client.post("/api/v1/broker/manual-order", json=body, headers=AUTH)
    # past the Live Trading gate now: it stops later (unknown strategy) - never on the switch
    assert "Live Trading is OFF" not in r.text

    await api_client.put("/api/v1/reliability/live-trading", json={"enabled": False}, headers=AUTH)
    r = await api_client.post("/api/v1/broker/manual-order", json=body, headers=AUTH)
    assert r.status_code == 409 and "Live Trading is OFF" in r.text


# ─── engine: enforced per event, switchable at runtime ──────────────────────

async def _drive_engine(mock_redis, *, event_type, live_enabled):
    """Runs the real EngineLoop with one master event of `event_type`; returns
    the pipeline mock so the caller can see whether replication was reached."""
    master_account_id = str(uuid.uuid4())
    fake_account_manager = MagicMock()
    fake_account_manager.initialize = AsyncMock()
    fake_account_manager.authenticate_all = AsyncMock(return_value={})
    fake_account_manager.get_master_client = MagicMock(return_value=_FakeMasterClient(account_id=master_account_id))
    fake_account_manager.get_follower_clients_map = MagicMock(return_value={})
    fake_account_manager.get_client = MagicMock(return_value=None)

    trade_event = MasterTradeEvent(
        master_order_id="LT-1", symbol="NIFTY", exchange="NFO", side="BUY", quantity=65,
        price=Decimal("61.90"), product_type="NRML", timestamp=datetime.now(timezone.utc),
        account_id=master_account_id, order_type="LIMIT", event_type=event_type,
        source="BROKER_EXTERNAL", status="OPEN",
    )
    detector = MagicMock()
    detector.process_order_update = MagicMock(return_value=trade_event)
    pipeline = MagicMock()
    pipeline.save_or_update_order = AsyncMock()
    pipeline.execute_replication = AsyncMock(return_value={"status": "completed"})

    def fake(**kw):
        m = MagicMock()
        for name in ("start", "stop"):
            setattr(m, name, AsyncMock())
        return m

    recon = MagicMock()
    recon.run_crash_recovery = AsyncMock(return_value=0)
    with patch("app.services.runtime.engine_loop.account_manager", fake_account_manager), \
         patch("app.services.runtime.engine_loop.MasterTradeDetector", return_value=detector), \
         patch("app.services.runtime.engine_loop.CopyEnginePipeline", return_value=pipeline), \
         patch("app.services.runtime.engine_loop.AliceBlueWebSocketClient", _FakeWebSocketClient), \
         patch("app.services.runtime.engine_loop.load_and_cache_contract_master", AsyncMock()), \
         patch("app.services.runtime.engine_loop.ReconciliationEngine", recon), \
         patch("app.services.runtime.engine_loop.StaleOrderSweeper", return_value=fake()), \
         patch("app.services.runtime.engine_loop.FollowerFillSyncer", return_value=fake()), \
         patch("app.services.runtime.engine_loop.MasterOrderPoller", return_value=fake()), \
         patch("app.services.runtime.engine_loop.is_live_trading_enabled", AsyncMock(return_value=live_enabled)):
        loop = EngineLoop(redis_client=mock_redis)
        await loop._run()
        loop._ws_client.on_order_update(OrderUpdateEvent(
            broker_order_id="LT-1", client_order_id=None, symbol="NIFTY", side="BUY", quantity=65,
            filled_quantity=0, fill_price=Decimal("0"), status="OPEN", timestamp=datetime.now(timezone.utc),
            account_id=master_account_id, product_type="NRML", order_type="LIMIT", price=Decimal("61.90"),
            trigger_price=Decimal("0"), rejection_reason=None, exchange="NFO",
        ))
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)
        await loop.stop()
    return pipeline


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["LIMIT_CREATED", "MARKET_ACCEPTED", "ORDER_MODIFIED", "SL_PENDING_CREATED"])
async def test_engine_copies_nothing_new_while_off(mock_redis, db_session, event_type):
    pipeline = await _drive_engine(mock_redis, event_type=event_type, live_enabled=False)
    pipeline.execute_replication.assert_not_called()


@pytest.mark.asyncio
async def test_engine_still_propagates_a_master_cancellation_while_off(mock_redis, db_session):
    pipeline = await _drive_engine(mock_redis, event_type="ORDER_CANCELLED", live_enabled=False)
    pipeline.execute_replication.assert_awaited_once()
    assert pipeline.execute_replication.call_args.kwargs["event"].event_type == "ORDER_CANCELLED"


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["LIMIT_CREATED", "MARKET_ACCEPTED", "ORDER_CANCELLED"])
async def test_engine_replicates_when_on(mock_redis, db_session, event_type):
    pipeline = await _drive_engine(mock_redis, event_type=event_type, live_enabled=True)
    pipeline.execute_replication.assert_awaited_once()
