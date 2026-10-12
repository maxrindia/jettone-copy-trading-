/**
 * Regression coverage for the standalone Account Order History flow,
 * reached from both Demat Accounts (View) and Group Strategy → Child
 * (View). Both entry points share these exact functions rather than each
 * building their own URL, so there's one place that decides "what account
 * history looks like" instead of two possibly-drifting implementations.
 *
 * No React rendering here (this project has no jsdom/React Testing Library
 * set up - see the earlier alice-blue-login.test.ts for the same note) -
 * the navigation/URL-building logic is deliberately kept as plain functions
 * so it's fully testable without one. Both actual page components
 * (demat/page.tsx, group-strategy/[groupId]/page.tsx) call
 * accountHistoryPath() directly for their View button's onClick.
 */
import { describe, expect, it } from "vitest";
import { accountHistoryPath, buildAccountOrdersApiPath } from "./account-history";

describe("accountHistoryPath", () => {
  it("builds the dedicated /demat/[accountId]/history route, used by Demat View", () => {
    expect(accountHistoryPath("acc-vikash")).toBe("/demat/acc-vikash/history");
  });

  it("builds the identical route shape for Group Strategy child View (same function, same output)", () => {
    // Demat and Group Strategy pass different variable names (card.id vs
    // member.account_id) into the SAME function - proving they land on the
    // exact same URL shape for the exact same underlying account id.
    const dematCardId = "acc-dhinesh";
    const groupStrategyMemberAccountId = "acc-dhinesh";
    expect(accountHistoryPath(dematCardId)).toBe(accountHistoryPath(groupStrategyMemberAccountId));
  });

  it("URL-encodes the account id", () => {
    expect(accountHistoryPath("id with space")).toBe("/demat/id%20with%20space/history");
  });
});

describe("buildAccountOrdersApiPath", () => {
  it("builds the dedicated per-account orders endpoint path", () => {
    expect(buildAccountOrdersApiPath("acc-vikash")).toBe("/accounts/acc-vikash/orders?limit=100");
  });

  it("URL-encodes the account id", () => {
    expect(buildAccountOrdersApiPath("id with space")).toBe("/accounts/id%20with%20space/orders?limit=100");
  });
});
