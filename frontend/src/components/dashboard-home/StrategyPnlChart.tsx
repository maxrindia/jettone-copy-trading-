"use client";

import { useId, useMemo, useState } from "react";
import { formatInr, moneyToneClass } from "@/lib/money";
import { buildStrategySeries, formatIstTime, rangeSummaryLabel, type DashboardRealizedTimeline, type DateRangeInfo } from "@/lib/dashboard";
import { EmptyState } from "@/components/ui/DataStates";
import { CardHeader } from "@/components/ui/Card";
import { COLORS, buildAreaPath, buildMonotonePath } from "@/components/dashboard-home/strategy-chart-shared";
import StrategyDailyPnlChart from "@/components/dashboard-home/StrategyDailyPnlChart";

/**
 * "Strategy P&L" - Chart 1, TODAY only: an intraday step line per strategy
 * (its master's and followers' fills combined - see dashboard.py's
 * _combine_strategy_series), from today's own live broker fills. Every other
 * range is drawn by StrategyDailyPnlChart (one point per trading day, from the
 * backend's per-day `daily[]`), because the cumulative curve for a multi-day
 * range is UNAVAILABLE as a whole whenever Today's live data is.
 * Cumulative TOTAL P&L is never offered here: no historical LTP/position
 * snapshot exists to reconstruct unrealized P&L at a past time (see
 * backend/app/services/pnl/historical.py) - this is REALIZED only. Hand-written
 * SVG, no charting library.
 */
const WIDTH = 640;
const HEIGHT = 180;
const PAD_X = 8;
const PAD_TOP = 16;
const PAD_BOTTOM = 24;

