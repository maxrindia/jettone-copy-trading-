"""
Broker Client Factory
=====================
Builds the broker client for an account's broker id via the registry.
An unsupported or unknown broker raises UnsupportedBrokerError - there is no
fallback to another broker's client.
"""

from typing import Any, Optional

from app.services.alice_blue.client import AliceBlueClient
from app.services.brokers.registry import require_engine_broker


def create_broker_client(
    broker_id: Optional[str],
    account_id: str,
    user_id: str,
    api_secret: str,
    redis: Any,
    http_client: Optional[Any] = None,
    proxy_url: Optional[str] = None,
) -> AliceBlueClient:
    spec = require_engine_broker(broker_id)
    if spec.id == "alicebluenew":
        return AliceBlueClient(
            account_id=account_id,
            user_id=user_id,
            api_secret=api_secret,
            redis=redis,
            http_client=http_client,
            proxy_url=proxy_url,
        )
    # require_engine_broker only passes brokers with an implemented client;
    # reaching here means the registry and this factory are out of sync.
    raise AssertionError(f"registry allows broker '{spec.id}' but no client is wired for it")
