import React from "react";
import {
  CheckCircle2,
  XCircle,
  Clock,
  Zap,
  TrendingUp,
  AlertCircle,
} from "lucide-react";
import { DematAccount } from "@/types";

interface OverviewTabProps {
  accounts: DematAccount[];
  isEmergencyStopped: boolean;
}

export function OverviewTab({ accounts, isEmergencyStopped }: OverviewTabProps) {
  const master = accounts.find((a) => a.accountType === "MASTER");
  const followers = accounts.filter((a) => a.accountType === "FOLLOWER");
  const activeFollowers = followers.filter((f) => f.isActive);

  return (
    <div className="space-y-6">
      {/* Reconnection Alert Banner if any account needs re-auth */}
      {accounts.some((a) => !a.sessionValid) && (
        <div className="bg-amber-500/10 border border-amber-500/30 rounded-xl p-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <AlertCircle className="w-5 h-5 text-amber-400 flex-shrink-0" />
            <div>
              <p className="text-sm font-semibold text-amber-200">
                Accounts Requiring Daily Alice Blue Login
              </p>
              <p className="text-xs text-amber-300/80">
                1 or more accounts have expired sessions. Re-authenticate via ANT API before market open.
              </p>
            </div>
          </div>
          <button className="px-3 py-1.5 bg-amber-500 hover:bg-amber-400 text-black text-xs font-bold rounded-lg transition-colors">
            Re-authenticate All
          </button>
        </div>
      )}

      {/* Metric Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-5">
        {/* Master Status */}
        <div className="bg-[#121826] border border-[#1e293b] rounded-xl p-5 shadow-sm">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
              Master Account
            </span>
            <span
              className={`w-2.5 h-2.5 rounded-full ${
                master?.sessionValid ? "bg-emerald-500" : "bg-rose-500 animate-ping"
              }`}
            />
          </div>
          <p className="text-xl font-bold text-white mt-2 font-mono">
            {master ? master.displayName : "Not Configured"}
          </p>
          <p className="text-xs text-gray-400 mt-1">
            ID: <span className="font-mono text-gray-300">{master?.aliceUserId || ""}</span>
          </p>
        </div>

        {/* Child Accounts Active */}
        <div className="bg-[#121826] border border-[#1e293b] rounded-xl p-5 shadow-sm">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
              Child Accounts
            </span>
            <Zap className="w-4 h-4 text-indigo-400" />
          </div>
          <p className="text-xl font-bold text-white mt-2 font-mono">
            {activeFollowers.length} / {followers.length} Active
          </p>
          <p className="text-xs text-gray-400 mt-1">Replication Parallel Stream</p>
        </div>

        {/* Today's Orders */}
        <div className="bg-[#121826] border border-[#1e293b] rounded-xl p-5 shadow-sm">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
              Today's Replications
            </span>
            <CheckCircle2 className="w-4 h-4 text-emerald-400" />
          </div>
          <p className="text-xl font-bold text-white mt-2 font-mono">0 Orders</p>
          <div className="flex items-center gap-3 text-[11px] text-gray-400 mt-1">
            <span className="text-emerald-400">0 Success</span>
            <span className="text-rose-400">0 Reject</span>
          </div>
        </div>

        {/* Engine Guard */}
        <div className="bg-[#121826] border border-[#1e293b] rounded-xl p-5 shadow-sm">
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
              Trading Guard
            </span>
            <TrendingUp className="w-4 h-4 text-indigo-400" />
          </div>
          <p
            className={`text-xl font-bold mt-2 font-mono ${
              isEmergencyStopped ? "text-rose-400" : "text-emerald-400"
            }`}
          >
            {isEmergencyStopped ? "LOCKED" : "READY"}
          </p>
          <p className="text-xs text-gray-400 mt-1">
            {isEmergencyStopped ? "Copy Trading Suspended" : "Idempotency Triple Active"}
          </p>
        </div>
      </div>

      {/* Account Status Table Preview */}
      <div className="bg-[#121826] border border-[#1e293b] rounded-xl p-6">
        <h3 className="text-sm font-semibold text-white mb-4">Connected Demat Accounts (1M + 5F)</h3>
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="text-xs text-gray-400 uppercase bg-[#0b0f19] border-b border-[#1e293b]">
              <tr>
                <th className="px-4 py-3">Role</th>
                <th className="px-4 py-3">Display Name</th>
                <th className="px-4 py-3">Alice Blue User ID</th>
                <th className="px-4 py-3">Session Status</th>
                <th className="px-4 py-3">Copy Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#1e293b]">
              {accounts.map((acc) => (
                <tr key={acc.id} className="hover:bg-[#161f32]/50 transition-colors">
                  <td className="px-4 py-3">
                    <span
                      className={`inline-flex items-center px-2 py-0.5 rounded text-[11px] font-bold font-mono ${
                        acc.accountType === "MASTER"
                          ? "bg-indigo-500/20 text-indigo-300 border border-indigo-500/30"
                          : "bg-gray-700/40 text-gray-300 border border-gray-600/40"
                      }`}
                    >
                      {acc.accountType}
                    </span>
                  </td>
                  <td className="px-4 py-3 font-medium text-white">{acc.displayName}</td>
                  <td className="px-4 py-3 font-mono text-gray-300">{acc.aliceUserId}</td>
                  <td className="px-4 py-3">
                    {acc.sessionValid ? (
                      <span className="inline-flex items-center gap-1 text-emerald-400 text-xs font-semibold">
                        <CheckCircle2 className="w-3.5 h-3.5" /> Valid
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 text-rose-400 text-xs font-semibold">
                        <XCircle className="w-3.5 h-3.5" /> Expired
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    {acc.isActive ? (
                      <span className="text-emerald-400 text-xs font-medium">Replicating</span>
                    ) : (
                      <span className="text-gray-500 text-xs font-medium">Paused</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
