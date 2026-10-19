import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { reloginRoute, serverBrokerLogin, serverLoginPrompt, UNSUPPORTED_RELOGIN } from "./relogin";

const read = (rel: string) => readFileSync(join(__dirname, "..", rel), "utf8");

describe("reloginRoute: the account's own broker picks the flow", () => {
  it("Groww and Angel One log in server-side (no redirect)", () => {
    for (const b of ["groww", "angelone", " GROWW ", "AngelOne"]) expect(reloginRoute(b), b).toBe("server-login");
  });

  it("Alice Blue and its legacy/blank values keep the Alice Blue OAuth flow", () => {
    for (const b of ["alicebluenew", "aliceblue", "Alice Blue", "alice_blue", "", null, undefined]) {
      expect(reloginRoute(b), String(b)).toBe("alice-oauth");
    }
  });

  it("any other broker fails closed: never sent to Alice Blue", () => {
    for (const b of ["zerodha", "dhan", "upstox", "made-up", "groww2"]) expect(reloginRoute(b), b).toBe("unsupported");
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
