import pytest
import uuid
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from app.models.accounts import Account, AccountType, CopySetting, QuantityMode
from app.services.alice_blue.types import FundsData, OrderUpdateEvent
from app.services.copy_engine.calculator import (
    CapitalProportionalStrategy,
    CopyRatioStrategy,
    FixedQuantityStrategy,
    QuantityCalculator,
    RiskBasedStrategy,
    floor_to_lot,
)
from app.services.copy_engine.detector import MasterTradeDetector
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.risk import RiskEngine
from app.services.copy_engine.validator import EventValidator


# ============================================================================
# 1. Quantity Calculation & Lot Sizing Unit Tests
# ============================================================================

def test_floor_to_lot_rounding_rules():
    # Lot size = 25 (e.g. NIFTY)
    assert floor_to_lot(50, 25) == 50
    assert floor_to_lot(49, 25) == 25  # Strictly rounded down, never up to 50
    assert floor_to_lot(24, 25) == 0   # Zero lot result
    assert floor_to_lot(74.9, 25) == 50

    # Lot size = 1 (Equity)
    assert floor_to_lot(10.8, 1) == 10


def test_fixed_quantity_strategy():
    settings = CopySetting(
        follower_account_id=uuid.uuid4(),
        fixed_qty=2,
    )
    # With lot_size = 25 -> 2 * 25 = 50 lots
    qty = FixedQuantityStrategy().calculate(
        master_qty=100,
        master_capital=Decimal("1000000"),
        follower_capital=Decimal("200000"),
        lot_size=25,
        settings=settings,
    )
    # fixed_qty is 2 lots -> 2 in lot_size=1, or if fixed_qty=50 with lot_size=25
    assert qty == 0  # 2 // 25 = 0

    settings.fixed_qty = 50
    qty2 = FixedQuantityStrategy().calculate(
        master_qty=100,
        master_capital=Decimal("1000000"),
        follower_capital=Decimal("200000"),
        lot_size=25,
        settings=settings,
    )
    assert qty2 == 50


def test_copy_ratio_strategy():
    settings = CopySetting(
        follower_account_id=uuid.uuid4(),
        ratio=Decimal("0.5"),
    )
    # Master trades 100 shares, lot_size=25, ratio 0.5 -> 50 shares
    qty = CopyRatioStrategy().calculate(
        master_qty=100,
        master_capital=Decimal("1000000"),
        follower_capital=Decimal("500000"),
        lot_size=25,
        settings=settings,
    )
    assert qty == 50

    # Ratio > 1.0 (e.g. 2.0x)
    settings.ratio = Decimal("2.0")
    qty_high = CopyRatioStrategy().calculate(
        master_qty=25,
        master_capital=Decimal("1000000"),
        follower_capital=Decimal("500000"),
        lot_size=25,
        settings=settings,
    )
    assert qty_high == 50


def test_capital_proportional_strategy():
    settings = CopySetting(follower_account_id=uuid.uuid4())
    # Master cap: 500k, Follower cap: 250k -> ratio 0.5. Master trades 50 -> Follower trades 25
    qty = CapitalProportionalStrategy().calculate(
        master_qty=50,
        master_capital=Decimal("500000"),
        follower_capital=Decimal("250000"),
        lot_size=25,
        settings=settings,
    )
    assert qty == 25

    # Follower capital too small for 1 lot -> rounds to 0
    qty_zero = CapitalProportionalStrategy().calculate(
        master_qty=25,
        master_capital=Decimal("500000"),
        follower_capital=Decimal("50000"),  # 10% of 25 = 2.5 shares -> 0 lots
        lot_size=25,
        settings=settings,
    )
    assert qty_zero == 0


def test_risk_based_strategy_capped_by_max_value():
    settings = CopySetting(
        follower_account_id=uuid.uuid4(),
        max_trade_value=Decimal("5000"),  # Hard cap ₹5,000
    )
    # Capital proportional would yield 100 qty @ ₹100 = ₹10,000 -> capped to ₹5,000 (50 shares)
    qty = RiskBasedStrategy().calculate(
        master_qty=100,
        master_capital=Decimal("500000"),
        follower_capital=Decimal("500000"),
        lot_size=25,
        settings=settings,
        reference_price=Decimal("100"),
    )
    assert qty == 50  # Capped from 100 down to 50


# ============================================================================
# 2. Master Trade Detector Unit Tests
# ============================================================================

