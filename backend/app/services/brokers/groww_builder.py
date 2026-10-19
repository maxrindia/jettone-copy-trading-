"""Builds a GrowwClient for an Account from the encrypted credential vault."""

from typing import Any, Optional

import httpx

from app.services.brokers.credential_vault import CredentialError, decrypt_groww
from app.services.groww.client import GrowwClient
from app.services.static_ip import extract_proxy_url


def build_groww_client(acc: Any, redis: Any, http_client: Optional[httpx.AsyncClient] = None) -> GrowwClient:
    """Raises CredentialError when the vault has no (readable) credentials. The
    account's own recorded static IP and its expiry are passed to the client,
    which refuses to send any order request without a valid one."""
    row = getattr(acc, "broker_credential", None)
    if row is None:
        raise CredentialError("no Groww credentials stored for this account")
    creds = decrypt_groww(row.encrypted_payload)
    return GrowwClient(
        account_id=str(acc.id),
        label=acc.alice_user_id,
        api_key=creds.api_key,
        api_secret=creds.api_secret,
        redis=redis,
        static_ip=getattr(acc, "static_ip_address", None),
        static_ip_valid_until=getattr(acc, "proxy_expires_on", None),
        http_client=http_client,
        proxy_url=extract_proxy_url(getattr(acc, "notes", None)),
    )
