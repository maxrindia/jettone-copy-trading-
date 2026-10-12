"""Per-day (DAILY, never cumulative) realized P&L for the Group Strategy chart
(app/services/pnl/historical.py::compute_historical_daily and
app/api/dashboard.py::_combine_strategy_daily). Pure function tests.

The invariant under test: every trading day stands alone. One day (or one
instrument within a day) that can't be costed is reported PARTIAL/UNAVAILABLE
with reasons and never erases, shifts, or leaks into any other day.
"""
from datetime import date, datetime, timezone
from decimal import Decimal

from app.api.dashboard import _combine_strategy_daily
from app.services.pnl.historical import DailyPnl, HistoricalFill, compute_historical_daily

SEP22, SEP23, SEP24, SEP25 = date(2026, 9, 22), date(2026, 9, 23), date(2026, 9, 24), date(2026, 9, 25)


def fill(day, side, qty, price, hour=4, minute=0, *, symbol="RELIANCE", exchange="NSE", product="MIS"):
    return HistoricalFill(
        account_id="acc", exchange=exchange, symbol=symbol, product_type=product, side=side,
        quantity=qty, price=None if price is None else Decimal(str(price)),
        created_at=datetime(day.year, day.month, day.day, hour, minute, tzinfo=timezone.utc), day=day,
    )


def round_trip(day, buy, sell, qty=10, **kw):
    return [fill(day, "BUY", qty, buy, 4, 0, **kw), fill(day, "SELL", qty, sell, 5, 0, **kw)]


def by_day(result):
    return {d.day: d for d in result}


# ── pure per-day computation ────────────────────────────────────────────────

def test_each_day_is_that_days_pnl_not_a_running_total():
    r = by_day(compute_historical_daily(
        round_trip(SEP22, 100, 105) + round_trip(SEP23, 100, 103) + round_trip(SEP24, 100, 98)
    ))
    assert r[SEP22].daily_pnl == Decimal("50")
    assert r[SEP23].daily_pnl == Decimal("30")   # NOT 80 (Sep 22 + Sep 23)
    assert r[SEP24].daily_pnl == Decimal("-20")  # NOT 60
    assert all(d.status == "OK" for d in r.values())


def test_a_later_losing_day_does_not_alter_an_earlier_days_value():
    only_first = by_day(compute_historical_daily(round_trip(SEP22, 100, 105)))
    with_second = by_day(compute_historical_daily(round_trip(SEP22, 100, 105) + round_trip(SEP23, 100, 50)))
    assert only_first[SEP22].daily_pnl == with_second[SEP22].daily_pnl == Decimal("50")


def test_days_are_independent_and_sorted():
    out = compute_historical_daily(round_trip(SEP24, 10, 11) + round_trip(SEP22, 10, 12))
    assert [d.day for d in out] == [SEP22, SEP24]


def test_nse_longterm_same_day_round_trip_is_included():
    r = by_day(compute_historical_daily(round_trip(SEP24, 100, 102, product="LONGTERM")))
    assert r[SEP24].daily_pnl == Decimal("20")
    assert r[SEP24].status == "OK"


def test_bse_longterm_round_trip_is_included():
    r = by_day(compute_historical_daily(round_trip(SEP24, 50, 51, exchange="BSE", product="LONGTERM")))
    assert r[SEP24].daily_pnl == Decimal("10")


def test_longterm_and_intraday_same_symbol_are_separate_positions():
    fills = (
        [fill(SEP24, "BUY", 10, 100, 4, 0, product="LONGTERM"), fill(SEP24, "SELL", 10, 110, 5, 0, product="LONGTERM")]
        + [fill(SEP24, "BUY", 5, 200, 6, 0, product="MIS"), fill(SEP24, "SELL", 5, 190, 7, 0, product="MIS")]
    )
    assert by_day(compute_historical_daily(fills))[SEP24].daily_pnl == Decimal("50")  # +100 - 50


