"use client";

import { useState } from "react";
import { Scale } from "lucide-react";
import { useEngineData } from "@/lib/use-engine-data";
import { EmptyState, ErrorPanel, LoadingPanel } from "@/components/ui/DataStates";
import { PageHeader, Card } from "@/components/ui/Card";
import Button from "@/components/ui/Button";
import { ReconciliationRecordItem } from "@/types";

/**
 * GET /reliability/reconciliation/records replies through reliability.py's
 * `ApiResponse` envelope ({success, data, message, error}) - unlike most
 * engine endpoints (e.g. GET /accounts), which return the resource directly.
 * useEngineData doesn't unwrap this, so without pulling `.data` out here,
 * `records` would silently be the envelope object itself: truthy, but with
 * no `.length`, so neither the empty-state nor the table branch below ever
 * matches and the page renders nothing - no error, just a blank list.
 */
interface ReconciliationRecordsResponse {
  success: boolean;
  data: ReconciliationRecordItem[];
  message: string;
  error: string | null;
}

export default function ReconciliationPage() {
  const [showResolved, setShowResolved] = useState(false);
  const path = showResolved
    ? "/reliability/reconciliation/records"
    : "/reliability/reconciliation/records?resolved=false";

  const { data: response, error, isLoading, refresh } = useEngineData<ReconciliationRecordsResponse>(path, 60000);
  const records = response?.data ?? null;
  const [runState, setRunState] = useState<"idle" | "running" | "error">("idle");
  const [runError, setRunError] = useState<string | null>(null);

  const accounts = useEngineData<{ id: string; display_name: string }[]>("/accounts");
  const accountNameById = new Map((accounts.data ?? []).map((a) => [a.id, a.display_name]));

  const runCheck = async () => {
    setRunState("running");
    setRunError(null);
    try {
      const resp = await fetch("/api/v1/reliability/reconciliation/run", { method: "POST" });
      if (!resp.ok) {
        setRunError(`Reconciliation run failed (${resp.status}).`);
        setRunState("error");
        return;
      }
      setRunState("idle");
      refresh();
    } catch {
      setRunError("Network error while triggering reconciliation.");
      setRunState("error");
    }
  };

  return (
    <div className="space-y-6 max-w-page mx-auto">
      <PageHeader
        title="Position Reconciliation"
        actions={
          <>
            <label className="flex items-center gap-1.5 text-xs text-[#6B7280] cursor-pointer select-none">
              <input
                type="checkbox"
                checked={showResolved}
                onChange={(e) => setShowResolved(e.target.checked)}
                className="accent-[#111111]"
              />
              Show resolved
            </label>
            <Button
              size="sm"
              loading={runState === "running"}
              onClick={() => void runCheck()}
            >
              <Scale className="w-3.5 h-3.5" aria-hidden />
              Run Reconciliation Check
            </Button>
          </>
        }
      />

      {runError && (
        <div role="alert" className="p-4 rounded-[10px] bg-red-50 border border-red-200 text-xs font-medium text-red-700">
          {runError}
        </div>
      )}

      {isLoading && <LoadingPanel label="Loading reconciliation records…" />}
      {error && !isLoading && <ErrorPanel message={error} onRetry={refresh} />}

      {!isLoading && !error && records && records.length === 0 && (
        <EmptyState
          icon={<Scale className="w-6 h-6 text-[#D1D5DB]" aria-hidden />}
          title="No position mismatches recorded"
          hint="Records appear when expected and broker-reported positions diverge."
        />
      )}

      {!isLoading && !error && records && records.length > 0 && (
        <Card className="overflow-x-auto">
          <table className="w-full text-left text-[13px] text-[#4B5563] min-w-[820px]">
            <thead className="bg-[#F9FAFB] text-[#6B7280] uppercase text-[10px] tracking-[0.08em] border-b border-[#E5E7EB]">
              <tr>
                <th className="py-3 px-4 font-semibold">Run Time</th>
                <th className="py-3 px-4 font-semibold">Account</th>
                <th className="py-3 px-4 font-semibold">Symbol</th>
                <th className="py-3 px-4 font-semibold text-right">Expected</th>
                <th className="py-3 px-4 font-semibold text-right">Actual</th>
                <th className="py-3 px-4 font-semibold text-right">Discrepancy</th>
                <th className="py-3 px-4 font-semibold">Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#F3F4F6] bg-white">
              {records.map((r) => (
                <tr key={r.id} className="hover:bg-[#F9FAFB] transition-colors">
                  <td className="py-3.5 px-4 font-mono text-xs text-[#6B7280] tabular-nums">
                    {new Date(r.run_at).toLocaleString()}
                  </td>
                  <td className="py-3.5 px-4 font-bold text-[#111111]">
                    {accountNameById.get(r.account_id) ?? r.account_id.slice(0, 8)}
                  </td>
                  <td className="py-3.5 px-4 font-medium text-[#111111]">{r.symbol}</td>
                  <td className="py-3.5 px-4 font-mono font-medium text-[#111111] text-right tabular-nums">{r.expected_qty}</td>
                  <td className="py-3.5 px-4 font-mono font-medium text-[#111111] text-right tabular-nums">{r.actual_qty}</td>
                  <td className={`py-3.5 px-4 font-mono font-bold text-right tabular-nums ${r.discrepancy_qty === 0 ? "text-[#6B7280]" : "text-red-600"}`}>
                    {r.discrepancy_qty > 0 ? `+${r.discrepancy_qty}` : r.discrepancy_qty}
                  </td>
                  <td className="py-3.5 px-4">
                    <span
                      className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-bold border ${
                        r.resolved
                          ? "bg-green-50 text-green-700 border-green-200"
                          : "bg-amber-50 text-amber-800 border-amber-200"
                      }`}
                    >
                      <span className={`w-1.5 h-1.5 rounded-full ${r.resolved ? "bg-green-500" : "bg-amber-500"}`} aria-hidden />
                      {r.resolved ? "RESOLVED" : "OPEN MISMATCH"}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}
