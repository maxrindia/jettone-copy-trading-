/**
 * Types and pure helpers for GET /dashboard/summary and
 * GET /dashboard/realized-timeline (backend/app/api/dashboard.py).
 *
 * Every P&L figure here is a value the backend already computed with
 * Decimal from each account's own broker fills (app/services/pnl). This
 * file never sums, subtracts or otherwise recomputes a money value itself -
 * "Combined Gross P&L" is `combined_total` as sent, not something added up
 * in the browser.
 *
 * DATE RANGE: the backend's `range.is_today` flag says whether the selected
 * range is actually today - P&L-derived fields (pnl, strategies[].pnl,
 * real_trading_impact.*_pnl) are ALWAYS today's numbers regardless of what
 * range was requested (PnlService has no historical capability - see
 * dashboard.py's module docstring), so this file's helpers make the caller
 * check `is_today` before presenting those fields as answering the
 * selected range, rather than silently mislabeling today's P&L as
 * "Yesterday's P&L".
 */
import type { PnlBrokerCheck, PnlStatus } from "./account-metrics";
import { formatInr } from "./money";

export type DateRangeKey = "today" | "yesterday" | "7d" | "30d" | "custom";

export interface DateRangeInfo {
  key: DateRangeKey;
  start: string; // "2026-09-11"
  end: string;
  is_today: boolean;
  /** Whether the range's end reaches Today - true for "today" and always
   * for "7d"/"30d", false for "yesterday" or a fully past "custom" range.
   * Unrealized P&L is only ever meaningful when this is true (see
   * backend/app/services/pnl/historical.py). */
  includes_today: boolean;
}

export const DATE_RANGE_OPTIONS: { key: DateRangeKey; label: string }[] = [
  { key: "today", label: "Today" },
  { key: "yesterday", label: "Yesterday" },
  { key: "7d", label: "Last 7 Days" },
  { key: "30d", label: "Last 30 Days" },
  { key: "custom", label: "Custom Date Range" },
];

export interface DashboardMaster {
  account_id: string;
  display_name: string;
  alice_user_id: string;
  session_valid: boolean;
  is_live: boolean;
}

export interface DashboardStatus {
  live_trading_enabled: boolean;
  master_trading_enabled: boolean;
  kill_switch_active: boolean;
  master: DashboardMaster | null;
  children: { total: number; connected: number };
}

/** Row 1 - platform state. NEVER affected by the date range. */
export interface DashboardPlatform {
  demat_accounts: { total: number; connected: number };
  static_ip: null; // served by the existing GET /system/static-ip, fetched separately
  strategies: { total: number; active: number };
}

export interface DashboardAccountPnl {
  account_id: string;
  display_name: string;
  account_type: "MASTER" | "FOLLOWER";
  is_active: boolean;
  session_valid: boolean;
  realized_pnl: string | null;
  unrealized_pnl: string | null;
  total_pnl: string | null;
  pnl_basis: "GROSS";
  pnl_status: PnlStatus;
  pnl_notes: string[];
  open_positions_count: number | null;
  broker_check: PnlBrokerCheck;
  trading_day: string;
  as_of: string;
}

export interface DashboardPnl {
  combined_realized: string | null;
  combined_unrealized: string | null;
  combined_total: string | null;
  pnl_basis: "GROSS";
  accounts_total: number;
  accounts_unavailable: number;
  accounts: DashboardAccountPnl[];
  /** Why combined_realized is null, when it is. For Today this mirrors the
   * live broker-unavailable reason; for any other range it explains a
   * historical-data gap (see backend/app/services/pnl/historical.py). */
  realized_unavailable_reason: string | null;
  /** Why combined_unrealized (and therefore combined_total) is null, when it
   * is. Always set for a range that ends before Today - no historical
   * end-of-day position or price data exists anywhere in this system. */
  unrealized_unavailable_reason: string | null;
}

export interface CopyFillStats {
  filled_quantity: number;
  target_quantity: number;
  rate_percent: number | null;
  filled_orders: number;
  working_orders: number;
  rejected_orders: number;
  considered_orders: number;
}

