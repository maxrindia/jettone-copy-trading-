import "client-only";

/**
 * Alice Blue's documented OAuth web login (ant.aliceblueonline.com) accepts
 * only an `appcode` parameter — there is no documented way to force a fresh
 * login, log out an existing browser session, or pin the flow to a specific
 * user (see https://ant.aliceblueonline.com/productdocumentation/Vendors/).
 * The `userId` we pass is an undocumented convenience hint only.
 *
 * If the browser already holds an Alice Blue session for a different user,
 * Alice Blue silently reuses it and skips the QR/password screen entirely.
 * Jettone's backend correctly detects and rejects that user-ID mismatch
 * (backend/app/api/accounts.py::handle_demat_callback), but only after the
 * round trip. Warn the user before redirecting so they can log out of Alice
 * Blue themselves first, instead of hitting that rejection blind.
 */
export function confirmAliceBlueLogin(targetUserId: string): boolean {
  if (typeof window === "undefined") return true;
  return window.confirm(
    `You're about to log in to Alice Blue for account ${targetUserId}.\n\n` +
      `Alice Blue does not support forcing a fresh login from Jettone: if this browser is already logged into Alice Blue as a DIFFERENT user, Alice Blue will skip the QR/password screen and silently reuse that session. Jettone will then reject it as a mismatch instead of connecting the wrong account.\n\n` +
      `If you're not sure this browser is logged out of any other Alice Blue account, open ant.aliceblueonline.com and log out first, then click OK to continue.`,
  );
}

const RECONNECT_KEYS = {
  accountId: "reconnecting_account_id",
  aliceUserId: "reconnecting_alice_user_id",
  startedAt: "reconnecting_started_at",
} as const;

// A real Alice Blue login (even the silent, session-reused kind) completes
// within seconds; 15 minutes is generous headroom before we stop trusting a
// leftover value as belonging to a login the user is actively completing.
const RECONNECT_TARGET_TTL_MS = 15 * 60 * 1000;

/**
 * Records which account an Alice Blue login redirect is for, so the
 * /platform/dematconnected callback page can recover it after the round
 * trip. Written to both sessionStorage and localStorage: the login tab is
 * opened with rel="noopener", which means it does not inherit the opener's
 * sessionStorage, so localStorage is the only channel actually available to
 * carry this across — Alice Blue's redirect itself only echoes back
 * `authCode`/`userId`, nothing we pass through it (see confirmAliceBlueLogin
 * above for why no custom parameter survives that round trip).
 */
export function setReconnectTarget(accountId: string, aliceUserId: string | null | undefined): void {
  if (typeof window === "undefined" || !accountId) return;
  const startedAt = Date.now().toString();
  for (const storage of [window.sessionStorage, window.localStorage]) {
    storage.setItem(RECONNECT_KEYS.accountId, accountId);
    storage.setItem(RECONNECT_KEYS.aliceUserId, aliceUserId ?? "");
    storage.setItem(RECONNECT_KEYS.startedAt, startedAt);
  }
}

/**
 * Reads the reconnect target set by setReconnectTarget() and immediately
 * clears it from both storages — single-use, so a page reload of the
 * callback tab, or a second reconnect started before the first one's
 * redirect lands, can't silently reuse or clobber this value. Also treats
 * anything older than RECONNECT_TARGET_TTL_MS as absent, since that can only
 * be a stale leftover, not the login currently in progress.
 */
export function consumeReconnectTarget(): { accountId: string | null; aliceUserId: string | null } {
  const empty = { accountId: null, aliceUserId: null };
  if (typeof window === "undefined") return empty;

  const read = (storage: Storage) => ({
    accountId: storage.getItem(RECONNECT_KEYS.accountId),
    aliceUserId: storage.getItem(RECONNECT_KEYS.aliceUserId),
    startedAt: storage.getItem(RECONNECT_KEYS.startedAt),
  });
  const fromSession = read(window.sessionStorage);
  const fromLocal = read(window.localStorage);

  for (const storage of [window.sessionStorage, window.localStorage]) {
    storage.removeItem(RECONNECT_KEYS.accountId);
    storage.removeItem(RECONNECT_KEYS.aliceUserId);
    storage.removeItem(RECONNECT_KEYS.startedAt);
  }

  const picked = fromSession.accountId ? fromSession : fromLocal;
  if (!picked.accountId || !picked.startedAt) return empty;

  const age = Date.now() - Number(picked.startedAt);
  if (!Number.isFinite(age) || age < 0 || age > RECONNECT_TARGET_TTL_MS) return empty;

  return { accountId: picked.accountId, aliceUserId: picked.aliceUserId || null };
}

/**
 * Records, server-side, which account a Reconnect click is about to
 * authenticate (backend/app/api/accounts.py::record_reconnect_intent).
 *
 * setReconnectTarget() above only works when the tab that opens Alice
 * Blue's login and the tab the OAuth redirect lands back on share the same
 * origin. Jettone is reachable on two host ports (3000 and 8080), but Alice
 * Blue's registered redirect_uri is fixed to one of them — localStorage is
 * scoped per-origin *including port*, so whenever the user is browsing on
 * the other port, that stored value is invisible on the callback page. This
 * call goes through the same-origin /api/v1 proxy no matter which port the
 * browser is on, and the engine relays it to Redis — so it survives the
 * port mismatch. Await this before opening the login tab so the intent is
 * guaranteed to be recorded before Alice Blue can redirect back.
 */
export async function recordReconnectIntent(accountId: string): Promise<void> {
  if (typeof window === "undefined" || !accountId) return;
  try {
    await fetch(`/api/v1/accounts/${accountId}/reconnect-intent`, { method: "POST" });
  } catch {
    // Best-effort: setReconnectTarget()'s localStorage value still covers
    // the same-origin case, and the callback rejects cleanly either way if
    // neither source has a matching intent.
  }
}
