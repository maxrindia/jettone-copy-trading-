import { describe, expect, it } from "vitest";
import { normalizeProxyHost } from "./proxy-host";

describe("normalizeProxyHost", () => {
  it("accepts a bare IPv4 address or hostname", () => {
    expect(normalizeProxyHost("178.18.252.165")).toEqual({ host: "178.18.252.165", port: null });
    expect(normalizeProxyHost("  dc-mum-1.staticip.in ")).toEqual({ host: "dc-mum-1.staticip.in", port: null });
  });

  it("drops a pasted scheme and path, keeping an explicit port", () => {
    expect(normalizeProxyHost("https://178.18.252.165:8080/")).toEqual({ host: "178.18.252.165", port: "8080" });
    expect(normalizeProxyHost("178.18.252.165:8080")).toEqual({ host: "178.18.252.165", port: "8080" });
  });

  it("rejects bad input", () => {
    for (const v of ["", "   ", "999.1.1.1", "1.2.3", "http://user:pass@host.com", "host name", "host:0", "host:70000", "-bad.com", "a..b"]) {
      expect(normalizeProxyHost(v)).toBeNull();
    }
  });
});