export interface ExecutionEdgeItem {
  symbol: string;
  side: "BUY" | "SELL";
  time: string;
  /** Shares actually filled on this copy - the weight applied to edge_per_share. */
  quantity: number;
  master_price: string;
  child_price: string;
  /** Unweighted per-share price difference (BUY: master - child, SELL: child - master). */
  edge_per_share: string;
  /** edge_per_share * quantity - the real ₹ impact of this one fill. */
  impact: string;
  child_account_id: string;
  group_strategy_id: string | null;
}

export interface ExecutionEdge {
  /** Quantity-weighted total impact in ₹ - sum of every item's `impact`. This is the KPI value. */
  total: string | null;
  /** total / total_quantity - impact-weighted average, not a plain average of per-fill edges. */
  average_per_share: string | null;
  total_quantity: number;
  sample_count: number;
  direction: "children_better" | "children_worse" | "even" | null;
  items: ExecutionEdgeItem[];
}

export interface DashboardIssue {
  id: string;
  kind: "AUDIT" | "SYSTEM_EVENT";
  type: string;
  severity: "INFO" | "WARNING" | "ERROR" | "CRITICAL";
  message: string;
  account_id: string | null;
  created_at: string;
}

/** Master/follower P&L for one strategy. master_pnl/followers_pnl/total_pnl
 * (all gross totals) are only ever populated for Today - unrealized P&L has
 * no historical data source (see backend/app/services/pnl/historical.py).
 * realized_pnl is populated for any range: Today's own live figure, or
 * Jettone's own verified historical realized (optionally continued by
 * Today's live realized when the range extends through Today). */
export interface StrategyPnl {
  master_pnl: string | null;
  followers_pnl: string | null;
  total_pnl: string | null;
  realized_pnl: string | null;
  unavailable_reason: string | null;
  /** Set when realized_pnl is real but incomplete: the master's own figure
   * is verified and included, but one or more followers were excluded
   * because their own historical fills couldn't be verified (never guessed
   * as zero). Names exactly which accounts were left out and why. */
  partial_note: string | null;
}

export interface StrategyExecutionStats {
  considered_orders: number;
  filled_orders: number;
  working_orders: number;
  rejected_orders: number;
  target_quantity: number;
  filled_quantity: number;
  rate_percent: number | null;
  execution_impact: string | null;
  average_per_share: string | null;
}

export interface StrategyFollower {
  account_id: string;
  display_name: string | null;
  multiplier: string;
  trading_enabled: boolean;
}

export interface StrategyPerformanceRow {
  id: string;
  name: string;
  status: "RUNNING" | "DISABLED" | "NOT_RUNNING";
  status_reason: string | null;
  master: { account_id: string | null; display_name: string | null };
  followers: StrategyFollower[];
  follower_count: number;
  pnl: StrategyPnl;
  execution: StrategyExecutionStats;
}

export interface StrategiesPayload {
  list: StrategyPerformanceRow[];
  live_master_id: string | null;
  ambiguous: boolean;
}

export interface OrdersByStrategyRow {
  strategy_id: string | null;
  strategy_name: string;
  count: number;
}

export interface RealTradingImpact {
  strategy_id: string;
  strategy_name: string;
  master_pnl: string | null;
  followers_pnl: string | null;
  pnl_unavailable_reason: string | null;
  copy_execution_impact: string | null;
  copied_orders: number;
  filled_copies: number;
  failed_missed_copies: number;
  working_copies: number;
}

export interface DashboardSummary {
  trading_day: string;
  as_of: string;
  range: DateRangeInfo;
  platform: DashboardPlatform;
  status: DashboardStatus;
  pnl: DashboardPnl;
  open_positions: { total: number | null; accounts_total: number };
  copy_fill: CopyFillStats;
  execution_edge: ExecutionEdge;
  issues: { count: number; items: DashboardIssue[] };
  strategies: StrategiesPayload;
  orders_by_strategy: OrdersByStrategyRow[];
  real_trading_impact: RealTradingImpact | null;
}

export interface StrategyRealizedPoint {
  time: string;
  cumulative_realized: string;
}

/** One trading day's OWN P&L (not a running total). `daily_pnl` is null when
 * that day could not be costed; `status`/`reasons` say why. */
export interface StrategyDailyPnl {
  date: string; // IST trading day, YYYY-MM-DD
  daily_pnl: string | null;
  status: PnlStatus;
  reasons: string[];
}

