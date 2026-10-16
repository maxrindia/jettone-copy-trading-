"""
Encrypted broker credential vault
=================================
Angel One needs three secrets (API key, PIN, TOTP secret). They are stored as a
single Fernet-encrypted JSON blob in `broker_credentials` and are write-only:
`describe()` reports which fields are configured, never their values. Plaintext
exists only inside `decrypt()` callers (client construction) and is never
logged or returned.
"""

import json
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.core.crypto import decrypt_secret, encrypt_secret
from app.services.angel_one.client import totp_now

ANGEL_FIELDS = ("api_key", "pin", "totp_secret")


class CredentialError(ValueError):
    """Invalid or missing broker credentials. Messages never contain secret values."""


@dataclass(frozen=True)
class AngelCredentials:
    api_key: str
    pin: str
    totp_secret: str

    def __repr__(self) -> str:
        return "AngelCredentials(<redacted>)"


def validate_angel_payload(raw: Dict[str, Any]) -> AngelCredentials:
    values: Dict[str, str] = {}
    missing = []
    for name in ANGEL_FIELDS:
        v = str(raw.get(name) or "").strip()
        if not v:
            missing.append(name)
        values[name] = v
    if missing:
        raise CredentialError(f"Missing required Angel One credential field(s): {', '.join(missing)}.")
    try:
        totp_now(values["totp_secret"])
    except Exception as exc:
        raise CredentialError("totp_secret is not a valid base32 TOTP secret.") from exc
    return AngelCredentials(**values)


def encrypt_angel(creds: AngelCredentials) -> str:
    return encrypt_secret(json.dumps({n: getattr(creds, n) for n in ANGEL_FIELDS}))


def decrypt_angel(ciphertext: str) -> AngelCredentials:
    try:
        data = json.loads(decrypt_secret(ciphertext))
    except (ValueError, TypeError) as exc:
        raise CredentialError("Stored Angel One credentials are unreadable.") from exc
    return validate_angel_payload(data)


def describe(row: Optional[Any]) -> Dict[str, Any]:
    """Safe-to-return status of an account's vault entry (no values)."""
    if row is None:
        return {"configured": False, "fields": []}
    try:
        decrypt_angel(row.encrypted_payload)
    except Exception:
        return {"configured": False, "fields": [], "error": "unreadable"}
    return {"configured": True, "fields": list(ANGEL_FIELDS)}
