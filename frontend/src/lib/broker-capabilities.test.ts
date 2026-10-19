import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import BrokerCapabilityStrip from "../components/demat/BrokerCapabilityStrip";
import {
  CAPABILITIES_UNKNOWN,
  CHILD_BLOCKED_FALLBACK,
  MASTER_BLOCKED_FALLBACK,
  brokerCapabilities,
  childBlocker,
  masterBlocker,
  type CatalogEntry,
} from "./broker-support";

const read = (rel: string) => readFileSync(join(__dirname, "..", rel), "utf8");

const ALICE: CatalogEntry = {
  id: "alicebluenew", support: "SUPPORTED", selectable: true,
  capabilities: { login: true, as_master: true, as_child: true },
};
const GROWW_OFF: CatalogEntry = {
  id: "groww", support: "NOT_SUPPORTED", selectable: false, support_note: "Groww Trading API support is in development and disabled.",
  capabilities: { login: false, as_master: false, as_child: false },
};
const GROWW_SETUP_ONLY: CatalogEntry = {
  id: "groww", support: "PARTIAL", selectable: true, support_note: "Credential setup and login only: Groww cannot be a strategy Child.",
  capabilities: { login: true, as_master: false, as_child: false },
};
const GROWW_CHILD: CatalogEntry = {
  id: "groww", support: "PARTIAL", selectable: true, support_note: "Child only (equity, NSE/BSE).",
  capabilities: { login: true, as_master: false, as_child: true },
};
const ANGEL_SETUP_ONLY: CatalogEntry = {
  id: "angelone", support: "PARTIAL", selectable: true, support_note: "Credential setup and login only.",
  capabilities: { login: true, as_master: false, as_child: false },
};
const CATALOG = [ALICE, GROWW_SETUP_ONLY, ANGEL_SETUP_ONLY];

describe("brokerCapabilities: setup/login, Master and Child are separate", () => {
  it("Alice Blue (and its legacy spellings) can do everything, with or without a catalog", () => {
    for (const v of ["alicebluenew", "Alice Blue", "aliceblue", "", null, undefined]) {
      for (const cat of [undefined, null, [], CATALOG]) {
        const c = brokerCapabilities(v, cat);
        expect([c.login, c.asMaster, c.asChild], `${String(v)}`).toEqual([true, true, true]);
      }
    }
  });

  it("a setup-only broker can log in but is neither Master nor Child", () => {
    const c = brokerCapabilities("groww", CATALOG);
    expect(c).toMatchObject({ known: true, login: true, asMaster: false, asChild: false });
    expect(c.note).toContain("setup and login only");
  });

  it("Child capability does not imply Master, and the reverse", () => {
    expect(brokerCapabilities("groww", [GROWW_CHILD])).toMatchObject({ login: true, asMaster: false, asChild: true });
    const masterOnly: CatalogEntry = { ...GROWW_CHILD, capabilities: { login: true, as_master: true, as_child: false } };
    expect(brokerCapabilities("groww", [masterOnly])).toMatchObject({ login: true, asMaster: true, asChild: false });
  });

  it("a broker that is not selectable has no capability, whatever the flags say (selectable wins)", () => {
    const contradictory: CatalogEntry = { ...GROWW_OFF, capabilities: { login: true, as_master: true, as_child: true } };
    expect(brokerCapabilities("groww", [contradictory])).toMatchObject({ login: false, asMaster: false, asChild: false });
  });

  it("fails closed with no catalog, an unknown broker, or missing capability data", () => {
    for (const cat of [undefined, null, []]) {
      expect(brokerCapabilities("groww", cat)).toMatchObject({ known: false, login: false, asMaster: false, asChild: false });
    }
    expect(brokerCapabilities("made-up", CATALOG)).toMatchObject({ known: true, login: false, asMaster: false, asChild: false });
    const noCaps: CatalogEntry = { id: "groww", support: "PARTIAL", selectable: true };
    expect(brokerCapabilities("groww", [noCaps])).toMatchObject({ login: false, asMaster: false, asChild: false });
  });

  it("an older catalog entry marked SUPPORTED without capabilities is treated as fully capable (Alice shape)", () => {
    const old: CatalogEntry = { id: "alicebluenew", support: "SUPPORTED", selectable: true };
    expect(brokerCapabilities("alicebluenew", [old])).toMatchObject({ login: true, asMaster: true, asChild: true });
  });

  it("is case/whitespace tolerant like the rest of the broker map", () => {
    expect(brokerCapabilities("  GROWW ", CATALOG).login).toBe(true);
  });
});

