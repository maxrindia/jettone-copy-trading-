"use client";

import { useMemo, useState } from "react";
import { useEngineData } from "@/lib/use-engine-data";
import { ErrorPanel, LoadingPanel } from "@/components/ui/DataStates";
import { Card, PageHeader } from "@/components/ui/Card";
import {
  rangeSummaryLabel,
  type DashboardRealizedTimeline,
  type DashboardSummary,
  type DateRangeKey,
} from "@/lib/dashboard";
import DateRangeSelector from "@/components/dashboard-home/DateRangeSelector";
import PlatformOverview from "@/components/dashboard-home/PlatformOverview";
import TradingPerformance from "@/components/dashboard-home/TradingPerformance";
import StrategyPerformanceTable from "@/components/dashboard-home/StrategyPerformanceTable";
import StrategyPnlChart from "@/components/dashboard-home/StrategyPnlChart";
import OrdersByStrategyChart from "@/components/dashboard-home/OrdersByStrategyChart";

/**
 * Main Dashboard. Every P&L, fill-rate and execution-impact figure here
 * comes from GET /dashboard/summary and GET /dashboard/realized-timeline
 * (backend/app/api/dashboard.py), which in turn read PnlService and
 * Jettone's own `orders` table - nothing on this page computes or sums a
 * money value itself.
 *
 * Row 1 (Platform Overview) is intentionally NOT wired to the date-range
 * selector below - it always reflects current platform state, regardless
 * of the selected range. Only the range-aware sections (Row 2, Strategy
 * Performance, both charts) read `range`.
 */
export default function DashboardPage() {
  const [range, setRange] = useState<DateRangeKey>("today");
  const [customFrom, setCustomFrom] = useState("");
  const [customTo, setCustomTo] = useState("");

  const rangeQuery = useMemo(() => {
    const params = new URLSearchParams({ range });
    if (range === "custom") {
      if (customFrom) params.set("from", customFrom);
      if (customTo) params.set("to", customTo);
    }
    return params.toString();
  }, [range, customFrom, customTo]);

  const summary = useEngineData<DashboardSummary>(`/dashboard/summary?${rangeQuery}`, 15000);
  // Strategy P&L chart now honors the same range as everything else - see
  // StrategyPnlChart.tsx / dashboard.py's get_dashboard_realized_timeline
  // for exactly what it can show for a historical range.
  const timeline = useEngineData<DashboardRealizedTimeline>(`/dashboard/realized-timeline?${rangeQuery}`, 15000);

  const anyLoading = summary.isLoading;
  const anyError = summary.error;

  return (
    <div className="space-y-6 max-w-page 2xl:max-w-page-wide mx-auto pb-16">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <PageHeader title="Dashboard" />
        <DateRangeSelector
          range={range}
          customFrom={customFrom}
          customTo={customTo}
          onChangeRange={setRange}
          onChangeCustom={(from, to) => {
            setCustomFrom(from);
            setCustomTo(to);
          }}
        />
      </div>

      {anyLoading && <LoadingPanel label="Loading dashboard…" />}
      {!anyLoading && anyError && <ErrorPanel message={anyError} onRetry={() => summary.refresh()} />}

      {!anyLoading && !anyError && summary.data && (
        <>
          <PlatformOverview platform={summary.data.platform} />

          <div>
            <p className="text-[11px] font-bold text-[#111111] uppercase tracking-[0.08em] mb-2.5 px-0.5">
              Trading Performance · {rangeSummaryLabel(summary.data.range)}
            </p>
            <TradingPerformance
              pnl={summary.data.pnl}
              range={summary.data.range}
              fill={summary.data.copy_fill}
              edge={summary.data.execution_edge}
            />
          </div>

          <Card>
            <StrategyPerformanceTable strategies={summary.data.strategies} />
          </Card>

          <div className="grid grid-cols-1 [@container_(min-width:56rem)]:grid-cols-2 gap-5">
            <Card>
              <div className="p-5">
                <p className="text-[11px] font-bold text-gray-700 uppercase tracking-[0.08em] mb-3">Strategy P&amp;L</p>
                {timeline.isLoading ? (
                  <LoadingPanel label="Loading chart…" />
                ) : timeline.error ? (
                  <ErrorPanel message={timeline.error} onRetry={() => timeline.refresh()} />
                ) : (
                  <StrategyPnlChart timeline={timeline.data} range={summary.data.range} />
                )}
              </div>
            </Card>
            <Card>
              <OrdersByStrategyChart rows={summary.data.orders_by_strategy} />
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
