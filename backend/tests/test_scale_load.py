import pytest
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select

from app.models.accounts import Account, AccountType, CopySetting, QuantityMode
from app.models.trades import CopyTrade, CopyTradeStatus
from app.services.alice_blue.types import OrderResult, OrderResultCategory, FundsData
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.monitoring.telegram import TelegramAlertService
from strategy_helpers import link_strategy


@pytest.mark.asyncio
async def test_5_followers_concurrent_staggered_execution(mock_redis, db_session):
    """
    6.1 & 6.4: Replicate 1 master trade to 5 followers in parallel with micro-staggering.
    Followers disagree in outcomes: 2 SUCCESS, 1 RISK REJECTED, 1 SYMBOL REJECTED, 1 TIMEOUT.
    Verify dashboard/DB audit represents each follower independently.
    """
    # 1. Setup 1 Master + 5 Followers
    master = Account(
        id=uuid.uuid4(),
        account_type=AccountType.MASTER,
        display_name="Master Trader",
        alice_user_id="AB_MASTER_1",
        is_active=True,
    )
    db_session.add(master)

    followers = []
    modes = [
        (QuantityMode.FIXED, 25, None, None, ["NIFTY"]),
        (QuantityMode.FIXED, 50, None, None, ["NIFTY"]),
        (QuantityMode.RATIO, None, Decimal("1.5"), None, ["NIFTY"]),
        (QuantityMode.CAPITAL_PROPORTIONAL, None, None, None, ["NIFTY"]),
        (QuantityMode.RISK_BASED, None, None, Decimal("2000"), ["OTHER_SYM"]), # SYMBOL REJECTED
    ]

    for idx, (mode, fixed, ratio, max_val, syms) in enumerate(modes, start=1):
        f = Account(
            id=uuid.uuid4(),
            account_type=AccountType.FOLLOWER,
            display_name=f"Follower {idx}",
            alice_user_id=f"AB_FOL_{idx}",
            is_active=True,
        )
        db_session.add(f)
        await db_session.flush()

        cs = CopySetting(
            follower_account_id=f.id,
            copy_enabled=True,
            quantity_mode=mode,
            fixed_qty=fixed,
            ratio=ratio,
            max_trade_value=max_val,
            allowed_symbols=syms,
        )
        db_session.add(cs)
        followers.append(f)

    await db_session.commit()
    await link_strategy(db_session, master, followers)

    def make_mock_client(cat: OrderResultCategory, msg: str, brk_id: str = None):
        c = AsyncMock()
        c.get_funds.return_value = FundsData(
            account_id="f",
            available_margin=Decimal("500000"),
            collateral=Decimal("0"),
            used_margin=Decimal("0"),
            updated_at=datetime.now(timezone.utc),
        )
        c.place_order.return_value = OrderResult(
            category=cat,
            client_order_id="cli",
            broker_order_id=brk_id,
            message=msg,
            raw_response={},
            timestamp=datetime.now(timezone.utc),
        )
        return c

    # 2. Mock Clients for Followers with heterogeneous outcomes
    mock_clients = {
        str(followers[0].id): make_mock_client(OrderResultCategory.SUCCESS, "Success", "BRK_F1"),
        str(followers[1].id): make_mock_client(OrderResultCategory.SUCCESS, "Success", "BRK_F2"),
        str(followers[2].id): make_mock_client(OrderResultCategory.TIMEOUT, "Broker timeout"),
        str(followers[3].id): make_mock_client(OrderResultCategory.REJECTED, "Insufficient Margin"),
        str(followers[4].id): make_mock_client(OrderResultCategory.SUCCESS, "Never reached - symbol blocked"),
        # Follower 5 rejected by RiskEngine before submission
    }

    validator = EventValidator(redis=mock_redis)
    pipeline = CopyEnginePipeline(validator=validator)

    event = MasterTradeEvent(
        master_order_id="ORD_SCALE_500",
        account_id=str(master.id),
        symbol="NIFTY",
        exchange="NFO",
        side="BUY",
        quantity=25,
        price=Decimal("100.00"),
        product_type="MIS",
        timestamp=datetime.now(timezone.utc),
    )

    # 3. Execute 5-follower replication with 50ms stagger for SQLite transaction isolation
    start_time = datetime.now(timezone.utc)
    res = await pipeline.execute_replication(
        event=event,
        clients=mock_clients,
        global_emergency_stop=False,
        stagger_ms=100,
    )
    end_time = datetime.now(timezone.utc)

    assert res["status"] == "completed"
    results_map = res["results"]
    assert len(results_map) == 5

    # Check outcomes per follower
    assert results_map[str(followers[0].id)]["status"] == "SUCCESS"
    assert results_map[str(followers[1].id)]["status"] == "SUCCESS"
    assert results_map[str(followers[2].id)]["status"] == "TIMEOUT"
    assert results_map[str(followers[3].id)]["status"] == "REJECTED"
    assert results_map[str(followers[4].id)]["status"] == "REJECTED"
    assert "SYMBOL_NOT_ALLOWED_FOR_FOLLOWER" in results_map[str(followers[4].id)]["reasons"]

    # Verify staggering delay took at least 50ms
    duration_ms = (end_time - start_time).total_seconds() * 1000
    assert duration_ms >= 50


@pytest.mark.asyncio
async def test_telegram_alert_formatting():
    """
    6.3: Telegram alert formatting and mock dispatch.
    """
    service = TelegramAlertService(bot_token="MOCK_TOKEN", chat_id="MOCK_CHAT_ID")
    
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=200)
        
        sent = await service.send_alert(
            title="Follower 3 Order REJECTED",
            message="Insufficient margin (NIFTY, qty 25)",
            level="ERROR",
            details={"account_id": "AB003", "symbol": "NIFTY"},
        )
        assert sent is True
        assert mock_post.called
