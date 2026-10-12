"use client";

import { formatInr, moneyToneClass } from "@/lib/money";
import {
  strategyFillRateLabel,
  strategyPnlDisplay,
  strategyStatusLabel,
  type StrategiesPayload,
} from "@/lib/dashboard";
import { CardHeader } from "@/components/ui/Card";
import { EmptyState } from "@/components/ui/DataStates";

/**
 * The dashboard's main, strategy-centric section: every real GroupStrategy
 * the backend returns (never fabricated), with the one currently RUNNING
 * (the engine copies at most one strategy at a time - see
 * CopyEnginePipeline._get_eligible_followers_for_master) visually
 * distinguished from strategies that are merely configured.
 */
export default function StrategyPerformanceTable({ strategies }: { strategies: StrategiesPayload }) {
  return (
    <div>
      <CardHeader title="Strategy Performance" />
      {strategies.list.length === 0 ? (
        <div className="px-5 pb-5">
          <EmptyState title="No strategies configured" hint="Create a strategy from Group Strategy to link a master to its followers." />
        </div>
      ) : (
        <>
          {strategies.ambiguous && (
            <p className="mx-5 mt-4 text-[11px] text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-2">
              More than one strategy is eligible for the live master. The engine's tie-break isn't defined, so which
              one actually runs may not match the strategy marked Running below.
            </p>
          )}
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-[10px] font-bold text-[#9CA3AF] uppercase tracking-[0.06em] border-b border-[#F3F4F6]">
                  <th className="px-5 py-2.5 font-bold">Strategy</th>
                  <th className="px-3 py-2.5 font-bold">Status</th>
                  <th className="px-3 py-2.5 font-bold">Master</th>
                  <th className="px-3 py-2.5 font-bold">Followers</th>
                  <th className="px-3 py-2.5 font-bold text-right">P&amp;L</th>
                  <th className="px-3 py-2.5 font-bold text-right">Fill Rate</th>
                  <th className="px-5 py-2.5 font-bold text-right">Execution Impact</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#F3F4F6]">
                {strategies.list.map((row) => {
                  const pnl = strategyPnlDisplay(row);
                  return (
                    <tr key={row.id} className={row.status === "RUNNING" ? "bg-green-50/40" : ""}>
                      <td className="px-5 py-3 font-bold text-[#111111] whitespace-nowrap">{row.name}</td>
                      <td className="px-3 py-3 whitespace-nowrap">
                        {row.status === "RUNNING" ? (
                          <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-bold bg-green-100 text-green-700 border border-green-200">
                            <span className="relative flex w-1.5 h-1.5">
                              <span className="absolute inline-flex h-full w-full rounded-full bg-green-400 opacity-75 animate-ping" />
                              <span className="relative inline-flex w-1.5 h-1.5 rounded-full bg-green-500" />
                            </span>
                            Running
                          </span>
                        ) : row.status === "DISABLED" ? (
                          <span
                            className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-bold bg-gray-100 text-gray-500 border border-gray-200"
                            title={strategyStatusLabel(row)}
                          >
                            Disabled
                          </span>
                        ) : (
                          <span className="text-[#9CA3AF]" title={strategyStatusLabel(row)}>
                            Not running
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-3 font-medium text-[#374151] whitespace-nowrap">{row.master.display_name ?? ""}</td>
                      <td className="px-3 py-3 font-medium text-[#374151] whitespace-nowrap">
                        {row.follower_count} {row.follower_count === 1 ? "follower" : "followers"}
                      </td>
                      <td className="px-3 py-3 text-right whitespace-nowrap">
                        {pnl.value === null ? (
                          <span className="font-medium text-[#9CA3AF]" title={pnl.reason ?? undefined}>Unavailable</span>
                        ) : (
                          <span
                            title={
                              pnl.partialNote ??
                              (pnl.isRealizedOnly ? "Realized only - unrealized has no historical data source" : undefined)
                            }
                          >
                            <span className={`font-sans font-semibold ${moneyToneClass(pnl.value)}`}>{formatInr(pnl.value)}</span>
                            {pnl.isRealizedOnly && (
                              <span className="ml-1 text-[9px] font-bold text-[#9CA3AF] uppercase tracking-wide align-middle">
                                Realized
                              </span>
                            )}
                            {pnl.partialNote && (
                              <span className="ml-1 text-[9px] font-bold text-amber-600 uppercase tracking-wide align-middle">
                                Partial
                              </span>
                            )}
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-3 text-right font-sans font-medium whitespace-nowrap">{strategyFillRateLabel(row)}</td>
                      <td className="px-5 py-3 text-right whitespace-nowrap">
                        <span className={`font-sans font-semibold ${moneyToneClass(row.execution.execution_impact)}`}>
                          {formatInr(row.execution.execution_impact)}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
