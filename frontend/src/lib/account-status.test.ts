import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import AccountBrokerBadges from "../components/accounts/AccountBrokerBadges";
import { brokerForEdit, saveTargetId } from "./account-update";
import {
  NOT_REPORTED,
  canSetUp,
  connectionView,
  identityNotice,
  roleChips,
  type AccountStatusFields,
  type ServerBrokerCapabilities,
} from "./account-status";

const caps = (over: Partial<ServerBrokerCapabilities> = {}): ServerBrokerCapabilities => ({
  broker_id: "groww", support: "PARTIAL", login: true, as_master: false, as_child: false,
  master_blocker: "Groww cannot be a Master yet.", child_blocker: "Groww cannot receive copied orders yet.", note: "", ...over,
});

const account = (over: Partial<AccountStatusFields> = {}): AccountStatusFields => ({
  broker: "groww", session_valid: false, connection_status: "DISCONNECTED", connection_detail: "Use Re-login.",
  broker_capabilities: caps(), credentials_configured: true, ...over,
});

const html = (a: AccountStatusFields) => renderToStaticMarkup(createElement(AccountBrokerBadges, { account: a }));

describe("connectionView", () => {
  it.each([
    ["CONNECTED", "Connected", "ok"],
    ["DISCONNECTED", "Not signed in", "muted"],
    ["NEEDS_CREDENTIALS", "Needs credentials", "warn"],
    ["BROKER_NOT_ENABLED", "Broker not enabled", "warn"],
    ["UNSUPPORTED_BROKER", "Unsupported broker", "warn"],
  ])("%s is shown as %s", (status, label, tone) => {
    expect(connectionView(account({ connection_status: status }))).toMatchObject({ status, label, tone });
  });

  it("carries the engine's explanation through", () => {
    expect(connectionView(account({ connection_status: "NEEDS_CREDENTIALS", connection_detail: "Add the API key." })).detail).toBe("Add the API key.");
  });

  it("fails closed: a missing or unknown state is never shown as connected, even if session_valid is true", () => {
    for (const connection_status of [undefined, null, "", "SOMETHING_NEW"]) {
      const v = connectionView(account({ connection_status, session_valid: true }));
      expect(v.status).toBe("DISCONNECTED");
    }
  });
});

describe("roleChips", () => {
  it("reports each role exactly as the server does, with the server's reason when blocked", () => {
    const [master, child] = roleChips(caps({ as_child: true, child_blocker: null }));
    expect(master).toEqual({ role: "Master", available: false, reason: "Groww cannot be a Master yet." });
    expect(child).toEqual({ role: "Child", available: true, reason: null });
  });

  it("only a literal true makes a role available", () => {
    const odd = caps({ as_master: "yes" as unknown as boolean, as_child: 1 as unknown as boolean });
    expect(roleChips(odd).every((r) => !r.available)).toBe(true);
  });

  it("missing capabilities leave both roles unavailable, never available", () => {
    for (const none of [null, undefined]) {
      const chips = roleChips(none);
      expect(chips.map((c) => c.available)).toEqual([false, false]);
      expect(chips.every((c) => c.reason === NOT_REPORTED)).toBe(true);
    }
  });

  it("a blocked role with no reason from the server still says something", () => {
    expect(roleChips(caps({ master_blocker: null }))[0].reason).toBe(NOT_REPORTED);
  });

  it("setup and copy trading are separate: a broker can be set up while both roles are blocked", () => {
    expect(canSetUp(caps())).toBe(true);
    expect(roleChips(caps()).every((r) => !r.available)).toBe(true);
    expect(canSetUp(caps({ login: false }))).toBe(false);
    expect(canSetUp(null)).toBe(false);
  });
});

describe("AccountBrokerBadges: a list of several accounts", () => {
  const alice = account({
    broker: "alicebluenew", session_valid: true, connection_status: "CONNECTED", connection_detail: "Signed in.",
    broker_capabilities: caps({ broker_id: "alicebluenew", support: "SUPPORTED", as_master: true, as_child: true, master_blocker: null, child_blocker: null }),
  });
  const groww = account();
  const zerodhaNoCreds = account({
    broker: "zerodha", connection_status: "NEEDS_CREDENTIALS", connection_detail: "No credentials are stored for this account.",
    credentials_configured: false, broker_capabilities: caps({ broker_id: "zerodha" }),
  });
  const angelOff = account({
    broker: "angelone", connection_status: "BROKER_NOT_ENABLED", connection_detail: "Angel One is disabled.",
    broker_capabilities: caps({ broker_id: "angelone", login: false }),
  });

  it("each account shows its own broker", () => {
    expect(html(alice)).toContain("Alice Blue");
    expect(html(groww)).toContain("Groww");
    expect(html(zerodhaNoCreds)).toContain("Zerodha");
    expect(html(angelOff)).toContain("Angel One");
  });

  it("two accounts of different brokers never share each other's state", () => {
    const a = html(alice);
    const z = html(zerodhaNoCreds);
    expect(a).not.toContain("Needs credentials");
    expect(z).toContain("Needs credentials");
    expect(a).toContain('data-testid="account-role-master" data-available="yes"');
    expect(z).toContain('data-testid="account-role-master" data-available="no"');
  });

  it("an Alice Blue account shows both roles available and no connection issue", () => {
    const a = html(alice);
    expect(a).toContain("Master: available");
    expect(a).toContain("Child: available");
    expect(a).not.toContain("account-connection-issue");
  });

  it("the other brokers show both roles blocked with the server's reason as the tooltip", () => {
    const g = html(groww);
    expect(g).toContain("Master: blocked");
    expect(g).toContain("Child: blocked");
    expect(g).toContain("Groww cannot be a Master yet.");
    expect(g).toContain("Groww cannot receive copied orders yet.");
  });

  it("a connection problem is flagged with the engine's explanation, a plain signed-out account is not", () => {
    expect(html(zerodhaNoCreds)).toContain('data-status="NEEDS_CREDENTIALS"');
    expect(html(zerodhaNoCreds)).toContain("No credentials are stored for this account.");
    expect(html(angelOff)).toContain('data-status="BROKER_NOT_ENABLED"');
    expect(html(groww)).not.toContain("account-connection-issue");
  });

  it("an account whose capabilities were not reported shows every role blocked", () => {
    const out = html(account({ broker_capabilities: null }));
    expect(out).toContain("Master: blocked");
    expect(out).toContain("Child: blocked");
    expect(out).toContain("The engine did not report this broker");     // (the apostrophe in the full text is HTML-escaped)
  });

  it("a missing broker value is shown as Alice Blue (the historical default)", () => {
    expect(html(account({ broker: null }))).toContain("Alice Blue");
  });
});

