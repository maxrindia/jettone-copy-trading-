"""Historical (non-Today) realized P&L from Jettone's own `orders` table
(app/services/pnl/historical.py). Pure function tests - no DB, no network.

Every case here mirrors a real constraint from the module docstring: this
must never approximate an overnight carry as flat, never cost a non-cash-
equity or carry-product fill, and never silently drop one bad fill from an
otherwise-plausible total - all such cases withhold the WHOLE result.
"""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from app.services.pnl.historical import HistoricalFill, compute_historical_realized

ACCOUNT = "acc-1"


def fill(day, side, qty, price, hour, minute, *, symbol="RELIANCE", exchange="NSE", product="MIS"):
    return HistoricalFill(
        account_id=ACCOUNT,
        exchange=exchange,
        symbol=symbol,
        product_type=product,
        side=side,
        quantity=qty,
        price=Decimal(str(price)),
        created_at=datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc),
        day=day,
    )


DAY1 = date(2026, 9, 15)
DAY2 = date(2026, 9, 16)


def test_no_fills_is_a_verified_zero_not_unavailable():
    """No trading activity in the period is a real, known answer (₹0.00),
    not the same as 'could not be calculated'."""
    result = compute_historical_realized([])
    assert result.available
    assert result.realized == Decimal("0")
    assert result.notes == []


def test_simple_round_trip_one_day_one_instrument():
    fills = [
        fill(DAY1, "BUY", 10, "100.00", 10, 0),
        fill(DAY1, "SELL", 10, "105.00", 11, 0),
    ]
    result = compute_historical_realized(fills)
    assert result.available
    assert result.realized == Decimal("50.00")  # 10 * (105 - 100)


def test_short_round_trip_profits_when_price_falls():
    fills = [
        fill(DAY1, "SELL", 10, "100.00", 10, 0),
        fill(DAY1, "BUY", 10, "95.00", 11, 0),
    ]
    result = compute_historical_realized(fills)
    assert result.available
    assert result.realized == Decimal("50.00")  # short: cost - buy = 10 * (100 - 95)


def test_partial_fills_use_weighted_average_cost():
    fills = [
        fill(DAY1, "BUY", 5, "100.00", 10, 0),
        fill(DAY1, "BUY", 5, "110.00", 10, 5),   # avg cost now 105
        fill(DAY1, "SELL", 10, "120.00", 11, 0),
    ]
    result = compute_historical_realized(fills)
    assert result.available
    assert result.realized == Decimal("150.00")  # 10 * (120 - 105)


def test_sums_across_multiple_completed_days():
    fills = [
        fill(DAY1, "BUY", 10, "100.00", 10, 0),
        fill(DAY1, "SELL", 10, "105.00", 11, 0),   # +50
        fill(DAY2, "BUY", 20, "50.00", 10, 0),
        fill(DAY2, "SELL", 20, "48.00", 11, 0),    # -40
    ]
    result = compute_historical_realized(fills)
    assert result.available
    assert result.realized == Decimal("10.00")  # 50 - 40


def test_multiple_instruments_same_day_sum_independently():
    fills = [
        fill(DAY1, "BUY", 10, "100.00", 10, 0, symbol="RELIANCE"),
        fill(DAY1, "SELL", 10, "110.00", 10, 5, symbol="RELIANCE"),  # +100
        fill(DAY1, "BUY", 5, "200.00", 10, 1, symbol="TCS"),
        fill(DAY1, "SELL", 5, "190.00", 10, 6, symbol="TCS"),        # -50
    ]
    result = compute_historical_realized(fills)
    assert result.available
    assert result.realized == Decimal("50.00")


def test_non_flat_ending_position_marks_whole_result_unavailable():
    """A position still open at day-end - either a real overnight carry, or
    the flat-at-open assumption being wrong for that day - must never be
    silently priced with today's LTP or ignored; the whole call fails
    closed."""
    fills = [
        fill(DAY1, "BUY", 10, "100.00", 10, 0),
        # never sold - position left open at day end.
    ]
    result = compute_historical_realized(fills)
    assert not result.available
    assert result.realized is None
    assert any("CARRY_SUSPECTED" in n for n in result.notes)


