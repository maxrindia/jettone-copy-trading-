import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import ReloginButton from "../components/accounts/ReloginButton";
import { InFlightGuard } from "./in-flight-guard";
import { serverBrokerLogin } from "./relogin";

const read = (rel: string) => readFileSync(join(__dirname, "..", rel), "utf8");

/** A promise whose completion the test controls. */
function deferred<T = void>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("InFlightGuard", () => {
  it("runs a task once and marks the key busy from the very first moment", async () => {
    const guard = new InFlightGuard();
    const gate = deferred();
    let runs = 0;
    const first = guard.run("acc-1", async () => {
      runs++;
      await gate.promise;
    });
    expect(guard.isBusy("acc-1")).toBe(true);                                    // busy synchronously, before any await
    gate.resolve();
    expect(await first).toBe(true);
    expect(runs).toBe(1);
    expect(guard.isBusy("acc-1")).toBe(false);
  });

  it("repeated activation while running starts at most one task (same tick and later)", async () => {
    const guard = new InFlightGuard();
    const gate = deferred();
    let runs = 0;
    const task = async () => {
      runs++;
      await gate.promise;
    };
    const results = [guard.run("a", task), guard.run("a", task), guard.run("a", task)];   // three clicks in one tick
    await Promise.resolve();
    results.push(guard.run("a", task));                                                    // and one more a moment later
    gate.resolve();
    expect(await Promise.all(results)).toEqual([true, false, false, false]);
    expect(runs).toBe(1);
  });

  it("clears the busy state after a SUCCESS and the key can be used again", async () => {
    const guard = new InFlightGuard();
    let runs = 0;
    await guard.run("a", async () => { runs++; });
    expect(guard.isBusy("a")).toBe(false);
    expect(await guard.run("a", async () => { runs++; })).toBe(true);
    expect(runs).toBe(2);
  });

  it("clears the busy state after a FAILURE (a thrown error) and still reports the error", async () => {
    const guard = new InFlightGuard();
    await expect(guard.run("a", async () => { throw new Error("boom"); })).rejects.toThrow("boom");
    expect(guard.isBusy("a")).toBe(false);
    let ran = false;
    await guard.run("a", async () => { ran = true; });
    expect(ran).toBe(true);
  });

  it("keeps different accounts independent", async () => {
    const guard = new InFlightGuard();
    const gate = deferred();
    const a = guard.run("a", () => gate.promise);
    expect(guard.isBusy("a")).toBe(true);
    expect(guard.isBusy("b")).toBe(false);
    expect(await guard.run("b", async () => {})).toBe(true);                     // another account is not blocked
    gate.resolve();
    await a;
  });

  it("notifies subscribers when a key becomes busy and idle, and stops after unsubscribe", async () => {
    const guard = new InFlightGuard();
    const seen: boolean[] = [];
    const off = guard.subscribe(() => seen.push(guard.isBusy("a")));
    await guard.run("a", async () => {});
    expect(seen).toEqual([true, false]);
    off();
    await guard.run("a", async () => {});
    expect(seen).toEqual([true, false]);
  });
});

describe("repeated Re-login activation issues at most one broker-login request", () => {
  it("through the real login helper: ten rapid activations, one POST; busy clears on success", async () => {
    const guard = new InFlightGuard();
    const gate = deferred<Response>();
    const calls: string[] = [];
    const fakeFetch = (async (url: RequestInfo | URL, init?: RequestInit) => {
      calls.push(`${init?.method} ${String(url)}`);
      return gate.promise;
    }) as typeof fetch;
    const click = () => guard.run("acc-9", async () => { await serverBrokerLogin("acc-9", fakeFetch); });
    const attempts = Array.from({ length: 10 }, click);
    expect(guard.isBusy("acc-9")).toBe(true);
    gate.resolve(new Response("{}", { status: 200, headers: { "content-type": "application/json" } }));
    const outcomes = await Promise.all(attempts);
    expect(outcomes.filter(Boolean)).toHaveLength(1);
    expect(calls).toEqual(["POST /api/v1/accounts/acc-9/broker-login"]);
    expect(guard.isBusy("acc-9")).toBe(false);
  });

  it("and when the login FAILS (401) the button is released and the next click may try again", async () => {
    const guard = new InFlightGuard();
    let posts = 0;
    const failing = (async () => {
      posts++;
      return new Response(JSON.stringify({ success: false, error: { code: "HTTP_401", message: "Groww login failed" } }), { status: 401 });
    }) as typeof fetch;
    const results: string[] = [];
    await guard.run("acc-9", async () => { const r = await serverBrokerLogin("acc-9", failing); results.push(r.ok ? "ok" : r.message); });
    expect(guard.isBusy("acc-9")).toBe(false);
    await guard.run("acc-9", async () => { const r = await serverBrokerLogin("acc-9", failing); results.push(r.ok ? "ok" : r.message); });
    expect(results).toEqual(["Groww login failed", "Groww login failed"]);
    expect(posts).toBe(2);
  });

  it("and when the engine is unreachable the busy state still clears", async () => {
    const guard = new InFlightGuard();
    const down = (async () => { throw new TypeError("offline"); }) as typeof fetch;
    await guard.run("acc-9", async () => { await serverBrokerLogin("acc-9", down); });
    expect(guard.isBusy("acc-9")).toBe(false);
  });
});

