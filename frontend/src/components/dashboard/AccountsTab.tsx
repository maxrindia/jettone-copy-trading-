import React, { useState } from "react";
import { Plus, RefreshCw, Key, CheckCircle2, XCircle } from "lucide-react";
import { DematAccount } from "@/types";

interface AccountsTabProps {
  accounts: DematAccount[];
  onToggleAccount: (id: string) => void;
}

export function AccountsTab({ accounts, onToggleAccount }: AccountsTabProps) {
  const [selectedAcc, setSelectedAcc] = useState<string | null>(null);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-semibold text-white">Demat Accounts (6 Total)</h2>
          <p className="text-xs text-gray-400">1 Master Account + Child Accounts (Single-User Setup)</p>
        </div>
        <button className="flex items-center gap-2 px-3.5 py-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg text-xs font-semibold shadow-lg shadow-indigo-600/30 transition-all">
          <Plus className="w-3.5 h-3.5" />
          Add Account
        </button>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-5">
        {accounts.map((acc) => (
          <div
            key={acc.id}
            className={`bg-[#121826] border rounded-xl p-5 shadow-sm transition-all ${
              acc.accountType === "MASTER"
                ? "border-indigo-500/40 bg-gradient-to-b from-indigo-950/20 to-[#121826]"
                : "border-[#1e293b] hover:border-gray-700"
            }`}
          >
            <div className="flex items-center justify-between mb-3">
              <span
                className={`text-[10px] font-bold font-mono px-2 py-0.5 rounded uppercase tracking-wider ${
                  acc.accountType === "MASTER"
                    ? "bg-indigo-500/20 text-indigo-300 border border-indigo-500/30"
                    : "bg-gray-800 text-gray-400 border border-gray-700"
                }`}
              >
                {acc.accountType}
              </span>
              <div className="flex items-center gap-1.5">
                <span className="text-[11px] text-gray-400">Status:</span>
                <span
                  className={`w-2 h-2 rounded-full ${
                    acc.isActive ? "bg-emerald-500" : "bg-gray-500"
                  }`}
                />
              </div>
            </div>

            <h3 className="text-base font-bold text-white mb-1">{acc.displayName}</h3>
            <p className="text-xs text-gray-400 font-mono mb-4">Alice Blue ID: {acc.aliceUserId}</p>

            <div className="space-y-2 py-3 border-t border-[#1e293b] text-xs">
              <div className="flex justify-between items-center text-gray-300">
                <span className="text-gray-500">API Session:</span>
                {acc.sessionValid ? (
                  <span className="text-emerald-400 font-medium flex items-center gap-1">
                    <CheckCircle2 className="w-3 h-3" /> Active
                  </span>
                ) : (
                  <span className="text-rose-400 font-medium flex items-center gap-1">
                    <XCircle className="w-3 h-3" /> Expired
                  </span>
                )}
              </div>
              <div className="flex justify-between items-center text-gray-300">
                <span className="text-gray-500">Encrypted Secret:</span>
                <span className="font-mono text-gray-400 flex items-center gap-1">
                  <Key className="w-3 h-3 text-indigo-400" /> ••••••••••••
                </span>
              </div>
            </div>

            <div className="mt-4 pt-3 border-t border-[#1e293b] flex items-center justify-between gap-2">
              <button className="flex items-center gap-1.5 px-3 py-1.5 bg-[#1e293b] hover:bg-gray-700 text-gray-200 text-xs font-semibold rounded-lg transition-colors">
                <RefreshCw className="w-3 h-3" />
                Login (ANT)
              </button>
              {acc.accountType === "FOLLOWER" && (
                <button
                  onClick={() => onToggleAccount(acc.id)}
                  className={`px-3 py-1.5 text-xs font-semibold rounded-lg transition-colors ${
                    acc.isActive
                      ? "bg-rose-500/10 text-rose-400 hover:bg-rose-500/20 border border-rose-500/20"
                      : "bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20 border border-emerald-500/20"
                  }`}
                >
                  {acc.isActive ? "Pause Copy" : "Enable Copy"}
                </button>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
