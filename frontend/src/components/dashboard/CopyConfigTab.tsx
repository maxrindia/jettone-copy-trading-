import React, { useState } from "react";
import { Zap, Shield, Save, Check } from "lucide-react";
import { DematAccount, QuantityMode } from "@/types";

interface CopyConfigTabProps {
  accounts: DematAccount[];
}

export function CopyConfigTab({ accounts }: CopyConfigTabProps) {
  const followers = accounts.filter((a) => a.accountType === "FOLLOWER");
  const [selectedFollowerId, setSelectedFollowerId] = useState<string>(
    followers[0]?.id || ""
  );

  const [mode, setMode] = useState<QuantityMode>("FIXED");
  const [fixedQty, setFixedQty] = useState<number>(1);
  const [ratio, setRatio] = useState<number>(1.0);
  const [maxTradeVal, setMaxTradeVal] = useState<number>(50000);
  const [maxDailyLoss, setMaxDailyLoss] = useState<number>(10000);
  const [savedSuccess, setSavedSuccess] = useState<boolean>(false);

  const handleSave = () => {
    setSavedSuccess(true);
    setTimeout(() => setSavedSuccess(false), 2500);
  };

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-base font-semibold text-white">Copy Configuration</h2>
        <p className="text-xs text-gray-400">
          Configure per-follower trade replication modes, lot sizing strategies, and hard risk caps.
        </p>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left: Follower Account Selector */}
        <div className="bg-[#121826] border border-[#1e293b] rounded-xl p-5 space-y-3">
          <span className="text-xs font-semibold text-gray-400 uppercase tracking-wider">
            Select Follower Account
          </span>
          <div className="space-y-2">
            {followers.map((f) => (
              <button
                key={f.id}
                onClick={() => setSelectedFollowerId(f.id)}
                className={`w-full flex items-center justify-between p-3 rounded-lg border text-left transition-all ${
                  selectedFollowerId === f.id
                    ? "bg-indigo-600/15 border-indigo-500/40 text-white font-medium shadow-sm"
                    : "bg-[#0b0f19] border-[#1e293b] text-gray-400 hover:text-gray-200"
                }`}
              >
                <div>
                  <p className="text-sm font-semibold">{f.displayName}</p>
                  <p className="text-xs font-mono text-gray-500">{f.aliceUserId}</p>
                </div>
                <span
                  className={`w-2 h-2 rounded-full ${
                    f.isActive ? "bg-emerald-500" : "bg-gray-600"
                  }`}
                />
              </button>
            ))}
          </div>
        </div>

        {/* Right: Strategy & Risk Limits Configuration */}
        <div className="lg:col-span-2 bg-[#121826] border border-[#1e293b] rounded-xl p-6 space-y-6">
          <div className="flex items-center justify-between border-b border-[#1e293b] pb-4">
            <div>
              <h3 className="text-sm font-semibold text-white">
                Replication Strategy Rules
              </h3>
              <p className="text-xs text-gray-400">
                Applied in real time when a MasterTradeEvent is emitted
              </p>
            </div>
            {savedSuccess && (
              <span className="text-xs text-emerald-400 font-semibold flex items-center gap-1">
                <Check className="w-3.5 h-3.5" /> Saved!
              </span>
            )}
          </div>

          {/* Mode Selector */}
          <div className="space-y-3">
            <label className="text-xs font-semibold text-gray-300">
              Quantity Calculation Mode
            </label>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
              {[
                {
                  id: "FIXED",
                  title: "Fixed Quantity",
                  desc: "Replicates exact fixed number of lots/shares",
                },
                {
                  id: "RATIO",
                  title: "Copy Ratio",
                  desc: "Multiplies master filled qty by multiplier (e.g. 0.5x, 2.0x)",
                },
                {
                  id: "CAPITAL_PROPORTIONAL",
                  title: "Capital Proportional",
                  desc: "Calculates size based on follower capital vs master capital",
                },
                {
                  id: "RISK_BASED",
                  title: "Risk-Based Cap",
                  desc: "Proportional sizing hard-capped by max loss and max exposure",
                },
              ].map((item) => (
                <div
                  key={item.id}
                  onClick={() => setMode(item.id as QuantityMode)}
                  className={`p-3.5 rounded-xl border cursor-pointer transition-all ${
                    mode === item.id
                      ? "bg-indigo-600/15 border-indigo-500 text-white"
                      : "bg-[#0b0f19] border-[#1e293b] text-gray-400 hover:border-gray-700"
                  }`}
                >
                  <p className="text-xs font-bold text-gray-200">{item.title}</p>
                  <p className="text-[11px] text-gray-500 mt-1">{item.desc}</p>
                </div>
              ))}
            </div>
          </div>

          {/* Mode Parameters */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 pt-2">
            {mode === "FIXED" && (
              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-gray-300">
                  Fixed Quantity (Lot Multiplier)
                </label>
                <input
                  type="number"
                  min="1"
                  value={fixedQty}
                  onChange={(e) => setFixedQty(parseInt(e.target.value) || 1)}
                  className="w-full bg-[#0b0f19] border border-[#1e293b] rounded-lg px-3 py-2 text-sm text-white font-mono focus:outline-none focus:border-indigo-500"
                />
              </div>
            )}

            {mode === "RATIO" && (
              <div className="space-y-1.5">
                <label className="text-xs font-semibold text-gray-300">
                  Copy Ratio (Multiplier)
                </label>
                <input
                  type="number"
                  step="0.1"
                  min="0.1"
                  value={ratio}
                  onChange={(e) => setRatio(parseFloat(e.target.value) || 1.0)}
                  className="w-full bg-[#0b0f19] border border-[#1e293b] rounded-lg px-3 py-2 text-sm text-white font-mono focus:outline-none focus:border-indigo-500"
                />
              </div>
            )}
          </div>

          {/* Hard Risk Caps Section */}
          <div className="border-t border-[#1e293b] pt-5 space-y-4">
            <div className="flex items-center gap-2">
              <Shield className="w-4 h-4 text-indigo-400" />
              <h4 className="text-xs font-bold text-gray-200 uppercase tracking-wider">
                Follower Risk Caps (Safety Constraints)
              </h4>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div className="space-y-1.5">
                <label className="text-xs text-gray-400">Max Trade Value (₹ INR)</label>
                <input
                  type="number"
                  value={maxTradeVal}
                  onChange={(e) => setMaxTradeVal(parseInt(e.target.value) || 0)}
                  className="w-full bg-[#0b0f19] border border-[#1e293b] rounded-lg px-3 py-2 text-sm text-white font-mono focus:outline-none focus:border-indigo-500"
                />
              </div>

              <div className="space-y-1.5">
                <label className="text-xs text-gray-400">Max Daily Loss (₹ INR)</label>
                <input
                  type="number"
                  value={maxDailyLoss}
                  onChange={(e) => setMaxDailyLoss(parseInt(e.target.value) || 0)}
                  className="w-full bg-[#0b0f19] border border-[#1e293b] rounded-lg px-3 py-2 text-sm text-white font-mono focus:outline-none focus:border-indigo-500"
                />
              </div>
            </div>
          </div>

          <div className="flex justify-end pt-2">
            <button
              onClick={handleSave}
              className="flex items-center gap-2 px-5 py-2.5 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg text-xs font-bold shadow-lg shadow-indigo-600/30 transition-all"
            >
              <Save className="w-3.5 h-3.5" />
              Save Configuration
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
