/**
 * Regression coverage for the Alice Blue re-login session-reuse warning.
 *
 * Alice Blue's OAuth web login only documents an `appcode` parameter — there
 * is no supported way for Jettone to force a fresh login or log out an
 * existing browser session (confirmed against
 * https://ant.aliceblueonline.com/productdocumentation/Vendors/). If the
 * browser already holds an Alice Blue session for a different user, Alice
 * Blue silently reuses it. confirmAliceBlueLogin() is the preventive warning
 * shown before redirecting, so the user can log out of Alice Blue themselves
 * first instead of hitting the backend's account-mismatch rejection blind.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { confirmAliceBlueLogin, consumeReconnectTarget, setReconnectTarget } from "./alice-blue-login";

/** Minimal in-memory Storage implementation for stubbing window.{session,local}Storage. */
class MemoryStorage {
  private store = new Map<string, string>();
  getItem(key: string): string | null {
    return this.store.has(key) ? this.store.get(key)! : null;
  }
  setItem(key: string, value: string): void {
    this.store.set(key, value);
  }
  removeItem(key: string): void {
    this.store.delete(key);
  }
}

function stubWindow(confirmImpl: () => boolean = () => true) {
  const sessionStorage = new MemoryStorage();
  const localStorage = new MemoryStorage();
  // @ts-expect-error -- minimal window stub used only by these tests
  global.window = { confirm: vi.fn(confirmImpl), sessionStorage, localStorage };
  return { sessionStorage, localStorage };
}

afterEach(() => {
  // @ts-expect-error -- minimal window stub used only by these tests
  delete global.window;
});

describe("confirmAliceBlueLogin", () => {
  it("is a no-op (returns true) outside the browser, e.g. during SSR", () => {
    expect(confirmAliceBlueLogin("2679228")).toBe(true);
  });

  it("warns with the target account id and returns true when the user proceeds (correct-user path)", () => {
    const confirmSpy = vi.fn().mockReturnValue(true);
    // @ts-expect-error -- minimal window stub used only by this test
    global.window = { confirm: confirmSpy };

    const result = confirmAliceBlueLogin("2679228");

    expect(result).toBe(true);
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(confirmSpy.mock.calls[0][0]).toContain("2679228");
  });

  it("returns false when the user cancels, so callers can bail out before redirecting (wrong-session guard)", () => {
    const confirmSpy = vi.fn().mockReturnValue(false);
    // @ts-expect-error -- minimal window stub used only by this test
    global.window = { confirm: confirmSpy };

    expect(confirmAliceBlueLogin("2679228")).toBe(false);
  });

  it("never claims Jettone can force a fresh Alice Blue login", () => {
    const confirmSpy = vi.fn().mockReturnValue(true);
    // @ts-expect-error -- minimal window stub used only by this test
    global.window = { confirm: confirmSpy };

    confirmAliceBlueLogin("2679228");

    const message = confirmSpy.mock.calls[0][0] as string;
    expect(message.toLowerCase()).not.toContain("force");
    expect(message).toContain("log out");
  });
});

describe("setReconnectTarget / consumeReconnectTarget", () => {
  it("round-trips the correct-user reconnect target (correct-user scenario)", () => {
    stubWindow();
    setReconnectTarget("acc-vikash", "2679228");

    const result = consumeReconnectTarget();

    expect(result).toEqual({ accountId: "acc-vikash", aliceUserId: "2679228" });
  });

  it("is single-use: a second consume after the first returns nothing", () => {
    stubWindow();
    setReconnectTarget("acc-vikash", "2679228");

    consumeReconnectTarget();
    const second = consumeReconnectTarget();

    expect(second).toEqual({ accountId: null, aliceUserId: null });
  });

  it("recovers from localStorage when sessionStorage has nothing (new-tab noopener case)", () => {
    const { sessionStorage, localStorage } = stubWindow();
    setReconnectTarget("acc-vikash", "2679228");
    // Simulate the target tab not inheriting sessionStorage from the opener.
    sessionStorage.removeItem("reconnecting_account_id");
    sessionStorage.removeItem("reconnecting_alice_user_id");
    sessionStorage.removeItem("reconnecting_started_at");
    expect(localStorage.getItem("reconnecting_account_id")).toBe("acc-vikash");

    const result = consumeReconnectTarget();

    expect(result).toEqual({ accountId: "acc-vikash", aliceUserId: "2679228" });
  });

  it("ignores a stale target older than the TTL", () => {
    const { localStorage } = stubWindow();
    localStorage.setItem("reconnecting_account_id", "acc-vikash");
    localStorage.setItem("reconnecting_alice_user_id", "2679228");
    localStorage.setItem("reconnecting_started_at", String(Date.now() - 20 * 60 * 1000)); // 20 minutes ago

    const result = consumeReconnectTarget();

    expect(result).toEqual({ accountId: null, aliceUserId: null });
  });

  it("returns nothing when no reconnect was ever started (stale/leftover-tab scenario)", () => {
    stubWindow();
    // No setReconnectTarget() call — this is the exact shape of the real
    // incident: a login completed from a tab Jettone never targeted.
    const result = consumeReconnectTarget();

    expect(result).toEqual({ accountId: null, aliceUserId: null });
  });
});
