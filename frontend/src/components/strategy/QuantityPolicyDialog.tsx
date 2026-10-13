"use client";

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import Button from "@/components/ui/Button";
import { extractError } from "@/lib/use-engine-data";

export type QuantityMode = "MULTIPLIER" | "FIXED" | "RATIO" | "CAPITAL_PROPORTIONAL" | "RISK_BASED";

export interface QuantityPolicy {
  quantity_mode: QuantityMode | null;
  fixed_qty: number | null;
  multiplier: number;
}

/** Short text for the Child card: how this Child's copy quantity is derived. */
export function quantityPolicyLabel(policy: QuantityPolicy): string {
  switch (policy.quantity_mode) {
    case "MULTIPLIER":
      return `${policy.multiplier}x of Master qty`;
    case "FIXED":
      return `Fixed qty ${policy.fixed_qty ?? 1}`;
    case null:
      return `${policy.multiplier}x of Master qty`;
    default:
      return policy.quantity_mode;
  }
}

function exampleQty(mode: "MULTIPLIER" | "FIXED", multiplier: number, fixedQty: number): number {
  return mode === "MULTIPLIER" ? Math.floor(65 * multiplier) : fixedQty;
}

/**
 * Lets the user choose, per Child, how the copy quantity is derived:
 * Master quantity x multiplier, or one fixed quantity. Changes only the
 * quantity policy - it never turns copying on or off.
 */
