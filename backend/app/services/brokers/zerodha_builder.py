"""Builds a ZerodhaClient for an Account from the encrypted credential vault."""

from typing import Any, Optional

import httpx

from app.services.brokers.credential_vault import CredentialError, decrypt_zerodha
from app.services.static_ip import extract_proxy_url
from app.services.zerodha.client import ZerodhaClient


def build_zerodha_client(acc: Any, redis: Any, http_client: Optional[httpx.AsyncClient] = None) -> ZerodhaClient:
    """Raises CredentialError when the vault has no (readable) credentials. Kite Connect's documentation states no static-IP
    requirement, so none is enforced here; an operator-configured proxy on the account is honoured as for the other brokers."""
    row = getattr(acc, "broker_credential", None)
    if row is None:
        raise CredentialError("no Zerodha credentials stored for this account")
    if getattr(row, "broker", None) != "zerodha":
        # Groww's vault entry has the same two fields; it must never be read as Zerodha credentials.
        raise CredentialError("the stored credentials are not Zerodha credentials")
    creds = decrypt_zerodha(row.encrypted_payload)
    return ZerodhaClient(
        account_id=str(acc.id),
        label=acc.alice_user_id,
        api_key=creds.api_key,
        api_secret=creds.api_secret,
        redis=redis,
        http_client=http_client,
        proxy_url=extract_proxy_url(getattr(acc, "notes", None)),
    )
