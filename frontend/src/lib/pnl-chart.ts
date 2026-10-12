/**
 * Data + geometry for the Group Strategy card's rotating P&L chart.
 *
 * One line chart, alternating between two views of the same data (the same
 * component with different data):
 *   "daily"      every trading day's own P&L since the strategy was created
 *   "cumulative" the running total of those days
 * ("7d", the latest 7 days' own P&L, can still be built here but is not part
 * of the rotation: the card shows the 7-day P&L as bars instead.)
 *
 * Everything is derived from the engine's per-day P&L (`daily[]`) that the
 * page already fetched - nothing is invented. A day with no verifiable value
 * has a null value: it is a gap in the line, never a zero. A partial day
 * keeps its value and its status. Money is only ever added in integer paise
 * (via sumMoney); the floats below are display geometry.
 */
import type { PnlStatus } from "./account-metrics";
import {
  buildCumulativeSeries,
  describeDailyReasons,
  type StrategyDailyPnl,
} from "./dashboard";

export type PnlChartMode = "7d" | "daily" | "cumulative";

/** The views the card's line chart rotates through, in order. */
export const PNL_CHART_MODES: readonly PnlChartMode[] = ["daily", "cumulative"];

export const PNL_CHART_LABELS: Record<PnlChartMode, string> = {
  "7d": "7-Day P&L",
  daily: "Daily P&L",
  cumulative: "Cumulative P&L",
};

export interface PnlChartPoint {
  date: string;
  /** The plotted value (engine money string). null = no verifiable value: a gap. */
  value: string | null;
  /** That day's own P&L; null when it could not be verified. */
  daily: string | null;
  /** Running total up to and including this day (cumulative mode only). */
  cumulative: string | null;
  status: PnlStatus;
  /** Plain-language reasons the day is partial / has no value. */
  reasons: string[];
  /** Cumulative mode: unverifiable days between the previous point and this one. */
  gapBefore: number;
  /** Cumulative mode: unverifiable days anywhere before this point. */
  missingBefore: number;
  /** Cumulative mode: partial days up to and including this point. */
  partialSoFar: number;
}

export interface PnlChartSeries {
  mode: PnlChartMode;
  label: string;
  points: PnlChartPoint[];
  /** Cumulative mode joins across an unverifiable day with a dashed segment;
   * the other modes break the line there instead. */
  connectAcrossGaps: boolean;
  /** Plotted days that are partial. */
  partialDays: number;
  /** Days with no verifiable value (null points, or skipped in cumulative mode). */
  missingDays: number;
}

function byDate(a: { date: string }, b: { date: string }): number {
  return a.date < b.date ? -1 : a.date > b.date ? 1 : 0;
}

function dayPoint(d: StrategyDailyPnl): PnlChartPoint {
  const value = d.daily_pnl ?? null;
  return {
    date: d.date,
    value,
    daily: value,
    cumulative: null,
    status: d.status,
    reasons: describeDailyReasons(d.reasons ?? []),
    gapBefore: 0,
    missingBefore: 0,
    partialSoFar: 0,
  };
}

/**
 * The series for one mode. Only days on/after `startIso` (the strategy's
 * creation day, IST) are used. `windowSize` is how many days "7d" shows.
 */
export function buildPnlChartSeries(
  daily: StrategyDailyPnl[] | undefined,
  startIso: string,
  mode: PnlChartMode,
  windowSize = 7,
): PnlChartSeries {
  const label = PNL_CHART_LABELS[mode];

  if (mode === "cumulative") {
    const cum = buildCumulativeSeries(daily, startIso);
    return {
      mode,
      label,
      connectAcrossGaps: true,
      partialDays: cum.partialDays,
      missingDays: cum.missingDays,
      points: cum.points.map((p) => ({
        date: p.date,
        value: p.cumulative,
        daily: p.daily,
        cumulative: p.cumulative,
        status: p.status,
        reasons: p.reasons,
        gapBefore: p.gapBefore,
        missingBefore: p.missingBefore,
        partialSoFar: p.partialSoFar,
      })),
    };
  }

  const inRange = [...(daily ?? [])].filter((d) => d.date >= startIso).sort(byDate);
  const days = mode === "7d" ? inRange.slice(-windowSize) : inRange;
  const points = days.map(dayPoint);
  return {
    mode,
    label,
    connectAcrossGaps: false,
    points,
    partialDays: points.filter((p) => p.value !== null && p.status !== "OK").length,
    missingDays: points.filter((p) => p.value === null).length,
  };
}

