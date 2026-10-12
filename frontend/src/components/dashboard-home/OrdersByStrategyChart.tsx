"use client";

import { useState } from "react";
import type { OrdersByStrategyRow } from "@/lib/dashboard";
import { CardHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/DataStates";

/**
 * "Orders by Strategy" - Chart 2. Real copy/order activity for the selected
 * date range, grouped by strategy - built from Jettone's own `orders`
 * table (the same population Copy Fill Rate uses), which has genuine
 * historical rows, so this chart (unlike Strategy P&L) does honor the
 * date-range selector. Vertical bar chart, hand-written SVG - no charting
 * library, matching StrategyPnlChart.tsx's approach elsewhere on this
 * dashboard. Scales via viewBox, so it stays responsive at any card width.
 */
const WIDTH = 640;
const HEIGHT = 200;
const PAD_X = 16;
const PAD_TOP = 22;
const PAD_BOTTOM = 34;
// Same palette as StrategyPnlChart's per-strategy line colors, so a
// strategy reads as the same "color identity" across both charts.
const COLORS = ["#E88900", "#111111", "#6B7280", "#B45309", "#9CA3AF"];
const MAX_BAR_WIDTH = 52;

function truncateLabel(name: string): string {
  return name.length > 12 ? `${name.slice(0, 11)}…` : name;
}

/** Rounded TOP corners only, flat bottom - bars sit on the baseline, so
 * rounding all four corners (the old plain `rx` rect) looked odd where the
 * bar met the axis line. Radius is clamped to the bar's own half-width/
 * height so a very short (e.g. zero-count) bar never produces an invalid
 * arc. */
function roundedTopBarPath(x: number, y: number, width: number, height: number, radius: number): string {
  const r = Math.max(0, Math.min(radius, width / 2, height));
  if (r === 0) return `M ${x} ${y} h ${width} v ${height} h ${-width} Z`;
  return [
    `M ${x} ${y + height}`,
    `L ${x} ${y + r}`,
    `A ${r} ${r} 0 0 1 ${x + r} ${y}`,
    `L ${x + width - r} ${y}`,
    `A ${r} ${r} 0 0 1 ${x + width} ${y + r}`,
    `L ${x + width} ${y + height}`,
    `Z`,
  ].join(" ");
}

export default function OrdersByStrategyChart({ rows }: { rows: OrdersByStrategyRow[] }) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const maxCount = Math.max(1, ...rows.map((r) => r.count));
  const plotHeight = HEIGHT - PAD_TOP - PAD_BOTTOM;
  const slot = rows.length > 0 ? (WIDTH - PAD_X * 2) / rows.length : 0;
  // Narrower ratio than before (was 0.5) opens up a bit more breathing room
  // between bars - purely spacing, same slot/column layout otherwise.
  const barWidth = Math.min(MAX_BAR_WIDTH, slot * 0.44);

  // Tooltip position is derived from the hovered bar's own geometry (same
  // formulas the map() below uses), not from raw mouse coordinates - this is
  // a discrete bar chart, so the tooltip should snap to the bar it's
  // attached to, not free-float under the cursor. Converted to percentages
  // of the SVG viewBox (matching the group-strategy chart's existing
  // tooltip-positioning pattern), so it stays correctly placed regardless of
  // how wide the responsive viewBox is actually rendered.
  const hoverRow = hoverIdx !== null ? rows[hoverIdx] : null;
  const hoverCxPct = hoverIdx !== null ? ((PAD_X + slot * hoverIdx + slot / 2) / WIDTH) * 100 : 0;
  const hoverBarTopPct =
    hoverIdx !== null && hoverRow
      ? ((HEIGHT - PAD_BOTTOM - (hoverRow.count / maxCount) * plotHeight) / HEIGHT) * 100
      : 0;

  return (
    <div>
      <CardHeader title="Orders by Strategy" showBorder={false} />
      {rows.length === 0 ? (
        <div className="px-5 pb-5">
          <EmptyState title="No copy activity in range" />
        </div>
      ) : (
        <div className="px-5 py-4 relative">
          <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} className="w-full h-[200px]" role="img" aria-label="Orders by strategy">
            <line
              x1={PAD_X}
              x2={WIDTH - PAD_X}
              y1={HEIGHT - PAD_BOTTOM}
              y2={HEIGHT - PAD_BOTTOM}
              stroke="#E5E7EB"
              strokeWidth={1}
            />
            {rows.map((row, i) => {
              const cx = PAD_X + slot * i + slot / 2;
              const barHeight = (row.count / maxCount) * plotHeight;
              const y = HEIGHT - PAD_BOTTOM - barHeight;
              const key = row.strategy_id ?? "unassigned";
              const color = COLORS[i % COLORS.length];
              return (
                <g
                  key={key}
                  onMouseEnter={() => setHoverIdx(i)}
                  onMouseLeave={() => setHoverIdx(null)}
                  style={{ transition: "opacity 150ms ease" }}
                >
                  {/* Native browser tooltip removed in favor of the custom
                      HTML tooltip below - having both would show two
                      overlapping tooltips at once. */}
                  {/* Wide invisible hit target so hover/tooltip isn't limited to the bar's own pixel width. */}
                  <rect x={cx - slot / 2} y={PAD_TOP} width={slot} height={plotHeight} fill="transparent" />
                  <path
                    d={roundedTopBarPath(
                      cx - barWidth / 2,
                      row.count > 0 ? y : HEIGHT - PAD_BOTTOM - 2,
                      barWidth,
                      row.count > 0 ? barHeight : 2,
                      5,
                    )}
                    fill={color}
                    opacity={hoverIdx === i ? 1 : 0.85}
                    style={{ transition: "opacity 150ms ease" }}
                  />
                  <text x={cx} y={y - 6} textAnchor="middle" fontSize={11} fontWeight={700} fill="#111111">
                    {row.count}
                  </text>
                  <text x={cx} y={HEIGHT - PAD_BOTTOM + 16} textAnchor="middle" fontSize={10} fill="#6B7280">
                    {truncateLabel(row.strategy_name)}
                  </text>
                </g>
              );
            })}
          </svg>

          {/* Custom tooltip - white card matching the Strategy P&L chart's
              tooltip exactly (same border color, radius, shadow, clamp
              range and transition timing), just with this chart's own
              Strategy/Orders content. Horizontally clamped so it never
              overflows the card at the first/last bar, and flips below the
              bar top whenever there isn't enough room above it. */}
          {hoverRow && (
            <div
              className="absolute pointer-events-none bg-white border border-[#E5E7EB] rounded-lg shadow-[0_4px_14px_rgba(15,23,42,0.12)] px-2.5 py-1.5 z-30 min-w-[92px] transition-opacity duration-150"
              style={{
                left: `${Math.min(90, Math.max(10, hoverCxPct))}%`,
                top: `${hoverBarTopPct}%`,
                transform:
                  hoverBarTopPct < 28
                    ? "translate(-50%, 12px)"
                    : "translate(-50%, calc(-100% - 12px))",
              }}
            >
              <div className="text-[8px] font-semibold uppercase tracking-wide text-[#9CA3AF]">Strategy</div>
              <div className="text-xs font-bold text-[#111111] truncate max-w-[140px]">
                {hoverRow.strategy_name}
              </div>
              <div className="text-[8px] font-semibold uppercase tracking-wide text-[#9CA3AF] mt-1">Orders</div>
              <div className="text-sm font-bold font-mono text-[#111111] leading-tight">
                {hoverRow.count}
                <span className="text-[9px] font-semibold text-[#6B7280] ml-1">
                  order{hoverRow.count === 1 ? "" : "s"}
                </span>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
