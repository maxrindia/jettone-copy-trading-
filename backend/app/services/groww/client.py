"""
Groww Trading API client (Child only; disabled by default)
===========================================================
Disabled unless GROWW_ENABLED=true. Every request goes to https://api.groww.in
and carries only that account's own token; nothing here can reach Alice Blue or
Angel One endpoints, and the Master's instrument id is never sent.

All paths, fields and vocabularies come from Groww's official documentation
(see contract.py for the source list and for what is documented as unclear).
Anything unclear fails closed: the account is not copied to, the read raises,
and an order that might have been accepted is never resent.

Sessions: POST /v1/token/api/access with key_type "approval" (the only flow
whose response is documented). Groww requires the owner to approve the API key
on Groww Cloud each day first; without that approval the login is refused and
the error says so. The token is kept encrypted in Redis only until the earlier
of its documented expiry readings. There is no documented logout, so logout
just forgets the session.
"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
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
from app.services.brokers.login_guard import run_guarded_login
from app.services.brokers.strict_numbers import StrictNumberError, need, optional_decimal, strict_decimal, strict_int
from app.services.brokers.position_checks import duplicate_position_key
from app.services.brokers.transport import SendState, is_provably_not_sent
from app.services.groww import contract as C
from app.services.groww.contract import GROWW_CONTRACT, GrowwContract, contract_is_usable
from app.services.groww.instruments import GrowwInstrumentStore, store as default_store

logger = logging.getLogger("groww_client")

IST = timezone(timedelta(hours=5, minutes=30))
GROWW_DAILY_LOGIN_CAP = 120            # token endpoint: 5/s, 30/min, 150 per 24 h - stay under it
MAX_SESSION_SECONDS = 24 * 3600
# order_reference_id -> Jettone client_order_id, kept long enough for the stale-order sweeper.
REFERENCE_MAP_TTL_SECONDS = 3 * 24 * 3600
REFERENCE_PREFIX = "JT"

ORDER_TYPE_MAP = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.SL: "SL",
    OrderType.SL_M: "SL_M",
}
# Jettone product -> Groww product. NRML is refused for equity (CASH) orders.
PRODUCT_MAP = {ProductType.CNC: "CNC", ProductType.MIS: "MIS"}

_OPEN_STATES = frozenset({"NEW", "ACKED", "APPROVED", "OPEN", "MODIFICATION_REQUESTED", "CANCELLATION_REQUESTED"})
_MODIFIABLE_STATES = frozenset({"NEW", "ACKED", "APPROVED", "OPEN", "TRIGGER_PENDING"})
_FILLED_STATES = frozenset({"EXECUTED", "COMPLETED", "DELIVERY_AWAITED"})


class GrowwError(Exception):
    pass


class GrowwAuthError(GrowwError):
    pass


class GrowwCapabilityUnavailable(GrowwError):
    """A response was missing, malformed or outside the documented vocabulary."""


class GrowwRateLimited(GrowwError):
    pass


def order_reference_for(client_order_id: Optional[str]) -> str:
    """Deterministic 20-character alphanumeric order_reference_id (docs: 8-20
    alphanumeric, at most two hyphens) so the same Jettone order always maps to
    the same Groww reference and an ambiguous placement can be looked up."""
    seed = client_order_id or uuid.uuid4().hex
    return REFERENCE_PREFIX + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:18]


def _strict(fn, *args, **kwargs):
    """Run a strict-number helper, turning its (value-free) error into the adapter's typed failure."""
    try:
        return fn(*args, **kwargs)
    except StrictNumberError as exc:
        raise GrowwCapabilityUnavailable(f"Groww response {exc}") from None
    except (ArithmeticError, TypeError, ValueError) as exc:           # overflow and anything exotic: never escapes untyped
        raise GrowwCapabilityUnavailable(f"Groww response has an unreadable number ({type(exc).__name__})") from None


def _dec(row: Any, key: str, **kw) -> Decimal:
    return _strict(lambda: strict_decimal(need(row, key), key, **kw))


def _int(row: Any, key: str, **kw) -> int:
    return _strict(lambda: strict_int(need(row, key), key, **kw))


def _opt(row: Any, key: str, **kw) -> Decimal:
    return _strict(lambda: optional_decimal(row, key, **kw))


def _parse_dt(raw: Any, assume: timezone) -> Optional[datetime]:
    try:
        text = str(raw).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=assume)


