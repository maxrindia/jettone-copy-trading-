"""
Encrypted broker credential vault
=================================
Brokers whose logins need several secrets keep them as a single Fernet-encrypted
JSON blob in `broker_credentials`:

  angelone: api_key, pin, totp_secret
  groww:    api_key, api_secret

Entries are write-only: `describe()` reports which fields are configured, never
their values. Plaintext exists only inside `decrypt_payload()` callers (client
construction) and is never logged or returned.
"""

import json
from typing import Any, Dict, Optional, Tuple

from app.core.crypto import decrypt_secret, encrypt_secret
from app.services.angel_one.client import totp_now

# broker id -> the exact fields its vault entry holds.
VAULT_FIELDS: Dict[str, Tuple[str, ...]] = {
    "angelone": ("api_key", "pin", "totp_secret"),
    "groww": ("api_key", "api_secret"),
}
ANGEL_FIELDS = VAULT_FIELDS["angelone"]
GROWW_FIELDS = VAULT_FIELDS["groww"]


class CredentialError(ValueError):
    """Invalid or missing broker credentials. Messages never contain secret values."""


class _Creds:
    """Immutable bag of secret fields whose repr never shows a value."""

    _fields: Tuple[str, ...] = ()

    def __init__(self, **values: str):
        for name in self._fields:
            object.__setattr__(self, name, values[name])

    def __setattr__(self, key, value):  # frozen
        raise AttributeError("credentials are immutable")

    def __eq__(self, other):
        return type(self) is type(other) and all(getattr(self, n) == getattr(other, n) for n in self._fields)

    def __hash__(self):
        return hash(tuple(getattr(self, n) for n in self._fields))

    def __repr__(self) -> str:
        return f"{type(self).__name__}(<redacted>)"


class AngelCredentials(_Creds):
    _fields = ANGEL_FIELDS


class GrowwCredentials(_Creds):
    _fields = GROWW_FIELDS


_TYPES = {"angelone": AngelCredentials, "groww": GrowwCredentials}


def _clean(broker: str, raw: Dict[str, Any]) -> Dict[str, str]:
    fields = VAULT_FIELDS.get(broker)
    if fields is None:
        raise CredentialError(f"Broker '{broker}' has no credential vault.")
    values: Dict[str, str] = {}
    missing = []
    for name in fields:
        v = str(raw.get(name) or "").strip()
        if not v:
            missing.append(name)
        values[name] = v
    if missing:
        raise CredentialError(f"Missing required {broker} credential field(s): {', '.join(missing)}.")
    return values


def validate_payload(broker: str, raw: Dict[str, Any]) -> _Creds:
    values = _clean(broker, raw)
    if broker == "angelone":
        try:
            totp_now(values["totp_secret"])
        except Exception as exc:
            raise CredentialError("totp_secret is not a valid base32 TOTP secret.") from exc
    return _TYPES[broker](**values)


def encrypt_payload(broker: str, creds: _Creds) -> str:
    return encrypt_secret(json.dumps({n: getattr(creds, n) for n in VAULT_FIELDS[broker]}))


def decrypt_payload(broker: str, ciphertext: str) -> _Creds:
    try:
        data = json.loads(decrypt_secret(ciphertext))
    except (ValueError, TypeError) as exc:
        raise CredentialError(f"Stored {broker} credentials are unreadable.") from exc
    if not isinstance(data, dict):
        raise CredentialError(f"Stored {broker} credentials are unreadable.")
    return validate_payload(broker, data)


# Angel One entry points (kept for existing callers).
def validate_angel_payload(raw: Dict[str, Any]) -> AngelCredentials:
    return validate_payload("angelone", raw)  # type: ignore[return-value]


def encrypt_angel(creds: AngelCredentials) -> str:
    return encrypt_payload("angelone", creds)


def decrypt_angel(ciphertext: str) -> AngelCredentials:
    return decrypt_payload("angelone", ciphertext)  # type: ignore[return-value]


# Groww entry points.
def validate_groww_payload(raw: Dict[str, Any]) -> GrowwCredentials:
    return validate_payload("groww", raw)  # type: ignore[return-value]


def decrypt_groww(ciphertext: str) -> GrowwCredentials:
    return decrypt_payload("groww", ciphertext)  # type: ignore[return-value]


def describe(row: Optional[Any], broker: Optional[str] = None) -> Dict[str, Any]:
    """Safe-to-return status of an account's vault entry (no values)."""
    if row is None:
        return {"configured": False, "fields": []}
    b = broker or getattr(row, "broker", None) or "angelone"
    fields = VAULT_FIELDS.get(b)
    if fields is None:
        return {"configured": False, "fields": []}
    try:
        decrypt_payload(b, row.encrypted_payload)
    except Exception:
        return {"configured": False, "fields": [], "error": "unreadable"}
    return {"configured": True, "fields": list(fields)}
