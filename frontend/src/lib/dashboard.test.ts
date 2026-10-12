import { describe, expect, it } from "vitest";
import {
  accountStatusLabel,
  brokerCheckLabel,
  buildDailyChartPoints,
  buildCumulativeSeries,
  buildPnlSnapshot,
  buildStrategyDailySeries,
  buildStrategySeries,
  dailyPartialNote,
  describeDailyReason,
  describeDailyReasons,
  layoutCumulativeLine,
  sumMoney,
  combinedPnlUnavailableReason,
  edgeCaption,
  edgeDirectionLabel,
  edgeItemTooltip,
  fillRateCaption,
  fillRateLabel,
  formatIstDate,
  formatIstTime,
  issueSeverityClass,
  istDayStart,
  realizedPnlRangeCaption,
  realizedPnlUnavailableReason,
  unrealizedPnlRangeCaption,
  unrealizedPnlUnavailableReason,
  rangeSummaryLabel,
  strategyFillRateLabel,
  strategyPnlDisplay,
  strategyStatusLabel,
  type CopyFillStats,
  type DashboardAccountPnl,
  type DashboardPnl,
  type DashboardRealizedTimeline,
  type DateRangeInfo,
  type ExecutionEdge,
  type ExecutionEdgeItem,
  type StrategyPerformanceRow,
} from "./dashboard";

function range(overrides: Partial<DateRangeInfo>): DateRangeInfo {
  return {
    key: "today", start: "2026-09-11", end: "2026-09-11", is_today: true, includes_today: true,
    ...overrides,
  };
}

describe("IST time formatting", () => {
  it("istDayStart anchors to explicit IST midnight, not a bare local-time string", () => {
    expect(istDayStart("2026-09-11")).toBe("2026-09-11T00:00:00+05:30");
  });

  it("formatIstTime shows the IST wall-clock time regardless of the timestamp's own offset", () => {
    expect(formatIstTime("2026-09-11T09:41:15+05:30")).toBe("09:41 am");
    expect(formatIstTime("2026-09-11T04:11:15Z")).toBe("09:41 am");
  });

  it("formatIstDate labels the date itself, independent of the viewer's local timezone", () => {
    expect(formatIstDate("2026-09-11", { weekday: "short", day: "numeric", month: "short" })).toBe("Fri, 11 Sept");
  });
});

describe("rangeSummaryLabel", () => {
  it("shows the fixed option label for the four presets", () => {
    expect(rangeSummaryLabel(range({ key: "today" }))).toBe("Today");
    expect(rangeSummaryLabel(range({ key: "yesterday" }))).toBe("Yesterday");
    expect(rangeSummaryLabel(range({ key: "7d" }))).toBe("Last 7 Days");
    expect(rangeSummaryLabel(range({ key: "30d" }))).toBe("Last 30 Days");
  });

  it("shows an explicit date (or span) for a custom range", () => {
    expect(rangeSummaryLabel(range({ key: "custom", start: "2026-09-10", end: "2026-09-10" }))).toBe("10 Sept 2026");
    expect(rangeSummaryLabel(range({ key: "custom", start: "2026-09-01", end: "2026-09-10" }))).toBe("1 Sept – 10 Sept 2026");
  });
});

function pnl(overrides: Partial<DashboardPnl>): DashboardPnl {
  return {
    combined_realized: null, combined_unrealized: null, combined_total: null,
    pnl_basis: "GROSS", accounts_total: 0, accounts_unavailable: 0, accounts: [],
    realized_unavailable_reason: null, unrealized_unavailable_reason: null,
    ...overrides,
  };
}

describe("combinedPnlUnavailableReason", () => {
  it("is null once Total is available, regardless of which range it came from", () => {
    expect(combinedPnlUnavailableReason(pnl({ combined_total: "-3.00" }))).toBeNull();
  });

  it("surfaces realized's reason first, then unrealized's, when Total is unavailable", () => {
    expect(combinedPnlUnavailableReason(pnl({ realized_unavailable_reason: "Some accounts have unverifiable historical fills" })))
      .toBe("Some accounts have unverifiable historical fills");
    expect(combinedPnlUnavailableReason(pnl({ unrealized_unavailable_reason: "No historical end-of-day position or price data exists before Today" })))
      .toBe("No historical end-of-day position or price data exists before Today");
  });

  it("falls back to a generic message when neither reason was given", () => {
    expect(combinedPnlUnavailableReason(pnl({}))).toBe("Unavailable");
  });
});

