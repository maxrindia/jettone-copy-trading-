"use client";

import { useId, useMemo, useState } from "react";
import { CalendarDays } from "lucide-react";
import { formatInr, moneyToneClass } from "@/lib/money";
import {
  buildStrategyDailySeries,
  formatIstDate,
  rangeSummaryLabel,
  type DashboardRealizedTimeline,
  type DateRangeInfo,
} from "@/lib/dashboard";
import { EmptyState } from "@/components/ui/DataStates";
import { COLORS, buildAreaPath, buildMonotonePath } from "@/components/dashboard-home/strategy-chart-shared";

/**
 * "Strategy P&L" for every range except Today: one point per trading day, per
 * strategy, read from the backend's per-day `daily[]` (each day costed on its
 * own). Today keeps the intraday step chart (StrategyPnlChart).
 *
 * A day with no verifiable P&L is a gap in the line - never a zero - and a
 * partial day is drawn as a hollow dot (its value is still the backend's).
 * Hand-written SVG, no charting library.
 */
const WIDTH = 640;
const HEIGHT = 180;
const PAD_X = 16;
const PAD_TOP = 16;
const PAD_BOTTOM = 24;

const dayLabel = (iso: string, withYear = false) =>
  formatIstDate(iso, { weekday: "short", day: "2-digit", month: "short", ...(withYear ? { year: "numeric" } : {}) });

