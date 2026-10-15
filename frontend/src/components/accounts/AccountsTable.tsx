"use client";

import { ReactNode, useMemo, useState } from "react";
import { Wifi, WifiOff, Download, Search, ChevronUp, ChevronDown, ChevronsUpDown } from "lucide-react";
import { openPositionsLabel, pnlHeadline, pnlTooltip, type AccountMetrics } from "@/lib/account-metrics";
import { formatInr, moneyToneClass } from "@/lib/money";
import type { AccountCardDangerAction } from "./AccountCard";
import Tooltip from "@/components/ui/Tooltip";
import { describeAssignedIp, formatExpiryCell } from "@/lib/static-ip";

function formatDate(iso: string | null | undefined): string {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short" });
}

export interface AccountsTableRow {
  id: string;
  displayName: string;
  aliceUserId: string;
  /** Current role in its strategy; null = not in any strategy. */
  role: "MASTER" | "CHILD" | null;
  /** Redacted StaticIP.in proxy host for this specific account (see
   * lib/account-proxy.ts), or null when this account has no dedicated proxy
   * configured and its calls go out through the shared VPS IP instead. */
  proxyHost: string | null;
  /** The public IP recorded for this account's Static IP (accounts.static_ip_address);
   * null when it has not been entered. Never derived from the proxy host. */
  assignedIp?: string | null;
  /** Last valid day of the Static IP (accounts.proxy_expires_on, YYYY-MM-DD); null when not set. */
  expiresOn?: string | null;
  /** ACTIVE / EXPIRED / UNTRACKED from the engine; null when the account has no Static IP. */
  staticIpStatus?: "ACTIVE" | "EXPIRED" | "UNTRACKED" | null;
  /** When this account was connected to Jettone (Account.created_at). */
  createdAt: string;
  sessionValid: boolean;
  lastAuthenticatedAt: string | null | undefined;
  pingMs?: number | null;
  badges: ReactNode;
  metrics: AccountMetrics;
  tradingEnabled: boolean;
  onToggleTrading: () => void | Promise<void>;
  onViewHistory: () => void;
  onRelogin: (() => void | Promise<void>) | null;
  onEditCredentials?: (() => void) | null;
  onSignOut?: (() => void | Promise<void>) | null;
  dangerAction: AccountCardDangerAction;
}

type SortKey = "name" | "ip" | "expiry" | "in_group" | "pnl" | "pos" | "orders";
type SortDir = "asc" | "desc";

function sortValue(row: AccountsTableRow, key: SortKey): number | string {
  const m = row.metrics;
  switch (key) {
    case "name":
      return row.displayName.toLowerCase();
    case "ip":
      // Accounts with a recorded public IP first, then proxies without one,
      // then shared-VPS accounts.
      return row.assignedIp ? `0${row.assignedIp}` : row.proxyHost ? "1" : "2";
    case "expiry":
      // ISO dates sort lexically; accounts with no expiry sort last.
      return row.expiresOn ? `0${row.expiresOn}` : "1";
    case "in_group":
      return m.in_group;
    case "pnl":
      return parseFloat(pnlHeadline(m).value ?? "0") || 0;
    case "pos":
      return m.open_positions_count ?? 0;
    case "orders":
      return m.orders_count;
  }
}

function SortHeader({
  label,
  sortKey,
  activeKey,
  dir,
  onSort,
  align = "left",
}: {
  label: string;
  sortKey: SortKey;
  activeKey: SortKey | null;
  dir: SortDir;
  onSort: (key: SortKey) => void;
  align?: "left" | "right";
}) {
  const isActive = activeKey === sortKey;
  return (
    <button
      onClick={() => onSort(sortKey)}
      className={`inline-flex items-center gap-1 font-bold uppercase tracking-[0.06em] text-[10px] transition-colors ${
        isActive ? "text-[#111111]" : "text-[#9CA3AF] hover:text-[#4B5563]"
      } ${align === "right" ? "flex-row-reverse" : ""}`}
    >
      {label}
      {isActive ? (
        dir === "asc" ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />
      ) : (
        <ChevronsUpDown className="w-3 h-3 opacity-40" />
      )}
    </button>
  );
}

