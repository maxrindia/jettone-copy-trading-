"use client";

import { useState, useEffect, useId, FormEvent } from "react";
import { useParams, useRouter } from "next/navigation";
import { createPortal } from "react-dom";
import { Plus, Search, RotateCw, X, Play, ArrowLeft, Trash2, Check, ChevronDown, UserPlus } from "lucide-react";
import { useEngineData, extractError } from "@/lib/use-engine-data";
import { confirmAliceBlueLogin, recordReconnectIntent, setReconnectTarget } from "@/lib/alice-blue-login";
import { accountHistoryPath } from "@/lib/account-history";
import { emptyMetrics, type AccountMetrics, type AllMetrics } from "@/lib/account-metrics";
import { LoadingPanel } from "@/components/ui/DataStates";
import Button from "@/components/ui/Button";
import AccountCard from "@/components/accounts/AccountCard";
import MasterSwitchDialog from "@/components/strategy/MasterSwitchDialog";
import QuantityPolicyDialog, {
  quantityPolicyLabel,
  type QuantityMode,
  type QuantityPolicy,
} from "@/components/strategy/QuantityPolicyDialog";

interface DematCardItem {
  id: string;
  display_name: string;
  alice_user_id: string;
  account_type: "MASTER" | "FOLLOWER";
  broker?: string;
  is_active: boolean;
  session_valid: boolean;
  is_emergency_stopped: boolean;
  last_authenticated_at: string | null;
  ping_ms?: number | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
  copy_settings?: { quantity_mode: QuantityMode; fixed_qty: number | null } | null;
}

interface MemberAccountInfo {
  id: string;
  display_name: string;
  alice_user_id: string;
  account_type?: string;
  broker?: string;
  is_active: boolean;
  session_valid?: boolean;
}

interface StrategyMemberItem {
  id: string;
  strategy_id: string;
  account_id: string;
  multiplier: number;
  trading_enabled: boolean;
  created_at: string;
  account: MemberAccountInfo;
}

interface BackendStrategyResponse {
  id: string;
  name: string;
  description?: string;
  visibility: "PRIVATE" | "PUBLIC";
  master_account_id?: string | null;
  trading_enabled: boolean;
  place_rejected_enabled: boolean;
  allocated_amount: number;
  max_subscribers: number;
  created_at: string;
  updated_at: string;
  member_count: number;
  master_account?: MemberAccountInfo | null;
  members: StrategyMemberItem[];
}

/**
 * MasterCardPicker — Shows selected master as a pill button.
 * Click the button to open a popover of eligible account cards.
 *
 * Any account can act as this strategy's master (not just account_type ===
 * "MASTER"). An account belongs to one strategy at a time, so accounts that
 * are already in ANOTHER strategy are excluded. A Child of this strategy can
 * be picked: the switch promotes it and removes its Child link. Picking an
 * account only opens the verified switch dialog - it never changes the
 * Master by itself.
 */
