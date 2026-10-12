import { describe, expect, it } from "vitest";
import { isUnauthenticated } from "./session-guard";

const resp = (status: number, redirected = false, url = "http://localhost:3000/api/v1/strategies") => {
  const r = new Response("{}", { status });
  Object.defineProperty(r, "redirected", { value: redirected });
  Object.defineProperty(r, "url", { value: url });
  return r;
};

describe("isUnauthenticated", () => {
  it("is true for a 401", () => {
    expect(isUnauthenticated(resp(401))).toBe(true);
  });
  it("is true when fetch was redirected to the sign-in page", () => {
    expect(isUnauthenticated(resp(200, true, "http://localhost:3000/auth/signin?callbackUrl=%2Fx"))).toBe(true);
  });
  it("is false for normal and engine-error responses", () => {
    expect(isUnauthenticated(resp(200))).toBe(false);
    expect(isUnauthenticated(resp(503))).toBe(false);
    expect(isUnauthenticated(resp(200, true, "http://localhost:3000/api/v1/other"))).toBe(false);
  });
});
