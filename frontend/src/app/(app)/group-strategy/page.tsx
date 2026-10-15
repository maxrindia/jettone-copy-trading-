"use client";

import { useState, useEffect, useId, useMemo, FormEvent } from "react";
import { useRouter } from "next/navigation";
import { createPortal } from "react-dom";
import {
  Plus,
  Search,
  RotateCw,
  Eye,
  X,
  Play,
  TrendingUp,
  TrendingDown,
  Trash2,
  Users,
  Activity,
  Layers,
  CheckCircle2,
  ReceiptText,
  CalendarDays,
} from "lucide-react";
import { useEngineData, extractError } from "@/lib/use-engine-data";
import { EmptyState, ErrorPanel, LoadingPanel } from "@/components/ui/DataStates";
import Button from "@/components/ui/Button";
import StrategyPnlSnapshot, { StrategyPnlSkeleton } from "@/components/group-strategy/StrategyPnlSnapshot";
import {
  formatIstTime,
  type DashboardSummary,
  type DashboardRealizedTimeline,
  type StrategyRealizedPoint,
} from "@/lib/dashboard";
import { formatInr, moneyToneClass } from "@/lib/money";
import Tooltip from "@/components/ui/Tooltip";

interface Account {
  id: string;
  display_name: string;
  alice_user_id: string;
  account_type?: string;
  broker?: string;
  is_active: boolean;
  notes?: string;
}

interface MemberAccountInfo {
  id: string;
  display_name: string;
  alice_user_id: string;
  account_type?: string;
  broker?: string;
  is_active: boolean;
  session_valid?: boolean;
}

interface StrategyMemberItem {
  id: string;
  strategy_id: string;
  account_id: string;
  multiplier: number;
  trading_enabled: boolean;
  created_at: string;
  account: MemberAccountInfo;
}

interface BackendStrategyResponse {
  id: string;
  name: string;
  description?: string;
  visibility: "PRIVATE" | "PUBLIC";
  master_account_id?: string | null;
  trading_enabled: boolean;
  place_rejected_enabled: boolean;
  allocated_amount: number;
  max_subscribers: number;
  created_at: string;
  updated_at: string;
  member_count: number;
  master_account?: MemberAccountInfo | null;
  members: StrategyMemberItem[];
}

/**
 * Premium P&L / Performance Line Chart Component
 *
 * Implements:
 * - Dynamic Green-to-Red transition based on actual data values and zero-line.
 * - Positive area: subtle emerald green gradient.
 * - Negative area: subtle rose red gradient.
 * - Smooth cubic spline curves.
 * - Clean zero reference dashed line with label.
 * - Interactive hover with crosshair, dot indicator, and formatted tooltip (IST time & INR P&L).
 */
interface ChartPoint {
  time: string;
  val: number;
}

/**
 * DEV-ONLY cosmetic demo curve, used solely so the chart doesn't render as a
 * flat line while reviewing UI on the developer's own machine with no real
 * fills yet. Fixed, deterministic fractions of `initialAmount` - never
 * randomised, never sourced from or written to any real account/order data.
 *
 * Gated at runtime on the browser's own `window.location.hostname` being
 * "localhost"/"127.0.0.1" (see `useIsLocalhost` below) rather than
 * `NODE_ENV`, because this Next.js app is served from inside a Docker
 * container with `NODE_ENV=production` baked in even when that container is
 * only reachable on the developer's own machine (docker-compose.yml /
 * frontend/Dockerfile) - an env-var check alone can't tell "my own laptop"
 * apart from "the real VPS". A real deployment is reached by its domain or
 * the VPS's IP (the STATIC_VPS_IP setting, see .env.example), never by the literal
 * hostname "localhost", so this can never render for a real user.
 */
const DEV_DEMO_WAVE_FRACTIONS = [
  0.0006, 0.0010, 0.0004, 0.0008, -0.0004, -0.0024, -0.0028,
  -0.0008, 0.0002, -0.0012, -0.0024, -0.0022, -0.0008, 0.0016,
  0.0028, 0.0018, 0.0012, 0.0036, 0.0060, 0.0080,
];
const DEV_DEMO_WAVE_TIMES = [
  "09:15", "09:35", "09:55", "10:15", "10:35", "10:55", "11:15",
  "11:35", "11:55", "12:15", "12:35", "12:55", "13:15", "13:35",
  "13:55", "14:15", "14:35", "14:55", "15:15", "15:30",
];

/**
 * DEV-ONLY placeholder strategy cards, so the grid layout can be reviewed
 * with more than one card on the developer's own machine. Same localhost
 * gate as DEV_DEMO_WAVE_FRACTIONS - these exist only in the rendered list
 * and are never created in, read from, or written to the database.
 *
 * Their ids carry DEMO_ID_PREFIX, and every action handler bails out on it,
 * so a placeholder card can never fire a real delete / trading-toggle /
 * order request against the engine.
 */
const DEMO_ID_PREFIX = "__demo__";

const isDemoStrategy = (id: string) => id.startsWith(DEMO_ID_PREFIX);

function buildDemoStrategies(count: number): BackendStrategyResponse[] {
  const now = new Date().toISOString();
  return Array.from({ length: count }, (_, i) => ({
    id: `${DEMO_ID_PREFIX}${i + 1}`,
    name: ["breakout", "momentum", "mean-revert"][i % 3],
    description: "Sample strategy (local preview only)",
    visibility: "PRIVATE" as const,
    master_account_id: null,
    trading_enabled: i % 2 === 0,
    place_rejected_enabled: false,
    allocated_amount: [100000, 250000, 75000][i % 3],
    max_subscribers: 10,
    created_at: now,
    updated_at: now,
    member_count: [2, 4, 1][i % 3],
    master_account: null,
    members: [],
  }));
}

/**
 * Starts `false` (matches server-rendered HTML exactly, so hydration never
 * mismatches) and flips to `true` after mount only when the page is being
 * viewed at localhost/127.0.0.1 - a one-time client-only check.
 */
function useIsLocalhost(): boolean {
  const [isLocalhost, setIsLocalhost] = useState(false);
  useEffect(() => {
    const h = window.location.hostname;
    if (h === "localhost" || h === "127.0.0.1") setIsLocalhost(true);
  }, []);
  return isLocalhost;
}

/**
 * TradingChart — Premium P&L Performance Line Chart
 *
 * Correct green/red coloring at EXACT zero-crossings using SVG clipPath technique:
 *   - Green stroke + subtle green gradient: all line segments ABOVE zero
 *   - Red stroke + subtle red gradient:    all line segments BELOW zero
 *   - Transitions happen at the real interpolated zero-crossing, not by position estimate.
 *   - When all data is flat at ₹0.00, renders an honest neutral flat line at zero.
 *   - Light-themed white tooltip on hover.
 */
