import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import AccountsTable, { buildAccountsCsv, formatDateDmy, type AccountsTableRow } from "../components/accounts/AccountsTable";
import { AvailableStaticIpCard } from "../components/dashboard-home/KpiCards";
import { emptyMetrics } from "./account-metrics";
import { describeAssignedIp, formatExpiryCell, staticIpCardState } from "./static-ip";

const counts = { available_count: 2, active_count: 2, untracked_count: 0, expired_count: 0 };

describe("staticIpCardState (dashboard card)", () => {
  it("is loading only while the first request is pending", () => {
    expect(staticIpCardState({ data: null, error: null, isLoading: true })).toEqual({ kind: "loading" });
  });

  it("becomes ready as soon as data arrives, even if a later poll fails", () => {
    expect(staticIpCardState({ data: counts, error: null, isLoading: false })).toEqual({ kind: "ready", counts });
    expect(staticIpCardState({ data: counts, error: "Network error", isLoading: false }).kind).toBe("ready");
  });

  it("shows an error, never an endless loading state, when the request failed", () => {
    expect(staticIpCardState({ data: null, error: "Request failed (502)", isLoading: false })).toEqual({
      kind: "error",
      message: "Request failed (502)",
    });
  });

  it("never reports loading once the request has finished, even with no data and no message", () => {
    expect(staticIpCardState({ data: null, error: null, isLoading: false }).kind).toBe("error");
  });
});

