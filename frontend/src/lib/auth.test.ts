/**
 * Regression coverage for the authentication-bypass fix in ./auth.ts.
 *
 * Before the fix, `authorize()` accepted ANY password of 4+ characters
 * (`allowedPasswords.includes(inputPass) || inputPass.length >= 4`), plus a
 * handful of hardcoded literal passwords. This file proves that only the
 * real bcrypt-hashed owner password (ADMIN_PASSWORD_HASH) can authenticate.
 */
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import bcrypt from "bcryptjs";
import { verifyCredentials } from "./auth";

const TEST_PASSWORD = "correct-horse-battery-staple";
let originalHash: string | undefined;

beforeAll(async () => {
  originalHash = process.env.ADMIN_PASSWORD_HASH;
  // Low bcrypt cost here only speeds up this test's own fixture hash and the
  // several bcrypt.compare() calls below; it has no effect on the real
  // ADMIN_PASSWORD_HASH, which is always generated at cost 12 by
  // scripts/hash-password.mjs. bcrypt.compare() works against a hash of any
  // cost, so this doesn't change what verifyCredentials() is actually doing.
  process.env.ADMIN_PASSWORD_HASH = await bcrypt.hash(TEST_PASSWORD, 4);
});

afterAll(() => {
  if (originalHash === undefined) {
    delete process.env.ADMIN_PASSWORD_HASH;
  } else {
    process.env.ADMIN_PASSWORD_HASH = originalHash;
  }
});

describe("verifyCredentials", () => {
  it("1. succeeds with the correct password", async () => {
    const user = await verifyCredentials("admin@jettone.local", TEST_PASSWORD);
    expect(user).not.toBeNull();
    expect(user?.role).toBe("admin");
    expect(user?.email).toBe("admin@jettone.local");
  });

  it("2. fails with an incorrect password", async () => {
    const user = await verifyCredentials("admin@jettone.local", "totally-wrong-password");
    expect(user).toBeNull();
  });

  it("3. fails with a short password", async () => {
    const user = await verifyCredentials("admin@jettone.local", "abc");
    expect(user).toBeNull();
  });

  it("4. rejects arbitrary 4+ character passwords that are not the real password", async () => {
    // Includes the exact hardcoded values the old vulnerable code accepted,
    // plus other clearly-wrong-but-long-enough guesses.
    const candidates = [
      "admin",
      "admin123",
      "aDJg98y4FRcnbONcr8oBZuPg",
      "password",
      "aaaa",
      "1234",
      "qwerty",
      "x".repeat(64),
    ];
    for (const candidate of candidates) {
      const user = await verifyCredentials("admin@jettone.local", candidate);
      expect(user, `expected "${candidate}" to be rejected`).toBeNull();
    }
  });

  it("fails when no password is provided", async () => {
    const user = await verifyCredentials("admin@jettone.local", undefined);
    expect(user).toBeNull();
  });

  it("fails closed when ADMIN_PASSWORD_HASH is not configured", async () => {
    const saved = process.env.ADMIN_PASSWORD_HASH;
    delete process.env.ADMIN_PASSWORD_HASH;
    try {
      const user = await verifyCredentials("admin@jettone.local", TEST_PASSWORD);
      expect(user).toBeNull();
    } finally {
      process.env.ADMIN_PASSWORD_HASH = saved;
    }
  });
});