/** Subtle tooltip text for the chart's info marker; null when the data is complete. */
export function pnlChartNote(series: PnlChartSeries): string | null {
  const plural = (n: number) => (n === 1 ? "" : "s");
  if (series.mode === "cumulative") {
    const bits: string[] = [];
    if (series.partialDays > 0) {
      bits.push(`includes ${series.partialDays} partial day${plural(series.partialDays)} (some trades, e.g. NFO, are not counted)`);
    }
    if (series.missingDays > 0) {
      bits.push(`leaves out ${series.missingDays} day${plural(series.missingDays)} with no verifiable P&L`);
    }
    return bits.length > 0 ? `This total ${bits.join(" and ")}, so it may differ from your real P&L.` : null;
  }
  const bits: string[] = [];
  if (series.partialDays > 0) {
    bits.push(`${series.partialDays} plotted day${plural(series.partialDays)} include trades that could not be fully costed (NFO, or positions still open)`);
  }
  if (series.missingDays > 0) {
    bits.push(`${series.missingDays} day${plural(series.missingDays)} ${series.missingDays === 1 ? "has" : "have"} no verifiable P&L and ${series.missingDays === 1 ? "is" : "are"} not drawn`);
  }
  return bits.length > 0 ? `${bits.join("; ")}. Figures may differ from your real P&L.` : null;
}

// ── geometry ────────────────────────────────────────────────────────────────

/** Same drawing box as the original Group Strategy chart. */
export const CHART_VB_W = 380;
export const CHART_VB_H = 120;
// 14 (the original chart used 10): the box is stretched to the card width, so
// at the narrowest card (~200px) 10 units is only ~5px - not enough to keep the
// latest-point ring at the right edge from being clipped.
export const CHART_PAD_X = 14;
export const CHART_PAD_TOP = 10;
export const CHART_PAD_BOT = 10;
/** How many points are visible at once. */
export const CHART_WINDOW = 14;
/** How many earlier points the viewport starts behind, before panning to the latest. */
export const CHART_PAN_POINTS = 6;

/**
 * Which slice of an n-point series is visible, and how far the viewport pans
 * during the entrance. The viewport always ENDS on the latest points (latest
 * at the right edge) and starts up to `panPoints` points earlier, so older
 * data moves out to the left as the latest enters from the right. A series
 * that fits entirely in the window does not pan.
 */
export function chartViewport(n: number, windowSize = CHART_WINDOW, panPoints = CHART_PAN_POINTS) {
  const visible = Math.min(Math.max(n, 0), windowSize);
  const endOffset = Math.max(0, n - visible);
  const startOffset = Math.max(0, endOffset - panPoints);
  return { visible, startOffset, endOffset };
}

export interface PnlChartLayout {
  n: number;
  visible: number;
  /** x distance between consecutive points. */
  step: number;
  /** Width of the whole series in view-box units (wider than CHART_VB_W when it pans). */
  totalW: number;
  startOffset: number;
  endOffset: number;
  /** Translation, as a percentage of totalW, at the start / end of the pan. */
  startPct: number;
  endPct: number;
  xs: number[];
  /** null where the point has no value. */
  ys: (number | null)[];
  zeroY: number;
  hasValues: boolean;
  /** Every plotted value is exactly 0: draw one neutral line. */
  allZero: boolean;
}