export interface StrategyRealizedTimeline {
  name: string;
  status: PnlStatus;
  points: StrategyRealizedPoint[];
  /** Additive: per-day P&L for the Group Strategy chart. Absent on older engines. */
  daily?: StrategyDailyPnl[];
  /** Set only when `daily` has zero usable (non-null) days. */
  unavailable_reason?: string | null;
}

/**
 * Group Strategy chart input from per-day P&L: one point per usable trading
 * day, shaped like StrategyRealizedPoint so the existing chart consumes it
 * unchanged. The `cumulative_realized` slot carries that day's own
 * `daily_pnl` here - the chart plots daily values, never a running total.
 * Days with a null value are skipped, never drawn as 0. A lone day is
 * repeated so the (>= 2 point) line renders without an invented 09:15 anchor.
 */
export function buildDailyChartPoints(daily: StrategyDailyPnl[] | undefined): StrategyRealizedPoint[] {
  const usable = (daily ?? [])
    .filter((d) => d.daily_pnl !== null && d.daily_pnl !== undefined)
    .map((d) => ({ time: d.date, cumulative_realized: d.daily_pnl as string }));
  return usable.length === 1 ? [usable[0], usable[0]] : usable;
}

/**
 * Tooltip text for the chart's "Partial" badge when some days are plotted but
 * not fully costed (e.g. an NFO fill with no contract identity, or a position
 * still open at day end), or have no value at all. null = every day is clean.
 * Built only from the engine's own per-day status - nothing is inferred.
 */
export function dailyPartialNote(daily: StrategyDailyPnl[] | undefined): string | null {
  if (!daily) return null;
  const hidden = daily.filter((d) => d.daily_pnl === null).length;
  const partial = daily.filter((d) => d.daily_pnl !== null && d.status !== "OK").length;
  const plural = (n: number) => (n === 1 ? "" : "s");
  const parts: string[] = [];
  if (partial > 0) {
    parts.push(`${partial} plotted day${plural(partial)} include trades that could not be fully costed (NFO, or positions still open)`);
  }
  if (hidden > 0) {
    parts.push(`${hidden} day${plural(hidden)} not shown - no verifiable P&L`);
  }
  return parts.length > 0 ? parts.join("; ") : null;
}

// ── Group Strategy card: compact P&L snapshot (headline + mini daily bars) ───
//
// Everything here is derived from the engine's own per-day P&L (`daily[]`).
// Nothing is invented: a day with no verifiable value stays null, and a
// partial day keeps its value but carries its plain-language reasons.

const MONEY_STRING = /^(-)?(\d+)(?:\.(\d{1,2}))?$/;

/**
 * Exact sum of the engine's 2-decimal money strings, in integer paise - no
 * float ever touches an amount (same rule as lib/money.ts). Returns null for
 * an empty list or if any value is not a plain money string.
 */
export function sumMoney(values: string[]): string | null {
  if (values.length === 0) return null;
  let paise = 0;
  for (const v of values) {
    const m = MONEY_STRING.exec(v.trim());
    if (!m) return null;
    const amount = Number(m[2]) * 100 + Number((m[3] ?? "").padEnd(2, "0"));
    paise += m[1] ? -amount : amount;
  }
  const abs = Math.abs(paise);
  return `${paise < 0 ? "-" : ""}${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, "0")}`;
}

/**
 * Plain-language text for one engine reason code, e.g.
 * "MASTER:UNSUPPORTED_INSTRUMENT:NFO:NIFTY" -> "NFO trades not included".
 * Which account the code came from is dropped (the card is strategy-level);
 * an unrecognised code is shown as-is rather than hidden.
 */
export function describeDailyReason(code: string): string {
  const body = code.replace(/^(?:MASTER|FOLLOWER_EXCLUDED|FOLLOWER):(?:[0-9a-f]{8}:)?/i, "");
  const [kind, a] = body.split(":");
  switch (kind) {
    case "UNSUPPORTED_INSTRUMENT":
      return `${a || "Unsupported"} trades not included`;
    case "OPEN_POSITION_AT_DAY_END":
      return `Position still open at day end${a ? ` (${a})` : ""}`;
    case "MISSING_FILL_PRICE":
      return `Fill price missing${a ? ` (${a})` : ""}`;
    case "TODAY_LIVE_UNAVAILABLE":
      return "Today's broker data could not be read";
    default:
      return body;
  }
}