def normalize_status(raw: Any, quantity: int, filled: int) -> str:
    """Groww order_status -> OPEN / PARTIAL / TRIGGER_PENDING / COMPLETE / REJECTED /
    CANCELLED (the vocabulary Jettone's fill sync understands). Names that imply a
    fill (EXECUTED, COMPLETED, DELIVERY_AWAITED) are accepted only when the filled
    quantity agrees; anything else raises rather than guessing."""
    s = str(raw or "").strip().upper()
    if s not in C.ORDER_STATUSES and s not in C.EXTRA_DOCUMENTED_STATUSES:
        raise GrowwCapabilityUnavailable(f"order_status '{s}' is not in Groww's documented vocabulary")
    if s in _OPEN_STATES:
        return "PARTIAL" if 0 < filled < quantity else "OPEN"
    if s == "TRIGGER_PENDING":
        return "TRIGGER_PENDING"
    if s in ("REJECTED", "FAILED"):
        return "REJECTED"
    if s == "CANCELLED":
        return "CANCELLED"
    if s in _FILLED_STATES:
        if quantity > 0 and filled == quantity:
            return "COMPLETE"
        if 0 < filled < quantity:
            return "PARTIAL"
        raise GrowwCapabilityUnavailable(f"order_status '{s}' contradicts filled_quantity={filled}/{quantity}")
    raise GrowwCapabilityUnavailable(f"unhandled order_status '{s}'")  # pragma: no cover


class _RateLimiter:
    """Sliding-window limiter for one Groww rate-limit type. Waits up to
    max_wait seconds for a slot, otherwise raises (the caller fails closed)."""

    def __init__(self, windows, max_wait: float = 2.0, now=time.monotonic, sleep=asyncio.sleep):
        self._windows = windows
        self._max_wait = max_wait
        self._now = now
        self._sleep = sleep
        self._stamps: List[float] = []

    async def acquire(self) -> None:
        waited = 0.0
        while True:
            now = self._now()
            horizon = max(w for _, w in self._windows)
            self._stamps = [t for t in self._stamps if now - t < horizon]
            wait = 0.0
            for limit, window in self._windows:
                recent = [t for t in self._stamps if now - t < window]
                if len(recent) >= limit:
                    wait = max(wait, window - (now - recent[0]))
            if wait <= 0:
                self._stamps.append(now)
                return
            if waited + wait > self._max_wait:
                raise GrowwRateLimited("Groww rate limit reached; request not sent")
            await self._sleep(wait)
            waited += wait


