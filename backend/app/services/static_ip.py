"""
Static IP identity, expiry status and availability counting.

A Static IP is not its own table: each demat account stores its proxy as a
``ProxyURL: https://user:pass@host[:port]`` fragment in ``accounts.notes`` (the
format the Alice Blue client/manager already parse) plus an optional
``accounts.proxy_expires_on`` date entered by the user. Nothing here changes how
the proxy is used for broker traffic - it only reads that configuration.

Identity: the normalised, lower-cased host plus the proxy username. A provider
such as staticip.in hands out ONE gateway host (e.g. dc-mum-007.staticip.in)
and distinguishes each purchased IP by its credentials, so two accounts with the
same host but different usernames are two Static IPs. The same host AND the
same username (scheme, port and password never matter) is one IP shared by
several accounts. Only the host is ever shown or returned by the API; the
username is used for comparison and never leaves this module.

Expiry (date-only, Asia/Kolkata calendar):
  * ``proxy_expires_on`` is the LAST day the IP is valid. It is ACTIVE through
    that whole day and EXPIRED from the next day.
  * NULL expiry is UNTRACKED - never treated as expired and never given a
    made-up date.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

ACTIVE = "ACTIVE"
EXPIRED = "EXPIRED"
UNTRACKED = "UNTRACKED"


def normalize_ip_address(value: Optional[str]) -> Optional[str]:
    """Canonical text of an IPv4/IPv6 address, or None when blank. Raises
    ValueError for anything that is not a literal IP address (a hostname is
    never accepted: the field records the ASSIGNED public IP, not a gateway)."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return str(ipaddress.ip_address(text))


def today_ist() -> date:
    return datetime.now(IST).date()


def extract_proxy_url(notes: Optional[str]) -> Optional[str]:
    """Same comma-split / prefix rules as AliceBlueAccountManager._build_client."""
    if not notes:
        return None
    for part in notes.split(","):
        part = part.strip()
        lower = part.lower()
        if lower.startswith("proxyurl:"):
            return part[len("proxyurl:"):].strip() or None
        if lower.startswith("http://") or lower.startswith("https://"):
            return part
    return None


def normalize_host(proxy_url: Optional[str]) -> Optional[str]:
    """Lower-cased host of a proxy URL (no credentials, scheme or port), or None."""
    if not proxy_url:
        return None
    raw = proxy_url.strip()
    if not raw:
        return None
    try:
        host = urlsplit(raw if "://" in raw else f"//{raw}").hostname
    except ValueError:
        host = None
    if not host:
        # Legacy / malformed value: take what follows the last "@", drop scheme, path and port.
        tail = raw.rsplit("@", 1)[-1]
        tail = tail.split("://", 1)[-1]
        host = tail.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].rsplit(":", 1)[0]
    host = host.strip().strip("[]").lower()
    return host or None


def account_static_ip_host(notes: Optional[str]) -> Optional[str]:
    return normalize_host(extract_proxy_url(notes))


def static_ip_identity(proxy_url: Optional[str]) -> Optional[str]:
    """`username@host` (or just `host` when the URL carries no username). Used
    only to decide whether two accounts share the same Static IP."""
    host = normalize_host(proxy_url)
    if host is None:
        return None
    raw = (proxy_url or "").strip()
    try:
        user = urlsplit(raw if "://" in raw else f"//{raw}").username
    except ValueError:
        user = None
    return f"{user}@{host}" if user else host


def account_static_ip_identity(notes: Optional[str]) -> Optional[str]:
    return static_ip_identity(extract_proxy_url(notes))


def static_ip_status(
    notes: Optional[str],
    expires_on: Optional[date],
    today: Optional[date] = None,
    assigned_ip: Optional[str] = None,
) -> Optional[str]:
    """None when the account has no Static IP (no proxy and no assigned IP);
    else ACTIVE / EXPIRED / UNTRACKED."""
    if account_static_ip_host(notes) is None and not assigned_ip:
        return None
    if expires_on is None:
        return UNTRACKED
    return EXPIRED if (today or today_ist()) > expires_on else ACTIVE


def account_assigned_ip(acc) -> Optional[str]:
    """The account's recorded assigned public IP (canonical), or None."""
    try:
        return normalize_ip_address(getattr(acc, "static_ip_address", None))
    except ValueError:
        return None


def account_static_ip_key(acc) -> Optional[str]:
    """Identity used to share one expiry between accounts: the assigned public
    IP when recorded, else the proxy's host + username (best effort only)."""
    ip = account_assigned_ip(acc)
    if ip:
        return f"ip:{ip}"
    identity = account_static_ip_identity(getattr(acc, "notes", None))
    return f"proxy:{identity}" if identity else None


@dataclass(frozen=True)
class StaticIpSummary:
    active: int
    untracked: int
    expired: int
    # Distinct proxy configurations (host + username) whose assigned public IP
    # has not been recorded. They are NOT counted in `available`.
    unmapped: int = 0

    @property
    def available(self) -> int:
        """Distinct IPs that are not expired (tracked-active plus expiry-not-tracked)."""
        return self.active + self.untracked


def summarize_static_ips(accounts: Iterable, today: Optional[date] = None) -> StaticIpSummary:
    """
    Counts DISTINCT, VERIFIED assigned public IPs across accounts: an IP is
    verified only when its address was recorded on an account
    (``static_ip_address``). Gateway hostnames and proxy usernames are never
    treated as an IP. An IP used by several accounts counts once; if its
    accounts disagree, the best status wins (ACTIVE > UNTRACKED > EXPIRED), so a
    renewed or still-valid use keeps the IP available. Expiry comes from
    ``proxy_expires_on`` (a missing expiry is UNTRACKED, never expired).

    A proxy with no recorded assigned IP is reported in ``unmapped`` (distinct
    host + username) and is NOT counted as available. Accounts with neither a
    proxy nor an assigned IP are ignored.
    """
    rank = {ACTIVE: 3, UNTRACKED: 2, EXPIRED: 1}
    best: dict[str, str] = {}
    unmapped: set[str] = set()
    for acc in accounts:
        ip = account_assigned_ip(acc)
        if ip is None:
            identity = account_static_ip_identity(getattr(acc, "notes", None))
            if identity is not None:
                unmapped.add(identity)
            continue
        status = static_ip_status(getattr(acc, "notes", None), acc.proxy_expires_on, today, assigned_ip=ip)
        assert status is not None
        if ip not in best or rank[status] > rank[best[ip]]:
            best[ip] = status
    values = list(best.values())
    return StaticIpSummary(
        active=values.count(ACTIVE),
        untracked=values.count(UNTRACKED),
        expired=values.count(EXPIRED),
        unmapped=len(unmapped),
    )