describe("realizedPnlUnavailableReason / unrealizedPnlUnavailableReason", () => {
  it("each is null once its own value is available, independent of the other", () => {
    expect(realizedPnlUnavailableReason(pnl({ combined_realized: "47.00", unrealized_unavailable_reason: "no historical price data" })))
      .toBeNull();
    expect(unrealizedPnlUnavailableReason(pnl({ combined_unrealized: "0.00" }))).toBeNull();
  });

  it("realized never inherits unrealized's unavailable reason, or vice versa", () => {
    const p = pnl({
      combined_realized: "47.00",
      combined_unrealized: null,
      unrealized_unavailable_reason: "No historical end-of-day position or price data exists before Today",
    });
    expect(realizedPnlUnavailableReason(p)).toBeNull();
    expect(unrealizedPnlUnavailableReason(p)).toBe("No historical end-of-day position or price data exists before Today");
  });
});

describe("realizedPnlRangeCaption / unrealizedPnlRangeCaption", () => {
  it("labels Realized with the selected period, not always 'Today'", () => {
    expect(realizedPnlRangeCaption(range({ is_today: true }))).toBe("Closed positions · Today");
    expect(realizedPnlRangeCaption(range({ key: "yesterday", is_today: false, includes_today: false }))).toBe(
      "Closed positions · Yesterday",
    );
    expect(realizedPnlRangeCaption(range({ key: "7d", is_today: false, includes_today: true }))).toBe(
      "Closed positions · Last 7 Days",
    );
  });

  it("labels Unrealized as a right-now snapshot whenever it's shown at all", () => {
    expect(unrealizedPnlRangeCaption(range({ is_today: true }))).toBe("Open positions · Mark-to-market");
    expect(unrealizedPnlRangeCaption(range({ key: "7d", is_today: false, includes_today: true }))).toBe(
      "Open positions · As of now",
    );
  });
});

describe("fillRateLabel / fillRateCaption", () => {
  function fill(overrides: Partial<CopyFillStats>): CopyFillStats {
    return {
      filled_quantity: 0, target_quantity: 0, rate_percent: null,
      filled_orders: 0, working_orders: 0, rejected_orders: 0, considered_orders: 0,
      ...overrides,
    };
  }

  it("shows the percentage and a quantity breakdown", () => {
    const f = fill({ filled_quantity: 34, target_quantity: 35, rate_percent: 97.1, filled_orders: 8, rejected_orders: 1 });
    expect(fillRateLabel(f)).toBe("97.1%");
    expect(fillRateCaption(f)).toBe("34 / 35 filled · 1 rejected/cancelled");
  });

  it("shows an honest empty state when nothing was copied in range", () => {
    expect(fillRateLabel(fill({}))).toBe("0%");
    expect(fillRateCaption(fill({}))).toBe("No copies in range");
  });
});

function edge(overrides: Partial<ExecutionEdge>): ExecutionEdge {
  return { total: null, average_per_share: null, total_quantity: 0, sample_count: 0, direction: null, items: [], ...overrides };
}

describe("edgeDirectionLabel / edgeCaption", () => {
  it("labels each direction and the no-data case using follower-facing language", () => {
    expect(edgeDirectionLabel(edge({ direction: "children_better" }))).toBe("Followers executed better");
    expect(edgeDirectionLabel(edge({ direction: "children_worse" }))).toBe("Followers executed worse");
    expect(edgeDirectionLabel(edge({ direction: null }))).toBe("No copied fills in range");
  });

  it("adds the impact-weighted average per share", () => {
    const e = edge({ direction: "children_better", average_per_share: "0.10" });
    expect(edgeCaption(e)).toBe("Followers executed better · avg +₹0.10/share");
  });
});

describe("edgeItemTooltip", () => {
  function item(overrides: Partial<ExecutionEdgeItem>): ExecutionEdgeItem {
    return {
      symbol: "CANBK", side: "BUY", time: "2026-09-11T09:41:15+05:30", quantity: 1,
      master_price: "100.10", child_price: "100.00", edge_per_share: "0.10", impact: "0.10",
      child_account_id: "child", group_strategy_id: "strat-1",
      ...overrides,
    };
  }

  it("shows master price, child price, quantity, impact, and per-share edge, and never labels it P&L", () => {
    const tip = edgeItemTooltip(item({}));
    expect(tip).toContain("Execution quality · not P&L");
    expect(tip).toContain("CANBK BUY × 1");
    expect(tip).toContain("Impact +₹0.10 (+₹0.10/share)");
  });

  it("includes the strategy name when supplied", () => {
    expect(edgeItemTooltip(item({}), "Intraday Alpha")).toContain("Strategy: Intraday Alpha");
  });
});