/** Positions for every point of a series. Display geometry only. */
export function layoutPnlChart(series: PnlChartSeries): PnlChartLayout {
  const n = series.points.length;
  const { visible, startOffset, endOffset } = chartViewport(n);
  const innerW = CHART_VB_W - 2 * CHART_PAD_X;
  const step = visible > 1 ? innerW / (visible - 1) : 0;
  const totalW = n > 1 ? 2 * CHART_PAD_X + (n - 1) * step : CHART_VB_W;
  const xs = series.points.map((_, i) => (n > 1 ? CHART_PAD_X + i * step : CHART_VB_W / 2));

  const values = series.points.map((p) => (p.value === null ? null : Number(p.value)));
  const real = values.filter((v): v is number => v !== null);
  const hasValues = real.length > 0;
  const allZero = hasValues && real.every((v) => v === 0);
  const maxV = Math.max(0, ...real);
  const minV = Math.min(0, ...real);
  const spread = allZero ? 20 : maxV - minV || 1;
  const pad = spread * 0.12;
  const dispMin = allZero ? -10 : minV - pad;
  const dispMax = allZero ? 10 : maxV + pad;
  const plotH = CHART_VB_H - CHART_PAD_TOP - CHART_PAD_BOT;
  const yOf = (v: number) => CHART_PAD_TOP + (1 - (v - dispMin) / (dispMax - dispMin)) * plotH;

  return {
    n,
    visible,
    step,
    totalW,
    startOffset,
    endOffset,
    startPct: (startOffset * step * 100) / totalW,
    endPct: (endOffset * step * 100) / totalW,
    xs,
    ys: values.map((v) => (v === null ? null : yOf(v))),
    zeroY: yOf(0),
    hasValues,
    allZero,
  };
}

const f = (n: number) => n.toFixed(2);

/**
 * A smooth curve through the points (a monotone cubic - Fritsch-Carlson):
 * it looks like the original smoothed chart but never overshoots, so between
 * two real values it cannot dip above or below either of them, and a smooth
 * line can never show a profit or loss that no data point has.
 */
export function smoothPath(pts: { x: number; y: number }[]): string {
  const n = pts.length;
  if (n === 0) return "";
  if (n === 1) return `M${f(pts[0].x)} ${f(pts[0].y)}`;
  if (n === 2) return `M${f(pts[0].x)} ${f(pts[0].y)} L${f(pts[1].x)} ${f(pts[1].y)}`;

  const dx: number[] = [];
  const m: number[] = [];
  for (let i = 0; i < n - 1; i++) {
    dx.push(pts[i + 1].x - pts[i].x);
    m.push((pts[i + 1].y - pts[i].y) / dx[i]);
  }
  const t: number[] = new Array(n);
  t[0] = m[0];
  t[n - 1] = m[n - 2];
  for (let i = 1; i < n - 1; i++) t[i] = m[i - 1] * m[i] <= 0 ? 0 : (m[i - 1] + m[i]) / 2;
  for (let i = 0; i < n - 1; i++) {
    if (m[i] === 0) {
      t[i] = 0;
      t[i + 1] = 0;
    } else {
      const a = t[i] / m[i];
      const b = t[i + 1] / m[i];
      const s = a * a + b * b;
      if (s > 9) {
        const tau = 3 / Math.sqrt(s);
        t[i] = tau * a * m[i];
        t[i + 1] = tau * b * m[i];
      }
    }
  }

  let d = `M${f(pts[0].x)} ${f(pts[0].y)}`;
  for (let i = 0; i < n - 1; i++) {
    const h = dx[i] / 3;
    d += ` C${f(pts[i].x + h)} ${f(pts[i].y + t[i] * h)}, ${f(pts[i + 1].x - h)} ${f(pts[i + 1].y - t[i + 1] * h)}, ${f(pts[i + 1].x)} ${f(pts[i + 1].y)}`;
  }
  return d;
}

export interface PnlChartRuns {
  /** Index runs drawn as solid, smooth lines. */
  solid: number[][];
  /** Straight dashed joins across unverifiable days (cumulative mode only). */
  dashed: [number, number][];
}

