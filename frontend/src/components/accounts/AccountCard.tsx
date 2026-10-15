"use client";

import { ReactNode, useEffect, useRef, useState } from "react";
import {
  CalendarDays,
  Wifi,
  WifiOff,
  MoreVertical,
  RefreshCw,
  LogOut,
  ExternalLink,
  Eye,
  Users,
  Wallet,
  Layers,
  ReceiptText,
  Clock,
  CheckCircle2,
  XCircle,
  Ban,
  Trash2,
  AlertTriangle,
} from "lucide-react";
import { formatExpiry } from "@/lib/static-ip";
import { openPositionsLabel, pnlDisplay, pnlHeadline, pnlTooltip, type AccountMetrics } from "@/lib/account-metrics";
import { formatInr, moneyToneClass } from "@/lib/money";
import Tooltip from "@/components/ui/Tooltip";

function formatDate(iso: string | null | undefined): string {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
}

export interface AccountCardDangerAction {
  /** e.g. "Delete Account" on Demat Accounts, "Remove from Strategy" here -
   * the only thing that differs between the two surfaces this card is used
   * on; everything else about the card is identical by design. */
  label: string;
  onClick: () => void | Promise<void>;
  confirmMessage?: string;
  runningLabel?: string;
}

/**
 * The one account card design used by both Demat Accounts and the Group
 * Strategy child list - kept as a single component (rather than two
 * independently-styled copies) specifically so they can never drift apart
 * again. The only intentional difference between the two call sites is the
 * "danger" menu action (delete the account entirely vs. just remove it from
 * this strategy) and the badge row content next to the Trading toggle,
 * both of which are passed in by the caller.
 */
