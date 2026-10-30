"""
Zerodha Kite Connect client (disabled by default; Child and Master roles gated separately)
==========================================================================================
Disabled unless ZERODHA_ENABLED=true. Every request goes to https://api.kite.trade and carries only that account's own api_key and
access token; nothing here can reach another broker's endpoints, and the Master's instrument id is never sent.

All paths, fields and vocabularies come from Zerodha's official Kite Connect v3 documentation (see contract.py for the source list and
for everything the documentation leaves unclear). Anything unclear fails closed: the account is not copied to, the read raises, and an
order that might have been accepted is never resent.

Sessions: Kite's login is interactive. The owner opens the login URL, signs in, and Kite redirects to the redirect URL registered for the
api_key with a short-lived `request_token`; `complete_login(request_token)` exchanges it (POST /session/token, checksum =
SHA-256(api_key + request_token + api_secret)). The access token expires at 6 AM the next day, so a fresh interactive login is needed
every trading day and the engine never logs in by itself (AUTO_LOGIN_ON_STARTUP is False). The token is kept encrypted in Redis only
until that expiry.

Safety rules shared with the other adapters: place_order never raises; an outcome that may have followed an accepted request is TIMEOUT
and is resolved only by a READ-ONLY lookup of the order's `tag`, never by a resend; a token rejection clears the session and is never
retried; only `regular` / `DAY` orders are placed; a part-filled order is never modified.
"""
import asyncio
import gzip
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
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
from app.services.brokers.login_guard import run_guarded_login
from app.services.brokers.position_checks import duplicate_position_key
from app.services.brokers.rate_limit import RateLimitExceeded, SlidingWindowLimiter
from app.services.brokers.strict_numbers import StrictNumberError, need, optional_decimal, strict_decimal, strict_int
from app.services.brokers.transport import SendState, is_provably_not_sent
from app.services.zerodha import contract as C
from app.services.zerodha.contract import ZERODHA_CONTRACT, ZerodhaContract, contract_is_usable
from app.services.zerodha.instruments import ZerodhaInstrumentStore, store as default_store

logger = logging.getLogger("zerodha_client")

IST = timezone(timedelta(hours=5, minutes=30))
ZERODHA_DAILY_LOGIN_CAP = 40                 # OUR OWN guard against a login loop; Zerodha documents no figure for /session/token
MAX_SESSION_SECONDS = 24 * 3600
SESSION_EXPIRY_HOUR_IST = 6                  # documented: the access token expires at 6 AM the next day
# order tag -> Jettone client_order_id, kept long enough for the stale-order sweeper.
TAG_MAP_TTL_SECONDS = 3 * 24 * 3600
TAG_PREFIX = "JT"

ORDER_TYPE_MAP = {OrderType.MARKET: "MARKET", OrderType.LIMIT: "LIMIT", OrderType.SL: "SL", OrderType.SL_M: "SL-M"}
# Jettone product -> Zerodha product, per exchange. Equity cash: CNC / MIS. NFO: NRML / MIS. Anything else is refused, not sent.
EQUITY_PRODUCTS = {ProductType.CNC: "CNC", ProductType.MIS: "MIS"}
DERIVATIVE_PRODUCTS = {ProductType.NRML: "NRML", ProductType.MIS: "MIS"}


class ZerodhaError(Exception):
    pass


class ZerodhaAuthError(ZerodhaError):
    pass


class ZerodhaCapabilityUnavailable(ZerodhaError):
    """A response was missing, malformed, inconsistent or outside the documented vocabulary."""


class ZerodhaRateLimited(ZerodhaError):
    pass


def order_tag_for(client_order_id: Optional[str]) -> str:
    """Deterministic 20-character alphanumeric `tag` (docs: "alphanumeric, max 20 chars"): the same Jettone order always maps to the
    same tag, so an ambiguous placement can be looked up in the order book by it."""
    seed = client_order_id or uuid.uuid4().hex
    return TAG_PREFIX + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:18]