function TradingChart({
  points: rawPoints,
  totalPnlStr,
  initialAmount,
  unavailableReason,
  partialNote,
  daily = false,
}: {
  /** True when `points` are per-trading-day P&L values (one point per day),
   * not per-fill cumulative ones - only changes the tooltip label. */
  daily?: boolean;
  points?: StrategyRealizedPoint[];
  totalPnlStr?: string | null;
  initialAmount: number;
  /** Set when the engine could not verify this account's/strategy's P&L for
   * the requested range (e.g. an unresolved overnight carry - see
   * backend/app/services/pnl/historical.py). Shown as an explicit muted
   * "Unavailable" state rather than the honest-flat-zero line, since a flat
   * ₹0 line would misleadingly read as "verified zero P&L" instead of
   * "could not be verified". */
  unavailableReason?: string | null;
  /** Set alongside a REAL totalPnlStr/points when the master's own figure is
   * shown but a follower was excluded from it (see dashboard.py's
   * _strategy_pnl) - a small corner badge, never hidden, so a real number is
   * never mistaken for the full combined total. */
  partialNote?: string | null;
}) {
  const clipAboveId = useId();   // clip rect above zero → for green
  const clipBelowId = useId();   // clip rect below zero → for red
  const greenFillId  = useId();
  const redFillId    = useId();

  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  const isLocalhost = useIsLocalhost();

  // ── 1. Build the series from real engine data ─────────────────────────────
  const chartPoints: ChartPoint[] = useMemo(() => {
    if (rawPoints && rawPoints.length >= 2) {
      return rawPoints.map((p) => ({
        time: p.time,
        val:  parseFloat(p.cumulative_realized) || 0,
      }));
    }
    if (rawPoints && rawPoints.length === 1) {
      const val = parseFloat(rawPoints[0].cumulative_realized) || 0;
      return [
        { time: "09:15", val: 0 },
        { time: rawPoints[0].time, val },
      ];
    }
    if (totalPnlStr) {
      const val = parseFloat(totalPnlStr) || 0;
      // Only show a single point if it is non-zero
      if (val !== 0) {
        return [
          { time: "09:15", val: 0 },
          { time: "Now",   val },
        ];
      }
    }
    // Localhost only: a cosmetic wave instead of a flat line (see
    // DEV_DEMO_WAVE_FRACTIONS doc comment) - never shown on a real
    // deployment, which is reached by a domain or the VPS's own IP.
    if (isLocalhost) {
      return DEV_DEMO_WAVE_FRACTIONS.map((frac, i) => ({
        time: DEV_DEMO_WAVE_TIMES[i],
        val: frac * Math.max(initialAmount, 1),
      }));
    }
    // Honest flat baseline — no fills yet today
    return [
      { time: "09:15", val: 0 },
      { time: "Now",   val: 0 },
    ];
  }, [rawPoints, totalPnlStr, initialAmount, isLocalhost]);

  const vals = chartPoints.map((p: ChartPoint) => p.val);
  const isAllZero = vals.every((v) => v === 0);

  // ── 2. Coordinate system ──────────────────────────────────────────────────
  const width   = 380;
  const height  = 120;
  const padX    = 10;
  const padTop  = 10;
  const padBot  = 10;
  const plotH   = height - padTop - padBot;

  const minVal  = Math.min(0, ...vals);
  const maxVal  = Math.max(0, ...vals);
  const rawSpread = maxVal - minVal;

  // Smart Y-range: ensure enough visual spread so even small movements are visible.
  // If spread is tiny (<= 50 ₹), use a minimum 80 ₹ window so line isn't flat.
  // If truly all-zero, use a ±10 window centred on zero (renders honest flat line).
  const minSpread = isAllZero ? 20 : Math.max(rawSpread, 80);
  const midVal    = (maxVal + minVal) / 2;
  const halfRange = minSpread / 2;
  const yPad      = halfRange * 0.12; // small breathing room so peaks aren't clipped
  const dispMin   = Math.min(0, midVal - halfRange) - yPad;
  const dispMax   = Math.max(0, midVal + halfRange) + yPad;
  const dispRange = dispMax - dispMin || 1;

  const getY    = (v: number) => padTop + (1 - (v - dispMin) / dispRange) * plotH;
  const zeroY   = getY(0);

  // Guard: ensure we always have at least 2 points before mapping coords
  const safePoints = chartPoints.length >= 2 ? chartPoints : [
    { time: "09:15", val: 0 },
    { time: "Now",   val: 0 },
  ];

  const coords  = safePoints.map((p: ChartPoint, i: number) => {
    const denom = safePoints.length > 1 ? safePoints.length - 1 : 1;
    const x = padX + (i / denom) * (width - padX * 2);
    const y = getY(p.val);
    return { x, y, val: p.val, time: p.time };
  });

  // ── 3. Smooth cubic spline (Catmull-Rom → Bezier conversion) ──────────────
  // Computed inline — coords is already re-created per render so useMemo([coords])
  // would never cache anyway. Inline is cleaner and avoids stale-closure issues.
  let pathD = "";
  if (coords.length >= 2) {
    pathD = `M ${coords[0].x.toFixed(2)} ${coords[0].y.toFixed(2)}`;
    for (let i = 0; i < coords.length - 1; i++) {
      const p0 = coords[Math.max(0, i - 1)];
      const p1 = coords[i];
      const p2 = coords[i + 1];
      const p3 = coords[Math.min(coords.length - 1, i + 2)];
      const cp1x = p1.x + (p2.x - p0.x) / 6;
      const cp1y = p1.y + (p2.y - p0.y) / 6;
      const cp2x = p2.x - (p3.x - p1.x) / 6;
      const cp2y = p2.y - (p3.y - p1.y) / 6;
      pathD += ` C ${cp1x.toFixed(2)} ${cp1y.toFixed(2)}, ${cp2x.toFixed(2)} ${cp2y.toFixed(2)}, ${p2.x.toFixed(2)} ${p2.y.toFixed(2)}`;
    }
  }

  const lastCoord = coords[coords.length - 1];
  const firstX    = coords[0].x;
  const lastX     = lastCoord.x;

  // Closed area path — anchored to the ZERO line, not the bottom of the SVG.
  // Anchoring at the bottom made the below-zero clip fill the entire width
  // as one solid block; against zero, each fill only ever covers the gap
  // between the line and ₹0, which is what a P&L area chart actually means.
  const areaD = `${pathD} L ${lastX.toFixed(2)} ${zeroY.toFixed(2)} L ${firstX.toFixed(2)} ${zeroY.toFixed(2)} Z`;

  // Max drawdown — largest peak-to-trough fall in the series being shown,
  // as a share of the strategy's allocated amount. Derived from the same
  // points as the line; nothing extra is assumed or fetched.
  let ddPeak = -Infinity;
  let maxDrawdown = 0;
  for (const v of vals) {
    if (v > ddPeak) ddPeak = v;
    const dd = v - ddPeak;
    if (dd < maxDrawdown) maxDrawdown = dd;
  }
  const maxDrawdownPct = initialAmount > 0 ? (maxDrawdown / initialAmount) * 100 : 0;

  // ── 4. Hover state ─────────────────────────────────────────────────────────
  const handleMouseMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect  = e.currentTarget.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    setHoverIndex(Math.round(ratio * (chartPoints.length - 1)));
  };

  const activeCoord       = hoverIndex !== null ? coords[hoverIndex] : lastCoord;
  const activePointPos    = activeCoord.val >= 0;
  const currentVal        = activeCoord.val;

  // ── 5. Tooltip date ────────────────────────────────────────────────────────
  const formattedDate = useMemo(() => {
    const t = activeCoord.time;
    if (!t || t === "09:15" || t === "Start") {
      return "Market Open";
    }
    if (t === "Now") {
      return "Current";
    }
    // A bare "HH:MM" label is already the display value - handing it to
    // new Date() yields "Invalid Date", so return it untouched.
    if (/^\d{1,2}:\d{2}$/.test(t)) {
      return t;
    }
    // A bare trading day (YYYY-MM-DD, already IST) is formatted in UTC so the
    // viewer's own timezone can never shift it to a neighbouring day.
    if (/^\d{4}-\d{2}-\d{2}$/.test(t)) {
      return new Date(`${t}T00:00:00Z`).toLocaleDateString("en-IN", {
        day: "2-digit",
        month: "short",
        year: "numeric",
        timeZone: "UTC",
      });
    }
    try {
      const d = new Date(t);
      if (!isNaN(d.getTime())) {
        // Format: "07 Aug 2026" — matching reference design
        const dateStr = d.toLocaleDateString("en-IN", {
          day: "2-digit",
          month: "short",
          year: "numeric",
        });
        return dateStr;
      }
    } catch { /* fallback */ }
    return formatIstTime(t);
  }, [activeCoord.time]);

  // ── 6. Neutral line color for flat-zero state ──────────────────────────────
  // When all values are zero we draw the line in a neutral #16A34A (green)
  // because "zero" is still technically non-negative. No data invented.

  // A real reason (from the engine's fail-closed historical P&L check) plus
  // no usable points means this is a genuine "could not verify", never a
  // verified ₹0 - show that plainly instead of the flat baseline below,
  // which is reserved for the honest "no fills at all" case.
  if (unavailableReason && isAllZero) {
    return (
      <div className="relative">
        <div className="relative h-32 bg-gradient-to-b from-white to-[#F9FAFB] border border-[#E5E7EB] rounded-[10px] overflow-hidden flex flex-col items-center justify-center gap-1 px-6 text-center">
          <span className="text-xs font-bold text-[#6B7280]">P&amp;L history unavailable</span>
          <span className="text-[10px] text-[#9CA3AF] leading-snug" title={unavailableReason}>
            {unavailableReason}
          </span>
        </div>
      </div>
    );
  }

  return (
    <div className="relative">
      <div className="relative h-32 bg-gradient-to-b from-white to-[#F9FAFB] border border-[#E5E7EB] rounded-[10px] overflow-hidden select-none">
        {partialNote && (
          <Tooltip content={partialNote} focusable><span
            className="absolute top-2 left-2 z-10 px-1.5 py-0.5 rounded text-[9px] font-bold uppercase tracking-wide bg-amber-50 text-amber-700 border border-amber-200"
          >
            Partial
          </span></Tooltip>
        )}
        <svg
          className="w-full h-full cursor-crosshair"
          viewBox={`0 0 ${width} ${height}`}
          preserveAspectRatio="none"
          onMouseMove={handleMouseMove}
          onMouseLeave={() => setHoverIndex(null)}
        >
          <defs>
            {/*
              ClipPath technique for accurate zero-crossing colour split:
              - clipAbove: rectangle that covers ONLY the area above the zero line → green region
              - clipBelow: rectangle that covers ONLY the area below the zero line → red region
              Both the stroke line and the fill area are drawn TWICE, each clipped to its region.
            */}
            <clipPath id={clipAboveId}>
              <rect x="0" y="0" width={width} height={zeroY} />
            </clipPath>
            <clipPath id={clipBelowId}>
              <rect x="0" y={zeroY} width={width} height={height - zeroY} />
            </clipPath>

            {/*
              Both gradients are pinned in user space so they fade toward the
              ZERO line rather than across each fill's own bounding box -
              that is what keeps the wash light near ₹0 and strongest at the
              extremes, however far the line happens to travel.
            */}
            {/* Green area gradient — strongest at the peak, fading into zero */}
            <linearGradient
              id={greenFillId}
              gradientUnits="userSpaceOnUse"
              x1="0" y1={Math.min(padTop, zeroY - 1)} x2="0" y2={zeroY}
            >
              <stop offset="0%"   stopColor="#16A34A" stopOpacity="0.28" />
              <stop offset="60%"  stopColor="#16A34A" stopOpacity="0.10" />
              <stop offset="100%" stopColor="#16A34A" stopOpacity="0.02" />
            </linearGradient>

            {/* Red area gradient — fading out of zero, strongest at the trough */}
            <linearGradient
              id={redFillId}
              gradientUnits="userSpaceOnUse"
              x1="0" y1={zeroY} x2="0" y2={Math.max(height - padBot, zeroY + 1)}
            >
              <stop offset="0%"   stopColor="#EF4444" stopOpacity="0.02" />
              <stop offset="40%"  stopColor="#EF4444" stopOpacity="0.10" />
              <stop offset="100%" stopColor="#EF4444" stopOpacity="0.26" />
            </linearGradient>
          </defs>

          {/* Subtle horizontal grid lines */}
          {[0.25, 0.5, 0.75].map((frac) => {
            const gy = padTop + frac * plotH;
            return (
              <line
                key={frac}
                x1={padX} y1={gy} x2={width - padX} y2={gy}
                stroke="#F3F4F6"
                strokeWidth="0.5"
                vectorEffect="non-scaling-stroke"
              />
            );
          })}

          {/* No drawn zero line — the green/red split already marks where ₹0
              sits, so the extra dashed rule only added noise. zeroY is still
              the baseline the fills and gradients are built against. */}

          {/* ── Fills (clipped) ── */}
          {!isAllZero && (
            <>
              {/* Green fill — above zero only */}
              <path
                d={areaD}
                fill={`url(#${greenFillId})`}
                clipPath={`url(#${clipAboveId})`}
              />
              {/* Red fill — below zero only */}
              <path
                d={areaD}
                fill={`url(#${redFillId})`}
                clipPath={`url(#${clipBelowId})`}
              />
            </>
          )}

          {/* ── Stroke (clipped) ── */}
          {isAllZero ? (
            // Flat line at zero — draw once in neutral green (no fake movement)
            <path
              d={pathD}
              fill="none"
              stroke="#16A34A"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              vectorEffect="non-scaling-stroke"
              opacity="0.8"
            />
          ) : (
            <>
              {/* Green segment — above zero */}
              <path
                d={pathD}
                fill="none"
                stroke="#16A34A"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                vectorEffect="non-scaling-stroke"
                clipPath={`url(#${clipAboveId})`}
              />
              {/* Red segment — below zero */}
              <path
                d={pathD}
                fill="none"
                stroke="#EF4444"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                vectorEffect="non-scaling-stroke"
                clipPath={`url(#${clipBelowId})`}
              />
            </>
          )}

        </svg>

        {/*
          Position marker, drawn as HTML rather than an SVG <circle>: the svg
          above uses preserveAspectRatio="none", which scales x and y by
          different factors and would squash any circle into an ellipse.
          A div sits outside that transform and stays perfectly round.
          `activeCoord` already falls back to the last point when nothing is
          hovered, so this is one mark that moves, not two that swap.
        */}
        <span
          className="absolute w-2.5 h-2.5 rounded-full bg-white border-2 pointer-events-none -translate-x-1/2 -translate-y-1/2 shadow-[0_1px_3px_rgba(15,23,42,0.18)]"
          style={{
            left: `${(activeCoord.x / width) * 100}%`,
            top: `${(activeCoord.y / height) * 100}%`,
            borderColor: activePointPos ? "#16A34A" : "#EF4444",
          }}
        />

        {/* Light-themed tooltip — matches Jettone white card style. Offset to
            the side of the dot with more room (not above/below it): a
            top/bottom flip still overlapped the dot whenever it sat near
            vertical center, since the tooltip's own height reaches past the
            midline either way. Left/right never has that problem - the
            chart is wide relative to the tooltip, so shifting to whichever
            side has more horizontal room always clears the dot. */}
        {hoverIndex !== null && (
          <div
            className="absolute pointer-events-none bg-white border border-[#E5E7EB] rounded-lg shadow-[0_4px_14px_rgba(15,23,42,0.12)] px-2 py-1.5 z-30 min-w-[132px]"
            style={{
              top: `${Math.max(15, Math.min(85, (activeCoord.y / height) * 100))}%`,
              ...((activeCoord.x / width) * 100 < 50
                ? { left: `${(activeCoord.x / width) * 100}%`, transform: "translate(14px, -50%)" }
                : { right: `${100 - (activeCoord.x / width) * 100}%`, transform: "translate(-14px, -50%)" }),
            }}
          >
            <div className="flex items-center gap-1 text-[9px] font-semibold text-[#4B5563] pb-1 mb-1 border-b border-[#F3F4F6]">
              <CalendarDays className="w-3 h-3 text-gray-500 shrink-0" aria-hidden />
              <span className="truncate">{formattedDate}</span>
            </div>
            <div className="space-y-0.5">
              <div className="flex items-center justify-between gap-2.5">
                <span className="text-[#6B7280] flex items-center gap-1 font-semibold text-[9px]">
                  {activePointPos
                    ? <TrendingUp   className="w-2.5 h-2.5 text-green-600 shrink-0" />
                    : <TrendingDown className="w-2.5 h-2.5 text-red-600 shrink-0" />}
                  {daily ? "DAILY P&L" : "PNL"}
                </span>
                <span className={`font-bold font-mono text-[10px] ${activePointPos ? "text-green-600" : "text-red-600"}`}>
                  {activePointPos ? "+" : ""}₹{currentVal.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
                </span>
              </div>
              <div className="flex items-center justify-between gap-2.5">
                <span className="text-[#6B7280] flex items-center gap-1 font-semibold text-[9px]">
                  <Activity className="w-2.5 h-2.5 text-amber-500 shrink-0" />
                  MAX DD
                </span>
                <span className="font-bold font-mono text-[10px] text-amber-600">
                  {maxDrawdownPct.toFixed(2)}%
                </span>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default function GroupStrategyPage() {
  const router = useRouter();

  // Fetch strategies from backend
  const {
    data: rawStrategies,
    error: stratError,
    isLoading: stratLoading,
    refresh: refreshStrategies,
  } = useEngineData<BackendStrategyResponse[]>("/strategies");

  // Each card's chart is a since-inception equity curve (like a real trading
  // app), not "Today only" - "inception" = the earliest strategy this admin
  // has created, so a strategy created later never loses days it never had.
  // Deferred (path=null) until strategies have loaded, since the range needs
  // rawStrategies first; useEngineData treats null as "don't fetch yet".
  const sinceInceptionQuery = useMemo(() => {
    if (!rawStrategies || rawStrategies.length === 0) return null;
    const earliest = rawStrategies.reduce(
      (min, s) => (s.created_at < min ? s.created_at : min),
      rawStrategies[0].created_at,
    );
    const from = earliest.slice(0, 10);
    const to = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" }).format(new Date());
    return `range=custom&from=${from}&to=${to}`;
  }, [rawStrategies]);

  // Fetch real-time dashboard summary & realized timeline for live P&L and execution metrics
  const { data: summary } = useEngineData<DashboardSummary>(
    sinceInceptionQuery ? `/dashboard/summary?${sinceInceptionQuery}` : null,
    15000,
  );
  const { data: timeline, isLoading: timelineLoading } = useEngineData<DashboardRealizedTimeline>(
    sinceInceptionQuery ? `/dashboard/realized-timeline?${sinceInceptionQuery}` : null,
    15000,
  );
  // The IST trading day, for the card's "Today" figure (same zone the range
  // query above uses for its end date).
  const istFormat = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Kolkata" });
  const todayIso = istFormat.format(new Date());
  // A strategy's creation instant as its IST trading day (YYYY-MM-DD).
  const istDay = (iso: string) => istFormat.format(new Date(iso));

  // Gates the placeholder cards - see buildDemoStrategies.
  const isLocalhost = useIsLocalhost();

  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setMounted(true);
  }, []);

  // Search & Filter
  const [searchTerm, setSearchTerm] = useState("");

  // Modals & Drawers
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showOrderModal, setShowOrderModal] = useState<BackendStrategyResponse | null>(null);
  // Which strategy card's "⋮" overflow menu is open (only one at a time)

  // Create Strategy Form State
  const [newStratName, setNewStratName] = useState("");
  const [newStratVisibility, setNewStratVisibility] = useState<"PRIVATE" | "PUBLIC">("PRIVATE");
  const [newStratMaxSubs, setNewStratMaxSubs] = useState("10");
  const [newStratCapital, setNewStratCapital] = useState("100000");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  // Place Order State
  const [orderScriptType, setOrderScriptType] = useState<"Equity" | "CE" | "PE" | "Future">("Equity");
  const [orderSide, setOrderSide] = useState<"BUY" | "SELL">("BUY");
  const [orderSymbol, setOrderSymbol] = useState("RELIANCE-EQ (NSE)");
  const [orderExchange, setOrderExchange] = useState("NSE");
  const [orderProduct, setOrderProduct] = useState("INTRADAY");
  const [orderQty, setOrderQty] = useState<string>("1");
  const [orderType, setOrderType] = useState<"Market" | "Limit" | "SL">("Market");
  const [orderPrice, setOrderPrice] = useState("");
  const [orderTriggerPrice, setOrderTriggerPrice] = useState("");
  const [placingOrder, setPlacingOrder] = useState(false);
  const [orderResult, setOrderResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [scriptSuggestions, setScriptSuggestions] = useState<Array<{ symbol: string; trading_symbol: string; exchange: string }>>([]);
  const [isScriptDropdownOpen, setIsScriptDropdownOpen] = useState(false);
  const [scriptSearchQuery, setScriptSearchQuery] = useState("");

  // Fetch script suggestions dynamically
  useEffect(() => {
    let active = true;
    const fetchScripts = async () => {
      try {
        const q = encodeURIComponent(scriptSearchQuery.trim());
        const resp = await fetch(`/api/v1/broker/instruments/search?query=${q}&script_type=${orderScriptType}`);
        if (resp.ok && active) {
          const data = await resp.json();
          setScriptSuggestions(data.instruments || []);
        }
      } catch (err) {
        console.error("Failed to fetch script suggestions", err);
      }
    };
    fetchScripts();
    return () => {
      active = false;
    };
  }, [orderScriptType, scriptSearchQuery]);

  const strategiesList = rawStrategies ?? [];

  // Handle Strategy Trading Toggle
  const toggleTrading = async (strategy: BackendStrategyResponse) => {
    if (isDemoStrategy(strategy.id)) return;
    try {
      await fetch(`/api/v1/strategies/${strategy.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trading_enabled: !strategy.trading_enabled }),
      });
      refreshStrategies();
    } catch (err) {
      console.error("Failed to toggle trading", err);
    }
  };

  // Handle Delete Strategy
  const handleDeleteStrategy = async (strategyId: string) => {
    if (isDemoStrategy(strategyId)) return;
    if (!confirm("Are you sure you want to delete this strategy?")) return;
    try {
      await fetch(`/api/v1/strategies/${strategyId}`, {
        method: "DELETE",
      });
      refreshStrategies();
    } catch (err) {
      console.error("Failed to delete strategy", err);
    }
  };

  // Handle Create Strategy Submit
  const handleCreateStrategy = async (e: FormEvent) => {
    e.preventDefault();
    setCreating(true);
    setCreateError(null);

    try {
      const resp = await fetch("/api/v1/strategies", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: newStratName.trim(),
          visibility: newStratVisibility,
          allocated_amount: parseFloat(newStratCapital) || 100000,
          max_subscribers: parseInt(newStratMaxSubs) || 10,
        }),
      });

      if (!resp.ok) {
        const errData = await resp.json().catch(() => ({}));
        throw new Error(errData.detail || "Failed to create strategy");
      }

      const created = await resp.json();
      setShowCreateModal(false);
      setNewStratName("");
      refreshStrategies();
      if (created?.id) {
        router.push(`/group-strategy/${created.id}`);
      }
    } catch (err: unknown) {
      setCreateError(err instanceof Error ? err.message : "Failed to create strategy");
    } finally {
      setCreating(false);
    }
  };

  // Place Order Submit
  const handlePlaceOrder = async (e: FormEvent) => {
    e.preventDefault();
    if (!showOrderModal) return;

    if (!orderSymbol.trim()) {
      setOrderResult({ ok: false, text: "Please enter or select a Script / Symbol." });
      return;
    }

    const qty = Number(orderQty);
    if (!orderQty || !Number.isInteger(qty) || qty <= 0) {
      setOrderResult({ ok: false, text: "Quantity must be a positive integer." });
      return;
    }

    if (orderType === "Limit" || orderType === "SL") {
      const p = Number(orderPrice);
      if (!orderPrice || isNaN(p) || p <= 0) {
        setOrderResult({ ok: false, text: "Limit Price must be greater than 0." });
        return;
      }
    }

    if (orderType === "SL") {
      const tp = Number(orderTriggerPrice);
      if (!orderTriggerPrice || isNaN(tp) || tp <= 0) {
        setOrderResult({ ok: false, text: "Trigger Price must be greater than 0 for Stop Loss (SL)." });
        return;
      }
    }

    let resolvedSymbol = orderSymbol.trim();
    let resolvedExchange = orderExchange || "NSE";
    const match = orderSymbol.match(/^([A-Za-z0-9\s\-]+)\s*\((NSE|BSE|NFO|MCX)\)$/i);
    if (match) {
      resolvedSymbol = match[1].trim();
      resolvedExchange = match[2].toUpperCase();
    } else if (orderScriptType === "CE" || orderScriptType === "PE" || orderScriptType === "Future") {
      resolvedExchange = "NFO";
    }

    setPlacingOrder(true);
    setOrderResult(null);

    try {
      const resp = await fetch("/api/v1/broker/manual-order", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          // Placed on THIS strategy's Master and copied to its Children.
          strategy_id: showOrderModal.id,
          symbol: resolvedSymbol.toUpperCase(),
          exchange: resolvedExchange.toUpperCase(),
          side: orderSide.toUpperCase(),
          quantity: qty,
          product_type: orderProduct,
          order_type: orderType.toUpperCase(),
          price: (orderType === "Limit" || orderType === "SL") ? Number(orderPrice) : 0,
          trigger_price: orderType === "SL" ? Number(orderTriggerPrice) : 0,
        }),
      });

      const data = (await resp.json()) as { placed?: boolean; master_order_id?: string; message?: string; detail?: string };
      if (resp.ok && data.placed) {
        setOrderResult({ ok: true, text: `Order executed. Master order ID: ${data.master_order_id}` });
      } else {
        setOrderResult({ ok: false, text: `${extractError(data) ?? data.message ?? "Failed to place order."}` });
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Network error placing order";
      setOrderResult({ ok: false, text: `Error: ${msg}` });
    } finally {
      setPlacingOrder(false);
    }
  };

  // Filtered strategies for main list — backend order is kept as-is.
  const realStrategies = strategiesList
    .filter((s) => s.name.toLowerCase().includes(searchTerm.toLowerCase()));

  // Localhost only - see buildDemoStrategies. Appended after the real ones so
  // a real strategy is never displaced or reordered by a placeholder.
  const filteredStrategies = isLocalhost
    ? [...realStrategies, ...buildDemoStrategies(3)]
    : realStrategies;

  return (
    <div className="space-y-6 max-w-page mx-auto pb-16">
      {/* Header & Main Action */}
      <div className="flex items-center justify-between flex-wrap gap-4">
        <div>
          <h1 className="text-xl font-extrabold text-[#111111] tracking-tight">Group Strategy</h1>
        </div>

        <div className="flex items-center gap-2">
          <Button
            variant="primary"
            size="sm"
            onClick={() => setShowCreateModal(true)}
            className="px-3.5"
          >
            <Plus className="w-4 h-4" />
            Create Group Strategy
          </Button>
          <button
            onClick={() => refreshStrategies()}
            className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-[#E5E7EB] bg-white text-[#4B5563] hover:text-[#111111] hover:bg-gray-50 transition-colors shadow-sm"
            title="Refresh"
          >
            <RotateCw className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* Search and Filters Bar */}
      <div className="relative w-full max-w-xs">
        <Search className="w-4 h-4 absolute left-3.5 top-1/2 -translate-y-1/2 text-[#9CA3AF]" />
        <input
          type="text"
          placeholder="Search strategies..."
          value={searchTerm}
          onChange={(e) => setSearchTerm(e.target.value)}
          className="w-full pl-10 pr-4 h-8 py-0 rounded-lg bg-white border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
        />
      </div>

      {/* Strategies Cards Grid */}
      {stratLoading ? (
        <LoadingPanel />
      ) : stratError ? (
        <ErrorPanel message={extractError(stratError) ?? "Failed to load strategies"} onRetry={refreshStrategies} />
      ) : filteredStrategies.length === 0 ? (
        <EmptyState
          title="No Group Strategies Found"
          hint="Create a new group strategy to start automating your trading across multiple accounts."
        />
      ) : (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,19.5rem),1fr))] gap-6">
          {filteredStrategies.map((strat) => {
            const perf = summary?.strategies?.list?.find((s) => s.id === strat.id);
            // The engine's per-trading-day P&L (each day on its own, one bad
            // day never hides the rest). `points` is only used by the old
            // line chart, as the fallback for an engine that predates `daily`.
            const stratTimeline = timeline?.strategies?.[strat.id];
            const hasDaily = Array.isArray(stratTimeline?.daily);
            const stratPoints = stratTimeline?.points;
            const isLiveRunning = strat.trading_enabled && perf?.status === "RUNNING";

            // Child stats from real strategy members
            const connectedChildren = strat.members
              ? strat.members.filter((m) => m.trading_enabled && (m.account?.session_valid ?? true)).length
              : strat.member_count;
            const failedChildren = Math.max(0, strat.member_count - connectedChildren);

            // Real master account / subtitle
            const subtitleText =
              strat.description?.trim() ||
              (strat.master_account?.display_name ? `Master: ${strat.master_account.display_name}` : "Group copy-trading strategy");

            const masterIdentifier =
              strat.master_account?.display_name ??
              strat.master_account?.alice_user_id ??
              (strat.master_account_id ? `Master ID: ${strat.master_account_id.slice(0, 18)}...` : "Default Master Account");

            return (
              <div
                key={strat.id}
                data-strategy-card className="bg-white border border-[#E5E7EB] rounded-[10px] shadow-[0_2px_8px_rgba(0,0,0,0.04)] hover:shadow-lg hover:border-[#D1D5DB] transition-all flex flex-col justify-between"
              >
                <div className="p-4 space-y-2.5">
                  {/* 1. Header: Icon Avatar + Strategy Name & LIVE Status + Actions */}
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex items-center gap-3 min-w-0">
                      <div className="w-9 h-9 rounded-full bg-orange-50 text-jettone-orange flex items-center justify-center shrink-0 border border-orange-100 shadow-sm">
                        <Users className="w-4 h-4" />
                      </div>
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <h3 className="text-base font-bold text-[#111111] tracking-tight truncate">
                            {strat.name}
                          </h3>
                          <span
                            className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-semibold border ${
                              strat.trading_enabled
                                ? isLiveRunning
                                  ? "bg-green-50 text-green-700 border-green-200"
                                  : "bg-green-50 text-green-700 border-green-200"
                                : "bg-gray-100 text-gray-600 border-gray-200"
                            }`}
                          >
                            <span
                              className={`w-1.5 h-1.5 rounded-full ${
                                strat.trading_enabled ? "bg-green-500" : "bg-gray-400"
                              } ${isLiveRunning ? "animate-pulse" : ""}`}
                            />
                            {strat.trading_enabled ? "LIVE" : "IDLE"}
                          </span>
                        </div>
                        <p className="text-xs text-[#6B7280] truncate mt-0.5">
                          {subtitleText}
                        </p>
                      </div>
                    </div>

                    {/* Action buttons matching reference rounded boxes */}
                    <div className="flex items-center gap-1.5 text-[#6B7280] shrink-0" onClick={(e) => e.stopPropagation()}>
                      <Tooltip content="View details"><button
                        type="button"
                        onClick={() => router.push(`/group-strategy/${strat.id}`)}
                        className="w-8 h-8 rounded-lg border border-[#E5E7EB] bg-[#F9FAFB] flex items-center justify-center transition-colors shadow-xs hover:bg-gray-100 hover:text-[#111111]"
                        aria-label="View details"
                      >
                        <Eye className="w-3.5 h-3.5" />
                      </button></Tooltip>
                      <Tooltip content="Delete strategy"><button
                        type="button"
                        onClick={() => handleDeleteStrategy(strat.id)}
                        className="w-8 h-8 rounded-lg border border-red-200 bg-red-50 text-red-600 flex items-center justify-center transition-colors shadow-xs hover:bg-red-100"
                        aria-label="Delete strategy"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button></Tooltip>

                    </div>
                  </div>

                  {/* 2. P&L: today's headline, 7-day total and the 7-day bars,
                      then ONE line chart that alternates Daily <-> Cumulative
                      every ~3s - all from the engine's already-fetched per-day
                      P&L. Only an engine that predates `daily` (or a failed
                      timeline fetch) falls back to the old line chart. */}
                  {hasDaily ? (
                    <StrategyPnlSnapshot
                      daily={stratTimeline?.daily ?? []}
                      unavailableReason={stratTimeline?.unavailable_reason}
                      todayIso={todayIso}
                      startIso={istDay(strat.created_at)}
                    />
                  ) : timelineLoading && !timeline ? (
                    <StrategyPnlSkeleton />
                  ) : (
                    <TradingChart
                      points={stratPoints}
                      totalPnlStr={perf?.pnl?.realized_pnl ?? perf?.pnl?.total_pnl}
                      initialAmount={strat.allocated_amount}
                      unavailableReason={perf?.pnl?.unavailable_reason}
                      partialNote={perf?.pnl?.partial_note}
                    />
                  )}

                  {/* 3. Master Account & Subscribed / Amount Summary */}
                  <div>
                    <h4 className="text-sm font-bold text-[#111111] tracking-tight truncate">
                      {masterIdentifier}
                    </h4>
                    <div className="flex items-center justify-between text-xs mt-0.5">
                      <div>
                        <span className="text-[#6B7280]">Subscribed </span>
                        {/* Count only - `max_subscribers` is stored but never
                            enforced (backend/app/api/strategies.py::add_member
                            does not check it), so showing "/N" advertised a
                            cap that does not exist. */}
                        <span className="font-bold text-green-600">
                          {strat.member_count}
                        </span>
                      </div>
                      <div className="flex items-baseline gap-1">
                        <span className="text-[#6B7280] text-xs font-medium">Amount</span>
                        <span className="font-bold text-[#111111] text-sm">
                          {(strat.allocated_amount / 1000).toFixed(1)}K
                        </span>
                      </div>
                    </div>
                  </div>

                  {/* 4. Execution Statistics 3-Column Box (Exact Reference Design).
                      The box is a CSS container, so its contents adapt to the
                      BOX width rather than the viewport (cards are ~230px at a
                      1024px window but ~370px at 1440px):
                        > 302px  unchanged
                        <= 302px slightly smaller type, tighter padding
                        <= 252px the three sections stack as rows - below that
                                 Pend/Done/Rej + Done/Left + Online/Failed no
                                 longer fit side by side at a legible size.
                      Widths are the box's INNER width (a container query
                      excludes the box's own padding and border, 18px here).
                      Thresholds come from measured text widths in the browser:
                      they keep at least ~3px between neighbouring labels. */}
                  <div className="bg-[#F9FAFB] border border-[#E5E7EB] rounded-[10px] p-2 grid grid-cols-3 divide-x divide-[#E5E7EB] [container-type:inline-size] [@container_(max-width:252px)]:divide-x-0 [@container_(max-width:252px)]:divide-y">
                    {/* Orders Breakdown */}
                    <div className="px-2 [@container_(max-width:302px)]:px-1 [@container_(max-width:252px)]:col-span-3 [@container_(max-width:252px)]:flex [@container_(max-width:252px)]:items-center [@container_(max-width:252px)]:justify-between [@container_(max-width:252px)]:gap-2 [@container_(max-width:252px)]:py-1">
                      <div className="mb-1 flex items-center justify-center gap-2 [@container_(max-width:302px)]:gap-1 [@container_(max-width:252px)]:mb-0">
                        <span className="font-semibold text-xs text-[#4B5563] [@container_(max-width:302px)]:text-[11px]">Orders</span>
                        <span className="font-bold text-sm text-[#111111] [@container_(max-width:302px)]:text-[13px]">{perf?.execution?.considered_orders ?? 0}</span>
                      </div>
                      <div className="grid grid-cols-3 text-center [@container_(max-width:252px)]:gap-x-2">
                        <Tooltip content="Pending: orders still working"><div>
                          <span className="text-[#9CA3AF] block text-[10px] font-medium [@container_(max-width:302px)]:text-[9px]">Pend</span>
                          <span className="font-bold text-amber-500 text-xs [@container_(max-width:302px)]:text-[11px]">{perf?.execution?.working_orders ?? 0}</span>
                        </div></Tooltip>
                        <Tooltip content="Done: orders filled"><div>
                          <span className="text-[#9CA3AF] block text-[10px] font-medium [@container_(max-width:302px)]:text-[9px]">Done</span>
                          <span className="font-bold text-green-600 text-xs [@container_(max-width:302px)]:text-[11px]">{perf?.execution?.filled_orders ?? 0}</span>
                        </div></Tooltip>
                        <Tooltip content="Rejected: orders the broker rejected"><div>
                          <span className="text-[#9CA3AF] block text-[10px] font-medium [@container_(max-width:302px)]:text-[9px]">Rej</span>
                          <span className="font-bold text-red-500 text-xs [@container_(max-width:302px)]:text-[11px]">{perf?.execution?.rejected_orders ?? 0}</span>
                        </div></Tooltip>
                      </div>
                    </div>

                    {/* Quantity Breakdown */}
                    <div className="px-2 [@container_(max-width:302px)]:px-1 [@container_(max-width:252px)]:col-span-3 [@container_(max-width:252px)]:flex [@container_(max-width:252px)]:items-center [@container_(max-width:252px)]:justify-between [@container_(max-width:252px)]:gap-2 [@container_(max-width:252px)]:py-1">
                      <div className="mb-1 flex items-center justify-center gap-2 [@container_(max-width:302px)]:gap-1 [@container_(max-width:252px)]:mb-0">
                        <span className="font-semibold text-xs text-[#4B5563] [@container_(max-width:302px)]:text-[11px]">Qty</span>
                        <span className="font-bold text-sm text-[#111111] [@container_(max-width:302px)]:text-[13px]">{perf?.execution?.target_quantity ?? 0}</span>
                      </div>
                      <div className="grid grid-cols-2 text-center [@container_(max-width:252px)]:gap-x-2">
                        <Tooltip content="Done: quantity filled"><div>
                          <span className="text-[#9CA3AF] block text-[10px] font-medium [@container_(max-width:302px)]:text-[9px]">Done</span>
                          <span className="font-bold text-green-600 text-xs [@container_(max-width:302px)]:text-[11px]">{perf?.execution?.filled_quantity ?? 0}</span>
                        </div></Tooltip>
                        <Tooltip content="Left: target quantity not yet filled"><div>
                          <span className="text-[#9CA3AF] block text-[10px] font-medium [@container_(max-width:302px)]:text-[9px]">Left</span>
                          <span className="font-bold text-red-500 text-xs [@container_(max-width:302px)]:text-[11px]">
                            {Math.max(0, (perf?.execution?.target_quantity ?? 0) - (perf?.execution?.filled_quantity ?? 0))}
                          </span>
                        </div></Tooltip>
                      </div>
                    </div>

                    {/* Child Breakdown */}
                    <div className="px-2 [@container_(max-width:302px)]:px-1 [@container_(max-width:252px)]:col-span-3 [@container_(max-width:252px)]:flex [@container_(max-width:252px)]:items-center [@container_(max-width:252px)]:justify-between [@container_(max-width:252px)]:gap-2 [@container_(max-width:252px)]:py-1">
                      <div className="mb-1 flex items-center justify-center gap-2 [@container_(max-width:302px)]:gap-1 [@container_(max-width:252px)]:mb-0">
                        <span className="font-semibold text-xs text-[#4B5563] [@container_(max-width:302px)]:text-[11px]">Child</span>
                        <span className="font-bold text-sm text-[#111111] [@container_(max-width:302px)]:text-[13px]">{strat.member_count}</span>
                      </div>
                      <div className="grid grid-cols-2 text-center [@container_(max-width:252px)]:gap-x-2">
                        <Tooltip content="Online: children with trading on and a valid session"><div>
                          <span className="text-[#9CA3AF] block text-[10px] font-medium [@container_(max-width:302px)]:text-[9px]">Online</span>
                          <span className="font-bold text-green-600 text-xs [@container_(max-width:302px)]:text-[11px]">{connectedChildren}</span>
                        </div></Tooltip>
                        <Tooltip content="Failed: children that are off or have no valid session"><div>
                          <span className="text-[#9CA3AF] block text-[10px] font-medium [@container_(max-width:302px)]:text-[9px]">Failed</span>
                          <span className="font-bold text-red-500 text-xs [@container_(max-width:302px)]:text-[11px]">{failedChildren}</span>
                        </div></Tooltip>
                      </div>
                    </div>
                  </div>
                </div>

                {/* 5. Footer: Trading Switch + Place Order Vibrant Button */}
                <div
                  className="px-4 py-2.5 bg-[#F7F7F7] rounded-b-[10px] flex items-center justify-between border-t border-[#E5E7EB]"
                  onClick={(e) => e.stopPropagation()}
                >
                  <div className="flex items-center gap-2.5">
                    <span className="text-xs font-medium text-[#4B5563]">Trading</span>
                    <button
                      role="switch"
                      aria-checked={strat.trading_enabled}
                      onClick={() => toggleTrading(strat)}
                      className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${
                        strat.trading_enabled ? "bg-[#16A34A]" : "bg-[#EF4444]"
                      }`}
                    >
                      <span
                        className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
                          strat.trading_enabled ? "translate-x-4" : "translate-x-0"
                        }`}
                      />
                    </button>
                  </div>

                  <button
                    onClick={() => {
                      if (isDemoStrategy(strat.id)) return;
                      setShowOrderModal(strat);
                      setOrderScriptType("Equity");
                      setOrderSymbol("RELIANCE-EQ (NSE)");
                      setOrderExchange("NSE");
                      setOrderProduct("INTRADAY");
                      setOrderSide("BUY");
                      setOrderQty("1");
                      setOrderType("Market");
                      setOrderPrice("");
                      setOrderTriggerPrice("");
                      setOrderResult(null);
                    }}
                    className="inline-flex items-center justify-center gap-1.5 h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm transition-all active:scale-[0.98] bg-[#2563EB] hover:bg-[#1D4ED8] text-white border-[#2563EB] hover:border-[#1D4ED8] active:bg-[#1D4ED8]"
                  >
                    Place Order
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {/* ========================================================================= */}
      {/* DRAWER: CREATE GROUP STRATEGY                                             */}
      {/* ========================================================================= */}
      {mounted && showCreateModal && createPortal(
        <div className="fixed inset-0 z-[9999] flex justify-end pointer-events-auto">
          <div
            className="fixed inset-0 bg-black/40 backdrop-blur-sm transition-opacity duration-200 animate-in fade-in"
            onClick={() => setShowCreateModal(false)}
            aria-hidden="true"
          />

          <div
            className="relative z-10 w-full max-w-md h-full bg-white border-l border-[#E5E7EB] shadow-2xl flex flex-col justify-between box-border animate-in slide-in-from-right duration-300 ease-out"
            role="dialog"
            aria-modal="true"
          >
            {/* Header */}
            <div className="px-6 py-4 border-b border-[#E5E7EB] bg-[#F7F7F7] flex items-center justify-between shrink-0">
              <h3 className="text-sm font-bold text-[#111111] tracking-tight">Create Group Strategy</h3>
              <button
                onClick={() => setShowCreateModal(false)}
                className="p-1.5 rounded-lg text-[#9CA3AF] hover:text-[#111111] hover:bg-gray-100 transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            {/* Content */}
            <div className="flex-1 min-h-0 overflow-y-auto p-6 space-y-4">
              <form id="create-group-strat-form" onSubmit={handleCreateStrategy} className="space-y-4">
                {createError && (
                  <div className="p-3 rounded-[10px] bg-red-50 border border-red-200 text-xs font-semibold text-red-700">
                    {createError}
                  </div>
                )}

                <div>
                  <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                    Strategy Name *
                  </label>
                  <input
                    type="text"
                    required
                    placeholder="Enter Strategy Name"
                    value={newStratName}
                    onChange={(e) => setNewStratName(e.target.value)}
                    className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-black"
                  />
                </div>

                <div>
                  <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                    Visibility
                  </label>
                  <select
                    value={newStratVisibility}
                    onChange={(e) => setNewStratVisibility(e.target.value as "PRIVATE" | "PUBLIC")}
                    className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs font-semibold text-[#111111] focus:outline-none focus:ring-2 focus:ring-black"
                  >
                    <option value="PRIVATE">PRIVATE</option>
                    <option value="PUBLIC">PUBLIC</option>
                  </select>
                </div>

                <div>
                  <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                    Allocated Capital (₹)
                  </label>
                  <input
                    type="number"
                    step="1000"
                    placeholder="Enter Allocated Capital"
                    value={newStratCapital}
                    onChange={(e) => setNewStratCapital(e.target.value)}
                    className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs text-[#111111] focus:outline-none focus:ring-2 focus:ring-black"
                  />
                </div>
              </form>
            </div>

            {/* Footer */}
            <div className="p-4 bg-white border-t border-[#E5E7EB] flex items-center justify-end gap-2.5 shrink-0 box-border">
              <button
                type="button"
                onClick={() => setShowCreateModal(false)}
                className="inline-flex items-center justify-center gap-1.5 h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm transition-all active:scale-[0.98] bg-red-600 hover:bg-red-700 text-white border-red-600 hover:border-red-700"
              >
                Close
              </button>
              <button
                type="submit"
                form="create-group-strat-form"
                disabled={creating}
                className="inline-flex items-center justify-center gap-1.5 h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm transition-all active:scale-[0.98] bg-jettone-orange hover:bg-jettone-orange-hover text-white disabled:opacity-50 border-jettone-orange hover:border-jettone-orange-hover"
              >
                {creating ? "Creating..." : "Create Strategy"}
              </button>
            </div>
          </div>
        </div>,
        document.body
      )}

      {/* ========================================================================= */}
      {/* DRAWER: PLACE ORDER (SLIDES IN FROM RIGHT IN EXISTING JETTONE DESIGN)      */}
      {/* ========================================================================= */}
      {mounted && showOrderModal && createPortal(
        <div className="fixed inset-0 z-[9999] flex justify-end pointer-events-auto">
          <div
            className="fixed inset-0 bg-black/40 backdrop-blur-sm transition-opacity duration-200 animate-in fade-in"
            onClick={() => setShowOrderModal(null)}
            aria-hidden="true"
          />

          <div
            className="relative z-10 w-full max-w-lg h-full bg-white border-l border-[#E5E7EB] shadow-2xl flex flex-col justify-between box-border animate-in slide-in-from-right duration-300 ease-out text-[#111111]"
            role="dialog"
            aria-modal="true"
          >
            {/* Header */}
            <div className="px-6 py-4 border-b border-[#E5E7EB] bg-[#F7F7F7] flex items-center justify-between shrink-0">
              <div className="flex flex-col">
                <h3 className="text-sm font-bold text-[#111111] tracking-tight">
                  Place Order (This will place the order in all child accounts)
                </h3>
                {showOrderModal.name && (
                  <span className="text-[11px] font-semibold text-gray-500">
                    Strategy: {showOrderModal.name}
                  </span>
                )}
              </div>
              <button
                onClick={() => setShowOrderModal(null)}
                className="p-1.5 rounded-lg text-[#9CA3AF] hover:text-[#111111] hover:bg-gray-100 transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            {/* Scrollable Content */}
            <div className="flex-1 min-h-0 overflow-y-auto p-6 space-y-4">
              {orderResult && (
                <div
                  className={`p-3 rounded-[10px] text-xs font-semibold ${
                    orderResult.ok
                      ? "bg-green-50 border border-green-200 text-green-800"
                      : "bg-red-50 border border-red-200 text-red-800"
                  }`}
                >
                  {orderResult.text}
                </div>
              )}

              <form id="place-order-drawer-form" onSubmit={handlePlaceOrder} className="space-y-4">
                {/* 1. Script Type */}
                <div>
                  <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                    Script Type *
                  </label>
                  <select
                    value={orderScriptType}
                    onChange={(e) => {
                      const val = e.target.value as "Equity" | "CE" | "PE" | "Future";
                      setOrderScriptType(val);
                      if (val === "Equity") setOrderSymbol("RELIANCE-EQ (NSE)");
                      else if (val === "CE") setOrderSymbol("NIFTY 24500 CE");
                      else if (val === "PE") setOrderSymbol("NIFTY 24500 PE");
                      else if (val === "Future") setOrderSymbol("NIFTY FUT");
                    }}
                    className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs font-semibold text-[#111111] focus:outline-none focus:ring-2 focus:ring-black cursor-pointer"
                  >
                    <option value="Equity">Equity</option>
                    <option value="CE">CE</option>
                    <option value="PE">PE</option>
                    <option value="Future">Future</option>
                  </select>
                </div>

                {/* 2. Script Search */}
                <div className="relative">
                  <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                    Script *
                  </label>
                  <input
                    type="text"
                    value={orderSymbol}
                    onChange={(e) => {
                      setOrderSymbol(e.target.value);
                      setScriptSearchQuery(e.target.value);
                      setIsScriptDropdownOpen(true);
                    }}
                    onFocus={() => setIsScriptDropdownOpen(true)}
                    placeholder="Search Script"
                    className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs font-semibold text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-black"
                  />

                  {/* Suggestions list */}
                  {isScriptDropdownOpen && scriptSuggestions.length > 0 && (
                    <div className="absolute left-0 right-0 top-full mt-1 max-h-48 overflow-y-auto bg-white border border-[#E5E7EB] rounded-[10px] shadow-xl z-50 divide-y divide-[#F3F4F6]">
                      {scriptSuggestions.map((s, idx) => (
                        <div
                          key={idx}
                          onMouseDown={() => {
                            const formatted = `${s.trading_symbol || s.symbol} (${s.exchange})`;
                            setOrderSymbol(formatted);
                            setOrderExchange(s.exchange);
                            setIsScriptDropdownOpen(false);
                          }}
                          className="px-3.5 py-2 text-xs font-semibold text-[#374151] hover:bg-[#F9FAFB] hover:text-[#111111] cursor-pointer flex items-center justify-between"
                        >
                          <span>{s.trading_symbol || s.symbol}</span>
                          <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-600 font-mono">
                            {s.exchange}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* 3. Product */}
                <div>
                  <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                    Product *
                  </label>
                  <select
                    value={orderProduct}
                    onChange={(e) => setOrderProduct(e.target.value)}
                    className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs font-semibold text-[#111111] focus:outline-none focus:ring-2 focus:ring-black cursor-pointer"
                  >
                    <option value="INTRADAY">INTRADAY (Intraday MIS)</option>
                    <option value="CARRYFORWARD">CARRYFORWARD (NRML / MTF)</option>
                    <option value="DELIVERY">DELIVERY (CNC Longterm)</option>
                    <option value="MARGIN">MARGIN (MTF)</option>
                  </select>
                </div>

                {/* 4. Quantity & 5. Order Type */}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                      Quantity *
                    </label>
                    <input
                      type="number"
                      min="1"
                      step="1"
                      placeholder="Enter Quantity"
                      value={orderQty}
                      onChange={(e) => setOrderQty(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs font-semibold text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-black"
                    />
                  </div>

                  <div>
                    <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                      Order Type *
                    </label>
                    <select
                      value={orderType}
                      onChange={(e) => setOrderType(e.target.value as "Market" | "Limit" | "SL")}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs font-semibold text-[#111111] focus:outline-none focus:ring-2 focus:ring-black cursor-pointer"
                    >
                      <option value="Market">Market</option>
                      <option value="Limit">Limit</option>
                      <option value="SL">SL</option>
                    </select>
                  </div>
                </div>

                {/* Dynamic Price fields */}
                {orderType !== "Market" && (
                  <div className={orderType === "SL" ? "grid grid-cols-1 sm:grid-cols-2 gap-3" : ""}>
                    <div>
                      <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                        Limit Price (₹) *
                      </label>
                      <input
                        type="number"
                        step="0.05"
                        min="0.05"
                        placeholder="Enter Price"
                        value={orderPrice}
                        onChange={(e) => setOrderPrice(e.target.value)}
                        className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs font-semibold text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-black"
                      />
                    </div>

                    {orderType === "SL" && (
                      <div>
                        <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                          Trigger Price (₹) *
                        </label>
                        <input
                          type="number"
                          step="0.05"
                          min="0.05"
                          placeholder="Enter Trigger Price"
                          value={orderTriggerPrice}
                          onChange={(e) => setOrderTriggerPrice(e.target.value)}
                          className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs font-semibold text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-black"
                        />
                      </div>
                    )}
                  </div>
                )}

                {/* 6. BUY / SELL Segmented Switch at the Bottom */}
                <div>
                  <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                    Transaction Side *
                  </label>
                  <div className="inline-flex w-44 p-0.5 bg-[#F3F4F6] rounded-lg border border-[#E5E7EB]">
                    <button
                      type="button"
                      onClick={() => setOrderSide("BUY")}
                      className={`flex-1 py-1.5 rounded-lg text-[11px] font-bold transition-all ${
                        orderSide === "BUY"
                          ? "bg-green-600 text-white shadow-sm"
                          : "text-[#4B5563] hover:text-[#111111]"
                      }`}
                    >
                      BUY
                    </button>
                    <button
                      type="button"
                      onClick={() => setOrderSide("SELL")}
                      className={`flex-1 py-1.5 rounded-lg text-[11px] font-bold transition-all ${
                        orderSide === "SELL"
                          ? "bg-red-600 text-white shadow-sm"
                          : "text-[#4B5563] hover:text-[#111111]"
                      }`}
                    >
                      SELL
                    </button>
                  </div>
                </div>
              </form>
            </div>

            {/* Footer */}
            <div className="p-4 bg-white border-t border-[#E5E7EB] flex items-center justify-end gap-2.5 shrink-0 box-border">
              <button
                type="button"
                onClick={() => setShowOrderModal(null)}
                className="inline-flex items-center justify-center gap-1.5 h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm transition-all active:scale-[0.98] bg-red-600 hover:bg-red-700 text-white border-red-600 hover:border-red-700"
              >
                Close
              </button>
              <button
                type="submit"
                form="place-order-drawer-form"
                disabled={placingOrder}
                className="inline-flex items-center justify-center gap-1.5 h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm transition-all active:scale-[0.98] bg-[#2563EB] hover:bg-[#1D4ED8] text-white disabled:opacity-50 border-[#2563EB] hover:border-[#1D4ED8]"
              >
                {placingOrder ? "Placing..." : "Place Order"}
              </button>
            </div>
          </div>
        </div>,
        document.body
      )}
    </div>
  );
}
