"use client";

import type { CopyFillStats, DashboardPnl, DateRangeInfo, ExecutionEdge } from "@/lib/dashboard";
import {
  CopyFillRateCard,
  ExecutionImpactCard,
  RealizedPnlCard,
  TotalGrossPnlCard,
  UnrealizedPnlCard,
} from "./KpiCards";

/**
 * Row 2: Trading Performance. Copy Fill Rate and Execution Impact honor the
 * selected date range (Jettone's own `orders` table has real historical
 * rows); the three P&L cards are always Today - PnlService has no
 * historical capability yet - and show an explicit unavailable caption
 * rather than reusing today's figure under a different date's label.
 */
export default function TradingPerformance({
  pnl,
  range,
  fill,
  edge,
}: {
  pnl: DashboardPnl;
  range: DateRangeInfo;
  fill: CopyFillStats;
  edge: ExecutionEdge;
}) {
  return (
    <div className="grid grid-cols-1 [@container_(min-width:26rem)]:grid-cols-2 [@container_(min-width:64rem)]:grid-cols-5 gap-3">
      <TotalGrossPnlCard pnl={pnl} range={range} />
      <RealizedPnlCard pnl={pnl} range={range} />
      <UnrealizedPnlCard pnl={pnl} range={range} />
      <CopyFillRateCard fill={fill} />
      <ExecutionImpactCard edge={edge} />
    </div>
  );
}
