"use client";

import { useEffect, useId, useRef, useState } from "react";
import { ReceiptText, Layers, Smartphone, Monitor } from "lucide-react";
import { EmptyState } from "@/components/ui/DataStates";
import { Card } from "@/components/ui/Card";
import { TRADING_TIMEZONE } from "@/lib/dashboard";
import { describeOrderType } from "@/lib/order-type";

export interface UnifiedOrder {
  id: string;
  internal_order_id: string;
  broker_order_id: string | null;
  account_id: string;
  group_strategy_id: string | null;
  parent_order_id: string | null;
  master_broker_order_id: string | null;
  source: string;
  broker: string;
  exchange: string;
  symbol: string;
  side: string;
  order_type: string;
  product_type: string;
  quantity: number;
  filled_quantity: number;
  pending_quantity: number;
  price: number;
  trigger_price: number;
  average_price: number;
  status: string;
  broker_status: string | null;
  rejection_reason: string | null;
  created_at: string;
  updated_at: string;
}

export interface OrdersTableAccountInfo {
  account_type: string;
  display_name: string;
}

const STATUS_BADGE_BASE = "inline-block px-2.5 py-1 rounded-full text-[10px] font-extrabold border";

/**
 * REJECTED badge that reveals the broker's rejection reason on hover, keyboard
 * focus or tap. The tooltip is position:fixed (the table card scrolls
 * horizontally, which would clip an absolutely positioned one) and is kept
 * inside the viewport, so it never takes part in the table layout or changes
 * the row height.
 */
function RejectedBadge({ label, reason, className }: { label: string; reason: string; className: string }) {
  const id = useId();
  const ref = useRef<HTMLButtonElement>(null);
  const [tip, setTip] = useState<{ left: number; top?: number; bottom?: number } | null>(null);

  const show = () => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const width = Math.min(320, window.innerWidth - 16);
    const left = Math.max(8, Math.min(r.left + r.width / 2 - width / 2, window.innerWidth - width - 8));
    // Open below the badge unless the lower half of the viewport is the tighter side.
    const below = window.innerHeight - r.bottom >= r.top;
    setTip(below ? { left, top: r.bottom + 6 } : { left, bottom: window.innerHeight - r.top + 6 });
  };
  const hide = () => setTip(null);

  useEffect(() => {
    if (!tip) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && hide();
    const onDown = (e: PointerEvent) => {
      if (!ref.current?.contains(e.target as Node)) hide();
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("pointerdown", onDown);
    window.addEventListener("resize", hide);
    window.addEventListener("scroll", hide, true); // any scroller, incl. the table card
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("resize", hide);
      window.removeEventListener("scroll", hide, true);
    };
  }, [tip]);

  return (
    <>
      <button
        ref={ref}
        type="button"
        aria-describedby={tip ? id : undefined}
        onMouseEnter={show}
        onMouseLeave={hide}
        onFocus={show}
        onBlur={hide}
        onClick={show}
        className={`${STATUS_BADGE_BASE} cursor-default focus:outline-none focus-visible:ring-2 focus-visible:ring-[#111111]/30 ${className}`}
      >
        {label}
      </button>
      {tip && (
        <span
          id={id}
          role="tooltip"
          style={{ left: tip.left, top: tip.top, bottom: tip.bottom, width: Math.min(320, window.innerWidth - 16) }}
          className="pointer-events-none fixed z-50 rounded-[10px] border border-[#E5E7EB] bg-white px-3 py-2.5 text-left text-[11px] font-medium leading-snug text-[#111111] shadow-xl shadow-gray-900/10 whitespace-pre-wrap break-words"
        >
          {reason}
        </span>
      )}
    </>
  );
}

export const ORDER_STATUS_FILTERS = ["ALL", "OPEN", "COMPLETE", "PARTIAL", "REJECTED", "CANCELLED"] as const;

/**
 * The unified orders table shell - shared by the cross-account Order
 * Manager page and the single-account order history page reached from the
 * "View" (eye) action on Demat Accounts / Group Strategy child cards. Kept
 * as one component so both surfaces render orders identically.
 */
