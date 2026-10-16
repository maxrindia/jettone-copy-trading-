/**
 * Which brokers can be added, logged in and traded. The engine's registry
 * (backend/app/services/brokers/registry.py) is the authority and rejects any
 * other broker with 400/409; this UI only mirrors it so the user is not offered
 * a choice that cannot work.
 *
 * The live catalog (GET /api/v1/accounts/supported-brokers) is used when it has
 * loaded. Until then - or if it cannot be loaded - only Alice Blue is
 * selectable (fail closed).
 */
export type BrokerSupport = "SUPPORTED" | "PARTIAL" | "NOT_SUPPORTED";

export interface CatalogEntry {
  id: string;
  support: BrokerSupport;
  selectable: boolean;
  support_note?: string;
}

const FALLBACK: Readonly<Record<string, BrokerSupport>> = {
  alicebluenew: "SUPPORTED",
};

export const NOT_SUPPORTED_HINT = "Not supported yet";
export const PARTIAL_HINT = "Limited support";

function isLegacyAlice(id: string): boolean {
  return !id || id === "alice blue" || id === "aliceblue";
}

export function brokerSupport(
  brokerId: string | null | undefined,
  catalog?: readonly CatalogEntry[] | null,
): BrokerSupport {
  const id = (brokerId ?? "").trim().toLowerCase();
  if (isLegacyAlice(id)) return "SUPPORTED"; // legacy Alice values
  if (catalog && catalog.length > 0) {
    const entry = catalog.find((c) => c.id === id);
    return entry && entry.selectable ? entry.support : "NOT_SUPPORTED";
  }
  return FALLBACK[id] ?? "NOT_SUPPORTED";
}

export function isBrokerSelectable(
  brokerId: string | null | undefined,
  catalog?: readonly CatalogEntry[] | null,
): boolean {
  return brokerSupport(brokerId, catalog) !== "NOT_SUPPORTED";
}

/** Marks every option whose broker is not selectable as disabled with a hint. */
export function withBrokerSupport<T extends { value: string; label: string }>(
  options: readonly T[],
  catalog?: readonly CatalogEntry[] | null,
): (T & { disabled?: boolean; hint?: string })[] {
  return options.map((o) => {
    const support = brokerSupport(o.value, catalog);
    if (support === "NOT_SUPPORTED") return { ...o, disabled: true, hint: NOT_SUPPORTED_HINT };
    if (support === "PARTIAL") return { ...o, hint: PARTIAL_HINT };
    return o;
  });
}
