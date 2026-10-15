"use client";

import { formatInr, moneyToneClass } from "@/lib/money";
import { edgeItemTooltip, type ExecutionEdge } from "@/lib/dashboard";
import { EmptyState } from "@/components/ui/DataStates";
import { CardHeader } from "@/components/ui/Card";
import Tooltip from "@/components/ui/Tooltip";

/**
 * "Copy Execution Quality" - child price vs master price for the selected
 * range's copied fills. Bars plot each fill's quantity-weighted ₹ impact
 * (the same value summed for the KPI card), not the raw per-share price
 * difference, so a 1,000-share fill reads as 1,000x a 1-share fill at the
 * same per-share edge - never equal weight regardless of size. An
 * execution-quality measurement, never P&L: it is not derived from, and
 * never feeds, any P&L figure on this page.
 */
export default function CopyExecutionChart({
  edge,
  strategyNames,
}: {
  edge: ExecutionEdge;
  /** strategy_id -> name, for the per-fill "Strategy" detail. */
  strategyNames: Record<string, string>;
}) {
  const items = edge.items;
  const maxAbs = Math.max(0.01, ...items.map((i) => Math.abs(Number(i.impact))));

  return (
    <div>
      <CardHeader
        title="Copy Execution Quality"
        description="Quantity-weighted ₹ impact of child vs master execution price, per copied fill"
      />
      {items.length === 0 ? (
        <div className="px-5 pb-5">
          <EmptyState title="No copied fills in range" hint="This fills in as master trades are copied and filled." />
        </div>
      ) : (
        <ul className="divide-y divide-[#F3F4F6] max-h-96 overflow-y-auto">
          {items.map((item, i) => {
            const value = Number(item.impact);
            const pct = Math.min(100, (Math.abs(value) / maxAbs) * 100);
            const strategyName = item.group_strategy_id ? strategyNames[item.group_strategy_id] : undefined;
            return (
              <Tooltip
                key={`${item.symbol}-${item.time}-${i}`}
                as="li"
                content={edgeItemTooltip(item, strategyName)}
                className="px-5 py-2.5 flex items-center gap-3 text-xs"
                focusable
              >
                <div className="w-32 shrink-0">
                  <span className="font-bold text-[#111111]">{item.symbol}</span>{" "}
                  <span className={item.side === "BUY" ? "text-green-600" : "text-red-600"}>{item.side}</span>{" "}
                  <span className="text-[#9CA3AF]">×{item.quantity}</span>
                  {strategyName && <div className="text-[10px] text-[#9CA3AF] truncate">{strategyName}</div>}
                </div>
                <div className="flex-1 flex items-center h-4">
                  <div className="relative w-full h-1.5 bg-[#F3F4F6] rounded-full overflow-hidden">
                    <div
                      className={`absolute top-0 h-full ${value >= 0 ? "left-1/2 bg-green-500" : "right-1/2 bg-red-500"}`}
                      style={{ width: `${pct / 2}%` }}
                    />
                    <div className="absolute top-0 left-1/2 w-px h-full bg-[#D1D5DB]" />
                  </div>
                </div>
                <div className={`w-16 text-right font-mono font-bold ${moneyToneClass(item.impact)}`}>
                  {formatInr(item.impact)}
                </div>
              </Tooltip>
            );
          })}
        </ul>
      )}
    </div>
  );
}