/** Splits a series into the solid runs and dashed joins that get drawn. */
export function pnlChartRuns(series: PnlChartSeries): PnlChartRuns {
  const solid: number[][] = [];
  const dashed: [number, number][] = [];
  let run: number[] = [];
  series.points.forEach((p, i) => {
    if (p.value === null) {
      if (run.length) solid.push(run);
      run = [];
      return;
    }
    if (series.connectAcrossGaps && p.gapBefore > 0 && run.length) {
      dashed.push([run[run.length - 1], i]);
      solid.push(run);
      run = [];
    }
    run.push(i);
  });
  if (run.length) solid.push(run);
  return { solid, dashed };
}

/** The point to mark as "latest": the last one that has a value. */
export function latestValueIndex(series: PnlChartSeries): number {
  for (let i = series.points.length - 1; i >= 0; i--) if (series.points[i].value !== null) return i;
  return -1;
}

/** Nearest point to a pointer, given the pointer's x in view-box units of the visible window. */
export function nearestPointIndex(layout: PnlChartLayout, viewX: number): number {
  if (layout.n <= 1 || layout.step === 0) return 0;
  const global = viewX + layout.endOffset * layout.step;
  const i = Math.round((global - CHART_PAD_X) / layout.step);
  return Math.min(layout.n - 1, Math.max(0, i));
}

/** The mode after `current` in the rotation. */
export function nextChartIndex(current: number, count = PNL_CHART_MODES.length): number {
  return (current + 1) % count;
}

export interface TooltipPlacementInput {
  /** The hovered thing, in the same coordinates as `bounds` (top/bottom may be equal for a point). */
  anchor: { x: number; top: number; bottom: number };
  tip: { w: number; h: number };
  /** The area the tooltip must stay inside (the card, limited to the viewport). */
  bounds: { left: number; top: number; right: number; bottom: number };
  gap?: number;
}

export interface TooltipPlacement {
  left: number;
  top: number;
  placement: "above" | "below" | "right" | "left";
}

/**
 * Collision-aware tooltip position (display only). Near the left edge the
 * tooltip goes to the right of the point, near the right edge to its left;
 * otherwise it is centred on the point, above it if there is room and below
 * it if not. If neither fits it tries beside the point, and only as a last
 * resort is clamped inside the bounds. It never leaves `bounds`.
 */
export function placeTooltip({ anchor, tip, bounds, gap = 6 }: TooltipPlacementInput): TooltipPlacement {
  const { w, h } = tip;
  const clampX = (x: number) => Math.max(bounds.left, Math.min(x, bounds.right - w));
  const clampY = (y: number) => Math.max(bounds.top, Math.min(y, bounds.bottom - h));
  const centerY = (anchor.top + anchor.bottom) / 2;
  const centered = anchor.x - w / 2;
  const nearEdge = centered < bounds.left || centered + w > bounds.right;

  const side = (): TooltipPlacement | null => {
    const preferRight = centered < bounds.left || !(centered + w > bounds.right);
    const options: Array<[number, "right" | "left"]> = preferRight
      ? [[anchor.x + gap, "right"], [anchor.x - gap - w, "left"]]
      : [[anchor.x - gap - w, "left"], [anchor.x + gap, "right"]];
    for (const [left, placement] of options) {
      if (left >= bounds.left && left + w <= bounds.right) return { left, top: clampY(centerY - h / 2), placement };
    }
    return null;
  };

  if (nearEdge) {
    const s = side();
    if (s) return s;
  }
  const spaceAbove = anchor.top - gap - bounds.top;
  const spaceBelow = bounds.bottom - (anchor.bottom + gap) ;
  if (spaceAbove >= h) return { left: clampX(centered), top: anchor.top - gap - h, placement: "above" };
  if (spaceBelow >= h) return { left: clampX(centered), top: anchor.bottom + gap, placement: "below" };
  const s = side();
  if (s) return s;
  return spaceAbove >= spaceBelow
    ? { left: clampX(centered), top: clampY(anchor.top - gap - h), placement: "above" }
    : { left: clampX(centered), top: clampY(anchor.bottom + gap), placement: "below" };
}
