/**
 * Static IP display helpers. The counting, identity and expiry rules live in the
 * engine (backend/app/services/static_ip.py) - the single source of truth - and
 * reach the UI as `GET /system/static-ip` counts and per-account
 * `static_ip_status` / `static_ip_host` / `proxy_expires_on`. Nothing here
 * recomputes expiry.
 */

export type StaticIpStatus = "ACTIVE" | "EXPIRED" | "UNTRACKED";

/** Subset of GET /system/static-ip used by the Dashboard KPI. */
export interface StaticIpCounts {
  available_count: number;
  active_count: number;
  untracked_count: number;
  expired_count: number;
  /** Proxy configurations whose assigned public IP was not entered; not part of available_count. */
  unmapped_count?: number;
}

const IPV4_LITERAL = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;
const IPV6_LITERAL = /^[0-9a-f:.]+$/i;

/** True for "" (the field is optional) or a literal IPv4 / IPv6 address. Hostnames are not accepted. */
export function isValidAssignedIp(value: string): boolean {
  const v = value.trim();
  if (!v) return true;
  if (IPV4_LITERAL.test(v)) return true;
  return (v.match(/:/g) ?? []).length >= 2 && IPV6_LITERAL.test(v);
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-10-15" -> "15-Oct-2026" (date only; no timezone shifting). */
export function formatExpiry(iso: string | null | undefined): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso ?? "");
  if (!m) return "";
  const month = Number(m[2]);
  if (month < 1 || month > 12) return "";
  return `${m[3]}-${MONTHS[month - 1]}-${m[1]}`;
}

/** True for "" (expiry is optional) or a real calendar date as YYYY-MM-DD. */
export function isValidExpiryInput(value: string): boolean {
  if (!value) return true;
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!m) return false;
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const date = new Date(Date.UTC(y, mo - 1, d));
  return y >= 2000 && date.getUTCFullYear() === y && date.getUTCMonth() === mo - 1 && date.getUTCDate() === d;
}

/** Short KPI caption: what is counted, split by state; says so when expiry is not recorded. */
export function staticIpCaption(c: StaticIpCounts | null): string {
  if (!c) return "Loading…";
  const parts: string[] = [];
  if (c.active_count > 0) parts.push(`${c.active_count} active`);
  if (c.untracked_count > 0) parts.push(`${c.untracked_count} no expiry`);
  if (c.expired_count > 0) parts.push(`${c.expired_count} expired`);
  const unmapped = c.unmapped_count ?? 0;
  if (unmapped > 0) parts.push(`${unmapped} IP not entered`);
  return parts.length > 0 ? parts.join(" · ") : "No Static IP configured";
}

export function staticIpTitle(c: StaticIpCounts | null): string {
  if (!c) return "Loading Static IPs…";
  return (
    "Distinct, non-expired assigned Static IPs you entered on your Demat accounts. " +
    `Active (expiry date in the future): ${c.active_count}. ` +
    `Expiry not recorded: ${c.untracked_count}${c.untracked_count > 0 ? " (counted, but cannot be confirmed active)" : ""}. ` +
    `Expired (not counted): ${c.expired_count}.` +
    ((c.unmapped_count ?? 0) > 0
      ? ` Proxies with no assigned IP entered (not counted): ${c.unmapped_count}.`
      : "")
  );
}

export interface StaticIpDisplay {
  label: string;
  detail: string;
  tone: "green" | "red" | "gray";
  action: "Renew" | "Update" | "Set expiry";
}

/** What the Demat account card shows for a Static IP. Never reports "Expired" for a missing expiry. */
export function describeStaticIp(status: StaticIpStatus, expiresOn: string | null | undefined): StaticIpDisplay {
  const when = formatExpiry(expiresOn);
  if (status === "EXPIRED") return { label: "Static IP Expired", detail: when ? `Expired: ${when}` : "Expired", tone: "red", action: "Renew" };
  if (status === "ACTIVE") return { label: "Static IP Active", detail: when ? `Expires: ${when}` : "", tone: "green", action: "Update" };
  return { label: "Static IP", detail: "Configured · expiry not tracked", tone: "gray", action: "Set expiry" };
}