describe("issueSeverityClass", () => {
  it("returns a distinct, non-empty class per severity", () => {
    const classes = (["INFO", "WARNING", "ERROR", "CRITICAL"] as const).map(issueSeverityClass);
    expect(new Set(classes).size).toBe(3); // ERROR and CRITICAL intentionally share a look
    for (const c of classes) expect(c.length).toBeGreaterThan(0);
  });
});

describe("accountStatusLabel / brokerCheckLabel", () => {
  function account(overrides: Partial<DashboardAccountPnl>): DashboardAccountPnl {
    return {
      account_id: "a", display_name: "Dhinesh", account_type: "MASTER",
      is_active: true, session_valid: true,
      realized_pnl: null, unrealized_pnl: null, total_pnl: null,
      pnl_basis: "GROSS", pnl_status: "OK", pnl_notes: [], open_positions_count: 0,
      broker_check: {
        status: "MATCH", tolerance: "0.05",
        realized: { jettone: null, broker: null, diff: null, status: "MATCH" },
        unrealized: { jettone: null, broker: null, diff: null, status: "MATCH" },
        total: { jettone: null, broker: null, diff: null, status: "MATCH" },
      },
      trading_day: "2026-09-11", as_of: "2026-09-11T09:00:00+00:00",
      ...overrides,
    };
  }

  it("labels connected, inactive, and not-authenticated accounts distinctly", () => {
    expect(accountStatusLabel(account({}))).toBe("Connected");
    expect(accountStatusLabel(account({ is_active: false }))).toBe("Inactive");
    expect(accountStatusLabel(account({ session_valid: false }))).toBe("Not authenticated");
  });

  it("surfaces a broker mismatch distinctly from a match, and nothing when not comparable", () => {
    expect(brokerCheckLabel(account({}))).toEqual({ label: "Broker matched", ok: true });
    const mismatched = account({ broker_check: { ...account({}).broker_check, status: "MISMATCH" } });
    expect(brokerCheckLabel(mismatched)).toEqual({ label: "Broker mismatch", ok: false });
    const unavailable = account({ broker_check: { ...account({}).broker_check, status: "UNAVAILABLE" } });
    expect(brokerCheckLabel(unavailable)).toBeNull();
  });
});

describe("strategy performance helpers", () => {
  function row(overrides: Partial<StrategyPerformanceRow>): StrategyPerformanceRow {
    return {
      id: "s1", name: "Intraday Alpha", status: "RUNNING", status_reason: null,
      master: { account_id: "m", display_name: "Dhinesh" },
      followers: [], follower_count: 2,
      pnl: {
        master_pnl: "1.00", followers_pnl: "2.00", total_pnl: "3.00", realized_pnl: null,
        unavailable_reason: null, partial_note: null,
      },
      execution: {
        considered_orders: 10, filled_orders: 10, working_orders: 0, rejected_orders: 0,
        target_quantity: 20, filled_quantity: 20, rate_percent: 100, execution_impact: "12.00", average_per_share: "0.60",
      },
      ...overrides,
    };
  }

  it("shows Running, Disabled, or the specific not-running reason", () => {
    expect(strategyStatusLabel(row({ status: "RUNNING" }))).toBe("Running");
    expect(strategyStatusLabel(row({ status: "DISABLED", status_reason: "Trading disabled for this strategy" }))).toBe(
      "Disabled",
    );
    expect(strategyStatusLabel(row({ status: "NOT_RUNNING", status_reason: "No live master session" }))).toBe(
      "No live master session",
    );
  });

  it("shows the strategy's combined (Today) P&L when available", () => {
    expect(strategyPnlDisplay(row({}))).toEqual({ value: "3.00", reason: null, isRealizedOnly: false, partialNote: null });
  });

  it("falls back to the historical realized figure, flagged, when Total isn't available", () => {
    const historical = row({
      pnl: {
        master_pnl: null, followers_pnl: null, total_pnl: null, realized_pnl: "47.00",
        unavailable_reason: null, partial_note: null,
      },
    });
    expect(strategyPnlDisplay(historical)).toEqual({ value: "47.00", reason: null, isRealizedOnly: true, partialNote: null });
  });

  it("shows the unavailable reason only when neither total nor realized exist", () => {
    const unavailable = row({
      pnl: {
        master_pnl: null, followers_pnl: null, total_pnl: null, realized_pnl: null,
        unavailable_reason: "Some linked accounts have unverifiable historical fills (missing price data or a suspected overnight carry)",
        partial_note: null,
      },
    });
    expect(strategyPnlDisplay(unavailable)).toEqual({
      value: null,
      reason: "Some linked accounts have unverifiable historical fills (missing price data or a suspected overnight carry)",
      isRealizedOnly: false,
      partialNote: null,
    });
  });

  it("surfaces partial_note when the master's own figure is real but a follower was excluded", () => {
    const partial = row({
      pnl: {
        master_pnl: null, followers_pnl: null, total_pnl: null, realized_pnl: "50.00",
        unavailable_reason: null,
        partial_note: "Excludes vikash - unverifiable historical fills (missing price data or a suspected overnight carry)",
      },
    });
    expect(strategyPnlDisplay(partial)).toEqual({
      value: "50.00",
      reason: null,
      isRealizedOnly: true,
      partialNote: "Excludes vikash - unverifiable historical fills (missing price data or a suspected overnight carry)",
    });
  });

  it("shows the strategy's own fill rate, or 0% when unknown", () => {
    expect(strategyFillRateLabel(row({}))).toBe("100%");
    expect(strategyFillRateLabel(row({ execution: { ...row({}).execution, rate_percent: null } }))).toBe("0%");
  });
});

