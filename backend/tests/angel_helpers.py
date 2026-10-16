"""Offline Angel One test doubles.

SYNTHETIC_CONTRACT uses made-up `syn_*` keys and statuses on purpose: Angel One's
real order-status / position / funds fields are NOT verified, so no test may
imply they are. These tests prove Jettone's logic (position safety, mapping,
fail-closed behaviour) given SOME contract; they say nothing about Angel One's
actual wire format. Nothing here opens a network connection.
"""
import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from app.services.angel_one.client import AngelOneClient
from app.services.angel_one.contract import AngelContract, PositionKeys, RmsKeys
from app.services.angel_one.instruments import AngelInstrumentStore

SYNTHETIC_CONTRACT = AngelContract(
    status_map={"syn_open": "OPEN", "syn_part": "PARTIAL", "syn_done": "COMPLETE",
                "syn_rej": "REJECTED", "syn_can": "CANCELLED"},
    positions=PositionKeys(symbol="syn_sym", token="syn_tok", exchange="syn_exch",
                           product="syn_prod", net_qty="syn_net", avg_price="syn_avg"),
    rms=RmsKeys(available_margin="syn_avail", used_margin="syn_used", collateral="syn_coll"),
)

SBIN_TOKEN = "3045"
ALICE_STYLE_TOKEN = "99999"   # a token from the MASTER's broker; must never reach Angel One

LOGIN_OK = (200, {"status": True, "message": "SUCCESS",
                  "data": {"jwtToken": "JWT1", "refreshToken": "R1", "feedToken": "F1"}})
FIXED_NOW = datetime(2026, 10, 16, 4, 0, tzinfo=timezone.utc)


def make_store() -> AngelInstrumentStore:
    s = AngelInstrumentStore()
    s.load_records([
        {"token": SBIN_TOKEN, "symbol": "SBIN-EQ", "name": "SBIN", "exch_seg": "NSE"},
        {"token": "11536", "symbol": "TCS-EQ", "name": "TCS", "exch_seg": "NSE"},
        {"token": "500112", "symbol": "SBIN", "name": "SBIN", "exch_seg": "BSE"},
        {"token": "26000", "symbol": "NIFTY26OCT23500CE", "name": "NIFTY", "exch_seg": "NFO"},
    ])
    return s


def position_row(symbol="SBIN-EQ", token=SBIN_TOKEN, exchange="NSE", product="INTRADAY", net="10", avg="800"):
    return {"syn_sym": symbol, "syn_tok": token, "syn_exch": exchange, "syn_prod": product,
            "syn_net": net, "syn_avg": avg}


class FakeAngel:
    """httpx.MockTransport handler standing in for apiconnect.angelone.in."""

    def __init__(self, positions: Optional[List[Dict[str, Any]]] = None, rms: Optional[Dict[str, Any]] = None):
        self.requests: List[httpx.Request] = []
        self.positions = positions if positions is not None else []
        self.positions_response: Optional[Any] = None      # override the whole position response
        self.rms = rms if rms is not None else {"syn_avail": "1000000", "syn_used": "0", "syn_coll": "0"}
        self.place_response = (200, {"status": True, "message": "SUCCESS", "data": {"orderid": "ANG-1"}})
        self.routes: Dict[str, Any] = {"loginByPassword": LOGIN_OK}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "apiconnect.angelone.in", request.url  # never another broker
        name = request.url.path.rsplit("/", 1)[-1]
        if name in self.routes:
            r = self.routes[name]
            if isinstance(r, Exception):
                raise r
            return httpx.Response(r[0], json=r[1])
        if name == "placeOrder":
            return httpx.Response(self.place_response[0], json=self.place_response[1])
        if name == "getPosition":
            if self.positions_response is not None:
                s, b = self.positions_response
                return httpx.Response(s, json=b)
            return httpx.Response(200, json={"status": True, "data": self.positions})
        if name == "getRMS":
            return httpx.Response(200, json={"status": True, "data": self.rms})
        return httpx.Response(404, json={})

    def calls(self, name: str) -> List[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith(name)]

    def order_bodies(self) -> List[Dict[str, Any]]:
        return [json.loads(r.content) for r in self.calls("placeOrder")]


def make_client(redis, fake: FakeAngel, *, contract=SYNTHETIC_CONTRACT, static_ip: Optional[str] = "151.245.182.10",
                valid_until=None, store: Optional[AngelInstrumentStore] = None, account_id="acc-angel") -> AngelOneClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0)      # yield once, like a real network call, so concurrent callers overlap
        return fake(request)

    return AngelOneClient(
        account_id=account_id, client_code="A123", api_key="KEY", pin="1234",
        totp_secret="JBSWY3DPEHPK3PXP", redis=redis, static_ip=static_ip, static_ip_valid_until=valid_until,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        clock=lambda: FIXED_NOW, contract=contract,
        instruments=store if store is not None else make_store(),
    )
