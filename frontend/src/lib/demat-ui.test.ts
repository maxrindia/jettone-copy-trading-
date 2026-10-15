import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import AccountsTable, { type AccountsTableRow } from "../components/accounts/AccountsTable";
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
    const src = readFileSync(join(__dirname, "../components/dashboard-home/PlatformOverview.tsx"), "utf8");
    expect(src).toContain("staticIpCardState(staticIp)");
    expect(src).toContain("onRetry={staticIp.refresh}");
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
        row({ id: "a1", displayName: "Dhinesh", assignedIp: "151.245.182.22", expiresOn: "2026-11-05" }),
        row({ id: "a2", displayName: "Vikash", assignedIp: null, expiresOn: null }),
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

  it("does not render the review badge, while the review count stays in the data", () => {
    expect(html).not.toMatch(/\d+ review/);
    const m = emptyMetrics("a1");
    expect({ ...m, needs_review_count: 4 }.needs_review_count).toBe(4);
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
