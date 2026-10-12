const FALLBACK = "/";

/**
 * Turns the `callbackUrl` query value into a safe in-app path to open after
 * sign-in, or "/" when it is missing or not acceptable (open-redirect guard).
 *
 * Accepted: an absolute path on this app ("/orders", "/demat?x=1"), or a full
 * URL whose origin is exactly `origin` (NextAuth sometimes builds those).
 * Rejected: other origins, protocol-relative ("//host"), backslash tricks,
 * control characters, non-http schemes, the sign-in page itself (would loop)
 * and API/auth endpoints (not pages).
 */
export function safeCallbackPath(raw: string | null | undefined, origin: string): string {
  if (!raw || raw.length > 2048) return FALLBACK;
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f\\]/.test(raw)) return FALLBACK;
  if (raw.startsWith("//")) return FALLBACK;
  // Only "/path" or an http(s) URL; bare "orders" or "javascript:" never resolve to a target.
  if (!raw.startsWith("/") && !/^https?:\/\//i.test(raw)) return FALLBACK;

  let url: URL;
  try {
    url = new URL(raw, origin);
  } catch {
    return FALLBACK;
  }
  if (url.origin !== new URL(origin).origin) return FALLBACK;

  const path = url.pathname;
  if (!path.startsWith("/") || path.startsWith("//")) return FALLBACK;
  if (path === "/auth" || path.startsWith("/auth/") || path === "/api" || path.startsWith("/api/")) return FALLBACK;
  if (path.startsWith("/_next/")) return FALLBACK;

  return path + url.search + url.hash;
}
