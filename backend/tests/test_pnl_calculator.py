"""Pure gross-P&L calculator tests (app/services/pnl): weighted average cost,
fill inclusion and dedup rules, the IST trading-day boundary, unverified
states, and the broker cross-check. No DB, no network."""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.core.trading_day import trading_day, trading_day_start_utc
from app.services.pnl.broker_data import parse_order_book_fills, parse_positions
from app.services.pnl.calculator import (
    CHECK_MATCH,
    CHECK_MISMATCH,
    CHECK_NOT_COMPARABLE,
    CHECK_UNAVAILABLE,
    CHECK_WITHIN_TOLERANCE,
    compute_pnl,
    compute_realized_timeline,
    money_str,
    reconcile_with_broker,
)
from pnl_live_fixtures import CHILD_ORDER_BOOK, CHILD_POSITIONS, MASTER_ORDER_BOOK, MASTER_POSITIONS, TRADING_DAY

DAY = date(2026, 9, 11)


def order(broker_order_id, side, filled, price, minute, *, quantity=None, status="COMPLETE",
          exchange="NSE", instrument_id="1", symbol="TEST", product="INTRADAY", time=None):
    return {
        "brokerOrderId": broker_order_id,
        "transactionType": side,
        "quantity": quantity if quantity is not None else filled,
        "filledQuantity": filled,
        "averageTradedPrice": price,
        "orderStatus": status,
        "exchangeTimestamp": time if time is not None else f"2026-09-11 10:{minute:02d}:00",
        "exchange": exchange,
        "instrumentId": instrument_id,
        "tradingSymbol": symbol,
        "product": product,
    }


def position(net, ltp, *, overnight=0, multiplier=1, realized=0.0, unrealized=0.0, mtm=0.0,
             exchange="NSE", instrument_id="1", symbol="TEST-EQ", product="MIS"):
    return {
        "netQuantity": net,
        "ltp": ltp,
        "overnightQuantity": overnight,
        "multiplier": multiplier,
        "realizedPnl": realized,
        "unrealizedPnl": unrealized,
        "MtoM": mtm,
        "exchange": exchange,
        "instrumentId": instrument_id,
        "tradingSymbol": symbol,
        "product": product,
    }


def compute(orders, positions=None, day=DAY):
    fills, issues = parse_order_book_fills(orders)
    parsed = parse_positions(positions) if positions is not None else None
    return compute_pnl(fills, parsed, day, account_notes=issues)


def as_strings(result):
    return money_str(result.realized), money_str(result.unrealized), money_str(result.total)


def cash_flow_identity(orders, net, ltp):
    """total = sell value - buy value + net * ltp, independent of cost method."""
    sell = sum(Decimal(str(o["filledQuantity"])) * Decimal(str(o["averageTradedPrice"]))
               for o in orders if o["transactionType"] == "SELL")
    buy = sum(Decimal(str(o["filledQuantity"])) * Decimal(str(o["averageTradedPrice"]))
              for o in orders if o["transactionType"] == "BUY")
    return money_str(sell - buy + net * Decimal(str(ltp)))


# ── core formulas ────────────────────────────────────────────────────────────

def test_buy_then_sell_profit():
    result = compute([order("1", "BUY", 10, 100, 1), order("2", "SELL", 10, 105, 2)], [position(0, 106)])
    assert as_strings(result) == ("50.00", "0.00", "50.00")
    assert result.fully_verified


def test_buy_then_sell_loss():
    result = compute([order("1", "BUY", 10, 100, 1), order("2", "SELL", 10, 95, 2)], [position(0, 94)])
    assert as_strings(result) == ("-50.00", "0.00", "-50.00")


def test_multiple_buys_then_partial_sell_uses_weighted_average_cost():
    orders = [order("1", "BUY", 10, 100, 1), order("2", "BUY", 10, 110, 2), order("3", "SELL", 5, 120, 3)]
    result = compute(orders, [position(15, 100)])
    # avg = (10*100 + 10*110) / 20 = 105; realized 5*(120-105); open 15 @105 at LTP 100
    assert as_strings(result) == ("75.00", "-75.00", "0.00")
    assert result.instruments[0].net_quantity == 15
    assert result.instruments[0].average_cost == Decimal("105")
    assert money_str(result.total) == cash_flow_identity(orders, 15, 100)


def test_multiple_buys_then_final_close():
    orders = [
        order("1", "BUY", 10, 100, 1), order("2", "BUY", 10, 110, 2),
        order("3", "SELL", 5, 120, 3), order("4", "SELL", 15, 108, 4),
    ]
    result = compute(orders, [position(0, 108)])
    assert as_strings(result) == ("120.00", "0.00", "120.00")
    assert money_str(result.total) == cash_flow_identity(orders, 0, 108)


