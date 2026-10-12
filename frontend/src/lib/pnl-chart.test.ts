import { describe, expect, it } from "vitest";
import {
  CHART_PAD_X,
  CHART_VB_W,
  PNL_CHART_MODES,
  buildPnlChartSeries,
  chartViewport,
  latestValueIndex,
  layoutPnlChart,
  nearestPointIndex,
  nextChartIndex,
  placeTooltip,
  pnlChartNote,
  pnlChartRuns,
  smoothPath,
} from "./pnl-chart";

const day = (date: string, daily_pnl: string | null, status: "OK" | "PARTIAL" | "UNAVAILABLE" = "OK", reasons: string[] = []) => ({
  date, daily_pnl, status, reasons,
});
const START = "2026-09-01";

describe("buildPnlChartSeries - 7-day mode", () => {
  const days = Array.from({ length: 10 }, (_, i) => day(`2026-09-${String(i + 2).padStart(2, "0")}`, `${i + 1}.00`));

  it("is the latest 7 days' own P&L, oldest first", () => {
    const s = buildPnlChartSeries(days, START, "7d");
    expect(s.points.map((p) => p.date)).toEqual(["2026-09-05", "2026-09-06", "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11"]);
    expect(s.points.map((p) => p.value)).toEqual(["4.00", "5.00", "6.00", "7.00", "8.00", "9.00", "10.00"]);
    expect(s.connectAcrossGaps).toBe(false);
  });

  it("respects the creation-date cutoff before taking the last 7", () => {
    const s = buildPnlChartSeries(days, "2026-09-10", "7d");
    expect(s.points.map((p) => p.date)).toEqual(["2026-09-10", "2026-09-11"]);
  });

  it("sorts unsorted input", () => {
    const s = buildPnlChartSeries([day("2026-09-04", "1.00"), day("2026-09-02", "2.00")], START, "7d");
    expect(s.points.map((p) => p.date)).toEqual(["2026-09-02", "2026-09-04"]);
  });
});

describe("buildPnlChartSeries - daily mode", () => {
  it("has one point per day since creation with that day's own value (not a running total)", () => {
    const s = buildPnlChartSeries([day("2026-09-02", "100.00"), day("2026-09-03", "0.00"), day("2026-09-04", "-50.00")], START, "daily");
    expect(s.points.map((p) => p.value)).toEqual(["100.00", "0.00", "-50.00"]);
    expect(s.points.every((p) => p.cumulative === null)).toBe(true);
  });

  it("keeps a verified zero day as a real 0.00 point", () => {
    const s = buildPnlChartSeries([day("2026-09-02", "0.00")], START, "daily");
    expect(s.points[0].value).toBe("0.00");
  });

  it("does not turn an unverifiable day into zero: it stays a null point", () => {
    const s = buildPnlChartSeries([day("2026-09-02", "5.00"), day("2026-09-03", null, "UNAVAILABLE", ["UNSUPPORTED_INSTRUMENT:NFO:NIFTY"]), day("2026-09-04", "1.00")], START, "daily");
    expect(s.points.map((p) => p.value)).toEqual(["5.00", null, "1.00"]);
    expect(s.points[1].daily).toBeNull();
    expect(s.points[1].reasons).toEqual(["NFO trades not included"]);
    expect(s.missingDays).toBe(1);
  });

  it("keeps partial days partial, with their plain-language reasons", () => {
    const s = buildPnlChartSeries([day("2026-09-02", "1.50", "PARTIAL", ["MASTER:UNSUPPORTED_INSTRUMENT:NFO:NIFTY"])], START, "daily");
    expect(s.points[0].status).toBe("PARTIAL");
    expect(s.points[0].value).toBe("1.50");
    expect(s.partialDays).toBe(1);
  });

  it("excludes days before the strategy existed", () => {
    const s = buildPnlChartSeries([day("2026-08-20", "99.00"), day("2026-09-02", "1.00")], START, "daily");
    expect(s.points.map((p) => p.date)).toEqual(["2026-09-02"]);
  });

  it("has no points - and so no fake flat line - when there is no history", () => {
    expect(buildPnlChartSeries([], START, "daily").points).toEqual([]);
    expect(buildPnlChartSeries(undefined, START, "daily").points).toEqual([]);
    expect(buildPnlChartSeries([day("2026-08-01", "9.00")], START, "daily").points).toEqual([]);
  });
});