/** De-duplicated plain-language reasons (master and follower often share one). */
export function describeDailyReasons(codes: string[]): string[] {
  return Array.from(new Set(codes.map(describeDailyReason)));
}

export interface PnlSnapshotBar {
  date: string;
  /** Engine money string; null = no verifiable value for that day (never 0). */
  value: string | null;
  status: PnlStatus;
  /** Plain-language reasons the day is partial / unavailable. */
  reasons: string[];
}

export type PnlSnapshotToday =
  | { kind: "value"; value: string; partial: boolean }
  | { kind: "unavailable"; reasons: string[] }
  | { kind: "none" }; // no trades today - not the same as a verified zero

export interface PnlSnapshot {
  today: PnlSnapshotToday;
  /** The most recent trading days with activity, oldest first. */
  bars: PnlSnapshotBar[];
  /** Exact sum of the bars that have a value; null when none do. */
  windowTotal: string | null;
  /** Tooltip text when some bars are partial or have no value; else null. */
  windowNote: string | null;
}

/**
 * Card snapshot from the engine's per-day P&L. `todayIso` is the IST trading
 * day (YYYY-MM-DD). The window is the last `windowSize` days that have an
 * entry - days with no trades have none, so it can span more than 7 calendar
 * days. Today's figure is today's realized P&L (the live broker path).
 */
export function buildPnlSnapshot(
  daily: StrategyDailyPnl[] | undefined,
  todayIso: string,
  windowSize = 7,
): PnlSnapshot {
  const sorted = [...(daily ?? [])].sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
  const window = sorted.slice(-windowSize);
  const bars: PnlSnapshotBar[] = window.map((d) => ({
    date: d.date,
    value: d.daily_pnl ?? null,
    status: d.status,
    reasons: describeDailyReasons(d.reasons ?? []),
  }));

  const todayEntry = sorted.find((d) => d.date === todayIso);
  let today: PnlSnapshotToday;
  if (!todayEntry) {
    today = { kind: "none" };
  } else if (todayEntry.daily_pnl === null || todayEntry.daily_pnl === undefined) {
    today = { kind: "unavailable", reasons: describeDailyReasons(todayEntry.reasons ?? []) };
  } else {
    today = { kind: "value", value: todayEntry.daily_pnl, partial: todayEntry.status !== "OK" };
  }

  return {
    today,
    bars,
    windowTotal: sumMoney(bars.flatMap((b) => (b.value === null ? [] : [b.value]))),
    windowNote: dailyPartialNote(window),
  };
}

// ── Group Strategy card: cumulative P&L line (creation date -> latest day) ───
//
// A running total of the engine's verified per-day P&L. A day with no
// verifiable value is NOT a point and adds nothing to the total - it is
// counted and surfaced (dashed segment, tooltip note), never silently read
// as zero. A partial day keeps its value and is flagged.

export interface CumulativePoint {
  date: string;
  /** That day's own P&L (engine money string). */
  daily: string;
  /** Exact running total up to and including this day. */
  cumulative: string;
  status: PnlStatus;
  /** Plain-language reasons the day is partial. */
  reasons: string[];
  /** Days with no verifiable P&L between the previous point and this one. */
  gapBefore: number;
  /** Days with no verifiable P&L anywhere before this point (since startIso). */
  missingBefore: number;
  /** Partial days up to and including this point. */
  partialSoFar: number;
}

export interface CumulativeSeries {
  points: CumulativePoint[];
  missingDays: number;
  partialDays: number;
}

/**
 * Running total over the days on/after `startIso` (the strategy's creation
 * day, IST). Money is summed in integer paise via sumMoney - never floats.
 * Returns no points when there is nothing verifiable in range.
 */