def test_partial_fill_then_cancellation_keeps_the_filled_quantity():
    cancelled_after_4 = order("1", "BUY", 4, 100, 1, quantity=10, status="CANCELED")
    closed = compute([cancelled_after_4, order("2", "SELL", 4, 102, 2)], [position(0, 101)])
    assert as_strings(closed) == ("8.00", "0.00", "8.00")

    still_open = compute([cancelled_after_4], [position(4, 101)])
    assert as_strings(still_open) == ("0.00", "4.00", "4.00")


def test_cancelled_and_rejected_orders_with_zero_fill_contribute_nothing():
    orders = [
        order("1", "BUY", 0, 0, 1, quantity=10, status="CANCELED"),
        order("2", "SELL", 0, 0, 2, quantity=5, status="REJECTED"),
    ]
    fills, issues = parse_order_book_fills(orders)
    assert fills == [] and issues == []
    result = compute(orders, [])
    assert as_strings(result) == ("0.00", "0.00", "0.00")
    assert result.instruments == []


def test_open_long_position():
    orders = [order("1", "BUY", 10, 100, 1)]
    result = compute(orders, [position(10, 103)])
    assert as_strings(result) == ("0.00", "30.00", "30.00")
    assert money_str(result.total) == cash_flow_identity(orders, 10, 103)


def test_short_position():
    orders = [order("1", "SELL", 10, 100, 1), order("2", "BUY", 4, 98, 2)]
    result = compute(orders, [position(-6, 101)])
    # covered 4 at 98 against cost 100 -> +8; still short 6 @100 at LTP 101 -> -6
    assert as_strings(result) == ("8.00", "-6.00", "2.00")
    assert money_str(result.total) == cash_flow_identity(orders, -6, 101)


def test_position_flip_from_long_to_short():
    orders = [order("1", "BUY", 5, 100, 1), order("2", "SELL", 8, 104, 2)]
    result = compute(orders, [position(-3, 103)])
    inst = result.instruments[0]
    assert (inst.net_quantity, inst.average_cost) == (-3, Decimal("104"))
    assert as_strings(result) == ("20.00", "3.00", "23.00")
    assert money_str(result.total) == cash_flow_identity(orders, -3, 103)


def test_non_terminating_average_is_kept_exact_until_rounding():
    orders = [order("1", "BUY", 1, 100, 1), order("2", "BUY", 2, 101, 2), order("3", "SELL", 3, 102, 3)]
    result = compute(orders, [position(0, 102)])
    assert as_strings(result) == ("4.00", "0.00", "4.00")


# ── live regression fixture (2026-09-11 CANBK) ───────────────────────────────

def test_live_fixture_master_and_child_use_their_own_fills():
    master = compute(MASTER_ORDER_BOOK, MASTER_POSITIONS, TRADING_DAY)
    child = compute(CHILD_ORDER_BOOK, CHILD_POSITIONS, TRADING_DAY)

    assert as_strings(master) == ("-1.52", "0.00", "-1.52")
    assert as_strings(child) == ("-1.48", "0.00", "-1.48")
    assert master.fully_verified and child.fully_verified
    assert master.total != child.total

    # The old card's +4200.16 was turnover; it is not what P&L reports.
    assert money_str(master.turnover) == "4200.16"
    assert money_str(child.turnover) == "4200.18"
    assert "4200.16" not in as_strings(master) + as_strings(child)


# ── inclusion, dedup and trading day ─────────────────────────────────────────

def test_duplicate_broker_order_rows_count_once():
    buy = order("1", "BUY", 10, 100, 1)
    result = compute([buy, dict(buy), order("2", "SELL", 10, 105, 2)], [position(0, 105)])
    assert as_strings(result) == ("50.00", "0.00", "50.00")


def test_partial_then_complete_uses_latest_cumulative_fill_once():
    partial = order("1", "BUY", 4, 100, 1, quantity=10, status="PARTIAL")
    complete = order("1", "BUY", 10, 101, 1, quantity=10, status="COMPLETE")
    sell = order("2", "SELL", 10, 102, 2)
    for rows in ([partial, complete, sell], [complete, partial, sell]):
        result = compute(rows, [position(0, 102)])
        assert as_strings(result) == ("10.00", "0.00", "10.00")
    # Recomputing from the same book is idempotent (no in-memory state).
    assert as_strings(compute([partial, complete, sell], [position(0, 102)])) == ("10.00", "0.00", "10.00")


