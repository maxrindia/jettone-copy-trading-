"""Offline Groww test doubles. Nothing here opens a network connection.

The request/response shapes below follow Groww's official documentation (see
app/services/groww/contract.py). The FAILURE body is NOT documented, so tests
use a minimal {"status": "FAILURE"} and assert only that Jettone fails closed.
"""
import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from app.services.groww.client import GrowwClient
from app.services.groww.instruments import GrowwInstrumentStore

FIXED_NOW = datetime(2026, 10, 16, 4, 0, tzinfo=timezone.utc)      # 09:30 IST
ALICE_STYLE_TOKEN = "99999"                                         # the MASTER broker's id; must never reach Groww

CSV_HEADER = ("exchange,exchange_token,trading_symbol,groww_symbol,name,instrument_type,segment,series,isin,"
              "underlying_symbol,underlying_exchange_token,expiry_date,strike_price,lot_size,tick_size,"
              "freeze_quantity,is_reserved,buy_allowed,sell_allowed")
CSV_ROWS = [
    "NSE,3045,SBIN,NSE-SBIN,State Bank of India,EQ,CASH,EQ,INE062A01020,,,,,1,0.05,100000,False,True,True",
    "NSE,11536,TCS,NSE-TCS,Tata Consultancy,EQ,CASH,EQ,INE467B01029,,,,,1,0.05,50000,False,True,True",
    "BSE,500112,SBIN,BSE-SBIN,State Bank of India,EQ,CASH,A,INE062A01020,,,,,1,0.05,100000,False,True,True",
    "NSE,9999,NOSELL,NSE-NOSELL,No Sell Ltd,EQ,CASH,EQ,INE000X00001,,,,,1,0.05,0,False,True,False",
    "NSE,8888,SMALLFRZ,NSE-SMALLFRZ,Small Freeze,EQ,CASH,EQ,INE000X00002,,,,,1,0.05,100,False,True,True",
    "NSE,7777,UNREADABLE,NSE-UNREADABLE,Unreadable Flags,EQ,CASH,EQ,INE000X00003,,,,,1,0.05,0,False,,",
    "NSE,26000,NIFTY26OCT23500CE,NSE-NIFTY,Nifty,CE,FNO,,,NIFTY,,2026-10-27,23500,75,0.05,1800,False,True,True",
    "NSE,5555,NIFTYBEES,NSE-NIFTYBEES,Nifty Bees,ETF,CASH,EQ,INE000X00004,,,,,1,0.05,0,False,True,True",
]
SBIN_ISIN = "INE062A01020"


def make_store() -> GrowwInstrumentStore:
    s = GrowwInstrumentStore()
    s.load_csv_text("\n".join([CSV_HEADER] + CSV_ROWS))
    return s


TOKEN_OK = (200, {"token": "TOK-1", "tokenRefId": "ref", "sessionName": "jettone", "expiry": "2026-10-17T06:00:00",
                  "isActive": True})
EXPECTED_TTL = 73800     # earlier reading of the naive expiry 06:00 (IST = 00:30Z): 20.5 h from 04:00Z


def _resp(pair) -> httpx.Response:
    status, body = pair
    if isinstance(body, (bytes, str)):          # a deliberately malformed (non-JSON) body
        return httpx.Response(status, content=body)
    return httpx.Response(status, json=body)


def ok(payload: Dict[str, Any]) -> tuple:
    return 200, {"status": "SUCCESS", "payload": payload}


def position_row(symbol="SBIN", exchange="NSE", product="MIS", qty=10, isin=SBIN_ISIN, segment="CASH", price=800):
    return {"trading_symbol": symbol, "segment": segment, "exchange": exchange, "product": product,
            "quantity": qty, "symbol_isin": isin, "net_price": price, "credit_quantity": max(qty, 0),
            "debit_quantity": 0, "carry_forward_credit_quantity": 0, "carry_forward_debit_quantity": 0,
            "net_carry_forward_quantity": 0, "net_carry_forward_price": 0, "realised_pnl": 0}