export function buildCumulativeSeries(daily: StrategyDailyPnl[] | undefined, startIso: string): CumulativeSeries {
  const sorted = [...(daily ?? [])]
    .filter((d) => d.date >= startIso)
    .sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));

  const points: CumulativePoint[] = [];
  let running = "0.00";
  let missing = 0;
  let partial = 0;
  let gap = 0;
  for (const d of sorted) {
    if (d.daily_pnl === null || d.daily_pnl === undefined) {
      missing += 1;
      gap += 1;
      continue;
    }
    const next = sumMoney([running, d.daily_pnl]);
    if (next === null) {
      // Not a plain money string: treat like an unverifiable day, never as 0.
      missing += 1;
      gap += 1;
      continue;
    }
    running = next;
    if (d.status !== "OK") partial += 1;
    points.push({
      date: d.date,
      daily: d.daily_pnl,
      cumulative: running,
      status: d.status,
      reasons: describeDailyReasons(d.reasons ?? []),
      gapBefore: points.length > 0 ? gap : 0,
      // Days are processed in order, so `missing` is exactly the unverifiable
      // days before this point.
      missingBefore: missing,
      partialSoFar: partial,
    });
    gap = 0;
  }
  return { points, missingDays: missing, partialDays: partial };
}

function daysBetweenIso(a: string, b: string): number {
  return Math.round((Date.UTC(+b.slice(0, 4), +b.slice(5, 7) - 1, +b.slice(8, 10)) -
    Date.UTC(+a.slice(0, 4), +a.slice(5, 7) - 1, +a.slice(8, 10))) / 86_400_000);
}

export interface CumulativeLayout {
  /** Positions in a W x H box, y growing downward (SVG). One per point. */
  xy: { x: number; y: number }[];
  /** y of the 0 line. */
  zeroY: number;
  /** Axis extremes in rupees (display only). */
  max: number;
  min: number;
}

/**
 * Where each point sits. X is calendar time from `startIso` (creation day) to
 * the latest point, so a long quiet stretch looks long; the latest point is
 * always at the right edge. Y always includes 0, so profit sits above the
 * zero line and loss below it. Display geometry only - floats are fine here.
 */
export function layoutCumulativeLine(
  points: CumulativePoint[],
  startIso: string,
  width = 1000,
  height = 100,
  padX = 6,
  padY = 8,
): CumulativeLayout {
  const values = points.map((p) => Number(p.cumulative));
  const max = Math.max(0, ...values);
  const min = Math.min(0, ...values);
  const range = max - min || 1;
  const yOf = (v: number) => padY + (1 - (v - min) / range) * (height - 2 * padY);

  const last = points.length > 0 ? points[points.length - 1].date : startIso;
  const span = Math.max(0, daysBetweenIso(startIso, last));
  const xOf = (date: string) =>
    span === 0 ? width - padX : padX + (Math.max(0, daysBetweenIso(startIso, date)) / span) * (width - 2 * padX);

  return {
    xy: points.map((p, i) => ({ x: xOf(p.date), y: yOf(values[i]) })),
    zeroY: yOf(0),
    max,
    min,
  };
}

export interface DashboardRealizedTimeline {
  trading_day: string;
  as_of: string;
  range?: DateRangeInfo;
  strategies: Record<string, StrategyRealizedTimeline>;
}

// ── IST time formatting (display-only; never changes a stored timestamp or
// a P&L figure) ────────────────────────────────────────────────────────────
//
// The engine's trading day (app/core/trading_day.py) is defined in this same
// zone, and every dashboard clock must read that same wall-clock time
// regardless of the viewer's own browser/OS timezone - otherwise a chart or
// label can silently show a non-IST time while still printing "IST" next to
// it. Real fill timestamps from the backend already carry a correct offset
// (e.g. "+05:30" or "Z") and so parse to the right absolute instant on their
// own; formatIstTime still forces the *display* zone. The one value that
// does NOT carry an offset is a plain date string ("2026-09-11") itself -
// istDayStart gives it one explicitly, rather than letting `new Date(...)`
// interpret a bare "T00:00:00" as browser-local midnight.

export const TRADING_TIMEZONE = "Asia/Kolkata";

/** A "2026-09-11" date string as an explicit IST midnight instant. */
export function istDayStart(day?: string): string {
  if (!day) {
    const todayStr = new Date().toISOString().split("T")[0];
    return `${todayStr}T00:00:00+05:30`;
  }
  const datePart = day.split("T")[0];
  return `${datePart}T00:00:00+05:30`;
}

/** Formats an ISO timestamp as IST time-of-day, regardless of the viewer's own timezone. */
export function formatIstTime(iso?: string, opts?: Intl.DateTimeFormatOptions): string {
  if (!iso) return "--:--";
  try {
    return new Date(iso).toLocaleTimeString("en-IN", {
      timeZone: TRADING_TIMEZONE,
      hour: "2-digit",
      minute: "2-digit",
      ...opts,
    });
  } catch {
    return "--:--";
  }
}