def test_ist_trading_day_boundary():
    assert trading_day(datetime(2026, 9, 10, 18, 29, tzinfo=timezone.utc)) == date(2026, 9, 10)
    assert trading_day(datetime(2026, 9, 10, 18, 30, tzinfo=timezone.utc)) == date(2026, 9, 11)
    assert trading_day_start_utc(date(2026, 9, 11)) == datetime(2026, 9, 10, 18, 30, tzinfo=timezone.utc)

    orders = [
        order("1", "BUY", 10, 90, 0, time="2026-09-10 15:29:59"),   # previous trading day
        order("2", "BUY", 10, 100, 0, time="2026-09-11 00:10:00"),  # 00:10 IST is today
        order("3", "SELL", 10, 105, 0, time="2026-09-11 10:00:00"),
    ]
    result = compute(orders, [position(0, 105)])
    assert as_strings(result) == ("50.00", "0.00", "50.00")
    assert result.instruments[0].fills_today == 2


# ── unverified / partial states ──────────────────────────────────────────────

def test_positions_unavailable_flat_account_reports_realized_only():
    result = compute([order("1", "BUY", 10, 100, 1), order("2", "SELL", 10, 105, 2)], None)
    assert as_strings(result) == ("50.00", None, None)
    assert not result.fully_verified
    assert "BROKER_POSITIONS_UNAVAILABLE" in result.notes


def test_positions_unavailable_open_position_reports_nothing():
    result = compute([order("1", "BUY", 10, 100, 1)], None)
    assert as_strings(result) == (None, None, None)


def test_overnight_position_is_not_costed():
    untouched = compute([], [position(10, 105, overnight=10)])
    assert as_strings(untouched) == ("0.00", None, None)
    assert any("OVERNIGHT_POSITION_COST_BASIS_UNAVAILABLE" in n for n in untouched.notes)

    closed_today = compute([order("1", "SELL", 10, 105, 1)], [position(0, 105, overnight=10)])
    assert as_strings(closed_today) == (None, "0.00", None)


def test_unsupported_instruments_are_unverified():
    multiplier = compute([order("1", "BUY", 1, 100, 1)], [position(1, 101, multiplier=10)])
    assert as_strings(multiplier) == (None, None, None)
    assert any("UNSUPPORTED_MULTIPLIER" in n for n in multiplier.notes)

    derivative = compute(
        [order("1", "BUY", 50, 100, 1, exchange="NFO"), order("2", "SELL", 50, 101, 2, exchange="NFO")],
        [position(0, 101, exchange="NFO")],
    )
    assert as_strings(derivative) == (None, None, None)
    assert any("UNSUPPORTED_INSTRUMENT:NFO" in n for n in derivative.notes)


def test_fill_without_exchange_timestamp_is_unverified():
    result = compute([order("1", "BUY", 10, 100, 1, time=""), order("2", "SELL", 10, 105, 2)], [position(0, 105)])
    assert result.realized is None and result.total is None
    assert any("FILL_TIME_UNAVAILABLE:1" in n for n in result.notes)


def test_books_disagreeing_on_net_quantity_is_unverified():
    result = compute([order("1", "BUY", 10, 100, 1)], [position(6, 101)])
    assert as_strings(result) == (None, None, None)
    assert any("NET_QUANTITY_MISMATCH" in n for n in result.notes)


def test_open_position_without_ltp_keeps_realized_but_not_unrealized():
    orders = [order("1", "BUY", 10, 100, 1), order("2", "SELL", 5, 104, 2)]
    result = compute(orders, [position(5, 0)])
    assert as_strings(result) == ("20.00", None, None)


def test_unparseable_fill_quantity_withholds_realized():
    bad = order("1", "BUY", 10, 100, 1)
    bad["filledQuantity"] = "ten"
    result = compute([bad], [position(0, 100)])
    assert result.realized is None
    assert result.notes[0].startswith("UNPARSEABLE_FILLED_QUANTITY")


# ── broker cross-check ───────────────────────────────────────────────────────

def test_broker_check_matches_live_fixture_despite_float_noise():
    result = compute(CHILD_ORDER_BOOK, CHILD_POSITIONS, TRADING_DAY)
    check = reconcile_with_broker(result, parse_positions(CHILD_POSITIONS))
    assert check.status == CHECK_MATCH
    assert (money_str(check.realized.broker), money_str(check.realized.diff)) == ("-1.48", "0.00")
    assert {check.realized.status, check.unrealized.status, check.total.status} == {CHECK_MATCH}


@pytest.mark.parametrize(
    "broker_realized, expected",
    [(50.0, CHECK_MATCH), (50.01, CHECK_MATCH), (50.03, CHECK_WITHIN_TOLERANCE), (50.5, CHECK_MISMATCH)],
)
def test_broker_check_tolerance(broker_realized, expected):
    orders = [order("1", "BUY", 10, 100, 1), order("2", "SELL", 10, 105, 2)]
    positions = [position(0, 105, realized=broker_realized, mtm=broker_realized)]
    result = compute(orders, positions)
    check = reconcile_with_broker(result, parse_positions(positions))
    assert check.realized.status == expected
    assert check.status == expected
    assert money_str(result.realized) == "50.00"  # Jettone's value is never replaced