describe("buildStrategySeries", () => {
  function timeline(): DashboardRealizedTimeline {
    return {
      trading_day: "2026-09-11",
      as_of: "2026-09-11T09:00:00+00:00",
      range: range({}),
      strategies: {
        "strat-1": {
          name: "Intraday Alpha", status: "OK",
          points: [
            { time: "2026-09-11T09:41:10+05:30", cumulative_realized: "0.00" },
            { time: "2026-09-11T11:20:32+05:30", cumulative_realized: "-3.00" },
          ],
        },
        "strat-2": {
          name: "Scalping", status: "UNAVAILABLE", points: [],
        },
      },
    };
  }

  it("puts every strategy on one shared, IST-anchored time axis", () => {
    const { times } = buildStrategySeries(timeline());
    expect(times).toEqual([
      "2026-09-11T00:00:00+05:30",
      "2026-09-11T09:41:10+05:30",
      "2026-09-11T11:20:32+05:30",
    ]);
  });

  it("forward-fills a running strategy's series and leaves an unavailable one empty", () => {
    const { series } = buildStrategySeries(timeline());
    const alpha = series.find((s) => s.strategyId === "strat-1")!;
    expect(alpha.points.map((p) => p.value)).toEqual([0, 0, -3]);
    const scalping = series.find((s) => s.strategyId === "strat-2")!;
    expect(scalping.points).toEqual([]);
  });

  it("anchors the zero-start checkpoint at the SELECTED RANGE's own start day, not always today's", () => {
    // A 7d range starting five days before trading_day (always today) must
    // not insert a fake reset-to-zero at today's midnight, which would
    // otherwise land in the middle of the sorted times and corrupt the
    // cumulative curve for every earlier day.
    const sevenDay: DashboardRealizedTimeline = {
      trading_day: "2026-09-11", // today
      as_of: "2026-09-11T09:00:00+00:00",
      range: range({ key: "7d", start: "2026-09-06", end: "2026-09-11", is_today: false, includes_today: true }),
      strategies: {
        "strat-1": {
          name: "master", status: "OK",
          points: [
            { time: "2026-09-07T10:00:00+05:30", cumulative_realized: "50.00" }, // historical day
            { time: "2026-09-11T09:41:10+05:30", cumulative_realized: "47.00" }, // today, continuing
          ],
        },
      },
    };

    const { times, series } = buildStrategySeries(sevenDay);
    expect(times[0]).toBe("2026-09-06T00:00:00+05:30"); // the range's own start, not today's midnight
    expect(times).not.toContain("2026-09-11T00:00:00+05:30");

    const alpha = series[0];
    // Never dips back to 0 between the two real historical/live points.
    expect(alpha.points.map((p) => p.value)).toEqual([0, 50, 47]);
  });

  it("never fabricates a chart when there are no real strategies", () => {
    const empty: DashboardRealizedTimeline = {
      trading_day: "2026-09-11", as_of: "...", range: range({}), strategies: {},
    };
    const { series } = buildStrategySeries(empty);
    expect(series).toEqual([]);
  });
});

