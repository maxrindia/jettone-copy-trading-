"use client";

import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { Landmark, Plus, RefreshCw, LayoutGrid, Table2 } from "lucide-react";
import { extractError, useEngineData } from "@/lib/use-engine-data";
import { EmptyState, ErrorPanel, LoadingPanel } from "@/components/ui/DataStates";
import AddAccountModal from "@/components/demat/AddAccountModal";
import RenewStaticIpDialog from "@/components/demat/RenewStaticIpDialog";
import AccountCard from "@/components/accounts/AccountCard";
import AccountBrokerBadges from "@/components/accounts/AccountBrokerBadges";
import type { ServerBrokerCapabilities } from "@/lib/account-status";
import AccountsTable, { type AccountsTableRow } from "@/components/accounts/AccountsTable";
import { confirmAliceBlueLogin, recordReconnectIntent, setReconnectTarget } from "@/lib/alice-blue-login";
import { reloginRoute, serverBrokerLogin, serverLoginPrompt, UNSUPPORTED_RELOGIN, zerodhaInteractiveLogin } from "@/lib/relogin";
import { useInFlight } from "@/lib/use-in-flight";
import { accountHistoryPath } from "@/lib/account-history";
import { emptyMetrics, type AccountMetrics, type AllMetrics } from "@/lib/account-metrics";
import { accountProxyHost } from "@/lib/account-proxy";

interface CopySettingResponse {
  id: string;
  copy_enabled: boolean;
  fixed_qty: number | null;
  ratio: number | null;
}

interface DematCardItem {
  id: string;
  display_name: string;
  broker?: string | null;
  /** Reported by the engine for every account: see lib/account-status.ts. */
  connection_status?: string | null;
  connection_detail?: string | null;
  credentials_configured?: boolean | null;
  broker_capabilities?: ServerBrokerCapabilities | null;
  alice_user_id: string;
  account_type: "MASTER" | "FOLLOWER";
  /** Current role from the strategy membership; null = in no strategy. `account_type` is only the creation-time label. */
  strategy_role?: "MASTER" | "CHILD" | null;
  is_active: boolean;
  session_valid: boolean;
  is_emergency_stopped: boolean;
  last_authenticated_at: string | null;
  ping_ms?: number | null;
  notes: string | null;
  proxy_expires_on: string | null;
  static_ip_host: string | null;
  /** The public IP assigned to this account's Static IP, as entered by the owner. */
  static_ip_address?: string | null;
  static_ip_status: "ACTIVE" | "EXPIRED" | "UNTRACKED" | null;
  copy_settings: CopySettingResponse | null;
  created_at: string;
  updated_at: string;
}

/** Master/Child + E-Stop pills - identical markup shared by the grid cards
 * and the list-view table so the two views never drift in what they show. */
function AccountBadges({ card }: { card: DematCardItem }) {
  return (
    <>
      <AccountBrokerBadges account={card} />
      <span
        className={`px-2 py-0.5 rounded-lg text-[10px] font-bold border ${
          card.strategy_role === "MASTER"
            ? "bg-orange-50 text-orange-700 border-orange-200"
            : "bg-[#F7F7F7] text-[#374151] border-[#E5E7EB]"
        }`}
      >
        {card.strategy_role === "MASTER" ? "Master" : card.strategy_role === "CHILD" ? "Child" : "Unassigned"}
      </span>
      {card.is_emergency_stopped && (
        <span className="px-2 py-0.5 rounded-lg text-[10px] font-bold bg-red-50 text-red-700 border border-red-200">
          E-Stop
        </span>
      )}
    </>
  );
}

