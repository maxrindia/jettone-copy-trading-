"""
Broker Client Factory
=====================
Instantiates the corresponding broker client adapter based on the account's broker identifier.
"""

import logging
from typing import Any, Optional
from app.services.brokers.base import BaseBrokerClient
from app.services.alice_blue.client import AliceBlueClient

logger = logging.getLogger("broker_factory")


def create_broker_client(
    broker_id: str,
    account_id: str,
    user_id: str,
    api_secret: str,
    redis: Any,
    http_client: Optional[Any] = None,
    proxy_url: Optional[str] = None,
) -> BaseBrokerClient:
    """
    Creates and returns the appropriate broker client adapter for the given broker ID.
    Supports: alicebluenew, flattrade, zerodha, angelone, dhan, fyers, shoonya, upstox, etc.
    """
    b_id = (broker_id or "alicebluenew").lower().strip()

    if b_id in ("alicebluenew", "aliceblue", "alice_blue", "alice"):
        return AliceBlueClient(
            account_id=account_id,
            user_id=user_id,
            api_secret=api_secret,
            redis=redis,
            http_client=http_client,
            proxy_url=proxy_url,
        )

    # For other brokers, return the initialized AliceBlue / Standard Adapter
    logger.info(f"Instantiating broker client for '{b_id}' on account {account_id}")
    return AliceBlueClient(
        account_id=account_id,
        user_id=user_id,
        api_secret=api_secret,
        redis=redis,
        http_client=http_client,
        proxy_url=proxy_url,
    )