describe("buildDailyChartPoints (Group Strategy daily P&L chart)", () => {
  const day = (date: string, daily_pnl: string | null, status: "OK" | "PARTIAL" | "UNAVAILABLE" = "OK") => ({
    date, daily_pnl, status, reasons: [] as string[],
  });

  it("plots each day's own value, in order, never a running total", () => {
    const pts = buildDailyChartPoints([day("2026-09-23", "30.00"), day("2026-09-24", "-20.00")]);
    expect(pts).toEqual([
      { time: "2026-09-23", cumulative_realized: "30.00" },
      { time: "2026-09-24", cumulative_realized: "-20.00" },
    ]);
  });

  it("skips null (uncostable) days instead of drawing them as 0, and keeps PARTIAL days", () => {
    const pts = buildDailyChartPoints([
      day("2026-09-22", "50.00", "PARTIAL"), day("2026-09-25", null, "UNAVAILABLE"), day("2026-09-26", "10.00"),
    ]);
    expect(pts.map((p) => p.time)).toEqual(["2026-09-22", "2026-09-26"]);
  });

  it("repeats a lone day so the line can render, and returns [] for no usable day", () => {
    expect(buildDailyChartPoints([day("2026-09-24", "7.00")])).toHaveLength(2);
    expect(buildDailyChartPoints([day("2026-09-24", null, "UNAVAILABLE")])).toEqual([]);
    expect(buildDailyChartPoints(undefined)).toEqual([]);
  });

  it("does not change the legacy per-fill series used by the main dashboard", () => {
    const { series } = buildStrategySeries({
      trading_day: "2026-10-01", as_of: "x",
      strategies: { a: { name: "A", status: "OK", points: [{ time: "2026-10-01T04:00:00Z", cumulative_realized: "5.00" }], daily: [day("2026-10-01", "5.00")] } },
    });
    expect(series[0].points.at(-1)?.value).toBe(5);
  });
});


describe("dailyPartialNote (Group Strategy chart 'Partial' badge)", () => {
  const day = (date: string, daily_pnl: string | null, status: "OK" | "PARTIAL" | "UNAVAILABLE" = "OK") => ({
    date, daily_pnl, status, reasons: [] as string[],
  });

  it("is null when every day is clean, or there is no daily data", () => {
    expect(dailyPartialNote([day("2026-09-23", "30.00"), day("2026-09-24", "-2.00")])).toBeNull();
    expect(dailyPartialNote([])).toBeNull();
    expect(dailyPartialNote(undefined)).toBeNull();
  });

  it("counts plotted-but-partial days separately from days with no value", () => {
    const note = dailyPartialNote([
      day("2026-09-22", "50.00", "PARTIAL"), day("2026-09-23", "30.00"), day("2026-09-24", "5.00", "PARTIAL"),
      day("2026-09-25", null, "UNAVAILABLE"),
    ]);
    expect(note).toContain("2 plotted days");
    expect(note).toContain("1 day not shown");
  });

  it("uses singular wording for one day", () => {
    expect(dailyPartialNote([day("2026-09-22", "1.00", "PARTIAL")])).toContain("1 plotted day include");
  });
});

describe("sumMoney (exact, integer paise)", () => {
  it("adds 2-decimal strings without float error", () => {
    expect(sumMoney(["0.10", "0.20"])).toBe("0.30");
    expect(sumMoney(["1.52", "-3.00", "0.30"])).toBe("-1.18");
    expect(sumMoney(["1234.50", "0.50"])).toBe("1235.00");
  });
  it("handles negatives, zero and short fractions", () => {
    expect(sumMoney(["-0.30"])).toBe("-0.30");
    expect(sumMoney(["5", "-5"])).toBe("0.00");
    expect(sumMoney(["2.5", "0.05"])).toBe("2.55");
  });
  it("returns null for nothing to add or a non-money value", () => {
    expect(sumMoney([])).toBeNull();
    expect(sumMoney(["1.00", "abc"])).toBeNull();
  });
});

describe("describeDailyReason(s)", () => {
  it("explains NFO, open-position and missing-price codes in plain words", () => {
    expect(describeDailyReason("MASTER:UNSUPPORTED_INSTRUMENT:NFO:NIFTY")).toBe("NFO trades not included");
    expect(describeDailyReason("UNSUPPORTED_INSTRUMENT:BSE:X")).toBe("BSE trades not included");
    expect(describeDailyReason("FOLLOWER:ce85cdc0:OPEN_POSITION_AT_DAY_END:CANBK:-3")).toBe("Position still open at day end (CANBK)");
    expect(describeDailyReason("FOLLOWER_EXCLUDED:ce85cdc0:MISSING_FILL_PRICE:YESBANK")).toBe("Fill price missing (YESBANK)");
    expect(describeDailyReason("TODAY_LIVE_UNAVAILABLE")).toBe("Today's broker data could not be read");
  });
  it("shows an unrecognised code as-is instead of hiding it", () => {
    expect(describeDailyReason("SOMETHING_NEW:42")).toBe("SOMETHING_NEW:42");
  });
  it("merges the same reason coming from master and follower", () => {
    expect(describeDailyReasons([
      "MASTER:UNSUPPORTED_INSTRUMENT:NFO:NIFTY", "FOLLOWER:ce85cdc0:UNSUPPORTED_INSTRUMENT:NFO:NIFTY",
    ])).toEqual(["NFO trades not included"]);
  });
});