export default function StrategyPnlChart({
  timeline,
  range,
}: {
  timeline: DashboardRealizedTimeline | null;
  range: DateRangeInfo;
}) {
  const gradientBaseId = useId();
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const [selected, setSelected] = useState<string>("all");

  const built = useMemo(() => (timeline ? buildStrategySeries(timeline) : null), [timeline]);

  // Any range other than Today is a per-day chart (see StrategyDailyPnlChart).
  if (range && !range.is_today) return <StrategyDailyPnlChart timeline={timeline} range={range} />;

  if (!built || built.series.length === 0) {
    return <EmptyState title="No strategies configured" hint="This chart fills in once a strategy links a master to its followers." />;
  }

  const allSeries = built.series;
  const series = selected === "all" ? allSeries : allSeries.filter((s) => s.strategyId === selected);
  const plottable = series.filter((s) => s.points.length > 0);

  if (plottable.length === 0) {
    return (
      <EmptyState
        title={range?.is_today ? "No fills yet today" : `No fills in ${rangeSummaryLabel(range)}`}
      />
    );
  }

  const { times } = built;
  const values = plottable.flatMap((s) => s.points.map((p) => p.value));
  const maxAbs = Math.max(1, ...values.map((v) => Math.abs(v)));
  const yMax = maxAbs * 1.15;

  const xFor = (i: number) => (times.length <= 1 ? WIDTH / 2 : PAD_X + (i / (times.length - 1)) * (WIDTH - PAD_X * 2));
  const yFor = (v: number) => PAD_TOP + (1 - (v + yMax) / (2 * yMax)) * (HEIGHT - PAD_TOP - PAD_BOTTOM);
  const zeroY = yFor(0);
  const baselineY = HEIGHT - PAD_BOTTOM;

  const handleMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    setHoverIdx(Math.round(ratio * (times.length - 1)));
  };

  // A multi-day range needs the date alongside the time - otherwise points
  // from different days are indistinguishable on the axis/hover label.
  const timeLabel = (iso?: string) =>
    !iso ? "--:--" : range?.is_today ? formatIstTime(iso) : formatIstTime(iso, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

  const hoverTime = hoverIdx !== null ? times[hoverIdx] : times[times.length - 1];
  const hoverLabel = timeLabel(hoverTime);

  // Per-series value/color at the hovered index, for the dots and the
  // custom tooltip - reads the exact same numeric series already driving
  // the line's geometry, nothing new is computed here.
  const hoverRows =
    hoverIdx !== null
      ? plottable
          .filter((s) => s.points[hoverIdx])
          .map((s) => ({
            name: s.name,
            color: COLORS[allSeries.indexOf(s) % COLORS.length],
            value: s.points[hoverIdx].value,
            x: xFor(hoverIdx),
            y: yFor(s.points[hoverIdx].value),
          }))
      : [];
  const hoverXPct = hoverIdx !== null ? (xFor(hoverIdx) / WIDTH) * 100 : 0;
  const hoverTopY = hoverRows.length > 0 ? Math.min(...hoverRows.map((r) => r.y)) : zeroY;
  const hoverYPct = (hoverTopY / HEIGHT) * 100;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5">
          {series.map((s, i) => {
            const rawPoints = timeline?.strategies[s.strategyId]?.points ?? [];
            // The legend text always reads the ORIGINAL backend money string
            // (never the chart-only numeric series) - see buildStrategySeries.
            const moneyStr = s.status === "UNAVAILABLE" ? null : rawPoints.length ? rawPoints[rawPoints.length - 1].cumulative_realized : "0.00";
            return (
              <div key={s.strategyId} className="flex items-center gap-1.5 text-xs">
                <span className="w-2 h-2 rounded-full shrink-0" style={{ background: COLORS[allSeries.indexOf(s) % COLORS.length] }} aria-hidden />
                <span className="font-semibold text-[#111111]">{s.name}</span>
                {s.status === "UNAVAILABLE" ? (
                  <span className="text-[#9CA3AF]">unavailable</span>
                ) : (
                  <span className={`font-mono font-bold ${moneyToneClass(moneyStr)}`}>{formatInr(moneyStr)}</span>
                )}
              </div>
            );
          })}
        </div>

        {allSeries.length > 1 && (
          <select
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
            aria-label="Filter Strategy P&L chart"
            className="h-8 px-2.5 rounded-lg border border-[#E5E7EB] bg-white text-xs font-semibold text-[#111111] focus:outline-none focus:border-[#9CA3AF]"
          >
            <option value="all">All Strategies</option>
            {allSeries.map((s) => (
              <option key={s.strategyId} value={s.strategyId}>
                {s.name}
              </option>
            ))}
          </select>
        )}
      </div>

      <div className="relative">
        <svg
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          className="w-full h-[180px]"
          onMouseMove={handleMove}
          onMouseLeave={() => setHoverIdx(null)}
          role="img"
          aria-label={`Strategy P&L - ${rangeSummaryLabel(range)}`}
        >
          <defs>
            {plottable.map((s) => (
              <linearGradient key={s.strategyId} id={`${gradientBaseId}-${s.strategyId}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={COLORS[allSeries.indexOf(s) % COLORS.length]} stopOpacity="0.16" />
                <stop offset="100%" stopColor={COLORS[allSeries.indexOf(s) % COLORS.length]} stopOpacity="0" />
              </linearGradient>
            ))}
          </defs>

          {series.map((s) => {
            if (s.points.length === 0) return null;
            const pts = s.points.map((p, idx) => ({ x: xFor(idx), y: yFor(p.value) }));
            const color = COLORS[allSeries.indexOf(s) % COLORS.length];
            return (
              <g key={s.strategyId}>
                <path d={buildAreaPath(pts, baselineY)} fill={`url(#${gradientBaseId}-${s.strategyId})`} stroke="none" />
                <path d={buildMonotonePath(pts)} fill="none" stroke={color} strokeWidth={2.5} strokeLinejoin="round" strokeLinecap="round" />
              </g>
            );
          })}

          {hoverIdx !== null && (
            <line
              x1={xFor(hoverIdx)}
              x2={xFor(hoverIdx)}
              y1={PAD_TOP}
              y2={baselineY}
              stroke="#9CA3AF"
              strokeWidth={1}
              style={{ transition: "x1 150ms ease, x2 150ms ease" }}
            />
          )}

          {hoverRows.map((r) => (
            <circle
              key={r.name}
              cx={r.x}
              cy={r.y}
              r={3.5}
              fill={r.color}
              stroke="#fff"
              strokeWidth={1.5}
              style={{ transition: "cx 150ms ease, cy 150ms ease" }}
            />
          ))}
        </svg>

        {/* Custom tooltip - matches the "Orders by Strategy" chart's white
            card (same border color, radius and shadow), clamped
            horizontally and flipped above/below the point so it always
            stays inside the card. */}
        {hoverIdx !== null && hoverRows.length > 0 && (
          <div
            className="absolute pointer-events-none bg-white border border-[#E5E7EB] rounded-lg shadow-[0_4px_14px_rgba(15,23,42,0.12)] px-2.5 py-2 z-30 min-w-[128px] transition-opacity duration-150"
            style={{
              left: `${Math.min(90, Math.max(10, hoverXPct))}%`,
              top: `${hoverYPct}%`,
              transform: hoverYPct < 30 ? "translate(-50%, 12px)" : "translate(-50%, calc(-100% - 12px))",
            }}
          >
            <div className="text-[10px] font-semibold text-[#6B7280] pb-1 mb-1 border-b border-[#F3F4F6]">{hoverLabel}</div>
            <div className="space-y-1">
              {hoverRows.map((r) => (
                <div key={r.name} className="flex items-center justify-between gap-3">
                  <span className="flex items-center gap-1.5 text-[11px] font-semibold text-[#4B5563] truncate max-w-[92px]">
                    <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: r.color }} aria-hidden />
                    {r.name}
                  </span>
                  <span className={`text-[11px] font-bold font-mono ${moneyToneClass(r.value.toFixed(2))}`}>
                    {formatInr(r.value.toFixed(2))}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="flex items-center justify-between text-[10px] text-[#9CA3AF]">
        <span>{timeLabel(times[0])}</span>
        <span className="font-semibold text-[#6B7280]">{hoverLabel} IST</span>
        <span>{timeLabel(times[times.length - 1])}</span>
      </div>
    </div>
  );
}
