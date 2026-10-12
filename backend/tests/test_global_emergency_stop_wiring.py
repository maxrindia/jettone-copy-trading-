"""
P3 #12 regression coverage: the RESOLVED global emergency-stop state
(`stop_active`, from self._resolve_stop_state) must reach RiskEngine.evaluate
- not the raw `global_emergency_stop` parameter these functions receive.

Background: replicate_limit_order, _execute_market_replication, and
handle_master_modification all correctly compute
`stop_active, stopped_followers = await self._resolve_stop_state(global_emergency_stop)`.
For the CREATION paths (replicate_limit_order / _execute_market_replication)
stop_active also feeds `EventValidator.validate(is_emergency_stopped=stop_active)`,
which blocks the ENTIRE event before any follower is processed whenever
stop_active is True - so for those two paths, RiskEngine's own inner
"Check 1: Global emergency stop" is provably unreachable with
global_emergency_stop=True (the validator always blocks first when the
resolved state is actually True). That makes the creation-path fix here
pure defense-in-depth / correctness, not a live behavior change - proven
below by feeding a stop_provider whose resolved value diverges from the
raw (dead) parameter and confirming the RESOLVED value, not the raw one,
governs the RiskEngine call.

handle_master_modification is different: it has NO EventValidator gate of
its own at all (verified by inspection - no self.validator.validate(...)
call anywhere in that function). For that path, the RiskEngine
global_emergency_stop check fed by stop_active is the ONLY thing that can
block a modification on a genuine global emergency stop (aside from the
per-follower runtime-pause set) - so the fix below is a REAL, observable
behavior change for master order modifications, not just redundant
defense-in-depth.

engine_loop.py's real call to pipeline.execute_replication(event=...,
clients=...) never passes global_emergency_stop=..., so in production that
raw parameter is always False; the actual global-stop signal always comes
from the pipeline's stop_provider (see EngineLoop._build_stop_provider).

No live trading is enabled and no real broker calls are made anywhere in
this file - all broker clients are AsyncMock test doubles and the DB is the
in-memory test SQLite engine (db_session fixture).
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.models.accounts import Account, AccountType, CopySetting, QuantityMode
from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.types import FundsData, OrderResult, OrderResultCategory
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.risk import RiskEngine
from app.services.copy_engine.validator import EventValidator
from strategy_helpers import SAMPLE_MASTER_ACCOUNT_ID


def _funds(available=Decimal("500000")):
    return FundsData(
        account_id="a", available_margin=available, collateral=Decimal("0"),
        used_margin=Decimal("0"), updated_at=datetime.now(timezone.utc),
    )


def _success_result(broker_order_id="BRK_OK"):
    return OrderResult(
        category=OrderResultCategory.SUCCESS, client_order_id=None, broker_order_id=broker_order_id,
        message="ok", raw_response={"stat": "Ok"}, timestamp=datetime.now(timezone.utc),
    )


def _limit_event(quantity=100, price=Decimal("100.00"), master_order_id="ORD_STOP_WIRING_LIMIT"):
    return MasterTradeEvent(
        account_id=SAMPLE_MASTER_ACCOUNT_ID,
        master_order_id=master_order_id,
        symbol="RELIANCE", exchange="NSE", side="BUY", quantity=quantity,
        price=price, limit_price=price, product_type="MIS",
        timestamp=datetime.now(timezone.utc), fill_sequence=1,
        order_type="LIMIT", event_type="LIMIT_CREATED", status="OPEN",
    )


def _market_event(quantity=100, master_order_id="ORD_STOP_WIRING_MARKET"):
    return MasterTradeEvent(
        account_id=SAMPLE_MASTER_ACCOUNT_ID,
        master_order_id=master_order_id,
        symbol="RELIANCE", exchange="NSE", side="BUY", quantity=quantity,
        price=Decimal("100.00"), product_type="MIS",
        timestamp=datetime.now(timezone.utc), fill_sequence=1,
        order_type="MARKET", event_type="MARKET_ACCEPTED", status="COMPLETE",
        total_quantity=quantity, filled_quantity=quantity, pending_quantity=0,
    )


def _spy_risk_engine(monkeypatch):
    """Wraps the real RiskEngine.evaluate so tests can assert exactly what
    global_emergency_stop value it was invoked with, while still running
    the real check logic (not a behavior-changing mock)."""
    calls = []
    original = RiskEngine.evaluate

    def spy(*args, **kwargs):
        calls.append(kwargs.get("global_emergency_stop"))
        return original(*args, **kwargs)

    monkeypatch.setattr(RiskEngine, "evaluate", staticmethod(spy))
    return calls


# ---------------------------------------------------------------------------
# Creation paths (LIMIT + MARKET): resolved stop_active must reach
# RiskEngine.evaluate, not the raw (dead-in-production) parameter.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_limit_creation_feeds_resolved_stop_state_not_raw_dead_parameter(
    db_session, sample_accounts, sample_strategy, monkeypatch
):
    """stop_provider resolves global stop to INACTIVE while the raw
    (unused-in-production) global_emergency_stop kwarg is passed as True.
    Before the fix, RiskEngine.evaluate received the raw True value and
    would incorrectly reject the follower with EMERGENCY_STOP_ACTIVE even
    though the authoritative stop_provider says trading is NOT stopped.
    After the fix, RiskEngine.evaluate receives the resolved (False) value
    and the follower order proceeds normally."""
    follower = sample_accounts["follower1"]
    db_session.add(
        CopySetting(
            follower_account_id=follower.id,
            copy_enabled=True,
            quantity_mode=QuantityMode.FIXED,
            fixed_qty=100,
        )
    )
    await db_session.commit()

    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.get_positions.return_value = []
    client.place_order.return_value = _success_result("BRK_LIMIT_STOP_WIRING")

    calls = _spy_risk_engine(monkeypatch)

    async def stop_provider():
        return False, set()  # authoritative: NOT globally stopped

    pipeline = CopyEnginePipeline(
        validator=EventValidator(redis=AsyncMock(set=AsyncMock(return_value=True))),
        stop_provider=stop_provider,
    )

    fid = str(follower.id)
    # Raw kwarg intentionally diverges from the provider's resolved value -
    # this can't happen from engine_loop.py today (it never passes this
    # kwarg) but is exactly the parameter this fix stops trusting.
    res = await pipeline.execute_replication(
        event=_limit_event(), clients={fid: client}, global_emergency_stop=True
    )

    assert res["status"] == "completed"
    assert res["results"][fid]["status"] == "SUCCESS"
    client.place_order.assert_awaited_once()

    assert calls, "RiskEngine.evaluate was never called"
    assert calls[-1] is False, (
        "RiskEngine.evaluate should have received the resolved stop_active "
        "(False), not the raw dead global_emergency_stop parameter (True)"
    )


@pytest.mark.asyncio
async def test_market_creation_feeds_resolved_stop_state_not_raw_dead_parameter(
    db_session, sample_accounts, sample_strategy, monkeypatch
):
    follower = sample_accounts["follower1"]
    db_session.add(
        CopySetting(
            follower_account_id=follower.id,
            copy_enabled=True,
            quantity_mode=QuantityMode.FIXED,
            fixed_qty=100,
        )
    )
    await db_session.commit()

    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.get_positions.return_value = []
    client.place_order.return_value = _success_result("BRK_MARKET_STOP_WIRING")

    calls = _spy_risk_engine(monkeypatch)

    async def stop_provider():
        return False, set()

    pipeline = CopyEnginePipeline(
        validator=EventValidator(redis=AsyncMock(set=AsyncMock(return_value=True))),
        stop_provider=stop_provider,
    )

    fid = str(follower.id)
    res = await pipeline.execute_replication(
        event=_market_event(), clients={fid: client}, global_emergency_stop=True
    )

    assert res["status"] == "completed"
    assert res["results"][fid]["status"] == OrderResultCategory.SUCCESS.value
    client.place_order.assert_awaited_once()

    assert calls, "RiskEngine.evaluate was never called"
    assert calls[-1] is False, (
        "RiskEngine.evaluate should have received the resolved stop_active "
        "(False), not the raw dead global_emergency_stop parameter (True)"
    )


# ---------------------------------------------------------------------------
# Modification path: handle_master_modification has NO EventValidator gate
# of its own - RiskEngine's global_emergency_stop check fed by the resolved
# stop_active is the ONLY thing that can block a genuinely active global
# stop here. This is a real, observable behavior fix, not dead code.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_modification_is_blocked_when_global_stop_is_genuinely_active(db_session, mock_redis):
    """Mirrors engine_loop.py exactly: execute_replication() is called
    WITHOUT global_emergency_stop=... (always defaults False), and the real
    global-stop signal comes entirely from the pipeline's stop_provider.
    Before the fix, handle_master_modification's RiskEngine.evaluate call
    used the raw (always-False-in-production) parameter, so a genuinely
    active global emergency stop could never block a master order
    modification. After the fix, it uses the resolved stop_active and
    correctly rejects."""
    child_acc = Account(
        id=uuid.uuid4(),
        account_type=AccountType.FOLLOWER,
        display_name="Child Stop Wiring",
        alice_user_id="STOPW001",
        is_active=True,
    )
    db_session.add(child_acc)
    await db_session.commit()

    db_session.add(
        CopySetting(
            follower_account_id=child_acc.id,
            copy_enabled=True,
            quantity_mode=QuantityMode.FIXED,
            fixed_qty=100,
        )
    )
    db_session.add(
        Order(
            id=uuid.uuid4(),
            internal_order_id="JT_CH_STOP_WIRING_001",
            broker_order_id="BRK_STOP_WIRING",
            account_id=child_acc.id,
            master_broker_order_id="ORD_STOP_WIRING_MOD",
            source=OrderSource.SYSTEM,
            exchange="NSE",
            symbol="RELIANCE",
            side=OrderSide.BUY,
            order_type="LIMIT",
            product_type="MIS",
            quantity=100,
            filled_quantity=0,
            pending_quantity=100,
            price=Decimal("2950.00"),
            status=OrderStatus.OPEN,
        )
    )
    await db_session.commit()

    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.get_positions.return_value = []
    client.modify_order.return_value = OrderResult(
        category=OrderResultCategory.SUCCESS,
        client_order_id=None,
        broker_order_id="BRK_STOP_WIRING",
        message="Modified successfully",
        raw_response={"stat": "Ok"},
        timestamp=datetime.now(timezone.utc),
    )

    async def stop_provider():
        return True, set()  # authoritative: globally stopped

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis), stop_provider=stop_provider)

    fid = str(child_acc.id)
    # Exactly like engine_loop.py's real call: execute_replication() invoked
    # with NO global_emergency_stop kwarg at all.
    res = await pipeline.execute_replication(
        event=MasterTradeEvent(
            account_id=SAMPLE_MASTER_ACCOUNT_ID,
            master_order_id="ORD_STOP_WIRING_MOD",
            symbol="RELIANCE", exchange="NSE", side="BUY", quantity=150,
            price=Decimal("2999.00"), limit_price=Decimal("2999.00"), product_type="MIS",
            timestamp=datetime.now(timezone.utc), fill_sequence=1,
            order_type="LIMIT", event_type="ORDER_MODIFIED", status="OPEN",
        ),
        clients={fid: client},
    )

    assert res["status"] == "completed"
    assert res["modifications"][fid] == "REJECTED: EMERGENCY_STOP_ACTIVE"
    client.modify_order.assert_not_called()