def test_suspected_carry_on_one_instrument_invalidates_the_whole_account():
    """Fail-closed is per call (per account), not per instrument - a clean
    RELIANCE day must not mask a suspect TCS day in the same result."""
    fills = [
        fill(DAY1, "BUY", 10, "100.00", 10, 0, symbol="RELIANCE"),
        fill(DAY1, "SELL", 10, "105.00", 10, 5, symbol="RELIANCE"),  # clean, +50
        fill(DAY1, "BUY", 5, "200.00", 11, 0, symbol="TCS"),          # never closed
    ]
    result = compute_historical_realized(fills)
    assert not result.available


def test_missing_fill_price_withholds_entire_result():
    fills = [
        fill(DAY1, "BUY", 10, "100.00", 10, 0),
        fill(DAY1, "SELL", 10, "0", 11, 0),  # e.g. a child square-off recorded at avg_price 0
    ]
    result = compute_historical_realized(fills)
    assert not result.available
    assert any("MISSING_FILL_PRICE" in n for n in result.notes)


def test_non_cash_equity_exchange_is_unsupported():
    fills = [fill(DAY1, "BUY", 10, "100.00", 10, 0, exchange="MCX")]
    result = compute_historical_realized(fills)
    assert not result.available
    assert any("UNSUPPORTED_INSTRUMENT" in n for n in result.notes)


@pytest.mark.parametrize("product", ["CNC", "NRML", "DELIVERY"])
def test_carry_products_are_unsupported_not_approximated(product):
    """CNC/NRML are genuine overnight-carry products on this platform - never
    treated as flat-at-open the way MIS/INTRADAY legitimately are."""
    fills = [fill(DAY1, "BUY", 10, "100.00", 10, 0, product=product)]
    result = compute_historical_realized(fills)
    assert not result.available
    assert any("CARRY_PRODUCT_UNSUPPORTED" in n for n in result.notes)


def test_points_are_chronologically_ordered_across_instruments():
    fills = [
        fill(DAY1, "BUY", 5, "200.00", 10, 1, symbol="TCS"),
        fill(DAY1, "BUY", 10, "100.00", 10, 0, symbol="RELIANCE"),
        fill(DAY1, "SELL", 10, "110.00", 10, 5, symbol="RELIANCE"),
        fill(DAY1, "SELL", 5, "190.00", 10, 6, symbol="TCS"),
    ]
    result = compute_historical_realized(fills)
    assert result.available
    times = [p.time for p in result.points]
    assert times == sorted(times)
    # last point's cumulative total matches the overall realized figure
    assert result.points[-1].cumulative_realized == result.realized


def test_zero_and_negative_quantity_fills_are_ignored_not_costed():
    fills = [
        fill(DAY1, "BUY", 10, "100.00", 10, 0),
        fill(DAY1, "SELL", 10, "105.00", 11, 0),
        HistoricalFill(
            account_id=ACCOUNT, exchange="NSE", symbol="RELIANCE", product_type="MIS",
            side="BUY", quantity=0, price=Decimal("100.00"),
            created_at=datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc), day=DAY1,
        ),
    ]
    result = compute_historical_realized(fills)
    assert result.available
    assert result.realized == Decimal("50.00")


# ── symbol normalization: "RELIANCE" vs "RELIANCE-EQ" (NSE) must group as
# one instrument, matching the NSE +EQ convention already established at
# app/services/alice_blue/instruments.py::resolve_instrument_info. Found in
# production: INTERNAL_UI orders store the -EQ suffixed form, BROKER_EXTERNAL
# orders mirrored from the broker's order-book endpoint do not - splitting
# one real round trip into two "instruments" that each looked unclosed. ────

