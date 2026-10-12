"use client";

import { useMemo, useState } from "react";
import clsx from "clsx";
import { buildPnlSnapshot, type PnlSnapshotBar, type StrategyDailyPnl } from "@/lib/dashboard";
import { formatInr } from "@/lib/money";
import PnlRotatingChart from "@/components/group-strategy/PnlRotatingChart";
import PnlTooltip from "@/components/group-strategy/PnlTooltip";

/**
 * The P&L area of a Group Strategy card: ONE fixed-size frame that rotates,
 * every ~3s, between three views of the same already-loaded data -
 * 7-Day P&L (one small bar per day), Daily P&L and
 * Cumulative P&L (line charts). See PnlRotatingChart for the frame; this file
 * builds the 7-day view; its caption exists only in that view.
 *
 * Everything comes from the engine's per-day P&L (`daily[]`). A day with no
 * verifiable value is drawn as an empty dashed slot - never as a zero bar -
 * and a partial day is drawn hatched, with the plain reason in its tooltip.
 * There is deliberately no permanent "Partial" badge, no axes and no
 * drawdown here.
 */

const WINDOW = 7;
const GREEN = "#16A34A";
const RED = "#EF4444";
const NEUTRAL = "#D1D5DB";
// Light diagonal stripes over a bar = "this day is incomplete".
const HATCH = "repeating-linear-gradient(135deg, rgba(255,255,255,0.7) 0 2px, transparent 2px 4px)";

const PANEL_CLASS =
  "rounded-[10px] border border-[#E5E7EB] bg-gradient-to-b from-white to-[#F9FAFB]";

function formatDay(iso: string): string {
  // Already an IST trading day; format in UTC so the viewer's own timezone
  // can never move it to a neighbouring day.
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-IN", {
    weekday: "short",
    day: "2-digit",
    month: "short",
    timeZone: "UTC",
  });
}

function StatePanel({ title, detail }: { title: string; detail?: string | null }) {
  return (
    <div className={clsx(PANEL_CLASS, "h-[148px] flex flex-col items-center justify-center gap-1 px-4 text-center")}>
      <span className="text-xs font-bold text-[#6B7280]">{title}</span>
      {detail && (
        <span className="text-[10px] text-[#9CA3AF] leading-snug" title={detail}>
          {detail}
        </span>
      )}
    </div>
  );
}

/** Same footprint as the loaded section, so cards don't jump when data arrives. */
export function StrategyPnlSkeleton() {
  return <div className={clsx(PANEL_CLASS, "h-[148px] animate-pulse motion-reduce:animate-none")} aria-hidden />;
}

function barLabel(bar: PnlSnapshotBar): string {
  const day = formatDay(bar.date);
  if (bar.value === null) return `${day}: no verifiable P&L`;
  return `${day}: daily P&L ${formatInr(bar.value)}${bar.status !== "OK" ? " (partial)" : ""}`;
}

