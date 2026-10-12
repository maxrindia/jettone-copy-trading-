/**
 * Shared logic for the standalone Account Order History page, reached from
 * both Demat Accounts (View) and Group Strategy → Child (View). Both entry
 * points navigate to /demat/[accountId]/history - a dedicated page, not the
 * cross-account Order Manager (/orders) and not the strategy-scoped
 * child-detail route (/group-strategy/[groupId]/child/[accountId]), which
 * requires the account to be a StrategyMember and is the wrong tool for
 * "show me this account's history" - it's designed for live positions +
 * square-off within a specific strategy context, not a general account view.
 *
 * Kept as plain functions (no React) so both entry points can share the
 * exact same route-building logic and it's testable without a DOM.
 */

/** The single account order-history route both entry points navigate to. */
export function accountHistoryPath(accountId: string): string {
  return `/demat/${encodeURIComponent(accountId)}/history`;
}

/**
 * Builds the dedicated per-account orders API path
 * (backend/app/api/accounts.py::get_account_orders) - a separate endpoint
 * from the cross-account /trades/orders listing, so the standalone history
 * page never depends on Order Manager's own route.
 */
export function buildAccountOrdersApiPath(accountId: string): string {
  return `/accounts/${encodeURIComponent(accountId)}/orders?limit=100`;
}
