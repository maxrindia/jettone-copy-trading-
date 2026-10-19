/**
 * Which login flow an account's "Re-login" uses - decided by the ACCOUNT'S OWN broker, never defaulted.
 *
 *   alice-oauth   Alice Blue (and its legacy/blank broker values): the existing browser OAuth redirect to
 *                 ant.aliceblueonline.com, unchanged.
 *   server-login  Vault brokers (Groww, Angel One): the engine logs in server-side with the account's stored,
 *                 encrypted credentials (POST /api/v1/accounts/{id}/broker-login). No redirect, no callback.
 *   unsupported   Anything else: nothing is opened. An unknown broker is never sent to Alice Blue.
 *
 * The engine still decides whether a broker may log in at all (its registry refuses a disabled one with 409).
 */
import { extractError } from "@/lib/use-engine-data";
import { brokerLoginPath, usesVault } from "@/lib/vault-credentials";

export type ReloginRoute = "alice-oauth" | "server-login" | "unsupported";

// The registry's Alice Blue aliases (backend/app/services/brokers/registry.py); blank/legacy rows are Alice Blue.
const ALICE_IDS = new Set(["", "alicebluenew", "aliceblue", "alice_blue", "alice", "alice blue", "alice blue (ant / a3)"]);

export function reloginRoute(broker: string | null | undefined): ReloginRoute {
  const id = (broker ?? "").trim().toLowerCase();
  if (ALICE_IDS.has(id)) return "alice-oauth";
  if (usesVault(id)) return "server-login";
  return "unsupported";
}

export const UNSUPPORTED_RELOGIN =
  "Re-login is not available for this account's broker. It is never sent to another broker's login page.";

export function serverLoginPrompt(displayName: string, broker: string): string {
  const name = broker.trim().toLowerCase() === "groww" ? "Groww" : "Angel One";
  const approval =
    name === "Groww" ? " Groww requires today's approval of this API key on Groww Cloud (API Keys page) first." : "";
  return `Log ${displayName} in to ${name} with its stored credentials?${approval}`;
}

export type ServerLoginResult = { ok: true } | { ok: false; status: number; message: string };

/** POST /broker-login. Never throws; the engine's message (no secrets) is returned on failure. */
export async function serverBrokerLogin(
  accountId: string,
  fetchImpl: typeof fetch = fetch,
): Promise<ServerLoginResult> {
  try {
    const resp = await fetchImpl(brokerLoginPath(accountId), { method: "POST" });
    if (resp.ok) return { ok: true };
    const body = (await resp.json().catch(() => null)) as unknown;
    return { ok: false, status: resp.status, message: extractError(body) ?? `Broker login failed (${resp.status})` };
  } catch {
    return { ok: false, status: 0, message: "Could not reach the engine to log in. Try again." };
  }
}