describe("buildPnlSnapshot (Group Strategy card)", () => {
  const day = (date: string, daily_pnl: string | null, status: "OK" | "PARTIAL" | "UNAVAILABLE" = "OK", reasons: string[] = []) => ({
    date, daily_pnl, status, reasons,
  });
  const TODAY = "2026-10-06";

  it("takes today's value from today's entry and the total from the last 7 days", () => {
    const daily = ["01", "02", "03", "04", "05", "06", "07", "08"].map((d, i) => day(`2026-09-${d}`, `${i + 1}.00`))
      .concat([day("2026-10-06", "10.00")]);
    const s = buildPnlSnapshot(daily, TODAY);
    expect(s.today).toEqual({ kind: "value", value: "10.00", partial: false });
    expect(s.bars).toHaveLength(7);
    expect(s.bars[0].date).toBe("2026-09-03");            // oldest of the last 7
    expect(s.bars[6].date).toBe("2026-10-06");
    expect(s.windowTotal).toBe("43.00");                  // 3+4+5+6+7+8+10 = last 7 only, not all 9
    expect(s.windowNote).toBeNull();
  });

  it("is 'none' (not a zero) when there is no entry for today", () => {
    expect(buildPnlSnapshot([day("2026-10-05", "1.00")], TODAY).today).toEqual({ kind: "none" });
  });

  it("reports today as unavailable, with plain reasons, when its value is null", () => {
    const s = buildPnlSnapshot([day(TODAY, null, "UNAVAILABLE", ["TODAY_LIVE_UNAVAILABLE"])], TODAY);
    expect(s.today).toEqual({ kind: "unavailable", reasons: ["Today's broker data could not be read"] });
    expect(s.windowTotal).toBeNull();
  });

  it("keeps partial days as bars with reasons and flags the window, without hiding them", () => {
    const s = buildPnlSnapshot([
      day("2026-10-01", "4.39"),
      day("2026-10-05", "1.50", "PARTIAL", ["MASTER:UNSUPPORTED_INSTRUMENT:NFO:NIFTY"]),
      day(TODAY, "-0.30"),
    ], TODAY);
    expect(s.bars.map((b) => b.value)).toEqual(["4.39", "1.50", "-0.30"]);
    expect(s.bars[1].status).toBe("PARTIAL");
    expect(s.bars[1].reasons).toEqual(["NFO trades not included"]);
    expect(s.windowTotal).toBe("5.59");
    expect(s.windowNote).toContain("1 plotted day");
  });

  it("keeps a no-value day as a null bar that is excluded from the total, never read as 0", () => {
    const s = buildPnlSnapshot([day("2026-10-01", "2.00"), day("2026-10-02", null, "UNAVAILABLE", ["UNSUPPORTED_INSTRUMENT:NFO:NIFTY"]), day(TODAY, "3.00")], TODAY);
    expect(s.bars.map((b) => b.value)).toEqual(["2.00", null, "3.00"]);
    expect(s.windowTotal).toBe("5.00");
    expect(s.windowNote).toContain("1 day not shown");
  });

  it("copes with no daily data at all", () => {
    const s = buildPnlSnapshot(undefined, TODAY);
    expect(s.bars).toEqual([]);
    expect(s.windowTotal).toBeNull();
    expect(s.today).toEqual({ kind: "none" });
  });
});

