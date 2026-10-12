"""
Issue #7 regression coverage: _process_single_limit_follower (the LIMIT
replication path, used immediately on every master LIMIT_CREATED event)
must honor a follower's configured CopySetting.quantity_mode via
QuantityCalculator - exactly like _process_single_market_follower already
does - instead of always using StrategyMember.multiplier regardless of the
follower's configuration.

quantity_mode defaults to FIXED (see app/schemas/accounts.py), so this bug
affected any follower with copy-trading enabled at all, not just an
uncommon configuration.

No live trading is enabled and no real broker calls are made anywhere in
this file - all broker clients are AsyncMock test doubles.
"""
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.models.accounts import CopySetting, QuantityMode
from app.services.alice_blue.types import FundsData, OrderResult, OrderResultCategory
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from strategy_helpers import SAMPLE_MASTER_ACCOUNT_ID


def _limit_event(quantity=100, limit_price=Decimal("100.00"), master_order_id="ORD_QM_1"):
    return MasterTradeEvent(
        account_id=SAMPLE_MASTER_ACCOUNT_ID,
        master_order_id=master_order_id,
        symbol="RELIANCE",
        exchange="NSE",
        side="BUY",
        quantity=quantity,
        price=limit_price,
        limit_price=limit_price,
        product_type="MIS",
        timestamp=datetime.now(timezone.utc),
        fill_sequence=1,
        order_type="LIMIT",
        event_type="LIMIT_CREATED",
        status="OPEN",
    )


def _funds(available=Decimal("500000")):
    return FundsData(
        account_id="a", available_margin=available, collateral=Decimal("0"),
        used_margin=Decimal("0"), updated_at=datetime.now(timezone.utc),
    )


def _success(broker_order_id="BRK_QM_OK"):
    return OrderResult(
        category=OrderResultCategory.SUCCESS, client_order_id=None, broker_order_id=broker_order_id,
        message="Order placed successfully", raw_response={"stat": "Ok"}, timestamp=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_A_fixed_mode_uses_fixed_qty_not_multiplier(db_session, sample_accounts, sample_strategy):
    follower = sample_accounts["follower1"]
    db_session.add(CopySetting(
        follower_account_id=follower.id, copy_enabled=True,
        quantity_mode=QuantityMode.FIXED, fixed_qty=15,
    ))
    await db_session.commit()

    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.get_positions.return_value = []
    client.place_order.return_value = _success()

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=AsyncMock(set=AsyncMock(return_value=True))))
    fid = str(follower.id)
    # master trades 100 shares; follower's fixed_qty=15 must win, not 100*multiplier(1.0)
    res = await pipeline.execute_replication(event=_limit_event(quantity=100), clients={fid: client})

    assert res["results"][fid]["status"] == "SUCCESS"
    assert res["results"][fid]["quantity"] == 15
    assert client.place_order.call_args.args[0].quantity == 15


@pytest.mark.asyncio
async def test_B_ratio_mode_scales_master_quantity(db_session, sample_accounts, sample_strategy):
    follower = sample_accounts["follower1"]
    db_session.add(CopySetting(
        follower_account_id=follower.id, copy_enabled=True,
        quantity_mode=QuantityMode.RATIO, ratio=Decimal("0.5"),
    ))
    await db_session.commit()

    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.get_positions.return_value = []
    client.place_order.return_value = _success()

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=AsyncMock(set=AsyncMock(return_value=True))))
    fid = str(follower.id)
    res = await pipeline.execute_replication(event=_limit_event(quantity=100), clients={fid: client})

    assert res["results"][fid]["status"] == "SUCCESS"
    assert res["results"][fid]["quantity"] == 50  # 100 * 0.5


@pytest.mark.asyncio
async def test_C_capital_proportional_mode_scales_by_follower_funds(db_session, sample_accounts, sample_strategy):
    follower = sample_accounts["follower1"]
    db_session.add(CopySetting(
        follower_account_id=follower.id, copy_enabled=True,
        quantity_mode=QuantityMode.CAPITAL_PROPORTIONAL,
    ))
    await db_session.commit()

    # MASTER_CAPITAL is 500000 (conftest env default); follower has half that
    # available -> expect half the master's quantity.
    client = AsyncMock()
    client.get_funds.return_value = _funds(available=settings.MASTER_CAPITAL / 2)
    client.get_positions.return_value = []
    client.place_order.return_value = _success()

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=AsyncMock(set=AsyncMock(return_value=True))))
    fid = str(follower.id)
    res = await pipeline.execute_replication(event=_limit_event(quantity=100), clients={fid: client})

    assert res["results"][fid]["status"] == "SUCCESS"
    assert res["results"][fid]["quantity"] == 50  # 100 * (250000/500000)


@pytest.mark.asyncio
async def test_D_no_copy_setting_still_uses_multiplier_fallback(db_session, sample_accounts, sample_strategy):
    """Regression: a follower with no CopySetting at all is unaffected by
    this fix and still uses the plain multiplier formula exactly as
    before."""
    follower = sample_accounts["follower1"]
    # No CopySetting added for this follower.

    client = AsyncMock()
    client.get_funds.return_value = _funds()
    client.place_order.return_value = _success()

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=AsyncMock(set=AsyncMock(return_value=True))))
    fid = str(follower.id)
    res = await pipeline.execute_replication(event=_limit_event(quantity=100), clients={fid: client})

    assert res["results"][fid]["status"] == "SUCCESS"
    assert res["results"][fid]["quantity"] == 100  # 100 * multiplier(1.0), unchanged fallback
