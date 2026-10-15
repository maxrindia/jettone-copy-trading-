import { describe, expect, it } from "vitest";
import { describeOrderType } from "./order-type";

describe("describeOrderType", () => {
  it("shows a triggered SL (stored as LIMIT with a trigger price) as SL (triggered)", () => {
    // 2026-10-15 incident: master BUY stored LIMIT, price 105.00, trigger 104.90
    expect(describeOrderType("LIMIT", 104.9)).toMatchObject({ label: "SL (triggered)", kind: "STOP", isStop: true, triggered: true });
  });

  it("shows a triggered SL-M (stored as MARKET with a trigger price) as SL-M (triggered)", () => {
    expect(describeOrderType("MARKET", 50)).toMatchObject({ label: "SL-M (triggered)", triggered: true });
  });

  it("keeps ordinary LIMIT and MARKET labels when there is no trigger price", () => {
    expect(describeOrderType("LIMIT", 0)).toMatchObject({ label: "LIMIT", kind: "LIMIT", isStop: false, triggered: false });
    expect(describeOrderType("MARKET", 0)).toMatchObject({ label: "MARKET", kind: "MARKET", triggered: false });
    expect(describeOrderType("LIMIT", null)).toMatchObject({ label: "LIMIT" });
    expect(describeOrderType("market", undefined)).toMatchObject({ label: "MARKET" });
    expect(describeOrderType("LIMIT", Number.NaN)).toMatchObject({ label: "LIMIT" });
  });

  it("keeps native SL and SL-M labels (any spelling)", () => {
    expect(describeOrderType("SL", 104.9)).toMatchObject({ label: "SL", isStop: true, triggered: false });
    expect(describeOrderType("SL-M", 10)).toMatchObject({ label: "SL-M", isStop: true });
    expect(describeOrderType("SLM", 0)).toMatchObject({ label: "SL-M" });
    expect(describeOrderType("stoploss", 0)).toMatchObject({ label: "SL" });
  });

  it("passes unknown types through unchanged", () => {
    expect(describeOrderType("BO", 0)).toMatchObject({ label: "BO", kind: "OTHER" });
    expect(describeOrderType("", 0).label).toBe("");
  });
});
