"use client";

import { useCallback, useEffect, useState } from "react";
import { extractError, useEngineData } from "@/lib/use-engine-data";
import { ErrorPanel, LoadingPanel } from "@/components/ui/DataStates";
import { PageHeader } from "@/components/ui/Card";
import OptionDropdown from "@/components/ui/OptionDropdown";
import { isUnauthenticated, redirectToSignIn } from "@/lib/session-guard";
import OrdersTable, { ORDER_STATUS_FILTERS, type OrdersTableAccountInfo, type UnifiedOrder } from "@/components/orders/OrdersTable";

type StatusFilter = (typeof ORDER_STATUS_FILTERS)[number];

const COUNT_OPTIONS = [
  { value: "50", label: "Last 50 orders" },
  { value: "100", label: "Last 100 orders" },
  { value: "200", label: "Last 200 orders" },
  { value: "all", label: "All orders" },
] as const;
type CountOption = (typeof COUNT_OPTIONS)[number]["value"];

// GET /trades/orders caps limit at 100 and has no status filter, so the page
// walks newest-first pages until it has `count` matching orders or runs out.
const PAGE_SIZE = 100;
const MAX_PAGES = 50;
const POLL_MS = 5000;

async function fetchLatestOrders(count: number | null, filter: StatusFilter): Promise<UnifiedOrder[]> {
  const matches: UnifiedOrder[] = [];
  for (let page = 0; page < MAX_PAGES; page++) {
    const resp = await fetch(`/api/v1/trades/orders?limit=${PAGE_SIZE}&offset=${page * PAGE_SIZE}`);
    if (isUnauthenticated(resp)) {
      redirectToSignIn();
      throw new Error("Session ended");
    }
    const body = (await resp.json()) as unknown;
    if (!resp.ok) throw new Error(extractError(body) ?? `Request failed (${resp.status})`);
    const rows = body as UnifiedOrder[];
    for (const o of rows) {
      if (filter === "ALL" || o.status.toUpperCase() === filter) matches.push(o);
    }
    if (count !== null && matches.length >= count) return matches.slice(0, count);
    if (rows.length < PAGE_SIZE) break;
  }
  return count === null ? matches : matches.slice(0, count);
}

function useLatestOrders(countOption: CountOption, filter: StatusFilter) {
  const [orders, setOrders] = useState<UnifiedOrder[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    let cancelled = false;
    const count = countOption === "all" ? null : Number(countOption);
    const load = async () => {
      try {
        const rows = await fetchLatestOrders(count, filter);
        if (!cancelled) {
          setOrders(rows);
          setError(null);
        }
      } catch (exc) {
        if (!cancelled) setError(exc instanceof Error ? exc.message : "Network error while contacting the trading engine.");
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    };
    setIsLoading(true);
    void load();
    const interval = setInterval(() => void load(), POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [countOption, filter, tick]);

  return { orders, error, isLoading, refresh };
}

export default function OrdersPage() {
  const [filter, setFilter] = useState<StatusFilter>("ALL");
  const [countOption, setCountOption] = useState<CountOption>("50");

  const { orders, error, isLoading, refresh } = useLatestOrders(countOption, filter);

  const accountsData = useEngineData<(OrdersTableAccountInfo & { id: string })[]>("/accounts");
  const accountMap = new Map((accountsData.data ?? []).map((a) => [a.id, a]));

  return (
    <div className="space-y-6 w-full pb-16">
      <PageHeader
        title="Order Manager"
        description="Unified order lifecycle: Internal UI orders & External broker mobile orders"
      />

      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex gap-1 bg-[#F3F4F6] p-1 rounded-[10px] border border-[#E5E7EB] flex-wrap">
          {ORDER_STATUS_FILTERS.map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-3.5 py-1.5 rounded-lg text-xs font-bold transition-all ${
                filter === f
                  ? "bg-white text-[#111111] shadow-sm"
                  : "text-[#6B7280] hover:text-[#111111]"
              }`}
            >
              {f}
            </button>
          ))}
        </div>
        <OptionDropdown
          value={countOption}
          options={COUNT_OPTIONS}
          onChange={setCountOption}
          ariaLabel="Number of orders to show"
        />
      </div>

      {isLoading && orders === null && <LoadingPanel label="Loading order records..." />}
      {error && <ErrorPanel message={error} onRetry={refresh} />}

      {orders !== null && !error && <OrdersTable orders={orders} accountMap={accountMap} showDate />}
    </div>
  );
}
