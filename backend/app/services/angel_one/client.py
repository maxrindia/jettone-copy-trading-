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

from app.core.crypto import EncryptionKeyError, decrypt_secret, encrypt_secret, validate_encryption_key
from app.services.alice_blue.instruments import InstrumentResolutionError
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
from app.core.config import settings
from app.services.brokers.login_guard import run_guarded_login
from app.services.brokers.position_checks import duplicate_position_key
from app.services.brokers.rate_limit import RateLimitConfigError, RateLimitExceeded, SlidingWindowLimiter, parse_rate_limits
from app.services.brokers.strict_numbers import StrictNumberError, need, optional_decimal, strict_decimal, strict_int
from app.services.brokers.transport import SendState, is_provably_not_sent
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


# SmartAPI ordertype -> Jettone's common vocabulary (MARKET / LIMIT / SL / SLM), the reverse of the
# verified ORDER_TYPE_MAP above. An unlisted value is passed through unchanged, as before.
_COMMON_ORDER_TYPE = {"MARKET": "MARKET", "LIMIT": "LIMIT", "STOPLOSS_LIMIT": "SL", "STOPLOSS_MARKET": "SLM"}


class AngelOneError(Exception):
    pass


class AngelAuthError(AngelOneError):
    pass


class AngelRateLimited(AngelOneError):
    """The request was NOT sent: a configured client-side rate limit (or its invalid configuration) refused it."""


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


def _strict(fn, *args, **kwargs):
    """Run a strict-number helper, turning its (value-free) error into the adapter's typed failure."""
    try:
        return fn(*args, **kwargs)
    except StrictNumberError as exc:
        raise AngelCapabilityUnavailable(f"Angel One response {exc}") from None
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise AngelCapabilityUnavailable(f"Angel One response has an unreadable number ({type(exc).__name__})") from None


def _dec(row: Any, key: str, **kw) -> Decimal:
    return _strict(lambda: strict_decimal(need(row, key), key, **kw))


def _int(row: Any, key: str, **kw) -> int:
    return _strict(lambda: strict_int(need(row, key), key, **kw))


def _opt(row: Any, key: str, **kw) -> Decimal:
    return _strict(lambda: optional_decimal(row, key, **kw))


