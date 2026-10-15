"use client";

import { useState } from "react";
import { Scale } from "lucide-react";
import { useEngineData } from "@/lib/use-engine-data";
import { EmptyState, ErrorPanel, LoadingPanel } from "@/components/ui/DataStates";
import { PageHeader, Card } from "@/components/ui/Card";
import Button from "@/components/ui/Button";
import { ReconciliationRecordItem } from "@/types";
import { formatExpiry } from "@/lib/static-ip";

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

/** "15-Oct-2026 11:41" in IST - the same DD-Mon-YYYY the Demat table uses, plus the time. */
function formatRunTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Kolkata",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(d);
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? "";
  const date = formatExpiry(`${get("year")}-${get("month")}-${get("day")}`) || "—";
  return `${date} ${get("hour")}:${get("minute")}`;
}

// Same header and cell typography as the Demat Accounts table.
const TH = "px-3 py-2.5 font-sans font-bold text-[10px] leading-4 text-[#9CA3AF] uppercase tracking-[0.06em] whitespace-nowrap";

export default function ReconciliationPage() {
  const [showResolved, setShowResolved] = useState(false);
  const path = showResolved
    ? "/reliability/reconciliation/records"
    : "/reliability/reconciliation/records?resolved=false";

  const { data: response, error, isLoading, refresh } = useEngineData<ReconciliationRecordsResponse>(path, 60000);
  const records = response?.data ?? null;
  const [runState, setRunState] = useState<"idle" | "running" | "error">("idle");
  const [runError, setRunError] = useState<string | null>(null);

  const accounts = useEngineData<{ id: string; display_name: string; alice_user_id?: string }[]>("/accounts");
  const accountById = new Map((accounts.data ?? []).map((a) => [a.id, a]));

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
          <table className="w-full text-left text-xs min-w-[820px]">
            <thead>
              <tr className="text-left border-b border-[#F3F4F6]">
                <th className={TH}>Run Time</th>
                <th className={TH}>Account</th>
                <th className={TH}>Symbol</th>
                <th className={`${TH} text-right`}>Expected</th>
                <th className={`${TH} text-right`}>Actual</th>
                <th className={`${TH} text-right`}>Discrepancy</th>
                <th className={TH}>Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#F3F4F6] bg-white">
              {records.map((r) => {
                const account = accountById.get(r.account_id);
                return (
                  <tr key={r.id} className="hover:bg-[#FAFAFB] transition-colors">
                    <td className="px-3 py-3 whitespace-nowrap text-[#4B5563] tabular-nums">{formatRunTime(r.run_at)}</td>
                    <td className="px-3 py-3">
                      <div className="font-bold text-[#111111] truncate">
                        {account?.display_name ?? r.account_id.slice(0, 8)}
                      </div>
                      {account?.alice_user_id && (
                        <div className="text-[11px] font-medium text-[#6B7280] truncate tabular-nums">{account.alice_user_id}</div>
                      )}
                    </td>
                    <td className="px-3 py-3 font-bold text-[#111111]">{r.symbol}</td>
                    <td className="px-3 py-3 text-right font-bold text-[#111111] tabular-nums">{r.expected_qty}</td>
                    <td className="px-3 py-3 text-right font-bold text-[#111111] tabular-nums">{r.actual_qty}</td>
                    <td
                      className={`px-3 py-3 text-right font-bold tabular-nums ${
                        r.discrepancy_qty === 0 ? "text-[#6B7280]" : "text-red-600"
                      }`}
                    >
                      {r.discrepancy_qty > 0 ? `+${r.discrepancy_qty}` : r.discrepancy_qty}
                    </td>
                    <td className="px-3 py-3 whitespace-nowrap">
                      <span
                        className={`px-2 py-0.5 rounded-lg text-[10px] font-bold border ${
                          r.resolved
                            ? "bg-green-50 text-green-700 border-green-200"
                            : "bg-amber-50 text-amber-800 border-amber-200"
                        }`}
                      >
                        {r.resolved ? "Resolved" : "Open mismatch"}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}
