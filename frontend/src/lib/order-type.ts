/**
 * Display label for an order's type. Display only: the stored order_type is
 * never changed.
 *
 * Alice Blue reports a native SL order as LIMIT (or MARKET for SL-M) once it
 * has triggered, and the engine stores the broker's current type. Such a row
 * still carries the trigger price it was placed with, while a plain LIMIT or
 * MARKET order never has one, so LIMIT/MARKET + a positive trigger price is
 * shown as "SL (triggered)".
 */

export type OrderTypeKind = "MARKET" | "LIMIT" | "STOP" | "OTHER";

export interface OrderTypeDisplay {
  label: string;
  kind: OrderTypeKind;
  /** A stop order (native SL/SL-M, or one that already triggered) - its trigger price is worth showing. */
  isStop: boolean;
  triggered: boolean;
}

export function describeOrderType(orderType: string | null | undefined, triggerPrice?: number | null): OrderTypeDisplay {
  const upper = (orderType ?? "").toUpperCase();
  // Case- and hyphen-safe: Jettone-placed stop-market orders are stored as
  // "SL-M", broker-reported ones as "SLM".
  const key = upper.replace(/[^A-Z]/g, "");
  if (key === "SL" || key === "STOPLOSS") return { label: "SL", kind: "STOP", isStop: true, triggered: false };
  if (key === "SLM" || key === "STOPLOSSMARKET") return { label: "SL-M", kind: "STOP", isStop: true, triggered: false };

  const hasTrigger = typeof triggerPrice === "number" && Number.isFinite(triggerPrice) && triggerPrice > 0;
  if ((key === "LIMIT" || key === "MARKET") && hasTrigger) {
    return { label: key === "MARKET" ? "SL-M (triggered)" : "SL (triggered)", kind: "STOP", isStop: true, triggered: true };
  }
  if (key === "LIMIT") return { label: "LIMIT", kind: "LIMIT", isStop: false, triggered: false };
  if (key === "MARKET") return { label: "MARKET", kind: "MARKET", isStop: false, triggered: false };
  return { label: upper || (orderType ?? ""), kind: "OTHER", isStop: false, triggered: false };
}