def test_weighted_average_cost_is_preserved_across_partial_fills():
    fills = [
        fill(SEP22, "BUY", 10, 100, 4, 0), fill(SEP22, "BUY", 10, 110, 4, 1),   # avg 105
        fill(SEP22, "SELL", 20, 108, 5, 0),
    ]
    assert by_day(compute_historical_daily(fills))[SEP22].daily_pnl == Decimal("60")


def test_nfo_fills_never_become_fake_zero_and_only_degrade_their_own_day():
    fills = (
        round_trip(SEP22, 100, 105)                                              # valid NSE
        + round_trip(SEP22, 200, 210, symbol="NIFTY", exchange="NFO", qty=65)    # unidentifiable contract
        + round_trip(SEP23, 100, 103)                                            # fully valid
    )
    r = by_day(compute_historical_daily(fills))
    assert r[SEP22].status == "PARTIAL"
    assert r[SEP22].daily_pnl == Decimal("50")   # NSE part only; NFO is not guessed
    assert any(x.startswith("UNSUPPORTED_INSTRUMENT:NFO") for x in r[SEP22].reasons)
    assert r[SEP23].status == "OK" and r[SEP23].daily_pnl == Decimal("30")


def test_nfo_only_day_is_unavailable_with_null_not_zero():
    r = by_day(compute_historical_daily(round_trip(SEP22, 200, 210, symbol="NIFTY", exchange="NFO", qty=65)))
    assert r[SEP22].status == "UNAVAILABLE"
    assert r[SEP22].daily_pnl is None
    assert r[SEP22].reasons


def test_missing_fill_price_only_affects_its_own_day():
    fills = round_trip(SEP22, 100, 105) + [fill(SEP23, "BUY", 10, None, 4, 0), fill(SEP23, "SELL", 10, 105, 5, 0)] \
        + round_trip(SEP24, 100, 102)
    r = by_day(compute_historical_daily(fills))
    assert r[SEP22].daily_pnl == Decimal("50") and r[SEP22].status == "OK"
    assert r[SEP23].daily_pnl is None and r[SEP23].status == "UNAVAILABLE"
    assert r[SEP23].reasons == ["MISSING_FILL_PRICE:RELIANCE"]
    assert r[SEP24].daily_pnl == Decimal("20") and r[SEP24].status == "OK"


def test_missing_price_on_one_instrument_keeps_the_other_instrument_that_day():
    fills = round_trip(SEP22, 100, 105) + [
        fill(SEP22, "BUY", 5, 0, 6, 0, symbol="TCS"), fill(SEP22, "SELL", 5, 50, 7, 0, symbol="TCS"),
    ]
    d = by_day(compute_historical_daily(fills))[SEP22]
    assert d.daily_pnl == Decimal("50") and d.status == "PARTIAL"


def test_open_position_at_day_end_is_partial_with_realized_only_and_named_reason():
    fills = [
        fill(SEP25, "BUY", 10, 100, 4, 0), fill(SEP25, "SELL", 4, 110, 5, 0),   # 6 left open, no mark price
    ]
    d = by_day(compute_historical_daily(fills))[SEP25]
    assert d.status == "PARTIAL"
    assert d.daily_pnl == Decimal("40")          # realized only; unrealized never fabricated
    assert d.reasons == ["OPEN_POSITION_AT_DAY_END:RELIANCE:6"]


def test_open_position_on_one_day_does_not_change_other_days():
    fills = [fill(SEP22, "BUY", 10, 100, 4, 0)] + round_trip(SEP23, 100, 103)
    r = by_day(compute_historical_daily(fills))
    assert r[SEP22].status == "PARTIAL"
    assert r[SEP23].status == "OK" and r[SEP23].daily_pnl == Decimal("30")


def test_zero_quantity_fills_are_ignored_and_empty_input_yields_no_days():
    assert compute_historical_daily([]) == []
    assert compute_historical_daily([fill(SEP22, "BUY", 0, 100)]) == []


# ── strategy-level combine ──────────────────────────────────────────────────

M, F1, F2 = "m" * 8 + "-1", "f1" * 4 + "-2", "f2" * 4 + "-3"


