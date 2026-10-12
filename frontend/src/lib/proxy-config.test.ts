import { describe, expect, it } from "vitest";
import { buildProxyUrl, parseProxyUrl, validateProxyUrlInput } from "./proxy-config";

describe("buildProxyUrl / parseProxyUrl (existing backend format)", () => {
  it("builds https://user:pass@host[:port], omitting 443", () => {
    expect(buildProxyUrl({ host: "178.18.252.165", port: "443", username: "u", password: "p" })).toBe("https://u:p@178.18.252.165");
    expect(buildProxyUrl({ host: "h.example.com", port: "8080", username: "u", password: "p" })).toBe("https://u:p@h.example.com:8080");
  });
  it("encodes special characters in credentials and round-trips them", () => {
    const url = buildProxyUrl({ host: "h.example.com", port: "443", username: "us@er", password: "p:a/ss", scheme: "https" });
    expect(parseProxyUrl(url)).toEqual({ host: "h.example.com", port: "443", username: "us@er", password: "p:a/ss", scheme: "https" });
  });
});

describe("existing http proxies keep their scheme and default port", () => {
  it("round-trips an http proxy without turning it into https", () => {
    const fields = parseProxyUrl("http://u:p@h.example.com");
    expect(fields).toMatchObject({ scheme: "http", port: "80" });
    expect(buildProxyUrl(fields!)).toBe("http://u:p@h.example.com");
    expect(buildProxyUrl({ ...fields!, port: "3128" })).toBe("http://u:p@h.example.com:3128");
  });
});

describe("validateProxyUrlInput", () => {
  it("accepts and normalises a valid https URL", () => {
    expect(validateProxyUrlInput(" https://user:pass@DC-Mum-007.staticip.in:443/ ")).toEqual({
      ok: true, url: "https://user:pass@dc-mum-007.staticip.in", host: "dc-mum-007.staticip.in",
    });
  });
  it("preserves http and a non-default port, drops path and query", () => {
    expect(validateProxyUrlInput("http://u:p@178.18.252.165:8080/x?y=1#z")).toEqual({
      ok: true, url: "http://u:p@178.18.252.165:8080", host: "178.18.252.165",
    });
  });
  it("accepts a URL with no credentials", () => {
    expect(validateProxyUrlInput("https://178.18.252.165:8443")).toEqual({ ok: true, url: "https://178.18.252.165:8443", host: "178.18.252.165" });
  });
  it("re-encodes credentials so the stored URL parses back identically", () => {
    const r = validateProxyUrlInput("https://us%40er:p%3Aass@h.example.com");
    expect(r.ok && parseProxyUrl(r.url)).toMatchObject({ username: "us@er", password: "p:ass" });
  });
  it("rejects a missing scheme or a wrong scheme", () => {
    for (const v of ["178.18.252.165:443", "user:pass@host.com", "ftp://u:p@host.com", "socks5://u:p@host.com"]) {
      expect(validateProxyUrlInput(v).ok).toBe(false);
    }
  });
  it("rejects malformed hosts, ports and credentials", () => {
    for (const v of ["https://u:p@999.1.1.1", "https://u:p@host.com:70000", "https://u:p@host.com:0", "https://u@host.com", "https://:p@host.com", "https://", "http://bad host.com", ""]) {
      expect(validateProxyUrlInput(v).ok).toBe(false);
    }
  });
});
