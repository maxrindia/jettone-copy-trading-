"""Reconciliation compares positions per INSTRUMENT, not per spelling: Alice Blue writes an NSE equity "SBIN-EQ", Groww writes "SBIN".
An Alice Blue Master copied to a Groww Child (or the reverse) therefore stores one form and receives the other from the broker."""
import uuid
from decimal import Decimal

import pytest

from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.types import Position
from app.services.reliability.reconciliation import ReconciliationEngine, instrument_key


def _order(account_id, symbol, qty=10, side=OrderSide.BUY):
    return Order(id=uuid.uuid4(), internal_order_id=f"JT_{uuid.uuid4().hex[:10]}", broker_order_id=f"B{uuid.uuid4().hex[:6]}",
                 account_id=account_id, master_broker_order_id="M1", source=OrderSource.SYSTEM, exchange="NSE", symbol=symbol,
                 side=side, order_type="LIMIT", product_type="CNC", quantity=qty, filled_quantity=qty, pending_quantity=0,
                 price=Decimal("100"), status=OrderStatus.COMPLETE)


def _pos(account_id, symbol, qty, product="CNC"):
    return Position(symbol=symbol, quantity=qty, side="BUY" if qty >= 0 else "SELL", average_price=Decimal("100"),
                    account_id=str(account_id), exchange="NSE", product_type=product)


def test_the_key_strips_only_the_cash_equity_suffix():
    assert instrument_key("SBIN-EQ") == "SBIN" and instrument_key("sbin") == "SBIN" and instrument_key(" SBIN-EQ ") == "SBIN"
    assert instrument_key("NIFTY26OCT23500CE") == "NIFTY26OCT23500CE"
    assert instrument_key("SBIN-BE") == "SBIN-BE" and instrument_key("-EQ") == "-EQ" and instrument_key(None) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("row_symbol,broker_symbol", [("SBIN-EQ", "SBIN"), ("SBIN", "SBIN-EQ"), ("SBIN", "SBIN"), ("SBIN-EQ", "SBIN-EQ")])
async def test_a_correct_holding_is_not_a_mismatch_whichever_spelling_each_side_uses(db_session, sample_accounts, row_symbol, broker_symbol):
    f = sample_accounts["follower1"]
    db_session.add(_order(f.id, row_symbol))
    await db_session.commit()
    assert await ReconciliationEngine.run_for_account(f.id, db_session, actual_positions=[_pos(f.id, broker_symbol, 10)]) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("row_symbol,broker_symbol", [("SBIN-EQ", "SBIN"), ("SBIN", "SBIN-EQ")])
async def test_a_real_difference_is_still_reported_once_across_spellings(db_session, sample_accounts, row_symbol, broker_symbol):
    f = sample_accounts["follower1"]
    db_session.add(_order(f.id, row_symbol, qty=10))
    await db_session.commit()
    records = await ReconciliationEngine.run_for_account(f.id, db_session, actual_positions=[_pos(f.id, broker_symbol, 7)])
    assert len(records) == 1
    assert (records[0].symbol, records[0].expected_qty, records[0].actual_qty, records[0].discrepancy_qty) == ("SBIN", 10, 7, -3)


@pytest.mark.asyncio
async def test_a_position_the_database_does_not_expect_is_still_reported(db_session, sample_accounts):
    f = sample_accounts["follower1"]
    records = await ReconciliationEngine.run_for_account(f.id, db_session, actual_positions=[_pos(f.id, "TCS", 5)])
    assert [(r.symbol, r.expected_qty, r.actual_qty) for r in records] == [("TCS", 0, 5)]


@pytest.mark.asyncio
async def test_two_broker_rows_for_one_instrument_are_added_not_overwritten(db_session, sample_accounts):
    f = sample_accounts["follower1"]
    db_session.add(_order(f.id, "SBIN-EQ", qty=10))
    await db_session.commit()
    both = [_pos(f.id, "SBIN", 6, product="CNC"), _pos(f.id, "SBIN-EQ", 4, product="MIS")]
    assert await ReconciliationEngine.run_for_account(f.id, db_session, actual_positions=both) == []


@pytest.mark.asyncio
async def test_derivative_symbols_are_compared_exactly(db_session, sample_accounts):
    f = sample_accounts["follower1"]
    db_session.add(_order(f.id, "NIFTY26OCT23500CE", qty=75))
    await db_session.commit()
    assert await ReconciliationEngine.run_for_account(f.id, db_session, actual_positions=[_pos(f.id, "NIFTY26OCT23500CE", 75)]) == []
    records = await ReconciliationEngine.run_for_account(f.id, db_session, actual_positions=[_pos(f.id, "NIFTY26OCT23600CE", 75)])
    assert len(records) == 2                                                       # a different strike is a different instrument
