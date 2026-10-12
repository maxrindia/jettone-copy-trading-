import { describe, expect, it } from "vitest";
import { safeCallbackPath } from "./callback-url";

const O = "http://178.18.252.165:8080";

describe("safeCallbackPath", () => {
  it("keeps in-app paths", () => {
    for (const p of ["/", "/orders", "/group-strategy", "/demat", "/reconciliation", "/dashboard"]) {
      expect(safeCallbackPath(p, O)).toBe(p);
    }
    expect(safeCallbackPath("/orders?status=OPEN", O)).toBe("/orders?status=OPEN");
    expect(safeCallbackPath("/group-strategy/abc-1", O)).toBe("/group-strategy/abc-1");
  });

  it("accepts a full URL only on the same origin", () => {
    expect(safeCallbackPath(`${O}/orders`, O)).toBe("/orders");
    expect(safeCallbackPath("http://178.18.252.165:9999/orders", O)).toBe("/");
    expect(safeCallbackPath("https://example.com/orders", O)).toBe("/");
  });

  it("falls back to / for missing or unsafe values", () => {
    for (const v of [
      null, undefined, "", "orders", "https://example.com", "//example.com", "///example.com/x",
      "/\\example.com", "\\\\example.com","javascript:alert(1)", "data:text/html,x",
      "/orders\n", "/auth/signin", "/auth/signin?callbackUrl=%2F", "/api/auth/signout", "/api/v1/orders", "/_next/static/x.js",
    ]) {
      expect(safeCallbackPath(v as string | null | undefined, O)).toBe("/");
    }
  });
});
