"""
Angel One SmartAPI client
=========================
STATUS: disabled by default (ANGEL_ONE_ENABLED=false). With the flag on it can
log in, but it only trades once ANGEL_CONTRACT (contract.py) is verified from
a supervised capture; until then every read that position safety, margin or
lifecycle tracking depends on refuses (fail closed).

Verified against Angel One's official SDKs (angel-one/smartapi-python
smartConnect.py and angel-one/smartapi-java Constants.java / models/Order.java):
  - base URL, route paths, request headers
  - login body {clientcode, password, totp} -> data.jwtToken/refreshToken/feedToken
  - place/modify/cancel body fields and data.orderid in the response
  - varieties NORMAL/STOPLOSS/ROBO/AMO, order types MARKET/LIMIT/
    STOPLOSS_LIMIT/STOPLOSS_MARKET, product types DELIVERY/INTRADAY/MARGIN/
    CARRYFORWARD/BO, durations DAY/IOC
  - order-book row keys (orderid, tradingsymbol, symboltoken, exchange,
    transactiontype, ordertype, producttype, price, triggerprice, quantity,
    filledshares, unfilledshares, averageprice, status, text, updatetime)
  - static IP required for order APIs; sessions end at midnight

NOT verified (held in the contract, which refuses until filled in):
  - order STATUS VALUES, position-book keys, RMS (funds) keys
  - the scrip master location (instruments.py takes it from configuration)
  - error-code catalogue (any status:false is a rejection; 401/403 or a
    TokenException clears the session)
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import struct
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from app.core.crypto import decrypt_secret, encrypt_secret
from app.services.alice_blue.types import (
    FundsData,
    OrderRecord,
    OrderRequest,
    OrderResult,
    OrderResultCategory,
    OrderSide,
    OrderType,
    Position,
    ProductType,
)
from app.services.angel_one.contract import ANGEL_CONTRACT, AngelContract, contract_is_usable
from app.services.angel_one.instruments import AngelInstrumentStore, store as default_store

logger = logging.getLogger("angel_one_client")

BASE_URL = "https://apiconnect.angelone.in"
ROUTES = {
    "login": "/rest/auth/angelbroking/user/v1/loginByPassword",
    "logout": "/rest/secure/angelbroking/user/v1/logout",
    "profile": "/rest/secure/angelbroking/user/v1/getProfile",
    "place": "/rest/secure/angelbroking/order/v1/placeOrder",
    "modify": "/rest/secure/angelbroking/order/v1/modifyOrder",
    "cancel": "/rest/secure/angelbroking/order/v1/cancelOrder",
    "order_book": "/rest/secure/angelbroking/order/v1/getOrderBook",
    "positions": "/rest/secure/angelbroking/order/v1/getPosition",
    "rms": "/rest/secure/angelbroking/user/v1/getRMS",
}

IST = timezone(timedelta(hours=5, minutes=30))

# Jettone product -> SmartAPI producttype. The SmartAPI values are verified
# (Constants.java); the pairing is the semantic equivalent and is re-checked in
# the supervised capture before Angel One is enabled for trading.
PRODUCT_MAP = {
    ProductType.CNC: "DELIVERY",
    ProductType.MIS: "INTRADAY",
    ProductType.NRML: "CARRYFORWARD",
}

# Jettone order type -> (variety, ordertype).
ORDER_TYPE_MAP = {
    OrderType.MARKET: ("NORMAL", "MARKET"),
    OrderType.LIMIT: ("NORMAL", "LIMIT"),
    OrderType.SL: ("STOPLOSS", "STOPLOSS_LIMIT"),
    OrderType.SL_M: ("STOPLOSS", "STOPLOSS_MARKET"),
}


class AngelOneError(Exception):
    pass


class AngelAuthError(AngelOneError):
    pass


class AngelCapabilityUnavailable(AngelOneError):
    """An operation whose response contract is not verified yet."""


def totp_now(secret_b32: str, at: Optional[float] = None, step: int = 30, digits: int = 6) -> str:
    """RFC 6238 TOTP (HMAC-SHA1). Computed server-side; never returned or logged."""
    clean = secret_b32.replace(" ", "").upper()
    key = base64.b32decode(clean + "=" * (-len(clean) % 8))
    counter = int((time.time() if at is None else at) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def seconds_until_ist_midnight(now: Optional[datetime] = None) -> int:
    now = (now or datetime.now(timezone.utc)).astimezone(IST)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, int((midnight - now).total_seconds()))


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else "0"))
    except InvalidOperation as exc:
        raise AngelCapabilityUnavailable(f"non-numeric value in broker response: {value!r}") from exc


class AngelOneClient:
    """One instance per Angel One account. Credentials arrive already
    decrypted from the credential vault and are held in memory only."""

    def __init__(
        self,
        account_id: str,
        client_code: str,
        api_key: str,
        pin: str,
        totp_secret: str,
        redis: Any,
        static_ip: Optional[str],
        static_ip_valid_until: Optional[date] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        proxy_url: Optional[str] = None,
        local_ip: str = "127.0.0.1",
        mac_address: str = "00:00:00:00:00:00",
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        contract: AngelContract = ANGEL_CONTRACT,
        instruments: Optional[AngelInstrumentStore] = None,
    ):
        self.account_id = str(account_id)
        self.user_id = client_code.strip()
        self._api_key = api_key
        self._pin = pin
        self._totp_secret = totp_secret
        self.redis = redis
        self.static_ip = static_ip
        self.static_ip_valid_until = static_ip_valid_until
        self._local_ip = local_ip
        self._mac = mac_address
        self._clock = clock
        self._contract = contract
        self._instruments = instruments if instruments is not None else default_store
        self._login_lock = asyncio.Lock()
        self._login_generation = 0
        if http_client is not None:
            self._http = http_client
        else:
            self._http = httpx.AsyncClient(timeout=10.0, proxy=proxy_url) if proxy_url else httpx.AsyncClient(timeout=10.0)

    def __repr__(self) -> str:  # never expose credentials
        return f"AngelOneClient(account_id={self.account_id!r}, client_code={self.user_id!r})"

    @property
    def session_key(self) -> str:
        return f"angel:session:{self.account_id}"

    # ── headers / session ────────────────────────────────────────────────
    def _headers(self, jwt: Optional[str] = None) -> Dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": self._local_ip,
            "X-ClientPublicIP": self.static_ip or "",
            "X-MACAddress": self._mac,
            "X-PrivateKey": self._api_key,
        }
        if jwt:
            h["Authorization"] = f"Bearer {jwt}"
        return h

    def _require_static_ip(self) -> None:
        # Exchange rule (1 Apr 2026): API orders only from the account's own
        # registered static IP. Without one recorded - or after it expired -
        # never send an order.
        if not self.static_ip:
            raise AngelOneError("no registered static IP recorded for this Angel One account")
        if self.static_ip_valid_until is not None and self._clock().astimezone(IST).date() > self.static_ip_valid_until:
            raise AngelOneError(f"static IP expired on {self.static_ip_valid_until.isoformat()}")

    async def login(self) -> None:
        """clientcode + PIN + server-generated TOTP. Stores the JWT encrypted in
        Redis until IST midnight (SmartAPI sessions end daily). Single-flight:
        concurrent callers share one login (a TOTP code cannot be reused)."""
        generation = self._login_generation
        async with self._login_lock:
            if self._login_generation != generation:
                return  # another caller completed a login while this one waited
            body = {"clientcode": self.user_id, "password": self._pin, "totp": totp_now(self._totp_secret)}
            try:
                resp = await self._http.post(f"{BASE_URL}{ROUTES['login']}", json=body, headers=self._headers())
            except httpx.RequestError as exc:
                raise AngelAuthError(f"network error during login: {type(exc).__name__}") from exc
            data = self._json(resp)
            if resp.status_code != 200 or data.get("status") is not True:
                raise AngelAuthError(f"login rejected: {data.get('message') or resp.status_code} ({data.get('errorcode') or '-'})")
            tokens = data.get("data") or {}
            jwt = tokens.get("jwtToken")
            if not jwt:
                raise AngelAuthError("login returned no jwtToken")
            payload = json.dumps({"jwt": jwt, "refresh": tokens.get("refreshToken"), "at": self._clock().isoformat()})
            await self.redis.set(self.session_key, encrypt_secret(payload), ex=seconds_until_ist_midnight(self._clock()))
            self._login_generation += 1
            logger.info(f"[{self.account_id}] Angel One session established.")

    async def get_session(self, force_refresh: bool = False) -> str:
        if force_refresh:
            await self.login()
        raw = await self.redis.get(self.session_key)
        if not raw:
            raise AngelAuthError("no active Angel One session; log in again")
        if isinstance(raw, bytes):
            raw = raw.decode()
        return json.loads(decrypt_secret(raw))["jwt"]

    async def logout(self) -> None:
        try:
            jwt = await self.get_session()
        except AngelAuthError:
            return
        try:
            await self._http.post(
                f"{BASE_URL}{ROUTES['logout']}", json={"clientcode": self.user_id}, headers=self._headers(jwt)
            )
        finally:
            await self.redis.delete(self.session_key)

    @staticmethod
    def _json(resp: httpx.Response) -> Dict[str, Any]:
        try:
            body = resp.json()
            return body if isinstance(body, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _is_token_failure(resp: httpx.Response, data: Dict[str, Any]) -> bool:
        return resp.status_code in (401, 403) or data.get("error_type") == "TokenException"

    async def _call(self, route: str, body: Optional[Dict[str, Any]] = None, method: str = "POST") -> Dict[str, Any]:
        """Authenticated call. A token failure clears the session and raises -
        it is never retried silently, so an order is sent at most once."""
        jwt = await self.get_session()
        resp = await self._http.request(method, f"{BASE_URL}{ROUTES[route]}", json=body, headers=self._headers(jwt))
        data = self._json(resp)
        if self._is_token_failure(resp, data):
            await self.redis.delete(self.session_key)
            raise AngelAuthError("Angel One session rejected; log in again")
        return {"_http_status": resp.status_code, **data}

    # ── instruments ──────────────────────────────────────────────────────
    async def resolve_instrument(self, exchange: str, symbol: str) -> Tuple[str, str]:
        """(Angel symboltoken, Angel tradingsymbol). Another broker's token is
        never accepted; raises InstrumentResolutionError when not exactly one."""
        return self._instruments.resolve(exchange, symbol)

    # ── orders ───────────────────────────────────────────────────────────
    def build_order_body(
        self,
        *,
        symbol: str,
        exchange: str,
        side: OrderSide,
        quantity: int,
        product_type: Any,
        order_type: Any,
        price: Decimal,
        trigger_price: Decimal = Decimal("0"),
    ) -> Dict[str, Any]:
        """Validates and maps an order; raises (nothing is sent) on any problem."""
        exch = str(exchange).upper()
        token, tradingsymbol = self._instruments.resolve(exch, symbol)  # NSE/BSE equity only
        if quantity <= 0:
            raise AngelOneError("quantity must be positive")
        product = PRODUCT_MAP.get(ProductType(product_type))
        variety_type = ORDER_TYPE_MAP.get(OrderType(order_type))
        if product is None or variety_type is None:
            raise AngelOneError(f"unmapped product/order type {product_type}/{order_type}")
        variety, ordertype = variety_type
        if ordertype in ("LIMIT", "STOPLOSS_LIMIT") and price <= 0:
            raise AngelOneError(f"{ordertype} requires a positive price")
        if variety == "STOPLOSS" and trigger_price <= 0:
            raise AngelOneError("stop-loss orders require a positive trigger price")
        body = {
            "variety": variety,
            "tradingsymbol": tradingsymbol,
            "symboltoken": token,
            "transactiontype": OrderSide(side).value,
            "exchange": exch,
            "ordertype": ordertype,
            "producttype": product,
            "duration": "DAY",
            "price": str(price if ordertype in ("LIMIT", "STOPLOSS_LIMIT") else 0),
            "squareoff": "0",
            "stoploss": "0",
            "quantity": str(quantity),
        }
        if variety == "STOPLOSS":
            body["triggerprice"] = str(trigger_price)
        return body

    def _result(self, category: OrderResultCategory, req_id: Optional[str], order_id: Optional[str], msg: str, raw: Dict[str, Any]) -> OrderResult:
        return OrderResult(category=category, client_order_id=req_id, broker_order_id=order_id, message=msg, raw_response=raw, timestamp=self._clock())

    async def place_order(self, req: OrderRequest) -> OrderResult:
        """req.instrument_token (another broker's id) is deliberately ignored."""
        try:
            self._require_static_ip()
            body = self.build_order_body(
                symbol=req.symbol, exchange=req.exchange, side=req.side, quantity=req.quantity,
                product_type=req.product_type, order_type=req.order_type, price=req.price,
                trigger_price=req.trigger_price,
            )
        except Exception as exc:
            return self._result(OrderResultCategory.REJECTED, req.client_order_id, None, f"Not sent: {exc}", {})
        try:
            data = await self._call("place", body)
        except AngelAuthError as exc:
            return self._result(OrderResultCategory.REJECTED, req.client_order_id, None, str(exc), {})
        except httpx.TimeoutException:
            # Ambiguous: the broker may have accepted it. Never resend.
            return self._result(OrderResultCategory.TIMEOUT, req.client_order_id, None, "placeOrder timed out; reconcile before retrying", {})
        except httpx.RequestError as exc:
            return self._result(OrderResultCategory.NETWORK_ERROR, req.client_order_id, None, type(exc).__name__, {})
        payload = data.get("data")
        order_id = payload.get("orderid") if isinstance(payload, dict) else None
        if data.get("status") is True and order_id:
            return self._result(OrderResultCategory.SUCCESS, req.client_order_id, str(order_id), data.get("message") or "SUCCESS", data)
        return self._result(OrderResultCategory.REJECTED, req.client_order_id, None, f"{data.get('message') or 'rejected'} ({data.get('errorcode') or data.get('_http_status')})", data)

    async def cancel_order(self, broker_order_id: str, symbol: str = "", exchange: str = "NSE", variety: str = "NORMAL") -> OrderResult:
        try:
            self._require_static_ip()
            data = await self._call("cancel", {"variety": variety, "orderid": broker_order_id})
        except (AngelOneError, httpx.RequestError) as exc:
            return self._result(OrderResultCategory.REJECTED, None, broker_order_id, str(exc), {})
        ok = data.get("status") is True
        return self._result(OrderResultCategory.SUCCESS if ok else OrderResultCategory.REJECTED, None, broker_order_id, data.get("message") or "", data)

    async def modify_order(
        self,
        broker_order_id: str,
        quantity: int,
        price: Decimal,
        trigger_price: Decimal = Decimal("0"),
        order_type: OrderType = OrderType.LIMIT,
        symbol: str = "",
        exchange: str = "NSE",
        product_type: Optional[ProductType] = None,
    ) -> OrderResult:
        """The product is required (never defaulted): a modify sent with the
        wrong product could change a different holding's order."""
        try:
            self._require_static_ip()
            if product_type is None:
                raise AngelOneError("product type is required to modify an Angel One order")
            body = self.build_order_body(
                symbol=symbol, exchange=exchange, side=OrderSide.BUY, quantity=quantity,
                product_type=product_type, order_type=order_type, price=price, trigger_price=trigger_price,
            )
            # transactiontype is not part of a modify request; orderid identifies the order.
            body.pop("transactiontype", None)
            body["orderid"] = broker_order_id
            data = await self._call("modify", body)
        except (AngelOneError, ValueError, httpx.RequestError) as exc:
            return self._result(OrderResultCategory.REJECTED, None, broker_order_id, str(exc), {})
        except Exception as exc:  # unresolved instrument etc.
            return self._result(OrderResultCategory.REJECTED, None, broker_order_id, f"Not sent: {exc}", {})
        ok = data.get("status") is True
        return self._result(OrderResultCategory.SUCCESS if ok else OrderResultCategory.REJECTED, None, broker_order_id, data.get("message") or "", data)

    # ── reads (fail closed until the contract is verified) ───────────────
    def _require_contract(self) -> AngelContract:
        if not contract_is_usable(self._contract):
            raise AngelCapabilityUnavailable(
                "Angel One response contract is not verified (order statuses, positions, funds)"
            )
        return self._contract

    @staticmethod
    def parse_order_row(row: Dict[str, Any], status_map: Dict[str, str]) -> OrderRecord:
        """Maps one order-book row using the SDK-verified keys. Raises for a
        status value not in `status_map` (never guesses a lifecycle state)."""
        raw_status = str(row.get("status", "")).strip().lower()
        if raw_status not in status_map:
            raise AngelCapabilityUnavailable(f"unverified Angel One order status '{raw_status}'")
        try:
            side = OrderSide(str(row["transactiontype"]).upper())
            qty = int(row["quantity"])
            filled = int(row.get("filledshares") or 0)
            order_id = str(row["orderid"])
        except (KeyError, ValueError) as exc:
            raise AngelCapabilityUnavailable(f"order row missing/invalid field: {exc}") from exc
        return OrderRecord(
            broker_order_id=order_id,
            client_order_id=None,
            symbol=str(row.get("tradingsymbol", "")),
            exchange=str(row.get("exchange", "")),
            side=side,
            quantity=qty,
            filled_quantity=filled,
            average_price=_dec(row.get("averageprice")),
            status=status_map[raw_status],
            order_timestamp=datetime.now(timezone.utc),
            order_type=str(row.get("ordertype", "")),
            product_type=str(row.get("producttype", "")),
            price=_dec(row.get("price")),
            trigger_price=_dec(row.get("triggerprice")),
            rejection_reason=(str(row.get("text")) if row.get("text") else None),
            instrument_id=(str(row["symboltoken"]) if row.get("symboltoken") else None),
        )

    async def get_order_book(self) -> List[OrderRecord]:
        contract = self._require_contract()
        data = await self._call("order_book", None, method="GET")
        rows = data.get("data")
        if data.get("status") is not True or not isinstance(rows, list):
            raise AngelOneError(f"order book fetch failed: {data.get('message')}")
        return [self.parse_order_row(r, contract.status_map) for r in rows]

    def parse_position_row(self, row: Dict[str, Any], keys) -> Position:
        try:
            qty = int(Decimal(str(row[keys.net_qty])))
            symbol = str(row[keys.symbol])
            product = str(row[keys.product])
            exchange = str(row[keys.exchange])
        except (KeyError, InvalidOperation, ValueError) as exc:
            raise AngelCapabilityUnavailable(f"position row missing/invalid field: {exc}") from exc
        token = row.get(keys.token)
        return Position(
            symbol=symbol,
            quantity=qty,
            side="BUY" if qty >= 0 else "SELL",
            average_price=_dec(row.get(keys.avg_price)),
            account_id=self.account_id,
            exchange=exchange,
            product_type=product,
            instrument_id=(str(token) if token else None),
        )

    async def get_positions(self) -> List[Position]:
        contract = self._require_contract()
        data = await self._call("positions", None, method="GET")
        rows = data.get("data")
        if data.get("status") is not True:
            raise AngelOneError(f"positions fetch failed: {data.get('message')}")
        if rows is None:
            return []  # a successful response with no positions
        if not isinstance(rows, list):
            raise AngelCapabilityUnavailable("positions response is not a list")
        return [self.parse_position_row(r, contract.positions) for r in rows]

    async def get_positions_or_none(self) -> Optional[List[Position]]:
        """None = the position book could not be established (unverified
        contract, no session, network or parse failure): callers fail closed.
        [] means a successful response that listed no positions."""
        try:
            return await self.get_positions()
        except Exception as exc:
            logger.warning(f"[{self.account_id}] Angel One position book unavailable: {type(exc).__name__}")
            return None

    async def get_funds(self) -> FundsData:
        contract = self._require_contract()
        data = await self._call("rms", None, method="GET")
        row = data.get("data")
        if data.get("status") is not True or not isinstance(row, dict):
            raise AngelOneError(f"funds fetch failed: {data.get('message')}")
        keys = contract.rms
        missing = [k for k in (keys.available_margin, keys.used_margin, keys.collateral) if k not in row]
        if missing:
            raise AngelCapabilityUnavailable(f"funds response missing expected field(s): {missing}")
        return FundsData(
            account_id=self.account_id,
            available_margin=_dec(row[keys.available_margin]),
            collateral=_dec(row[keys.collateral]),
            used_margin=_dec(row[keys.used_margin]),
            updated_at=self._clock(),
        )

    async def aclose(self) -> None:
        await self._http.aclose()
