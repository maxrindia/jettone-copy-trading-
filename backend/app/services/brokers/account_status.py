"""
What an account's broker allows and whether the account is connected, as the SERVER reports it.

The registry (brokers/registry.py) is the single authority on which brokers may log in and in which roles, so this only reshapes
it for an account response: the UI shows what the engine says (and why a role is blocked) instead of guessing from a broker name.

Identity: a successful login proves the credentials worked, not that they belong to the account NAMED in Jettone. How much each broker
lets Jettone confirm is reported per broker (`identity`), and a broker that cannot confirm it says so wherever the account is shown.
That statement is informational only: it never grants or removes a Master / Child role.

Setting an account up is not the same as copy trading with it. `login` says an account can be set up and signed in;
`as_master` / `as_child` say whether it may take part in copying, and each blocked role carries the reason. Nothing here enables
or relaxes any gate.
"""
from typing import Any, Dict, Optional, Tuple

from app.services.brokers.registry import BrokerSupport, get_broker_spec

CONNECTED = "CONNECTED"
DISCONNECTED = "DISCONNECTED"
STATUS_UNKNOWN = "STATUS_UNKNOWN"
NEEDS_CREDENTIALS = "NEEDS_CREDENTIALS"
BROKER_NOT_ENABLED = "BROKER_NOT_ENABLED"
UNSUPPORTED_BROKER = "UNSUPPORTED_BROKER"

# How far a successful login confirms WHICH broker account it was.
#   CONFIRMED         the broker returns the signed-in user id and Jettone requires it to match the account
#   CREDENTIAL_BOUND  the login is made with this account's own id / client code and secrets; Jettone does not use the response to confirm an identity
#   NOT_AVAILABLE     the login response carries nothing that ties the credentials to a named account
IDENTITY_CONFIRMED = "CONFIRMED"
IDENTITY_CREDENTIAL_BOUND = "CREDENTIAL_BOUND"
IDENTITY_NOT_AVAILABLE = "NOT_AVAILABLE"

_IDENTITY: Dict[str, Tuple[str, str]] = {
    "zerodha": (IDENTITY_CONFIRMED, "Kite returns the signed-in user id and Jettone requires it to match this account."),
    "alicebluenew": (
        IDENTITY_CREDENTIAL_BOUND,
        "The login is made with this account's own user id and secret. Jettone does not use the login response to confirm the identity independently.",
    ),
    "angelone": (
        IDENTITY_CREDENTIAL_BOUND,
        "The login is made with this account's own client code, PIN and TOTP secret. Jettone does not use the login response to confirm the identity independently.",
    ),
    "groww": (
        IDENTITY_NOT_AVAILABLE,
        "The Groww token response Jettone uses carries no account identity, so Jettone cannot confirm that this API key belongs to the account named here.",
    ),
}
_IDENTITY_UNKNOWN = (IDENTITY_NOT_AVAILABLE, "Jettone cannot confirm which broker account a login belongs to for this broker.")


def identity_assurance(broker_id: Optional[str]) -> Dict[str, str]:
    """{"level", "note"}: what a successful login does and does not prove about the account's identity."""
    spec = get_broker_spec(broker_id)
    level, note = _IDENTITY.get(spec.id, _IDENTITY_UNKNOWN) if spec else _IDENTITY_UNKNOWN
    return {"level": level, "note": note}


MASTER_BLOCKED_FALLBACK = "This broker cannot be a strategy Master: its orders cannot be detected yet."
CHILD_BLOCKED_FALLBACK = "This broker cannot receive copied orders yet."

# What "not connected" means to the person, per broker. Alice Blue and Zerodha need the owner's browser; the others sign in server-side.
_RECONNECT_HINT = {
    "alicebluenew": "Use Re-login to sign in to Alice Blue.",
    "zerodha": "Use Re-login to sign in to Kite. The Kite session ends at 6 AM the next day.",
    "groww": "Use Re-login. Groww needs today's approval of this API key on Groww Cloud first.",
    "angelone": "Use Re-login to sign in to Angel One with the stored credentials.",
}


def broker_capabilities(broker_id: Optional[str]) -> Dict[str, Any]:
    """Setup / Master / Child availability for a stored broker value, each blocked role with its reason."""
    spec = get_broker_spec(broker_id)
    if spec is None:
        reason = "Unknown broker: it cannot be set up or used."
        return {
            "broker_id": None, "support": BrokerSupport.NOT_SUPPORTED.value, "login": False, "as_master": False, "as_child": False,
            "master_blocker": reason, "child_blocker": reason, "note": reason,
            "identity": {"level": IDENTITY_NOT_AVAILABLE, "note": _IDENTITY_UNKNOWN[1]},
        }
    caps = spec.capabilities
    return {
        "broker_id": spec.id,
        "support": spec.support.value,
        "login": bool(caps.login),
        "as_master": bool(caps.as_master),
        "as_child": bool(caps.as_child),
        "master_blocker": None if caps.as_master else (spec.reason or MASTER_BLOCKED_FALLBACK),
        "child_blocker": None if caps.as_child else (spec.reason or CHILD_BLOCKED_FALLBACK),
        "note": spec.reason or "",
        "identity": identity_assurance(spec.id),
    }


def connection_status(
    broker_id: Optional[str], session_valid: bool, credentials_configured: Optional[bool],
    session_store_available: Optional[bool] = None,
) -> Tuple[str, str]:
    """(status, detail). `credentials_configured` None means "not looked up": it is never read as missing or present.
    `session_store_available` False means Jettone could not check this account's session (the session store was unreachable): the
    state is then reported as unknown, never as connected and never as a confirmed sign-out.

    CONNECTED is reported only when the account's session is currently valid; a broker that is not enabled, an account without
    credentials, and an expired session each get their own state, so a connected-looking badge is never shown for any of them.
    """
    spec = get_broker_spec(broker_id)
    if spec is None:
        return UNSUPPORTED_BROKER, "This account's broker is not recognised, so it cannot be connected."
    if not spec.capabilities.login:
        return BROKER_NOT_ENABLED, spec.reason or "This broker is not enabled, so the account cannot be connected."
    if session_store_available is False:
        return STATUS_UNKNOWN, "Jettone could not check this account's session because the session store is unreachable. Try again shortly."
    if session_valid:
        identity = identity_assurance(spec.id)
        if identity["level"] == IDENTITY_NOT_AVAILABLE:
            return CONNECTED, f"Signed in. {identity['note']}"
        return CONNECTED, "Signed in."
    if credentials_configured is False:
        return NEEDS_CREDENTIALS, "No credentials are stored for this account. Edit the account and add them."
    return DISCONNECTED, _RECONNECT_HINT.get(spec.id, "Sign in to connect this account.")