function MasterCardPicker({
  accounts,
  excludeAccountIds,
  selectedId,
  onSelect,
}: {
  accounts: DematCardItem[];
  excludeAccountIds: string[];
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const excludeSet = new Set(excludeAccountIds);
  const eligible = accounts.filter((a) => a.id === selectedId || !excludeSet.has(a.id));
  const selected = eligible.find((a) => a.id === selectedId);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className={`group inline-flex h-9 items-center gap-2 rounded-lg border bg-[#F9FAFB] pl-3 pr-2.5 text-xs font-semibold text-[#111111] shadow-sm transition-colors hover:border-[#9CA3AF] focus:outline-none focus-visible:ring-2 focus-visible:ring-jettone-orange/40 ${
          selected ? "border-[#E5E7EB]" : "border-dashed border-[#D1D5DB] text-[#6B7280]"
        }`}
      >
        {selected ? (
          <span className="max-w-[160px] truncate">{selected.display_name}</span>
        ) : (
          <span>Select Master</span>
        )}
        <ChevronDown
          className={`h-3.5 w-3.5 shrink-0 opacity-70 transition-transform duration-150 ${open ? "rotate-180" : ""}`}
          aria-hidden
        />
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div
            role="listbox"
            aria-label="Master account"
            className="absolute left-0 top-full z-50 mt-2 w-64 rounded-xl border border-[#E5E7EB] bg-white p-1.5 shadow-xl animate-in fade-in zoom-in-95 duration-150"
          >
            <p className="px-2.5 pb-1 pt-1.5 text-[11px] font-semibold text-[#9CA3AF]">Master account</p>
            {eligible.length === 0 && (
              <p className="px-2.5 py-2 text-[11px] italic text-[#9CA3AF]">
                No available accounts - every other account already belongs to another strategy.
              </p>
            )}
            {eligible.map((m) => {
              const isSelected = m.id === selectedId;
              return (
                <button
                  key={m.id}
                  type="button"
                  role="option"
                  aria-selected={isSelected}
                  onClick={() => {
                    if (!isSelected) onSelect(m.id);
                    setOpen(false);
                  }}
                  className={`flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors ${
                    isSelected ? "bg-[#F3F4F6]" : "hover:bg-[#F9FAFB]"
                  }`}
                >
                  <span
                    className={`h-2 w-2 shrink-0 rounded-full ${m.session_valid ? "bg-green-500" : "bg-gray-300"}`}
                  />
                  <span className="min-w-0 flex-1 truncate text-xs font-semibold text-[#111111]">
                    {m.display_name}
                  </span>
                  {isSelected && <Check className="h-3.5 w-3.5 shrink-0 text-jettone-orange" aria-hidden />}
                  <span className="shrink-0 text-[10px] text-[#9CA3AF]">{m.session_valid ? "Live" : "Offline"}</span>
                </button>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}

export default function GroupStrategyDetailPage() {
  const params = useParams();
  const router = useRouter();
  const groupId = Array.isArray(params?.groupId) ? params.groupId[0] : (params?.groupId as string);

  // Fetch specific strategy by dynamic ID
  const {
    data: strategy,
    error: stratError,
    isLoading: stratLoading,
    refresh: refreshStrategy,
  } = useEngineData<BackendStrategyResponse>(groupId ? `/strategies/${groupId}` : null, 10000);

  // Fetch Demat accounts (source of truth) & metrics
  const { data: accounts, refresh: refreshAccounts } = useEngineData<DematCardItem[]>("/accounts", 15000);
  const { data: metricsData, refresh: refreshMetrics } = useEngineData<AllMetrics>("/accounts/metrics", 15000);
  // Every strategy, to know which accounts already belong to ANOTHER one (an
  // account can be in one strategy at a time; the engine enforces it too).
  const { data: allStrategies, refresh: refreshAllStrategies } = useEngineData<BackendStrategyResponse[]>(
    "/strategies",
    15000,
  );

  const [mounted, setMounted] = useState(false);
  useEffect(() => {
    setMounted(true);
  }, []);

  // Search & Filter
  const [childSearchTerm, setChildSearchTerm] = useState("");

  // Modals & Drawers
  const [showAddChildModal, setShowAddChildModal] = useState(false);
  const [showOrderModal, setShowOrderModal] = useState(false);

  // Add Child Picker State
  const [addingChildId, setAddingChildId] = useState<string | null>(null);
  const [childError, setChildError] = useState<string | null>(null);

  // Master switch: the account picked in MasterCardPicker, awaiting the
  // verified switch dialog; and the outcome note shown after a switch.
  const [pendingMasterId, setPendingMasterId] = useState<string | null>(null);
  const [masterNotice, setMasterNotice] = useState<string | null>(null);
  const [memberError, setMemberError] = useState<string | null>(null);
  const [quantityTarget, setQuantityTarget] = useState<
    { accountId: string; accountName: string; policy: QuantityPolicy } | null
  >(null);

  // Place Order State
  const [orderScriptType, setOrderScriptType] = useState<"Equity" | "CE" | "PE" | "Future">("Equity");
  const [orderSide, setOrderSide] = useState<"BUY" | "SELL">("BUY");
  const [orderSymbol, setOrderSymbol] = useState("RELIANCE-EQ (NSE)");
  const [orderExchange, setOrderExchange] = useState("NSE");
  const [orderProduct, setOrderProduct] = useState("INTRADAY");
  const [orderQty, setOrderQty] = useState<string>("1");
  const [orderType, setOrderType] = useState<"Market" | "Limit" | "SL">("Market");
  const [orderPrice, setOrderPrice] = useState("");
  const [orderTriggerPrice, setOrderTriggerPrice] = useState("");
  const [placingOrder, setPlacingOrder] = useState(false);
  const [orderResult, setOrderResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [scriptSuggestions, setScriptSuggestions] = useState<Array<{ symbol: string; trading_symbol: string; exchange: string }>>([]);
  const [isScriptDropdownOpen, setIsScriptDropdownOpen] = useState(false);
  const [scriptSearchQuery, setScriptSearchQuery] = useState("");

  // Fetch script suggestions dynamically
  useEffect(() => {
    let active = true;
    const fetchScripts = async () => {
      try {
        const q = encodeURIComponent(scriptSearchQuery.trim());
        const resp = await fetch(`/api/v1/broker/instruments/search?query=${q}&script_type=${orderScriptType}`);
        if (resp.ok && active) {
          const data = await resp.json();
          setScriptSuggestions(data.instruments || []);
        }
      } catch (err) {
        console.error("Failed to fetch script suggestions", err);
      }
    };
    fetchScripts();
    return () => {
      active = false;
    };
  }, [orderScriptType, scriptSearchQuery]);

  const getMetrics = (id: string): AccountMetrics =>
    metricsData?.metrics?.[id] ?? emptyMetrics(id);

  // Reconnect Alice Blue
  const handleReconnect = async (acc: DematCardItem) => {
    let appCode = "uCKZ00C9th";
    if (acc.notes && acc.notes.includes("APIKey:")) {
      const match = acc.notes.match(/APIKey:\s*([^\s,]+)/);
      if (match && match[1]) appCode = match[1];
    }
    if (typeof window !== "undefined") {
      if (!confirmAliceBlueLogin(acc.alice_user_id)) return;

      setReconnectTarget(acc.id, acc.alice_user_id);
      await recordReconnectIntent(acc.id);

      const url = `https://ant.aliceblueonline.com/?appcode=${encodeURIComponent(appCode)}&userId=${encodeURIComponent(acc.alice_user_id)}`;
      const a = document.createElement("a");
      a.href = url;
      a.target = "_blank";
      a.rel = "noreferrer noopener";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    }
  };

  // Handle Strategy Trading Toggle
  const toggleTrading = async () => {
    if (!strategy) return;
    try {
      await fetch(`/api/v1/strategies/${strategy.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trading_enabled: !strategy.trading_enabled }),
      });
      refreshStrategy();
    } catch (err) {
      console.error("Failed to toggle trading", err);
    }
  };

  // Handle Strategy Place Rejected Toggle
  const togglePlaceRejected = async () => {
    if (!strategy) return;
    try {
      await fetch(`/api/v1/strategies/${strategy.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ place_rejected_enabled: !strategy.place_rejected_enabled }),
      });
      refreshStrategy();
    } catch (err) {
      console.error("Failed to toggle place rejected", err);
    }
  };

  // Handle Master change on strategy: opens the verified switch dialog. The
  // Master only changes once the engine's preflight passes and the switch
  // is confirmed there.
  const handleMasterChange = (masterAccountId: string) => {
    if (!strategy || masterAccountId === strategy.master_account_id) return;
    setMasterNotice(null);
    setPendingMasterId(masterAccountId);
  };

  const handleMasterSwitched = (monitorReady: boolean) => {
    const name = (accounts ?? []).find((a) => a.id === pendingMasterId)?.display_name ?? "The new Master";
    setPendingMasterId(null);
    setMasterNotice(
      monitorReady
        ? `${name} is now this strategy's Master. Orders it places from now on are copied to the Children below.`
        : `${name} is now this strategy's Master. The engine is still taking its first look at ${name}'s ` +
            "order book - orders placed in the next few seconds are recorded but not copied.",
    );
    refreshStrategy();
    refreshAllStrategies();
  };

  // Handle Child Trading Toggle
  const toggleChildTrading = async (member: StrategyMemberItem) => {
    if (!strategy) return;
    try {
      await fetch(`/api/v1/strategies/${strategy.id}/members/${member.account_id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trading_enabled: !member.trading_enabled }),
      });
      refreshStrategy();
    } catch (err) {
      console.error("Failed to toggle child trading", err);
    }
  };

  // Handle Remove Child Account - unlinks the account from this strategy
  // only; it never deletes the Demat account itself (that's a separate,
  // genuinely destructive action that stays on Demat Accounts).
  const handleRemoveChild = async (accountId: string) => {
    if (!strategy) return;
    setMemberError(null);
    try {
      const resp = await fetch(`/api/v1/strategies/${strategy.id}/members/${accountId}`, {
        method: "DELETE",
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        setMemberError(extractError(data) ?? "Failed to remove the Child account.");
        return;
      }
      refreshStrategy();
      refreshAllStrategies();
    } catch (err) {
      console.error("Failed to remove child account", err);
      setMemberError("Could not reach the trading engine. The Child account was not removed.");
    }
  };

  // Sign Out: same semantics as Demat Accounts' Sign Out - clears Jettone's
  // own connection status only, never touches Alice Blue's own session.
  const handleSignOut = async (acc: DematCardItem) => {
    if (
      !window.confirm(
        `Sign out ${acc.display_name} (${acc.alice_user_id}) from Jettone?\n\n` +
          `This clears Jettone's own connection status so it stops showing as Live. ` +
          `It does not log this account out of Alice Blue's own website — Jettone has no access to that browser session.`,
      )
    ) {
      return;
    }
    try {
      const resp = await fetch(`/api/v1/accounts/${acc.id}/sign-out`, { method: "POST" });
      if (resp.ok) {
        refreshAccounts();
        refreshMetrics();
      }
    } catch {
      // ignore
    }
  };

  // Handle Delete Strategy
  const handleDeleteStrategy = async () => {
    if (!strategy) return;
    if (!confirm(`Are you sure you want to delete strategy "${strategy.name}"?`)) return;
    try {
      await fetch(`/api/v1/strategies/${strategy.id}`, {
        method: "DELETE",
      });
      router.push("/group-strategy");
    } catch (err) {
      console.error("Failed to delete strategy", err);
    }
  };

  // Handle Add Child Account
  const handleAddChild = async (accountId: string, multiplier: number = 1.0) => {
    if (!strategy) return;
    setAddingChildId(accountId);
    setChildError(null);
    try {
      const resp = await fetch(`/api/v1/strategies/${strategy.id}/members`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          account_id: accountId,
          multiplier: multiplier,
          trading_enabled: true,
        }),
      });
      if (!resp.ok) {
        const data = await resp.json().catch(() => null);
        throw new Error(extractError(data) ?? "Failed to add account");
      }
      refreshStrategy();
      refreshAllStrategies();
      setShowAddChildModal(false);
    } catch (err: unknown) {
      setChildError(err instanceof Error ? err.message : "Failed to add account");
    } finally {
      setAddingChildId(null);
    }
  };

  // Handle Place Order
  const handlePlaceOrder = async (e: FormEvent) => {
    e.preventDefault();
    if (!showOrderModal || !strategy) return;

    if (!orderSymbol.trim()) {
      setOrderResult({ ok: false, text: "Please enter or select a Script / Symbol." });
      return;
    }

    const qty = Number(orderQty);
    if (!orderQty || !Number.isInteger(qty) || qty <= 0) {
      setOrderResult({ ok: false, text: "Quantity must be a positive integer." });
      return;
    }

    if (orderType === "Limit" || orderType === "SL") {
      const p = Number(orderPrice);
      if (!orderPrice || isNaN(p) || p <= 0) {
        setOrderResult({ ok: false, text: "Limit Price must be greater than 0." });
        return;
      }
    }

    if (orderType === "SL") {
      const tp = Number(orderTriggerPrice);
      if (!orderTriggerPrice || isNaN(tp) || tp <= 0) {
        setOrderResult({ ok: false, text: "Trigger Price must be greater than 0 for Stop Loss (SL)." });
        return;
      }
    }

    let resolvedSymbol = orderSymbol.trim();
    let resolvedExchange = orderExchange || "NSE";
    const match = orderSymbol.match(/^([A-Za-z0-9\s\-]+)\s*\((NSE|BSE|NFO|MCX)\)$/i);
    if (match) {
      resolvedSymbol = match[1].trim();
      resolvedExchange = match[2].toUpperCase();
    } else if (orderScriptType === "CE" || orderScriptType === "PE" || orderScriptType === "Future") {
      resolvedExchange = "NFO";
    }

    setPlacingOrder(true);
    setOrderResult(null);

    try {
      const resp = await fetch("/api/v1/broker/manual-order", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          // Placed on THIS strategy's Master and copied to its Children.
          strategy_id: strategy.id,
          symbol: resolvedSymbol.toUpperCase(),
          exchange: resolvedExchange.toUpperCase(),
          side: orderSide.toUpperCase(),
          quantity: qty,
          product_type: orderProduct,
          order_type: orderType.toUpperCase(),
          price: (orderType === "Limit" || orderType === "SL") ? Number(orderPrice) : 0,
          trigger_price: orderType === "SL" ? Number(orderTriggerPrice) : 0,
        }),
      });

      const data = (await resp.json()) as { placed?: boolean; master_order_id?: string; message?: string; detail?: string };
      if (resp.ok && data.placed) {
        setOrderResult({ ok: true, text: `Order executed. Master order ID: ${data.master_order_id}` });
      } else {
        setOrderResult({ ok: false, text: `${extractError(data) ?? "Failed to place order."}` });
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : "Network error placing order";
      setOrderResult({ ok: false, text: `Error: ${msg}` });
    } finally {
      setPlacingOrder(false);
    }
  };

  // Loading State
  if (stratLoading) {
    return (
      <div className="max-w-page mx-auto py-8">
        <LoadingPanel />
      </div>
    );
  }

  // Not Found or Error State
  if (stratError || !strategy) {
    return (
      <div className="max-w-4xl mx-auto py-12 space-y-6">
        <div className="bg-white border border-[#E5E7EB] rounded-[10px] p-8 text-center space-y-4 shadow-sm">
          <h2 className="text-lg font-bold text-[#111111]">Group Strategy Not Found</h2>
          <p className="text-xs text-[#6B7280]">
            The strategy with ID <span className="font-mono font-bold text-[#111111]">{groupId}</span> does not exist or has been removed.
          </p>
          <Button
            variant="primary"
            onClick={() => router.push("/group-strategy")}
            size="sm"
            className="px-3.5"
          >
            <ArrowLeft className="w-4 h-4" />
            Back to Group Strategies
          </Button>
        </div>
      </div>
    );
  }

  const activeMembers = strategy.members ?? [];
  const accountsMap = new Map((accounts ?? []).map((a) => [a.id, a]));

  const filteredMembers = activeMembers.filter((m) => {
    const acc = accountsMap.get(m.account_id) ?? m.account;
    return (
      (acc?.display_name ?? "").toLowerCase().includes(childSearchTerm.toLowerCase()) ||
      (acc?.alice_user_id ?? "").toLowerCase().includes(childSearchTerm.toLowerCase())
    );
  });

  // Accounts that already belong to ANOTHER strategy (as Master or Child):
  // one strategy per account, so they can't join this one until removed.
  const takenByOtherStrategy = new Map<string, string>();
  for (const s of allStrategies ?? []) {
    if (s.id === strategy.id) continue;
    if (s.master_account_id) takenByOtherStrategy.set(s.master_account_id, s.name);
    for (const m of s.members ?? []) takenByOtherStrategy.set(m.account_id, s.name);
  }

  const availableDematAccounts = (accounts ?? []).filter((acc) => {
    if (strategy.master_account_id === acc.id) return false;
    if (takenByOtherStrategy.has(acc.id)) return false;
    const isMember = strategy.members.some((m) => m.account_id === acc.id);
    return !isMember;
  });

  const pendingMaster = pendingMasterId ? accountsMap.get(pendingMasterId) ?? null : null;
  const currentMasterAccount = strategy.master_account_id
    ? accountsMap.get(strategy.master_account_id) ?? strategy.master_account ?? null
    : null;

  const memberCountText =
    activeMembers.length === 1
      ? "1 Linked Child"
      : `${activeMembers.length} Linked Children`;

  return (
    <div className="space-y-6 max-w-page mx-auto pb-16">
      {/* Top Bar: Back, Strategy Name, Master Account Selector, Toggles, Place Order */}
      <div className="bg-white text-[#111111] p-3.5 sm:p-4 rounded-[10px] border border-[#E5E7EB] shadow-sm flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center flex-wrap gap-3">
          {/* Back button to /group-strategy */}
          <button
            onClick={() => router.push("/group-strategy")}
            className="w-8 h-8 rounded-lg bg-[#111111] hover:bg-[#1F2937] text-white flex items-center justify-center transition-transform active:scale-95 shadow-sm"
            title="Back to strategies"
          >
            <ArrowLeft className="w-4 h-4 stroke-[2.5]" />
          </button>

          {/* Strategy Name */}
          <div className="flex items-center gap-2">
            <span className="text-sm sm:text-base font-extrabold text-[#111111]">
              {strategy.name}
            </span>
          </div>

          {/* Master Account Card Picker (top-bar) */}
          <MasterCardPicker
            accounts={accounts ?? []}
            excludeAccountIds={Array.from(takenByOtherStrategy.keys())}
            selectedId={strategy.master_account_id ?? null}
            onSelect={(id) => handleMasterChange(id)}
          />

          {/* Place Rejected Toggle */}
          <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-[#F9FAFB] border border-[#E5E7EB] shadow-sm">
            <span className="text-[11px] font-semibold text-[#374151] flex items-center gap-1">
              Place Rejected
            </span>
            <button
              role="switch"
              aria-checked={strategy.place_rejected_enabled}
              onClick={togglePlaceRejected}
              className={`relative inline-flex h-4 w-7 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${
                strategy.place_rejected_enabled ? "bg-jettone-orange" : "bg-gray-300"
              }`}
            >
              <span
                className={`pointer-events-none inline-block h-3 w-3 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
                  strategy.place_rejected_enabled ? "translate-x-3" : "translate-x-0"
                }`}
              />
            </button>
          </div>

          {/* Strategy Trading Enable Toggle */}
          <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-[#F9FAFB] border border-[#E5E7EB] shadow-sm">
            <span className="text-[11px] font-semibold text-[#374151]">Trading Enabled</span>
            <button
              role="switch"
              aria-checked={strategy.trading_enabled}
              onClick={toggleTrading}
              className={`relative inline-flex h-4 w-7 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${
                strategy.trading_enabled ? "bg-[#16A34A]" : "bg-[#EF4444]"
              }`}
            >
              <span
                className={`pointer-events-none inline-block h-3 w-3 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
                  strategy.trading_enabled ? "translate-x-3" : "translate-x-0"
                }`}
              />
            </button>
          </div>
        </div>

        {/* Right Action: Place Order & Delete */}
        <div className="flex items-center gap-2">
          <button
            onClick={() => {
              setShowOrderModal(true);
              setOrderScriptType("Equity");
              setOrderSymbol("RELIANCE-EQ (NSE)");
              setOrderExchange("NSE");
              setOrderProduct("INTRADAY");
              setOrderSide("BUY");
              setOrderQty("1");
              setOrderType("Market");
              setOrderPrice("");
              setOrderTriggerPrice("");
              setOrderResult(null);
            }}
            className="inline-flex items-center justify-center gap-1.5 h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm transition-all active:scale-[0.98] bg-[#2563EB] hover:bg-[#1D4ED8] text-white border-[#2563EB] hover:border-[#1D4ED8]"
          >
            <Play className="w-3.5 h-3.5 fill-current" />
            Place Order
          </button>
          <button
            onClick={handleDeleteStrategy}
            className="inline-flex h-8 w-8 items-center justify-center rounded-lg bg-red-50 hover:bg-red-100 border border-red-200 text-red-600 transition-colors"
            title="Delete Strategy"
          >
            <Trash2 className="w-4 h-4" />
          </button>
        </div>
      </div>

      {(masterNotice || memberError) && (
        <div className="space-y-2">
          {masterNotice && (
            <div className="flex items-start justify-between gap-3 rounded-[10px] border border-green-200 bg-green-50 p-3">
              <p className="text-xs font-semibold text-green-800">{masterNotice}</p>
              <button
                onClick={() => setMasterNotice(null)}
                className="shrink-0 rounded p-0.5 text-green-700 hover:bg-green-100"
                aria-label="Dismiss"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          )}
          {memberError && (
            <div className="flex items-start justify-between gap-3 rounded-[10px] border border-red-200 bg-red-50 p-3">
              <p className="text-xs font-semibold text-red-700">{memberError}</p>
              <button
                onClick={() => setMemberError(null)}
                className="shrink-0 rounded p-0.5 text-red-700 hover:bg-red-100"
                aria-label="Dismiss"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          )}
        </div>
      )}

      {/* Accounts Header & Filter Bar */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex items-center flex-wrap gap-3">
          <h2 className="text-base font-extrabold text-[#111111]">Accounts</h2>
          <span className="px-2.5 py-0.5 rounded-full bg-gray-100 text-[#374151] text-xs font-bold border border-[#E5E7EB]">
            {memberCountText}
          </span>

          {/* Add Child Action Button */}
          <button
            onClick={() => setShowAddChildModal(true)}
            className="inline-flex items-center justify-center gap-1.5 h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm transition-all active:scale-[0.98] bg-jettone-orange hover:bg-jettone-orange-hover text-white border-jettone-orange hover:border-jettone-orange-hover"
          >
            <Plus className="w-3.5 h-3.5" />
            Add Child Account
          </button>
        </div>

        <div className="flex items-center gap-2">
          <div className="relative">
            <Search className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-[#9CA3AF]" />
            <input
              type="text"
              placeholder="Search accounts..."
              value={childSearchTerm}
              onChange={(e) => setChildSearchTerm(e.target.value)}
              className="pl-8 pr-3 py-1.5 rounded-lg bg-white border border-[#E5E7EB] text-xs text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-[#111111] w-48"
            />
          </div>
          <button
            onClick={() => {
              refreshStrategy();
              refreshAccounts();
              refreshMetrics();
            }}
            className="w-8 h-8 rounded-lg bg-white border border-[#E5E7EB] text-[#4B5563] hover:text-[#111111] hover:bg-gray-50 flex items-center justify-center shadow-sm"
            title="Refresh"
          >
            <RotateCw className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {/* Child Accounts Content: Empty State vs Rich Demat Account Cards Grid */}
      {filteredMembers.length === 0 ? (
        <div className="min-h-[360px] bg-white border border-[#E5E7EB] rounded-[10px] p-12 flex flex-col items-center justify-center text-center shadow-sm relative">
          <div className="relative mb-5">
            <div className="w-24 h-24 rounded-full bg-orange-50 blur-xl absolute inset-0 -translate-y-2" />
            <UserPlus className="w-16 h-16 text-jettone-orange relative z-10" />
          </div>

          <h3 className="text-sm sm:text-base font-bold text-[#111111] max-w-md mb-2">
            No child accounts linked to this strategy.
          </h3>
          <p className="text-xs text-[#6B7280] max-w-sm mb-5">
            Add child accounts from your existing Demat accounts to start copying trades automatically.
          </p>

          <button
            onClick={() => setShowAddChildModal(true)}
            className="inline-flex items-center justify-center gap-1.5 h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm transition-all active:scale-[0.98] bg-jettone-orange hover:bg-jettone-orange-hover text-white border-jettone-orange hover:border-jettone-orange-hover"
          >
            <Plus className="w-4 h-4" />
            Add Child Account
          </button>
        </div>
      ) : (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(min(100%,19.5rem),1fr))] gap-5">
          {filteredMembers.map((member) => {
            const acc = accountsMap.get(member.account_id);
            const displayName = acc?.display_name ?? member.account?.display_name ?? "Child Account";
            const aliceUserId = acc?.alice_user_id ?? member.account?.alice_user_id ?? "";
            const isSessionValid = acc?.session_valid ?? member.account?.session_valid ?? false;
            const pingMs = acc?.ping_ms ?? 40;
            const lastAuth = acc?.last_authenticated_at;
            // How this Child's copy quantity is derived. Unknown until the
            // accounts list has loaded - never guessed.
            const policy: QuantityPolicy | null = acc
              ? {
                  quantity_mode: acc.copy_settings?.quantity_mode ?? null,
                  fixed_qty: acc.copy_settings?.fixed_qty ?? null,
                  multiplier: member.multiplier,
                }
              : null;

            return (
              <AccountCard
                key={member.id}
                displayName={displayName}
                aliceUserId={aliceUserId}
                sessionValid={isSessionValid}
                lastAuthenticatedAt={lastAuth}
                pingMs={pingMs}
                metrics={getMetrics(member.account_id)}
                tradingEnabled={member.trading_enabled}
                onToggleTrading={() => toggleChildTrading(member)}
                onViewHistory={() => router.push(accountHistoryPath(member.account_id))}
                onRelogin={acc ? () => handleReconnect(acc) : null}
                onSignOut={acc ? () => handleSignOut(acc) : null}
                dangerAction={{
                  label: "Remove from Strategy",
                  runningLabel: "Removing...",
                  onClick: () => handleRemoveChild(member.account_id),
                }}
                badges={
                  <>
                    <span className="px-2 py-0.5 rounded-lg text-[10px] font-bold bg-[#F7F7F7] text-[#374151] border border-[#E5E7EB]">
                      Child
                    </span>
                    {policy && (
                      <button
                        type="button"
                        onClick={() =>
                          setQuantityTarget({ accountId: member.account_id, accountName: displayName, policy })
                        }
                        title="Change how this Child's copy quantity is calculated"
                        className="px-2 py-0.5 rounded-lg text-[10px] font-bold bg-[#F7F7F7] text-[#374151] border border-[#E5E7EB] hover:border-[#111111] underline decoration-dotted underline-offset-2"
                      >
                        {quantityPolicyLabel(policy)}
                      </button>
                    )}
                    <span className="font-mono text-[10px] bg-gray-100 text-gray-700 px-1.5 py-0.5 rounded border border-[#E5E7EB]">
                      {aliceUserId}
                    </span>
                  </>
                }
              />
            );
          })}
        </div>
      )}

      {/* ========================================================================= */}
      {/* DRAWER: ADD CHILD ACCOUNT (ACCOUNT PICKER FROM DEMAT ACCOUNTS)            */}
      {/* ========================================================================= */}
      {mounted && showAddChildModal && strategy && createPortal(
        <div className="fixed inset-0 z-[9999] flex justify-end pointer-events-auto">
          <div
            className="fixed inset-0 bg-black/40 backdrop-blur-sm transition-opacity duration-200 animate-in fade-in"
            onClick={() => setShowAddChildModal(false)}
            aria-hidden="true"
          />

          <div
            className="relative z-10 w-full max-w-md h-full bg-white border-l border-[#E5E7EB] shadow-2xl flex flex-col justify-between box-border animate-in slide-in-from-right duration-300 ease-out"
            role="dialog"
            aria-modal="true"
          >
            {/* Header */}
            <div className="px-6 py-4 border-b border-[#E5E7EB] bg-[#F7F7F7] flex items-center justify-between shrink-0">
              <h3 className="text-sm font-bold text-[#111111] tracking-tight">
                Add Child Account to {strategy.name}
              </h3>
              <button
                onClick={() => setShowAddChildModal(false)}
                className="p-1.5 rounded-lg text-[#9CA3AF] hover:text-[#111111] hover:bg-gray-100 transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            {/* Body: List of available Demat Accounts to Pick */}
            <div className="flex-1 min-h-0 overflow-y-auto p-6 space-y-4">
              <p className="text-xs text-[#6B7280]">
                Select an existing Demat Account from your account list to link as a Child account in this strategy.
              </p>

              {childError && (
                <div className="p-3 rounded-[10px] bg-red-50 border border-red-200 text-xs font-semibold text-red-700">
                  {childError}
                </div>
              )}

              {availableDematAccounts.length === 0 ? (
                <div className="text-center py-8 px-4 bg-[#F7F7F7] rounded-[10px] border border-dashed border-[#E5E7EB]">
                  <p className="text-xs font-semibold text-[#6B7280]">
                    No available Demat accounts to add.
                  </p>
                  <p className="text-[11px] text-[#9CA3AF] mt-1">
                    Every account is already in this strategy or another one. An account belongs to one
                    strategy at a time - remove it from the other strategy first.
                  </p>
                </div>
              ) : (
                <div className="space-y-2.5">
                  {availableDematAccounts.map((acc) => (
                    <div
                      key={acc.id}
                      className="p-3.5 bg-white border border-[#E5E7EB] rounded-[10px] flex items-center justify-between gap-3 hover:border-[#111111] transition-colors"
                    >
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <span
                            className={`w-2 h-2 rounded-full shrink-0 ${
                              acc.is_active ? "bg-green-500" : "bg-gray-400"
                            }`}
                          />
                          <h4 className="text-xs font-bold text-[#111111] truncate">
                            {acc.display_name}
                          </h4>
                          <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-600 font-mono">
                            {acc.account_type}
                          </span>
                        </div>
                        <p className="text-[11px] font-mono text-[#6B7280] mt-0.5">
                          {acc.alice_user_id}
                        </p>
                      </div>

                      <button
                        onClick={() => handleAddChild(acc.id, 1.0)}
                        disabled={addingChildId === acc.id}
                        className="inline-flex items-center justify-center gap-1.5 h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm transition-all active:scale-[0.98] bg-jettone-orange hover:bg-jettone-orange-hover text-white shrink-0 disabled:opacity-50 border-jettone-orange hover:border-jettone-orange-hover"
                      >
                        <Plus className="w-3.5 h-3.5" />
                        {addingChildId === acc.id ? "Adding..." : "Add"}
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* Footer */}
            <div className="p-4 bg-white border-t border-[#E5E7EB] flex items-center justify-end shrink-0">
              <button
                type="button"
                onClick={() => setShowAddChildModal(false)}
                className="inline-flex items-center justify-center gap-1.5 h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm transition-all active:scale-[0.98] bg-gray-100 hover:bg-gray-200 text-[#374151] border-[#E5E7EB]"
              >
                Close
              </button>
            </div>
          </div>
        </div>,
        document.body
      )}

      {/* ========================================================================= */}
      {/* DRAWER: PLACE ORDER (SLIDES IN FROM RIGHT IN EXISTING JETTONE DESIGN)      */}
      {/* ========================================================================= */}
      {mounted && showOrderModal && strategy && createPortal(
        <div className="fixed inset-0 z-[9999] flex justify-end pointer-events-auto">
          <div
            className="fixed inset-0 bg-black/40 backdrop-blur-sm transition-opacity duration-200 animate-in fade-in"
            onClick={() => setShowOrderModal(false)}
            aria-hidden="true"
          />

          <div
            className="relative z-10 w-full max-w-lg h-full bg-white border-l border-[#E5E7EB] shadow-2xl flex flex-col justify-between box-border animate-in slide-in-from-right duration-300 ease-out text-[#111111]"
            role="dialog"
            aria-modal="true"
          >
            {/* Header */}
            <div className="px-6 py-4 border-b border-[#E5E7EB] bg-[#F7F7F7] flex items-center justify-between shrink-0">
              <div className="flex flex-col">
                <h3 className="text-sm font-bold text-[#111111] tracking-tight">
                  Place Order (This will place the order in all child accounts)
                </h3>
                <span className="text-[11px] font-semibold text-gray-500">
                  Strategy: {strategy.name}
                </span>
              </div>
              <button
                onClick={() => setShowOrderModal(false)}
                className="p-1.5 rounded-lg text-[#9CA3AF] hover:text-[#111111] hover:bg-gray-100 transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            {/* Scrollable Content */}
            <div className="flex-1 min-h-0 overflow-y-auto p-6 space-y-4">
              {orderResult && (
                <div
                  className={`p-3 rounded-[10px] text-xs font-semibold ${
                    orderResult.ok
                      ? "bg-green-50 border border-green-200 text-green-800"
                      : "bg-red-50 border border-red-200 text-red-800"
                  }`}
                >
                  {orderResult.text}
                </div>
              )}

              <form id="place-order-drawer-form" onSubmit={handlePlaceOrder} className="space-y-4">
                {/* 1. Script Type */}
                <div>
                  <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                    Script Type *
                  </label>
                  <select
                    value={orderScriptType}
                    onChange={(e) => {
                      const val = e.target.value as "Equity" | "CE" | "PE" | "Future";
                      setOrderScriptType(val);
                      if (val === "Equity") setOrderSymbol("RELIANCE-EQ (NSE)");
                      else if (val === "CE") setOrderSymbol("NIFTY 24500 CE");
                      else if (val === "PE") setOrderSymbol("NIFTY 24500 PE");
                      else if (val === "Future") setOrderSymbol("NIFTY FUT");
                    }}
                    className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs font-semibold text-[#111111] focus:outline-none focus:ring-2 focus:ring-black cursor-pointer"
                  >
                    <option value="Equity">Equity</option>
                    <option value="CE">CE</option>
                    <option value="PE">PE</option>
                    <option value="Future">Future</option>
                  </select>
                </div>

                {/* 2. Script Search */}
                <div className="relative">
                  <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                    Script *
                  </label>
                  <input
                    type="text"
                    value={orderSymbol}
                    onChange={(e) => {
                      setOrderSymbol(e.target.value);
                      setScriptSearchQuery(e.target.value);
                      setIsScriptDropdownOpen(true);
                    }}
                    onFocus={() => setIsScriptDropdownOpen(true)}
                    placeholder="Search Script (e.g. YESBANK, RELIANCE, NIFTY)"
                    className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs font-semibold text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-black"
                  />

                  {/* Suggestions list */}
                  {isScriptDropdownOpen && scriptSuggestions.length > 0 && (
                    <div className="absolute left-0 right-0 top-full mt-1 max-h-48 overflow-y-auto bg-white border border-[#E5E7EB] rounded-[10px] shadow-xl z-50 divide-y divide-[#F3F4F6]">
                      {scriptSuggestions.map((s, idx) => (
                        <div
                          key={idx}
                          onMouseDown={() => {
                            const formatted = `${s.trading_symbol || s.symbol} (${s.exchange})`;
                            setOrderSymbol(formatted);
                            setOrderExchange(s.exchange);
                            setIsScriptDropdownOpen(false);
                          }}
                          className="px-3.5 py-2 text-xs font-semibold text-[#374151] hover:bg-[#F9FAFB] hover:text-[#111111] cursor-pointer flex items-center justify-between"
                        >
                          <span>{s.trading_symbol || s.symbol}</span>
                          <span className="text-[10px] px-1.5 py-0.5 rounded bg-gray-100 text-gray-600 font-mono">
                            {s.exchange}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                {/* 3. Product */}
                <div>
                  <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                    Product *
                  </label>
                  <select
                    value={orderProduct}
                    onChange={(e) => setOrderProduct(e.target.value)}
                    className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs font-semibold text-[#111111] focus:outline-none focus:ring-2 focus:ring-black cursor-pointer"
                  >
                    <option value="INTRADAY">INTRADAY (Intraday MIS)</option>
                    <option value="CARRYFORWARD">CARRYFORWARD (NRML / MTF)</option>
                    <option value="DELIVERY">DELIVERY (CNC Longterm)</option>
                    <option value="MARGIN">MARGIN (MTF)</option>
                  </select>
                </div>

                {/* 4. Quantity & 5. Order Type */}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                  <div>
                    <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                      Quantity *
                    </label>
                    <input
                      type="number"
                      min="1"
                      step="1"
                      placeholder="Enter Quantity"
                      value={orderQty}
                      onChange={(e) => setOrderQty(e.target.value)}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs font-semibold text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-black"
                    />
                  </div>

                  <div>
                    <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                      Order Type *
                    </label>
                    <select
                      value={orderType}
                      onChange={(e) => setOrderType(e.target.value as "Market" | "Limit" | "SL")}
                      className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs font-semibold text-[#111111] focus:outline-none focus:ring-2 focus:ring-black cursor-pointer"
                    >
                      <option value="Market">Market</option>
                      <option value="Limit">Limit</option>
                      <option value="SL">SL</option>
                    </select>
                  </div>
                </div>

                {/* Dynamic Price fields */}
                {orderType !== "Market" && (
                  <div className={orderType === "SL" ? "grid grid-cols-1 sm:grid-cols-2 gap-3" : ""}>
                    <div>
                      <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                        Limit Price (₹) *
                      </label>
                      <input
                        type="number"
                        step="0.05"
                        min="0.05"
                        placeholder="Enter Price"
                        value={orderPrice}
                        onChange={(e) => setOrderPrice(e.target.value)}
                        className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs font-semibold text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-black"
                      />
                    </div>

                    {orderType === "SL" && (
                      <div>
                        <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                          Trigger Price (₹) *
                        </label>
                        <input
                          type="number"
                          step="0.05"
                          min="0.05"
                          placeholder="Enter Trigger Price"
                          value={orderTriggerPrice}
                          onChange={(e) => setOrderTriggerPrice(e.target.value)}
                          className="w-full px-3.5 py-2.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-xs font-semibold text-[#111111] placeholder-[#9CA3AF] focus:outline-none focus:ring-2 focus:ring-black"
                        />
                      </div>
                    )}
                  </div>
                )}

                {/* 6. BUY / SELL Segmented Switch at the Bottom */}
                <div>
                  <label className="block text-[11px] font-semibold text-[#4B5563] mb-1.5 uppercase tracking-wider">
                    Transaction Side *
                  </label>
                  <div className="inline-flex w-44 p-0.5 bg-[#F3F4F6] rounded-lg border border-[#E5E7EB]">
                    <button
                      type="button"
                      onClick={() => setOrderSide("BUY")}
                      className={`flex-1 py-1.5 rounded-lg text-[11px] font-bold transition-all ${
                        orderSide === "BUY"
                          ? "bg-green-600 text-white shadow-sm"
                          : "text-[#4B5563] hover:text-[#111111]"
                      }`}
                    >
                      BUY
                    </button>
                    <button
                      type="button"
                      onClick={() => setOrderSide("SELL")}
                      className={`flex-1 py-1.5 rounded-lg text-[11px] font-bold transition-all ${
                        orderSide === "SELL"
                          ? "bg-red-600 text-white shadow-sm"
                          : "text-[#4B5563] hover:text-[#111111]"
                      }`}
                    >
                      SELL
                    </button>
                  </div>
                </div>
              </form>
            </div>

            {/* Footer */}
            <div className="p-4 bg-white border-t border-[#E5E7EB] flex items-center justify-end gap-2.5 shrink-0 box-border">
              <button
                type="button"
                onClick={() => setShowOrderModal(false)}
                className="inline-flex items-center justify-center gap-1.5 h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm transition-all active:scale-[0.98] bg-red-600 hover:bg-red-700 text-white border-red-600 hover:border-red-700"
              >
                Close
              </button>
              <button
                type="submit"
                form="place-order-drawer-form"
                disabled={placingOrder}
                className="inline-flex items-center justify-center gap-1.5 h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm transition-all active:scale-[0.98] bg-[#2563EB] hover:bg-[#1D4ED8] text-white disabled:opacity-50 border-[#2563EB] hover:border-[#1D4ED8]"
              >
                {placingOrder ? "Placing..." : "Place Order"}
              </button>
            </div>
          </div>
        </div>,
        document.body
      )}

      {mounted && quantityTarget && strategy && (
        <QuantityPolicyDialog
          strategyId={strategy.id}
          accountId={quantityTarget.accountId}
          accountName={quantityTarget.accountName}
          current={quantityTarget.policy}
          onClose={() => setQuantityTarget(null)}
          onSaved={() => {
            setQuantityTarget(null);
            refreshStrategy();
            refreshAccounts();
          }}
        />
      )}

      {mounted && pendingMaster && (
        <MasterSwitchDialog
          strategyId={strategy.id}
          strategyName={strategy.name}
          currentMaster={currentMasterAccount}
          newMaster={pendingMaster}
          onClose={() => setPendingMasterId(null)}
          onSwitched={handleMasterSwitched}
        />
      )}
    </div>
  );
}