export default function QuantityPolicyDialog({
  strategyId,
  accountId,
  accountName,
  current,
  onClose,
  onSaved,
}: {
  strategyId: string;
  accountId: string;
  accountName: string;
  current: QuantityPolicy;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [mode, setMode] = useState<"MULTIPLIER" | "FIXED">(current.quantity_mode === "MULTIPLIER" ? "MULTIPLIER" : "FIXED");
  const [multiplier, setMultiplier] = useState<string>(String(current.multiplier));
  const [fixedQty, setFixedQty] = useState<string>(String(current.fixed_qty ?? 1));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !saving) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, saving]);

  const multiplierNum = Number(multiplier);
  const fixedNum = Number(fixedQty);
  const multiplierValid = Number.isFinite(multiplierNum) && multiplierNum >= 0.01 && multiplierNum <= 100;
  const fixedValid = Number.isInteger(fixedNum) && fixedNum >= 1;
  const valid = mode === "MULTIPLIER" ? multiplierValid : fixedValid;
  const fractional = mode === "MULTIPLIER" && multiplierValid && !Number.isInteger(multiplierNum);
  const otherMode =
    current.quantity_mode !== null && current.quantity_mode !== "MULTIPLIER" && current.quantity_mode !== "FIXED";

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      const resp = await fetch(`/api/v1/strategies/${strategyId}/members/${accountId}/quantity-policy`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(
          mode === "MULTIPLIER"
            ? { quantity_mode: "MULTIPLIER", multiplier: multiplierNum }
            : { quantity_mode: "FIXED", fixed_qty: fixedNum },
        ),
      });
      const body = await resp.json().catch(() => null);
      if (!resp.ok) {
        setError(extractError(body) ?? "Could not save the quantity setting. Nothing was changed.");
        return;
      }
      onSaved();
    } catch {
      setError("Could not reach the trading engine. Nothing was changed.");
    } finally {
      setSaving(false);
    }
  };

  const optionClass = (selected: boolean) =>
    `block cursor-pointer rounded-[10px] border p-3 ${
      selected ? "border-[#111111] bg-[#F7F7F7]" : "border-[#E5E7EB] bg-white"
    }`;

  return createPortal(
    <div className="fixed inset-0 z-[9999] flex items-center justify-center p-4">
      <div className="fixed inset-0 bg-black/40 backdrop-blur-sm" onClick={() => !saving && onClose()} aria-hidden="true" />
      <div
        className="relative z-10 flex max-h-[90dvh] w-full max-w-md flex-col rounded-[10px] border border-[#E5E7EB] bg-white shadow-2xl"
        role="dialog"
        aria-modal="true"
        aria-labelledby="quantity-policy-title"
      >
        <div className="flex shrink-0 items-start justify-between gap-3 rounded-t-[10px] border-b border-[#E5E7EB] bg-[#F7F7F7] px-5 py-4">
          <div className="min-w-0">
            <h3 id="quantity-policy-title" className="text-sm font-bold text-[#111111]">
              Copy quantity for {accountName}
            </h3>
            <p className="mt-0.5 text-[11px] text-[#6B7280]">Applies to orders copied after you save.</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={saving}
            aria-label="Close"
            className="rounded-md p-1 text-[#6B7280] hover:bg-[#E5E7EB] disabled:opacity-50"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-5 py-4">
          {otherMode && (
            <p className="rounded-[10px] border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-800">
              This Child currently uses the {current.quantity_mode} setting. Saving replaces it with your choice below.
            </p>
          )}

          <label className={optionClass(mode === "MULTIPLIER")}>
            <span className="flex items-center gap-2 text-xs font-semibold text-[#111111]">
              <input
                type="radio"
                name="quantity-mode"
                checked={mode === "MULTIPLIER"}
                onChange={() => setMode("MULTIPLIER")}
                disabled={saving}
                className="accent-[#111111]"
              />
              Multiply the Master quantity
            </span>
            <span className="mt-2 flex items-center gap-2 pl-5">
              <input
                type="number"
                inputMode="decimal"
                min={0.01}
                max={100}
                step={1}
                value={multiplier}
                onChange={(e) => setMultiplier(e.target.value)}
                onFocus={() => setMode("MULTIPLIER")}
                disabled={saving}
                aria-label="Multiplier"
                className="w-24 rounded-md border border-[#E5E7EB] px-2 py-1 text-xs text-[#111111]"
              />
              <span className="text-[11px] text-[#6B7280]">x Master quantity</span>
            </span>
          </label>

          <label className={optionClass(mode === "FIXED")}>
            <span className="flex items-center gap-2 text-xs font-semibold text-[#111111]">
              <input
                type="radio"
                name="quantity-mode"
                checked={mode === "FIXED"}
                onChange={() => setMode("FIXED")}
                disabled={saving}
                className="accent-[#111111]"
              />
              Same quantity for every order
            </span>
            <span className="mt-2 flex items-center gap-2 pl-5">
              <input
                type="number"
                inputMode="numeric"
                min={1}
                step={1}
                value={fixedQty}
                onChange={(e) => setFixedQty(e.target.value)}
                onFocus={() => setMode("FIXED")}
                disabled={saving}
                aria-label="Fixed quantity"
                className="w-24 rounded-md border border-[#E5E7EB] px-2 py-1 text-xs text-[#111111]"
              />
              <span className="text-[11px] text-[#6B7280]">whatever the Master trades</span>
            </span>
          </label>

          {valid && (
            <p className="text-[11px] text-[#374151]">
              Example: Master buys 65, this Child buys{" "}
              <span className="font-semibold">{exampleQty(mode, multiplierNum, fixedNum)}</span>.
            </p>
          )}

          {fractional && (
            <p className="rounded-[10px] border border-amber-200 bg-amber-50 px-3 py-2 text-[11px] text-amber-800">
              A fractional multiplier can give a quantity that is not a full lot. Alice Blue rejects such F&amp;O orders.
              Use a whole number for F&amp;O.
            </p>
          )}
          {mode === "FIXED" && (
            <p className="text-[11px] leading-relaxed text-[#6B7280]">
              For F&amp;O the fixed quantity must be a multiple of the lot size, or Alice Blue rejects the order. A fixed
              quantity is also used for every exit, even when the Master exits a larger position in one order.
            </p>
          )}
          <p className="text-[11px] leading-relaxed text-[#6B7280]">
            Jettone does not know each instrument&apos;s lot size, so it cannot check F&amp;O quantities before sending
            them. Sell orders still need this Child to hold enough of the position.
          </p>

          {error && (
            <p role="alert" className="rounded-[10px] border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-700">
              {error}
            </p>
          )}
        </div>

        <div className="flex shrink-0 flex-wrap items-center justify-end gap-2 border-t border-[#E5E7EB] px-5 py-3">
          <Button variant="ghost" size="sm" onClick={onClose} disabled={saving}>
            Cancel
          </Button>
          <Button size="sm" onClick={() => void handleSave()} loading={saving} disabled={!valid}>
            Save quantity setting
          </Button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