def d(day, pnl, status="OK", reasons=()):
    return DailyPnl(day, None if pnl is None else Decimal(str(pnl)), status, list(reasons))


def test_strategy_daily_sums_master_and_followers_per_day_not_cumulatively():
    out = _combine_strategy_daily(M, [F1], {
        M: {SEP22: d(SEP22, 50), SEP23: d(SEP23, 30)},
        F1: {SEP22: d(SEP22, 10), SEP23: d(SEP23, -5)},
    })
    assert [(x["date"], x["daily_pnl"]) for x in out["daily"]] == [("2026-09-22", "60.00"), ("2026-09-23", "25.00")]
    assert out["unavailable_reason"] is None


def test_master_that_is_also_a_member_is_counted_once():
    out = _combine_strategy_daily(M, [M, F1, F1], {M: {SEP22: d(SEP22, 50)}, F1: {SEP22: d(SEP22, 10)}})
    assert out["daily"][0]["daily_pnl"] == "60.00"


def test_unusable_day_never_erases_other_days_in_the_strategy():
    out = _combine_strategy_daily(M, [], {M: {
        SEP22: d(SEP22, 50, "PARTIAL", ["UNSUPPORTED_INSTRUMENT:NFO:NIFTY"]),
        SEP23: d(SEP23, 30),
        SEP24: d(SEP24, 20),
        SEP25: d(SEP25, None, "UNAVAILABLE", ["UNSUPPORTED_INSTRUMENT:NFO:NIFTY"]),
    }})
    by = {x["date"]: x for x in out["daily"]}
    assert by["2026-09-22"]["status"] == "PARTIAL" and by["2026-09-22"]["daily_pnl"] == "50.00"
    assert by["2026-09-23"]["daily_pnl"] == "30.00" and by["2026-09-24"]["daily_pnl"] == "20.00"
    assert by["2026-09-25"]["daily_pnl"] is None and by["2026-09-25"]["status"] == "UNAVAILABLE"
    assert out["unavailable_reason"] is None   # usable points exist


def test_unavailable_reason_only_when_zero_usable_points():
    out = _combine_strategy_daily(M, [], {M: {SEP22: d(SEP22, None, "UNAVAILABLE", ["UNSUPPORTED_INSTRUMENT:NFO:NIFTY"])}})
    assert all(x["daily_pnl"] is None for x in out["daily"])
    assert out["unavailable_reason"] and "NIFTY" in out["unavailable_reason"]


def test_no_trading_at_all_is_empty_not_unavailable():
    out = _combine_strategy_daily(M, [F1], {M: {}, F1: {}})
    assert out == {"daily": [], "unavailable_reason": None}


def test_unverifiable_follower_day_is_excluded_not_zeroed_and_makes_day_partial():
    out = _combine_strategy_daily(M, [F1], {
        M: {SEP22: d(SEP22, 50)},
        F1: {SEP22: d(SEP22, None, "UNAVAILABLE", ["MISSING_FILL_PRICE:YESBANK"])},
    })
    day = out["daily"][0]
    assert day["daily_pnl"] == "50.00" and day["status"] == "PARTIAL"
    assert any("MISSING_FILL_PRICE" in r for r in day["reasons"])


def test_master_unavailable_day_is_unavailable_even_if_a_follower_has_value():
    out = _combine_strategy_daily(M, [F1], {
        M: {SEP22: d(SEP22, None, "UNAVAILABLE", ["X"])},
        F1: {SEP22: d(SEP22, 10)},
    })
    assert out["daily"][0]["daily_pnl"] is None


def test_follower_only_day_counts_master_as_verified_no_trades():
    out = _combine_strategy_daily(M, [F1], {M: {}, F1: {SEP22: d(SEP22, 10)}})
    assert out["daily"][0]["daily_pnl"] == "10.00" and out["daily"][0]["status"] == "OK"


def test_no_master_is_a_named_unavailable():
    out = _combine_strategy_daily(None, [F1], {F1: {SEP22: d(SEP22, 10)}})
    assert out["daily"] == [] and out["unavailable_reason"]
