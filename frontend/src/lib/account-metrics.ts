/**
 * Shape of GET /accounts/metrics, shared by the Demat and Group Strategy
 * cards so both render each account's OWN P&L the same way.
 *
 * Money is a 2-decimal string from the engine, or null when it could not be
 * verified from the broker - never a float and never a guessed value.
 */
import { formatInr } from "./money";

export type PnlStatus = "OK" | "PARTIAL" | "UNAVAILABLE";

export interface PnlComponentCheck {
  jettone: string | null;
  broker: string | null;
  diff: string | null;
  status: string;
}

export interface PnlBrokerCheck {
  status: string;
  tolerance: string | null;
  realized: PnlComponentCheck;
  unrealized: PnlComponentCheck;
  total: PnlComponentCheck;
}

export interface AccountMetrics {
  account_id: string;
  in_group: number;
  orders_count: number;
  pending_count: number;
  completed_count: number;
  rejected_count: number;
  cancelled_count: number;
  /** SUBMITTED (in flight, ambiguous) + RECONCILIATION_REQUIRED copy trades -
   * the engine couldn't confirm the outcome, so this is never folded into
   * pending_count (which implies "will resolve on its own"). See the
   * Reconciliation page for what to do about a nonzero count here. */
  needs_review_count: number;
  realized_pnl: string | null;
  unrealized_pnl: string | null;
  total_pnl: string | null;
  pnl_basis: "GROSS";
  pnl_status: PnlStatus;
  pnl_notes: string[];
  open_positions_count: number | null;
  broker_check: PnlBrokerCheck;
  trading_day: string;
  as_of: string;
}

export interface AllMetrics {
  metrics: Record<string, AccountMetrics>;
}

export const PNL_BASIS_LABEL = "Gross, before charges";

const UNAVAILABLE_CHECK: PnlComponentCheck = { jettone: null, broker: null, diff: null, status: "UNAVAILABLE" };

/** Placeholder until metrics load: every value unknown, not zero. */
export function emptyMetrics(accountId: string): AccountMetrics {
  return {
    account_id: accountId,
    in_group: 0,
    orders_count: 0,
    pending_count: 0,
    completed_count: 0,
    rejected_count: 0,
    cancelled_count: 0,
    needs_review_count: 0,
    realized_pnl: null,
    unrealized_pnl: null,
    total_pnl: null,
    pnl_basis: "GROSS",
    pnl_status: "UNAVAILABLE",
    pnl_notes: [],
    open_positions_count: null,
    broker_check: {
      status: "UNAVAILABLE",
      tolerance: null,
      realized: UNAVAILABLE_CHECK,
      unrealized: UNAVAILABLE_CHECK,
      total: UNAVAILABLE_CHECK,
    },
    trading_day: "",
    as_of: "",
  };
}

/** What a card shows: total gross P&L, or realized alone when that is all that could be verified. */
export function pnlHeadline(m: AccountMetrics): { value: string | null; caption: string } {
  if (m.total_pnl !== null) return { value: m.total_pnl, caption: PNL_BASIS_LABEL };
  if (m.realized_pnl !== null) return { value: m.realized_pnl, caption: "Realized only · gross" };
  return { value: null, caption: PNL_BASIS_LABEL };
}

/**
 * What a P&L cell shows. An unverified P&L (null) is "N/A", never "₹0.00":
 * a genuine zero is a verified value of 0.00 and still shows as ₹0.00.
 */
export function pnlDisplay(m: AccountMetrics): { text: string; unavailable: boolean; value: string | null } {
  const { value } = pnlHeadline(m);
  if (value === null) return { text: "N/A", unavailable: true, value: null };
  return { text: formatInr(value), unavailable: false, value };
}

export function pnlTooltip(m: AccountMetrics): string {
  const lines = [
    "Gross P&L · Before charges",
    `Realized ${formatInr(m.realized_pnl)} · Unrealized ${formatInr(m.unrealized_pnl)} · Total ${formatInr(m.total_pnl)}`,
  ];
  if (m.pnl_status === "UNAVAILABLE") {
    lines.push("P&L could not be verified from the broker data, so no value is shown.");
  } else if (m.pnl_status === "PARTIAL") {
    lines.push("Partially verified:");
  }
  lines.push(...m.pnl_notes);
  lines.push(`Alice Blue check: ${m.broker_check.status}`);
  return lines.join("\n");
}

/** POS: open broker positions (netQuantity != 0), or "0" when unknown. */
export function openPositionsLabel(m: AccountMetrics): string {
  return m.open_positions_count === null ? "0" : String(m.open_positions_count);
}