export default function StrategyPnlSnapshot({
  daily,
  unavailableReason,
  todayIso,
  startIso,
}: {
  daily: StrategyDailyPnl[];
  /** The engine's reason when no day has a usable value (e.g. no master linked). */
  unavailableReason?: string | null;
  /** The current IST trading day, YYYY-MM-DD. */
  todayIso: string;
  /** The strategy's creation day in IST (YYYY-MM-DD): the line chart starts here. */
  startIso: string;
}) {
  const snap = useMemo(() => buildPnlSnapshot(daily, todayIso, WINDOW), [daily, todayIso]);
  const [hover, setHover] = useState<number | null>(null);

  const hasValue = snap.bars.some((b) => b.value !== null);
  if (!hasValue) {
    if (daily.length === 0 && !unavailableReason) return <StatePanel title="No P&L history yet" />;
    // With days present, say why in plain words (the engine's own message
    // embeds raw reason codes); with none (e.g. no master linked), use its text.
    const plain = Array.from(new Set(snap.bars.flatMap((b) => b.reasons))).slice(0, 2);
    const detail =
      daily.length > 0 && plain.length > 0
        ? `No day has a verifiable P&L: ${plain.join("; ")}`
        : unavailableReason ?? null;
    return <StatePanel title="P&L history unavailable" detail={detail} />;
  }

  // Geometry only (display): bars grow up from a shared zero line.
  const nums = snap.bars.map((b) => (b.value === null ? 0 : Number(b.value)));
  const maxPos = Math.max(0, ...nums);
  const maxNeg = Math.max(0, ...nums.map((n) => -n));
  const total = maxPos + maxNeg;
  const pct = (n: number) => (total > 0 ? (n / total) * 100 : 0);

  const spacers = Math.max(0, WINDOW - snap.bars.length);
  const active = hover !== null ? snap.bars[hover] : null;

  // The 7-day view of the ONE rotating frame: bars + caption. It
  // exists only inside that view. The title and view dots live outside the frame.
  const barsLayer = (
    <div className="flex h-full flex-col px-3 pb-2 pt-2.5">
      {/* One bar per trading day */}
      <div className="relative min-h-0 flex-1">
        <div className="flex h-full gap-1" onMouseLeave={() => setHover(null)}>
          {Array.from({ length: spacers }, (_, i) => (
            <div key={`pad-${i}`} className="flex-1 min-w-0" aria-hidden />
          ))}
          {snap.bars.map((bar, i) => {
            const n = nums[i];
            const partial = bar.value !== null && bar.status !== "OK";
            const isActive = hover === i;
            let mark: React.ReactNode;
            if (bar.value === null) {
              mark = (
                <span
                  className="absolute inset-x-[12%] h-2 rounded-sm border border-dashed border-[#D1D5DB]"
                  style={{ bottom: `${pct(maxNeg)}%` }}
                />
              );
            } else if (n === 0) {
              mark = (
                <span
                  className="absolute inset-x-[12%] h-[2px] rounded-sm"
                  style={{ bottom: `${pct(maxNeg)}%`, backgroundColor: NEUTRAL }}
                />
              );
            } else {
              const positive = n > 0;
              mark = (
                <span
                  className="absolute inset-x-[12%] rounded-sm transition-opacity"
                  style={{
                    ...(positive ? { bottom: `${pct(maxNeg)}%` } : { top: `${pct(maxPos)}%` }),
                    height: `${pct(Math.abs(n))}%`,
                    minHeight: 2,
                    backgroundColor: positive ? GREEN : RED,
                    backgroundImage: partial ? HATCH : undefined,
                    opacity: isActive ? 1 : 0.85,
                  }}
                />
              );
            }
            return (
              <div
                key={bar.date}
                role="img"
                tabIndex={0}
                aria-label={barLabel(bar)}
                data-hover-anchor={isActive ? "" : undefined}
                className="relative flex-1 min-w-0 rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-[#111111]/20"
                onMouseEnter={() => setHover(i)}
                onFocus={() => setHover(i)}
                onBlur={() => setHover(null)}
              >
                {mark}
              </div>
            );
          })}
        </div>
      </div>

      <div className="mt-auto flex flex-wrap items-center justify-between gap-x-2 pt-1.5 text-[10px] text-[#9CA3AF]">
        <span>Last {snap.bars.length} trading {snap.bars.length === 1 ? "day" : "days"}</span>
        <span>Realized · before charges</span>
      </div>
    </div>
  );

  // Rendered by the frame outside its clipped box; FloatingTip keeps it on screen.
  const barsTooltip =
    active && hover !== null ? (
      <PnlTooltip date={formatDay(active.date)} label="Daily P&L" value={active.value} />
    ) : null;

  return (
    <PnlRotatingChart daily={daily} startIso={startIso} barsLayer={barsLayer} barsTooltip={barsTooltip} />
  );
}