def test_nse_symbol_with_and_without_eq_suffix_group_as_one_instrument():
    """The exact production scenario: an INTERNAL_UI BUY recorded as
    'RELIANCE-EQ' and its real closing BROKER_EXTERNAL SELL recorded as
    plain 'RELIANCE' must be recognized as the same round trip."""
    fills = [
        fill(DAY1, "BUY", 1, "1310.50", 7, 44, symbol="RELIANCE-EQ"),
        fill(DAY1, "SELL", 1, "1310.80", 8, 19, symbol="RELIANCE"),
    ]
    result = compute_historical_realized(fills)
    assert result.available
    assert result.realized == Decimal("0.30")  # 1 * (1310.80 - 1310.50)


def test_yesbank_mixed_suffix_fills_across_a_whole_day_net_correctly():
    """The other production case: several -EQ buys mixed with plain-symbol
    buys/sells for the same instrument across one day must all combine into
    a single position rather than fragmenting into two false 'open'
    instruments that both look like a suspected carry."""
    fills = [
        fill(DAY1, "BUY", 1, "22.45", 5, 13, symbol="YESBANK-EQ"),
        fill(DAY1, "BUY", 1, "22.48", 6, 21, symbol="YESBANK-EQ"),
        fill(DAY1, "SELL", 1, "22.47", 8, 30, symbol="YESBANK"),
        fill(DAY1, "SELL", 1, "22.47", 8, 31, symbol="YESBANK"),
    ]
    result = compute_historical_realized(fills)
    assert result.available  # would be CARRY_SUSPECTED on both keys without normalization
    assert result.realized == Decimal("0.01")  # 2 * 22.47 - (22.45 + 22.48)


def test_nse_index_symbols_are_never_eq_suffixed():
    """NIFTY/BANKNIFTY are exempt from the +EQ rule (same exemption as
    resolve_instrument_info) - a NIFTY fill must not be grouped with, or
    turned into, 'NIFTY-EQ'."""
    fills = [
        fill(DAY1, "BUY", 1, "100.00", 10, 0, symbol="NIFTY"),
        fill(DAY1, "SELL", 1, "105.00", 11, 0, symbol="NIFTY"),
    ]
    result = compute_historical_realized(fills)
    assert result.available
    assert result.realized == Decimal("5.00")


def test_bse_symbols_are_never_eq_suffixed():
    """BSE equities canonically carry NO -EQ suffix at all (see
    KNOWN_INSTRUMENTS: ("BSE", "RELIANCE") -> "RELIANCE", never
    "RELIANCE-EQ") - appending one would invent a convention this project
    doesn't use, so a BSE fill must group under its plain symbol only."""
    fills = [
        fill(DAY1, "BUY", 1, "100.00", 10, 0, symbol="RELIANCE", exchange="BSE"),
        fill(DAY1, "SELL", 1, "105.00", 11, 0, symbol="RELIANCE", exchange="BSE"),
    ]
    result = compute_historical_realized(fills)
    assert result.available
    assert result.realized == Decimal("5.00")


def test_bse_and_nse_same_symbol_are_never_conflated():
    """A BSE 'RELIANCE' fill and an NSE 'RELIANCE-EQ' fill are two separate
    real accounts/instruments (different exchange order books) - the
    normalization must not accidentally merge them."""
    fills = [
        fill(DAY1, "BUY", 1, "100.00", 10, 0, symbol="RELIANCE", exchange="BSE"),
        # No matching sell for the BSE leg - if this were merged with the
        # NSE leg below, the whole result would incorrectly resolve to
        # "available" instead of flagging the BSE position as unclosed.
        fill(DAY1, "BUY", 1, "100.00", 10, 5, symbol="RELIANCE-EQ", exchange="NSE"),
        fill(DAY1, "SELL", 1, "105.00", 11, 0, symbol="RELIANCE-EQ", exchange="NSE"),
    ]
    result = compute_historical_realized(fills)
    assert not result.available
    assert any("CARRY_SUSPECTED:RELIANCE:" in n for n in result.notes)