/** Formats a date string ("2026-09-11") as an IST calendar date label. */
export function formatIstDate(day?: string, opts?: Intl.DateTimeFormatOptions): string {
  if (!day) return "";
  try {
    return new Date(istDayStart(day)).toLocaleDateString("en-IN", {
      timeZone: TRADING_TIMEZONE,
      ...opts,
    });
  } catch {
    return day || "";
  }
}

// ── date range helpers (no React) ────────────────────────────────────────

/** Label for the range chip/summary near the dashboard heading. */
export function rangeSummaryLabel(range?: DateRangeInfo | null): string {
  if (!range) return "Today";
  const option = DATE_RANGE_OPTIONS.find((o) => o.key === range.key);
  if (range.key === "custom" || !option) {
    if (!range.start || !range.end) return "Custom";
    return range.start === range.end
      ? formatIstDate(range.start, { day: "numeric", month: "short", year: "numeric" })
      : `${formatIstDate(range.start, { day: "numeric", month: "short" })} – ${formatIstDate(range.end, { day: "numeric", month: "short", year: "numeric" })}`;
  }
  return option.label;
}

/** Caption under Realized P&L for the selected range: what period the
 * number covers, so it's never mistaken for "only Today" once historical
 * ranges are supported. */
export function realizedPnlRangeCaption(range?: DateRangeInfo | null): string {
  if (!range || range.is_today) return "Closed positions · Today";
  return `Closed positions · ${rangeSummaryLabel(range)}`;
}

/** Caption under Unrealized P&L: an open position's unrealized P&L is a
 * point-in-time fact as of right now - only meaningful when the selected
 * range's end is Today (Today itself, or any multi-day range, which always
 * extends through Today). A range that ends before Today (Yesterday, a
 * fully past custom range) has no historical end-of-day position/price
 * data, so this never applies to it. */
export function unrealizedPnlRangeCaption(range?: DateRangeInfo | null): string {
  if (!range || range.is_today) return "Open positions · Mark-to-market";
  return "Open positions · As of now";
}

// ── pure display helpers (no React) ──────────────────────────────────────

export function fillRateLabel(fill: CopyFillStats): string {
  return fill.rate_percent === null ? "0%" : `${fill.rate_percent}%`;
}

export function fillRateCaption(fill: CopyFillStats): string {
  if (fill.target_quantity === 0) return "No copies in range";
  const parts = [`${fill.filled_quantity} / ${fill.target_quantity} filled`];
  if (fill.working_orders > 0) parts.push(`${fill.working_orders} working`);
  if (fill.rejected_orders > 0) parts.push(`${fill.rejected_orders} rejected/cancelled`);
  return parts.join(" · ");
}

export function edgeDirectionLabel(edge: ExecutionEdge): string {
  switch (edge.direction) {
    case "children_better":
      return "Followers executed better";
    case "children_worse":
      return "Followers executed worse";
    case "even":
      return "Even with the master";
    default:
      return "No copied fills in range";
  }
}

/** Caption under the Execution Impact KPI: direction plus the
 * impact-weighted average, e.g. "Followers executed better · avg +₹0.10/share". */
export function edgeCaption(edge: ExecutionEdge): string {
  const direction = edgeDirectionLabel(edge);
  if (edge.average_per_share === null) return direction;
  return `${direction} · avg ${formatInr(edge.average_per_share)}/share`;
}

/** Full breakdown for one copied fill's tooltip - master price, child
 * price, filled quantity, total ₹ impact, and the per-share edge. */
export function edgeItemTooltip(item: ExecutionEdgeItem, strategyName?: string): string {
  const lines = [
    "Execution quality · not P&L",
    `${item.symbol} ${item.side} × ${item.quantity}`,
    `Master ${formatInr(item.master_price)} · Child ${formatInr(item.child_price)}`,
    `Impact ${formatInr(item.impact)} (${formatInr(item.edge_per_share)}/share)`,
  ];
  if (strategyName) lines.splice(1, 0, `Strategy: ${strategyName}`);
  return lines.join("\n");
}

