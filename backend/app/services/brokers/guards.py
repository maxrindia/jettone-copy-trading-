"""HTTP-layer guards that turn registry refusals into 409 responses."""

from typing import Any, Optional

from fastapi import HTTPException, status

from app.services.brokers.registry import (
    BrokerSupport,
    UnsupportedBrokerError,
    get_broker_spec,
    require_alice_blue,
)


def ensure_alice_blue_account(account: Any) -> None:
    """409 UNSUPPORTED_BROKER unless the account's broker is Alice Blue.
    Call before any AliceBlueClient is built for the account."""
    try:
        require_alice_blue(getattr(account, "broker", None))
    except UnsupportedBrokerError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def ensure_broker_selectable(broker_id: Optional[str]) -> None:
    """400 for a broker id an account may not be created/updated with: an
    unknown id, or one that is listed but not supported for trading."""
    spec = get_broker_spec(broker_id)
    if spec is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown broker '{broker_id}'.")
    if spec.support == BrokerSupport.NOT_SUPPORTED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Broker '{broker_id}' is not supported yet. {spec.reason}".strip(),
        )