export default function StrategyDailyPnlChart({
  timeline,
  range,
}: {
  timeline: DashboardRealizedTimeline | null;
  range: DateRangeInfo;
}) {
  const gradientBaseId = useId();
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const [selected, setSelected] = useState<string>("all");

  const built = useMemo(() => (timeline ? buildStrategyDailySeries(timeline) : null), [timeline]);

  if (!built || built.series.length === 0) {
    return <EmptyState title="No strategies configured" hint="This chart fills in once a strategy links a master to its followers." />;
  }

  const { days } = built;
  const allSeries = built.series;
  const series = selected === "all" ? allSeries : allSeries.filter((s) => s.strategyId === selected);
  const plottable = series.filter((s) => s.hasValue);

  if (days.length === 0) {
    return <EmptyState title={`No fills in ${rangeSummaryLabel(range)}`} />;
  }
  if (plottable.length === 0) {
    return (
      <EmptyState
        title={`P&L unavailable for ${rangeSummaryLabel(range)}`}
        hint="No trading day in this range has a verifiable P&L."
      />
    );
  }

  const values = plottable.flatMap((s) => s.points.flatMap((p) => (p.value === null ? [] : [p.value])));
  const maxAbs = Math.max(1, ...values.map((v) => Math.abs(v)));
  const yMax = maxAbs * 1.15;

  const xFor = (i: number) => (days.length <= 1 ? WIDTH / 2 : PAD_X + (i / (days.length - 1)) * (WIDTH - PAD_X * 2));
  const yFor = (v: number) => PAD_TOP + (1 - (v + yMax) / (2 * yMax)) * (HEIGHT - PAD_TOP - PAD_BOTTOM);
  const zeroY = yFor(0);
  const baselineY = HEIGHT - PAD_BOTTOM;

  const handleMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    setHoverIdx(days.length <= 1 ? 0 : Math.round(ratio * (days.length - 1)));
  };

  const hoverDay = hoverIdx !== null ? days[hoverIdx] : days[days.length - 1];
  const hoverRows =
    hoverIdx !== null
      ? plottable
          .map((s) => ({ s, p: s.points[hoverIdx] }))
          .filter(({ p }) => p.value !== null)
          .map(({ s, p }) => ({
            name: s.name,
            color: COLORS[allSeries.indexOf(s) % COLORS.length],
            money: p.money as string,
            y: yFor(p.value as number),
          }))
      : [];
  const hoverXPct = hoverIdx !== null ? (xFor(hoverIdx) / WIDTH) * 100 : 0;
  const hoverTopY = hoverRows.length > 0 ? Math.min(...hoverRows.map((r) => r.y)) : zeroY;
  const hoverYPct = (hoverTopY / HEIGHT) * 100;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1.5">
          {series.map((s) => (
            <div key={s.strategyId} className="flex items-center gap-1.5 text-xs">
              <span className="w-2 h-2 rounded-full shrink-0" style={{ background: COLORS[allSeries.indexOf(s) % COLORS.length] }} aria-hidden />
              <span className="font-semibold text-[#111111]">{s.name}</span>
              {!s.hasValue && <span className="text-[#9CA3AF]">unavailable</span>}
            </div>
          ))}
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
          aria-label={`Strategy P&L by day - ${rangeSummaryLabel(range)}`}
        >
          <defs>
            {plottable.map((s) => (
              <linearGradient key={s.strategyId} id={`${gradientBaseId}-${s.strategyId}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={COLORS[allSeries.indexOf(s) % COLORS.length]} stopOpacity="0.16" />
                <stop offset="100%" stopColor={COLORS[allSeries.indexOf(s) % COLORS.length]} stopOpacity="0" />
              </linearGradient>
            ))}
          </defs>

          <line x1={PAD_X} x2={WIDTH - PAD_X} y1={zeroY} y2={zeroY} stroke="#E5E7EB" strokeWidth={1} strokeDasharray="3 4" />

          {series.map((s) => {
            if (!s.hasValue) return null;
            const color = COLORS[allSeries.indexOf(s) % COLORS.length];
            // Consecutive verified days form one line; an unverifiable day breaks it.
            const runs: { x: number; y: number }[][] = [];
            s.points.forEach((p, i) => {
              if (p.value === null) {
                runs.push([]);
                return;
              }
              if (runs.length === 0) runs.push([]);
              runs[runs.length - 1].push({ x: xFor(i), y: yFor(p.value) });
            });
            return (
              <g key={s.strategyId}>
                {runs
                  .filter((r) => r.length > 1)
                  .map((r, i) => (
                    <g key={i}>
                      <path d={buildAreaPath(r, baselineY)} fill={`url(#${gradientBaseId}-${s.strategyId})`} stroke="none" />
                      <path d={buildMonotonePath(r)} fill="none" stroke={color} strokeWidth={2.5} strokeLinejoin="round" strokeLinecap="round" />
                    </g>
                  ))}
                {s.points.map((p, i) =>
                  p.value === null ? null : (
                    <circle
                      key={p.date}
                      cx={xFor(i)}
                      cy={yFor(p.value)}
                      r={hoverIdx === i ? 4.5 : 3}
                      fill={p.status === "OK" ? color : "#FFFFFF"}
                      stroke={color}
                      strokeWidth={p.status === "OK" ? 1.5 : 2}
                    />
                  ),
                )}
              </g>
            );
          })}

          {hoverIdx !== null && <line x1={xFor(hoverIdx)} x2={xFor(hoverIdx)} y1={PAD_TOP} y2={baselineY} stroke="#9CA3AF" strokeWidth={1} />}
        </svg>

        {hoverIdx !== null && (
          <div
            className="absolute pointer-events-none bg-white border border-[#E5E7EB] rounded-lg shadow-[0_4px_14px_rgba(15,23,42,0.12)] px-2.5 py-1.5 z-30 min-w-[128px]"
            style={{
              left: `${Math.min(88, Math.max(12, hoverXPct))}%`,
              top: `${hoverYPct}%`,
              transform: hoverYPct < 30 ? "translate(-50%, 12px)" : "translate(-50%, calc(-100% - 12px))",
            }}
          >
            <div className="flex items-center gap-1 text-[10px] font-semibold text-[#4B5563]">
              <CalendarDays className="h-3 w-3 shrink-0 text-gray-500" aria-hidden />
              <span>{dayLabel(hoverDay)}</span>
            </div>
            <div className="mt-0.5 space-y-0.5">
              {hoverRows.length === 0 ? (
                <div className="flex items-baseline justify-between gap-3">
                  <span className="text-[10px] font-semibold text-[#6B7280]">Daily P&amp;L</span>
                  <span className="font-mono text-[11px] font-bold text-[#9CA3AF]">—</span>
                </div>
              ) : (
                hoverRows.map((r) => (
                  <div key={r.name} className="flex items-center justify-between gap-3">
                    <span className="flex items-center gap-1.5 text-[10px] font-semibold text-[#6B7280] truncate max-w-[92px]">
                      <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: r.color }} aria-hidden />
                      {r.name}
                    </span>
                    <span className={`text-[11px] font-bold font-mono ${moneyToneClass(r.money)}`}>{formatInr(r.money)}</span>
                  </div>
                ))
              )}
            </div>
          </div>
        )}
      </div>

      <div className="flex items-center justify-between text-[10px] text-[#9CA3AF]">
        <span>{dayLabel(days[0])}</span>
        <span className="font-semibold text-[#6B7280]">
          {days.length} trading day{days.length === 1 ? "" : "s"} · daily P&amp;L
        </span>
        <span>{dayLabel(days[days.length - 1])}</span>
      </div>
    </div>
  );
}
