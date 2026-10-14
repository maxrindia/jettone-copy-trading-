"use client";

import { FormEvent, useEffect, useState } from "react";
import { X, Zap, CircleAlert } from "lucide-react";
import Button from "@/components/ui/Button";
import { extractError } from "@/lib/use-engine-data";

interface ManualOrderResult {
  placed: boolean;
  category?: string;
  message?: string;
  master_order_id?: string | null;
  fill_price?: number;
}

interface PlaceOrderModalProps {
  open: boolean;
  observeOnly: boolean;
  onClose: () => void;
  onPlaced: () => void;
}

export default function PlaceOrderModal({ open, observeOnly, onClose, onPlaced }: PlaceOrderModalProps) {
  const [symbol, setSymbol] = useState("");
  const [exchange, setExchange] = useState("NSE");
  const [side, setSide] = useState<"BUY" | "SELL">("BUY");
  const [quantity, setQuantity] = useState("");
  const [productType, setProductType] = useState("MIS");
  const [orderType, setOrderType] = useState<"MARKET" | "LIMIT">("MARKET");
  const [price, setPrice] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !submitting) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose, submitting]);

  useEffect(() => {
    if (open) {
      setError(null);
      setFieldErrors({});
      setSymbol("");
      setQuantity("");
      setSide("BUY");
      setExchange("NSE");
      setProductType("MIS");
      setOrderType("MARKET");
      setPrice("");
    }
  }, [open]);

  if (!open) return null;

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    const fe: Record<string, string> = {};
    if (!symbol.trim()) fe.symbol = "Symbol is required.";
    const qty = Number(quantity);
    if (!quantity || !Number.isInteger(qty) || qty <= 0) fe.quantity = "Quantity must be a positive integer.";

    if (orderType === "LIMIT") {
      const p = Number(price);
      if (!price || isNaN(p) || p <= 0) {
        fe.price = "Price must be greater than 0 for LIMIT orders.";
      }
    }

    setFieldErrors(fe);
    if (Object.keys(fe).length > 0) return;

    setSubmitting(true);
    try {
      const resp = await fetch("/api/v1/broker/manual-order", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          symbol: symbol.trim().toUpperCase(),
          exchange,
          side,
          quantity: qty,
          product_type: productType,
          order_type: orderType,
          price: orderType === "LIMIT" ? Number(price) : 0,
        }),
      });
      const body = (await resp.json()) as unknown;

      if (!resp.ok) {
        setError(extractError(body) ?? `Order failed (${resp.status}).`);
        return;
      }

      const result = body as ManualOrderResult;
      if (!result.placed) {
        setError(result.message ? result.message : `Broker did not place the order (${result.category}).`);
        return;
      }

      onPlaced();
      onClose();
    } catch {
      setError("Network error while placing the order.");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="place-order-title"
    >
      <button
        aria-label="Close dialog"
        tabIndex={-1}
        onClick={() => !submitting && onClose()}
        className="absolute inset-0 bg-black/40 cursor-default"
      />

      <div className="relative w-full max-w-md bg-white rounded-[10px] shadow-xl border border-[#E5E7EB]">
        <div className="flex items-center justify-between px-5 py-4 border-b border-[#F3F4F6]">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-lg bg-red-50 flex items-center justify-center">
              <Zap className="w-4 h-4 text-red-600" aria-hidden />
            </div>
            <h2 id="place-order-title" className="text-sm font-bold text-[#111111] tracking-tight">
              Place Order — Master
            </h2>
          </div>
          <button
            onClick={() => !submitting && onClose()}
            aria-label="Close"
            className="p-1.5 rounded-lg text-[#9CA3AF] hover:text-[#111111] hover:bg-[#F3F4F6] transition-colors"
          >
            <X className="w-4 h-4" aria-hidden />
          </button>
        </div>

        <form onSubmit={handleSubmit} noValidate>
          <div className="px-5 py-5 space-y-4">
            {observeOnly && (
              <div className="flex items-start gap-2 p-3 rounded-lg bg-amber-50 border border-amber-200 text-xs font-medium text-amber-800">
                <CircleAlert className="w-3.5 h-3.5 mt-0.5 shrink-0" aria-hidden />
                Engine is in observe-only mode — orders are blocked until
                ENGINE_LIVE_TRADING_ENABLED=true.
              </div>
            )}
            {error && (
              <div
                role="alert"
                className="flex items-start gap-2 p-3 rounded-lg bg-red-50 border border-red-200 text-xs font-medium text-red-700"
              >
                <CircleAlert className="w-3.5 h-3.5 mt-0.5 shrink-0" aria-hidden />
                {error}
              </div>
            )}

            <div className="space-y-1">
              <label htmlFor="po-symbol" className="block text-[11px] font-semibold text-[#374151] uppercase tracking-wide">
                Symbol
              </label>
              <input
                id="po-symbol"
                value={symbol}
                onChange={(e) => setSymbol(e.target.value)}
                placeholder="Enter Symbol"
                disabled={submitting}
                aria-invalid={!!fieldErrors.symbol}
                className={`w-full px-3 py-2 rounded-lg border text-sm font-mono focus:outline-none focus:ring-2 focus:ring-[#111111] disabled:opacity-60 ${
                  fieldErrors.symbol ? "border-red-300" : "border-[#E5E7EB]"
                }`}
              />
              {fieldErrors.symbol && <p className="text-[11px] font-medium text-red-600">{fieldErrors.symbol}</p>}
            </div>

            <div className="grid grid-cols-2 gap-2">
              {(["BUY", "SELL"] as const).map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => setSide(s)}
                  className={`h-9 rounded-lg text-xs font-bold border transition-colors ${
                    side === s
                      ? s === "BUY"
                        ? "bg-green-600 text-white border-green-600"
                        : "bg-red-600 text-white border-red-600"
                      : "bg-white text-[#6B7280] border-[#E5E7EB] hover:bg-[#F9FAFB]"
                  }`}
                >
                  {s}
                </button>
              ))}
            </div>

            <div className="grid grid-cols-3 gap-2">
              <div className="space-y-1 col-span-1">
                <label htmlFor="po-qty" className="block text-[11px] font-semibold text-[#374151] uppercase tracking-wide">
                  Qty
                </label>
                <input
                  id="po-qty"
                  type="number"
                  min={1}
                  step={1}
                  value={quantity}
                  onChange={(e) => setQuantity(e.target.value)}
                  placeholder="Enter Quantity"
                  disabled={submitting}
                  aria-invalid={!!fieldErrors.quantity}
                  className={`w-full px-3 py-2 rounded-lg border text-sm tabular-nums focus:outline-none focus:ring-2 focus:ring-[#111111] disabled:opacity-60 ${
                    fieldErrors.quantity ? "border-red-300" : "border-[#E5E7EB]"
                  }`}
                />
                {fieldErrors.quantity && <p className="text-[11px] font-medium text-red-600">{fieldErrors.quantity}</p>}
              </div>
              <div className="space-y-1 col-span-1">
                <label htmlFor="po-exchange" className="block text-[11px] font-semibold text-[#374151] uppercase tracking-wide">
                  Exchange
                </label>
                <select
                  id="po-exchange"
                  value={exchange}
                  onChange={(e) => setExchange(e.target.value)}
                  disabled={submitting}
                  className="w-full px-3 py-2 rounded-lg border border-[#E5E7EB] text-sm focus:outline-none focus:ring-2 focus:ring-[#111111]"
                >
                  {["NSE", "NFO", "BSE", "MCX"].map((x) => (
                    <option key={x} value={x}>{x}</option>
                  ))}
                </select>
              </div>
              <div className="space-y-1 col-span-1">
                <label htmlFor="po-product" className="block text-[11px] font-semibold text-[#374151] uppercase tracking-wide">
                  Product
                </label>
                <select
                  id="po-product"
                  value={productType}
                  onChange={(e) => setProductType(e.target.value)}
                  disabled={submitting}
                  className="w-full px-3 py-2 rounded-lg border border-[#E5E7EB] text-sm focus:outline-none focus:ring-2 focus:ring-[#111111]"
                >
                  {["MIS", "NRML", "CNC"].map((p) => (
                    <option key={p} value={p}>{p}</option>
                  ))}
                </select>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-2">
              <div className="space-y-1">
                <label htmlFor="po-order-type" className="block text-[11px] font-semibold text-[#374151] uppercase tracking-wide">
                  Order Type
                </label>
                <select
                  id="po-order-type"
                  value={orderType}
                  onChange={(e) => setOrderType(e.target.value as "MARKET" | "LIMIT")}
                  disabled={submitting}
                  className="w-full px-3 py-2 rounded-lg border border-[#E5E7EB] text-sm focus:outline-none focus:ring-2 focus:ring-[#111111]"
                >
                  <option value="MARKET">MARKET</option>
                  <option value="LIMIT">LIMIT</option>
                </select>
              </div>

              {orderType === "LIMIT" && (
                <div className="space-y-1">
                  <label htmlFor="po-price" className="block text-[11px] font-semibold text-[#374151] uppercase tracking-wide">
                    Price
                  </label>
                  <input
                    id="po-price"
                    type="number"
                    step="0.05"
                    min="0.05"
                    value={price}
                    onChange={(e) => setPrice(e.target.value)}
                    placeholder="Enter Price"
                    disabled={submitting}
                    aria-invalid={!!fieldErrors.price}
                    className={`w-full px-3 py-2 rounded-lg border text-sm tabular-nums focus:outline-none focus:ring-2 focus:ring-[#111111] disabled:opacity-60 ${
                      fieldErrors.price ? "border-red-300" : "border-[#E5E7EB]"
                    }`}
                  />
                  {fieldErrors.price && <p className="text-[11px] font-medium text-red-600">{fieldErrors.price}</p>}
                </div>
              )}
            </div>

            <p className="text-[11px] text-red-600 font-medium">
              This places a REAL order on the master account and replicates it to all
              active followers through the full validation pipeline.
            </p>
          </div>

          <div className="flex justify-end gap-2 px-5 py-4 border-t border-[#F3F4F6] bg-[#F7F7F7] rounded-b-[10px]">
            <Button type="button" variant="secondary" size="sm" onClick={onClose} disabled={submitting}>
              Cancel
            </Button>
            <Button type="submit" variant="destructive" size="sm" loading={submitting} disabled={observeOnly}>
              Place Order
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
