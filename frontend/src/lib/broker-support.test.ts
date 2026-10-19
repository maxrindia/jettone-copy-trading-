import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import {
  angelCredentialProblem,
  angelCredentialsComplete,
  buildAngelCredentialBody,
  hasAnyAngelCredential,
} from "./angel-credentials";
import {
  brokerSupport,
  isBrokerSelectable,
  NOT_SUPPORTED_HINT,
  PARTIAL_HINT,
  withBrokerSupport,
  type CatalogEntry,
} from "./broker-support";

const read = (rel: string) => readFileSync(join(__dirname, "..", rel), "utf8");

const ALICE: CatalogEntry = { id: "alicebluenew", support: "SUPPORTED", selectable: true };
const ANGEL_OFF: CatalogEntry = { id: "angelone", support: "NOT_SUPPORTED", selectable: false };
const ANGEL_LIMITED: CatalogEntry = { id: "angelone", support: "PARTIAL", selectable: true };

describe("broker support map (no catalog loaded yet)", () => {
  it("only Alice Blue (and its legacy values) is selectable", () => {
    for (const v of ["alicebluenew", "Alice Blue", "aliceblue", "", null, undefined]) {
      expect(isBrokerSelectable(v), String(v)).toBe(true);
    }
    for (const v of ["angelone", "zerodha", "groww", "dhan", "upstox", "made-up"]) {
      expect(brokerSupport(v), v).toBe("NOT_SUPPORTED");
      expect(isBrokerSelectable(v), v).toBe(false);
    }
  });

  it("treats an empty catalog like no catalog (fail closed)", () => {
    expect(isBrokerSelectable("angelone", [])).toBe(false);
    expect(isBrokerSelectable("angelone", null)).toBe(false);
  });

  it("marks unsupported options disabled with a hint and leaves Alice untouched", () => {
    const out = withBrokerSupport([
      { value: "alicebluenew", label: "Alice Blue" },
      { value: "angelone", label: "Angel One" },
    ]);
    expect(out[0]).toEqual({ value: "alicebluenew", label: "Alice Blue" });
    expect(out[1]).toMatchObject({ value: "angelone", disabled: true, hint: NOT_SUPPORTED_HINT });
  });
});

describe("broker support follows the engine's catalog once loaded", () => {
  it("keeps Angel One closed while the engine reports it not supported", () => {
    expect(isBrokerSelectable("angelone", [ALICE, ANGEL_OFF])).toBe(false);
  });

  it("opens Angel One as limited support only when the engine says it is selectable", () => {
    expect(brokerSupport("angelone", [ALICE, ANGEL_LIMITED])).toBe("PARTIAL");
    const out = withBrokerSupport([{ value: "angelone", label: "Angel One" }], [ALICE, ANGEL_LIMITED]);
    expect(out[0]).toMatchObject({ value: "angelone", hint: PARTIAL_HINT });
    expect(out[0].disabled).toBeUndefined();
  });

  it("never selects a broker the catalog does not list", () => {
    expect(isBrokerSelectable("zerodha", [ALICE, ANGEL_LIMITED])).toBe(false);
  });

  it("a catalog cannot disable Alice Blue's legacy values", () => {
    expect(isBrokerSelectable("Alice Blue", [ANGEL_OFF])).toBe(true);
  });
});

describe("Angel One credentials", () => {
  const full = { apiKey: " KEY ", pin: " 1234 ", totpSecret: " JBSWY3DPEHPK3PXP " };

  it("builds the vault body with trimmed values", () => {
    expect(buildAngelCredentialBody(full)).toEqual({ api_key: "KEY", pin: "1234", totp_secret: "JBSWY3DPEHPK3PXP" });
  });

  it("requires all three for a new account and all-or-nothing when editing", () => {
    const blank = { apiKey: "", pin: "", totpSecret: "" };
    expect(angelCredentialsComplete(full)).toBe(true);
    expect(angelCredentialProblem(blank, true)).toMatch(/key, PIN and TOTP/);
    expect(angelCredentialProblem(blank, false)).toBeNull(); // editing: keep the stored ones
    expect(hasAnyAngelCredential(blank)).toBe(false);
    expect(angelCredentialProblem({ ...blank, pin: "1" }, false)).toMatch(/key, PIN and TOTP/);
    expect(angelCredentialProblem(full, true)).toBeNull();
  });
});

describe("Add Demat dialog gating", () => {
  const modal = read("components/demat/AddAccountModal.tsx");

  it("feeds the broker dropdown through the engine catalog", () => {
    expect(modal).toContain("withBrokerSupport(g.options, catalog)");
    expect(modal).toContain('fetch("/api/v1/accounts/supported-brokers")');
  });

  it("collects credentials only for a selectable broker and blocks submit otherwise", () => {
    expect(modal).toContain("{broker && isBrokerSelectable(broker, catalog) && (");
    expect(modal).toContain("This broker is not supported yet.");
    const submit = modal.slice(modal.indexOf("const submit = async"));
    expect(submit.indexOf("!isBrokerSelectable(broker, catalog)")).toBeGreaterThan(-1);
    expect(submit.indexOf("!isBrokerSelectable(broker, catalog)")).toBeLessThan(submit.indexOf("submitGuard.current.tryEnter()"));
  });

  it("keeps vault brokers' secrets out of notes and sends them only to the vault endpoint", () => {
    expect(modal).toContain("!isVault && apiKey ?");
    expect(modal).toContain("!isVault && apiSecret ?");
    expect(modal).toContain("/broker-credentials");
    expect(modal).toContain("brokerLoginPath(saved.id)");
    // the TOTP secret is a password field, never displayed
    const angelBlock = modal.slice(modal.indexOf("{/* 4. Angel One */}"), modal.indexOf("{/* 5. Dhan */}"));
    expect(angelBlock).toContain('type="password"');
    expect(angelBlock.match(/type="password"/g)?.length).toBe(2); // PIN and TOTP secret
    // Angel One never reaches the Alice Blue login redirect
    const reconnect = modal.slice(modal.indexOf("async (saved) =>"));
    expect(reconnect.indexOf("if (isVault)")).toBeLessThan(reconnect.indexOf("confirmAliceBlueLogin("));
  });
});

describe("OptionDropdown disabled option", () => {
  it("renders aria-disabled and ignores clicks for disabled rows", async () => {
    const src = read("components/ui/OptionDropdown.tsx");
    expect(src).toContain("aria-disabled={opt.disabled || undefined}");
    expect(src).toContain("if (opt.disabled) return;");
    const { default: OptionDropdown } = await import("../components/ui/OptionDropdown");
    const html = renderToStaticMarkup(
      createElement(OptionDropdown, {
        value: "",
        onChange: () => {},
        ariaLabel: "Broker",
        options: [{ value: "angelone", label: "Angel One", disabled: true, hint: NOT_SUPPORTED_HINT }],
        placeholder: "Select Broker",
      }),
    );
    expect(html).toContain("Select Broker");
  });
});