def _strict(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except StrictNumberError as exc:
        raise ZerodhaCapabilityUnavailable(f"Zerodha response {exc}") from None
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ZerodhaCapabilityUnavailable(f"Zerodha response has an unreadable number ({type(exc).__name__})") from None


def _dec(row: Any, key: str, **kw) -> Decimal:
    return _strict(lambda: strict_decimal(need(row, key), key, **kw))


def _int(row: Any, key: str, **kw) -> int:
    return _strict(lambda: strict_int(need(row, key), key, **kw))


def _opt(row: Any, key: str, **kw) -> Decimal:
    return _strict(lambda: optional_decimal(row, key, **kw))


def _num(value: Decimal) -> str:
    """A Decimal as plain text for a form field (never scientific notation)."""
    return format(value.normalize(), "f")


def _parse_dt(raw: Any, assume: timezone = IST) -> Optional[datetime]:
    text = str(raw if raw is not None else "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=assume)


def normalize_status(raw: Any, quantity: int, filled: int) -> str:
    """Zerodha status -> the engine's common vocabulary, checked against the fill the row itself reports.

    OPEN / TRIGGER PENDING / COMPLETE / CANCELLED / REJECTED map directly (a part-filled OPEN order becomes PARTIAL: Zerodha defines no
    partial status). A row whose status and fill disagree (COMPLETE without a full fill, OPEN with no quantity left, a pending trigger
    that has filled) is REFUSED, never guessed. Interim states (VALIDATION PENDING, OPEN PENDING, ...) and any status Zerodha has not
    documented are returned unchanged and upper-cased: the Master poller does not dispatch an unmapped status, so such an order is
    neither copied nor lost, and it is picked up when it reaches OPEN."""
    status = " ".join(str(raw if raw is not None else "").upper().split())
    if status not in C.STATUS_MAP:
        return status or "UNKNOWN"
    mapped = C.STATUS_MAP[status]
    if mapped == "COMPLETE" and filled != quantity:
        raise ZerodhaCapabilityUnavailable("order is COMPLETE but its filled quantity differs from its quantity")
    if mapped == "OPEN":
        if filled >= quantity:
            raise ZerodhaCapabilityUnavailable("order is OPEN but nothing is left to fill")
        return "PARTIAL" if filled > 0 else "OPEN"
    if mapped == "TRIGGER_PENDING" and filled != 0:
        raise ZerodhaCapabilityUnavailable("order is TRIGGER PENDING but has fills")
    return mapped


def decode_instruments_payload(content: bytes) -> str:
    """The instruments dump is a gzipped CSV. It may arrive already decoded by the HTTP layer (Content-Encoding) or as a gzip file."""
    data = bytes(content)
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data.decode("utf-8")


class ZerodhaClient:
    """One instance per Zerodha account. Credentials arrive already decrypted from the credential vault and are held in memory only."""

    # The login is interactive (a request_token from the browser), so the engine never logs in by itself.
    AUTO_LOGIN_ON_STARTUP = False

    def __init__(
        self,
        account_id: str,
        label: str,
        api_key: str,
        api_secret: str,
        redis: Any,
        http_client: Optional[httpx.AsyncClient] = None,
        proxy_url: Optional[str] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        contract: ZerodhaContract = ZERODHA_CONTRACT,
        instruments: Optional[ZerodhaInstrumentStore] = None,
        sleep=asyncio.sleep,
        monotonic=time.monotonic,
    ):
        self.account_id = str(account_id)
        self.user_id = label.strip()
        self._api_key = api_key
        self._api_secret = api_secret
        self.redis = redis
        self._clock = clock
        self._contract = contract
        self._instruments = instruments if instruments is not None else default_store
        self._login_lock = asyncio.Lock()
        self._login_generation = 0
        self._instruments_lock = asyncio.Lock()
        self._orders_limiter = SlidingWindowLimiter(C.LIMIT_ORDERS, now=monotonic, sleep=sleep)
        self._reads_limiter = SlidingWindowLimiter(C.LIMIT_READS, now=monotonic, sleep=sleep)
        self._session_limiter = SlidingWindowLimiter(C.LIMIT_SESSION, now=monotonic, sleep=sleep)
        if http_client is not None:
            self._http = http_client
        else:
            self._http = httpx.AsyncClient(timeout=10.0, proxy=proxy_url) if proxy_url else httpx.AsyncClient(timeout=10.0)

    def __repr__(self) -> str:  # never expose credentials
        return f"ZerodhaClient(account_id={self.account_id!r}, label={self.user_id!r})"

    @property
    def session_key(self) -> str:
        return f"zerodha:session:{self.account_id}"

    # ── session ──────────────────────────────────────────────────────────
    def login_url(self) -> str:
        """The documented Kite login URL for this api_key. The owner opens it; Kite redirects with a `request_token`."""
        return f"{C.LOGIN_URL}?v={C.API_VERSION}&api_key={self._api_key}"

    @staticmethod
    def _json(resp: httpx.Response) -> Dict[str, Any]:
        try:
            body = resp.json()
            return body if isinstance(body, dict) else {}
        except Exception:
            return {}

    def _session_ttl(self) -> int:
        """Seconds until the documented expiry: 6 AM IST of the next day (or today, when it is still before 6 AM). Capped at 24 h."""
        now = self._clock().astimezone(IST)
        expiry = now.replace(hour=SESSION_EXPIRY_HOUR_IST, minute=0, second=0, microsecond=0)
        if now >= expiry:
            expiry += timedelta(days=1)
        ttl = int(min((expiry - now).total_seconds(), MAX_SESSION_SECONDS))
        if ttl <= 0:
            raise ZerodhaAuthError("the session would already be expired")
        return ttl

    async def login(self) -> None:
        """Kite's login needs the owner's browser: there is nothing the engine can do by itself. Fails with the instruction."""
        raise ZerodhaAuthError(
            "Zerodha login is interactive: open the Kite login URL, sign in, and submit the request_token it redirects with."
        )

    async def complete_login(self, request_token: str) -> None:
        """POST /session/token, through the cross-instance Redis guard (spacing and a daily cap, fail-closed if Redis is down). The
        request_token is single-use and short-lived, so a failed exchange needs a new browser login, never a retry here."""
        token = str(request_token or "").strip()
        if not token or len(token) > 256 or not token.isalnum():
            raise ZerodhaAuthError("the request_token is missing or malformed")
        generation = self._login_generation
        async with self._login_lock:
            if self._login_generation != generation:
                return
            await run_guarded_login(
                redis=self.redis, broker="zerodha", account_id=self.account_id, clock=self._clock,
                do_login=lambda: self._login_once(token), session_key=self.session_key, daily_cap=ZERODHA_DAILY_LOGIN_CAP,
            )
            self._login_generation += 1
        await self.ensure_instruments()

    async def _login_once(self, request_token: str) -> None:
        try:
            await self._session_limiter.acquire()
        except RateLimitExceeded as exc:
            raise ZerodhaAuthError(str(exc)) from exc
        checksum = hashlib.sha256((self._api_key + request_token + self._api_secret).encode("utf-8")).hexdigest()
        try:
            resp = await self._http.post(
                f"{C.BASE_URL}{C.ROUTES['session']}",
                data={"api_key": self._api_key, "request_token": request_token, "checksum": checksum},
                headers={"X-Kite-Version": C.API_VERSION},
            )
        except httpx.RequestError as exc:
            raise ZerodhaAuthError(f"network error during login: {type(exc).__name__}") from None
        data = self._json(resp)
        if resp.status_code != 200 or data.get("status") != "success" or not isinstance(data.get("data"), dict):
            raise ZerodhaAuthError(f"Zerodha refused the token request (HTTP {resp.status_code}); log in again to get a new request_token.")
        access = data["data"].get("access_token")
        if not isinstance(access, str) or not access.strip():
            raise ZerodhaAuthError("token response contained no access_token")
        # The session must belong to THIS account's Kite user: a request_token obtained by signing in as someone else is refused, never
        # stored. A response without a user_id cannot be bound to the account, so it is refused too.
        returned_user = data["data"].get("user_id")
        if not isinstance(returned_user, str) or not returned_user.strip() or returned_user.strip().upper() != self.user_id.strip().upper():
            raise ZerodhaAuthError("the Kite login belongs to a different user than this account; no session was stored")
        ttl = self._session_ttl()
        blob = json.dumps({"token": access.strip(), "at": self._clock().isoformat()})
        await self.redis.set(self.session_key, encrypt_secret(blob), ex=ttl)
        logger.info(f"[{self.account_id}] Zerodha session established (ttl={ttl}s).")

    async def get_session(self) -> str:
        try:
            raw = await self.redis.get(self.session_key)
        except Exception:
            raise ZerodhaAuthError("Zerodha session store unavailable; request not sent") from None
        if not raw:
            raise ZerodhaAuthError("no active Zerodha session; log in again")
        return await self._decode_session(raw)

    async def _decode_session(self, raw: Any) -> str:
        try:
            validate_encryption_key()
        except EncryptionKeyError:
            raise ZerodhaAuthError("Zerodha session cannot be read: the encryption key is not configured") from None
        try:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            data = json.loads(decrypt_secret(text))
            token = data["token"]
            if not isinstance(token, str) or not token.strip():
                raise ValueError("no usable token")
            return token
        except (EncryptionKeyError, ValueError, KeyError, TypeError, AttributeError):
            await self._discard_unreadable_session(raw)
            raise ZerodhaAuthError("stored Zerodha session is unreadable; log in again") from None

    async def _discard_unreadable_session(self, raw: Any) -> None:
        try:
            current = await self.redis.get(self.session_key)
            if current == raw:
                await self.redis.delete(self.session_key)
                logger.warning(f"[{self.account_id}] Zerodha stored session was unreadable and has been discarded.")
        except Exception:
            pass

    async def logout(self) -> None:
        """Invalidate the access token at Zerodha (DELETE /session/token) when a session exists, then forget it locally. A failed remote
        call still forgets the local session, so the account shows offline and a new login is required either way."""
        try:
            token = await self.get_session()
        except ZerodhaError:
            token = None
        try:
            if token:
                await self._http.request("DELETE", f"{C.BASE_URL}{C.ROUTES['session']}",
                                         params={"api_key": self._api_key, "access_token": token},
                                         headers={"X-Kite-Version": C.API_VERSION})
        except httpx.RequestError:
            pass
        finally:
            await self.redis.delete(self.session_key)

    # ── transport ────────────────────────────────────────────────────────
    async def _call(
        self, kind: str, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
        form: Optional[Dict[str, str]] = None, state: Optional[SendState] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """Authenticated call. kind: "order" (place/modify/cancel) or "read". A rejected token clears the session and raises; it is never
        retried, so an order write is sent at most once."""
        try:
            await (self._orders_limiter if kind == "order" else self._reads_limiter).acquire()
        except RateLimitExceeded as exc:
            raise ZerodhaRateLimited(str(exc)) from exc
        token = await self.get_session()
        headers = {"X-Kite-Version": C.API_VERSION, "Authorization": f"token {self._api_key}:{token}"}
        if state is not None:
            state.sent = True      # from here on a failure may follow a request the broker already accepted
        resp = await self._http.request(method, f"{C.BASE_URL}{path}", params=params, data=form, headers=headers)
        data = self._json(resp)
        if resp.status_code == 403 or data.get("error_type") in C.SESSION_ERROR_TYPES:
            await self.redis.delete(self.session_key)
            raise ZerodhaAuthError("Zerodha session rejected; log in again")
        return resp.status_code, data

    @staticmethod
    def _data(status_code: int, data: Dict[str, Any]) -> Any:
        """The `data` of a documented success envelope, or ZerodhaError. The error body names only the documented error_type."""
        if status_code == 200 and data.get("status") == "success" and "data" in data:
            return data["data"]
        raise ZerodhaError(f"Zerodha returned HTTP {status_code} / {data.get('error_type') or data.get('status')!r}")

    # ── instruments ──────────────────────────────────────────────────────
    async def resolve_instrument(self, exchange: str, symbol: str) -> Tuple[str, str]:
        """(Zerodha instrument_token, tradingsymbol). Another broker's token is never accepted. The SELL guard matches positions on the
        tradingsymbol (positions carry no token here), so a stale token can never license a SELL."""
        inst = self._instruments.resolve(exchange, symbol)
        return inst.instrument_token, inst.tradingsymbol

    async def ensure_instruments(self) -> None:
        """Load the NSE and BSE instrument dumps into the store when it is empty (after a login or a restart). Read-only and best
        effort: a failure is logged by type only, and the order path then refuses the symbol as "not loaded" instead of guessing."""
        if self._instruments.size:
            return
        async with self._instruments_lock:
            if self._instruments.size:
                return
            for exchange in ("NSE", "BSE"):                  # cash equity only: a cross-broker derivative is refused upstream
                try:
                    self._instruments.load_csv_text(await self.fetch_instruments(exchange))
                except Exception as exc:
                    logger.warning(f"[{self.account_id}] Zerodha {exchange} instrument dump not loaded ({type(exc).__name__})")

    async def fetch_instruments(self, exchange: str) -> str:
        """GET /instruments/:exchange: the CSV text (decoded from gzip when needed). Read-only; raises on anything unexpected."""
        exch = str(exchange or "").strip().upper()
        if exch not in C.SUPPORTED_EXCHANGES:
            raise ZerodhaError(f"{exchange!r} is not a supported exchange")
        await self._reads_limiter.acquire()
        token = await self.get_session()
        resp = await self._http.request("GET", f"{C.BASE_URL}{C.ROUTES['instruments'].format(exchange=exch)}",
                                        headers={"X-Kite-Version": C.API_VERSION, "Authorization": f"token {self._api_key}:{token}"})
        if resp.status_code != 200:
            raise ZerodhaError(f"instrument dump returned HTTP {resp.status_code}")
        return decode_instruments_payload(resp.content)

    # ── orders ───────────────────────────────────────────────────────────
    def _result(self, category: OrderResultCategory, req_id: Optional[str], order_id: Optional[str], msg: str,
                raw: Dict[str, Any]) -> OrderResult:
        return OrderResult(category=category, client_order_id=req_id, broker_order_id=order_id, message=msg,
                           raw_response=raw, timestamp=self._clock())

    def build_order_body(
        self, *, symbol: str, exchange: str, side: Any, quantity: int, product_type: Any, order_type: Any,
        price: Decimal, trigger_price: Decimal = Decimal("0"), tag: str,
    ) -> Dict[str, str]:
        """Validates and maps an order to the documented form parameters. Raises (nothing sent) on any problem."""
        inst = self._instruments.resolve(exchange, symbol)
        side_v = OrderSide(side).value
        if quantity <= 0:
            raise ZerodhaError("quantity must be positive")
        if inst.lot_size_stated and inst.lot_size > 1 and quantity % inst.lot_size:
            raise ZerodhaError(f"quantity is not a multiple of the lot size ({inst.lot_size}); not rounded")
        products = EQUITY_PRODUCTS if inst.exchange in C.EQUITY_EXCHANGES else DERIVATIVE_PRODUCTS
        product = products.get(ProductType(product_type))
        if product is None:
            raise ZerodhaError(f"product {ProductType(product_type).value} is not supported for Zerodha {inst.exchange} orders")
        otype = ORDER_TYPE_MAP.get(OrderType(order_type))
        if otype is None:
            raise ZerodhaError(f"unmapped order type {order_type}")
        if otype in ("LIMIT", "SL") and price <= 0:
            raise ZerodhaError(f"{otype} requires a positive price")
        if otype in ("SL", "SL-M") and trigger_price <= 0:
            raise ZerodhaError(f"{otype} requires a positive trigger price")
        if len(tag) > C.TAG_MAX_LENGTH or not tag.isalnum():
            raise ZerodhaError("the order tag is not alphanumeric within 20 characters")
        body: Dict[str, str] = {
            "tradingsymbol": inst.tradingsymbol, "exchange": inst.exchange, "transaction_type": side_v, "order_type": otype,
            "quantity": str(int(quantity)), "product": product, "validity": C.PLACED_VALIDITY, "tag": tag,
        }
        if otype in ("LIMIT", "SL"):
            body["price"] = _num(price)
        if otype in ("SL", "SL-M"):
            body["trigger_price"] = _num(trigger_price)
        if otype in ("MARKET", "SL-M"):
            body["market_protection"] = C.MARKET_PROTECTION_AUTOMATIC     # documented value -1: automatic protection
        return body

    @staticmethod
    def _safe_reason(exc: BaseException) -> str:
        if isinstance(exc, (ZerodhaError, InstrumentResolutionError)):
            return str(exc)
        return f"invalid order ({type(exc).__name__})"

    def _tag_key(self, tag: str) -> str:
        return f"zerodha:tag:{self.account_id}:{tag}"

    def _ambiguous(self, req: OrderRequest, tag: str, why: str) -> OrderResult:
        """The order may or may not exist at Zerodha. TIMEOUT is not retried by the pipeline and is reconciled by the stale-order sweeper;
        nothing here may ever resend the create."""
        return self._result(OrderResultCategory.TIMEOUT, req.client_order_id, None,
                            f"Zerodha create outcome unknown ({why}); not resent - reconcile at the broker", {"tag": tag})

    @staticmethod
    def _is_definitive_rejection(status: int, data: Dict[str, Any]) -> bool:
        """Only a 4xx (other than the session / throttle codes handled before) proves Zerodha did not take the order. A 5xx - including
        502 (OMS down) and 504 (gateway timeout) - may follow a request the OMS accepted."""
        return 400 <= status < 500

    async def place_order(self, req: OrderRequest) -> OrderResult:
        """Total: never raises. An unexpected error before the request is issued is a clean rejection; one after it is an ambiguous
        TIMEOUT. req.instrument_token (another broker's id) is deliberately ignored."""
        tag = order_tag_for(req.client_order_id)
        state = SendState()
        try:
            return await self._place_order(req, tag, state)
        except Exception as exc:
            logger.error(f"[{self.account_id}] Zerodha place_order internal error ({type(exc).__name__}); sent={state.sent}")
            if state.sent:
                return self._ambiguous(req, tag, "unexpected error after send")
            return self._result(OrderResultCategory.REJECTED, req.client_order_id, None,
                                f"Not sent: internal error ({type(exc).__name__})", {})

    async def _place_order(self, req: OrderRequest, tag: str, state: SendState) -> OrderResult:
        await self.ensure_instruments()
        try:
            body = self.build_order_body(
                symbol=req.symbol, exchange=req.exchange, side=req.side, quantity=req.quantity, product_type=req.product_type,
                order_type=req.order_type, price=req.price, trigger_price=req.trigger_price, tag=tag,
            )
        except Exception as exc:
            return self._result(OrderResultCategory.REJECTED, req.client_order_id, None, f"Not sent: {self._safe_reason(exc)}", {})

        # Persist tag -> Jettone order id BEFORE sending, so a crash after the send still leaves the sweeper able to match the order book
        # entry. No mapping, no send.
        if req.client_order_id:
            try:
                await self.redis.set(self._tag_key(tag), req.client_order_id, ex=TAG_MAP_TTL_SECONDS)
            except Exception:
                return self._result(OrderResultCategory.REJECTED, req.client_order_id, None,
                                    "Not sent: the order tag could not be recorded", {})

        try:
            status, data = await self._call("order", "POST", C.ROUTES["order_place"].format(variety=C.PLACED_VARIETY), form=body, state=state)
        except (ZerodhaAuthError, ZerodhaRateLimited) as exc:
            return self._result(OrderResultCategory.REJECTED, req.client_order_id, None, str(exc), {})
        except httpx.RequestError as exc:
            if is_provably_not_sent(exc):
                return self._result(OrderResultCategory.NETWORK_ERROR, req.client_order_id, None,
                                    f"Zerodha unreachable ({type(exc).__name__}); order not sent", {})
            return await self._resolve_ambiguous(req, tag, f"{type(exc).__name__} after send")

        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        oid = payload.get("order_id")
        if status == 200 and data.get("status") == "success" and isinstance(oid, (str, int)) and not isinstance(oid, bool) and str(oid).strip():
            return self._result(OrderResultCategory.SUCCESS, req.client_order_id, str(oid).strip(), "SUCCESS", data)
        if status == 429:
            return self._result(OrderResultCategory.REJECTED, req.client_order_id, None,
                                "Zerodha rate limit reached (HTTP 429); order not placed", data)
        if self._is_definitive_rejection(status, data):
            etype = data.get("error_type")
            msg = f"Zerodha rejected the order (HTTP {status}" + (f", {etype}" if isinstance(etype, str) and etype else "") + ")"
            reason = data.get("message")
            if isinstance(reason, str) and reason:
                msg += f": {reason[:200]}"
            return self._result(OrderResultCategory.REJECTED, req.client_order_id, None, msg, data)
        # 5xx, an unexpected 2xx/3xx, success without an order id, an unreadable body: the order may exist.
        return await self._resolve_ambiguous(req, tag, f"HTTP {status}, unusable response")

    async def _resolve_ambiguous(self, req: OrderRequest, tag: str, why: str) -> OrderResult:
        """One read-only lookup by our tag: found -> SUCCESS; otherwise TIMEOUT. Never a resend."""
        try:
            found = await self.find_by_client_reference(tag)
        except Exception:
            found = None
        if found is not None and found.broker_order_id:
            # The order EXISTS. Only a live or filled state counts as placed; a rejected / cancelled one is reported as such, and an
            # interim or unrecognised state is still tracked by its id (it exists at the broker).
            state = str(found.status or "").upper()
            if state in ("REJECTED", "CANCELLED"):
                return self._result(OrderResultCategory.REJECTED, req.client_order_id, found.broker_order_id,
                                    f"Zerodha order exists but is {state} (found by tag lookup)", {"tag": tag})
            return self._result(OrderResultCategory.SUCCESS, req.client_order_id, found.broker_order_id,
                                "SUCCESS (confirmed by tag lookup)", {"tag": tag})
        return self._ambiguous(req, tag, why)

    def _write_outcome(self, status: int, data: Dict[str, Any], order_id: str, verb: str) -> OrderResult:
        """Result of a modify / cancel write. SUCCESS only on the documented envelope; a 4xx is a definitive rejection; anything else may
        have been applied, so it is an ambiguous TIMEOUT (never reported as a rejection)."""
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        if status == 200 and data.get("status") == "success" and str(payload.get("order_id") or "").strip():
            return self._result(OrderResultCategory.SUCCESS, None, order_id, "SUCCESS", data)
        if status == 429 or self._is_definitive_rejection(status, data):
            reason = data.get("message")
            msg = f"Zerodha rejected the {verb} (HTTP {status}" + (f", {data.get('error_type')}" if data.get("error_type") else "") + ")"
            if isinstance(reason, str) and reason:
                msg += f": {reason[:200]}"
            return self._result(OrderResultCategory.REJECTED, None, order_id, msg, data)
        return self._result(OrderResultCategory.TIMEOUT, None, order_id,
                            f"Zerodha {verb} outcome unknown (HTTP {status}); not repeated - check the order at the broker", data)

    async def cancel_order(self, broker_order_id: str, symbol: str = "", exchange: str = "NSE") -> OrderResult:
        try:
            status, data = await self._call(
                "order", "DELETE", C.ROUTES["order_cancel"].format(variety=C.PLACED_VARIETY, order_id=broker_order_id), state=SendState(),
            )
        except (ZerodhaError, httpx.RequestError) as exc:
            if isinstance(exc, httpx.RequestError) and not is_provably_not_sent(exc):
                return self._result(OrderResultCategory.TIMEOUT, None, broker_order_id,
                                    f"Zerodha cancel outcome unknown ({type(exc).__name__}); not repeated", {})
            return self._result(OrderResultCategory.REJECTED, None, broker_order_id, str(exc) or type(exc).__name__, {})
        return self._write_outcome(status, data, broker_order_id, "cancel")

    async def modify_order(
        self, broker_order_id: str, quantity: int, price: Decimal, trigger_price: Decimal = Decimal("0"),
        order_type: OrderType = OrderType.LIMIT, symbol: str = "", exchange: str = "NSE",
        product_type: Optional[ProductType] = None,
    ) -> OrderResult:
        """Only an untouched, still-pending order is modified: the documentation does not say whether `quantity` is the total or the
        remaining size, so a part-filled order is refused rather than risk a wrong size."""
        try:
            if quantity <= 0:
                raise ZerodhaError("quantity must be positive")
            otype = ORDER_TYPE_MAP.get(OrderType(order_type))
            if otype is None:
                raise ZerodhaError(f"unmapped order type {order_type}")
            if otype in ("LIMIT", "SL") and price <= 0:
                raise ZerodhaError(f"{otype} requires a positive price")
            if otype in ("SL", "SL-M") and trigger_price <= 0:
                raise ZerodhaError(f"{otype} requires a positive trigger price")
            current = await self.get_order(broker_order_id)
            if current is None:
                raise ZerodhaError("order not found; not modifiable")
            state = " ".join(str(current.status or "").upper().split())
            if state == "PARTIAL" or (state in ("OPEN", "TRIGGER_PENDING") and current.filled_quantity != 0):
                raise ZerodhaError("order is part-filled; modify quantity semantics are not documented")
            if state not in ("OPEN", "TRIGGER_PENDING"):
                raise ZerodhaError(f"order is {state or 'in an unknown state'}; not modifiable")
            if current.filled_quantity != 0:
                raise ZerodhaError("order is part-filled; modify quantity semantics are not documented")
            form: Dict[str, str] = {"order_type": otype, "quantity": str(int(quantity)), "validity": C.PLACED_VALIDITY}
            if otype in ("LIMIT", "SL"):
                form["price"] = _num(price)
            if otype in ("SL", "SL-M"):
                form["trigger_price"] = _num(trigger_price)
            status, data = await self._call(
                "order", "PUT", C.ROUTES["order_modify"].format(variety=C.PLACED_VARIETY, order_id=broker_order_id), form=form,
                state=SendState(),
            )
        except (ZerodhaError, ValueError) as exc:
            return self._result(OrderResultCategory.REJECTED, None, broker_order_id, str(exc) or type(exc).__name__, {})
        except httpx.RequestError as exc:
            if is_provably_not_sent(exc):
                return self._result(OrderResultCategory.REJECTED, None, broker_order_id, f"Zerodha unreachable ({type(exc).__name__}); not sent", {})
            return self._result(OrderResultCategory.TIMEOUT, None, broker_order_id,
                                f"Zerodha modify outcome unknown ({type(exc).__name__}); not repeated", {})
        return self._write_outcome(status, data, broker_order_id, "modify")

    # ── reads (fail closed on anything undocumented) ─────────────────────
    def _require_contract(self) -> None:
        if not contract_is_usable(self._contract):
            raise ZerodhaCapabilityUnavailable("Zerodha response contract is not verified")

    def parse_order_row(self, row: Dict[str, Any]) -> OrderRecord:
        """Every number is strict (see strict_numbers): missing, null, blank, boolean, NaN, Infinity, negative or inconsistent values
        refuse the whole row. A well-formed row Jettone deliberately does not copy (another variety, validity or exchange) is NOT an
        error: it is returned with an unsupported status the Master poller will not dispatch."""
        if not isinstance(row, dict):
            raise ZerodhaCapabilityUnavailable("order row is not an object")
        raw_id = row.get("order_id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)) or not str(raw_id).strip():
            raise ZerodhaCapabilityUnavailable("order row has no usable order_id")
        qty = _int(row, "quantity", positive=True)
        filled = _int(row, "filled_quantity", minimum=0, maximum=qty)
        for optional_int in ("pending_quantity", "cancelled_quantity"):
            if row.get(optional_int) is not None:
                _int(row, optional_int, minimum=0, maximum=qty)
        try:
            side = OrderSide(str(row["transaction_type"]).upper())
        except (KeyError, ValueError):
            raise ZerodhaCapabilityUnavailable("order row has no usable transaction_type") from None
        otype = str(row.get("order_type") or "").upper()
        if otype not in C.ORDER_TYPES:
            raise ZerodhaCapabilityUnavailable("order row has an undocumented order_type")
        status = normalize_status(row.get("status"), qty, filled)
        price = _opt(row, "price", applicable=otype in ("LIMIT", "SL"))
        trigger = _opt(row, "trigger_price", applicable=otype in ("SL", "SL-M"))
        average = _opt(row, "average_price", applicable=False)            # documented "only for COMPLETE orders": never required
        exchange = str(row.get("exchange") or "").strip().upper()
        symbol = str(row.get("tradingsymbol") or "").strip()
        if not exchange or not symbol:
            raise ZerodhaCapabilityUnavailable("order row has no usable exchange or tradingsymbol")
        variety = str(row.get("variety") or C.PLACED_VARIETY).strip().lower()
        validity = str(row.get("validity") or C.PLACED_VALIDITY).strip().upper()
        if variety != C.PLACED_VARIETY:
            status = f"UNSUPPORTED_VARIETY_{variety.upper()}"
        elif validity != C.PLACED_VALIDITY:
            status = f"UNSUPPORTED_VALIDITY_{validity}"
        elif exchange not in C.SUPPORTED_EXCHANGES:
            status = f"UNSUPPORTED_EXCHANGE_{exchange}"
        tag = row.get("tag")
        if not isinstance(tag, str) or not tag:
            tags = row.get("tags")
            tag = tags[0] if isinstance(tags, list) and tags and isinstance(tags[0], str) else None
        created = _parse_dt(row.get("order_timestamp")) or self._clock()
        message = row.get("status_message")
        return OrderRecord(
            broker_order_id=str(raw_id).strip(),
            client_order_id=tag or None,
            symbol=symbol,
            exchange=exchange,
            side=side,
            quantity=qty,
            filled_quantity=filled,
            average_price=average,
            status=status,
            order_timestamp=created,
            order_type="SLM" if otype == "SL-M" else otype,          # the common vocabulary: MARKET / LIMIT / SL / SLM
            product_type=str(row.get("product") or ""),
            price=price,
            trigger_price=trigger,
            rejection_reason=(str(message)[:200] if status == "REJECTED" and isinstance(message, str) and message else None),
            instrument_id=None,                                      # Zerodha's token must never be mistaken for another broker's
        )

    async def get_order_book(self) -> List[OrderRecord]:
        self._require_contract()
        status, data = await self._call("read", "GET", C.ROUTES["orders"])
        rows = self._data(status, data)
        if not isinstance(rows, list):
            raise ZerodhaCapabilityUnavailable("order book response has no list of orders")
        records = [self.parse_order_row(r) for r in rows]
        await self._restore_client_order_ids(records)
        return records

    async def get_order(self, broker_order_id: str) -> Optional[OrderRecord]:
        """GET /orders/:order_id returns the order's HISTORY (a list of states); the last entry is its current state. None only when
        Zerodha says the order does not exist (HTTP 404). Anything unreadable raises."""
        self._require_contract()
        oid = str(broker_order_id or "").strip()
        if not oid:
            raise ZerodhaError("an order id is required")
        status, data = await self._call("read", "GET", C.ROUTES["order_history"].format(order_id=oid))
        if status == 404:
            return None
        rows = self._data(status, data)
        if not isinstance(rows, list) or not rows:
            raise ZerodhaCapabilityUnavailable("order history is empty or not a list")
        if any(not isinstance(r, dict) or str(r.get("order_id")) != oid for r in rows):
            raise ZerodhaCapabilityUnavailable("order history lists a different order")
        record = self.parse_order_row(rows[-1])
        await self._restore_client_order_ids([record])
        return record

    async def find_by_client_reference(self, reference: str) -> Optional[OrderRecord]:
        """The order carrying this `tag` in today's order book (documented: `tag` is returned by GET /orders), or None when no order has
        it. Two orders with the same tag are ambiguous and raise. Only matching rows are parsed, so an unrelated unreadable row cannot
        hide the answer. Read-only."""
        self._require_contract()
        tag = str(reference or "").strip()
        if not tag:
            raise ZerodhaError("a reference is required")
        status, data = await self._call("read", "GET", C.ROUTES["orders"])
        rows = self._data(status, data)
        if not isinstance(rows, list):
            raise ZerodhaCapabilityUnavailable("order book response has no list of orders")

        def carries(row: Any) -> bool:
            if not isinstance(row, dict):
                return False
            if row.get("tag") == tag:
                return True
            tags = row.get("tags")
            return isinstance(tags, list) and tag in tags

        matches = [r for r in rows if carries(r)]
        if not matches:
            return None
        if len(matches) > 1:
            raise ZerodhaCapabilityUnavailable("more than one order carries this tag")
        record = self.parse_order_row(matches[0])
        await self._restore_client_order_ids([record])
        return record

    async def _restore_client_order_ids(self, records: List[OrderRecord]) -> None:
        """Replace Zerodha's tag with the Jettone client_order_id it was derived from (the stale-order sweeper matches TIMEOUT trades on
        that id). An unmapped or unreadable tag is left as is, which simply leaves the order for manual review."""
        for rec in records:
            tag = rec.client_order_id
            if not tag or not tag.startswith(TAG_PREFIX):
                continue
            try:
                mapped = await self.redis.get(self._tag_key(tag))
            except Exception:
                continue
            if isinstance(mapped, bytes):
                mapped = mapped.decode()
            if isinstance(mapped, str) and mapped:
                rec.client_order_id = mapped

    def parse_position_row(self, row: Dict[str, Any]) -> Position:
        if not isinstance(row, dict):
            raise ZerodhaCapabilityUnavailable("position row is not an object")
        qty = _int(row, "quantity")                                           # signed: a short is legitimately negative
        for key in ("tradingsymbol", "product", "exchange"):
            value = row.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ZerodhaCapabilityUnavailable(f"position row has no usable {key}")
        # average_price feeds the exposure check (|quantity| x price): required and positive for an open position; a closed one
        # (quantity 0) contributes nothing, so its price may be absent or null.
        price = _dec(row, "average_price", positive=True, allow_negative=False) if qty != 0 else _opt(row, "average_price")
        return Position(
            symbol=row["tradingsymbol"].strip(), quantity=qty, side="BUY" if qty >= 0 else "SELL", average_price=price,
            account_id=self.account_id, exchange=row["exchange"].strip().upper(), product_type=row["product"].strip().upper(),
            instrument_id=None,        # matched on the exact tradingsymbol: Kite warns tokens are reused for other derivatives
        )

    async def get_positions(self) -> List[Position]:
        self._require_contract()
        status, data = await self._call("read", "GET", C.ROUTES["positions"])
        payload = self._data(status, data)
        rows = payload.get("net") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            # An unexpected shape is "unknown", never "flat".
            raise ZerodhaCapabilityUnavailable("positions response has no net positions list")
        positions = [self.parse_position_row(r) for r in rows]
        if duplicate_position_key(positions) is not None:
            raise ZerodhaCapabilityUnavailable("position book lists an instrument and product more than once")
        return positions

    async def get_positions_or_none(self) -> Optional[List[Position]]:
        """None = the position book could not be established (no session, network, or unexpected response): callers fail closed.
        [] = a documented-shape response listing no positions."""
        try:
            return await self.get_positions()
        except Exception as exc:
            logger.warning(f"[{self.account_id}] Zerodha position book unavailable: {type(exc).__name__}")
            return None

    async def get_funds(self) -> FundsData:
        """available_margin = available.live_balance (documented: "current available balance"), collateral = available.collateral,
        used_margin = utilised.debits. A balance that cannot be read exactly is refused, never read as zero."""
        self._require_contract()
        status, data = await self._call("read", "GET", C.ROUTES["margins"])
        p = self._data(status, data)
        available, utilised = (p.get("available"), p.get("utilised")) if isinstance(p, dict) else (None, None)
        if not isinstance(available, dict) or not isinstance(utilised, dict):
            raise ZerodhaCapabilityUnavailable("margins response has no available / utilised sections")
        return FundsData(
            account_id=self.account_id,
            available_margin=_dec(available, "live_balance"),                  # finite; a negative balance is real
            collateral=_dec(available, "collateral", allow_negative=False),
            used_margin=_dec(utilised, "debits", allow_negative=False),
            updated_at=self._clock(),
        )

    async def aclose(self) -> None:
        await self._http.aclose()
