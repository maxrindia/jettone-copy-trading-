export interface ProxyHostInput {
  host: string;
  /** Present only when the user typed "host:port"; otherwise the form's Port field applies. */
  port: string | null;
}

const IPV4 = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;
const HOSTNAME = /^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$/i;
const IPV6 = /^\[[0-9a-f:.]+\]$/i;

/**
 * Cleans what the user typed into the "Proxy Host" field. Accepts a bare IPv4
 * address or hostname ("178.18.252.165", "dc-mum-1.staticip.in") and also
 * tolerates a pasted "https://host:port/" by dropping the scheme and path.
 * Credentials are never read from this field (they have their own inputs), so a
 * value containing "user:pass@" is rejected instead of silently dropped.
 * Returns null when the result is not a valid IPv4 address / hostname / [IPv6].
 */
export function normalizeProxyHost(raw: string): ProxyHostInput | null {
  let value = raw.trim();
  if (!value) return null;
  value = value.replace(/^[a-z][a-z0-9+.-]*:\/\//i, "");
  value = value.split(/[/?#]/, 1)[0];
  if (value.includes("@")) return null;

  let host = value;
  let port: string | null = null;
  const portMatch = /^(.*):(\d+)$/.exec(value);
  if (portMatch && !IPV6.test(value)) {
    host = portMatch[1];
    port = portMatch[2];
    const portNum = Number(port);
    if (!Number.isInteger(portNum) || portNum < 1 || portNum > 65535) return null;
  }
  if (!(IPV4.test(host) || HOSTNAME.test(host) || IPV6.test(host))) return null;
  // All-numeric dotted values must be real IPv4 (HOSTNAME alone would accept "999.1.1.1").
  if (/^[\d.]+$/.test(host) && !IPV4.test(host)) return null;
  return { host, port };
}
