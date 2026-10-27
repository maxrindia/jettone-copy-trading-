"""Offline Zerodha (Kite Connect v3) test doubles. Nothing here opens a network connection.

Request/response shapes follow Zerodha's official documentation (see app/services/zerodha/contract.py). Error bodies use the documented
{"status": "error", "message", "error_type"} envelope. Nothing here proves compatibility with the real service.
"""
import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from app.core.crypto import encrypt_secret
from app.services.zerodha.client import ZerodhaClient
from app.services.zerodha.instruments import ZerodhaInstrumentStore

FIXED_NOW = datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc)       # 09:30 IST
ALICE_STYLE_TOKEN = "99999"                                          # the MASTER broker's id; must never reach Zerodha

CSV_HEADER = "instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange"
CSV_ROWS = [
    "779521,3045,SBIN,STATE BANK OF INDIA,0,,0,0.05,1,EQ,NSE,NSE",
    "2953217,11536,TCS,TATA CONSULTANCY,0,,0,0.05,1,EQ,NSE,NSE",
    "128028676,500112,SBIN,STATE BANK OF INDIA,0,,0,0.05,1,EQ,BSE,BSE",
    "13568258,52999,NIFTY26NOV23500CE,NIFTY,0,2026-11-26,23500,0.05,75,CE,NFO-OPT,NFO",
    "13568770,53001,NIFTY26NOV23500PE,NIFTY,0,2026-11-26,23500,0.05,75,PE,NFO-OPT,NFO",
    "13569026,53002,NIFTY26NOVFUT,NIFTY,0,2026-11-26,0,0.05,75,FUT,NFO-FUT,NFO",
    "13569027,53003,NIFTY26DECFUT,NIFTY,0,2026-12-31,0,0.05,75,FUT,NFO-FUT,NFO",
    "1,1,GOLDM,GOLD,0,2026-12-04,0,1,10,FUT,MCX-FUT,MCX",
]


def make_store() -> ZerodhaInstrumentStore:
    s = ZerodhaInstrumentStore()
    s.load_csv_text("\n".join([CSV_HEADER] + CSV_ROWS))
    return s


def order_row(oid="240101000000001", status="OPEN", qty=10, filled=0, side="BUY", otype="LIMIT", price=800, symbol="SBIN",
              product="CNC", tag="JTabc12345678901234", exchange="NSE", variety="regular", validity="DAY", trigger=None,
              average=None):
    row = {
        "order_id": oid, "exchange_order_id": "1300000000", "parent_order_id": None, "status": status, "status_message": None,
        "order_timestamp": "2026-10-27 09:31:00", "exchange_timestamp": "2026-10-27 09:31:01", "variety": variety,
        "exchange": exchange, "tradingsymbol": symbol, "instrument_token": 779521, "order_type": otype, "transaction_type": side,
        "validity": validity, "product": product, "quantity": qty, "disclosed_quantity": 0, "price": price,
        "trigger_price": trigger if trigger is not None else (790 if otype in ("SL", "SL-M") else 0),
        "filled_quantity": filled, "pending_quantity": qty - filled, "cancelled_quantity": 0,
        "average_price": average if average is not None else (price if status == "COMPLETE" else 0),
        "placed_by": "AB1234", "tag": tag, "meta": {},
    }
    return row


def position_row(symbol="SBIN", exchange="NSE", product="CNC", qty=10, price=800):
    return {"tradingsymbol": symbol, "exchange": exchange, "instrument_token": 779521, "product": product, "quantity": qty,
            "overnight_quantity": 0, "multiplier": 1, "average_price": price, "close_price": price, "last_price": price,
            "value": 0, "pnl": 0, "m2m": 0, "unrealised": 0, "realised": 0, "buy_quantity": max(qty, 0), "sell_quantity": 0}


def ok(data: Any) -> tuple:
    return 200, {"status": "success", "data": data}


def err(status: int, error_type: str, message: str = "x") -> tuple:
    return status, {"status": "error", "message": message, "error_type": error_type}


def _resp(pair) -> httpx.Response:
    status, body = pair
    if isinstance(body, (bytes, str)):
        return httpx.Response(status, content=body)
    return httpx.Response(status, json=body)


def seed_session(redis, account_id="acc-zerodha", token="ACCESS-1"):
    async def _do():
        await redis.set(f"zerodha:session:{account_id}", encrypt_secret(json.dumps({"token": token, "at": FIXED_NOW.isoformat()})),
                        ex=3600)
    return _do()


class FakeKite:
    """httpx.MockTransport handler standing in for api.kite.trade."""

    def __init__(self):
        self.requests: List[httpx.Request] = []
        self.session_response = ok({"access_token": "ACCESS-1", "user_id": "AB1234"})
        self.place_response = ok({"order_id": "240101000000099"})
        self.place_exception: Optional[Exception] = None
        self.modify_response = ok({"order_id": "240101000000001"})
        self.cancel_response = ok({"order_id": "240101000000001"})
        self.history_response = ok([order_row(status="OPEN")])
        self.orders_response = ok([])
        self.positions_response = ok({"net": [], "day": []})
        self.margins_response = ok({"enabled": True, "net": 100000.0,
                                    "available": {"live_balance": 90000.5, "collateral": 5000, "cash": 90000.5},
                                    "utilised": {"debits": 10000.0}})
        self.session_rejected = False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)
        self.requests.append(request)
        assert request.url.host == "api.kite.trade", request.url        # never another broker
        path, method = request.url.path, request.method
        if path == "/session/token" and method == "POST":
            return _resp(self.session_response)
        if path == "/session/token" and method == "DELETE":
            return _resp(ok(True))
        if self.session_rejected:
            return _resp(err(403, "TokenException", "Incorrect api_key or access_token."))
        if path == "/orders/regular" and method == "POST":
            if self.place_exception:
                raise self.place_exception
            return _resp(self.place_response)
        if path.startswith("/orders/regular/") and method == "PUT":
            return _resp(self.modify_response)
        if path.startswith("/orders/regular/") and method == "DELETE":
            return _resp(self.cancel_response)
        if path == "/orders" and method == "GET":
            return _resp(self.orders_response)
        if path.startswith("/orders/") and method == "GET":
            return _resp(self.history_response)
        if path == "/portfolio/positions":
            return _resp(self.positions_response)
        if path == "/user/margins/equity":
            return _resp(self.margins_response)
        return httpx.Response(404, json={"status": "error", "message": "no", "error_type": "GeneralException"})

    def calls(self, path_part: str, method: Optional[str] = None) -> List[httpx.Request]:
        return [r for r in self.requests if path_part in r.url.path and (method is None or r.method == method)]

    @staticmethod
    def form(request: httpx.Request) -> Dict[str, str]:
        from urllib.parse import parse_qsl
        return dict(parse_qsl(request.content.decode()))


def make_client(redis, fake: FakeKite, *, account_id="acc-zerodha", store: Optional[ZerodhaInstrumentStore] = None,
                contract=None) -> ZerodhaClient:
    kwargs = {}
    if contract is not None:
        kwargs["contract"] = contract
    return ZerodhaClient(
        account_id=account_id, label="AB1234", api_key="KITE-KEY", api_secret="KITE-SECRET", redis=redis,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake)), clock=lambda: FIXED_NOW,
        instruments=store if store is not None else make_store(), **kwargs,
    )
