import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import AccountsTable, { type AccountsTableRow } from "../components/accounts/AccountsTable";
import { emptyMetrics } from "./account-metrics";
import { buildAccountUpdateBody, createSubmitGuard, runSubmit } from "./account-update";

const read = (rel: string) => readFileSync(join(__dirname, "..", rel), "utf8");

function row(over: Partial<AccountsTableRow>): AccountsTableRow {
  return {
    id: "a1",
    displayName: "Dhinesh",
    aliceUserId: "2679228",
    role: "MASTER",
    proxyHost: "dc-mum-007.staticip.in",
    assignedIp: "151.245.182.76",
    expiresOn: "2026-11-05",
    staticIpStatus: "EXPIRED",
    createdAt: "2026-10-14T05:00:00Z",
    sessionValid: true,
    lastAuthenticatedAt: "2026-10-15T04:00:00Z",
    badges: createElement("span", null, "Master"),
    metrics: emptyMetrics(over.id ?? "a1"),
    tradingEnabled: true,
    onToggleTrading: () => {},
    onViewHistory: () => {},
    onRelogin: null,
    dangerAction: { label: "Remove", runningLabel: "Removing...", onClick: () => {} } as never,
    ...over,
  };
}

describe("Demat table: no tooltips, account ids, P&L state", () => {
  const verifiedZero = {
    ...emptyMetrics("a2"),
    realized_pnl: "0.00",
    unrealized_pnl: "0.00",
    total_pnl: "0.00",
    pnl_status: "OK" as const,
  };
  const html = renderToStaticMarkup(
    createElement(AccountsTable, {
      rows: [
        row({ id: "a1", displayName: "Dhinesh", aliceUserId: "2679228", role: "MASTER" }),
        row({ id: "a2", displayName: "Vikash", aliceUserId: "2679246", role: "CHILD", metrics: verifiedZero, assignedIp: null }),
      ],
      vpsIp: "178.18.252.165",
    }),
  );

  it("renders no tooltip markup or native title in the table", () => {
    expect(html).not.toMatch(/\btitle=/);
    expect(html).not.toContain("aria-describedby");
    expect(html).not.toContain('role="tooltip"');
    const src = read("components/accounts/AccountsTable.tsx");
    expect(src).not.toContain("components/ui/Tooltip");
    expect(src).not.toContain("pnlTooltip");
  });

  it("shows the account id under the name for both the Master and the Child", () => {
    expect(html).toMatch(/Dhinesh<\/div><div[^>]*>2679228</);
    expect(html).toMatch(/Vikash<\/div><div[^>]*>2679246</);
  });

  it("shows N/A for an unverified P&L and ₹0.00 only for a verified zero", () => {
    expect(html.split("N/A").length - 1).toBe(1); // Dhinesh: unverified
    expect(html.split("₹0.00").length - 1).toBe(1); // Vikash: verified zero
  });

  it("gives the IP Address, Created Date and Expiry Date headers the same bold style as the rest", () => {
    const headers = Array.from(html.matchAll(/<th[^>]*>([\s\S]*?)<\/th>/g)).map((m) => m[1]);
    for (const name of ["IP Address", "Created Date", "Expiry Date", "Account", "Orders"]) {
      const h = headers.find((x) => x.replace(/<[^>]*>/g, "").trim() === name) as string;
      const cls = /<button[^>]*class="([^"]*)"/.exec(h)?.[1] ?? "";
      for (const token of ["font-bold", "text-[10px]", "uppercase", "tracking-[0.06em]", "whitespace-nowrap"]) {
        expect(cls.split(" "), `${name}: ${token}`).toContain(token);
      }
    }
  });
});

