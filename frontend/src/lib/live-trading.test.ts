import { describe, expect, it } from "vitest";
import {
  liveTradingBadge,
  liveTradingRequest,
  unwrapLiveTrading,
  type LiveTradingState,
} from "./live-trading";

const base: LiveTradingState = {
  enabled: true,
  source: "runtime",
  updated_at: null,
  updated_by: "ui",
  master_trading_enabled: true,
  emergency_stop_active: false,
};

describe("liveTradingBadge", () => {
  it("is unknown until the state has loaded", () => {
    expect(liveTradingBadge(null).tone).toBe("unknown");
  });

  it("shows ON only when nothing pauses copying", () => {
    expect(liveTradingBadge(base)).toMatchObject({ tone: "on", label: "Live trading: ON", note: null });
  });

  it("shows OFF when disabled", () => {
    expect(liveTradingBadge({ ...base, enabled: false })).toMatchObject({ tone: "off", label: "Live trading: OFF" });
  });

  it("explains an ON that is paused by the emergency stop or the master switch", () => {
    expect(liveTradingBadge({ ...base, emergency_stop_active: true })).toMatchObject({ tone: "paused" });
    expect(liveTradingBadge({ ...base, master_trading_enabled: false }).note).toMatch(/Master trading switch/);
  });

  it("treats an unreadable state as OFF, never ON", () => {
    expect(liveTradingBadge({ ...base, enabled: true, source: "error" }).tone).toBe("off");
  });
});

describe("liveTradingRequest", () => {
  it("confirms only when turning ON", () => {
    expect(liveTradingRequest(true)).toMatchObject({ enabled: true, confirm: true });
    expect(liveTradingRequest(false)).toMatchObject({ enabled: false, confirm: false });
  });
});

describe("unwrapLiveTrading", () => {
  it("accepts the API envelope or a bare state", () => {
    expect(unwrapLiveTrading({ success: true, data: base })).toEqual(base);
    expect(unwrapLiveTrading(base)).toEqual(base);
    expect(unwrapLiveTrading(null)).toBeNull();
    expect(unwrapLiveTrading({ success: false })).toBeNull();
  });
});
