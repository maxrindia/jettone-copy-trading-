import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { brokerSupport, isBrokerSelectable, withBrokerSupport, type CatalogEntry } from "./broker-support";
import {
  brokerLoginPath,
  hasAnyVaultCredential,
  usesVault,
  vaultCredentialBody,
  vaultCredentialProblem,
  zerodhaLoginCompletePath,
  zerodhaLoginUrlPath,
} from "./vault-credentials";

const read = (rel: string) => readFileSync(join(__dirname, "..", rel), "utf8");

const ALICE: CatalogEntry = { id: "alicebluenew", support: "SUPPORTED", selectable: true };
const GROWW_OFF: CatalogEntry = { id: "groww", support: "NOT_SUPPORTED", selectable: false };
const GROWW_ON: CatalogEntry = { id: "groww", support: "PARTIAL", selectable: true };

describe("vault brokers", () => {
  it("only Angel One, Groww and Zerodha use the credential vault", () => {
    expect(usesVault("angelone")).toBe(true);
    expect(usesVault("groww")).toBe(true);
    expect(usesVault("zerodha")).toBe(true);
    for (const v of ["alicebluenew", "dhan", "", null, undefined]) expect(usesVault(v as string)).toBe(false);
  });

  it("builds each broker's vault body from trimmed values", () => {
    const f = { apiKey: " KEY ", secret: " SEC ", totpSecret: " TOTP " };
    expect(vaultCredentialBody("groww", f)).toEqual({ api_key: "KEY", api_secret: "SEC" });
    expect(vaultCredentialBody("angelone", f)).toEqual({ api_key: "KEY", pin: "SEC", totp_secret: "TOTP" });
    expect(vaultCredentialBody("zerodha", f)).toEqual({ api_key: "KEY", api_secret: "SEC" });
  });

  it("Zerodha needs the Kite API key and secret, and has its own interactive-login routes", () => {
    const blank = { apiKey: "", secret: "", totpSecret: "" };
    expect(vaultCredentialProblem("zerodha", blank, true)).toMatch(/Kite API key and API secret/);
    expect(vaultCredentialProblem("zerodha", { ...blank, apiKey: "K" }, false)).toMatch(/Kite API key and API secret/);
    expect(vaultCredentialProblem("zerodha", blank, false)).toBeNull();
    expect(zerodhaLoginUrlPath("a1")).toBe("/api/v1/accounts/a1/broker-login-url");
    expect(zerodhaLoginCompletePath("a1")).toBe("/api/v1/accounts/a1/broker-login-complete");
  });

  it("requires both Groww fields for a new account and all-or-nothing when editing", () => {
    const blank = { apiKey: "", secret: "", totpSecret: "" };
    expect(vaultCredentialProblem("groww", blank, true)).toMatch(/API key and API secret/);
    expect(vaultCredentialProblem("groww", blank, false)).toBeNull(); // editing: keep the stored ones
    expect(vaultCredentialProblem("groww", { ...blank, apiKey: "k" }, false)).toMatch(/API key and API secret/);
    expect(vaultCredentialProblem("groww", { apiKey: "k", secret: "s", totpSecret: "" }, true)).toBeNull();
    expect(hasAnyVaultCredential("groww", blank)).toBe(false);
    // a Groww account never needs a TOTP secret; Angel One does
    expect(vaultCredentialProblem("angelone", { apiKey: "k", secret: "s", totpSecret: "" }, true)).toMatch(/TOTP/);
  });

  it("logs in through the generic broker-login route", () => {
    expect(brokerLoginPath("abc")).toBe("/api/v1/accounts/abc/broker-login");
  });
});

describe("Groww follows the engine catalog", () => {
  it("is closed until the engine says it is selectable", () => {
    expect(isBrokerSelectable("groww")).toBe(false);
    expect(isBrokerSelectable("groww", [ALICE, GROWW_OFF])).toBe(false);
    expect(brokerSupport("groww", [ALICE, GROWW_ON])).toBe("PARTIAL");
    const out = withBrokerSupport([{ value: "groww", label: "Groww" }], [ALICE, GROWW_ON]);
    expect(out[0].disabled).toBeUndefined();
  });
});

describe("Add Demat dialog: Groww", () => {
  const modal = read("components/demat/AddAccountModal.tsx");
  const groww = modal.slice(modal.indexOf("{/* 10. Groww */}"), modal.indexOf("{/* 11. 5Paisa */}"));

  it("collects the key and secret as masked fields and names the daily approval", () => {
    expect(groww.match(/type="password"/g)?.length).toBe(2);
    expect(groww).not.toMatch(/Access Token/);
    expect(groww).toContain("approve this API key on Groww Cloud");
  });

  it("sends Groww secrets only to the vault and logs in through the engine", () => {
    expect(modal).toContain("vaultCredentialBody(vaultBroker, vaultFields)");
    expect(modal).toContain("usesVault(broker)");
    expect(modal).not.toContain("angel-login");
  });
});
