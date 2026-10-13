/**
 * Extracts the StaticIP.in per-account proxy address from an Account's free-
 * text `notes` field, redacted for display.
 *
 * There is no dedicated `proxy_url` column - the AddAccountModal writes it
 * as a "ProxyURL: <value>" fragment inside `notes` (see AddAccountModal.tsx,
 * the notesString join), and backend/app/api/accounts.py::verify_connection
 * reads it back with the exact same comma-split / case-insensitive
 * "proxyurl:" prefix match this mirrors, then computes its own redacted
 * `safe_proxy` before ever logging it. This does the same redaction for the
 * UI: an account's ProxyURL commonly embeds basic-auth credentials
 * (`https://user:pass@host`), which must never be rendered as visible text
 * (project rule: no plaintext secrets - never log tokens, secrets, or
 * passwords) - only the host is shown.
 */
export function accountProxyHost(notes: string | null | undefined): string | null {
  if (!notes) return null;
  let rawProxyUrl: string | null = null;
  for (const rawPart of notes.split(",")) {
    const part = rawPart.trim();
    const lower = part.toLowerCase();
    if (lower.startsWith("proxyurl:")) {
      rawProxyUrl = part.slice("proxyurl:".length).trim();
      break;
    }
    if (lower.startsWith("http://") || lower.startsWith("https://")) {
      rawProxyUrl = part;
      break;
    }
  }
  if (!rawProxyUrl) return null;
  // Strip "user:pass@" credentials - keep only the host (+ path, if any).
  const atIndex = rawProxyUrl.lastIndexOf("@");
  return atIndex === -1 ? rawProxyUrl : rawProxyUrl.slice(atIndex + 1);
}
