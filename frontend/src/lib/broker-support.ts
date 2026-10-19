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

export interface BrokerCapabilities {
  login: boolean;
  as_master: boolean;
  as_child: boolean;
}

export interface CatalogEntry {
  id: string;
  support: BrokerSupport;
  selectable: boolean;
  support_note?: string;
  /** Setup/login, Master eligibility and Child eligibility are separate backend gates. */
  capabilities?: BrokerCapabilities;
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

/**
 * What a broker may be used for, as the engine's catalog reports it. Credential setup/login, Master
 * eligibility and Child eligibility are three separate gates and are never merged into one "supported" flag.
 *
 * This only DESCRIBES the backend's decision so the UI can disable what cannot work and say why. It is not a
 * security check: the engine enforces Master assignment itself (POST /strategies/{id}/switch-master refuses a
 * broker that cannot be a Master) and skips copies to a broker that cannot be a Child.
 *
 * Fail closed: with no catalog (not loaded or unreachable) only Alice Blue is reported as capable.
 */
export interface CapabilityView {
  /** false = the catalog has not told us anything about this broker */
  known: boolean;
  login: boolean;
  asMaster: boolean;
  asChild: boolean;
  /** The engine's own explanation (support_note), when it supplied one. */
  note: string;
}

const ALL_CAPABLE: CapabilityView = { known: true, login: true, asMaster: true, asChild: true, note: "" };
const NONE: CapabilityView = { known: false, login: false, asMaster: false, asChild: false, note: "" };

export function brokerCapabilities(
  brokerId: string | null | undefined,
  catalog?: readonly CatalogEntry[] | null,
): CapabilityView {
  const id = (brokerId ?? "").trim().toLowerCase();
  if (isLegacyAlice(id) || id === "alicebluenew") return ALL_CAPABLE;
  if (!catalog || catalog.length === 0) return NONE;
  const entry = catalog.find((c) => c.id === id);
  if (!entry) return { ...NONE, known: true };
  const note = entry.support_note ?? "";
  if (!entry.selectable) return { known: true, login: false, asMaster: false, asChild: false, note };
  if (entry.support === "SUPPORTED" && !entry.capabilities) return { ...ALL_CAPABLE, note };
  const c = entry.capabilities;
  if (!c) return { known: true, login: false, asMaster: false, asChild: false, note };
  return { known: true, login: c.login === true, asMaster: c.as_master === true, asChild: c.as_child === true, note };
}

export const MASTER_BLOCKED_FALLBACK = "This broker cannot be a strategy Master: its orders cannot be detected yet.";
export const CHILD_BLOCKED_FALLBACK = "This broker cannot receive copied orders yet.";
export const CAPABILITIES_UNKNOWN = "Broker capabilities have not loaded, so only Alice Blue can be used here.";

/** Why this account's broker cannot be a Master, or null when it can. */
export function masterBlocker(
  brokerId: string | null | undefined,
  catalog?: readonly CatalogEntry[] | null,
): string | null {
  const c = brokerCapabilities(brokerId, catalog);
  if (c.asMaster) return null;
  if (!c.known) return CAPABILITIES_UNKNOWN;
  return c.note || MASTER_BLOCKED_FALLBACK;
}

/** Why this account's broker cannot be a Child, or null when it can. */
export function childBlocker(
  brokerId: string | null | undefined,
  catalog?: readonly CatalogEntry[] | null,
): string | null {
  const c = brokerCapabilities(brokerId, catalog);
  if (c.asChild) return null;
  if (!c.known) return CAPABILITIES_UNKNOWN;
  return c.note || CHILD_BLOCKED_FALLBACK;
}