def order_row(oid="GRW1", status="EXECUTED", qty=10, filled=10, side="BUY", otype="LIMIT", price=800, symbol="SBIN",
              product="MIS", ref="JTabc123456789012345", trigger=None):
    return {"groww_order_id": oid, "trading_symbol": symbol, "order_status": status, "remark": "",
            "quantity": qty, "price": price,
            "trigger_price": trigger if trigger is not None else (790 if otype in ("SL", "SL_M") else 0),
            "filled_quantity": filled,
            "remaining_quantity": qty - filled, "average_fill_price": price if filled else 0,
            "deliverable_quantity": 0, "amo_status": "NA", "validity": "DAY", "exchange": "NSE",
            "order_type": otype, "transaction_type": side, "segment": "CASH", "product": product,
            "created_at": "2026-10-16T09:31:00", "exchange_time": "2026-10-16T09:31:01",
            "trade_date": "2026-10-16", "order_reference_id": ref}


class FakeGroww:
    """httpx.MockTransport handler standing in for api.groww.in."""

    def __init__(self):
        self.requests: List[httpx.Request] = []
        self.token_response = TOKEN_OK
        self.create_response = ok({"groww_order_id": "GRW-NEW-1", "order_status": "OPEN",
                                   "order_reference_id": "x", "remark": "Order placed successfully"})
        self.create_exception: Optional[Exception] = None
        self.create_hook = None                       # called with the request just before the create is answered
        self.modify_response = ok({"groww_order_id": "GRW1", "order_status": "MODIFICATION_REQUESTED"})
        self.cancel_response = ok({"groww_order_id": "GRW1", "order_status": "CANCELLATION_REQUESTED"})
        self.detail_response = ok(order_row(status="OPEN", filled=0))
        self.reference_response: tuple = (404, {"status": "FAILURE"})
        self.order_list_response = ok({"order_list": []})
        self.positions_response = ok({"positions": []})
        self.margin_response = ok({"clear_cash": 1000000.0, "net_margin_used": 0.0, "collateral_available": 0.0})
        self.session_rejected = False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)       # yield once, like a real network call
        self.requests.append(request)
        assert request.url.host == "api.groww.in", request.url      # never another broker
        path, method = request.url.path, request.method
        if path == "/v1/token/api/access":
            s, b = self.token_response
            return httpx.Response(s, json=b)
        if self.session_rejected and path != "/v1/token/api/access":
            return httpx.Response(401, json={"status": "FAILURE"})
        if path == "/v1/order/create":
            if self.create_hook:
                self.create_hook(request)
            if self.create_exception:
                raise self.create_exception
            return _resp(self.create_response)
        if path == "/v1/order/modify":
            return _resp(self.modify_response)
        if path == "/v1/order/cancel":
            return _resp(self.cancel_response)
        if path.startswith("/v1/order/detail/"):
            return _resp(self.detail_response)
        if path.startswith("/v1/order/status/reference/"):
            return _resp(self.reference_response)
        if path == "/v1/order/list":
            return _resp(self.order_list_response)
        if path == "/v1/positions/user":
            return _resp(self.positions_response)
        if path == "/v1/margins/detail/user":
            return _resp(self.margin_response)
        return httpx.Response(404, json={})

    def calls(self, path_part: str) -> List[httpx.Request]:
        return [r for r in self.requests if path_part in r.url.path]

    def bodies(self, path_part: str) -> List[Dict[str, Any]]:
        return [json.loads(r.content) for r in self.calls(path_part)]


class Clock:
    """Controllable monotonic clock + sleep for the limiter and login spacing."""

    def __init__(self):
        self.t = 1000.0
        self.slept: List[float] = []

    def monotonic(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def make_client(redis, fake: FakeGroww, *, static_ip: Optional[str] = "151.245.182.20", valid_until=None,
                store: Optional[GrowwInstrumentStore] = None, account_id="acc-groww",
                clock: Optional[Clock] = None, contract=None) -> GrowwClient:
    kwargs = {}
    if clock is not None:
        kwargs.update(sleep=clock.sleep, monotonic=clock.monotonic)
    if contract is not None:
        kwargs["contract"] = contract
    return GrowwClient(
        account_id=account_id, label="groww-user", api_key="GROWW-API-KEY", api_secret="GROWW-API-SECRET",
        redis=redis, static_ip=static_ip, static_ip_valid_until=valid_until,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake)),
        clock=lambda: FIXED_NOW, instruments=store if store is not None else make_store(), **kwargs,
    )