export default function DematPage() {
  const router = useRouter();
  const { data: accounts, error, isLoading, refresh } = useEngineData<DematCardItem[]>(
    "/accounts",
    15000,
  );
  const { data: metricsData, refresh: refreshMetrics } = useEngineData<AllMetrics>(
    "/accounts/metrics",
    15000,
  );
  // The one system-wide whitelisted IP (same for every account) - shown once
  // in the list view's table toolbar, never as a per-row value.
  const { data: staticIp } = useEngineData<{ vps_ip: string }>("/system/static-ip", 60000);

  const [modalOpen, setModalOpen] = useState(false);
  const [editingAccount, setEditingAccount] = useState<DematCardItem | null>(null);
  const [savedNotice, setSavedNotice] = useState<string | null>(null);
  const [reloginError, setReloginError] = useState<string | null>(null);
  const reloginGuard = useInFlight();
  const [renewingAccount, setRenewingAccount] = useState<DematCardItem | null>(null);
  const [viewMode, setViewMode] = useState<"grid" | "list">("grid");

  // Listen for OAuth completion via localStorage (works with new-tab flow, no popup needed)
  useEffect(() => {
    const handleStorage = (e: StorageEvent) => {
      if (e.key === "demat_connected" && e.newValue) {
        localStorage.removeItem("demat_connected");
        refresh();
        refreshMetrics();
      }
    };
    // Also check on focus (in case storage event missed)
    const handleFocus = () => {
      if (localStorage.getItem("demat_connected")) {
        localStorage.removeItem("demat_connected");
        refresh();
        refreshMetrics();
      }
    };
    window.addEventListener("storage", handleStorage);
    window.addEventListener("focus", handleFocus);
    // Legacy popup postMessage support
    const handleMessage = (e: MessageEvent) => {
      if (e.data?.type === "DEMAT_CONNECTED") {
        refresh();
        refreshMetrics();
      }
    };
    window.addEventListener("message", handleMessage);
    return () => {
      window.removeEventListener("storage", handleStorage);
      window.removeEventListener("focus", handleFocus);
      window.removeEventListener("message", handleMessage);
    };
  }, [refresh, refreshMetrics]);

  const getMetrics = (id: string): AccountMetrics =>
    metricsData?.metrics?.[id] ?? emptyMetrics(id);

  // ─── Trading Toggle: PUT is_active to backend ─────────────────────────────
  const handleToggleTrading = async (card: DematCardItem) => {
    try {
      const resp = await fetch(`/api/v1/accounts/${card.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_active: !card.is_active }),
      });
      if (resp.ok) refresh();
    } catch {
      // silently fail
    }
  };

  // ─── Static IP renewal: update the expiry (engine applies it to every account on the same host) ───
  const handleRenewStaticIp = async (card: DematCardItem, date: string) => {
    const resp = await fetch(`/api/v1/accounts/${card.id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ proxy_expires_on: date }),
    });
    if (!resp.ok) {
      let message = `Could not update the expiry (${resp.status}).`;
      try {
        const body = (await resp.json()) as unknown;
        message = extractError(body) ?? message;
      } catch {
        // keep the generic message
      }
      throw new Error(message);
    }
    refresh();
  };

  // ─── Delete: Soft-delete via DELETE endpoint ──────────────────────────────
  const handleDelete = async (id: string) => {
    try {
      await fetch(`/api/v1/accounts/${id}`, { method: "DELETE" });
      refresh();
      refreshMetrics();
    } catch {
      // ignore
    }
  };

  // ─── Sign Out: clears Jettone's own connection status for this account.
  // Does NOT log the account out of Alice Blue's website — that's a
  // cross-origin browser session Jettone has no access to. This only resets
  // session_valid/last_authenticated_at and the cached Redis token, which
  // matters because the "Live" badge trusts that cached state and never
  // re-verifies with the broker on its own. ─────────────────────────────────
  const handleSignOut = async (card: DematCardItem) => {
    if (
      !window.confirm(
        `Sign out ${card.display_name} (${card.alice_user_id}) from Jettone?\n\n` +
          `This clears Jettone's own connection status so it stops showing as Live. ` +
          `It does not log this account out of Alice Blue's own website — Jettone has no access to that browser session.`,
      )
    ) {
      return;
    }
    try {
      const resp = await fetch(`/api/v1/accounts/${card.id}/sign-out`, { method: "POST" });
      if (resp.ok) {
        refresh();
        refreshMetrics();
      }
    } catch {
      // ignore
    }
  };

  // ─── Reconnect: Open Alice Blue Login in new tab (bypasses popup blockers) ──
  const handleReconnect = async (card: DematCardItem) => {
    // The account's OWN broker picks the flow; only Alice Blue accounts go to the Alice Blue OAuth page.
    const route = reloginRoute(card.broker);
    if (route === "unsupported") {
      setReloginError(UNSUPPORTED_RELOGIN);
      return;
    }
    if (route === "server-login") {
      // One login per account at a time: a repeated click while it runs does nothing (not even a second prompt).
      if (reloginGuard.isBusy(card.id)) return;
      if (typeof window !== "undefined" && !window.confirm(serverLoginPrompt(card.display_name, card.broker ?? ""))) return;
      setReloginError(null);
      await reloginGuard.run(card.id, async () => {
        const result = await serverBrokerLogin(card.id);
        if (result.ok) setSavedNotice(`${card.display_name} is logged in.`);
        else setReloginError(`${card.display_name}: ${result.message}`);
        refresh();
        refreshMetrics();
      });
      return;
    }
    if (route === "interactive-login") {
      // Zerodha: Kite's login needs the owner's browser. Open the Kite login URL, then ask for the request_token it redirects with.
      if (reloginGuard.isBusy(card.id)) return;
      setReloginError(null);
      await reloginGuard.run(card.id, async () => {
        const result = await zerodhaInteractiveLogin(
          card.id,
          (url) => { window.open(url, "_blank", "noopener,noreferrer"); },
          () => window.prompt("After signing in to Kite, paste the request_token (or the full redirect URL) here:"),
        );
        if (result.ok) setSavedNotice(`${card.display_name} is logged in.`);
        else if (result.status !== 0 || !result.message.includes("cancelled")) setReloginError(`${card.display_name}: ${result.message}`);
        refresh();refreshMetrics();
      });
      return;
    }
    // Alice Blue (unchanged below).
    // Extract API Key from notes if available, or default to uCKZ00C9th
    let appCode = "uCKZ00C9th";
    if (card.notes && card.notes.includes("APIKey:")) {
      const match = card.notes.match(/APIKey:\s*([^\s,]+)/);
      if (match && match[1]) appCode = match[1];
    }

    if (typeof window !== "undefined") {
      if (!confirmAliceBlueLogin(card.alice_user_id)) return;

      setReconnectTarget(card.id, card.alice_user_id);
      await recordReconnectIntent(card.id);

      const url = `https://ant.aliceblueonline.com/?appcode=${encodeURIComponent(appCode)}&userId=${encodeURIComponent(card.alice_user_id)}`;
      
      // Use programmatic anchor click — browsers never block this (unlike window.open)
      const a = document.createElement("a");
      a.href = url;
      a.target = "_blank";
      a.rel = "noreferrer noopener";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    }
  };

  const activeAccounts = accounts ?? [];

  // Success feedback for "Save" (the dialog closes), cleared after a few seconds.
  useEffect(() => {
    if (!savedNotice) return;
    const t = setTimeout(() => setSavedNotice(null), 4000);
    return () => clearTimeout(t);
  }, [savedNotice]);

  return (
    <div className="space-y-6 max-w-page mx-auto pb-12">
      {savedNotice && (
        <div role="status" className="p-3 rounded-[10px] bg-green-50 border border-green-200 text-xs font-semibold text-green-700">
          {savedNotice}
        </div>
      )}
      {reloginError && (
        <div role="alert" data-testid="relogin-error" className="flex items-start justify-between gap-3 p-3 rounded-[10px] bg-red-50 border border-red-200 text-xs font-semibold text-red-700">
          <span>{reloginError}</span>
          <button type="button" onClick={() => setReloginError(null)} className="shrink-0 underline">Dismiss</button>
        </div>
      )}

      {/* Header Bar */}
      <div className="flex items-center justify-between flex-wrap gap-4">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-[10px] bg-[#111111] flex items-center justify-center text-white shadow-sm">
            <Landmark className="w-5 h-5" />
          </div>
          <div>
            <h1 className="text-xl font-bold text-[#111111] tracking-tight">Demat Accounts</h1>
          </div>
        </div>

        <div className="flex items-center flex-wrap gap-3">
          <button
            onClick={() => setModalOpen(true)}
            className="inline-flex items-center justify-center gap-1.5 h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm transition-all active:scale-[0.98] bg-jettone-orange hover:bg-jettone-orange-hover text-white border-jettone-orange hover:border-jettone-orange-hover"
          >
            <Plus className="w-4 h-4" />
            Add Demat
          </button>

          {/* View Mode Toggle: compact segmented utility control */}
          <div role="group" aria-label="View mode" className="inline-flex items-center gap-0.5 h-8 p-0.5 bg-white rounded-lg border border-[#E5E7EB]">
            <button
              onClick={() => setViewMode("grid")}
              aria-pressed={viewMode === "grid"}
              className={`inline-flex h-6 w-7 items-center justify-center rounded-md transition-colors ${
                viewMode === "grid" ? "bg-[#111111] text-white" : "text-[#6B7280] hover:bg-[#F7F7F7]"
              }`}
              title="Grid View"
              aria-label="Grid view"
            >
              <LayoutGrid className="w-4 h-4" />
            </button>
            <button
              onClick={() => setViewMode("list")}
              aria-pressed={viewMode === "list"}
              className={`inline-flex h-6 w-7 items-center justify-center rounded-md transition-colors ${
                viewMode === "list" ? "bg-[#111111] text-white" : "text-[#6B7280] hover:bg-[#F7F7F7]"
              }`}
              title="Table View"
              aria-label="Table view"
            >
              <Table2 className="w-4 h-4" />
            </button>
          </div>

          <button
            onClick={() => { refresh(); refreshMetrics(); }}
            className="inline-flex items-center justify-center gap-1.5 h-8 px-3 rounded-lg text-xs font-semibold text-[#111111] bg-transparent hover:bg-[#F7F7F7] transition-colors active:scale-[0.98]"
          >
            <RefreshCw className="w-3.5 h-3.5" />
            Refresh All
          </button>
        </div>
      </div>

      {isLoading && <LoadingPanel label="Loading demat accounts..." className="!rounded-[10px]" />}
      {error && !isLoading && <ErrorPanel message={error} onRetry={refresh} />}

      {!isLoading && !error && activeAccounts.length === 0 && (
        <EmptyState className="!rounded-[10px]"
          icon={<Landmark className="w-6 h-6 text-[#D1D5DB]" />}
          title="No Demat Accounts Connected"
          hint="Click '+ Add Demat' above to connect your Alice Blue trading account."
        />
      )}

      {/* Demat Accounts: grid of cards, or a real table when List View is selected */}
      {!isLoading && !error && activeAccounts.length > 0 && (
        viewMode === "grid" ? (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,19.5rem),1fr))] gap-5">
            {activeAccounts.map((card) => (
              <AccountCard
                key={card.id}
                displayName={card.display_name}
                aliceUserId={card.alice_user_id}
                sessionValid={card.session_valid}
                lastAuthenticatedAt={card.last_authenticated_at}
                pingMs={card.ping_ms}
                metrics={getMetrics(card.id)}
                tradingEnabled={card.is_active}
                onToggleTrading={() => handleToggleTrading(card)}
                onViewHistory={() => router.push(accountHistoryPath(card.id))}
                onRelogin={() => handleReconnect(card)}
                reloginBusy={reloginGuard.isBusy(card.id)}
                onEditCredentials={() => {
                  setEditingAccount(card);
                  setModalOpen(true);
                }}
                onSignOut={() => handleSignOut(card)}
                dangerAction={{
                  label: "Delete Account",
                  runningLabel: "Deleting...",
                  confirmMessage: "Delete this account? This action cannot be undone.",
                  onClick: () => handleDelete(card.id),
                }}
                badges={<AccountBadges card={card} />}
                staticIpExpired={
                  // Server-provided status only: no Static IP, or an untracked expiry, is never "expired".
                  card.static_ip_status === "EXPIRED"
                    ? {
                        expiresOn: card.proxy_expires_on,
                        onRenew: () => setRenewingAccount(card),
                        onSetNewIp: () => {
                          setEditingAccount(card);
                          setModalOpen(true);
                        },
                      }
                    : null
                }
              />
            ))}
          </div>
        ) : (
          <AccountsTable
            vpsIp={staticIp?.vps_ip}
            rows={activeAccounts.map((card): AccountsTableRow => ({
              id: card.id,
              displayName: card.display_name,
              aliceUserId: card.alice_user_id,
              role: card.strategy_role ?? null,
              proxyHost: accountProxyHost(card.notes),
              assignedIp: card.static_ip_address ?? null,
              expiresOn: card.proxy_expires_on ?? null,
              staticIpStatus: card.static_ip_status,
              createdAt: card.created_at,
              sessionValid: card.session_valid,
              lastAuthenticatedAt: card.last_authenticated_at,
              pingMs: card.ping_ms,
              metrics: getMetrics(card.id),
              tradingEnabled: card.is_active,
              onToggleTrading: () => handleToggleTrading(card),
              onViewHistory: () => router.push(accountHistoryPath(card.id)),
              onRelogin: () => handleReconnect(card),
              onEditCredentials: () => {
                setEditingAccount(card);
                setModalOpen(true);
              },
              onSignOut: () => handleSignOut(card),
              dangerAction: {
                label: "Delete Account",
                runningLabel: "Deleting...",
                confirmMessage: "Delete this account? This action cannot be undone.",
                onClick: () => handleDelete(card.id),
              },
              badges: <AccountBadges card={card} />,
            }))}
          />
        )
      )}

      {renewingAccount && (
        <RenewStaticIpDialog
          host={renewingAccount.static_ip_host}
          accountName={renewingAccount.display_name}
          initialDate={renewingAccount.proxy_expires_on}
          onClose={() => setRenewingAccount(null)}
          onSave={(date) => handleRenewStaticIp(renewingAccount, date)}
        />
      )}

      {/* Add / Reconnect Demat Modal */}
      <AddAccountModal
        open={modalOpen}
        accountToEdit={editingAccount}
        onClose={() => {
          setModalOpen(false);
          setEditingAccount(null);
        }}
        onCreated={() => {
          setModalOpen(false);
          setEditingAccount(null);
          refresh();
          refreshMetrics();
        }}
        onSaved={() => {
          // Details saved from the Reconnect dialog: refresh the list and confirm.
          refresh();
          refreshMetrics();
          setSavedNotice("Account details saved.");
        }}
      />
    </div>
  );
}
