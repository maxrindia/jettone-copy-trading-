/**
 * Which login flow an account's "Re-login" uses - decided by the ACCOUNT'S OWN broker, never defaulted.
 *
 *   alice-oauth   Alice Blue (and its legacy/blank broker values): the existing browser OAuth redirect to
 *                 ant.aliceblueonline.com, unchanged.
 *   server-login  Vault brokers (Groww, Angel One): the engine logs in server-side with the account's stored,
 *                 encrypted credentials (POST /api/v1/accounts/{id}/broker-login). No redirect, no callback.
 *   interactive-login  Zerodha: Kite's login needs the owner's browser. The engine returns the Kite login URL; the owner signs in
 *                 and pastes the request_token Kite redirects with; the engine exchanges it for today's session.
 *   unsupported   Anything else: nothing is opened. An unknown broker is never sent to Alice Blue.
 *
 * The engine still decides whether a broker may log in at all (its registry refuses a disabled one with 409).
 */
import { extractError } from "@/lib/use-engine-data";
import { brokerLoginPath, usesVault, zerodhaLoginCompletePath, zerodhaLoginUrlPath } from "@/lib/vault-credentials";

export type ReloginRoute = "alice-oauth" | "server-login" | "interactive-login" | "unsupported";

// The registry's Alice Blue aliases (backend/app/services/brokers/registry.py); blank/legacy rows are Alice Blue.
const ALICE_IDS = new Set(["", "alicebluenew", "aliceblue", "alice_blue", "alice", "alice blue", "alice blue (ant / a3)"]);

export function reloginRoute(broker: string | null | undefined): ReloginRoute {
  const id = (broker ?? "").trim().toLowerCase();
  if (ALICE_IDS.has(id)) return "alice-oauth";
  if (id === "zerodha") return "interactive-login";
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

/** The request_token from whatever the owner pasted: the bare token, or the full redirect URL carrying `request_token=`. */
export function extractRequestToken(pasted: string | null | undefined): string | null {
  const text = (pasted ?? "").trim();
  if (!text) return null;
  const fromUrl = /[?&]request_token=([A-Za-z0-9]+)/.exec(text);
  const token = fromUrl ? fromUrl[1] : text;
  return /^[A-Za-z0-9]{1,256}$/.test(token) ? token : null;
}

export type InteractiveLoginResult = { ok: true } | { ok: false; status: number; message: string };

/**
 * Zerodha's interactive login: fetch the Kite login URL, hand it to `openUrl`, ask for the request_token with `askToken`, and
 * exchange it. Never throws; a cancelled prompt is a quiet "cancelled" failure, and no request_token is ever logged or returned.
 */
export async function zerodhaInteractiveLogin(
  accountId: string,
  openUrl: (url: string) => void,
  askToken: () => Promise<string | null> | string | null,
  fetchImpl: typeof fetch = fetch,
): Promise<InteractiveLoginResult> {
  try {
    const urlResp = await fetchImpl(zerodhaLoginUrlPath(accountId));
    const urlBody = (await urlResp.json().catch(() => null)) as { login_url?: unknown } | null;
    if (!urlResp.ok || typeof urlBody?.login_url !== "string") {
      return { ok: false, status: urlResp.status, message: extractError(urlBody) ?? `Could not get the Kite login URL (${urlResp.status})` };
    }
    openUrl(urlBody.login_url);
    const token = extractRequestToken(await askToken());
    if (!token) return { ok: false, status: 0, message: "Zerodha login cancelled: no request_token was provided." };
    const resp = await fetchImpl(zerodhaLoginCompletePath(accountId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_token: token }),
    });
    if (resp.ok) return { ok: true };
    const body = (await resp.json().catch(() => null)) as unknown;
    return { ok: false, status: resp.status, message: extractError(body) ?? `Zerodha login failed (${resp.status})` };
  } catch {
    return { ok: false, status: 0, message: "Could not reach the engine to log in. Try again." };
  }
}
