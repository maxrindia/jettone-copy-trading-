"""
Static IP identity, expiry status and availability counting.

A Static IP is not its own table: each demat account stores its proxy as a
``ProxyURL: https://user:pass@host[:port]`` fragment in ``accounts.notes`` (the
format the Alice Blue client/manager already parse) plus an optional
``accounts.proxy_expires_on`` date entered by the user. Nothing here changes how
the proxy is used for broker traffic - it only reads that configuration.

Identity: the normalised, lower-cased host. Credentials, scheme and port never
make two IPs different, so the same host on several accounts is one IP.

Expiry (date-only, Asia/Kolkata calendar):
  * ``proxy_expires_on`` is the LAST day the IP is valid. It is ACTIVE through
    that whole day and EXPIRED from the next day.
  * NULL expiry is UNTRACKED - never treated as expired and never given a
    made-up date.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

ACTIVE = "ACTIVE"
EXPIRED = "EXPIRED"
UNTRACKED = "UNTRACKED"


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


def static_ip_status(
    notes: Optional[str], expires_on: Optional[date], today: Optional[date] = None
) -> Optional[str]:
    """None when the account has no Static IP; else ACTIVE / EXPIRED / UNTRACKED."""
    if account_static_ip_host(notes) is None:
        return None
    if expires_on is None:
        return UNTRACKED
    return EXPIRED if (today or today_ist()) > expires_on else ACTIVE


@dataclass(frozen=True)
class StaticIpSummary:
    active: int
    untracked: int
    expired: int

    @property
    def available(self) -> int:
        """Distinct IPs that are not expired (tracked-active plus expiry-not-tracked)."""
        return self.active + self.untracked


def summarize_static_ips(accounts: Iterable, today: Optional[date] = None) -> StaticIpSummary:
    """
    Counts DISTINCT hosts across accounts (anything with ``notes`` and
    ``proxy_expires_on``). A host used by several accounts counts once; if its
    accounts disagree, the best status wins (ACTIVE > UNTRACKED > EXPIRED), so a
    renewed or still-valid use keeps the IP available. Accounts with no Static IP
    are ignored.
    """
    rank = {ACTIVE: 3, UNTRACKED: 2, EXPIRED: 1}
    best: dict[str, str] = {}
    for acc in accounts:
        host = account_static_ip_host(acc.notes)
        if host is None:
            continue
        status = static_ip_status(acc.notes, acc.proxy_expires_on, today)
        assert status is not None
        if host not in best or rank[status] > rank[best[host]]:
            best[host] = status
    values = list(best.values())
    return StaticIpSummary(
        active=values.count(ACTIVE),
        untracked=values.count(UNTRACKED),
        expired=values.count(EXPIRED),
    )