describe("Position Reconciliation typography matches the Demat table", () => {
  const page = read("app/(app)/reconciliation/page.tsx");
  const table = read("components/accounts/AccountsTable.tsx");

  it("uses the Demat header and cell text styles", () => {
    expect(page).toContain("font-bold text-[10px] leading-4 text-[#9CA3AF] uppercase tracking-[0.06em]");
    expect(table).toContain("font-bold uppercase tracking-[0.06em] text-[10px]");
    expect(page).toContain("text-left text-xs");
    expect(page).toContain("hover:bg-[#FAFAFB]");
    expect(page).toContain("font-bold text-[#111111] truncate"); // account name
    expect(page).toContain("text-[11px] font-medium text-[#6B7280] truncate tabular-nums"); // account id
  });

  it("uses tabular numbers, one date format and the Demat badge shape", () => {
    expect(page).toContain("tabular-nums");
    expect(page).not.toContain("font-mono");
    expect(page).not.toContain("toLocaleString");
    expect(page).toContain("rounded-lg text-[10px] font-bold border");
  });

  it("keeps the records, filters and actions", () => {
    expect(page).toContain("Run Reconciliation Check");
    expect(page).toContain("Show resolved");
    expect(page).toContain("reconciliation/run");
    expect(page).toContain("discrepancy_qty");
  });
});

describe("Reconnect dialog: Save and Save & Reconnect", () => {
  const modal = read("components/demat/AddAccountModal.tsx");
  const page = read("app/(app)/demat/page.tsx");

  it("has Save and Save & Reconnect in edit mode, keeps the top-right X, and no longer says Reconnect & Login", () => {
    expect(modal).toContain("Save & Reconnect");
    expect(modal).not.toContain("Reconnect & Login");
    expect(modal).toContain('title="Close panel"');
    expect(modal).toContain('onClick={() => void submit("save")}');
    const footer = modal.slice(modal.indexOf("Fixed Bottom Action Bar"));
    expect(footer.indexOf('"Save"')).toBeLessThan(footer.indexOf("Close"));
  });

  it("closes the dialog on a successful Save and shows the success feedback on the page", () => {
    const outcome = modal.slice(modal.indexOf('case "saved":'), modal.indexOf('case "reconnected":'));
    expect(outcome).toContain("onSaved?.()");
    expect(outcome).toContain("onClose()");
    expect(page).toContain('setSavedNotice("Account details saved.")');
    expect(page).toMatch(/role="status"[^>]*>\s*\{savedNotice\}/);
  });

  it("keeps the dialog open and shows the error when saving fails", () => {
    const failed = modal.slice(modal.indexOf('case "save_failed":'), modal.indexOf('case "reconnect_failed":'));
    expect(failed).toContain("setError(");
    expect(failed).not.toContain("onClose");
    expect(failed).not.toContain("onCreated");
  });

  it("reports a failed reconnect separately from a failed save, and the details stay saved", () => {
    const failed = modal.slice(modal.indexOf('case "reconnect_failed":'));
    expect(failed).toContain("but the reconnect could not be started");
    expect(failed).toContain("onSaved?.()");
    expect(failed.slice(0, failed.indexOf("break;"))).not.toContain("onClose");
  });

  it("only starts the broker login inside the reconnect step, which runs after the save", () => {
    const submit = modal.slice(modal.indexOf("const submit = async"));
    const reconnectStep = submit.indexOf("async (saved) =>");
    expect(reconnectStep).toBeGreaterThan(submit.indexOf("buildAccountUpdateBody("));
    for (const call of ["confirmAliceBlueLogin(", "setReconnectTarget(", "recordReconnectIntent("]) {
      expect(submit.indexOf(call), call).toBeGreaterThan(reconnectStep);
    }
  });

  it("prevents duplicate submissions with a synchronous guard and disabled buttons", () => {
    expect(modal).toContain("createSubmitGuard()");
    expect(modal).toContain("submitGuard.current.tryEnter()");
    expect(modal).toContain("submitGuard.current.leave()");
    expect(modal.match(/disabled=\{submitting\}/g)?.length).toBeGreaterThanOrEqual(2);
  });

  it("keeps the Add dialog's own Close button and flow", () => {
    expect(modal).toContain('"Add"');
    expect(modal).toContain("Adding...");
    expect(modal).toContain('fetch("/api/v1/accounts", {');
  });
});