const SEVERITY_TONE: Record<DashboardIssue["severity"], string> = {
  INFO: "text-[#6B7280] bg-[#F3F4F6] border-[#E5E7EB]",
  WARNING: "text-amber-800 bg-amber-50 border-amber-200",
  ERROR: "text-red-700 bg-red-50 border-red-200",
  CRITICAL: "text-red-700 bg-red-50 border-red-200",
};

export function issueSeverityClass(severity: DashboardIssue["severity"]): string {
  return SEVERITY_TONE[severity] ?? SEVERITY_TONE.INFO;
}

/** Total Gross P&L needs BOTH realized and unrealized - unavailable
 * whenever either is, for whichever reason the backend gave (see
 * DashboardPnl.realized_unavailable_reason / unrealized_unavailable_reason). */
export function combinedPnlUnavailableReason(pnl: DashboardPnl): string | null {
  if (pnl.combined_total !== null) return null;
  return pnl.realized_unavailable_reason ?? pnl.unrealized_unavailable_reason ?? "Unavailable";
}

/** Realized P&L's own reason, independent of Unrealized - Realized is
 * available for any range once it can be verified, so it must never share
 * Total's (frequently unrealized-caused) unavailable message. */
export function realizedPnlUnavailableReason(pnl: DashboardPnl): string | null {
  if (pnl.combined_realized !== null) return null;
  return pnl.realized_unavailable_reason ?? "Unavailable";
}

export function unrealizedPnlUnavailableReason(pnl: DashboardPnl): string | null {
  if (pnl.combined_unrealized !== null) return null;
  return pnl.unrealized_unavailable_reason ?? "Unavailable";
}

export function accountStatusDotClass(account: DashboardAccountPnl): string {
  return account.is_active && account.session_valid ? "bg-green-500" : "bg-gray-300";
}

export function accountStatusLabel(account: DashboardAccountPnl): string {
  if (!account.is_active) return "Inactive";
  return account.session_valid ? "Connected" : "Not authenticated";
}

export function brokerCheckLabel(account: DashboardAccountPnl): { label: string; ok: boolean } | null {
  const status = account.broker_check.status;
  if (status === "MATCH" || status === "WITHIN_TOLERANCE") return { label: "Broker matched", ok: true };
  if (status === "MISMATCH") return { label: "Broker mismatch", ok: false };
  return null; // NOT_COMPARABLE / UNAVAILABLE: nothing meaningful to show yet
}

// ── strategy performance helpers ─────────────────────────────────────────

export function strategyStatusLabel(row: StrategyPerformanceRow): string {
  if (row.status === "RUNNING") return "Running";
  if (row.status === "DISABLED") return "Disabled";
  return row.status_reason ?? "Not running";
}

/** The strategy's own combined P&L, or an honest reason it isn't available
 * (range history, or a broker-data gap already flagged on the account). */
/** The strategy's P&L for the Strategy Performance table: the gross total
 * (realized+unrealized) when available - only ever Today - otherwise falls
 * back to the combined REALIZED figure for the selected range (Jettone's
 * own verified historical orders, optionally continued by Today's own live
 * realized), flagged via isRealizedOnly so the UI can caption it instead of
 * silently presenting a realized-only number as the full gross total. */
export function strategyPnlDisplay(
  row: StrategyPerformanceRow,
): { value: string | null; reason: string | null; isRealizedOnly: boolean; partialNote: string | null } {
  if (row.pnl.total_pnl !== null) {
    return { value: row.pnl.total_pnl, reason: null, isRealizedOnly: false, partialNote: null };
  }
  if (row.pnl.realized_pnl !== null) {
    return { value: row.pnl.realized_pnl, reason: null, isRealizedOnly: true, partialNote: row.pnl.partial_note };
  }
  return { value: null, reason: row.pnl.unavailable_reason ?? "Unavailable", isRealizedOnly: false, partialNote: null };
}

export function strategyFillRateLabel(row: StrategyPerformanceRow): string {
  return row.execution.rate_percent === null ? "0%" : `${row.execution.rate_percent}%`;
}

// ── strategy P&L chart (Chart 1) ─────────────────────────────────────────

/** `value` is a plain number for SVG y-positioning ONLY - the legend/tooltip
 * text must still read the original backend money string via formatInr. */
export interface ChartSeriesPoint {
  time: string;
  value: number;
}