describe("dialog helpers", () => {
  it("brokerForEdit prefers the stored broker over the notes line", () => {
    expect(brokerForEdit({ broker: "Groww", notes: "Broker: alicebluenew, Mobile: 1" })).toBe("groww");
    expect(brokerForEdit({ broker: "zerodha", notes: null })).toBe("zerodha");
  });

  it("brokerForEdit falls back to the notes line, then to Alice Blue", () => {
    expect(brokerForEdit({ broker: null, notes: "Mobile: 1, Broker: angelone, Email: a@b.c" })).toBe("angelone");
    expect(brokerForEdit({ broker: "", notes: "no broker line" })).toBe("alicebluenew");
    expect(brokerForEdit({})).toBe("alicebluenew");
  });

  it("an account being edited, or created on an earlier attempt, is updated rather than created again", () => {
    expect(saveTargetId("edit-1", null)).toBe("edit-1");
    expect(saveTargetId(undefined, "created-1")).toBe("created-1");
    expect(saveTargetId(null, "created-1")).toBe("created-1");
    expect(saveTargetId("edit-1", "created-1")).toBe("edit-1");
    expect(saveTargetId(undefined, null)).toBeNull();
    expect(saveTargetId("", "")).toBeNull();
  });
});

describe("identity notice and unknown status", () => {
  const withIdentity = (level: string, note: string, over: Partial<AccountStatusFields> = {}): AccountStatusFields =>
    account({
      session_valid: true, connection_status: "CONNECTED", connection_detail: "Signed in.",
      broker_capabilities: caps({ identity: { level, note } }), ...over,
    });

  it("a connected account whose identity cannot be confirmed shows the engine's note", () => {
    const a = withIdentity("NOT_AVAILABLE", "Groww token carries no account identity.");
    expect(identityNotice(a)).toBe("Groww token carries no account identity.");
    const out = html(a);
    expect(out).toContain('data-testid="account-identity-notice"');
    expect(out).toContain("Identity not confirmed");
    expect(out).toContain("Groww token carries no account identity.");
  });

  it("confirmed or credential-bound identities show no notice", () => {
    for (const level of ["CONFIRMED", "CREDENTIAL_BOUND"]) {
      expect(identityNotice(withIdentity(level, "note"))).toBeNull();
      expect(html(withIdentity(level, "note"))).not.toContain("account-identity-notice");
    }
  });

  it("an account that is not connected shows no identity notice (there is no login to describe)", () => {
    const off = withIdentity("NOT_AVAILABLE", "n", { session_valid: false, connection_status: "DISCONNECTED" });
    expect(identityNotice(off)).toBeNull();
    expect(html(off)).not.toContain("account-identity-notice");
  });

  it("the notice never changes the roles: they come only from the server's capability flags", () => {
    const a = withIdentity("NOT_AVAILABLE", "n");
    expect(roleChips(a.broker_capabilities).map((r) => r.available)).toEqual([false, false]);
    const open = withIdentity("NOT_AVAILABLE", "n", { broker_capabilities: caps({ as_child: true, child_blocker: null, identity: { level: "NOT_AVAILABLE", note: "n" } }) });
    expect(roleChips(open.broker_capabilities).map((r) => r.available)).toEqual([false, true]);
  });

  it("an unreachable session store is shown as unknown, with the engine's explanation, and never as connected", () => {
    const unknown = account({ session_valid: false, connection_status: "STATUS_UNKNOWN", connection_detail: "Could not check the session store.", session_store_available: false });
    expect(connectionView(unknown)).toMatchObject({ status: "STATUS_UNKNOWN", label: "Status unknown", tone: "warn" });
    const out = html(unknown);
    expect(out).toContain('data-status="STATUS_UNKNOWN"');
    expect(out).toContain("Could not check the session store.");
    expect(identityNotice(unknown)).toBeNull();
  });
});
