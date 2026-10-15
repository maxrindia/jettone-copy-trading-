"use client";

import { ArrowRight } from "lucide-react";
import { formatInr, moneyToneClass } from "@/lib/money";
import type { RealTradingImpact as RealTradingImpactData } from "@/lib/dashboard";
import { CardHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/DataStates";
import Tooltip from "@/components/ui/Tooltip";

/**
 * "Real Trading Impact" - what happened to follower performance compared
 * with the master, for the strategy the engine is actually running right
 * now. Master performance -> copy execution -> follower result, in that
 * order. Copy Execution Impact is an execution-quality measurement, never
 * folded into either P&L figure - kept as its own, clearly separate stat.
 */
export default function RealTradingImpact({ impact }: { impact: RealTradingImpactData | null }) {
  return (
    <div>
      <CardHeader title="Real Trading Impact" description="Master performance → copy execution → follower result" />
      {impact === null ? (
        <div className="px-5 pb-5">
          <EmptyState
            title="No strategy is currently running"
            hint="This section fills in once the engine has a live master actually copying to followers."
          />
        </div>
      ) : (
        <div className="p-5 space-y-4">
          <p className="text-xs font-bold text-[#111111]">{impact.strategy_name}</p>

          <div className="flex items-center gap-3 flex-wrap">
            <Stat label="Master Gross P&L" value={impact.master_pnl} unavailableReason={impact.pnl_unavailable_reason} />
            <ArrowRight className="w-4 h-4 text-[#D1D5DB] shrink-0" aria-hidden />
            <Stat label="Copy Execution Impact" value={impact.copy_execution_impact} note="Execution quality, not P&L" />
            <ArrowRight className="w-4 h-4 text-[#D1D5DB] shrink-0" aria-hidden />
            <Stat label="Follower Gross P&L" value={impact.followers_pnl} unavailableReason={impact.pnl_unavailable_reason} />
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs pt-3 border-t border-[#F3F4F6]">
            <Count label="Copied Orders" value={impact.copied_orders} />
            <Count label="Filled Copies" value={impact.filled_copies} tone="text-green-600" />
            <Count label="Working Copies" value={impact.working_copies} />
            <Count label="Failed / Missed" value={impact.failed_missed_copies} tone={impact.failed_missed_copies > 0 ? "text-red-600" : undefined} />
          </div>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, unavailableReason, note }: { label: string; value: string | null; unavailableReason?: string | null; note?: string }) {
  return (
    <Tooltip content={value === null ? unavailableReason ?? undefined : note} focusable><div className="min-w-[9rem]">
      <p className="text-[10px] text-[#9CA3AF]">{label}</p>
      <p className={`text-sm font-extrabold font-mono ${moneyToneClass(value)}`}>{formatInr(value)}</p>
    </div></Tooltip>
  );
}

function Count({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div>
      <p className="text-[10px] text-[#9CA3AF]">{label}</p>
      <p className={`text-sm font-bold font-mono ${tone ?? "text-[#111111]"}`}>{value}</p>
    </div>
  );
}