export default function AccountCard({
  displayName,
  aliceUserId,
  sessionValid,
  lastAuthenticatedAt,
  pingMs,
  badges,
  metrics,
  tradingEnabled,
  onToggleTrading,
  onViewHistory,
  onRelogin,
  onEditCredentials,
  onSignOut,
  dangerAction,
  staticIpExpired,
}: {
  displayName: string;
  aliceUserId: string;
  sessionValid: boolean;
  lastAuthenticatedAt: string | null | undefined;
  pingMs?: number | null;
  badges: ReactNode;
  metrics: AccountMetrics;
  tradingEnabled: boolean;
  onToggleTrading: () => void | Promise<void>;
  onViewHistory: () => void;
  /** Footer "Re-login" button - the actual Alice Blue OAuth flow. Null when
   * reconnecting isn't possible yet (e.g. account data still loading). */
  onRelogin: (() => void | Promise<void>) | null;
  /** Menu item "Reconnect Demat Account" - opens the credentials edit modal.
   * A different action from onRelogin. Omitted entirely on surfaces that
   * don't own credential editing (e.g. Group Strategy's child cards). */
  onEditCredentials?: (() => void) | null;
  /** omitted entirely when sign-out doesn't apply on this surface. */
  onSignOut?: (() => void | Promise<void>) | null;
  dangerAction: AccountCardDangerAction;
  /** Set only for an account whose Static IP has expired (server-provided status): the body is blurred and
   * disabled behind an overlay offering to renew the IP or configure a new one. */
  staticIpExpired?: { expiresOn: string | null; onRenew: () => void; onSetNewIp: () => void } | null;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [isToggling, setIsToggling] = useState(false);
  const [isSigningOut, setIsSigningOut] = useState(false);
  const [isRunningDangerAction, setIsRunningDangerAction] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const handleOutsideClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenuOpen(false);
    };
    window.addEventListener("click", handleOutsideClick);
    return () => window.removeEventListener("click", handleOutsideClick);
  }, [menuOpen]);

  const pnl = pnlHeadline(metrics);

  const runToggle = async () => {
    if (isToggling) return;
    setIsToggling(true);
    try {
      await onToggleTrading();
    } finally {
      setIsToggling(false);
    }
  };

  const runSignOut = async () => {
    if (!onSignOut || isSigningOut) return;
    setIsSigningOut(true);
    try {
      await onSignOut();
    } finally {
      setIsSigningOut(false);
      setMenuOpen(false);
    }
  };

  const runDangerAction = async () => {
    if (isRunningDangerAction) return;
    if (dangerAction.confirmMessage && !window.confirm(dangerAction.confirmMessage)) return;
    setIsRunningDangerAction(true);
    try {
      await dangerAction.onClick();
    } finally {
      setIsRunningDangerAction(false);
      setMenuOpen(false);
    }
  };

  const dateNode = (
    <div
      className={`flex items-center gap-1 text-[11px] ${staticIpExpired ? "font-semibold text-[#EF4444]" : "text-[#6B7280]"}`}
    >
      <CalendarDays className="w-3.5 h-3.5 text-gray-500" aria-hidden />
      <span>
        {staticIpExpired
          ? `Expired${staticIpExpired.expiresOn ? ` ${formatExpiry(staticIpExpired.expiresOn)}` : ""}`
          : formatDate(lastAuthenticatedAt)}
      </span>
    </div>
  );

  const statusNode = (
    <div className={`flex items-center gap-1 text-[11px] font-semibold ${sessionValid ? "text-green-600" : "text-red-500"}`}>
      {sessionValid ? <Wifi className="w-3 h-3" /> : <WifiOff className="w-3 h-3" />}
      <span>{sessionValid ? `Live${pingMs ? ` (${pingMs}ms)` : ""}` : "Offline"}</span>
    </div>
  );

  const menuNode = (
    <div className="flex items-center gap-1 pl-1">
      {/* ── 3-Dot Options Dropdown ── */}
      <div className="relative" ref={menuRef}>
        <Tooltip content="Options"><button
          onClick={(e) => {
            e.stopPropagation();
            setMenuOpen((v) => !v);
          }}
          className="p-1 hover:bg-gray-100 rounded-lg hover:text-[#111111] transition-colors" aria-label="Options">
          <MoreVertical className="w-3.5 h-3.5" />
        </button></Tooltip>

        {menuOpen && (
          <div
            className="absolute right-0 mt-1 w-52 bg-white border border-[#E5E7EB] rounded-[10px] shadow-xl z-50 py-1 text-xs font-semibold animate-in fade-in zoom-in-95 duration-150"
            onClick={(e) => e.stopPropagation()}
          >
            {onEditCredentials && (
              <button
                onClick={() => {
                  onEditCredentials();
                  setMenuOpen(false);
                }}
                className="w-full text-left px-3.5 py-2 hover:bg-[#F9FAFB] flex items-center gap-2 text-[#111111] transition-colors"
              >
                <RefreshCw className="w-3.5 h-3.5 text-jettone-orange" />
                <span>Reconnect Demat Account</span>
              </button>
            )}
            {onSignOut && (
              <button
                onClick={runSignOut}
                disabled={isSigningOut}
                className="w-full text-left px-3.5 py-2 hover:bg-amber-50 flex items-center gap-2 text-amber-700 transition-colors border-t border-[#F3F4F6] disabled:opacity-50"
              >
                <LogOut className="w-3.5 h-3.5" />
                <span>{isSigningOut ? "Signing out..." : "Sign Out"}</span>
              </button>
            )}
            <button
              onClick={runDangerAction}
              disabled={isRunningDangerAction}
              className="w-full text-left px-3.5 py-2 hover:bg-red-50 flex items-center gap-2 text-red-600 transition-colors border-t border-[#F3F4F6] disabled:opacity-50"
            >
              <Trash2 className="w-3.5 h-3.5" />
              <span>
                {isRunningDangerAction ? dangerAction.runningLabel ?? "Working..." : dangerAction.label}
              </span>
            </button>
          </div>
        )}
      </div>

      <Tooltip content="View order history"><button onClick={onViewHistory} className="hover:text-[#111111] transition-colors" aria-label="View order history">
        <Eye className="w-3.5 h-3.5" />
      </button></Tooltip>
    </div>
  );

  return (
    <div className="bg-white border border-[#E5E7EB] rounded-[10px] shadow-sm hover:shadow-md transition-all flex flex-col justify-between">
      <div className="px-4 pt-4">
        {/* ── Header: Status dot, name, date, session, 3-dot menu, View ── */}
        <div className="flex items-center justify-between flex-wrap gap-x-2 gap-y-1.5">
          <div className={`flex items-center gap-2 min-w-0 ${staticIpExpired ? "flex-1" : ""}`}>
            <span className={`w-2.5 h-2.5 rounded-full shrink-0 ${sessionValid ? "bg-green-500" : "bg-red-400"}`} />
            <span className="text-xs font-extrabold text-[#111111] tracking-tight truncate">{displayName}</span>
          </div>

          {staticIpExpired ? (
            <div className="flex shrink-0 items-center text-[#9CA3AF]">{menuNode}</div>
          ) : (
            <div className="flex items-center gap-2 text-[#9CA3AF] shrink-0">
              {dateNode}
              {statusNode}
              {menuNode}
            </div>
          )}
          {/* Expired: the date and session status get their own line under the name, so the header is two tidy
              rows at every card width instead of wrapping unpredictably. */}
          {staticIpExpired && (
            <div className="flex basis-full flex-wrap items-center gap-x-3 gap-y-1 pl-[18px]">
              {dateNode}
              {statusNode}
            </div>
          )}
        </div>

      </div>

      {/* Body: trading toggle, metrics, footer. For an expired Static IP it is blurred, inert and covered. */}
      <div className="relative flex flex-1 flex-col">
        <div
          className={`flex flex-1 flex-col justify-between ${staticIpExpired ? "blur-[2px] opacity-60 select-none pointer-events-none" : ""}`}
          aria-hidden={staticIpExpired ? true : undefined}
          {...(staticIpExpired ? ({ inert: "" } as object) : {})}
        >
        <div className="px-4 pb-4 pt-3 space-y-3">
        {/* ── Trading Toggle + Badges (caller-supplied: account type / role, etc.) ── */}
        <div className="flex items-center justify-between text-xs pt-1">
          <div className="flex items-center gap-2">
            <span className="text-[#4B5563] font-medium">Trading</span>
            <button
              role="switch"
              aria-checked={tradingEnabled}
              onClick={runToggle}
              disabled={isToggling}
              className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none ${
                tradingEnabled ? "bg-[#16A34A]" : "bg-[#EF4444]"
              } ${isToggling ? "opacity-50" : ""}`}
            >
              <span
                className={`pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
                  tradingEnabled ? "translate-x-4" : "translate-x-0"
                }`}
              />
            </button>
          </div>

          <div className="flex items-center gap-1.5 flex-wrap">{badges}</div>
        </div>

        {/* ── Metrics Box (In Group, P&L, POS, Orders, Pending, Complete, Reject, Cancel) ── */}
        <div className="p-3 bg-[#F9FAFB] border border-[#E5E7EB] rounded-[10px] space-y-2.5">
          <div className="grid grid-cols-4 gap-1 text-center">
            <div>
              <div className="flex items-center justify-center gap-1 text-[10px] text-[#6B7280]">
                <Users className="w-2.5 h-2.5 text-gray-400" />
                In Group
              </div>
              <div className="text-xs font-bold text-[#111111]">{metrics.in_group}</div>
            </div>
            <Tooltip content={pnlTooltip(metrics)} as="div" className="block" focusable><div>
              <div className="flex items-center justify-center gap-1 text-[10px] text-[#6B7280]">
                <Wallet className="w-2.5 h-2.5 text-[#9CA3AF]" />
                P&amp;L
              </div>
              <div className={`text-xs font-bold ${pnlDisplay(metrics).unavailable ? "text-[#9CA3AF]" : moneyToneClass(pnl.value)}`}>
                {pnlDisplay(metrics).text}
              </div>
              <div className="text-[9px] leading-tight text-[#9CA3AF]">{pnl.caption}</div>
            </div></Tooltip>
            <div>
              <div className="flex items-center justify-center gap-1 text-[10px] text-[#6B7280]">
                <Layers className="w-2.5 h-2.5 text-[#9CA3AF]" />
                POS
              </div>
              <div className="text-xs font-bold text-[#111111]">{openPositionsLabel(metrics)}</div>
            </div>
            <div>
              <div className="flex items-center justify-center gap-1 text-[10px] text-[#6B7280]">
                <ReceiptText className="w-2.5 h-2.5 text-[#9CA3AF]" />
                Orders
              </div>
              <div className="text-xs font-bold text-[#111111]">{metrics.orders_count}</div>
            </div>
          </div>

          <div className="grid grid-cols-5 gap-1 text-center pt-2 border-t border-[#E5E7EB]">
            <div>
              <div className="flex items-center justify-center gap-1 text-[10px] text-[#6B7280]">
                <Clock className="w-2.5 h-2.5 text-amber-400" />
                Pending
              </div>
              <div className="text-xs font-bold text-[#111111]">{metrics.pending_count}</div>
            </div>
            <div>
              <div className="flex items-center justify-center gap-1 text-[10px] text-[#6B7280]">
                <CheckCircle2 className="w-2.5 h-2.5 text-green-500" />
                Complete
              </div>
              <div className="text-xs font-bold text-green-600">{metrics.completed_count}</div>
            </div>
            <div>
              <div className="flex items-center justify-center gap-1 text-[10px] text-[#6B7280]">
                <XCircle className={`w-2.5 h-2.5 ${metrics.rejected_count > 0 ? "text-red-500" : "text-gray-300"}`} />
                Reject
              </div>
              <div className={`text-xs font-bold ${metrics.rejected_count > 0 ? "text-red-600" : "text-[#111111]"}`}>
                {metrics.rejected_count}
              </div>
            </div>
            <div>
              <div className="flex items-center justify-center gap-1 text-[10px] text-[#6B7280]">
                <Ban className="w-2.5 h-2.5 text-gray-400" />
                Cancel
              </div>
              <div className="text-xs font-bold text-[#111111]">{metrics.cancelled_count}</div>
            </div>
            <Tooltip content="The engine couldn't confirm this trade's outcome - see the Reconciliation page." as="div" className="block" focusable><div>
              <div className="flex items-center justify-center gap-1 text-[10px] text-[#6B7280]">
                <AlertTriangle className={`w-2.5 h-2.5 ${metrics.needs_review_count > 0 ? "text-amber-500" : "text-gray-300"}`} />
                Review
              </div>
              <div className={`text-xs font-bold ${metrics.needs_review_count > 0 ? "text-amber-600" : "text-[#111111]"}`}>
                {metrics.needs_review_count}
              </div>
            </div></Tooltip>
          </div>
        </div>
      </div>

      {/* ── Bottom Action Footer: Re-login ── */}
      <div className="p-4 bg-white border-t border-[#F3F4F6] flex items-center justify-end">
        {onRelogin && (
          <button
            onClick={() => onRelogin()}
            className="inline-flex items-center justify-center gap-1.5 h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm transition-all active:scale-[0.98] bg-[#2563EB] hover:bg-[#1D4ED8] text-white border-[#2563EB] hover:border-[#1D4ED8]"
          >
            <ExternalLink className="w-3.5 h-3.5" />
            Re-login
          </button>
        )}
      </div>
        </div>

        {staticIpExpired && (
          <div
            role="alert"
            className="absolute inset-0 flex items-center justify-center overflow-y-auto rounded-b-[10px] bg-white/70 p-4"
          >
            <div className="flex w-full max-w-[17rem] flex-col items-center gap-2 text-center">
              <span className="flex h-9 w-9 items-center justify-center rounded-full bg-red-50 text-[#EF4444]">
                <AlertTriangle className="h-5 w-5" aria-hidden />
              </span>
              <h3 className="text-sm font-bold text-[#111111]">Static IP Expired</h3>
              <p className="text-xs leading-snug text-[#6B7280]">
                This account&apos;s static IP has expired. Please renew or set a new IP to continue trading.
              </p>
              <div className="mt-1 flex flex-wrap items-center justify-center gap-2">
                <button
                  type="button"
                  onClick={staticIpExpired.onRenew}
                  className="inline-flex h-8 items-center justify-center rounded-lg border border-[#E88900] bg-[#E88900] px-3.5 text-xs font-semibold text-white shadow-sm transition-all hover:border-[#D97700] hover:bg-[#D97700] active:scale-[0.98]"
                >
                  Renew Static IP
                </button>
                <button
                  type="button"
                  onClick={staticIpExpired.onSetNewIp}
                  className="inline-flex h-8 items-center justify-center rounded-lg border border-[#E5E7EB] bg-white px-3.5 text-xs font-semibold text-[#111111] shadow-sm transition-all hover:bg-gray-50 active:scale-[0.98]"
                >
                  Set New IP
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