describe("buildAccountUpdateBody", () => {
  const fields = {
    displayName: "Dhinesh",
    broker: "alicebluenew",
    notes: "Broker: alicebluenew",
    proxyExpiresOn: "2026-11-05",
    staticIpAddress: "151.245.182.76",
  };

  it("sends the edited details only and never is_active (the Trading switch)", () => {
    const body = buildAccountUpdateBody(fields);
    expect(body).toEqual({
      display_name: "Dhinesh",
      broker: "alicebluenew",
      notes: "Broker: alicebluenew",
      proxy_expires_on: "2026-11-05",
      static_ip_address: "151.245.182.76",
    });
    expect("is_active" in body).toBe(false);
  });
});

describe("runSubmit ordering and failure handling", () => {
  const savedAccount = { id: "acc-1", userId: "2679228" };

  it("Save only: saves, never reconnects", async () => {
    const calls: string[] = [];
    const outcome = await runSubmit(
      "save",
      async () => {
        calls.push("save");
        return savedAccount;
      },
      async () => {
        calls.push("reconnect");
      },
    );
    expect(outcome).toEqual({ status: "saved" });
    expect(calls).toEqual(["save"]);
  });

  it("Save & Reconnect: saves first, then reconnects with what was saved", async () => {
    const calls: string[] = [];
    let received: unknown = null;
    const outcome = await runSubmit(
      "reconnect",
      async () => {
        calls.push("save");
        return savedAccount;
      },
      async (saved) => {
        calls.push("reconnect");
        received = saved;
      },
    );
    expect(outcome).toEqual({ status: "reconnected" });
    expect(calls).toEqual(["save", "reconnect"]);
    expect(received).toEqual(savedAccount);
  });

  it("a failed save never starts the reconnect", async () => {
    let reconnects = 0;
    const outcome = await runSubmit(
      "reconnect",
      async () => {
        throw new Error("Invalid static IP");
      },
      async () => {
        reconnects += 1;
      },
    );
    expect(outcome).toEqual({ status: "save_failed", message: "Invalid static IP" });
    expect(reconnects).toBe(0);
  });

  it("a failed reconnect is reported separately, after a successful save", async () => {
    let saves = 0;
    const outcome = await runSubmit(
      "reconnect",
      async () => {
        saves += 1;
        return savedAccount;
      },
      async () => {
        throw new Error("Could not record the reconnect intent");
      },
    );
    expect(outcome).toEqual({ status: "reconnect_failed", message: "Could not record the reconnect intent" });
    expect(saves).toBe(1);
  });

  it("uses a clear message when the error carries none", async () => {
    const outcome = await runSubmit(
      "save",
      async () => {
        throw "boom";
      },
      async () => {},
    );
    expect(outcome.status).toBe("save_failed");
    expect((outcome as { message: string }).message).toMatch(/could not be saved/i);
  });
});

describe("createSubmitGuard (duplicate-submission prevention)", () => {
  it("lets only one submission in until it leaves", () => {
    const guard = createSubmitGuard();
    expect(guard.tryEnter()).toBe(true);
    expect(guard.busy).toBe(true);
    expect(guard.tryEnter()).toBe(false); // second click while the first is running
    guard.leave();
    expect(guard.busy).toBe(false);
    expect(guard.tryEnter()).toBe(true);
  });

  it("a double click runs one save and one reconnect", async () => {
    const guard = createSubmitGuard();
    let saves = 0;
    let reconnects = 0;
    const click = async () => {
      if (!guard.tryEnter()) return;
      try {
        await runSubmit(
          "reconnect",
          async () => {
            saves += 1;
            await Promise.resolve();
            return { id: "a" };
          },
          async () => {
            reconnects += 1;
          },
        );
      } finally {
        guard.leave();
      }
    };
    await Promise.all([click(), click()]);
    expect({ saves, reconnects }).toEqual({ saves: 1, reconnects: 1 });
  });
});