export interface StrategyChartSeries {
  strategyId: string;
  name: string;
  status: PnlStatus;
  points: ChartSeriesPoint[];
}

export function buildStrategySeries(
  timeline?: DashboardRealizedTimeline | null,
): { times: string[]; series: StrategyChartSeries[] } {
  if (!timeline || !timeline.strategies) {
    return { times: [], series: [] };
  }
  // The synthetic "starts at 0" anchor must sit at the SELECTED RANGE's own
  // start day, not always today's - a multi-day range (7d/30d) or a fully
  // past one (Yesterday) would otherwise get a fake reset-to-zero at
  // today's midnight, corrupting the cumulative curve for every day before
  // it (see StrategyPnlChart.tsx's module docstring).
  const startDay = timeline.range?.start || timeline.trading_day || new Date().toISOString().split("T")[0];
  const dayStart = istDayStart(startDay);
  const timeSet = new Set<string>([dayStart]);
  for (const strat of Object.values(timeline.strategies)) {
    if (strat && Array.isArray(strat.points)) {
      for (const point of strat.points) {
        if (point?.time) timeSet.add(point.time);
      }
    }
  }
  const times = Array.from(timeSet).sort();

  const series = Object.entries(timeline.strategies).map(([strategyId, strat]) => {
    if (!strat || strat.status === "UNAVAILABLE") {
      return { strategyId, name: strat?.name ?? "Strategy", status: (strat?.status ?? "UNAVAILABLE") as PnlStatus, points: [] };
    }
    // Forward-fill: a step chart holds its last known value between fills,
    // starting at 0 at the synthetic day-start checkpoint.
    const pointsList = Array.isArray(strat.points) ? strat.points : [];
    const byTime = new Map(pointsList.map((p) => [p.time, Number(p.cumulative_realized)]));
    let last = 0;
    const points: ChartSeriesPoint[] = times.map((t) => {
      if (byTime.has(t)) last = byTime.get(t) as number;
      return { time: t, value: last };
    });
    return { strategyId, name: strat.name, status: strat.status, points };
  });

  return { times, series };
}

// ── strategy P&L chart, multi-day ranges: one point per trading day ──────────

/** One strategy's P&L for one trading day. `value` is null when the day has
 * no verifiable P&L - a gap in the line, NEVER zero. */
export interface DailyChartPoint {
  date: string;
  value: number | null;
  /** Backend money string for display; null when `value` is null. */
  money: string | null;
  status: PnlStatus;
}

export interface StrategyDailyChartSeries {
  strategyId: string;
  name: string;
  points: DailyChartPoint[];
  /** True when at least one day has a verifiable value. */
  hasValue: boolean;
}

/**
 * Per-day Strategy P&L for any range that is not Today. This reads the
 * backend's per-day `daily[]` (each day costed on its own), not the
 * cumulative `points` curve: that curve is UNAVAILABLE as a whole whenever
 * Today's live data is (every 7d/30d range includes Today), which used to
 * blank the chart even though the earlier days were fully verified.
 *
 * Days are the union of every strategy's trading days, oldest first. A
 * strategy with no record for a day gets null there - unknown, not zero.
 */
export function buildStrategyDailySeries(
  timeline?: DashboardRealizedTimeline | null,
): { days: string[]; series: StrategyDailyChartSeries[] } {
  if (!timeline || !timeline.strategies) return { days: [], series: [] };
  const entries = Object.entries(timeline.strategies);
  const days = Array.from(
    new Set(entries.flatMap(([, s]) => (Array.isArray(s?.daily) ? s.daily.map((d) => d.date) : []))),
  ).sort();

  const series = entries.map(([strategyId, strat]) => {
    const byDay = new Map((Array.isArray(strat?.daily) ? strat.daily : []).map((d) => [d.date, d]));
    const points: DailyChartPoint[] = days.map((date) => {
      const d = byDay.get(date);
      const usable = d != null && d.daily_pnl !== null && d.daily_pnl !== undefined;
      return {
        date,
        value: usable ? Number(d.daily_pnl) : null,
        money: usable ? d.daily_pnl : null,
        status: d ? d.status : "UNAVAILABLE",
      };
    });
    return { strategyId, name: strat?.name ?? "Strategy", points, hasValue: points.some((p) => p.value !== null) };
  });
  return { days, series };
}
