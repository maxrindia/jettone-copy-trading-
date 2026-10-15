"use client";

import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { AlertTriangle, CheckCircle2, RotateCw, X } from "lucide-react";
import Button from "@/components/ui/Button";
import { extractError } from "@/lib/use-engine-data";
import { CNC_ACKNOWLEDGEMENT_TEXT, canConfirmSwitch, requiresCncAcknowledgement } from "@/lib/master-switch";

export interface SwitchAccount {
  id: string;
  display_name: string;
  alice_user_id: string;
}

interface SwitchBlocker {
  code: string;
  message: string;
  account_id: string | null;
  account_name: string | null;
  role: "CURRENT_MASTER" | "NEW_MASTER" | "CHILD" | null;
  count: number | null;
  items: string[];
}

interface SwitchPreflight {
  allowed: boolean;
  can_override?: boolean;
  blockers: SwitchBlocker[];
  warnings: SwitchBlocker[];
  limitations: string[];
  checked_accounts: { account_id: string; account_name: string; role: string }[];
  /** Open CNC positions exist: the switch needs the user's acknowledgement (enforced by the engine). */
  requires_cnc_acknowledgement?: boolean;
}

type OldMasterAction = "CHILD" | "REMOVE";

// Record-based advisories (old/flagged reconciliation items, delivery totals from
// Jettone's own records). They never block a switch, nobody can act on them from
// here, and they show on every switch - so they are kept out of this dialog
// (they live on the Reconciliation page) to keep real warnings noticeable.
const ADVISORY_CODES = new Set(["RECONCILIATION_UNRESOLVED", "RECONCILIATION_HISTORY", "CARRY_POSITION_UNVERIFIED"]);

const BLOCKER_TITLES: Record<string, string> = {
  PENDING_MASTER_ORDERS: "Pending Master orders",
  PENDING_CHILD_ORDERS: "Pending or partially filled Child orders",
  CNC_POSITIONS_OPEN: "Open CNC (delivery) positions",
  INTRADAY_POSITIONS_OPEN: "Open intraday positions",
  COPY_IN_FLIGHT: "Copy placement in progress",
  RECONCILIATION_UNRESOLVED: "Flagged for review today",
  OPEN_POSITIONS: "Open positions detected",
  CARRY_POSITION_UNVERIFIED: "Delivery holdings in Jettone's records",
  BROKER_VERIFICATION_UNAVAILABLE: "Broker verification unavailable",
  RECONCILIATION_HISTORY: "Older unresolved items",
};

const ROLE_LABELS: Record<string, string> = {
  CURRENT_MASTER: "Current Master",
  NEW_MASTER: "New Master",
  CHILD: "Child",
};