export default function OrdersTable({
  orders,
  accountMap,
  showDate = false,
}: {
  orders: UnifiedOrder[];
  accountMap: Map<string, OrdersTableAccountInfo>;
  showDate?: boolean;
}) {
  if (orders.length === 0) {
    return (
      <EmptyState
        icon={<ReceiptText className="w-6 h-6 text-[#D1D5DB]" aria-hidden />}
        title="No orders found"
        hint="Orders placed from Jettone UI or the Alice Blue Mobile app will appear here in real-time."
      />
    );
  }

  return (
    <Card className="overflow-x-auto p-0 border border-[#E5E7EB] rounded-[10px] shadow-sm">
      <table
        className={`w-full text-left text-xs text-[#4B5563] border-collapse ${showDate ? "min-w-[1140px]" : "min-w-[1020px]"}`}
      >
        <thead className="bg-[#F9FAFB] text-[#4B5563] uppercase text-[10px] font-extrabold tracking-wider border-b border-[#E5E7EB]">
          <tr>
            {showDate && <th className="py-3 px-4 whitespace-nowrap">Date</th>}
            <th className="py-3 px-4 whitespace-nowrap">Time</th>
            <th className="py-3 px-4">Source</th>
            <th className="py-3 px-4">Account</th>
            <th className="py-3 px-4">Symbol</th>
            <th className="py-3 px-4">Type</th>
            <th className="py-3 px-4">Side</th>
            <th className="py-3 px-4 text-right whitespace-nowrap">Req / Fill / Pend</th>
            <th className="py-3 px-4 text-right">Price</th>
            <th className="py-3 px-4 text-center">Status</th>
            {/* w-[1%]: this column keeps to its content. The rejection reason inside it is a
                wide, wrappable block, which would otherwise make the auto table layout hand
                this column most of any extra width (e.g. when the sidebar is collapsed). */}
            <th className="py-3 px-4 w-[1%] whitespace-nowrap">Broker Order ID</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-[#F3F4F6] bg-white font-medium text-[#111111]">
          {orders.map((o) => {
            const acc = accountMap.get(o.account_id);
            const isMaster = acc?.account_type === "MASTER" || o.master_broker_order_id === null;
            const isMobile = o.source === "BROKER_EXTERNAL";

            const isComplete = o.status.toUpperCase() === "COMPLETE";
            const isOpen = o.status.toUpperCase() === "OPEN";
            const isPartial = o.status.toUpperCase() === "PARTIAL";
            const isRejected = o.status.toUpperCase() === "REJECTED";
            const isCancelled = o.status.toUpperCase() === "CANCELLED";

            // Order type label (display only; see lib/order-type.ts): native
            // SL / SL-M, or a triggered SL the broker now reports as LIMIT/MARKET.
            const orderType = describeOrderType(o.order_type, o.trigger_price);
            const isAnyStop = orderType.isStop;

            return (
              <tr key={o.id} className="hover:bg-[#F9FAFB] transition-colors">
                {/* Date (order-history surfaces only) */}
                {showDate && (
                  <td className="py-3.5 px-4 text-[11px] text-[#6B7280] whitespace-nowrap">
                    {new Date(o.created_at).toLocaleDateString("en-GB", {
                      day: "2-digit",
                      month: "short",
                      year: "numeric",
                      timeZone: TRADING_TIMEZONE,
                    })}
                  </td>
                )}

                {/* Time (IST, 24-hour) */}
                <td className="py-3.5 px-4 text-[11px] text-[#6B7280] whitespace-nowrap tabular-nums">
                  {new Date(o.created_at).toLocaleTimeString("en-GB", {
                    hour: "2-digit",
                    minute: "2-digit",
                    second: "2-digit",
                    hourCycle: "h23",
                    timeZone: TRADING_TIMEZONE,
                  })}
                </td>

                {/* Source */}
                <td className="py-3.5 px-4 whitespace-nowrap">
                  {isMobile ? (
                    <span className="inline-flex items-center gap-1 whitespace-nowrap text-[10px] font-bold text-[#7C3AED]">
                      <Smartphone className="w-3 h-3 shrink-0" />
                      Alice Blue Mobile
                    </span>
                  ) : o.source === "SYSTEM" ? (
                    <span className="inline-flex items-center gap-1 text-[10px] font-bold text-gray-700">
                      <Layers className="w-3 h-3 shrink-0" />
                      System Copy
                    </span>
                  ) : (
                    <span className="inline-flex items-center gap-1 text-[10px] font-bold text-[#111111]">
                      <Monitor className="w-3 h-3 shrink-0" />
                      Jettone UI
                    </span>
                  )}
                </td>

                {/* Account */}
                <td className="py-3.5 px-4">
                  <div className="flex items-center gap-1.5">
                    <span className="font-bold text-[#111111]">
                      {acc?.display_name ?? o.account_id.slice(0, 8)}
                    </span>
                    {isMaster ? (
                      <span
                        className="w-4 h-4 flex items-center justify-center rounded text-[9px] font-bold bg-orange-100 text-orange-800"
                        title="Master"
                      >
                        M
                      </span>
                    ) : (
                      <span
                        className="w-4 h-4 flex items-center justify-center rounded text-[9px] font-bold bg-gray-100 text-gray-600"
                        title="Child"
                      >
                        C
                      </span>
                    )}
                  </div>
                </td>

                {/* Symbol */}
                <td className="py-3.5 px-4">
                  <div className="flex items-baseline gap-1 min-w-0">
                    <span className="font-bold text-[#111111] truncate max-w-[180px]" title={o.symbol}>
                      {o.symbol}
                    </span>
                    <span className="shrink-0 text-[10px] text-[#9CA3AF]">{o.exchange}</span>
                  </div>
                </td>

                {/* Type */}
                <td className="py-3.5 px-4">
                  <span
                    className={`whitespace-nowrap px-2 py-0.5 rounded text-[10px] font-bold border ${
                      orderType.kind === "MARKET"
                        ? "bg-green-50 text-green-700 border-green-200"
                        : orderType.kind === "LIMIT"
                        ? "bg-amber-50 text-amber-700 border-amber-200"
                        : isAnyStop
                        ? "bg-orange-50 text-orange-700 border-orange-200"
                        : "bg-[#F7F7F7] text-[#4B5563] border-[#E5E7EB]"
                    }`}
                  >
                    {orderType.label}
                  </span>
                </td>

                {/* Side */}
                <td className="py-3.5 px-4">
                  <span
                    className={`inline-flex items-center font-bold px-2 py-0.5 rounded-md text-[10px] border ${
                      o.side === "BUY"
                        ? "text-green-700 bg-green-50 border-green-200"
                        : "text-red-700 bg-red-50 border-red-200"
                    }`}
                  >
                    {o.side}
                  </span>
                </td>

                {/* Quantities: requested / filled / pending, one line */}
                <td className="py-3.5 px-4 text-right tabular-nums">
                  <div className="inline-flex items-baseline gap-1 whitespace-nowrap">
                    <span className="font-bold text-[#111111]">{o.quantity}</span>
                    <span className="text-[#9CA3AF]">/</span>
                    <span className="text-green-600 font-bold">{o.filled_quantity}</span>
                    <span className="text-[#9CA3AF]">/</span>
                    <span className="text-amber-600 font-bold">{o.pending_quantity}</span>
                  </div>
                </td>

                {/* Price — order price, trigger (SL/SL-M), fill price */}
                <td className="py-3.5 px-4 text-right tabular-nums">
                  <div className="flex flex-col items-end gap-0.5">
                    <span className="font-bold text-[#111111]">
                      {o.price > 0 ? `₹${o.price.toFixed(2)}` : "MKT"}
                    </span>
                    {isAnyStop && o.trigger_price > 0 && (
                      <span className="whitespace-nowrap text-[10px] text-orange-700 font-medium tabular-nums">
                        Trig ₹{o.trigger_price.toFixed(2)}
                      </span>
                    )}
                    {o.average_price > 0 && (
                      <span className="whitespace-nowrap text-[10px] text-green-600 font-medium tabular-nums">
                        Fill ₹{o.average_price.toFixed(2)}
                      </span>
                    )}
                  </div>
                </td>

                {/* Status */}
                <td className="py-3.5 px-4 text-center">
                  {isRejected && o.rejection_reason ? (
                    <RejectedBadge label={o.status} reason={o.rejection_reason} className="bg-red-50 text-red-700 border-red-200" />
                  ) : (
                    <span
                      className={`${STATUS_BADGE_BASE} ${
                        isComplete
                        ? "bg-green-50 text-green-700 border-green-200"
                        : isOpen
                        ? "bg-[#F7F7F7] text-[#374151] border-[#E5E7EB]"
                        : isPartial
                        ? "bg-amber-50 text-amber-700 border-amber-200"
                        : isRejected
                        ? "bg-red-50 text-red-700 border-red-200"
                        : isCancelled
                        ? "bg-gray-100 text-gray-600 border-gray-200"
                        : "bg-gray-100 text-gray-600 border-gray-200"
                      }`}
                    >
                      {o.status}
                    </span>
                  )}
                </td>

                {/* Broker Order ID - always one line (the rejection reason lives in the status tooltip) */}
                <td className="py-3.5 px-4 text-[11px] text-[#6B7280] whitespace-nowrap">{o.broker_order_id || ""}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </Card>
  );
}
