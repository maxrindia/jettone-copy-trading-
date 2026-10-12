"use client";

import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";
import DatePicker from "@/components/ui/DatePicker";
import { isValidExpiryInput } from "@/lib/static-ip";

/**
 * Renew an expired Static IP: pick the new last-valid day and save. The save
 * handler is the Demat page's existing PUT /accounts/{id} { proxy_expires_on }
 * call (the engine applies the date to every account on the same host); this
 * dialog changes nothing else about the account.
 */
export default function RenewStaticIpDialog({
  host,
  accountName,
  initialDate,
  onClose,
  onSave,
}: {
  host: string | null;
  accountName: string;
  /** The current (expired) expiry date, YYYY-MM-DD, shown until a new one is picked. */
  initialDate: string | null;
  onClose: () => void;
  /** Persist the new expiry (YYYY-MM-DD). Rejects with an Error whose message is shown. */
  onSave: (date: string) => Promise<void>;
}) {
  const [date, setDate] = useState(initialDate ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !saving) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, saving]);

  const save = async () => {
    if (!date || !isValidExpiryInput(date)) {
      setError("Choose the new expiry date.");
      return;
    }
    if (initialDate && date === initialDate) {
      setError("That is the date that already expired. Choose a later date.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await onSave(date);
      onClose();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Could not update the expiry.");
    } finally {
      setSaving(false);
    }
  };

  if (typeof document === "undefined") return null;
  return createPortal(
    <div className="fixed inset-0 z-[9999] flex justify-center overflow-y-auto p-4">
      <div className="fixed inset-0 bg-black/40 backdrop-blur-sm animate-in fade-in" onClick={() => !saving && onClose()} aria-hidden />
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="renew-static-ip-title"
        className="relative z-10 my-auto w-full max-w-sm bg-white border border-[#E5E7EB] rounded-[10px] shadow-2xl p-5 space-y-4 animate-in zoom-in-95 duration-150"
      >
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 id="renew-static-ip-title" className="text-sm font-bold text-[#111111]">
              Renew Static IP
            </h2>
            <p className="mt-0.5 text-xs text-[#6B7280] truncate">
              {accountName}
              {host ? ` · ${host}` : ""}
            </p>
          </div>
          <button
            type="button"
            onClick={() => !saving && onClose()}
            aria-label="Close"
            className="p-1.5 rounded-lg text-[#9CA3AF] hover:text-[#111111] hover:bg-gray-100 transition-colors"
          >
            <X className="w-4 h-4" aria-hidden />
          </button>
        </div>

        <div>
          <label className="block text-[10px] font-semibold text-[#4B5563] mb-1 uppercase tracking-wider">New expiry date</label>
          <DatePicker value={date} onChange={setDate} ariaLabel="New Static IP expiry date" placeholder="Select date" />
          <p className="mt-1 text-[10px] text-[#6B7280]">Last day the IP is valid. Broker credentials and other settings are not changed.</p>
        </div>

        {error && <p className="text-xs font-medium text-[#EF4444]">{error}</p>}

        <div className="flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={saving}
            className="inline-flex items-center justify-center h-8 px-3.5 rounded-lg border text-xs font-semibold bg-white hover:bg-gray-50 text-[#111111] border-[#E5E7EB]"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={save}
            disabled={saving}
            className="inline-flex items-center justify-center h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm bg-[#E88900] hover:bg-[#D97700] text-white border-[#E88900] hover:border-[#D97700] disabled:opacity-50"
          >
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