function TradingSwitch({ row }: { row: AccountsTableRow }) {
  const [isToggling, setIsToggling] = useState(false);
  return (
    <Tooltip content={row.staticIpStatus === "EXPIRED" ? "Static IP expired: renew it or set a new IP to trade" : undefined} className="inline-flex [&>button:disabled]:pointer-events-none"><button
      role="switch"
      aria-checked={row.tradingEnabled}
      onClick={async (e) => {
        e.stopPropagation();
        if (isToggling) return;
        setIsToggling(true);
        try {
          await row.onToggleTrading();
        } finally {
          setIsToggling(false);
        }
      }}
      disabled={isToggling || row.staticIpStatus === "EXPIRED"}
      className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${
        row.tradingEnabled ? "bg-[#16A34A]" : "bg-[#EF4444]"
      } ${isToggling || row.staticIpStatus === "EXPIRED" ? "opacity-50 cursor-not-allowed" : ""}`}
    >
      <span
        className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
          row.tradingEnabled ? "translate-x-4" : "translate-x-0"
        }`}
      />
    </button></Tooltip>
  );
}

/** Builds and downloads a CSV of exactly what the table currently shows -
 * same filtered/sorted rows, same real values, nothing recomputed. */
function exportCsv(rows: AccountsTableRow[]) {
  const header = [
    "S.No", "Name", "Broker User ID", "Role", "IP Address", "Expiry Date", "Trading", "Created Date", "Session", "Last Authenticated",
    "In Group", "P&L", "Open Positions", "Orders", "Pending", "Completed", "Rejected", "Cancelled", "Needs Review",
  ];
  const lines = rows.map((row, i) => {
    const m = row.metrics;
    const pnl = pnlHeadline(m);
    return [
      i + 1,
      row.displayName,
      row.aliceUserId,
      row.role === "MASTER" ? "Master" : row.role === "CHILD" ? "Child" : "Unassigned",
      describeAssignedIp(row.assignedIp, row.proxyHost).text,
      formatExpiryCell(row.expiresOn),
      row.tradingEnabled ? "On" : "Off",
      formatDate(row.createdAt),
      row.sessionValid ? "Live" : "Offline",
      formatDate(row.lastAuthenticatedAt),
      m.in_group,
      pnl.value ?? "",
      openPositionsLabel(m),
      m.orders_count,
      m.pending_count,
      m.completed_count,
      m.rejected_count,
      m.cancelled_count,
      m.needs_review_count,
    ]
      .map((v) => `"${String(v).replace(/"/g, '""')}"`)
      .join(",");
  });
  const csv = [header.join(","), ...lines].join("\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `demat-accounts-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

/**
 * A real data table for the Demat Accounts "list" view - same underlying
 * data and actions as AccountCard (the "grid" view), just laid out as rows
 * and columns instead of a stack of cards. Matches the dense financial-table
 * language already established for Strategy Performance
 * (StrategyPerformanceTable.tsx): text-xs body, uppercase muted headers,
 * colored value emphasis, no unnecessary boldness. Sorting/search/export are
 * client-side over the same rows the grid view already has - no new data
 * source, and no column for anything Jettone doesn't actually track (no
 * auto-renew, margin, or per-account expiry - see AccountsTableRow).
 *
 * The IP Address column shows each account's own StaticIP.in proxy host
 * (`row.proxyHost`, from lib/account-proxy.ts - real per-account data, not
 * fabricated). `vpsIp` (GET /system/static-ip) is the fallback for accounts
 * with no dedicated proxy configured, whose calls go out through the one
 * shared VPS IP instead - shown inline for just those rows, not as its own
 * column, since it isn't actually that account's own address.
 */
export default function AccountsTable({ rows, vpsIp }: { rows: AccountsTableRow[]; vpsIp?: string | null }) {
  const [search, setSearch] = useState("");
  const [sortKey, setSortKey] = useState<SortKey | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>("asc");

  const visibleRows = useMemo(() => {
    const q = search.trim().toLowerCase();
    const filtered = q
      ? rows.filter((r) => r.displayName.toLowerCase().includes(q) || r.aliceUserId.toLowerCase().includes(q))
      : rows;
    if (!sortKey) return filtered;
    const dir = sortDir === "asc" ? 1 : -1;
    return [...filtered].sort((a, b) => {
      const va = sortValue(a, sortKey);
      const vb = sortValue(b, sortKey);
      if (typeof va === "string" || typeof vb === "string") return String(va).localeCompare(String(vb)) * dir;
      return (va - vb) * dir;
    });
  }, [rows, search, sortKey, sortDir]);

  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir("asc");
    }
  };

  return (
    <div className="bg-white border border-[#E5E7EB] rounded-[10px] shadow-sm">
      <div className="flex items-center justify-between gap-3 flex-wrap px-4 py-3 border-b border-[#F3F4F6]">
        <button
          onClick={() => exportCsv(visibleRows)}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-[#E5E7EB] text-[#4B5563] hover:text-[#111111] hover:bg-[#F9FAFB] text-[11px] font-semibold transition-colors"
        >
          <Download className="w-3.5 h-3.5" />
          Export
        </button>
        <div className="relative w-full max-w-[220px]">
          <Search className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-[#9CA3AF]" />
          <input
            type="text"
            placeholder="Search..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="w-full pl-8 pr-3 py-1.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-[11px] text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111]"
          />
        </div>
      </div>

      <div className="overflow-x-auto rounded-b-[10px]">
        <table className="w-full text-xs table-fixed">
          <colgroup>
            <col className="w-[4%]" />
            <col className="w-[16%]" />
            <col className="w-[10%]" />
            <col className="w-[7%]" />
            <col className="w-[14%]" />
            <col className="w-[10%]" />
            <col className="w-[8%]" />
            <col className="w-[7%]" />
            <col className="w-[8%]" />
            <col className="w-[6%]" />
            <col className="w-[10%]" />
          </colgroup>
          <thead>
            <tr className="text-left border-b border-[#F3F4F6]">
              <th className="px-4 py-2.5 font-bold text-[10px] text-[#9CA3AF] uppercase tracking-[0.06em]">S.No</th>
              <th className="px-3 py-2.5">
                <SortHeader label="Account" sortKey="name" activeKey={sortKey} dir={sortDir} onSort={handleSort} />
              </th>
              <th className="px-3 py-2.5 font-bold text-[10px] text-[#9CA3AF] uppercase tracking-[0.06em]">Session</th>
              <th className="px-3 py-2.5 font-bold text-[10px] text-[#9CA3AF] uppercase tracking-[0.06em]">Trading</th>
              <th className="px-3 py-2.5">
                <SortHeader label="IP Address" sortKey="ip" activeKey={sortKey} dir={sortDir} onSort={handleSort} />
              </th>
              <th className="px-3 py-2.5">
                <SortHeader label="Expiry Date" sortKey="expiry" activeKey={sortKey} dir={sortDir} onSort={handleSort} />
              </th>
              <th className="px-3 py-2.5 font-bold text-[10px] text-[#9CA3AF] uppercase tracking-[0.06em]">Created Date</th>
              <th className="px-3 py-2.5 text-right">
                <SortHeader label="In Group" sortKey="in_group" activeKey={sortKey} dir={sortDir} onSort={handleSort} align="right" />
              </th>
              <th className="px-3 py-2.5 text-right">
                <SortHeader label="P&L" sortKey="pnl" activeKey={sortKey} dir={sortDir} onSort={handleSort} align="right" />
              </th>
              <th className="px-3 py-2.5 text-right">
                <SortHeader label="POS" sortKey="pos" activeKey={sortKey} dir={sortDir} onSort={handleSort} align="right" />
              </th>
              <th className="px-4 py-2.5 text-right">
                <SortHeader label="Orders" sortKey="orders" activeKey={sortKey} dir={sortDir} onSort={handleSort} align="right" />
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-[#F3F4F6]">
            {visibleRows.map((row, i) => {
              const pnl = pnlHeadline(row.metrics);
              const m = row.metrics;
              return (
                <tr key={row.id} className="hover:bg-[#FAFAFB] transition-colors">
                  <td className="px-4 py-3 text-[#9CA3AF] font-medium">{i + 1}</td>

                  <td className="px-3 py-3 max-w-[220px]">
                    <div className="flex items-center gap-2 min-w-0">
                      <span className="font-bold text-[#111111] truncate" title={row.aliceUserId}>
                        {row.displayName}
                      </span>
                      <div className="flex items-center gap-1.5 shrink-0">{row.badges}</div>
                    </div>
                  </td>

                  <td className="px-3 py-3 whitespace-nowrap">
                    <div className={`flex items-center gap-1 font-semibold ${row.sessionValid ? "text-green-600" : "text-red-500"}`}>
                      {row.sessionValid ? <Wifi className="w-3 h-3" /> : <WifiOff className="w-3 h-3" />}
                      <span>{row.sessionValid ? `Live${row.pingMs ? ` (${row.pingMs}ms)` : ""}` : "Offline"}</span>
                    </div>
                    <div className="text-[#9CA3AF] text-[10px] mt-0.5">{formatDate(row.lastAuthenticatedAt)}</div>
                  </td>

                  <td className="px-3 py-3">
                    <TradingSwitch row={row} />
                  </td>

                  <td className="px-3 py-3 whitespace-nowrap">
                    {(() => {
                      const ip = describeAssignedIp(row.assignedIp, row.proxyHost);
                      if (ip.kind === "shared_vps") {
                        return (
                          <Tooltip content={vpsIp ? `Uses the shared VPS IP: ${vpsIp}` : "No dedicated proxy configured"} focusable>
                            <span className="text-[#9CA3AF]">{ip.text}</span>
                          </Tooltip>
                        );
                      }
                      return (
                        <span className="inline-flex items-center gap-1.5">
                          {ip.kind === "assigned" ? (
                            <span className="font-mono text-[#111111]">{ip.text}</span>
                          ) : (
                            <Tooltip
                              content="No public IP has been entered for this account's Static IP. Edit the account to add it."
                              focusable
                            >
                              <span className="text-[#9CA3AF]">{ip.text}</span>
                            </Tooltip>
                          )}
                          {row.staticIpStatus === "EXPIRED" && (
                            <span className="px-1.5 py-0.5 rounded-lg text-[10px] font-bold bg-red-50 text-red-700 border border-red-200">
                              Expired
                            </span>
                          )}
                        </span>
                      );
                    })()}
                  </td>

                  <td className="px-3 py-3 whitespace-nowrap text-[#4B5563]">
                    {row.expiresOn ? formatExpiryCell(row.expiresOn) : <span className="text-[#9CA3AF]">Not set</span>}
                  </td>

                  <td className="px-3 py-3 whitespace-nowrap text-[#4B5563]">{formatDate(row.createdAt)}</td>

                  <td className="px-3 py-3 text-right font-bold text-[#111111]">{m.in_group}</td>

                  <td className="px-3 py-3 text-right whitespace-nowrap">
                    <Tooltip content={pnlTooltip(m)} focusable>
                      <span className={`font-bold ${moneyToneClass(pnl.value)}`}>{formatInr(pnl.value)}</span>
                    </Tooltip>
                  </td>

                  <td className="px-3 py-3 text-right font-bold text-[#111111]">{openPositionsLabel(m)}</td>

                  <td className="px-4 py-3 text-right whitespace-nowrap">
                    <span className="font-bold text-[#111111]">{m.orders_count}</span>
                  </td>
                </tr>
              );
            })}
            {visibleRows.length === 0 && (
              <tr>
                <td colSpan={10} className="px-4 py-8 text-center text-[#9CA3AF]">
                  No accounts match &ldquo;{search}&rdquo;.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