describe("buildCumulativeSeries (cumulative P&L line)", () => {
  const day = (date: string, daily_pnl: string | null, status: "OK" | "PARTIAL" | "UNAVAILABLE" = "OK", reasons: string[] = []) => ({
    date, daily_pnl, status, reasons,
  });
  const START = "2026-09-01";

  it("is a running total: day 1, day 1 + day 2, ...", () => {
    const s = buildCumulativeSeries([day("2026-09-02", "10.00"), day("2026-09-03", "5.50"), day("2026-09-04", "4.50")], START);
    expect(s.points.map((p) => p.cumulative)).toEqual(["10.00", "15.50", "20.00"]);
    expect(s.points.map((p) => p.daily)).toEqual(["10.00", "5.50", "4.50"]);
  });

  it("handles an all-loss run", () => {
    const s = buildCumulativeSeries([day("2026-09-02", "-3.00"), day("2026-09-03", "-1.25")], START);
    expect(s.points.map((p) => p.cumulative)).toEqual(["-3.00", "-4.25"]);
  });

  it("crosses from profit to loss and back, with exact money", () => {
    const s = buildCumulativeSeries([
      day("2026-09-02", "5.00"), day("2026-09-03", "-8.00"), day("2026-09-04", "4.10"), day("2026-09-05", "0.10"),
    ], START);
    expect(s.points.map((p) => p.cumulative)).toEqual(["5.00", "-3.00", "1.10", "1.20"]);
  });

  it("is exact where floats are not (0.1 + 0.2)", () => {
    const s = buildCumulativeSeries([day("2026-09-02", "0.10"), day("2026-09-03", "0.20")], START);
    expect(s.points[1].cumulative).toBe("0.30");
  });

  it("sorts unsorted input by date", () => {
    const s = buildCumulativeSeries([day("2026-09-04", "1.00"), day("2026-09-02", "2.00"), day("2026-09-03", "3.00")], START);
    expect(s.points.map((p) => p.date)).toEqual(["2026-09-02", "2026-09-03", "2026-09-04"]);
    expect(s.points.map((p) => p.cumulative)).toEqual(["2.00", "5.00", "6.00"]);
  });

  it("keeps a partial day's value in the total and flags it, with plain reasons", () => {
    const s = buildCumulativeSeries([
      day("2026-09-02", "2.00"),
      day("2026-09-03", "1.50", "PARTIAL", ["MASTER:UNSUPPORTED_INSTRUMENT:NFO:NIFTY"]),
      day("2026-09-04", "1.00"),
    ], START);
    expect(s.points.map((p) => p.cumulative)).toEqual(["2.00", "3.50", "4.50"]);
    expect(s.points[1].status).toBe("PARTIAL");
    expect(s.points[1].reasons).toEqual(["NFO trades not included"]);
    expect(s.points.map((p) => p.partialSoFar)).toEqual([0, 1, 1]);
    expect(s.partialDays).toBe(1);
  });

  it("never turns an unverifiable day into a point or a zero: it adds nothing and is counted", () => {
    const s = buildCumulativeSeries([
      day("2026-09-02", "4.00"),
      day("2026-09-03", null, "UNAVAILABLE", ["UNSUPPORTED_INSTRUMENT:NFO:NIFTY"]),
      day("2026-09-04", "1.00"),
    ], START);
    expect(s.points.map((p) => p.date)).toEqual(["2026-09-02", "2026-09-04"]);   // no point for Sep 3
    expect(s.points.map((p) => p.cumulative)).toEqual(["4.00", "5.00"]);
    expect(s.points[1].gapBefore).toBe(1);
    expect(s.points[1].missingBefore).toBe(1);
    expect(s.points[0].missingBefore).toBe(0);
    expect(s.missingDays).toBe(1);
  });

  it("only counts unverifiable days that come BEFORE a point against that point", () => {
    const s = buildCumulativeSeries([day("2026-09-02", "1.00"), day("2026-09-03", null, "UNAVAILABLE"), day("2026-09-04", null, "UNAVAILABLE")], START);
    expect(s.points).toHaveLength(1);
    expect(s.points[0].missingBefore).toBe(0);
    expect(s.missingDays).toBe(2);
  });

  it("starts at the strategy's creation day: earlier days are not included", () => {
    const s = buildCumulativeSeries([day("2026-08-30", "100.00"), day("2026-09-01", "2.00"), day("2026-09-02", "3.00")], START);
    expect(s.points.map((p) => p.cumulative)).toEqual(["2.00", "5.00"]);
  });

  it("has no points - and so no flat zero line - when there is no history", () => {
    expect(buildCumulativeSeries([], START).points).toEqual([]);
    expect(buildCumulativeSeries(undefined, START).points).toEqual([]);
    expect(buildCumulativeSeries([day("2026-08-01", "9.00")], START).points).toEqual([]);   // all before creation
    expect(buildCumulativeSeries([day("2026-09-02", null, "UNAVAILABLE")], START).points).toEqual([]);
  });

  it("plots only the days that exist (a few days -> a few points)", () => {
    expect(buildCumulativeSeries([day("2026-09-05", "1.00"), day("2026-09-20", "1.00")], START).points).toHaveLength(2);
  });
});

