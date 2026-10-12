import React from "react";
import { Server, ShieldCheck, CheckCircle2, AlertCircle, Copy } from "lucide-react";

export function StaticIPTab() {
  const vpsIp = "178.18.252.165";

  return (
    <div className="space-y-6 max-w-4xl">
      <div>
        <h2 className="text-base font-semibold text-white">VPS Static IP & Broker Gateway</h2>
        <p className="text-xs text-gray-400">
          All 6 Alice Blue account sessions and REST/WebSocket streams originate exclusively from this whitelisted VPS IP.
        </p>
      </div>

      {/* Main IP Info Card */}
      <div className="bg-[#121826] border border-[#1e293b] rounded-xl p-6 space-y-6 shadow-sm">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 pb-6 border-b border-[#1e293b]">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-xl bg-indigo-600/20 border border-indigo-500/30 flex items-center justify-center">
              <Server className="w-5 h-5 text-indigo-400" />
            </div>
            <div>
              <p className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
                Production VPS Static IP
              </p>
              <p className="text-2xl font-bold font-mono text-white tracking-tight mt-0.5">
                {vpsIp}
              </p>
            </div>
          </div>

          <div className="flex items-center gap-2">
            <span className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
              <ShieldCheck className="w-4 h-4" /> Whitelisted with Alice Blue
            </span>
          </div>
        </div>

        {/* Details Grid */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 text-xs">
          <div className="p-4 bg-[#0b0f19] border border-[#1e293b] rounded-xl space-y-1">
            <span className="text-gray-500">Broker API Target</span>
            <p className="text-sm font-semibold text-white">Alice Blue ANT / A3 API</p>
            <p className="text-[11px] text-gray-400">NorenWS WebSocket + REST Gateways</p>
          </div>

          <div className="p-4 bg-[#0b0f19] border border-[#1e293b] rounded-xl space-y-1">
            <span className="text-gray-500">Network Routing Policy</span>
            <p className="text-sm font-semibold text-white">Fail Closed (Zero External Fallback)</p>
            <p className="text-[11px] text-gray-400">
              If client IP != VPS IP, raises IPNotWhitelistedError
            </p>
          </div>
        </div>

        {/* Verification Alert */}
        <div className="bg-indigo-950/20 border border-indigo-500/30 rounded-xl p-4 flex items-start gap-3">
          <CheckCircle2 className="w-4 h-4 text-indigo-400 flex-shrink-0 mt-0.5" />
          <div className="text-xs space-y-1">
            <p className="font-semibold text-indigo-200">
              Single-User Architecture Confirmation
            </p>
            <p className="text-indigo-300/80">
              All 6 demat accounts (Master + 5 Followers) belong to the single account owner and route through this single VPS static IP. No user-level IP pooling or reselling is required.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
