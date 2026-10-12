"""
Automated Diagnostic Probe: Test Alice Blue A3 Payload Variations for EC965 Root Cause
======================================================================================
"""

import asyncio
import json
import logging
from sqlalchemy import select
import redis.asyncio as aioredis
import httpx

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.accounts import Account, AccountType

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("probe_variations")

A3_BASE_URL = "https://a3.aliceblueonline.com"


async def run_probes():
    logger.info("=== STEP 1: Finding Active Master Account and Session in DB & Redis ===")
    r = aioredis.from_url(settings.REDIS_URL)

    master_acc = None
    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(Account).where(Account.account_type == AccountType.MASTER, Account.deleted_at.is_(None))
        )
        master_acc = res.scalars().first()

    if not master_acc:
        logger.error("No MASTER account found in DB.")
        await r.aclose()
        return

    logger.info(f"Master Account: {master_acc.display_name} ({master_acc.alice_user_id}), ID: {master_acc.id}")

    session_token = await r.get(f"alice:session:{master_acc.id}")
    if not session_token:
        # Check by user_id
        session_token = await r.get(f"alice:session:{master_acc.alice_user_id}")

    if not session_token:
        logger.error(f"No active session token found in Redis for master account {master_acc.id}")
        await r.aclose()
        return

    token_str = session_token.decode("utf-8") if isinstance(session_token, bytes) else str(session_token)
    logger.info(f"Using Active Session JWT: {token_str[:10]}...{token_str[-6:]}")

    headers = {
        "Authorization": f"Bearer {token_str}",
        "Content-Type": "application/json",
    }

    variations = [
        {
            "name": "Variation 1: product=INTRADAY, orderComplexity=regular, orderType=MARKET, priceType=MKT",
            "payload": [
                {
                    "exchange": "NSE",
                    "token": "2885",
                    "instrumentId": "2885",
                    "instrumentToken": "2885",
                    "tradingSymbol": "RELIANCE-EQ",
                    "qty": "1",
                    "quantity": "1",
                    "orderQuantity": "1",
                    "price": "0",
                    "product": "INTRADAY",
                    "productType": "INTRADAY",
                    "priceType": "MKT",
                    "orderType": "MARKET",
                    "transactionType": "BUY",
                    "transType": "BUY",
                    "orderComplexity": "regular",
                    "order_complexity": "regular",
                    "ret": "DAY",
                    "validity": "DAY",
                    "triggerPrice": "0",
                    "disclosedQty": "0",
                    "mktProtection": "",
                }
            ],
        },
        {
            "name": "Variation 2: product=MIS, orderComplexity=regular, orderType=MARKET, priceType=MKT",
            "payload": [
                {
                    "exchange": "NSE",
                    "token": "2885",
                    "instrumentId": "2885",
                    "instrumentToken": "2885",
                    "tradingSymbol": "RELIANCE-EQ",
                    "qty": "1",
                    "quantity": "1",
                    "orderQuantity": "1",
                    "price": "0",
                    "product": "MIS",
                    "productType": "MIS",
                    "priceType": "MKT",
                    "orderType": "MARKET",
                    "transactionType": "BUY",
                    "transType": "BUY",
                    "orderComplexity": "regular",
                    "order_complexity": "regular",
                    "ret": "DAY",
                    "validity": "DAY",
                    "triggerPrice": "0",
                    "disclosedQty": "0",
                    "mktProtection": "",
                }
            ],
        },
        {
            "name": "Variation 3: product=CNC, orderComplexity=regular, orderType=MARKET, priceType=MKT",
            "payload": [
                {
                    "exchange": "NSE",
                    "token": "2885",
                    "instrumentId": "2885",
                    "instrumentToken": "2885",
                    "tradingSymbol": "RELIANCE-EQ",
                    "qty": "1",
                    "quantity": "1",
                    "orderQuantity": "1",
                    "price": "0",
                    "product": "CNC",
                    "productType": "CNC",
                    "priceType": "MKT",
                    "orderType": "MARKET",
                    "transactionType": "BUY",
                    "transType": "BUY",
                    "orderComplexity": "regular",
                    "order_complexity": "regular",
                    "ret": "DAY",
                    "validity": "DAY",
                    "triggerPrice": "0",
                    "disclosedQty": "0",
                    "mktProtection": "",
                }
            ],
        },
        {
            "name": "Variation 4: YESBANK (Token 11915), product=MIS, orderComplexity=regular, orderType=MARKET",
            "payload": [
                {
                    "exchange": "NSE",
                    "token": "11915",
                    "instrumentId": "11915",
                    "instrumentToken": "11915",
                    "tradingSymbol": "YESBANK-EQ",
                    "qty": "1",
                    "quantity": "1",
                    "orderQuantity": "1",
                    "price": "0",
                    "product": "MIS",
                    "productType": "MIS",
                    "priceType": "MKT",
                    "orderType": "MARKET",
                    "transactionType": "BUY",
                    "transType": "BUY",
                    "orderComplexity": "regular",
                    "order_complexity": "regular",
                    "ret": "DAY",
                    "validity": "DAY",
                    "triggerPrice": "0",
                    "disclosedQty": "0",
                    "mktProtection": "",
                }
            ],
        },
        {
            "name": "Variation 5: RELIANCE, LIMIT Order (Price=2950), orderType=LIMIT, priceType=L",
            "payload": [
                {
                    "exchange": "NSE",
                    "token": "2885",
                    "instrumentId": "2885",
                    "instrumentToken": "2885",
                    "tradingSymbol": "RELIANCE-EQ",
                    "qty": "1",
                    "quantity": "1",
                    "orderQuantity": "1",
                    "price": "2950.00",
                    "product": "INTRADAY",
                    "productType": "INTRADAY",
                    "priceType": "L",
                    "orderType": "LIMIT",
                    "transactionType": "BUY",
                    "transType": "BUY",
                    "orderComplexity": "regular",
                    "order_complexity": "regular",
                    "ret": "DAY",
                    "validity": "DAY",
                    "triggerPrice": "0",
                    "disclosedQty": "0",
                    "mktProtection": "",
                }
            ],
        },
    ]

    async with httpx.AsyncClient(timeout=10.0) as client:
        for v in variations:
            logger.info(f"\n==========================================")
            logger.info(f"TESTING: {v['name']}")
            logger.info(f"Payload: {json.dumps(v['payload'])}")
            try:
                resp = await client.post(
                    f"{A3_BASE_URL}/open-api/od/v1/orders/placeorder",
                    json=v["payload"],
                    headers=headers,
                )
                logger.info(f"HTTP Status: {resp.status_code}")
                logger.info(f"Response Body: {resp.text}")
            except Exception as e:
                logger.error(f"Request failed with error: {e}")

    await r.aclose()


if __name__ == "__main__":
    asyncio.run(run_probes())