describe("layoutCumulativeLine", () => {
  const pt = (date: string, cumulative: string) => ({
    date, daily: "0.00", cumulative, status: "OK" as const, reasons: [], gapBefore: 0, missingBefore: 0, partialSoFar: 0,
  });

  it("puts profit above the zero line and loss below it (SVG y grows downward)", () => {
    const pts = [pt("2026-09-02", "5.00"), pt("2026-09-03", "-3.00"), pt("2026-09-04", "1.10")];
    const l = layoutCumulativeLine(pts, "2026-09-01");
    expect(l.xy[0].y).toBeLessThan(l.zeroY);
    expect(l.xy[1].y).toBeGreaterThan(l.zeroY);
    expect(l.xy[2].y).toBeLessThan(l.zeroY);
    expect(l.max).toBe(5);
    expect(l.min).toBe(-3);
  });

  it("places points left to right by date and the latest point on the right edge", () => {
    const pts = [pt("2026-09-02", "1.00"), pt("2026-09-10", "2.00"), pt("2026-09-30", "3.00")];
    const l = layoutCumulativeLine(pts, "2026-09-01", 1000, 100, 6);
    expect(l.xy[0].x).toBeLessThan(l.xy[1].x);
    expect(l.xy[1].x).toBeLessThan(l.xy[2].x);
    expect(l.xy[2].x).toBe(994);
  });

  it("keeps the line off the very top and bottom, and a single point is drawn at the right edge", () => {
    const l = layoutCumulativeLine([pt("2026-09-02", "5.00")], "2026-09-02", 1000, 100, 6, 8);
    expect(l.xy[0].x).toBe(994);
    expect(l.xy[0].y).toBeGreaterThanOrEqual(8);
    expect(l.zeroY).toBeLessThanOrEqual(92);
  });

  it("does not divide by zero when everything is 0", () => {
    const l = layoutCumulativeLine([pt("2026-09-02", "0.00"), pt("2026-09-03", "0.00")], "2026-09-01");
    expect(Number.isFinite(l.xy[0].y)).toBe(true);
    expect(Number.isFinite(l.zeroY)).toBe(true);
  });
});
describe("buildStrategyDailySeries (Strategy P&L, ranges other than Today)", () => {
  const strat = (name: string, daily: Array<[string, string | null, "OK" | "PARTIAL" | "UNAVAILABLE"]>, status: "OK" | "UNAVAILABLE" = "UNAVAILABLE") => ({
    name,
    status,
    // The cumulative curve is UNAVAILABLE whenever Today's live data is - the bug this guards.
    points: [],
    daily: daily.map(([date, daily_pnl, st]) => ({ date, daily_pnl, status: st, reasons: [] })),
  });
  const timeline = (strategies: Record<string, ReturnType<typeof strat>>) =>
    ({ trading_day: "2026-10-08", as_of: "", strategies }) as unknown as Parameters<typeof buildStrategyDailySeries>[0];

  it("plots each verified day even when the cumulative curve is UNAVAILABLE", () => {
    const r = buildStrategyDailySeries(
      timeline({ a: strat("master", [["2026-10-05", "-1.50", "OK"], ["2026-10-06", "2.00", "PARTIAL"], ["2026-10-07", null, "UNAVAILABLE"]]) }),
    );
    expect(r.days).toEqual(["2026-10-05", "2026-10-06", "2026-10-07"]);
    expect(r.series[0].hasValue).toBe(true);
    expect(r.series[0].points.map((p) => p.value)).toEqual([-1.5, 2, null]);
    expect(r.series[0].points[1].status).toBe("PARTIAL");
  });

  it("never turns an unverifiable or missing day into zero", () => {
    const r = buildStrategyDailySeries(
      timeline({
        a: strat("master", [["2026-10-05", "1.00", "OK"], ["2026-10-06", null, "UNAVAILABLE"]]),
        b: strat("other", [["2026-10-06", "3.00", "OK"]]),
      }),
    );
    expect(r.days).toEqual(["2026-10-05", "2026-10-06"]);
    const [a, b] = r.series;
    expect(a.points.map((p) => p.value)).toEqual([1, null]);
    expect(b.points.map((p) => p.value)).toEqual([null, 3]);
    expect(b.points[0].money).toBeNull();
  });

  it("reports a strategy with no verifiable day, or no daily data, as having no value", () => {
    const r = buildStrategyDailySeries(
      timeline({
        a: strat("allbad", [["2026-10-05", null, "UNAVAILABLE"]]),
        b: { name: "legacy", status: "OK", points: [] } as unknown as ReturnType<typeof strat>,
      }),
    );
    expect(r.series.map((s) => s.hasValue)).toEqual([false, false]);
  });

  it("returns nothing for an empty or missing timeline", () => {
    expect(buildStrategyDailySeries(null)).toEqual({ days: [], series: [] });
    expect(buildStrategyDailySeries(timeline({})).days).toEqual([]);
  });
});
