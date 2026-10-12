import { normalizeProxyHost } from "./proxy-host";

export interface ProxyFields {
  host: string;
  port: string;
  username: string;
  password: string;
  /** Defaults to https (the format already in production); http is kept when an existing proxy uses it. */
  scheme?: "http" | "https";
}

/**
 * Decomposes an existing `https://user:pass@host[:port]` ProxyURL (see
 * backend/app/services/alice_blue/manager.py's identical parser and
 * lib/account-proxy.ts's read-only display version) into the 4 fields the
 * Host / IP form edits. Never call this to DISPLAY the result - it recovers the
 * plaintext password so the Password input can be pre-filled.
 */
export function parseProxyUrl(raw: string): ProxyFields | null {
  try {
    const url = new URL(raw.trim());
    if (!url.hostname) return null;
    const scheme = url.protocol === "http:" ? "http" : "https";
    return {
      host: url.hostname,
      port: url.port || (scheme === "http" ? "80" : "443"),
      username: decodeURIComponent(url.username),
      password: decodeURIComponent(url.password),
      scheme,
    };
  } catch {
    return null;
  }
}

/** Inverse of parseProxyUrl - the exact string format the backend already
 * expects (see manager.py / accounts.py's "proxyurl:" prefix parsing).
 * Port is omitted when it's the https default (443), matching the format
 * already in production. */
export function buildProxyUrl({ host, port, username, password, scheme = "https" }: ProxyFields): string {
  const auth = `${encodeURIComponent(username)}:${encodeURIComponent(password)}`;
  const defaultPort = scheme === "http" ? "80" : "443";
  const portSuffix = port && port !== defaultPort ? `:${port}` : "";
  return `${scheme}://${auth}@${host}${portSuffix}`;
}

export type ProxyUrlCheck = { ok: true; url: string; host: string } | { ok: false; error: string };

/**
 * Validates and normalises a full Proxy URL typed in "Proxy URL" mode.
 * Requires an http(s) scheme, a valid IPv4 / hostname, a valid port when given,
 * and credentials that are either both present or both absent (a provider may
 * authenticate by whitelist instead). Any path / query / hash is dropped, and the
 * credentials are re-encoded so the stored string parses the same way the engine
 * parses it. The scheme is preserved as typed.
 */
export function validateProxyUrlInput(raw: string): ProxyUrlCheck {
  const value = raw.trim();
  if (!value) return { ok: false, error: "Please enter the Proxy URL." };
  if (!/^https?:\/\//i.test(value)) {
    return { ok: false, error: "Proxy URL must start with http:// or https://" };
  }
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return { ok: false, error: "Proxy URL is not valid (expected https://username:password@host:port)." };
  }
  const parsedHost = normalizeProxyHost(url.hostname);
  if (!parsedHost) return { ok: false, error: "Proxy URL host must be a valid IPv4 address or hostname." };

  if (url.port) {
    const port = Number(url.port);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      return { ok: false, error: "Proxy URL port must be between 1 and 65535." };
    }
  }

  let username = "";
  let password = "";
  try {
    username = decodeURIComponent(url.username);
    password = decodeURIComponent(url.password);
  } catch {
    return { ok: false, error: "Proxy URL username or password is not correctly encoded." };
  }
  if (Boolean(username) !== Boolean(password)) {
    return { ok: false, error: "Proxy URL needs both a username and a password (or neither)." };
  }

  const auth = username ? `${encodeURIComponent(username)}:${encodeURIComponent(password)}@` : "";
  const port = url.port ? `:${url.port}` : "";
  return { ok: true, url: `${url.protocol}//${auth}${parsedHost.host}${port}`, host: parsedHost.host };
}