function BlockerRow({ b, tone }: { b: SwitchBlocker; tone: "block" | "warn" }) {
  if (tone === "warn") {
    // Warnings are informational: one compact line with the items inline.
    return (
      <li className="rounded-[10px] border border-amber-200 bg-amber-50 px-3 py-2">
        <p className="text-xs text-amber-900">
          <span className="font-semibold">{BLOCKER_TITLES[b.code] ?? b.code}</span>
          {b.account_name ? <span className="text-amber-800"> · {b.account_name}</span> : null}
          {b.count != null && b.count > 0 ? <span className="text-amber-800"> ({b.count})</span> : null}
        </p>
        {b.items.length > 0 ? (
          <p className="mt-0.5 font-mono text-[10px] leading-relaxed text-amber-800 break-words">
            {b.items.join("  ·  ")}
          </p>
        ) : null}
      </li>
    );
  }
  return (
    <li
      className={`rounded-[10px] border p-3 ${
        tone === "block" ? "border-red-200 bg-red-50" : "border-amber-200 bg-amber-50"
      }`}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span className={`text-xs font-bold ${tone === "block" ? "text-red-800" : "text-amber-800"}`}>
          {BLOCKER_TITLES[b.code] ?? b.code}
          {b.count != null && b.count > 0 ? ` (${b.count})` : ""}
        </span>
        {b.account_name && (
          <span className="text-[11px] font-semibold text-[#374151]">
            {b.account_name}
            {b.role ? <span className="font-normal text-[#6B7280]"> · {ROLE_LABELS[b.role] ?? b.role}</span> : null}
          </span>
        )}
      </div>
      <p className={`mt-1 text-[11px] leading-relaxed ${tone === "block" ? "text-red-700" : "text-amber-700"}`}>
        {b.message}
      </p>
      {b.items.length > 0 && (
        <ul className="mt-1.5 space-y-0.5">
          {b.items.map((item) => (
            <li key={item} className="font-mono text-[10px] text-[#4B5563] break-all">
              {item}
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

/**
 * Switches a strategy's Master only after the engine's preflight confirms
 * nothing is still in motion. Shows every specific blocker (account + count)
 * when it can't; there is no override, and nothing is ever cancelled or
 * squared off from here.
 */
export default function MasterSwitchDialog({
  strategyId,
  strategyName,
  currentMaster,
  newMaster,
  onClose,
  onSwitched,
}: {
  strategyId: string;
  strategyName: string;
  currentMaster: SwitchAccount | null;
  newMaster: SwitchAccount;
  onClose: () => void;
  onSwitched: (monitorReady: boolean) => void;
}) {
  const [action, setAction] = useState<OldMasterAction>("CHILD");
  const [preflight, setPreflight] = useState<SwitchPreflight | null>(null);
  const [checking, setChecking] = useState(false);
  const [switching, setSwitching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Only offered when Alice Blue could not be read (never for real findings):
  // the user confirms they checked the accounts themselves.
  const [acknowledged, setAcknowledged] = useState(false);
  // Open CNC positions stay open in Alice Blue; the switch needs this explicit,
  // initially-unchecked acknowledgement. The dialog is unmounted when closed,
  // and every re-check also clears it, so it never carries over.
  const [cncAcknowledged, setCncAcknowledged] = useState(false);

  // keepError: re-check after a refused switch without wiping the reason
  // the switch was refused - the user must see both.
  const runPreflight = useCallback(async (opts?: { keepError?: boolean }) => {
    const keepError = opts?.keepError ?? false;
    setChecking(true);
    if (!keepError) setError(null);
    try {
      const params = new URLSearchParams({ new_master_account_id: newMaster.id, old_master_action: action });
      const resp = await fetch(`/api/v1/strategies/${strategyId}/switch-master/preflight?${params}`);
      const body = await resp.json().catch(() => null);
      if (!resp.ok) {
        setPreflight(null);
        if (!keepError) setError(extractError(body) ?? "Could not check whether the Master can be switched.");
        return;
      }
      setPreflight(body as SwitchPreflight);
      setAcknowledged(false);
      setCncAcknowledged(false);
    } catch {
      setPreflight(null);
      if (!keepError) setError("Could not reach the trading engine to check the switch.");
    } finally {
      setChecking(false);
    }
  }, [strategyId, newMaster.id, action]);

  useEffect(() => {
    void runPreflight();
  }, [runPreflight]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !switching) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, switching]);

  const handleSwitch = async () => {
    setSwitching(true);
    setError(null);
    try {
      const resp = await fetch(`/api/v1/strategies/${strategyId}/switch-master`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          new_master_account_id: newMaster.id,
          old_master_action: action,
          acknowledge_unverified: acknowledged,
          acknowledge_cnc_positions: cncAcknowledged,
        }),
      });
      const body = await resp.json().catch(() => null);
      if (!resp.ok) {
        // Something changed since the check (e.g. a new order): show the
        // fresh blockers rather than a bare error.
        setError(extractError(body) ?? "The Master could not be switched.");
        await runPreflight({ keepError: true });
        return;
      }
      onSwitched(Boolean((body as { monitor_ready?: boolean } | null)?.monitor_ready));
    } catch {
      setError("Could not reach the trading engine. Nothing was changed.");
    } finally {
      setSwitching(false);
    }
  };

  const blocked = preflight !== null && !preflight.allowed;
  const overridable = blocked && preflight?.can_override === true;
  const needsCncAck = requiresCncAcknowledgement(preflight);
  const canSwitch = canConfirmSwitch({
    preflight,
    checking,
    unverifiedAcknowledged: acknowledged,
    cncAcknowledged,
  });
  const visibleWarnings = (preflight?.warnings ?? []).filter((w) => !ADVISORY_CODES.has(w.code));
  const hasOpenPositionWarning = visibleWarnings.some(
    (w) => w.code === "CNC_POSITIONS_OPEN" || w.code === "INTRADAY_POSITIONS_OPEN",
  );

  return createPortal(
    <div className="fixed inset-0 z-[9999] flex items-center justify-center p-4">
      <div
        className="fixed inset-0 bg-black/40 backdrop-blur-sm"
        onClick={() => !switching && onClose()}
        aria-hidden="true"
      />
      <div
        className="relative z-10 flex max-h-[90dvh] w-full max-w-lg flex-col rounded-[10px] border border-[#E5E7EB] bg-white shadow-2xl"
        role="dialog"
        aria-modal="true"
        aria-labelledby="master-switch-title"
      >
        <div className="flex shrink-0 items-start justify-between gap-3 border-b border-[#E5E7EB] bg-[#F7F7F7] px-5 py-4 rounded-t-[10px]">
          <div className="min-w-0">
            <h3 id="master-switch-title" className="text-sm font-bold text-[#111111]">
              Change Master of {strategyName}
            </h3>
            <p className="mt-0.5 text-[11px] text-[#6B7280]">
              {currentMaster ? currentMaster.display_name : "No Master"} → {newMaster.display_name}{" "}
              <span className="font-mono">{newMaster.alice_user_id}</span>
            </p>
          </div>
          <button
            onClick={onClose}
            disabled={switching}
            className="rounded-lg p-1.5 text-[#9CA3AF] transition-colors hover:bg-gray-100 hover:text-[#111111] disabled:opacity-50"
            aria-label="Close"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-5 py-4">
          {currentMaster && (
            <fieldset>
              <legend className="mb-2 text-[11px] font-semibold text-[#374151]">
                What happens to {currentMaster.display_name}?
              </legend>
              <div className="grid gap-2 sm:grid-cols-2">
                {(
                  [
                    ["CHILD", "Keep as a Child", "Stays in this strategy as a Child, with Trading on. You can switch it off any time."],
                    ["REMOVE", "Remove from strategy", "Leaves this strategy and can join another one."],
                  ] as const
                ).map(([value, title, hint]) => (
                  <label
                    key={value}
                    className={`cursor-pointer rounded-[10px] border p-3 transition-colors ${
                      action === value ? "border-[#111111] bg-[#F9FAFB]" : "border-[#E5E7EB] hover:border-[#9CA3AF]"
                    }`}
                  >
                    <span className="flex items-center gap-2">
                      <input
                        type="radio"
                        name="old-master-action"
                        value={value}
                        checked={action === value}
                        onChange={() => setAction(value)}
                        disabled={switching}
                        className="accent-[#111111]"
                      />
                      <span className="text-xs font-semibold text-[#111111]">{title}</span>
                    </span>
                    <span className="mt-1 block text-[11px] leading-snug text-[#6B7280]">{hint}</span>
                  </label>
                ))}
              </div>
            </fieldset>
          )}

          {error && (
            <div className="rounded-[10px] border border-red-200 bg-red-50 p-3 text-xs font-semibold text-red-700">
              {error}
            </div>
          )}

          {checking && !preflight && (
            <p className="flex items-center gap-2 text-xs text-[#6B7280]">
              <RotateCw className="h-3.5 w-3.5 animate-spin" /> Checking orders and positions at Alice Blue…
            </p>
          )}

          {preflight && preflight.allowed && (
            <div className="rounded-[10px] border border-green-200 bg-green-50 p-3">
              <p className="mb-2 text-xs font-bold text-green-800">Ready to switch</p>
              <ul className="space-y-1.5">
                {[
                  <>No pending orders on {preflight.checked_accounts.map((a) => a.account_name).join(", ")}</>,
                  <>No copies in progress</>,
                  ...(hasOpenPositionWarning ? [] : [<>No open positions</>]),
                  <>
                    {newMaster.display_name}&apos;s new orders will be copied to this strategy&apos;s Children
                  </>,
                  <>Orders already on {newMaster.display_name}&apos;s order book are not copied</>,
                ].map((line, i) => (
                  <li key={i} className="flex items-start gap-2 text-xs text-green-900">
                    <CheckCircle2 className="mt-px h-3.5 w-3.5 shrink-0 text-green-600" />
                    <span>{line}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {blocked && (
            <div>
              <p className="mb-2 flex items-center gap-1.5 text-xs font-bold text-red-800">
                <AlertTriangle className="h-4 w-4" /> The Master can&apos;t be changed yet
              </p>
              <ul className="space-y-2">
                {preflight.blockers.map((b, i) => (
                  <BlockerRow key={`${b.code}-${b.account_id}-${i}`} b={b} tone="block" />
                ))}
              </ul>
              <p className="mt-2 text-[11px] text-[#6B7280]">
                Jettone never cancels orders or squares off positions for a switch. Resolve these in Alice Blue,
                then check again.
              </p>
            </div>
          )}

          {overridable && (
            <div className="rounded-[10px] border border-amber-300 bg-amber-50 p-3">
              <p className="text-xs font-semibold text-amber-900">Alice Blue could not be read right now</p>
              <p className="mt-1 text-[11px] leading-relaxed text-amber-800">
                Orders and positions could not be verified automatically. Try Check again first. If you have
                already checked in Alice Blue, you can continue.
              </p>
              <label className="mt-2 flex cursor-pointer items-start gap-2 text-xs text-amber-900">
                <input
                  type="checkbox"
                  checked={acknowledged}
                  onChange={(e) => setAcknowledged(e.target.checked)}
                  disabled={switching}
                  className="mt-0.5 accent-[#111111]"
                />
                <span>
                  I checked in Alice Blue: no open orders and no open positions on these accounts.
                </span>
              </label>
            </div>
          )}

          {visibleWarnings.length > 0 && (
            <ul className="space-y-2">
              {visibleWarnings.map((w, i) => (
                <BlockerRow key={`${w.code}-${w.account_id}-${i}`} b={w} tone="warn" />
              ))}
            </ul>
          )}

          {preflight && needsCncAck && (
            <div className="rounded-[10px] border border-amber-300 bg-amber-50 p-3">
              <p className="text-xs font-semibold text-amber-900">CNC positions stay open</p>
              <p className="mt-1 text-[11px] leading-relaxed text-amber-800">
                Changing the Master does not touch these positions. Close or manage them in Alice Blue yourself.
              </p>
              <label className="mt-2 flex cursor-pointer items-start gap-2 text-xs text-amber-900">
                <input
                  type="checkbox"
                  checked={cncAcknowledged}
                  onChange={(e) => setCncAcknowledged(e.target.checked)}
                  disabled={switching}
                  className="mt-0.5 accent-[#111111]"
                />
                <span>{CNC_ACKNOWLEDGEMENT_TEXT}</span>
              </label>
            </div>
          )}
        </div>

        <div className="flex shrink-0 flex-wrap items-center justify-end gap-2 border-t border-[#E5E7EB] px-5 py-3">
          <Button variant="ghost" size="sm" onClick={onClose} disabled={switching}>
            Cancel
          </Button>
          <Button variant="secondary" size="sm" onClick={() => void runPreflight()} loading={checking} disabled={switching}>
            {!checking && <RotateCw className="h-3.5 w-3.5" />}
            Check again
          </Button>
          <Button
            size="sm"
            onClick={() => void handleSwitch()}
            loading={switching}
            disabled={!canSwitch || switching}
          >
            {overridable ? "Change Master anyway" : "Change Master"}
          </Button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
