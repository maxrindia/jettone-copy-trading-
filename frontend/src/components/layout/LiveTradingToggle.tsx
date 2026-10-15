"use client";

import { useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import Button from "@/components/ui/Button";
import { extractError, useEngineData } from "@/lib/use-engine-data";
import {
  liveTradingBadge,
  liveTradingRequest,
  unwrapLiveTrading,
  type LiveTradingEnvelope,
  type LiveTradingTone,
} from "@/lib/live-trading";
import Tooltip from "@/components/ui/Tooltip";

const TONE_CLASSES: Record<LiveTradingTone, { pill: string; dot: string }> = {
  on: { pill: "bg-green-50 text-green-800 border-green-200", dot: "bg-green-500" },
  paused: { pill: "bg-amber-50 text-amber-800 border-amber-200", dot: "bg-amber-500" },
  off: { pill: "bg-[#F7F7F7] text-[#374151] border-[#E5E7EB]", dot: "bg-[#9CA3AF]" },
  unknown: { pill: "bg-[#F7F7F7] text-[#6B7280] border-[#E5E7EB]", dot: "bg-[#D1D5DB]" },
};

/**
 * Header switch for Live Trading. OFF -> ON needs an explicit acknowledgement;
 * ON -> OFF takes one confirmation. The backend enforces the state; this only
 * changes it.
 */
export default function LiveTradingToggle() {
  const { data, error, refresh } = useEngineData<LiveTradingEnvelope>("/reliability/live-trading", 10000);
  const state = unwrapLiveTrading(data);
  const badge = liveTradingBadge(state);
  const tone = TONE_CLASSES[badge.tone];

  const [open, setOpen] = useState(false);
  const [ack, setAck] = useState(false);
  const [saving, setSaving] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const boxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!saving && boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !saving) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open, saving]);

  const turningOn = !(state?.enabled ?? false);

  const apply = async () => {
    setSaving(true);
    setFailure(null);
    try {
      const resp = await fetch("/api/v1/reliability/live-trading", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(liveTradingRequest(turningOn)),
      });
      const body = await resp.json().catch(() => null);
      if (!resp.ok) {
        setFailure(extractError(body) ?? "Could not change Live Trading. Nothing was changed.");
        return;
      }
      setOpen(false);
      setAck(false);
      refresh();
    } catch {
      setFailure("Could not reach the trading engine. Nothing was changed.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="relative" ref={boxRef}>
      <Tooltip content={badge.note ?? "Turn Live Trading ON or OFF"}><button
        type="button"
        onClick={() => {
          setFailure(null);
          setAck(false);
          setOpen((o) => !o);
        }}
        disabled={state === null}
        aria-haspopup="dialog"
        aria-expanded={open}
        className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 text-[11px] font-semibold transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-[#111111]/20 disabled:opacity-60 ${tone.pill}`}
      >
        <span className={`h-1.5 w-1.5 rounded-full ${tone.dot}`} aria-hidden />
        {badge.label}
      </button></Tooltip>
      {error && state === null && (
        <span className="ml-2 text-[11px] text-red-600" role="status">
          state unavailable
        </span>
      )}

      {open && state !== null && (
        <div
          role="dialog"
          aria-labelledby="live-trading-title"
          className="absolute left-0 top-full z-50 mt-2 w-80 rounded-[10px] border border-[#E5E7EB] bg-white p-4 shadow-lg shadow-gray-900/10"
        >
          <h2 id="live-trading-title" className="text-xs font-bold text-[#111111]">
            {turningOn ? "Turn Live Trading ON?" : "Turn Live Trading OFF?"}
          </h2>

          {turningOn ? (
            <>
              <p className="mt-1.5 text-xs leading-relaxed text-[#374151]">
                Real orders will be placed on the Child accounts whenever the strategy&apos;s Master trades, using each Child&apos;s
                saved quantity setting.
              </p>
              <ul className="mt-2 list-disc space-y-1 pl-4 text-[11px] leading-relaxed text-[#6B7280]">
                <li>Only orders the Master places after this moment are copied.</li>
                <li>Do not run another Jettone engine on the same broker accounts at the same time.</li>
              </ul>
              <label className="mt-3 flex cursor-pointer items-start gap-2 text-xs text-[#111111]">
                <input
                  type="checkbox"
                  checked={ack}
                  onChange={(e) => setAck(e.target.checked)}
                  disabled={saving}
                  className="mt-0.5 accent-[#111111]"
                />
                <span>I understand real money will be traded.</span>
              </label>
            </>
          ) : (
            <p className="mt-1.5 text-xs leading-relaxed text-[#374151]">
              No new copy orders will be placed. A Master cancellation is still passed on to Child orders that are already
              working; filled orders and open positions are left as they are.
            </p>
          )}

          {failure && (
            <p role="alert" className="mt-3 rounded-lg border border-red-200 bg-red-50 px-2.5 py-1.5 text-[11px] text-red-700">
              {failure}
            </p>
          )}

          <div className="mt-3 flex justify-end gap-2">
            <Button type="button" variant="secondary" size="sm" onClick={() => setOpen(false)} disabled={saving}>
              Cancel
            </Button>
            <Button
              type="button"
              size="sm"
              variant={turningOn ? "primary" : "destructive"}
              onClick={() => void apply()}
              disabled={saving || (turningOn && !ack)}
            >
              {saving && <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />}
              {turningOn ? "Turn ON" : "Turn OFF"}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
