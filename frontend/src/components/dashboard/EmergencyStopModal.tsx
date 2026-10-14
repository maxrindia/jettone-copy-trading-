import React, { useState } from "react";
import { ShieldAlert, AlertTriangle, X } from "lucide-react";

interface EmergencyStopModalProps {
  isOpen: boolean;
  onClose: () => void;
  isEmergencyStopped: boolean;
  onConfirm: (reason: string) => void;
}

export function EmergencyStopModal({
  isOpen,
  onClose,
  isEmergencyStopped,
  onConfirm,
}: EmergencyStopModalProps) {
  const [reason, setReason] = useState("");

  if (!isOpen) return null;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    onConfirm(reason || (isEmergencyStopped ? "Manual Resume" : "Manual Emergency Trigger"));
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm p-4">
      <div className="bg-[#121826] border border-[#1e293b] w-full max-w-md rounded-2xl shadow-2xl p-6 space-y-5">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div
              className={`w-10 h-10 rounded-xl flex items-center justify-center ${
                isEmergencyStopped
                  ? "bg-emerald-500/20 text-emerald-400"
                  : "bg-rose-500/20 text-rose-400"
              }`}
            >
              <ShieldAlert className="w-5 h-5" />
            </div>
            <div>
              <h3 className="text-base font-bold text-white">
                {isEmergencyStopped ? "Resume Copy Trading?" : "Trigger Emergency Stop"}
              </h3>
              <p className="text-xs text-gray-400">
                {isEmergencyStopped
                  ? "Re-enable trade replication across all followers"
                  : "Immediately halt all new follower trade submissions"}
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-white transition-colors"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <label className="text-xs font-semibold text-gray-300">
              Audit Reason (Logged to System Events)
            </label>
            <input
              type="text"
              required
              placeholder="Enter Reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              className="w-full bg-[#0b0f19] border border-[#1e293b] rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-indigo-500"
            />
          </div>

          {!isEmergencyStopped && (
            <div className="p-3 bg-rose-500/10 border border-rose-500/20 rounded-xl flex items-start gap-2.5 text-xs text-rose-300">
              <AlertTriangle className="w-4 h-4 text-rose-400 flex-shrink-0 mt-0.5" />
              <span>
                Existing pending queues will be rejected with reason code{" "}
                <code className="font-mono bg-rose-950 px-1 py-0.5 rounded">
                  EMERGENCY_STOP_ACTIVE
                </code>
                .
              </span>
            </div>
          )}

          <div className="flex items-center justify-end gap-3 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 bg-[#1e293b] hover:bg-gray-700 text-gray-300 text-xs font-semibold rounded-lg transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              className={`px-5 py-2 text-xs font-bold rounded-lg shadow-lg transition-all ${
                isEmergencyStopped
                  ? "bg-emerald-600 hover:bg-emerald-500 text-white shadow-emerald-600/30"
                  : "bg-rose-600 hover:bg-rose-500 text-white shadow-rose-600/30"
              }`}
            >
              {isEmergencyStopped ? "Confirm Resume" : "Confirm Emergency Stop"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