describe("buildPnlChartSeries - cumulative mode", () => {
  it("is the exact running total: day 1, day 1 + day 2, ...", () => {
    const s = buildPnlChartSeries([day("2026-09-02", "0.10"), day("2026-09-03", "0.20"), day("2026-09-04", "-1.00")], START, "cumulative");
    expect(s.points.map((p) => p.value)).toEqual(["0.10", "0.30", "-0.70"]);
    expect(s.points.map((p) => p.daily)).toEqual(["0.10", "0.20", "-1.00"]);
    expect(s.connectAcrossGaps).toBe(true);
  });

  it("adds nothing for an unverifiable day, has no point for it, and counts it", () => {
    const s = buildPnlChartSeries([day("2026-09-02", "4.00"), day("2026-09-03", null, "UNAVAILABLE"), day("2026-09-04", "1.00")], START, "cumulative");
    expect(s.points.map((p) => p.date)).toEqual(["2026-09-02", "2026-09-04"]);
    expect(s.points.map((p) => p.value)).toEqual(["4.00", "5.00"]);
    expect(s.points[1].gapBefore).toBe(1);
    expect(s.missingDays).toBe(1);
  });

  it("includes a partial day's value and keeps it flagged", () => {
    const s = buildPnlChartSeries([day("2026-09-02", "2.00"), day("2026-09-03", "1.50", "PARTIAL", ["UNSUPPORTED_INSTRUMENT:NFO:NIFTY"])], START, "cumulative");
    expect(s.points.map((p) => p.value)).toEqual(["2.00", "3.50"]);
    expect(s.points[1].status).toBe("PARTIAL");
    expect(s.partialDays).toBe(1);
  });

  it("uses only values that came from the input (no invented numbers)", () => {
    const input = [day("2026-09-02", "1.00"), day("2026-09-03", "2.00")];
    const s = buildPnlChartSeries(input, START, "cumulative");
    expect(s.points).toHaveLength(2);
    expect(s.points[1].value).toBe("3.00");
  });
});

describe("pnlChartNote", () => {
  it("is null when the data is complete", () => {
    for (const mode of ["7d", ...PNL_CHART_MODES] as const) {
      expect(pnlChartNote(buildPnlChartSeries([day("2026-09-02", "1.00")], START, mode))).toBeNull();
    }
  });

  it("warns, in plain words, when the cumulative total includes partial days", () => {
    const s = buildPnlChartSeries([day("2026-09-02", "1.00", "PARTIAL", ["UNSUPPORTED_INSTRUMENT:NFO:NIFTY"])], START, "cumulative");
    expect(pnlChartNote(s)).toContain("includes 1 partial day");
    expect(pnlChartNote(s)).toContain("may differ from your real P&L");
  });

  it("mentions days that are not drawn", () => {
    const s = buildPnlChartSeries([day("2026-09-02", null, "UNAVAILABLE"), day("2026-09-03", "1.00")], START, "daily");
    expect(pnlChartNote(s)).toContain("1 day has no verifiable P&L");
  });
});

describe("rotation order", () => {
  it("alternates daily <-> cumulative, forever, and never includes the 7-day view", () => {
    expect(PNL_CHART_MODES).toEqual(["daily", "cumulative"]);
    expect(PNL_CHART_MODES).not.toContain("7d");
    let i = 0;
    const seen: string[] = [];
    for (let k = 0; k < 6; k++) { seen.push(PNL_CHART_MODES[i]); i = nextChartIndex(i); }
    expect(seen).toEqual(["daily", "cumulative", "daily", "cumulative", "daily", "cumulative"]);
  });
});

describe("chartViewport (leftward movement over existing points)", () => {
  it("does not pan a series that fits in the window", () => {
    expect(chartViewport(5)).toEqual({ visible: 5, startOffset: 0, endOffset: 0 });
    expect(chartViewport(14)).toEqual({ visible: 14, startOffset: 0, endOffset: 0 });
  });

  it("ends on the latest points and starts behind them, so older data leaves to the left", () => {
    expect(chartViewport(18)).toEqual({ visible: 14, startOffset: 0, endOffset: 4 });
    expect(chartViewport(40)).toEqual({ visible: 14, startOffset: 20, endOffset: 26 });
  });

  it("never starts past the end or before the first point", () => {
    for (const n of [0, 1, 2, 14, 15, 20, 100]) {
      const v = chartViewport(n);
      expect(v.startOffset).toBeGreaterThanOrEqual(0);
      expect(v.startOffset).toBeLessThanOrEqual(v.endOffset);
      expect(v.endOffset + v.visible).toBeLessThanOrEqual(Math.max(n, 0));
    }
  });
});