def test_broker_check_tolerance_scales_with_turnover():
    orders = [order("1", "BUY", 1000, 1000, 1), order("2", "SELL", 1000, 1001, 2)]
    positions = [position(0, 1001, realized=1000.6, mtm=1000.6)]
    result = compute(orders, positions)
    check = reconcile_with_broker(result, parse_positions(positions))
    assert money_str(check.tolerance) == "2.00"  # 1e-6 x 2,001,000 traded
    assert check.realized.status == CHECK_WITHIN_TOLERANCE


def test_broker_check_unavailable_and_not_comparable():
    orders = [order("1", "BUY", 10, 100, 1), order("2", "SELL", 10, 105, 2)]
    assert reconcile_with_broker(compute(orders, None), None).status == CHECK_UNAVAILABLE

    overnight_positions = [position(10, 105, overnight=10, realized=5.0, unrealized=7.0, mtm=12.0)]
    result = compute([], overnight_positions)
    check = reconcile_with_broker(result, parse_positions(overnight_positions))
    assert check.unrealized.status == CHECK_NOT_COMPARABLE
    assert check.total.status == CHECK_NOT_COMPARABLE


# ── realized P&L timeline (dashboard chart) ──────────────────────────────────

def timeline_values(orders, day=DAY):
    fills, _ = parse_order_book_fills(orders)
    points, notes = compute_realized_timeline(fills, day)
    return [money_str(p.cumulative_realized) for p in points], notes


def test_timeline_steps_after_each_closing_fill():
    orders = [
        order("1", "BUY", 10, 100, 1),
        order("2", "SELL", 10, 105, 2),
        order("3", "BUY", 5, 90, 3),
        order("4", "SELL", 5, 92, 4),
    ]
    values, notes = timeline_values(orders)
    # Opening buys don't move realized P&L; only closing sells do.
    assert values == ["0.00", "50.00", "50.00", "60.00"]
    assert notes == []


def test_timeline_last_point_matches_compute_pnl_realized():
    result = compute(MASTER_ORDER_BOOK, MASTER_POSITIONS, TRADING_DAY)
    fills, _ = parse_order_book_fills(MASTER_ORDER_BOOK)
    points, notes = compute_realized_timeline(fills, TRADING_DAY)
    assert notes == []
    assert points[-1].cumulative_realized == result.realized
    assert money_str(points[-1].cumulative_realized) == "-1.52"


def test_timeline_master_and_child_are_independent():
    master_values, _ = timeline_values(MASTER_ORDER_BOOK, TRADING_DAY)
    child_values, _ = timeline_values(CHILD_ORDER_BOOK, TRADING_DAY)
    assert master_values[-1] == "-1.52"
    assert child_values[-1] == "-1.48"
    assert master_values != child_values


def test_timeline_respects_ist_trading_day_boundary():
    orders = [
        order("1", "BUY", 10, 100, 0, time="2026-09-10 15:29:59"),
        order("2", "BUY", 10, 100, 0, time="2026-09-11 00:10:00"),
        order("3", "SELL", 10, 105, 0, time="2026-09-11 10:00:00"),
    ]
    values, _ = timeline_values(orders, DAY)
    assert values == ["0.00", "50.00"]  # the previous-day fill contributes no point


def test_timeline_skips_unparseable_fills_and_reports_a_note():
    bad = order("1", "BUY", 10, 100, 1, time="")
    good = [order("2", "BUY", 5, 100, 2), order("3", "SELL", 5, 103, 3)]
    values, notes = timeline_values([bad] + good)
    assert values == ["0.00", "15.00"]
    assert notes == ["FILL_TIME_UNAVAILABLE:1"]


def test_timeline_multi_instrument_totals_across_symbols():
    orders = [
        order("1", "BUY", 10, 100, 1, instrument_id="A", symbol="AAA"),
        order("2", "BUY", 10, 50, 1, instrument_id="B", symbol="BBB"),
        order("3", "SELL", 10, 106, 2, instrument_id="A", symbol="AAA"),  # +60
        order("4", "SELL", 10, 48, 3, instrument_id="B", symbol="BBB"),   # -20
    ]
    values, notes = timeline_values(orders)
    assert values == ["0.00", "0.00", "60.00", "40.00"]
    assert notes == []


def test_money_is_rounded_half_up_only_at_the_boundary():
    assert money_str(Decimal("-0.001")) == "0.00"
    assert money_str(Decimal("1.005")) == "1.01"
    assert money_str(Decimal("-1.005")) == "-1.01"
    assert money_str(None) is None