describe("AvailableStaticIpCard", () => {
  it("renders a skeleton only while loading", () => {
    const html = renderToStaticMarkup(createElement(AvailableStaticIpCard, { state: { kind: "loading" } }));
    expect(html).toContain("animate-pulse");
    expect(html).not.toContain("Loading…");
  });

  it("renders the real count when ready", () => {
    const html = renderToStaticMarkup(createElement(AvailableStaticIpCard, { state: { kind: "ready", counts } }));
    expect(html).toContain(">2<");
    expect(html).not.toContain("animate-pulse");
  });

  it("renders an explicit unavailable state with a retry on failure", () => {
    const html = renderToStaticMarkup(
      createElement(AvailableStaticIpCard, { state: { kind: "error", message: "Request failed (502)" }, onRetry: () => {} }),
    );
    expect(html).toContain("Unavailable");
    expect(html).toContain("<button");
    expect(html).not.toContain("animate-pulse");
  });

  it("is wired to the request's error and loading state, not just its data", () => {
    const page = readFileSync(join(__dirname, "../app/(app)/page.tsx"), "utf8");
    expect(page).toContain("staticIp={staticIpCardState(staticIp)}");
    expect(page).toContain("onRetryStaticIp={staticIp.refresh}");
  });

  it("starts the request at page load and owns it above the row that unmounts", () => {
    const page = readFileSync(join(__dirname, "../app/(app)/page.tsx"), "utf8");
    const row = readFileSync(join(__dirname, "../components/dashboard-home/PlatformOverview.tsx"), "utf8");
    // The page keeps the request across summary reloads / range changes...
    expect(page).toMatch(/useEngineData<StaticIpCounts>\("\/system\/static-ip"/);
    // ...while the row (unmounted during every summary load) makes no request of its own.
    expect(row).not.toContain("useEngineData");
  });

  it("never replaces a loaded count with a skeleton", () => {
    // Once data exists the state stays ready, whatever the hook reports next
    // (refresh, range change, or a failed poll).
    for (const isLoading of [true, false]) {
      expect(staticIpCardState({ data: counts, error: null, isLoading }).kind).toBe("ready");
    }
  });
});

describe("Dashboard KPI cards carry no hover tooltip", () => {
  const kpi = readFileSync(join(__dirname, "../components/dashboard-home/KpiCards.tsx"), "utf8");

  it("has no Tooltip wrapper, title attribute or help-only focus stop", () => {
    expect(kpi).not.toContain("components/ui/Tooltip");
    expect(kpi).not.toMatch(/title=/);
    expect(kpi).not.toContain("tabIndex");
  });

  it("renders the Static IP card without title or tooltip markup", () => {
    for (const state of [
      { kind: "ready" as const, counts },
      { kind: "error" as const, message: "Request failed (502)" },
    ]) {
      const html = renderToStaticMarkup(createElement(AvailableStaticIpCard, { state, onRetry: () => {} }));
      expect(html).not.toMatch(/title=/);
      expect(html).not.toContain("aria-describedby");
    }
  });

  it("keeps chart hover tooltips", () => {
    const charts = ["StrategyPnlChart.tsx", "OrdersByStrategyChart.tsx", "CopyExecutionChart.tsx"].map((f) =>
      readFileSync(join(__dirname, "../components/dashboard-home", f), "utf8"),
    );
    expect(charts[0]).toMatch(/hoverIdx/);
    expect(charts[1]).toMatch(/hoverRow/);
    expect(charts[2]).toContain("Tooltip");
  });
});

describe("Demat IP and expiry cells", () => {
  it("shows the recorded public IP, never the proxy hostname", () => {
    expect(describeAssignedIp("151.245.182.22", "dc-mum-007.staticip.in")).toEqual({
      text: "151.245.182.22",
      kind: "assigned",
    });
  });

  it("says 'Not entered' for a proxy without a recorded IP, and keeps 'Shared VPS' for no proxy", () => {
    expect(describeAssignedIp(null, "dc-mum-007.staticip.in")).toEqual({ text: "Not entered", kind: "not_entered" });
    expect(describeAssignedIp("  ", "dc-mum-007.staticip.in").kind).toBe("not_entered");
    expect(describeAssignedIp(null, null)).toEqual({ text: "Shared VPS", kind: "shared_vps" });
  });

  it("formats the expiry like the rest of the app and shows 'Not set' when missing", () => {
    expect(formatExpiryCell("2026-11-05")).toBe("05-Nov-2026");
    expect(formatExpiryCell(null)).toBe("Not set");
    expect(formatExpiryCell("")).toBe("Not set");
  });
});

/** What the Demat page passes: the same role pill (+ E-Stop pill) AccountBadges renders. */
function badgesFor(role: AccountsTableRow["role"], eStop = false) {
  const label = role === "MASTER" ? "Master" : role === "CHILD" ? "Child" : "Unassigned";
  return createElement(
    "span",
    null,
    createElement("span", null, label),
    eStop ? createElement("span", null, "E-Stop") : null,
  );
}

function row(over: Partial<AccountsTableRow>): AccountsTableRow {
  const metrics = { ...emptyMetrics(over.id ?? "a1"), needs_review_count: 4 };
  return {
    id: "a1",
    displayName: "Dhinesh",
    aliceUserId: "2679228",
    role: "MASTER",
    proxyHost: "dc-mum-007.staticip.in",
    assignedIp: null,
    expiresOn: null,
    staticIpStatus: "ACTIVE",
    createdAt: "2026-10-14T05:00:00Z",
    sessionValid: true,
    lastAuthenticatedAt: "2026-10-15T04:00:00Z",
    badges: null,
    metrics,
    tradingEnabled: true,
    onToggleTrading: () => {},
    onViewHistory: () => {},
    onRelogin: null,
    dangerAction: { label: "Remove", runningLabel: "Removing...", onClick: () => {} } as never,
    ...over,
  };
}

describe("AccountsTable (Demat list view)", () => {
  const html = renderToStaticMarkup(
    createElement(AccountsTable, {
      rows: [
        row({ id: "a1", displayName: "Dhinesh", assignedIp: "151.245.182.22", expiresOn: "2026-11-05", badges: badgesFor("MASTER") }),
        row({ id: "a2", displayName: "Vikash", aliceUserId: "2679246", role: "CHILD", assignedIp: null, expiresOn: null, badges: badgesFor("CHILD") }),
        row({ id: "a3", displayName: "Loose", aliceUserId: "2679999", role: null, badges: badgesFor(null, true) }),
      ],
      vpsIp: "178.18.252.165",
    }),
  );

  it("shows the recorded public IP and not the proxy hostname", () => {
    expect(html).toContain("151.245.182.22");
    expect(html).not.toContain("dc-mum-007.staticip.in");
  });

  it("shows 'Not entered' when no public IP is recorded", () => {
    expect(html).toContain("Not entered");
  });

  it("has an Expiry Date column with formatted dates and 'Not set'", () => {
    expect(html).toContain("Expiry Date");
    expect(html).toContain("05-Nov-2026");
    expect(html).toContain("Not set");
  });

  const headerLabels = () =>
    Array.from(html.matchAll(/<th[^>]*>([\s\S]*?)<\/th>/g)).map((m) => m[1].replace(/<[^>]*>/g, "").replace(/&amp;/g, "&").trim());
  const rowCells = (index: number) => {
    const body = html.slice(html.indexOf("<tbody"));
    const rowHtml = Array.from(body.matchAll(/<tr[^>]*>([\s\S]*?)<\/tr>/g))[index][1];
    return Array.from(rowHtml.matchAll(/<td[^>]*>([\s\S]*?)<\/td>/g)).map((m) => m[1]);
  };
  const text = (h: string) => h.replace(/<[^>]*>/g, " ").replace(/\s+/g, " ").trim();

  it("orders the columns S.No, Account, Role, Session, Trading, IP Address, Created Date, Expiry Date, P&L, Positions, Orders", () => {
    expect(headerLabels()).toEqual([
      "S.No", "Account", "Role", "Session", "Trading", "IP Address", "Created Date", "Expiry Date", "P&L", "Positions", "Orders",
    ]);
  });

  it("has no In Group column", () => {
    expect(headerLabels()).not.toContain("In Group");
    expect(html).not.toContain("In Group");
    expect(rowCells(0)).toHaveLength(11);
  });

  it("keeps only the account name and user id in the Account cell, and the role in its own column", () => {
    const cells = rowCells(0);
    expect(text(cells[1])).toBe("Dhinesh 2679228");
    expect(text(cells[1])).not.toMatch(/Master|Child|Unassigned|E-Stop/);
    expect(text(cells[2])).toBe("Master");
    expect(text(rowCells(1)[2])).toBe("Child");
    expect(text(rowCells(2)[2])).toBe("Unassigned E-Stop");
  });

  it("shows Created Date and Expiry Date in the same DD-Mon-YYYY format, in that order, with 'Not set' for missing dates", () => {
    const cells = rowCells(0);
    expect(text(cells[6])).toBe("14-Oct-2026");
    expect(text(cells[7])).toBe("05-Nov-2026");
    expect(text(rowCells(1)[7])).toBe("Not set");
    expect(text(cells[6])).toMatch(/^\d{2}-[A-Z][a-z]{2}-\d{4}$/);
    expect(text(cells[7])).toMatch(/^\d{2}-[A-Z][a-z]{2}-\d{4}$/);
  });

  it("gives every column header the same text style, with the sorting indicator on the data columns", () => {
    const headers = Array.from(html.matchAll(/<th[^>]*>([\s\S]*?)<\/th>/g)).map((m) => m[1]);
    const label = (h: string) => h.replace(/<[^>]*>/g, "").trim();
    const byName = (name: string) => headers.find((h) => label(h) === name) as string;
    const buttonClass = (h: string) => /<button[^>]*class="([^"]*)"/.exec(h)?.[1] ?? "";
    const base = (c: string) => c.replace(/text-\[#(111111|9CA3AF)\]|hover:text-\[#4B5563\]/g, "").replace(/\s+/g, " ").trim();

    const reference = base(buttonClass(byName("Account")));
    expect(reference).toContain("whitespace-nowrap");
    for (const name of ["Orders", "Role", "Positions", "IP Address", "Expiry Date", "Created Date", "Session", "Trading"]) {
      expect(byName(name), name).toBeDefined();
      expect(buttonClass(byName(name)), name).not.toBe("");
      // same font size, weight, case and spacing as Account / Orders
      expect(base(buttonClass(byName(name))).replace("flex-row-reverse", "").trim(), name).toBe(
        reference.replace("flex-row-reverse", "").trim(),
      );
      // same sort indicator design (lucide chevrons, 3x3)
      expect(byName(name), name).toMatch(/lucide-chevrons-up-down|lucide-chevron-/);
    }
  });

  it("does not render the review badge, while the review count stays in the data", () => {
    expect(html).not.toMatch(/\d+ review/);
    const m = emptyMetrics("a1");
    expect({ ...m, needs_review_count: 4 }.needs_review_count).toBe(4);
  });
});

describe("CSV export", () => {
  const csv = buildAccountsCsv([
    row({ id: "a1", displayName: "Dhinesh", assignedIp: "151.245.182.22", expiresOn: "2026-11-05" }),
    row({ id: "a2", displayName: "Vikash", aliceUserId: "2679246", role: "CHILD", expiresOn: null, createdAt: "" }),
  ]);
  const lines = csv.split(String.fromCharCode(10));
  const header = lines[0].split(",");
  const parse = (line: string) => line.split('","').map((c) => c.replace(/^"|"$/g, ""));
  const first = parse(lines[1]);
  const second = parse(lines[2]);

  it("has no In Group column and follows the table's column order", () => {
    expect(header).not.toContain("In Group");
    const order = ["Name", "Role", "Session", "Trading", "IP Address", "Created Date", "Expiry Date", "P&L", "Open Positions", "Orders"];
    const idx = order.map((c) => header.indexOf(c));
    expect(idx.every((i) => i >= 0)).toBe(true);
    expect([...idx].sort((a, b) => a - b)).toEqual(idx);
  });

  it("writes role, recorded IP and both dates in DD-Mon-YYYY, 'Not set' when missing", () => {
    const col = (name: string, r: string[]) => r[header.indexOf(name)];
    expect(col("Role", first)).toBe("Master");
    expect(col("IP Address", first)).toBe("151.245.182.22");
    expect(col("Created Date", first)).toBe("14-Oct-2026");
    expect(col("Expiry Date", first)).toBe("05-Nov-2026");
    expect(col("Role", second)).toBe("Child");
    expect(col("Created Date", second)).toBe("Not set");
    expect(col("Expiry Date", second)).toBe("Not set");
  });
});

describe("formatDateDmy", () => {
  it("formats a timestamp as a date in IST", () => {
    expect(formatDateDmy("2026-10-14T05:00:00Z")).toBe("14-Oct-2026");
    // 20:00 UTC on the 14th is already the 15th in IST
    expect(formatDateDmy("2026-10-14T20:00:00Z")).toBe("15-Oct-2026");
  });

  it("says 'Not set' for missing or invalid values", () => {
    expect(formatDateDmy(null)).toBe("Not set");
    expect(formatDateDmy("")).toBe("Not set");
    expect(formatDateDmy("not a date")).toBe("Not set");
  });
});

describe("Order history header", () => {
  it("no longer shows the account-id subtitle", () => {
    const page = readFileSync(join(__dirname, "../app/(app)/demat/[accountId]/history/page.tsx"), "utf8");
    expect(page).not.toContain("Account-specific order history");
    expect(page).toContain("— Order History");
    expect(page).toContain("accountError");
  });
});