describe("layoutPnlChart", () => {
  it("puts profit above the zero line and loss below it (SVG y grows downward)", () => {
    const l = layoutPnlChart(buildPnlChartSeries([day("2026-09-02", "5.00"), day("2026-09-03", "-3.00"), day("2026-09-04", "1.00")], START, "daily"));
    expect(l.ys[0]!).toBeLessThan(l.zeroY);
    expect(l.ys[1]!).toBeGreaterThan(l.zeroY);
    expect(l.ys[2]!).toBeLessThan(l.zeroY);
  });

  it("leaves a null day's y as null (never zero) while the others keep their positions", () => {
    const l = layoutPnlChart(buildPnlChartSeries([day("2026-09-02", "5.00"), day("2026-09-03", null, "UNAVAILABLE"), day("2026-09-04", "1.00")], START, "daily"));
    expect(l.ys[1]).toBeNull();
    expect(l.ys[0]).not.toBeNull();
    expect(l.xs[0]).toBeLessThan(l.xs[1]);
    expect(l.xs[1]).toBeLessThan(l.xs[2]);
  });

  it("spans the full width with the latest point at the right edge, no empty left side", () => {
    const l = layoutPnlChart(buildPnlChartSeries([day("2026-09-02", "1.00"), day("2026-09-20", "2.00"), day("2026-09-30", "3.00")], START, "daily"));
    expect(l.xs[0]).toBe(CHART_PAD_X);
    expect(l.xs[2]).toBe(CHART_VB_W - CHART_PAD_X);
  });

  it("is wider than the view box only when it has to pan, and pans to the latest", () => {
    const many = Array.from({ length: 20 }, (_, i) => day(`2026-09-${String(i + 2).padStart(2, "0")}`, "1.00"));
    const l = layoutPnlChart(buildPnlChartSeries(many, START, "daily"));
    expect(l.totalW).toBeGreaterThan(CHART_VB_W);
    expect(l.endPct).toBeGreaterThan(0);
    expect(l.startPct).toBeLessThanOrEqual(l.endPct);
    // after the pan, the last point sits at the right edge of the viewport
    const lastX = l.xs[l.n - 1] - (l.endPct * l.totalW) / 100;
    expect(lastX).toBeCloseTo(CHART_VB_W - CHART_PAD_X, 5);
    const short = layoutPnlChart(buildPnlChartSeries(many.slice(0, 5), START, "daily"));
    expect(short.totalW).toBe(CHART_VB_W);
    expect(short.endPct).toBe(0);
  });

  it("flags an all-zero series (one neutral line) and survives a single point and no data", () => {
    expect(layoutPnlChart(buildPnlChartSeries([day("2026-09-02", "0.00"), day("2026-09-03", "0.00")], START, "daily")).allZero).toBe(true);
    const one = layoutPnlChart(buildPnlChartSeries([day("2026-09-02", "2.00")], START, "daily"));
    expect(Number.isFinite(one.xs[0])).toBe(true);
    const none = layoutPnlChart(buildPnlChartSeries([], START, "daily"));
    expect(none.hasValues).toBe(false);
    expect(Number.isFinite(none.zeroY)).toBe(true);
  });
});

describe("smoothPath", () => {
  const ys = (d: string) => (d.match(/-?\d+\.\d+/g) ?? []).map(Number).filter((_, i) => i % 2 === 1);

  it("never overshoots: every control point stays inside the data's own range", () => {
    const pts = [{ x: 0, y: 50 }, { x: 10, y: 10 }, { x: 20, y: 12 }, { x: 30, y: 90 }, { x: 40, y: 88 }];
    const all = ys(smoothPath(pts));
    expect(Math.min(...all)).toBeGreaterThanOrEqual(10 - 1e-6);
    expect(Math.max(...all)).toBeLessThanOrEqual(90 + 1e-6);
  });

  it("is a straight line between two points and a bare move for one", () => {
    expect(smoothPath([{ x: 0, y: 0 }, { x: 10, y: 5 }])).toBe("M0.00 0.00 L10.00 5.00");
    expect(smoothPath([{ x: 3, y: 4 }])).toBe("M3.00 4.00");
    expect(smoothPath([])).toBe("");
  });
});

