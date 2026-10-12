import React, { useState } from "react";
import { Search, Filter, CheckCircle2, XCircle, Clock, AlertTriangle } from "lucide-react";
import { OrderStatus } from "@/types";

interface MockTrade {
  id: string;
  timestamp: string;
  masterOrderId: string;
  followerName: string;
  symbol: string;
  side: "BUY" | "SELL";
  qty: number;
  price: string;
  status: OrderStatus;
  failureReason?: string;
}

export function OrdersTab() {
  const [filter, setFilter] = useState<string>("ALL");

  // Sample trades for demonstration
  const trades: MockTrade[] = [
    {
      id: "tr-001",
      timestamp: "Today, 09:32:15",
      masterOrderId: "AB_M_982341",
      followerName: "Follower 1",
      symbol: "NIFTY24AUG24500CE",
      side: "BUY",
      qty: 50,
      price: "₹142.50",
      status: "SUCCESS",
    },
    {
      id: "tr-002",
      timestamp: "Today, 09:32:15",
      masterOrderId: "AB_M_982341",
      followerName: "Follower 2",
      symbol: "NIFTY24AUG24500CE",
      side: "BUY",
      qty: 25,
      price: "₹142.50",
      status: "SUCCESS",
    },
    {
      id: "tr-003",
      timestamp: "Today, 09:32:15",
      masterOrderId: "AB_M_982341",
      followerName: "Follower 3",
      symbol: "NIFTY24AUG24500CE",
      side: "BUY",
      qty: 50,
      price: "₹142.50",
      status: "REJECTED",
      failureReason: "INSUFFICIENT_MARGIN (Available: ₹2,100, Required: ₹7,125)",
    },
    {
      id: "tr-004",
      timestamp: "Today, 09:32:15",
      masterOrderId: "AB_M_982341",
      followerName: "Follower 4",
      symbol: "NIFTY24AUG24500CE",
      side: "BUY",
      qty: 0,
      price: "₹142.50",
      status: "SKIPPED",
      failureReason: "ZERO_LOT_RESULT (Capital ratio rounded down to 0 lots)",
    },
  ];

  const filteredTrades = trades.filter((t) => {
    if (filter === "ALL") return true;
    return t.status === filter;
  });

  const getStatusBadge = (status: OrderStatus) => {
    switch (status) {
      case "SUCCESS":
        return (
          <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
            <CheckCircle2 className="w-3 h-3" /> Success
          </span>
        );
      case "REJECTED":
        return (
          <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-rose-500/10 text-rose-400 border border-rose-500/20">
            <XCircle className="w-3 h-3" /> Rejected
          </span>
        );
      case "PENDING":
        return (
          <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-amber-500/10 text-amber-400 border border-amber-500/20">
            <Clock className="w-3 h-3" /> Pending
          </span>
        );
      case "SKIPPED":
        return (
          <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full text-[11px] font-semibold bg-gray-500/10 text-gray-400 border border-gray-500/20">
            <AlertTriangle className="w-3 h-3" /> Skipped
          </span>
        );
      default:
        return (
          <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-gray-800 text-gray-300">
            {status}
          </span>
        );
    }
  };

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
        <div>
          <h2 className="text-base font-semibold text-white">Order History & Audit Logs</h2>
          <p className="text-xs text-gray-400">
            Every copy-trade attempt recorded with idempotency keys and categorized broker responses.
          </p>
        </div>

        {/* Filter Pills */}
        <div className="flex items-center gap-1 bg-[#121826] border border-[#1e293b] p-1 rounded-lg text-xs">
          {["ALL", "SUCCESS", "REJECTED", "SKIPPED"].map((st) => (
            <button
              key={st}
              onClick={() => setFilter(st)}
              className={`px-3 py-1.5 rounded-md font-semibold transition-all ${
                filter === st
                  ? "bg-indigo-600 text-white shadow-sm"
                  : "text-gray-400 hover:text-gray-200"
              }`}
            >
              {st}
            </button>
          ))}
        </div>
      </div>

      <div className="bg-[#121826] border border-[#1e293b] rounded-xl overflow-hidden shadow-sm">
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm">
            <thead className="text-xs text-gray-400 uppercase bg-[#0b0f19] border-b border-[#1e293b]">
              <tr>
                <th className="px-4 py-3">Time</th>
                <th className="px-4 py-3">Master Order ID</th>
                <th className="px-4 py-3">Follower</th>
                <th className="px-4 py-3">Symbol</th>
                <th className="px-4 py-3">Side</th>
                <th className="px-4 py-3">Qty</th>
                <th className="px-4 py-3">Price</th>
                <th className="px-4 py-3">Status</th>
                <th className="px-4 py-3">Details / Reason</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#1e293b]">
              {filteredTrades.map((t) => (
                <tr key={t.id} className="hover:bg-[#161f32]/40 transition-colors">
                  <td className="px-4 py-3 text-xs text-gray-400 whitespace-nowrap">
                    {t.timestamp}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-indigo-400">
                    {t.masterOrderId}
                  </td>
                  <td className="px-4 py-3 font-medium text-white">{t.followerName}</td>
                  <td className="px-4 py-3 font-mono text-xs text-gray-200">{t.symbol}</td>
                  <td className="px-4 py-3">
                    <span
                      className={`text-xs font-bold font-mono px-2 py-0.5 rounded ${
                        t.side === "BUY"
                          ? "bg-emerald-500/20 text-emerald-400 border border-emerald-500/30"
                          : "bg-rose-500/20 text-rose-400 border border-rose-500/30"
                      }`}
                    >
                      {t.side}
                    </span>
                  </td>
                  <td className="px-4 py-3 font-mono text-white">{t.qty}</td>
                  <td className="px-4 py-3 font-mono text-gray-300">{t.price}</td>
                  <td className="px-4 py-3">{getStatusBadge(t.status)}</td>
                  <td className="px-4 py-3 text-xs text-gray-400 font-mono max-w-xs truncate">
                    {t.failureReason || "Executed at market"}
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
