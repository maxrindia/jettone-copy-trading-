"use client";

import { useState } from "react";
import { useParams, useRouter } from "next/navigation";
import {
  ArrowLeft,
  RotateCw,
  Wifi,
  WifiOff,
  AlertTriangle,
  TrendingUp,
  TrendingDown,
  Layers,
  ListOrdered,
  X,
  CheckCircle2,
  XCircle,
} from "lucide-react";
import { useEngineData } from "@/lib/use-engine-data";
import { LoadingPanel, EmptyState } from "@/components/ui/DataStates";
import Button from "@/components/ui/Button";

interface ChildAccountData {
  id: string;
  display_name: string;
  alice_user_id: string;
  account_type: string;
  is_active: boolean;
  session_valid: boolean;
  is_emergency_stopped: boolean;
  multiplier: number;
  trading_enabled: boolean;
}

interface PositionItem {
  id: string;
  symbol: string;
  product: string;
  side: "BUY" | "SELL" | "CLOSED";
  quantity: number;
  pnl: number;
  ltp: number;
  avg_price: number;
  exchange: string;
  raw_product?: string;
  can_square_off: boolean;
}

interface OrderItem {
  order_id: string;
  symbol: string;
  exchange: string;
  side: string;
  quantity: number;
  price: number;
  average_price: number;
  status: string;
  product_type: string;
  order_type: string;
  timestamp: string | null;
  rejection_reason?: string | null;
}

interface ChildPositionsResponse {
  strategy_id: string;
  strategy_name: string;
  account: ChildAccountData;
  positions: PositionItem[];
  positions_count: number;
  orders?: OrderItem[];
  orders_count?: number;
}