class GrowwClient:
    """One instance per Groww account. Credentials arrive already decrypted from
    the credential vault and are held in memory only."""

    # Sessions are created only by an explicit login (the owner approves the key
    # on Groww Cloud first), never by the engine's startup authenticate_all().
    AUTO_LOGIN_ON_STARTUP = False

    def __init__(
        self,
        account_id: str,
        label: str,
        api_key: str,
        api_secret: str,
        redis: Any,
        static_ip: Optional[str],
        static_ip_valid_until: Optional[date] = None,
        http_client: Optional[httpx.AsyncClient] = None,
        proxy_url: Optional[str] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        contract: GrowwContract = GROWW_CONTRACT,
        instruments: Optional[GrowwInstrumentStore] = None,
        sleep=asyncio.sleep,
        monotonic=time.monotonic,
    ):
        self.account_id = str(account_id)
        self.user_id = label.strip()
        self._api_key = api_key
        self._api_secret = api_secret
        self.redis = redis
        self.static_ip = static_ip
        self.static_ip_valid_until = static_ip_valid_until
        self._clock = clock
        self._contract = contract
        self._instruments = instruments if instruments is not None else default_store
        self._monotonic = monotonic
        self._login_lock = asyncio.Lock()
        self._login_generation = 0
        self._orders_limiter = _RateLimiter(C.LIMIT_ORDERS, now=monotonic, sleep=sleep)
        self._reads_limiter = _RateLimiter(C.LIMIT_READS, now=monotonic, sleep=sleep)
        self._token_limiter = _RateLimiter(C.LIMIT_TOKEN, now=monotonic, sleep=sleep)
        if http_client is not None:
            self._http = http_client
        else:
            self._http = httpx.AsyncClient(timeout=10.0, proxy=proxy_url) if proxy_url else httpx.AsyncClient(timeout=10.0)

    def __repr__(self) -> str:  # never expose credentials
        return f"GrowwClient(account_id={self.account_id!r}, label={self.user_id!r})"

    @property
    def session_key(self) -> str:
        return f"groww:session:{self.account_id}"

    # ── static IP ────────────────────────────────────────────────────────
    def _require_static_ip(self) -> None:
        # Groww: order placement must come from a whitelisted static IP. Without a
        # recorded IP - or after it expired - never send a create/modify/cancel.
        if not self.static_ip:
            raise GrowwError("no registered static IP recorded for this Groww account")
        if self.static_ip_valid_until is not None and self._clock().astimezone(IST).date() > self.static_ip_valid_until:
            raise GrowwError(f"static IP expired on {self.static_ip_valid_until.isoformat()}")

    # ── session ──────────────────────────────────────────────────────────
    @staticmethod
    def _json(resp: httpx.Response) -> Dict[str, Any]:
        try:
            body = resp.json()
            return body if isinstance(body, dict) else {}
        except Exception:
            return {}

    def _session_ttl(self, expiry_raw: Any) -> int:
        """Seconds the token may be trusted: the EARLIER reading of the documented
        expiry (timezone unspecified), capped at 24h. Unparseable -> refused."""
        now = self._clock()
        readings = []
        for tz in (IST, timezone.utc):
            dt = _parse_dt(expiry_raw, tz)
            if dt is None:
                raise GrowwAuthError("token response has no readable expiry")
            readings.append((dt - now).total_seconds())
        ttl = int(min(min(readings), MAX_SESSION_SECONDS))
        if ttl <= 0:
            raise GrowwAuthError("token response is already expired")
        return ttl

    async def login(self) -> None:
        """POST /v1/token/api/access (approval flow), through the cross-instance Redis guard: at most one
        token request per account per spacing window across ALL client instances and API requests, a
        daily cap under Groww's documented 150, and fail-closed if Redis is unavailable. Raises
        LoginThrottled (not a broker call) when the guard refuses."""
        generation = self._login_generation
        async with self._login_lock:
            if self._login_generation != generation:
                return  # another caller on this instance completed a login while this one waited
            await run_guarded_login(
                redis=self.redis, broker="groww", account_id=self.account_id, clock=self._clock,
                do_login=self._login_once, session_key=self.session_key, daily_cap=GROWW_DAILY_LOGIN_CAP,
            )
            self._login_generation += 1

    async def _login_once(self) -> None:
        try:
            await self._token_limiter.acquire()
        except GrowwRateLimited as exc:
            raise GrowwAuthError(str(exc)) from exc
        ts = int(self._clock().timestamp())
        checksum = hashlib.sha256((self._api_secret + str(ts)).encode("utf-8")).hexdigest()
        try:
            resp = await self._http.post(
                f"{C.BASE_URL}{C.ROUTES['token']}",
                json={"key_type": "approval", "checksum": checksum, "timestamp": ts},
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            )
        except httpx.RequestError as exc:
            raise GrowwAuthError(f"network error during login: {type(exc).__name__}") from None
        data = self._json(resp)
        if resp.status_code != 200:
            raise GrowwAuthError(
                f"Groww refused the token request (HTTP {resp.status_code}). "
                "Has today's approval been given for this API key on Groww Cloud?"
            )
        body = data.get("payload") if isinstance(data.get("payload"), dict) else data
        token = body.get("token")
        if not isinstance(token, str) or not token:
            raise GrowwAuthError("token response contained no token")
        if body.get("isActive") is not True:
            raise GrowwAuthError("Groww reports the session is not active")
        ttl = self._session_ttl(body.get("expiry"))
        blob = json.dumps({"token": token, "at": self._clock().isoformat()})
        await self.redis.set(self.session_key, encrypt_secret(blob), ex=ttl)
        logger.info(f"[{self.account_id}] Groww session established.")

    async def get_session(self, force_refresh: bool = False) -> str:
        if force_refresh:
            await self.login()
        try:
            raw = await self.redis.get(self.session_key)
        except Exception:
            raise GrowwAuthError("Groww session store unavailable; request not sent") from None
        if not raw:
            raise GrowwAuthError("no active Groww session; log in again")
        return await self._decode_session(raw)

    async def _decode_session(self, raw: Any) -> str:
        """The stored token, or a sanitized GrowwAuthError. A missing/invalid ENCRYPTION KEY is a
        configuration problem, not a corrupt session, so the stored value is left alone then. Any
        other decode failure (undecryptable - corrupt or made with another key - bad JSON, no usable
        token) proves THIS stored session is unreadable: only that exact key, and only if it is still
        unchanged, is removed so the account shows offline until the next explicit login."""
        try:
            validate_encryption_key()
        except EncryptionKeyError:
            raise GrowwAuthError("Groww session cannot be read: the encryption key is not configured") from None
        try:
            text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
            data = json.loads(decrypt_secret(text))
            token = data["token"]
            if not isinstance(token, str) or not token.strip():
                raise ValueError("no usable token")
            return token
        except (EncryptionKeyError, ValueError, KeyError, TypeError, AttributeError):
            await self._discard_unreadable_session(raw)
            raise GrowwAuthError("stored Groww session is unreadable; log in again") from None

    async def _discard_unreadable_session(self, raw: Any) -> None:
        """Remove this account's session key only if it still holds the exact unreadable value
        (a concurrent fresh login must never be erased). Best effort; never raises."""
        try:
            current = await self.redis.get(self.session_key)
            if current == raw:
                await self.redis.delete(self.session_key)
                logger.warning(f"[{self.account_id}] Groww stored session was unreadable and has been discarded.")
        except Exception:
            pass

    async def logout(self) -> None:
        """Groww documents no logout/revocation endpoint: forget the session locally."""
        await self.redis.delete(self.session_key)

    # ── transport ────────────────────────────────────────────────────────
    async def _call(
        self, kind: str, method: str, path: str, *, params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None, state: Optional[SendState] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """Authenticated call. kind: "order" (create/modify/cancel) or "read".
        A rejected token clears the session and raises; it is never retried, so a
        create/modify/cancel is sent at most once."""
        await (self._orders_limiter if kind == "order" else self._reads_limiter).acquire()
        token = await self.get_session()
        headers = {"Accept": "application/json", "Authorization": f"Bearer {token}", "X-API-VERSION": C.API_VERSION}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if state is not None:
            state.sent = True      # from here on a failure may follow a request the broker already accepted
        resp = await self._http.request(method, f"{C.BASE_URL}{path}", params=params, json=body, headers=headers)
        if resp.status_code in (401, 403):
            await self.redis.delete(self.session_key)
            raise GrowwAuthError("Groww session rejected; log in again")
        return resp.status_code, self._json(resp)

    @staticmethod
    def _payload(status_code: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """The `payload` of a SUCCESS response, or GrowwError (FAILURE / non-200).
        The FAILURE body is undocumented, so only the status is reported."""
        if status_code == 200 and data.get("status") == "SUCCESS" and isinstance(data.get("payload"), dict):
            return data["payload"]
        raise GrowwError(f"Groww returned HTTP {status_code} / status={data.get('status')!r}")

    # ── instruments ──────────────────────────────────────────────────────
    async def resolve_instrument(self, exchange: str, symbol: str) -> Tuple[str, str]:
        """(ISIN or '', Groww trading_symbol). Another broker's token is never accepted."""
        inst = self._instruments.resolve(exchange, symbol)
        return inst.isin, inst.trading_symbol

    # ── orders ───────────────────────────────────────────────────────────
    def _result(self, category: OrderResultCategory, req_id: Optional[str], order_id: Optional[str], msg: str,
                raw: Dict[str, Any]) -> OrderResult:
        return OrderResult(category=category, client_order_id=req_id, broker_order_id=order_id, message=msg,
                           raw_response=raw, timestamp=self._clock())

    def build_order_body(
        self, *, symbol: str, exchange: str, side: Any, quantity: int, product_type: Any, order_type: Any,
        price: Decimal, trigger_price: Decimal = Decimal("0"), reference: str,
    ) -> Dict[str, Any]:
        """Validates and maps an order. Raises (nothing sent) on any problem."""
        inst = self._instruments.resolve(exchange, symbol)    # NSE/BSE equity only
        side_v = OrderSide(side).value
        allowed = inst.buy_allowed if side_v == "BUY" else inst.sell_allowed
        if allowed is not True:
            raise GrowwError(f"Groww does not allow {side_v} for {inst.exchange}:{inst.trading_symbol}")
        if quantity <= 0:
            raise GrowwError("quantity must be positive")
        if inst.freeze_quantity and quantity > inst.freeze_quantity:
            raise GrowwError(f"quantity exceeds the exchange freeze quantity ({inst.freeze_quantity}); not split")
        if inst.lot_size > 1 and quantity % inst.lot_size:
            raise GrowwError(f"quantity is not a multiple of the lot size ({inst.lot_size}); not rounded")
        product = PRODUCT_MAP.get(ProductType(product_type))
        if product is None:
            raise GrowwError(f"product {product_type} is not supported for Groww equity orders")
        otype = ORDER_TYPE_MAP.get(OrderType(order_type))
        if otype is None:
            raise GrowwError(f"unmapped order type {order_type}")
        if otype in ("LIMIT", "SL") and price <= 0:
            raise GrowwError(f"{otype} requires a positive price")
        if otype in ("SL", "SL_M") and trigger_price <= 0:
            raise GrowwError(f"{otype} requires a positive trigger price")
        body: Dict[str, Any] = {
            "trading_symbol": inst.trading_symbol, "quantity": int(quantity), "validity": C.VALIDITY_DAY,
            "exchange": inst.exchange, "segment": C.SUPPORTED_SEGMENT, "product": product, "order_type": otype,
            "transaction_type": side_v, "order_reference_id": reference,
        }
        if otype in ("LIMIT", "SL"):
            body["price"] = float(price)
        if otype in ("SL", "SL_M"):
            body["trigger_price"] = float(trigger_price)
        return body

    async def _lookup_by_reference(self, reference: str) -> Optional[str]:
        """After an ambiguous create: the groww_order_id if Groww has the order."""
        try:
            status, data = await self._call(
                "read", "GET", C.ROUTES["order_status_ref"].format(order_reference_id=reference),
                params={"segment": C.SUPPORTED_SEGMENT},
            )
            payload = self._payload(status, data)
        except Exception:
            return None
        oid = payload.get("groww_order_id")
        return str(oid) if oid else None

    @staticmethod
    def _safe_reason(exc: BaseException) -> str:
        """Our own validation messages carry no secrets and are shown; anything else is reduced to its type."""
        if isinstance(exc, (GrowwError, InstrumentResolutionError)):
            return str(exc)
        return f"invalid order ({type(exc).__name__})"

    def _reference_key(self, reference: str) -> str:
        return f"groww:ref:{self.account_id}:{reference}"

    def _ambiguous(self, req: OrderRequest, reference: str, why: str) -> OrderResult:
        """The order may or may not exist at Groww. TIMEOUT is not retried by the pipeline and is
        reconciled by the stale-order sweeper; nothing here may ever resend the create."""
        return self._result(
            OrderResultCategory.TIMEOUT, req.client_order_id, None,
            f"Groww create outcome unknown ({why}); not resent - reconcile at the broker",
            {"order_reference_id": reference},
        )

    @staticmethod
    def _is_definitive_rejection(status: int, data: Dict[str, Any]) -> bool:
        """Only a 4xx, or a documented FAILURE status on a 200, proves Groww did not take the order."""
        return 400 <= status < 500 or (status == 200 and data.get("status") == "FAILURE")

    async def place_order(self, req: OrderRequest) -> OrderResult:
        """Total: never raises. An unexpected error before the request is issued is a clean
        rejection; one after it is an ambiguous TIMEOUT. Either way the pipeline's retry loop
        sees a result, never an exception it could answer by re-sending.
        req.instrument_token (another broker's id) is deliberately ignored."""
        reference = order_reference_for(req.client_order_id)
        state = SendState()
        try:
            return await self._place_order(req, reference, state)
        except Exception as exc:
            logger.error(f"[{self.account_id}] Groww place_order internal error ({type(exc).__name__}); sent={state.sent}")
            if state.sent:
                return self._ambiguous(req, reference, "unexpected error after send")
            return self._result(OrderResultCategory.REJECTED, req.client_order_id, None,
                                f"Not sent: internal error ({type(exc).__name__})", {})

    async def _place_order(self, req: OrderRequest, reference: str, state: SendState) -> OrderResult:
        try:
            self._require_static_ip()
            body = self.build_order_body(
                symbol=req.symbol, exchange=req.exchange, side=req.side, quantity=req.quantity,
                product_type=req.product_type, order_type=req.order_type, price=req.price,
                trigger_price=req.trigger_price, reference=reference,
            )
        except Exception as exc:
            return self._result(OrderResultCategory.REJECTED, req.client_order_id, None,
                                f"Not sent: {self._safe_reason(exc)}", {})

        # Persist reference -> Jettone order id BEFORE sending, so a crash after the send still
        # leaves the sweeper able to match Groww's order book entry. No mapping, no send.
        if req.client_order_id:
            try:
                await self.redis.set(self._reference_key(reference), req.client_order_id, ex=REFERENCE_MAP_TTL_SECONDS)
            except Exception:
                return self._result(OrderResultCategory.REJECTED, req.client_order_id, None,
                                    "Not sent: the order reference could not be recorded", {})

        try:
            status, data = await self._call("order", "POST", C.ROUTES["order_create"], body=body, state=state)
        except (GrowwAuthError, GrowwRateLimited) as exc:
            return self._result(OrderResultCategory.REJECTED, req.client_order_id, None, str(exc), {})
        except httpx.RequestError as exc:
            if is_provably_not_sent(exc):
                # Nothing was transmitted, so a retry cannot create a duplicate.
                return self._result(OrderResultCategory.NETWORK_ERROR, req.client_order_id, None,
                                    f"Groww unreachable ({type(exc).__name__}); order not sent", {})
            return await self._resolve_ambiguous(req, reference, f"{type(exc).__name__} after send")

        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        oid = payload.get("groww_order_id")
        if status == 200 and data.get("status") == "SUCCESS" and isinstance(oid, (str, int)) and not isinstance(oid, bool) and str(oid).strip():
            return self._result(OrderResultCategory.SUCCESS, req.client_order_id, str(oid).strip(),
                                str(payload.get("remark") or "SUCCESS")[:200], data)
        if self._is_definitive_rejection(status, data):
            remark = payload.get("remark")
            msg = f"Groww rejected the order (HTTP {status}, status={data.get('status')!r})"
            if isinstance(remark, str) and remark:
                msg += f": {remark[:200]}"
            return self._result(OrderResultCategory.REJECTED, req.client_order_id, None, msg, data)
        # 5xx, an unexpected 2xx/3xx, SUCCESS without an order id, an unreadable body: the order may exist.
        return await self._resolve_ambiguous(req, reference, f"HTTP {status}, unusable response")

    async def _resolve_ambiguous(self, req: OrderRequest, reference: str, why: str) -> OrderResult:
        """One read-only lookup by our reference: found -> SUCCESS; otherwise TIMEOUT. Never a resend."""
        oid = await self._lookup_by_reference(reference)
        if oid:
            return self._result(OrderResultCategory.SUCCESS, req.client_order_id, oid,
                                "SUCCESS (confirmed by reference lookup)", {"order_reference_id": reference})
        return self._ambiguous(req, reference, why)

    async def cancel_order(self, broker_order_id: str, symbol: str = "", exchange: str = "NSE") -> OrderResult:
        try:
            self._require_static_ip()
            status, data = await self._call(
                "order", "POST", C.ROUTES["order_cancel"],
                body={"segment": C.SUPPORTED_SEGMENT, "groww_order_id": broker_order_id},
            )
        except (GrowwError, httpx.RequestError) as exc:
            return self._result(OrderResultCategory.REJECTED, None, broker_order_id, str(exc) or type(exc).__name__, {})
        ok = status == 200 and data.get("status") == "SUCCESS"
        return self._result(OrderResultCategory.SUCCESS if ok else OrderResultCategory.REJECTED, None, broker_order_id,
                            "SUCCESS" if ok else f"Groww rejected the cancel (HTTP {status})", data)

    async def modify_order(
        self, broker_order_id: str, quantity: int, price: Decimal, trigger_price: Decimal = Decimal("0"),
        order_type: OrderType = OrderType.LIMIT, symbol: str = "", exchange: str = "NSE",
        product_type: Optional[ProductType] = None,
    ) -> OrderResult:
        """Only an untouched, still-pending order is modified: the docs do not say
        whether `quantity` is the total or the remaining size, so a part-filled
        order is refused rather than risk a wrong size."""
        try:
            self._require_static_ip()
            if quantity <= 0:
                raise GrowwError("quantity must be positive")
            otype = ORDER_TYPE_MAP.get(OrderType(order_type))
            if otype is None:
                raise GrowwError(f"unmapped order type {order_type}")
            if otype in ("LIMIT", "SL") and price <= 0:
                raise GrowwError(f"{otype} requires a positive price")
            if otype in ("SL", "SL_M") and trigger_price <= 0:
                raise GrowwError(f"{otype} requires a positive trigger price")
            status, data = await self._call(
                "read", "GET", C.ROUTES["order_detail"].format(groww_order_id=broker_order_id),
                params={"segment": C.SUPPORTED_SEGMENT},
            )
            detail = self._payload(status, data)
            state = str(detail.get("order_status") or "").upper()
            if state not in _MODIFIABLE_STATES:
                raise GrowwError(f"order is {state or 'in an unknown state'}; not modifiable")
            if _int(detail, "filled_quantity", minimum=0) != 0:
                raise GrowwError("order is part-filled; modify quantity semantics are not documented")
            body: Dict[str, Any] = {
                "quantity": int(quantity), "order_type": otype, "segment": C.SUPPORTED_SEGMENT,
                "groww_order_id": broker_order_id,
            }
            if otype in ("LIMIT", "SL"):
                body["price"] = float(price)
            if otype in ("SL", "SL_M"):
                body["trigger_price"] = float(trigger_price)
            status, data = await self._call("order", "POST", C.ROUTES["order_modify"], body=body)
        except (GrowwError, httpx.RequestError, ValueError) as exc:
            return self._result(OrderResultCategory.REJECTED, None, broker_order_id, str(exc) or type(exc).__name__, {})
        ok = status == 200 and data.get("status") == "SUCCESS"
        return self._result(OrderResultCategory.SUCCESS if ok else OrderResultCategory.REJECTED, None, broker_order_id,
                            "SUCCESS" if ok else f"Groww rejected the modify (HTTP {status})", data)

    # ── reads (fail closed on anything undocumented) ─────────────────────
    def _require_contract(self) -> None:
        if not contract_is_usable(self._contract):
            raise GrowwCapabilityUnavailable("Groww response contract is not verified")

    def parse_order_row(self, row: Dict[str, Any]) -> OrderRecord:
        """Every number is strict (see strict_numbers): missing, null, blank, boolean, NaN, Infinity,
        negative or inconsistent values refuse the whole row - they are never read as zero."""
        if not isinstance(row, dict):
            raise GrowwCapabilityUnavailable("order row is not an object")
        raw_id = row.get("groww_order_id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)) or not str(raw_id).strip():
            raise GrowwCapabilityUnavailable("order row has no usable groww_order_id")
        qty = _int(row, "quantity", positive=True)
        filled = _int(row, "filled_quantity", minimum=0, maximum=qty)          # a fill can never exceed the order
        try:
            side = OrderSide(str(row["transaction_type"]).upper())
            status = normalize_status(row["order_status"], qty, filled)
        except (KeyError, ValueError) as exc:
            raise GrowwCapabilityUnavailable(f"order row missing/invalid field: {type(exc).__name__}") from None
        otype = str(row.get("order_type") or "").upper()
        if otype not in C.ORDER_TYPES:
            raise GrowwCapabilityUnavailable("order row has an undocumented order_type")
        price = _opt(row, "price", applicable=otype in ("LIMIT", "SL"))
        trigger = _opt(row, "trigger_price", applicable=otype in ("SL", "SL_M"))
        average = _opt(row, "average_fill_price", applicable=filled > 0)       # a fill must carry its price
        created = _parse_dt(row.get("created_at"), IST) or self._clock()
        return OrderRecord(
            broker_order_id=str(raw_id).strip(),
            client_order_id=(str(row["order_reference_id"]) if row.get("order_reference_id") else None),
            symbol=str(row.get("trading_symbol") or ""),
            exchange=str(row.get("exchange") or ""),
            side=side,
            quantity=qty,
            filled_quantity=filled,
            average_price=average,
            status=status,
            order_timestamp=created,
            order_type="SLM" if otype == "SL_M" else otype,      # the common vocabulary: MARKET / LIMIT / SL / SLM
            product_type=str(row.get("product") or ""),
            price=price,
            trigger_price=trigger,
            rejection_reason=(str(row["remark"])[:200] if status == "REJECTED" and row.get("remark") else None),
            instrument_id=None,
        )

    async def get_order_book(self) -> List[OrderRecord]:
        self._require_contract()
        status, data = await self._call(
            "read", "GET", C.ROUTES["order_list"],
            params={"segment": C.SUPPORTED_SEGMENT, "page_size": C.ORDER_LIST_PAGE_SIZE},
        )
        payload = self._payload(status, data)
        rows = payload.get("order_list")
        if not isinstance(rows, list):
            raise GrowwCapabilityUnavailable("order list response has no order_list array")
        if len(rows) >= C.ORDER_LIST_PAGE_SIZE:
            # The page base of Groww's paging is undocumented: a full page may hide orders.
            raise GrowwCapabilityUnavailable("order list may be truncated (full page); not trusting it")
        records = [self.parse_order_row(r) for r in rows]
        await self._restore_client_order_ids(records)
        return records

    async def _restore_client_order_ids(self, records: List[OrderRecord]) -> None:
        """Replace Groww's order_reference_id with the Jettone client_order_id it was derived from
        (the stale-order sweeper matches TIMEOUT trades on that id). An unmapped or unreadable
        reference is left as is, which simply leaves the order for manual review."""
        for rec in records:
            ref = rec.client_order_id
            if not ref or not ref.startswith(REFERENCE_PREFIX):
                continue
            try:
                mapped = await self.redis.get(self._reference_key(ref))
            except Exception:
                continue
            if isinstance(mapped, bytes):
                mapped = mapped.decode()
            if isinstance(mapped, str) and mapped:
                rec.client_order_id = mapped

    def parse_position_row(self, row: Dict[str, Any]) -> Position:
        if not isinstance(row, dict):
            raise GrowwCapabilityUnavailable("position row is not an object")
        qty = _int(row, "quantity")                                           # signed: a short is legitimately negative
        for key in ("trading_symbol", "product", "exchange"):
            value = row.get(key)
            if not isinstance(value, str) or not value.strip():
                raise GrowwCapabilityUnavailable(f"position row has no usable {key}")
        # net_price feeds the exposure check (|quantity| x price): required and positive for an open
        # position; a closed one (quantity 0) contributes nothing, so its price may be absent or null.
        price = _dec(row, "net_price", positive=True, allow_negative=False) if qty != 0 else _opt(row, "net_price")
        isin = row.get("symbol_isin")
        return Position(
            symbol=row["trading_symbol"], quantity=qty, side="BUY" if qty >= 0 else "SELL",
            average_price=price, account_id=self.account_id,
            exchange=row["exchange"], product_type=row["product"],
            instrument_id=(str(isin) if isin else None),
        )

    async def get_positions(self) -> List[Position]:
        self._require_contract()
        status, data = await self._call("read", "GET", C.ROUTES["positions"], params={"segment": C.SUPPORTED_SEGMENT})
        payload = self._payload(status, data)
        rows = payload.get("positions")
        if not isinstance(rows, list):
            # The empty-positions shape is not documented; an unexpected shape is "unknown", never "flat".
            raise GrowwCapabilityUnavailable("positions response has no positions array")
        positions: List[Position] = []
        for r in rows:
            if not isinstance(r, dict):                                       # validate before any field access
                raise GrowwCapabilityUnavailable("position row is not an object")
            if str(r.get("segment") or C.SUPPORTED_SEGMENT).upper() == C.SUPPORTED_SEGMENT:
                positions.append(self.parse_position_row(r))
        if duplicate_position_key(positions) is not None:
            # One row per instrument and product is expected; two cannot be told apart, so the book is unknown.
            raise GrowwCapabilityUnavailable("position book lists an instrument and product more than once")
        return positions

    async def get_positions_or_none(self) -> Optional[List[Position]]:
        """None = the position book could not be established (no session, network,
        or unexpected response): callers fail closed. [] = a documented-shape
        response listing no positions."""
        try:
            return await self.get_positions()
        except Exception as exc:
            logger.warning(f"[{self.account_id}] Groww position book unavailable: {type(exc).__name__}")
            return None

    async def get_funds(self) -> FundsData:
        self._require_contract()
        status, data = await self._call("read", "GET", C.ROUTES["margin"])
        p = self._payload(status, data)
        return FundsData(
            account_id=self.account_id,
            available_margin=_dec(p, "clear_cash"),                           # finite; a negative balance is real
            collateral=_dec(p, "collateral_available", allow_negative=False),
            used_margin=_dec(p, "net_margin_used", allow_negative=False),
            updated_at=self._clock(),
        )

    async def aclose(self) -> None:
        await self._http.aclose()
