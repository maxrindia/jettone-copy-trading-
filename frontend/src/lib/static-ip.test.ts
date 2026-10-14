import { describe, expect, it } from "vitest";
import { describeStaticIp, formatExpiry, isValidAssignedIp, isValidExpiryInput, staticIpCaption } from "./static-ip";

const counts = (active: number, untracked: number, expired: number) => ({
  available_count: active + untracked,
  active_count: active,
  untracked_count: untracked,
  expired_count: expired,
});

describe("formatExpiry", () => {
  it("formats a date-only value as DD-MMM-YYYY without timezone drift", () => {
    expect(formatExpiry("2026-10-15")).toBe("15-Oct-2026");
    expect(formatExpiry("2026-01-01")).toBe("01-Jan-2026");
    expect(formatExpiry("2026-12-31")).toBe("31-Dec-2026");
  });
  it("returns empty for missing or malformed values", () => {
    for (const v of [null, undefined, "", "15-Oct-2026", "2026-13-01"]) expect(formatExpiry(v)).toBe("");
  });
});

describe("isValidExpiryInput", () => {
  it("accepts empty (expiry is optional) and real dates", () => {
    expect(isValidExpiryInput("")).toBe(true);
    expect(isValidExpiryInput("2026-10-15")).toBe(true);
    expect(isValidExpiryInput("2028-02-29")).toBe(true);
  });
  it("rejects impossible or malformed dates", () => {
    for (const v of ["2026-02-30", "2027-02-29", "2026-13-01", "15-10-2026", "2026-1-5", "abc", "1999-01-01"]) {
      expect(isValidExpiryInput(v)).toBe(false);
    }
  });
});

describe("staticIpCaption", () => {
  it("splits counts by state", () => {
    expect(staticIpCaption(counts(2, 0, 1))).toBe("2 active · 1 expired");
    expect(staticIpCaption(counts(2, 0, 0))).toBe("2 active");
  });
  it("says so when some IPs have no recorded expiry, instead of claiming all are active", () => {
    expect(staticIpCaption(counts(1, 1, 0))).toBe("1 active · 1 no expiry");
    expect(staticIpCaption(counts(0, 2, 0))).toBe("2 no expiry");
  });
  it("handles loading and nothing configured", () => {
    expect(staticIpCaption(null)).toBe("Loading…");
    expect(staticIpCaption(counts(0, 0, 0))).toBe("No Static IP configured");
  });
});

describe("describeStaticIp", () => {
  it("active shows the expiry date and an Update action", () => {
    expect(describeStaticIp("ACTIVE", "2026-10-15")).toEqual({
      label: "Static IP Active", detail: "Expires: 15-Oct-2026", tone: "green", action: "Update",
    });
  });
  it("expired shows the date it expired and offers Renew", () => {
    expect(describeStaticIp("EXPIRED", "2026-10-15")).toEqual({
      label: "Static IP Expired", detail: "Expired: 15-Oct-2026", tone: "red", action: "Renew",
    });
  });
  it("a missing expiry is neutral, never Expired", () => {
    const d = describeStaticIp("UNTRACKED", null);
    expect(d.tone).toBe("gray");
    expect(d.label).not.toMatch(/expired/i);
    expect(d.detail).toBe("Configured · expiry not tracked");
    expect(d.action).toBe("Set expiry");
  });
});

describe("assigned public IP", () => {
  it("accepts blank (optional), IPv4 and IPv6 literals", () => {
    expect(isValidAssignedIp("")).toBe(true);
    expect(isValidAssignedIp(" 203.0.113.10 ")).toBe(true);
    expect(isValidAssignedIp("2001:db8::1")).toBe(true);
  });

  it("rejects hostnames and malformed addresses", () => {
    for (const bad of ["dc-mum-007.staticip.in", "203.0.113", "300.1.1.1", "203.0.113.10:443"]) {
      expect(isValidAssignedIp(bad)).toBe(false);
    }
  });

  it("says when a proxy has no assigned IP entered, without counting it", () => {
    const c = { available_count: 0, active_count: 0, untracked_count: 0, expired_count: 0, unmapped_count: 2 };
    expect(staticIpCaption(c)).toBe("2 IP not entered");
    expect(staticIpCaption({ ...c, active_count: 1, available_count: 1 })).toBe("1 active · 2 IP not entered");
  });
});
