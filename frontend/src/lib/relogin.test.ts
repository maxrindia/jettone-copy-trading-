import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import {
  extractRequestToken,
  reloginRoute,
  serverBrokerLogin,
  serverLoginPrompt,
  UNSUPPORTED_RELOGIN,
  zerodhaInteractiveLogin,
} from "./relogin";

const read = (rel: string) => readFileSync(join(__dirname, "..", rel), "utf8");

describe("reloginRoute: the account's own broker picks the flow", () => {
  it("Groww and Angel One log in server-side (no redirect)", () => {
    for (const b of ["groww", "angelone", " GROWW ", "AngelOne"]) expect(reloginRoute(b), b).toBe("server-login");
  });

  it("Zerodha uses the interactive Kite login, never the server-side or Alice flow", () => {
    for (const b of ["zerodha", " ZERODHA "]) expect(reloginRoute(b), b).toBe("interactive-login");
  });

  it("Alice Blue and its legacy/blank values keep the Alice Blue OAuth flow", () => {
    for (const b of ["alicebluenew", "aliceblue", "Alice Blue", "alice_blue", "", null, undefined]) {
      expect(reloginRoute(b), String(b)).toBe("alice-oauth");
    }
  });

  it("any other broker fails closed: never sent to Alice Blue", () => {
    for (const b of ["dhan", "upstox", "made-up", "groww2"]) expect(reloginRoute(b), b).toBe("unsupported");
    expect(UNSUPPORTED_RELOGIN).toContain("never sent to another broker");
  });
});