describe("pnlChartRuns / latestValueIndex / nearestPointIndex", () => {
  it("breaks the daily line at an unverifiable day (no joining across it)", () => {
    const s = buildPnlChartSeries([day("2026-09-02", "1.00"), day("2026-09-03", "2.00"), day("2026-09-04", null, "UNAVAILABLE"), day("2026-09-05", "3.00")], START, "daily");
    expect(pnlChartRuns(s)).toEqual({ solid: [[0, 1], [3]], dashed: [] });
  });

  it("joins the cumulative line across an unverifiable day with a dashed segment", () => {
    const s = buildPnlChartSeries([day("2026-09-02", "1.00"), day("2026-09-03", null, "UNAVAILABLE"), day("2026-09-04", "3.00")], START, "cumulative");
    expect(pnlChartRuns(s)).toEqual({ solid: [[0], [1]], dashed: [[0, 1]] });
  });

  it("marks the last point that has a value as the latest", () => {
    const s = buildPnlChartSeries([day("2026-09-02", "1.00"), day("2026-09-03", null, "UNAVAILABLE")], START, "daily");
    expect(latestValueIndex(s)).toBe(0);
    expect(latestValueIndex(buildPnlChartSeries([], START, "daily"))).toBe(-1);
  });

  it("finds the nearest point from a pointer position in the visible window", () => {
    const many = Array.from({ length: 20 }, (_, i) => day(`2026-09-${String(i + 2).padStart(2, "0")}`, "1.00"));
    const l = layoutPnlChart(buildPnlChartSeries(many, START, "daily"));
    expect(nearestPointIndex(l, CHART_VB_W - CHART_PAD_X)).toBe(19);   // right edge = latest
    expect(nearestPointIndex(l, CHART_PAD_X)).toBe(l.endOffset);        // left edge = first visible
    expect(nearestPointIndex(l, -10_000)).toBe(0);                     // clamps at the first point
    expect(nearestPointIndex(l, 10_000)).toBe(19);                     // and at the last
    expect(nearestPointIndex(layoutPnlChart(buildPnlChartSeries([day("2026-09-02", "1.00")], START, "daily")), 100)).toBe(0);
  });
});

describe("placeTooltip", () => {
  const bounds = { left: 0, top: 0, right: 300, bottom: 300 };
  const tip = { w: 140, h: 60 };
  const at = (x: number, y: number) => ({ x, top: y - 5, bottom: y + 5 });
  const inside = (p: { left: number; top: number }) =>
    p.left >= bounds.left && p.left + tip.w <= bounds.right && p.top >= bounds.top && p.top + tip.h <= bounds.bottom;
  const covers = (p: { left: number; top: number }, a: { x: number; top: number; bottom: number }) =>
    a.x >= p.left && a.x <= p.left + tip.w && (a.top + a.bottom) / 2 >= p.top && (a.top + a.bottom) / 2 <= p.top + tip.h;

  it("centres above a middle point", () => {
    const a = at(150, 200);
    const p = placeTooltip({ anchor: a, tip, bounds });
    expect(p.placement).toBe("above");
    expect(p.left).toBe(80);
    expect(inside(p) && !covers(p, a)).toBe(true);
  });

  it("goes to the right of a point near the left edge", () => {
    const a = at(10, 200);
    const p = placeTooltip({ anchor: a, tip, bounds });
    expect(p.placement).toBe("right");
    expect(p.left).toBeGreaterThan(10);
    expect(inside(p) && !covers(p, a)).toBe(true);
  });

  it("goes to the left of a point near the right edge", () => {
    const a = at(290, 200);
    const p = placeTooltip({ anchor: a, tip, bounds });
    expect(p.placement).toBe("left");
    expect(p.left + tip.w).toBeLessThan(290);
    expect(inside(p) && !covers(p, a)).toBe(true);
  });

  it("goes below when there is no room above", () => {
    const a = at(150, 30);
    const p = placeTooltip({ anchor: a, tip, bounds });
    expect(p.placement).toBe("below");
    expect(inside(p) && !covers(p, a)).toBe(true);
  });

  it("stays inside the bounds for every point on a grid, and never covers the point when it can avoid it", () => {
    for (let x = 0; x <= 300; x += 15) {
      for (let y = 0; y <= 300; y += 15) {
        const a = at(x, y);
        const p = placeTooltip({ anchor: a, tip, bounds });
        expect(inside(p)).toBe(true);
        expect(covers(p, a)).toBe(false);
      }
    }
  });

  it("falls back to a clamped position when the tooltip is bigger than the bounds", () => {
    const p = placeTooltip({ anchor: at(50, 50), tip: { w: 400, h: 400 }, bounds });
    expect(p.left).toBeLessThanOrEqual(bounds.left);
  });
});