class AngelOneClient:
    """One instance per Angel One account. Credentials arrive already
    decrypted from the credential vault and are held in memory only."""

    # Logging in contacts Angel One, so it happens only on an explicit request
    # (POST /accounts/{id}/broker-login), never from the engine's startup auth.
    AUTO_LOGIN_ON_STARTUP = False

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
        order_rate_limits: Optional[str] = None,
        read_rate_limits: Optional[str] = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
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
        # Client-side limits come from configuration only (no figure is assumed). A malformed value does not
        # silently disable limiting: every non-login request is then refused before it is sent.
        self._rate_config_invalid = False
        self._order_limiter = self._build_limiter(
            settings.ANGEL_ONE_ORDER_RATE_LIMITS if order_rate_limits is None else order_rate_limits, monotonic, sleep)
        self._read_limiter = self._build_limiter(
            settings.ANGEL_ONE_READ_RATE_LIMITS if read_rate_limits is None else read_rate_limits, monotonic, sleep)
        if http_client is not None:
            self._http = http_client
        else:
            self._http = httpx.AsyncClient(timeout=10.0, proxy=proxy_url) if proxy_url else httpx.AsyncClient(timeout=10.0)

    def _build_limiter(self, text: Optional[str], monotonic, sleep) -> SlidingWindowLimiter:
        try:
            return SlidingWindowLimiter(parse_rate_limits(text), now=monotonic, sleep=sleep)
        except RateLimitConfigError:
            self._rate_config_invalid = True
            return SlidingWindowLimiter(())

    async def _acquire_rate_slot(self, route: str) -> None:
        """Waits briefly for a slot or refuses - always BEFORE anything is sent, so it can neither duplicate
        nor mask an order. Callers must never answer a refusal by re-sending."""
        if self._rate_config_invalid:
            raise AngelRateLimited("Angel One rate-limit configuration is invalid; request not sent")
        limiter = self._order_limiter if route in ("place", "modify", "cancel") else self._read_limiter
        try:
            await limiter.acquire()
        except RateLimitExceeded as exc:
            raise AngelRateLimited(str(exc)) from None

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
        """clientcode + PIN + server-generated TOTP, through the cross-instance Redis guard: one login per
        account per spacing window across ALL client instances and API requests (a TOTP code cannot be
        reused and repeated attempts can lock the account), fail-closed if Redis is unavailable. Stores the
        JWT encrypted in Redis until IST midnight. Raises LoginThrottled when the guard refuses."""
        generation = self._login_generation
        async with self._login_lock:
            if self._login_generation != generation:
                return  # another caller on this instance completed a login while this one waited
            await run_guarded_login(
                redis=self.redis, broker="angelone", account_id=self.account_id, clock=self._clock,
                do_login=self._login_once, session_key=self.session_key,
            )
            self._login_generation += 1

    async def _login_once(self) -> None:
        body = {"clientcode": self.user_id, "password": self._pin, "totp": totp_now(self._totp_secret)}
        try:
            resp = await self._http.post(f"{BASE_URL}{ROUTES['login']}", json=body, headers=self._headers())
        except httpx.RequestError as exc:
            raise AngelAuthError(f"network error during login: {type(exc).__name__}") from None
        data = self._json(resp)
        if resp.status_code != 200 or data.get("status") is not True:
            raise AngelAuthError(f"login rejected: {data.get('message') or resp.status_code} ({data.get('errorcode') or '-'})")
        tokens = data.get("data") or {}
        jwt = tokens.get("jwtToken")
        if not jwt:
            raise AngelAuthError("login returned no jwtToken")
        payload = json.dumps({"jwt": jwt, "refresh": tokens.get("refreshToken"), "at": self._clock().isoformat()})
        await self.redis.set(self.session_key, encrypt_secret(payload), ex=seconds_until_ist_midnight(self._clock()))
        logger.info(f"[{self.account_id}] Angel One session established.")

    async def get_session(self, force_refresh: bool = False) -> str:
        if force_refresh:
            await self.login()
        try:
            raw = await self.redis.get(self.session_key)
        except Exception:
            raise AngelAuthError("Angel One session store unavailable; request not sent") from None
        if not raw:
            raise AngelAuthError("no active Angel One session; log in again")
        return await self._decode_session(raw)

    async def _decode_session(self, raw: Any) -> str:
        """See GrowwClient._decode_session: a sanitized AngelAuthError, discarding only this account's
        exact unreadable session key (never on a missing/invalid encryption key)."""
        try:
            validate_encryption_key()
        except EncryptionKeyError:
            raise AngelAuthError("Angel One session cannot be read: the encryption key is not configured") from None
        try:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            data = json.loads(decrypt_secret(text))
            jwt = data["jwt"]
            if not isinstance(jwt, str) or not jwt.strip():
                raise ValueError("no usable token")
            return jwt
        except (EncryptionKeyError, ValueError, KeyError, TypeError, AttributeError):
            await self._discard_unreadable_session(raw)
            raise AngelAuthError("stored Angel One session is unreadable; log in again") from None

    async def _discard_unreadable_session(self, raw: Any) -> None:
        try:
            current = await self.redis.get(self.session_key)
            if current == raw:
                await self.redis.delete(self.session_key)
                logger.warning(f"[{self.account_id}] Angel One stored session was unreadable and has been discarded.")
        except Exception:
            pass

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
    def _well_formed(resp: httpx.Response) -> bool:
        """True when the body is a JSON object (a malformed body is an ambiguous outcome for an order)."""
        try:
            return isinstance(resp.json(), dict)
        except Exception:
            return False

    @staticmethod
    def _is_token_failure(resp: httpx.Response, data: Dict[str, Any]) -> bool:
        return resp.status_code in (401, 403) or data.get("error_type") == "TokenException"

    async def _call(
        self, route: str, body: Optional[Dict[str, Any]] = None, method: str = "POST",
        state: Optional[SendState] = None,
    ) -> Dict[str, Any]:
        """Authenticated call. A token failure clears the session and raises -
        it is never retried silently, so an order is sent at most once."""
        await self._acquire_rate_slot(route)
        jwt = await self.get_session()
        if state is not None:
            state.sent = True      # from here on a failure may follow a request the broker already accepted
        resp = await self._http.request(method, f"{BASE_URL}{ROUTES[route]}", json=body, headers=self._headers(jwt))
        data = self._json(resp)
        if self._is_token_failure(resp, data):
            await self.redis.delete(self.session_key)
            raise AngelAuthError("Angel One session rejected; log in again")
        return {"_http_status": resp.status_code, "_well_formed": self._well_formed(resp), **data}

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

    @staticmethod
    def _safe_reason(exc: BaseException) -> str:
        """Our own validation messages carry no secrets and are shown; anything else is reduced to its type."""
        if isinstance(exc, (AngelOneError, InstrumentResolutionError)):
            return str(exc)
        return f"invalid order ({type(exc).__name__})"

    def _ambiguous(self, req: OrderRequest, why: str) -> OrderResult:
        """The order may or may not exist at Angel One. SmartAPI's placeOrder carries no client
        reference, so there is nothing to look it up or auto-match by: TIMEOUT routes it to the
        stale-order sweeper, which cannot match it and raises TIMEOUT_UNRESOLVED for manual review.
        It is never resent."""
        return self._result(
            OrderResultCategory.TIMEOUT, req.client_order_id, None,
            f"Angel One placeOrder outcome unknown ({why}); not resent - verify at the broker", {},
        )

    async def place_order(self, req: OrderRequest) -> OrderResult:
        """Total: never raises (see GrowwClient.place_order).
        req.instrument_token (another broker's id) is deliberately ignored."""
        state = SendState()
        try:
            return await self._place_order(req, state)
        except Exception as exc:
            logger.error(f"[{self.account_id}] Angel One place_order internal error ({type(exc).__name__}); sent={state.sent}")
            if state.sent:
                return self._ambiguous(req, "unexpected error after send")
            return self._result(OrderResultCategory.REJECTED, req.client_order_id, None,
                                f"Not sent: internal error ({type(exc).__name__})", {})

    async def _place_order(self, req: OrderRequest, state: SendState) -> OrderResult:
        try:
            self._require_static_ip()
            body = self.build_order_body(
                symbol=req.symbol, exchange=req.exchange, side=req.side, quantity=req.quantity,
                product_type=req.product_type, order_type=req.order_type, price=req.price,
                trigger_price=req.trigger_price,
            )
        except Exception as exc:
            return self._result(OrderResultCategory.REJECTED, req.client_order_id, None,
                                f"Not sent: {self._safe_reason(exc)}", {})
        try:
            data = await self._call("place", body, state=state)
        except (AngelAuthError, AngelRateLimited) as exc:
            return self._result(OrderResultCategory.REJECTED, req.client_order_id, None, str(exc), {})
        except httpx.RequestError as exc:
            if is_provably_not_sent(exc):
                return self._result(OrderResultCategory.NETWORK_ERROR, req.client_order_id, None,
                                    f"Angel One unreachable ({type(exc).__name__}); order not sent", {})
            return self._ambiguous(req, f"{type(exc).__name__} after send")

        http_status = data.get("_http_status", 0)
        payload = data.get("data")
        order_id = payload.get("orderid") if isinstance(payload, dict) else None
        if (data.get("status") is True and isinstance(order_id, (str, int)) and not isinstance(order_id, bool)
                and str(order_id).strip()):
            return self._result(OrderResultCategory.SUCCESS, req.client_order_id, str(order_id).strip(),
                                str(data.get("message") or "SUCCESS")[:200], data)
        # Definitive: a 4xx, or a well-formed body that says status false below 500.
        if http_status < 500 and data.get("_well_formed") and (400 <= http_status < 500 or data.get("status") is False):
            msg = f"{data.get('message') or 'rejected'} ({data.get('errorcode') or http_status})"
            return self._result(OrderResultCategory.REJECTED, req.client_order_id, None, str(msg)[:240], data)
        # 5xx, an unreadable body, status true without an order id, anything unexpected: the order may exist.
        return self._ambiguous(req, f"HTTP {http_status}, unusable response")

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
        """Maps one order-book row using the SDK-verified keys. Raises for a status value not in
        `status_map` (never guesses a lifecycle state) and for any number that is missing, null, blank,
        boolean, NaN, Infinity, negative or inconsistent (never reads one as zero)."""
        if not isinstance(row, dict):
            raise AngelCapabilityUnavailable("order row is not an object")
        raw_status = str(row.get("status", "")).strip().lower()
        if raw_status not in status_map:
            raise AngelCapabilityUnavailable(f"unverified Angel One order status '{raw_status}'")
        raw_id = row.get("orderid")
        if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)) or not str(raw_id).strip():
            raise AngelCapabilityUnavailable("order row has no usable orderid")
        try:
            side = OrderSide(str(row["transactiontype"]).upper())
        except (KeyError, ValueError):
            raise AngelCapabilityUnavailable("order row missing/invalid transactiontype") from None
        qty = _int(row, "quantity", positive=True)
        filled = _int(row, "filledshares", minimum=0, maximum=qty)            # a fill can never exceed the order
        otype = str(row.get("ordertype") or "").upper()
        price = _opt(row, "price", applicable=otype in ("LIMIT", "STOPLOSS_LIMIT"))
        trigger = _opt(row, "triggerprice", applicable=otype in ("STOPLOSS_LIMIT", "STOPLOSS_MARKET"))
        average = _opt(row, "averageprice", applicable=filled > 0)           # a fill must carry its price
        return OrderRecord(
            broker_order_id=str(raw_id).strip(),
            client_order_id=None,
            symbol=str(row.get("tradingsymbol", "")),
            exchange=str(row.get("exchange", "")),
            side=side,
            quantity=qty,
            filled_quantity=filled,
            average_price=average,
            status=status_map[raw_status],
            order_timestamp=datetime.now(timezone.utc),
            order_type=_COMMON_ORDER_TYPE.get(otype, str(row.get("ordertype", ""))),
            product_type=str(row.get("producttype", "")),
            price=price,
            trigger_price=trigger,
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
        if not isinstance(row, dict):
            raise AngelCapabilityUnavailable("position row is not an object")
        qty = _int(row, keys.net_qty)                                         # signed: a short is legitimately negative
        for key in (keys.symbol, keys.product, keys.exchange):
            value = row.get(key)
            if not isinstance(value, str) or not value.strip():
                raise AngelCapabilityUnavailable("position row has an unusable identifying field")
        # the price feeds the exposure check: required and positive for an open position
        price = _dec(row, keys.avg_price, positive=True, allow_negative=False) if qty != 0 else _opt(row, keys.avg_price)
        token = row.get(keys.token)
        return Position(
            symbol=row[keys.symbol],
            quantity=qty,
            side="BUY" if qty >= 0 else "SELL",
            average_price=price,
            account_id=self.account_id,
            exchange=row[keys.exchange],
            product_type=row[keys.product],
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
        positions = [self.parse_position_row(r, contract.positions) for r in rows]
        if duplicate_position_key(positions) is not None:
            raise AngelCapabilityUnavailable("position book lists an instrument and product more than once")
        return positions

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
        return FundsData(
            account_id=self.account_id,
            available_margin=_dec(row, keys.available_margin),              # finite; a negative balance is real
            collateral=_dec(row, keys.collateral, allow_negative=False),
            used_margin=_dec(row, keys.used_margin, allow_negative=False),
            updated_at=self._clock(),
        )

    async def aclose(self) -> None:
        await self._http.aclose()
