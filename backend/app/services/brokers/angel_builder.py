"""Builds an AngelOneClient for an Account from the encrypted credential vault."""

from typing import Any, Iterable, List, Optional

import httpx

from app.services.angel_one.client import AngelOneClient
from app.services.brokers.credential_vault import CredentialError, decrypt_angel
from app.services.static_ip import extract_proxy_url


def build_angel_client(acc: Any, redis: Any, http_client: Optional[httpx.AsyncClient] = None) -> AngelOneClient:
    """Raises CredentialError when the vault has no (readable) credentials.
    The account's own recorded static IP and its expiry date are passed to the
    client, which refuses to send orders without a valid one."""
    row = getattr(acc, "broker_credential", None)
    if row is None:
        raise CredentialError("no Angel One credentials stored for this account")
    creds = decrypt_angel(row.encrypted_payload)
    return AngelOneClient(
        account_id=str(acc.id),
        client_code=acc.alice_user_id,
        api_key=creds.api_key,
        pin=creds.pin,
        totp_secret=creds.totp_secret,
        redis=redis,
        static_ip=getattr(acc, "static_ip_address", None),
        static_ip_valid_until=getattr(acc, "proxy_expires_on", None),
        http_client=http_client,
        proxy_url=extract_proxy_url(getattr(acc, "notes", None)),
    )


def accounts_sharing_static_ip(accounts: Iterable[Any]) -> List[str]:
    """Ids of Angel One accounts whose recorded static IP is shared with another
    Angel One account. Each account needs its own registered IP, so the engine
    registers none of the accounts in a shared group."""
    by_ip: dict = {}
    for acc in accounts:
        ip = (getattr(acc, "static_ip_address", None) or "").strip()
        if ip:
            by_ip.setdefault(ip, []).append(str(acc.id))
    return [aid for ids in by_ip.values() if len(ids) > 1 for aid in ids]
