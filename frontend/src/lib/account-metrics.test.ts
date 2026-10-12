/**
 * The Demat and Group Strategy cards render P&L and POS through these
 * helpers (no React rendering here - see alice-blue-login.test.ts for why).
 */
import { describe, expect, it } from "vitest";
import { emptyMetrics, openPositionsLabel, pnlHeadline, pnlTooltip, type AccountMetrics } from "./account-metrics";
import { formatInr } from "./money";

function metrics(overrides: Partial<AccountMetrics>): AccountMetrics {
  return { ...emptyMetrics("acc"), pnl_status: "OK", ...overrides };
}

describe("pnlHeadline", () => {
  it("shows total gross P&L with the basis caption", () => {
    const m = metrics({ realized_pnl: "-1.52", unrealized_pnl: "0.00", total_pnl: "-1.52" });
    expect(pnlHeadline(m)).toEqual({ value: "-1.52", caption: "Gross, before charges" });
    expect(formatInr(pnlHeadline(m).value)).toBe("−₹1.52");
  });

  it("falls back to realized only, and says so, when unrealized could not be verified", () => {
    const m = metrics({ pnl_status: "PARTIAL", realized_pnl: "-1.48", unrealized_pnl: null, total_pnl: null });
    expect(pnlHeadline(m)).toEqual({ value: "-1.48", caption: "Realized only · gross" });
  });

  it("shows no value when nothing could be verified", () => {
    const m = emptyMetrics("acc");
    expect(pnlHeadline(m).value).toBeNull();
    expect(formatInr(pnlHeadline(m).value)).toBe("₹0.00");
  });

  it("keeps master and child values independent", () => {
    const master = metrics({ account_id: "master", total_pnl: "-1.52" });
    const child = metrics({ account_id: "child", total_pnl: "-1.48" });
    expect(formatInr(pnlHeadline(master).value)).toBe("−₹1.52");
    expect(formatInr(pnlHeadline(child).value)).toBe("−₹1.48");
  });
});

describe("pnlTooltip", () => {
  it("labels the figure gross and lists the breakdown and broker check", () => {
    const m = metrics({ realized_pnl: "-1.52", unrealized_pnl: "0.00", total_pnl: "-1.52" });
    m.broker_check = { ...m.broker_check, status: "MATCH" };
    const tip = pnlTooltip(m);
    expect(tip).toContain("Gross P&L · Before charges");
    expect(tip).toContain("Realized −₹1.52 · Unrealized ₹0.00 · Total −₹1.52");
    expect(tip).toContain("Alice Blue check: MATCH");
  });

  it("explains that an unavailable value is only a placeholder", () => {
    expect(pnlTooltip(emptyMetrics("acc"))).toContain("shown as a placeholder, not a confirmed value");
  });
});

describe("openPositionsLabel", () => {
  it("shows the broker open-position count, or 0 when unknown", () => {
    expect(openPositionsLabel(metrics({ open_positions_count: 0 }))).toBe("0");
    expect(openPositionsLabel(metrics({ open_positions_count: 2 }))).toBe("2");
    expect(openPositionsLabel(metrics({ open_positions_count: null }))).toBe("0");
  });
});