describe("masterBlocker / childBlocker", () => {
  it("are null for Alice Blue, so its screens are unchanged", () => {
    for (const v of ["alicebluenew", "Alice Blue", null, undefined]) {
      expect(masterBlocker(v, CATALOG)).toBeNull();
      expect(childBlocker(v, CATALOG)).toBeNull();
      expect(masterBlocker(v, null)).toBeNull();
      expect(childBlocker(v, null)).toBeNull();
    }
  });

  it("explain a setup-only broker with the engine's own note", () => {
    expect(masterBlocker("groww", CATALOG)).toBe(GROWW_SETUP_ONLY.support_note);
    expect(childBlocker("groww", CATALOG)).toBe(GROWW_SETUP_ONLY.support_note);
  });

  it("let a Child-capable broker be a Child but still not a Master", () => {
    expect(childBlocker("groww", [GROWW_CHILD])).toBeNull();
    expect(masterBlocker("groww", [GROWW_CHILD])).toBe(GROWW_CHILD.support_note);
  });

  it("use fixed fallback text when the engine gave no note, and say so when capabilities are unknown", () => {
    const quiet: CatalogEntry = { id: "groww", support: "PARTIAL", selectable: true, capabilities: { login: true, as_master: false, as_child: false } };
    expect(masterBlocker("groww", [quiet])).toBe(MASTER_BLOCKED_FALLBACK);
    expect(childBlocker("groww", [quiet])).toBe(CHILD_BLOCKED_FALLBACK);
    expect(masterBlocker("groww", null)).toBe(CAPABILITIES_UNKNOWN);
    expect(childBlocker("angelone", [])).toBe(CAPABILITIES_UNKNOWN);
  });
});

describe("BrokerCapabilityStrip", () => {
  const html = (broker: string, catalog: CatalogEntry[] | null) =>
    renderToStaticMarkup(createElement(BrokerCapabilityStrip, { broker, catalog }));

  it("shows the three capabilities separately for a setup-only broker, with the engine's explanation", () => {
    const out = html("groww", CATALOG);
    expect(out).toContain("Setup &amp; login: available");
    expect(out).toContain("Master: not available");
    expect(out).toContain("Child: not available");
    expect(out).toContain('data-capability="Setup &amp; login" data-available="yes"');
    expect(out).toContain('data-capability="Master" data-available="no"');
    expect(out).toContain('data-capability="Child" data-available="no"');
    expect(out).toContain("Credential setup and login only");
  });

  it("shows Child as available for a Child-capable broker while Master stays unavailable", () => {
    const out = html("groww", [GROWW_CHILD]);
    expect(out).toContain('data-capability="Child" data-available="yes"');
    expect(out).toContain('data-capability="Master" data-available="no"');
  });

  it("renders nothing for Alice Blue, an unsupported broker, an unknown broker, or an unloaded catalog", () => {
    expect(html("alicebluenew", CATALOG)).toBe("");
    expect(html("groww", [GROWW_OFF])).toBe("");
    expect(html("zerodha", CATALOG)).toBe("");
    expect(html("groww", null)).toBe("");
  });
});

describe("pages use the capability helpers (display only; the engine enforces)", () => {
  const strategyPage = read("app/(app)/group-strategy/[groupId]/page.tsx");
  const modal = read("components/demat/AddAccountModal.tsx");

  it("the strategy page loads the engine's broker catalog and passes it to the Master picker", () => {
    expect(strategyPage).toContain('"/accounts/supported-brokers"');
    expect(strategyPage).toContain("catalog={catalog}");
    expect(strategyPage).toContain("masterBlocker(m.broker, catalog)");
  });

  it("a Master option with a blocker is disabled, not clickable, and explains why", () => {
    expect(strategyPage).toContain("disabled={Boolean(blocker)}");
    expect(strategyPage).toContain("if (blocker) return;");
    expect(strategyPage).toContain("Can&apos;t be a Master:");
    expect(strategyPage).toContain('data-testid="master-blocker"');
  });

  it("the Add-child list disables Add for a broker that cannot be a Child and explains why", () => {
    expect(strategyPage).toContain("childBlocker(acc.broker, catalog)");
    expect(strategyPage).toContain("disabled={addingChildId === acc.id || Boolean(blocker)}");
    expect(strategyPage).toContain("Can&apos;t be added as a Child:");
    expect(strategyPage).toContain('data-testid="child-blocker"');
  });

  it("no broker name is hard-coded into the capability decisions", () => {
    expect(strategyPage).not.toMatch(/broker\s*===\s*"(groww|angelone)"/);
    expect(strategyPage).not.toMatch(/\.broker\s*===\s*"/);
  });

  it("the Add Demat Account modal renders the capability strip from the same catalog", () => {
    expect(modal).toContain("<BrokerCapabilityStrip broker={broker} catalog={catalog} />");
  });
});
