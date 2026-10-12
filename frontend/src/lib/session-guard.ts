let signingOut = false;

/**
 * An explicit logout is in flight: NextAuth is about to navigate to the
 * sign-in page itself. Background polls that now come back 401 must not start
 * a second, competing navigation (it restarts the page load and lands on
 * /auth/signin?callbackUrl=... instead of the plain sign-in URL).
 */
export function setSigningOut(value: boolean): void {
  signingOut = value;
}

// Browser Back can restore this page from the back/forward cache with the
// flag still set (and polls silenced). We logged out in this page, so reload
// it: the middleware then sends the signed-out visitor to the sign-in page.
if (typeof window !== "undefined") {
  window.addEventListener("pageshow", (event) => {
    if (event.persisted && signingOut) window.location.reload();
  });
}

/**
 * Called when an authenticated-only API request comes back unauthenticated
 * (the middleware answers 401 JSON for /api/* without a session). The session
 * is gone, so leave the protected page for the sign-in page - same target as
 * the middleware's own redirect for page requests.
 */
export function redirectToSignIn(): void {
  if (typeof window === "undefined" || signingOut) return;
  if (window.location.pathname.startsWith("/auth/signin")) return;
  const here = window.location.pathname + window.location.search;
  window.location.replace(`/auth/signin?callbackUrl=${encodeURIComponent(here)}`);
}

/** True when the response says "not signed in" (401) or was bounced to the sign-in page. */
export function isUnauthenticated(resp: Response): boolean {
  return resp.status === 401 || (resp.redirected && new URL(resp.url).pathname.startsWith("/auth/signin"));
}
