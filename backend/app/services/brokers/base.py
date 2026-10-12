"""
Base Broker Client Interface (Multi-Broker Abstract Contract)
==============================================================
Defines the standard asynchronous interface that every broker client adapter
(Alice Blue, Flattrade, Zerodha, Angel One, Dhan, Fyers, Upstox, Shoonya, etc.)
must implement for unified order execution, portfolio management, and copy-trading.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from app.services.alice_blue.types import (
    FundsData,
    OrderRecord,
    OrderRequest,
    OrderResult,
    Position,
)


class BaseBrokerClient(ABC):
    """Abstract Base Class for all broker client implementations."""

    def __init__(
        self,
        account_id: str,
        user_id: str,
        api_secret: str,
        redis: Any,
        broker_name: str = "generic",
        http_client: Optional[Any] = None,
        proxy_url: Optional[str] = None,
    ):
        self.account_id = str(account_id)
        self.user_id = str(user_id).strip()
        self.api_secret = api_secret
        self.redis = redis
        self.broker_name = broker_name
        self.http_client = http_client
        self.proxy_url = proxy_url

    @abstractmethod
    async def authenticate(self) -> bool:
        """Authenticates or refreshes the broker API session."""
        pass

    @abstractmethod
    async def place_order(self, order: OrderRequest) -> OrderResult:
        """Places an order with the broker and returns a normalized OrderResult."""
        pass

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> bool:
        """Cancels an open order on the broker."""
        pass

    @abstractmethod
    async def get_order_book(self) -> List[OrderRecord]:
        """Fetches the day's order book from the broker."""
        pass

    @abstractmethod
    async def get_positions(self) -> List[Position]:
        """Fetches current open positions from the broker."""
        pass

    @abstractmethod
    async def get_funds(self) -> Optional[FundsData]:
        """Fetches account margin / funds balance."""
        pass