describe("ReloginButton", () => {
  const html = (props: { busy?: boolean }) =>
    renderToStaticMarkup(createElement(ReloginButton, { onRelogin: () => {}, ...props }));

  it("idle: enabled, labelled Re-login, same look as before", () => {
    const out = html({});
    expect(out).toContain("Re-login");
    expect(out).not.toMatch(/\sdisabled(=|\s|>)/);                              // the attribute (the class "disabled:..." is fine)
    expect(out).not.toContain("aria-busy");
    expect(out).toContain("bg-[#2563EB]");
  });

  it("busy: disabled, marked busy and relabelled", () => {
    const out = html({ busy: true });
    expect(out).toMatch(/\sdisabled=""/);
    expect(out).toContain('aria-busy="true"');
    expect(out).toContain("Logging in...");
    expect(out).not.toContain(">Re-login<");
  });
});

describe("both pages guard the server login before the confirm prompt and feed the busy state to the card", () => {
  const demat = read("app/(app)/demat/page.tsx");
  const strategy = read("app/(app)/group-strategy/[groupId]/page.tsx");
  const card = read("components/accounts/AccountCard.tsx");

  for (const [name, src, arg, busyProp] of [
    ["demat", demat, "card", "reloginBusy={reloginGuard.isBusy(card.id)}"],
    ["strategy", strategy, "acc", "reloginBusy={acc ? reloginGuard.isBusy(acc.id) : false}"],
  ] as const) {
    it(`${name}: busy check precedes the confirm, the login runs inside the guard, and the card gets the busy flag`, () => {
      const start = src.indexOf("const handleReconnect = async");
      const end = src.indexOf("document.body.removeChild(a);", start);
      const handler = src.slice(start, end);
      const busyAt = handler.indexOf(`reloginGuard.isBusy(${arg}.id)`);
      const confirmAt = handler.indexOf("window.confirm(serverLoginPrompt");
      const runAt = handler.indexOf(`reloginGuard.run(${arg}.id`);
      const loginAt = handler.indexOf(`serverBrokerLogin(${arg}.id)`);
      expect(busyAt).toBeGreaterThan(-1);
      expect(confirmAt).toBeGreaterThan(busyAt);                                  // a repeated click never shows a second prompt
      expect(runAt).toBeGreaterThan(confirmAt);
      expect(loginAt).toBeGreaterThan(runAt);                                     // the request is only ever made inside run()
      expect(src).toContain("useInFlight()");
      expect(src).toContain(busyProp);
      expect(src.match(/serverBrokerLogin\(/g)?.length).toBe(1);                  // no second, unguarded call site
    });
  }

  it("Alice Blue and unsupported routing are untouched by the guard", () => {
    for (const src of [demat, strategy]) {
      const start = src.indexOf("const handleReconnect = async");
      const aliceUrl = src.indexOf("ant.aliceblueonline.com", start);
      const guardRun = src.indexOf("reloginGuard.run(", start);
      expect(aliceUrl).toBeGreaterThan(guardRun);                                 // Alice path is after, and outside, the guarded branch
      expect(src.slice(start, aliceUrl)).toContain('route === "unsupported"');
    }
  });

  it("the card disables the button through ReloginButton and takes the flag as an optional prop", () => {
    expect(card).toContain("<ReloginButton onRelogin={onRelogin} busy={reloginBusy} />");
    expect(card).toContain("reloginBusy?: boolean;");
    expect(card).toContain("reloginBusy = false,");
  });
});
