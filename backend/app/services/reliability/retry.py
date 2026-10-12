import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Set

from app.services.alice_blue.types import OrderResult, OrderResultCategory

logger = logging.getLogger(__name__)

# TIMEOUT is deliberately NOT retryable: a timed-out request may still have
# executed at the broker, and re-sending it can double the position. TIMEOUT
# rows are resolved by the stale-order sweeper against the broker order book.
RETRYABLE_CATEGORIES: Set[OrderResultCategory] = {
    OrderResultCategory.NETWORK_ERROR,
}


class RetryPolicy:
    def __init__(self, max_attempts: int = 3, base_delay_sec: float = 1.0, backoff_factor: float = 2.0):
        self.max_attempts = max_attempts
        self.base_delay_sec = base_delay_sec
        self.backoff_factor = backoff_factor

    def _failure(self, category: OrderResultCategory, message: str) -> OrderResult:
        return OrderResult(
            category=category,
            client_order_id=None,
            broker_order_id=None,
            message=message,
            raw_response={},
            timestamp=datetime.now(timezone.utc),
        )

    async def execute(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> OrderResult:
        attempt = 0
        current_delay = self.base_delay_sec

        while attempt < self.max_attempts:
            attempt += 1
            try:
                result: OrderResult = await func(*args, **kwargs)

                if result.category not in RETRYABLE_CATEGORIES:
                    return result

                logger.warning(
                    f"Retryable failure on attempt {attempt}/{self.max_attempts}: {result.category.value} - {result.message}"
                )

                if attempt >= self.max_attempts:
                    logger.error(f"Exhausted all {self.max_attempts} retry attempts.")
                    return result

                await asyncio.sleep(current_delay)
                current_delay *= self.backoff_factor

            except Exception as exc:
                logger.error(f"Exception during order execution on attempt {attempt}/{self.max_attempts}: {exc}", exc_info=True)
                if attempt >= self.max_attempts:
                    return self._failure(
                        OrderResultCategory.UNKNOWN,
                        f"Exception after retries: {str(exc)}",
                    )
                await asyncio.sleep(current_delay)
                current_delay *= self.backoff_factor

        return self._failure(OrderResultCategory.UNKNOWN, "Retry execution failed unexpectedly")