describe("serverBrokerLogin", () => {
  const fakeFetch = (status: number, body: unknown, calls: string[] = []) =>
    (async (url: RequestInfo | URL, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${String(url)}`);
      return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
    }) as typeof fetch;

  it("POSTs only to the engine's broker-login route for that account", async () => {
    const calls: string[] = [];
    const res = await serverBrokerLogin("acc-123", fakeFetch(200, { id: "acc-123", session_valid: true }, calls));
    expect(res).toEqual({ ok: true });
    expect(calls).toEqual(["POST /api/v1/accounts/acc-123/broker-login"]);
    expect(calls.join(" ")).not.toContain("aliceblueonline");
  });

  it("returns the engine's own message on failure (e.g. Groww's daily approval missing)", async () => {
    const res = await serverBrokerLogin(
      "acc-1",
      fakeFetch(401, { success: false, error: { code: "HTTP_401", message: "Groww login failed: Has today's approval been given?" } }),
    );
    expect(res).toEqual({ ok: false, status: 401, message: "Groww login failed: Has today's approval been given?" });
  });

  it("never throws, even when the engine is unreachable", async () => {
    const boom = (async () => {
      throw new TypeError("network down");
    }) as typeof fetch;
    const res = await serverBrokerLogin("acc-1", boom);
    expect(res.ok).toBe(false);
  });

  it("prompts with Groww's daily approval requirement", () => {
    expect(serverLoginPrompt("Vikash-1", "groww")).toContain("Groww Cloud");
    expect(serverLoginPrompt("A", "angelone")).toContain("Angel One");
  });
});

describe("both Re-login handlers branch on the account's broker BEFORE the Alice Blue URL", () => {
  const pages = {
    demat: { src: read("app/(app)/demat/page.tsx"), arg: "card" },
    strategy: { src: read("app/(app)/group-strategy/[groupId]/page.tsx"), arg: "acc" },
  };

  for (const [name, { src, arg }] of Object.entries(pages)) {
    it(`${name}: routes by ${arg}.broker and only the Alice path builds the Alice Blue URL`, () => {
      const start = src.indexOf("const handleReconnect = async");
      const end = src.indexOf("};", src.indexOf("document.body.removeChild(a);", start));
      const handler = src.slice(start, end);
      const routeAt = handler.indexOf(`reloginRoute(${arg}.broker)`);
      const unsupportedAt = handler.indexOf('route === "unsupported"');
      const serverAt = handler.indexOf('route === "server-login"');
      const aliceUrlAt = handler.indexOf("ant.aliceblueonline.com");
      expect(routeAt).toBeGreaterThan(-1);
      expect(unsupportedAt).toBeGreaterThan(routeAt);
      expect(serverAt).toBeGreaterThan(routeAt);
      expect(aliceUrlAt).toBeGreaterThan(Math.max(unsupportedAt, serverAt));      // Alice URL only after both early returns
      expect(handler).toContain(`serverBrokerLogin(${arg}.id)`);
      // the server-login branch returns before any Alice Blue step
      const serverBranch = handler.slice(serverAt, aliceUrlAt);
      expect(serverBranch).toContain("return;");
    });
  }

  it("the demat card type now carries the account's broker", () => {
    const demat = pages.demat.src;
    const iface = demat.slice(demat.indexOf("interface DematCardItem {"), demat.indexOf("}", demat.indexOf("interface DematCardItem {")));
    expect(iface).toContain("broker?: string | null;");
  });
});


describe("zerodha interactive login", () => {
  const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

  it("extracts a request_token from a bare token or the redirect URL, and refuses anything else", () => {
    expect(extractRequestToken(" AbC123 ")).toBe("AbC123");
    expect(extractRequestToken("https://x.example/cb?status=success&request_token=Tok9&action=login")).toBe("Tok9");
    for (const bad of ["", "  ", null, undefined, "a b", "tok;en", "x".repeat(300), "https://x.example/cb?status=error"]) {
      expect(extractRequestToken(bad as string), String(bad)).toBeNull();
    }
  });

  it("fetches the login URL, opens it, then posts only the request_token to this account's complete route", async () => {
    const calls: string[] = [];
    let posted = "";
    const f = (async (url: RequestInfo | URL, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${String(url)}`);
      if (String(url).endsWith("broker-login-url")) return json(200, { login_url: "https://kite.zerodha.com/connect/login?v=3&api_key=K" });
      posted = String(init?.body);
      return json(200, { id: "a1" });
    }) as typeof fetch;
    const opened: string[] = [];
    const res = await zerodhaInteractiveLogin("a1", (u) => opened.push(u), () => "Tok9", f);
    expect(res).toEqual({ ok: true });
    expect(opened).toEqual(["https://kite.zerodha.com/connect/login?v=3&api_key=K"]);
    expect(calls).toEqual(["GET /api/v1/accounts/a1/broker-login-url", "POST /api/v1/accounts/a1/broker-login-complete"]);
    expect(JSON.parse(posted)).toEqual({ request_token: "Tok9" });
  });

  it("a cancelled prompt sends nothing and says so", async () => {
    const calls: string[] = [];
    const f = (async (url: RequestInfo | URL) => {
      calls.push(String(url));
      return json(200, { login_url: "https://kite.zerodha.com/x" });
    }) as typeof fetch;
    const res = await zerodhaInteractiveLogin("a1", () => undefined, () => null, f);
    expect(res).toMatchObject({ ok: false, status: 0 });
    expect((res as { message: string }).message).toContain("cancelled");
    expect(calls).toEqual(["/api/v1/accounts/a1/broker-login-url"]);
  });

  it("returns the engine's message when the login URL or the exchange fails, and never throws", async () => {
    const down = (async () => json(409, { detail: "Zerodha credentials missing" })) as typeof fetch;
    expect(await zerodhaInteractiveLogin("a1", () => undefined, () => "T", down)).toMatchObject({ ok: false, status: 409 });
    const f = (async (url: RequestInfo | URL) =>
      String(url).endsWith("url") ? json(200, { login_url: "https://kite.zerodha.com/x" }) : json(401, { detail: "Zerodha login failed" })) as typeof fetch;
    expect(await zerodhaInteractiveLogin("a1", () => undefined, () => "T", f)).toMatchObject({ ok: false, status: 401 });
    const boom = (async () => {
      throw new Error("offline");
    }) as typeof fetch;
    expect(await zerodhaInteractiveLogin("a1", () => undefined, () => "T", boom)).toMatchObject({ ok: false, status: 0 });
  });
});
