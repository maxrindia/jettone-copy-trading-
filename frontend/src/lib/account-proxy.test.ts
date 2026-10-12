import { describe, expect, it } from "vitest";
import { accountProxyHost } from "./account-proxy";

describe("accountProxyHost", () => {
  it("returns null when notes has no ProxyURL fragment", () => {
    expect(accountProxyHost("Broker: alicebluenew, Mobile: 9999999999")).toBeNull();
  });

  it("returns null for null/undefined notes", () => {
    expect(accountProxyHost(null)).toBeNull();
    expect(accountProxyHost(undefined)).toBeNull();
  });

  it("strips embedded basic-auth credentials, keeping only the host", () => {
    const notes = "Broker: alicebluenew, ProxyURL: https://user:pass@dc-mum-007.staticip.in";
    expect(accountProxyHost(notes)).toBe("dc-mum-007.staticip.in");
  });

  it("is case-insensitive on the ProxyURL: prefix, matching the backend", () => {
    expect(accountProxyHost("proxyurl: https://u:p@host.example.com")).toBe("host.example.com");
  });

  it("returns the URL unchanged when it has no credentials to strip", () => {
    expect(accountProxyHost("ProxyURL: https://dc-mum-007.staticip.in")).toBe("https://dc-mum-007.staticip.in");
  });

  it("never leaks the credential portion into the returned value", () => {
    const notes = "ProxyURL: https://secretuser:secretpass@dc-del-002.staticip.in";
    const result = accountProxyHost(notes);
    expect(result).not.toContain("secretuser");
    expect(result).not.toContain("secretpass");
  });
});