export default function ChildAccountViewPage() {
  const params = useParams();
  const router = useRouter();
  const groupId = Array.isArray(params?.groupId) ? params.groupId[0] : (params?.groupId as string);
  const accountId = Array.isArray(params?.accountId) ? params.accountId[0] : (params?.accountId as string);

  const [activeTab, setActiveTab] = useState<"positions" | "orders">("positions");
  const [showClosed, setShowClosed] = useState(false);

  // Fetch child account details, live positions, and order book
  const {
    data,
    error,
    isLoading,
    refresh,
  } = useEngineData<ChildPositionsResponse>(
    groupId && accountId
      ? `/strategies/${groupId}/members/${accountId}/positions?include_closed=${showClosed}`
      : null,
    5000 // auto-refresh every 5s for live updates
  );

  // Square Off State
  const [squareOffModalPos, setSquareOffModalPos] = useState<PositionItem | null>(null);
  const [squaringOff, setSquaringOff] = useState(false);
  const [squareOffResult, setSquareOffResult] = useState<{ success: boolean; message: string } | null>(null);

  const handleSquareOff = async (pos: PositionItem) => {
    if (!pos.can_square_off || pos.quantity === 0 || pos.side === "CLOSED") return;
    setSquareOffModalPos(pos);
    setSquareOffResult(null);
  };

  const confirmSquareOff = async () => {
    if (!squareOffModalPos) return;
    setSquaringOff(true);
    setSquareOffResult(null);

    try {
      const resp = await fetch(`/api/v1/strategies/${groupId}/members/${accountId}/square-off`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: squareOffModalPos.symbol,
          exchange: squareOffModalPos.exchange || "NSE",
          quantity: Math.abs(squareOffModalPos.quantity),
          side: squareOffModalPos.side === "SELL" ? "SELL" : "BUY",
          product_type: squareOffModalPos.raw_product || squareOffModalPos.product,
        }),
      });

      const resData = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        throw new Error(resData.detail || "Failed to square off position");
      }

      setSquareOffResult({
        success: true,
        message: resData.message || `Square off order submitted for ${squareOffModalPos.symbol}`,
      });
      refresh();
      setTimeout(() => {
        setSquareOffModalPos(null);
        setSquareOffResult(null);
      }, 1500);
    } catch (err: unknown) {
      setSquareOffResult({
        success: false,
        message: err instanceof Error ? err.message : "Error squaring off position",
      });
    } finally {
      setSquaringOff(false);
    }
  };

  const fmtPnl = (pnl: number) => {
    const sign = pnl > 0 ? "+" : "";
    return `${sign}₹${pnl.toFixed(2)}`;
  };

  const formatTime = (iso: string | null) => {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleTimeString("en-IN", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      });
    } catch {
      return iso;
    }
  };

  // Loading State
  if (isLoading) {
    return (
      <div className="max-w-page mx-auto py-12">
        <LoadingPanel />
      </div>
    );
  }

  // Error / Unauthorized / Not Found State
  if (error || !data) {
    return (
      <div className="max-w-4xl mx-auto py-12 space-y-6">
        <div className="bg-white border border-[#E5E7EB] rounded-[10px] p-8 text-center space-y-4 shadow-sm">
          <div className="w-12 h-12 rounded-full bg-red-50 text-red-600 flex items-center justify-center mx-auto">
            <AlertTriangle className="w-6 h-6" />
          </div>
          <h2 className="text-lg font-bold text-[#111111]">Unable to load child account details</h2>
          <p className="text-xs text-[#6B7280]">
            The requested child account does not belong to this group strategy or could not be found.
          </p>
          <div className="flex items-center justify-center gap-3 pt-2">
            <Button
              variant="secondary"
              size="sm"
              onClick={() => refresh()}
              className="px-3.5"
            >
              <RotateCw className="w-3.5 h-3.5 mr-1.5" />
              Retry
            </Button>
            <Button
              variant="primary"
              size="sm"
              onClick={() => router.push(`/group-strategy/${groupId}`)}
              className="px-3.5"
            >
              <ArrowLeft className="w-4 h-4" />
              Back to Group Strategy
            </Button>
          </div>
        </div>
      </div>
    );
  }

  const { account, strategy_name, positions, orders = [] } = data;

  const totalPnl = positions.reduce((acc, p) => acc + (p.pnl || 0), 0);
  const openPositionsCount = positions.filter((p) => p.side !== "CLOSED" && p.quantity !== 0).length;

  return (
    <div className="space-y-6 max-w-page mx-auto pb-16">
      {/* ── Top Bar: Back Button, Account Name, Client ID, Status Badges ── */}
      <div className="bg-white text-[#111111] p-4 sm:p-5 rounded-[10px] border border-[#E5E7EB] shadow-sm flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center flex-wrap gap-3 sm:gap-4">
          {/* Back Button */}
          <button
            onClick={() => router.push(`/group-strategy/${groupId}`)}
            className="w-8 h-8 rounded-lg bg-[#111111] hover:bg-[#1F2937] text-white flex items-center justify-center transition-transform active:scale-95 shadow-sm"
            title="Back to Group Strategy"
          >
            <ArrowLeft className="w-4 h-4 stroke-[2.5]" />
          </button>

          {/* Account Title & Client ID */}
          <div>
            <div className="flex items-center gap-2">
              <span
                className={`w-2.5 h-2.5 rounded-full shrink-0 ${
                  account.session_valid ? "bg-green-500" : "bg-red-500"
                }`}
              />
              <h1 className="text-base sm:text-lg font-extrabold text-[#111111] tracking-tight capitalize">
                {account.display_name}
              </h1>
              <span className="font-mono text-xs bg-gray-100 text-[#374151] px-2 py-0.5 rounded-lg border border-[#E5E7EB] font-bold">
                {account.alice_user_id}
              </span>
            </div>
            <p className="text-[11px] text-[#6B7280] mt-0.5">
              Group Strategy: <span className="font-bold text-[#111111]">{strategy_name}</span>
            </p>
          </div>
        </div>

        {/* Status Badges & Summary Stats */}
        <div className="flex items-center flex-wrap gap-2.5">
          {/* Live / Offline Badge */}
          <div
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg border text-xs font-semibold ${
              account.session_valid
                ? "bg-green-50 border-green-200 text-green-700"
                : "bg-red-50 border-red-200 text-red-600"
            }`}
          >
            {account.session_valid ? <Wifi className="w-3.5 h-3.5" /> : <WifiOff className="w-3.5 h-3.5" />}
            <span>{account.session_valid ? "Status: Live" : "Status: Offline"}</span>
          </div>

          {/* Trading Enabled / Disabled */}
          <div
            className={`px-3 py-1.5 rounded-lg border text-xs font-semibold ${
              account.is_active && account.trading_enabled
                ? "bg-orange-50 border-orange-200 text-orange-700"
                : "bg-gray-100 border-gray-300 text-gray-600"
            }`}
          >
            Trading: {account.is_active && account.trading_enabled ? "Enabled" : "Disabled"}
          </div>

          {/* Multiplier Badge */}
          <div className="px-3 py-1.5 rounded-lg bg-[#F7F7F7] border border-[#E5E7EB] text-[#374151] text-xs font-bold font-mono">
            Multiplier: {account.multiplier}x
          </div>

          {/* Refresh Button */}
          <button
            onClick={() => refresh()}
            className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-[#E5E7EB] bg-white text-[#4B5563] hover:text-[#111111] hover:bg-gray-50 transition-colors shadow-sm"
            title="Refresh"
          >
            <RotateCw className="w-4 h-4" />
          </button>
        </div>
      </div>

      {/* ── Summary Stat Cards ── */}
      <div className="grid grid-cols-2 [@container_(min-width:40rem)]:grid-cols-4 gap-4">
        <div className="bg-white border border-[#E5E7EB] rounded-[10px] p-4 shadow-sm">
          <span className="text-[11px] font-bold text-[#6B7280] uppercase tracking-wider block">
            Open Positions
          </span>
          <span className="text-lg sm:text-xl font-extrabold text-[#111111] font-mono mt-1 block">
            {openPositionsCount}
          </span>
        </div>

        <div className="bg-white border border-[#E5E7EB] rounded-[10px] p-4 shadow-sm">
          <span className="text-[11px] font-bold text-[#6B7280] uppercase tracking-wider block">
            Orders Today
          </span>
          <span className="text-lg sm:text-xl font-extrabold text-[#111111] font-mono mt-1 block">
            {orders.length}
          </span>
        </div>

        <div className="bg-white border border-[#E5E7EB] rounded-[10px] p-4 shadow-sm col-span-2 sm:col-span-2">
          <span className="text-[11px] font-bold text-[#6B7280] uppercase tracking-wider block">
            Net Realized &amp; Unrealized P&amp;L
          </span>
          <div className="flex items-center gap-2 mt-1">
            <span
              className={`text-lg sm:text-xl font-extrabold font-mono ${
                totalPnl > 0 ? "text-green-600" : totalPnl < 0 ? "text-red-600" : "text-[#111111]"
              }`}
            >
              {fmtPnl(totalPnl)}
            </span>
            <span
              className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-bold ${
                totalPnl > 0
                  ? "bg-green-50 text-green-700"
                  : totalPnl < 0
                  ? "bg-red-50 text-red-700"
                  : "bg-gray-100 text-gray-600"
              }`}
            >
              {totalPnl > 0 ? (
                <TrendingUp className="w-3.5 h-3.5 mr-1" />
              ) : totalPnl < 0 ? (
                <TrendingDown className="w-3.5 h-3.5 mr-1" />
              ) : null}
              {totalPnl > 0 ? "Profit" : totalPnl < 0 ? "Loss" : "Neutral"}
            </span>
          </div>
        </div>
      </div>

      {/* ── Tab Selector: Positions vs Order Book ── */}
      <div className="flex items-center gap-2 border-b border-[#E5E7EB] pb-2">
        <button
          onClick={() => setActiveTab("positions")}
          className={`flex items-center gap-2 h-8 px-3.5 rounded-lg text-xs font-semibold transition-all ${
            activeTab === "positions"
              ? "bg-[#111111] text-white shadow-sm"
              : "bg-white text-[#6B7280] hover:text-[#111111] border border-[#E5E7EB]"
          }`}
        >
          <Layers className="w-3.5 h-3.5" />
          Positions ({positions.length})
        </button>

        <button
          onClick={() => setActiveTab("orders")}
          className={`flex items-center gap-2 h-8 px-3.5 rounded-lg text-xs font-semibold transition-all ${
            activeTab === "orders"
              ? "bg-[#111111] text-white shadow-sm"
              : "bg-white text-[#6B7280] hover:text-[#111111] border border-[#E5E7EB]"
          }`}
        >
          <ListOrdered className="w-3.5 h-3.5" />
          Order Book ({orders.length})
        </button>
      </div>

      {/* ── TAB 1: POSITIONS TABLE ── */}
      {activeTab === "positions" && (
        <div className="bg-white border border-[#E5E7EB] rounded-[10px] shadow-sm">
          <div className="px-5 py-4 border-b border-[#E5E7EB] bg-[#F7F7F7] rounded-t-[10px] flex items-center justify-between flex-wrap gap-2">
            <div className="flex items-center gap-2">
              <Layers className="w-4 h-4 text-[#9CA3AF]" />
              <h2 className="text-sm font-extrabold text-[#111111] tracking-tight">
                Live Positions
              </h2>
            </div>

            <div className="flex items-center gap-3">
              <label className="flex items-center gap-2 text-xs font-medium text-[#6B7280] cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={showClosed}
                  onChange={(e) => setShowClosed(e.target.checked)}
                  className="rounded border-gray-300 text-black focus:ring-black cursor-pointer"
                />
                <span>Show Closed (0 Qty) Positions</span>
              </label>
            </div>
          </div>

          {positions.length === 0 ? (
            <div className="p-12 text-center">
              <EmptyState
                title="No Open Positions"
                hint="There are no active open positions for this child account right now. Check the 'Order Book' tab to see pending or filled orders."
              />
            </div>
          ) : (
            <div className="overflow-x-auto rounded-b-[10px]">
              <table className="w-full text-left border-collapse">
                <thead>
                  <tr className="border-b border-[#E5E7EB] bg-[#F9FAFB] text-[11px] font-bold text-[#4B5563] uppercase tracking-wider">
                    <th className="py-3 px-4">Id</th>
                    <th className="py-3 px-4">Symbol</th>
                    <th className="py-3 px-4">Product</th>
                    <th className="py-3 px-4">B/S</th>
                    <th className="py-3 px-4 text-right">Quantity</th>
                    <th className="py-3 px-4 text-right">Pnl</th>
                    <th className="py-3 px-4 text-right">Ltp</th>
                    <th className="py-3 px-4 text-right">Avgprice</th>
                    <th className="py-3 px-4 text-center">Square Off</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#F3F4F6] text-xs font-semibold text-[#111111]">
                  {positions.map((pos) => {
                    const isClosed = pos.side === "CLOSED" || pos.quantity === 0;
                    const isPositivePnl = pos.pnl > 0;
                    const isNegativePnl = pos.pnl < 0;

                    return (
                      <tr key={pos.id} className="hover:bg-[#F9FAFB] transition-colors">
                        <td className="py-3.5 px-4 font-mono text-[#6B7280]">{pos.id}</td>
                        <td className="py-3.5 px-4">
                          <span className="font-bold text-[#111111]">{pos.symbol}</span>
                          <span className="ml-1.5 text-[10px] font-mono text-[#9CA3AF]">
                            {pos.exchange}
                          </span>
                        </td>
                        <td className="py-3.5 px-4">
                          <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-gray-100 text-gray-700 border border-gray-200">
                            {pos.product}
                          </span>
                        </td>
                        <td className="py-3.5 px-4">
                          <span
                            className={`px-2 py-0.5 rounded text-[10px] font-bold border ${
                              pos.side === "BUY"
                                ? "bg-green-50 text-green-700 border-green-200"
                                : pos.side === "SELL"
                                ? "bg-red-50 text-red-700 border-red-200"
                                : "bg-gray-100 text-gray-600 border-gray-200"
                            }`}
                          >
                            {pos.side}
                          </span>
                        </td>
                        <td className="py-3.5 px-4 text-right font-mono font-bold">
                          {pos.quantity}
                        </td>
                        <td
                          className={`py-3.5 px-4 text-right font-mono font-extrabold ${
                            isPositivePnl
                              ? "text-green-600"
                              : isNegativePnl
                              ? "text-red-600"
                              : "text-[#6B7280]"
                          }`}
                        >
                          {fmtPnl(pos.pnl)}
                        </td>
                        <td className="py-3.5 px-4 text-right font-mono text-[#111111]">
                          ₹{pos.ltp.toFixed(2)}
                        </td>
                        <td className="py-3.5 px-4 text-right font-mono text-[#111111]">
                          ₹{pos.avg_price.toFixed(2)}
                        </td>
                        <td className="py-3.5 px-4 text-center">
                          <button
                            onClick={() => handleSquareOff(pos)}
                            disabled={isClosed || !pos.can_square_off}
                            className={`px-3 py-1.5 rounded-lg text-xs font-bold transition-all shadow-sm ${
                              isClosed || !pos.can_square_off
                                ? "bg-gray-100 text-gray-400 border border-gray-200 cursor-not-allowed"
                                : "bg-red-600 hover:bg-red-700 text-white active:scale-95 cursor-pointer"
                            }`}
                          >
                            Square Off
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* ── TAB 2: ORDER BOOK TABLE ── */}
      {activeTab === "orders" && (
        <div className="bg-white border border-[#E5E7EB] rounded-[10px] shadow-sm">
          <div className="px-5 py-4 border-b border-[#E5E7EB] bg-[#F7F7F7] rounded-t-[10px] flex items-center justify-between">
            <div className="flex items-center gap-2">
              <ListOrdered className="w-4 h-4 text-[#9CA3AF]" />
              <h2 className="text-sm font-extrabold text-[#111111] tracking-tight">
                Broker Order Book
              </h2>
            </div>
            <span className="text-[11px] text-[#6B7280]">
              All placed Market &amp; Limit orders
            </span>
          </div>

          {orders.length === 0 ? (
            <div className="p-12 text-center">
              <EmptyState
                title="No Orders Found"
                hint="No orders have been submitted for this account in today's trading session."
              />
            </div>
          ) : (
            <div className="overflow-x-auto rounded-b-[10px]">
              <table className="w-full text-left border-collapse">
                <thead>
                  <tr className="border-b border-[#E5E7EB] bg-[#F9FAFB] text-[11px] font-bold text-[#4B5563] uppercase tracking-wider">
                    <th className="py-3 px-4">Order ID</th>
                    <th className="py-3 px-4">Time</th>
                    <th className="py-3 px-4">Symbol</th>
                    <th className="py-3 px-4">Side</th>
                    <th className="py-3 px-4">Type</th>
                    <th className="py-3 px-4 text-right">Quantity</th>
                    <th className="py-3 px-4 text-right">Price</th>
                    <th className="py-3 px-4 text-right">Avg Price</th>
                    <th className="py-3 px-4 text-center">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-[#F3F4F6] text-xs font-semibold text-[#111111]">
                  {orders.map((ord, idx) => {
                    const isComplete = ord.status.toUpperCase() === "COMPLETE";
                    const isPending = ord.status.toUpperCase() === "OPEN" || ord.status.toUpperCase() === "PENDING";
                    const isRejected = ord.status.toUpperCase() === "REJECTED";

                    return (
                      <tr key={ord.order_id || idx} className="hover:bg-[#F9FAFB] transition-colors">
                        <td className="py-3.5 px-4 font-mono text-[11px] text-gray-500">
                          {ord.order_id || ""}
                        </td>
                        <td className="py-3.5 px-4 font-mono text-[11px] text-[#6B7280]">
                          {formatTime(ord.timestamp)}
                        </td>
                        <td className="py-3.5 px-4">
                          <span className="font-bold text-[#111111]">{ord.symbol}</span>
                          <span className="ml-1.5 text-[10px] font-mono text-[#9CA3AF]">
                            {ord.exchange}
                          </span>
                        </td>
                        <td className="py-3.5 px-4">
                          <span
                            className={`px-2 py-0.5 rounded text-[10px] font-bold border ${
                              ord.side.toUpperCase() === "BUY"
                                ? "bg-green-50 text-green-700 border-green-200"
                                : "bg-red-50 text-red-700 border-red-200"
                            }`}
                          >
                            {ord.side.toUpperCase()}
                          </span>
                        </td>
                        <td className="py-3.5 px-4">
                          <span className="px-1.5 py-0.5 rounded bg-gray-100 text-gray-700 text-[10px] font-mono">
                            {ord.order_type}
                          </span>
                        </td>
                        <td className="py-3.5 px-4 text-right font-mono font-bold">
                          {ord.quantity}
                        </td>
                        <td className="py-3.5 px-4 text-right font-mono text-[#111111]">
                          {ord.price > 0 ? `₹${ord.price.toFixed(2)}` : "MKT"}
                        </td>
                        <td className="py-3.5 px-4 text-right font-mono text-[#111111]">
                          {ord.average_price > 0 ? `₹${ord.average_price.toFixed(2)}` : "₹0.00"}
                        </td>
                        <td className="py-3.5 px-4 text-center">
                          <span
                            className={`inline-block px-2.5 py-1 rounded-full text-[10px] font-bold border ${
                              isComplete
                                ? "bg-green-50 text-green-700 border-green-200"
                                : isPending
                                ? "bg-amber-50 text-amber-700 border-amber-200"
                                : isRejected
                                ? "bg-red-50 text-red-700 border-red-200"
                                : "bg-gray-100 text-gray-600 border-gray-200"
                            }`}
                          >
                            {ord.status}
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {/* ── Modal: Confirm Square Off ── */}
      {squareOffModalPos && (
        <div className="fixed inset-0 z-[9999] flex justify-center overflow-y-auto p-4">
          <div
            className="fixed inset-0 bg-black/40 backdrop-blur-sm animate-in fade-in"
            onClick={() => !squaringOff && setSquareOffModalPos(null)}
          />

          <div className="relative z-10 my-auto w-full max-w-md bg-white border border-[#E5E7EB] rounded-[10px] shadow-2xl p-6 space-y-4 animate-in zoom-in-95 duration-150">
            <div className="flex items-center justify-between border-b border-[#F3F4F6] pb-3">
              <div className="flex items-center gap-2 text-red-600">
                <AlertTriangle className="w-5 h-5" />
                <h3 className="text-sm font-bold text-[#111111]">Confirm Square Off</h3>
              </div>
              <button
                onClick={() => !squaringOff && setSquareOffModalPos(null)}
                className="p-1 rounded-lg text-gray-400 hover:text-black hover:bg-gray-100"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {squareOffResult ? (
              <div
                className={`p-3.5 rounded-[10px] text-xs font-semibold flex items-center gap-2 ${
                  squareOffResult.success
                    ? "bg-green-50 border border-green-200 text-green-800"
                    : "bg-red-50 border border-red-200 text-red-800"
                }`}
              >
                {squareOffResult.success ? (
                  <CheckCircle2 className="w-4 h-4 shrink-0 text-green-600" />
                ) : (
                  <XCircle className="w-4 h-4 shrink-0 text-red-600" />
                )}
                <span>{squareOffResult.message}</span>
              </div>
            ) : (
              <div className="space-y-3 text-xs text-[#374151]">
                <p>
                  Are you sure you want to square off the open position for child account{" "}
                  <span className="font-bold text-[#111111]">{account.display_name}</span>?
                </p>
                <div className="p-3 bg-[#F9FAFB] border border-[#E5E7EB] rounded-[10px] space-y-1.5 font-mono">
                  <div className="flex justify-between">
                    <span className="text-gray-500">Symbol:</span>
                    <span className="font-bold text-[#111111]">{squareOffModalPos.symbol}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-500">Side:</span>
                    <span className="font-bold text-red-600">
                      {squareOffModalPos.side === "BUY" ? "SELL (Close)" : "BUY (Close)"}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-500">Quantity:</span>
                    <span className="font-bold text-[#111111]">
                      {Math.abs(squareOffModalPos.quantity)}
                    </span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-gray-500">Product:</span>
                    <span className="font-bold text-[#111111]">{squareOffModalPos.product}</span>
                  </div>
                </div>
              </div>
            )}

            {!squareOffResult?.success && (
              <div className="flex items-center justify-end gap-2.5 pt-2">
                <button
                  type="button"
                  disabled={squaringOff}
                  onClick={() => setSquareOffModalPos(null)}
                  className="inline-flex items-center justify-center gap-1.5 h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm transition-all active:scale-[0.98] bg-gray-100 hover:bg-gray-200 text-[#374151] border-[#E5E7EB]"
                >
                  Cancel
                </button>
                <button
                  type="button"
                  disabled={squaringOff}
                  onClick={confirmSquareOff}
                  className="inline-flex items-center justify-center gap-1.5 h-8 px-3.5 rounded-lg border text-xs font-semibold shadow-sm transition-all active:scale-[0.98] bg-red-600 hover:bg-red-700 text-white disabled:opacity-50 border-red-600 hover:border-red-700"
                >
                  {squaringOff ? "Submitting..." : "Confirm Square Off"}
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
