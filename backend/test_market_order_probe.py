"""
Standalone Diagnostic Script: Probe Alice Blue A3 MARKET Order Placement
========================================================================

Purpose:
  Sends an isolated MARKET order for RELIANCE (instrumentId: 2885, qty: 1, BUY, INTRADAY)
  directly to Alice Blue A3 to inspect the exact broker response and headers.

Usage (Safe Manual Execution on Server or Local environment with valid session in Redis):
  python test_market_order_probe.py

Note:
  This script does NOT execute automatically. It requires an active Redis session token.
"""

import asyncio
import json
import logging
import sys
from decimal import Decimal

import httpx
import redis.asyncio as aioredis

from app.core.config import settings
from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.types import OrderRequest, OrderSide, OrderType, ProductType

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("market_order_probe")

A3_BASE_URL = "https://a3.aliceblueonline.com"


async def probe_market_order(account_id: str, user_id: str, api_secret: str, redis_url: str):
    logger.info("Initializing Redis connection...")
    r = aioredis.from_url(redis_url)

    # 1. Check if session token exists in Redis
    session_token = await r.get(f"alice:session:{account_id}")
    if not session_token:
        logger.error(f"No active session token found in Redis at 'alice:session:{account_id}'.")
        logger.error("Please ensure the account is logged in via OAuth before running this test.")
        await r.aclose()
        return

    token_str = session_token.decode("utf-8") if isinstance(session_token, bytes) else str(session_token)
    logger.info(f"Active session found. Session Token Preview: {token_str[:8]}...{token_str[-4:]}")

    # 2. Build AliceBlueClient
    client = AliceBlueClient(
        account_id=account_id,
        user_id=user_id,
        api_secret=api_secret,
        redis=r,
    )

    req = OrderRequest(
        symbol="RELIANCE",
        exchange="NSE",
        side=OrderSide.BUY,
        quantity=1,
        product_type=ProductType.MIS,
        order_type=OrderType.MARKET,
        price=Decimal("0"),
        client_order_id=f"PROBE_MKT_{int(asyncio.get_event_loop().time())}",
        instrument_token="2885",
    )

    logger.info("Submitting isolated MARKET order probe to Alice Blue A3...")
    result = await client.place_order(req)

    logger.info("=== PROBE RESULT ===")
    logger.info(f"Category: {result.category.value}")
    logger.info(f"Broker Order ID: {result.broker_order_id}")
    logger.info(f"Message: {result.message}")
    logger.info(f"Raw Response: {json.dumps(result.raw_response, indent=2)}")

    await r.aclose()


if __name__ == "__main__":
    print("Market order probe script ready.")
    print("To run, call probe_market_order with your account details in an interactive session.")