def test_detector_detects_complete_fill():
    detector = MasterTradeDetector(master_account_id="acc-master-1")

    event = OrderUpdateEvent(
        broker_order_id="ORD_1001",
        client_order_id=None,
        symbol="NIFTY24AUG24500CE",
        side="BUY",
        quantity=50,
        filled_quantity=50,
        fill_price=Decimal("140.50"),
        status="COMPLETE",
        timestamp=datetime.utcnow(),
        account_id="acc-master-1",
    )

    trade = detector.process_order_update(event)
    assert trade is not None
    assert trade.master_order_id == "ORD_1001"
    assert trade.quantity == 50
    # P0#2: fill_sequence is now the cumulative filled_quantity (durable,
    # restart-safe dedup differentiator), not an incrementing counter.
    assert trade.fill_sequence == 50


def test_detector_handles_partial_fill_increments():
    detector = MasterTradeDetector(master_account_id="acc-master-1")

    # Fill 1: 20 out of 50 filled
    event1 = OrderUpdateEvent(
        broker_order_id="ORD_PARTIAL",
        client_order_id=None,
        symbol="BANKNIFTY24AUG51000CE",
        side="BUY",
        quantity=50,
        filled_quantity=20,
        fill_price=Decimal("250.00"),
        status="PARTIAL",
        timestamp=datetime.utcnow(),
        account_id="acc-master-1",
    )
    trade1 = detector.process_order_update(event1)
    assert trade1 is not None
    assert trade1.quantity == 20
    # P0#2: fill_sequence is now the cumulative filled_quantity, not an
    # incrementing counter.
    assert trade1.fill_sequence == 20

    # Fill 2: Cumulative 50 out of 50 filled (increment = 30)
    event2 = OrderUpdateEvent(
        broker_order_id="ORD_PARTIAL",
        client_order_id=None,
        symbol="BANKNIFTY24AUG51000CE",
        side="BUY",
        quantity=50,
        filled_quantity=50,
        fill_price=Decimal("252.00"),
        status="COMPLETE",
        timestamp=datetime.utcnow(),
        account_id="acc-master-1",
    )
    trade2 = detector.process_order_update(event2)
    assert trade2 is not None
    assert trade2.quantity == 30  # Exactly the remaining increment
    # P0#2: cumulative filled_quantity at this fill (50), not counter value 2.
    assert trade2.fill_sequence == 50


# ============================================================================
# 3. Risk Engine 8-Rule Independent Checks
# ============================================================================

def test_risk_engine_all_checks():
    acc = Account(
        id=uuid.uuid4(),
        account_type=AccountType.FOLLOWER,
        display_name="Follower 1",
        alice_user_id="AB00201",
        is_emergency_stopped=False,
    )
    settings = CopySetting(
        follower_account_id=acc.id,
        copy_enabled=True,
        max_trade_value=Decimal("10000"),
        max_daily_loss=Decimal("5000"),
        max_exposure=Decimal("20000"),
        allowed_symbols=["NIFTY24AUG24500CE"],
    )
    funds = FundsData(str(acc.id), Decimal("50000"), Decimal("0"), Decimal("0"), datetime.utcnow())

    # Happy path: passes all checks
    res_pass = RiskEngine.evaluate(
        account=acc,
        settings=settings,
        symbol="NIFTY24AUG24500CE",
        quantity=25,
        price=Decimal("100"),  # Order val = ₹2,500
        funds=funds,
    )
    assert res_pass.passed is True
    assert len(res_pass.failures) == 0

    # Test Check: FOLLOWER_PAUSED
    settings.copy_enabled = False
    res_paused = RiskEngine.evaluate(acc, settings, "NIFTY24AUG24500CE", 25, Decimal("100"), funds)
    assert "FOLLOWER_PAUSED" in res_paused.failures
    settings.copy_enabled = True

    # Test Check: SYMBOL_NOT_ALLOWED_FOR_FOLLOWER
    res_sym = RiskEngine.evaluate(acc, settings, "UNKNOWN_SYM", 25, Decimal("100"), funds)
    assert "SYMBOL_NOT_ALLOWED_FOR_FOLLOWER" in res_sym.failures

    # Test Check: MAX_TRADE_VALUE_BREACHED
    # Order val = 200 * ₹100 = ₹20,000 > max_trade_value ₹10,000
    res_max_val = RiskEngine.evaluate(acc, settings, "NIFTY24AUG24500CE", 200, Decimal("100"), funds)
    assert "MAX_TRADE_VALUE_BREACHED" in res_max_val.failures

    # Test Check: EMERGENCY_STOP_ACTIVE
    res_es = RiskEngine.evaluate(acc, settings, "NIFTY24AUG24500CE", 25, Decimal("100"), funds, global_emergency_stop=True)
    assert "EMERGENCY_STOP_ACTIVE" in res_es.failures
