import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
import httpx
import redis.asyncio as aioredis

from app.core.database import AsyncSessionLocal
from app.models.accounts import Account
from app.services.alice_blue.exceptions import (
    AuthenticationError,
    IPNotWhitelistedError,
    RateLimitExceededError,
    SessionExpiredError,
)
from app.services.alice_blue.instruments import InstrumentResolutionError, resolve_instrument_id, resolve_instrument_info
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

logger = logging.getLogger("alice_blue_client")


def order_book_row_id(item: Dict[str, Any]) -> str:
    """The broker order id of one raw /orders/book row - the single
    definition shared by get_order_book() and the Master-switch preflight
    snapshot, so both always identify the same order the same way."""
    return str(item.get("brokerOrderId", item.get("Nstordno", item.get("orderId", ""))))


class AliceBlueClient:
    """
    Reusable async client for Alice Blue ANT/A3 API.
    One instance per account (Master or Follower).
    """

    A3_BASE_URL = "https://a3.aliceblueonline.com"
    SESSION_TTL_SECONDS = 18 * 3600  # 18 hours or until market day ends

    def __init__(
        self,
        account_id: str,
        user_id: str,
        api_secret: str,
        redis: aioredis.Redis,
        http_client: Optional[httpx.AsyncClient] = None,
        proxy_url: Optional[str] = None,
    ):
        self.account_id = str(account_id)
        self.user_id = user_id
        self.api_secret = api_secret
        self.redis = redis
        self.proxy_url = proxy_url
        if http_client:
            self._http = http_client
        elif proxy_url:
            self._http = httpx.AsyncClient(timeout=10.0, proxy=proxy_url)
            # Log only the host (not credentials) for safety
            safe_host = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
            logger.info(f"[{self.account_id}] REST client using proxy: {safe_host}")
        else:
            self._http = httpx.AsyncClient(timeout=10.0)

    def _make_checksum(self, *parts: str) -> str:
        raw = "".join(parts)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def authenticate_with_auth_code(self, auth_code: str) -> str:
        """
        Official Alice Blue A3 OAuth session exchange (getUserDetails).
        Calculates SHA-256(userId + authCode + apiSecret), posts to
        https://a3.aliceblueonline.com/open-api/od/v1/vendor/getUserDetails,
        and caches the returned userSession JWT in Redis.
        """
        clean_auth_code = auth_code.strip()
        if not clean_auth_code:
            raise AuthenticationError(self.account_id, "Missing authCode for Alice Blue session exchange.")

        logger.info(f"[{self.account_id}] Exchanging authCode with Alice Blue A3 getUserDetails for user {self.user_id}...")

        checksum = self._make_checksum(self.user_id, clean_auth_code, self.api_secret)

        try:
            resp = await self._http.post(
                f"{self.A3_BASE_URL}/open-api/od/v1/vendor/getUserDetails",
                json={"checkSum": checksum},
                headers={"Content-Type": "application/json"},
            )
        except httpx.RequestError as exc:
            raise AuthenticationError(self.account_id, f"Network error during getUserDetails request: {exc}")

        try:
            data = resp.json()
        except Exception:
            raise AuthenticationError(
                self.account_id,
                f"Invalid response from Alice Blue A3 (HTTP {resp.status_code}): {resp.text}",
            )

        # Check for IP restriction / whitelist error
        error_msg = str(data.get("emsg", data.get("message", ""))).lower()
        if "ip" in error_msg and ("whitelist" in error_msg or "not allowed" in error_msg or "not registered" in error_msg):
            raise IPNotWhitelistedError(self.account_id, "VPS_OR_PROXY_IP", data.get("emsg", "IP restriction"))

        stat = str(data.get("stat", "")).lower()
        if stat != "ok":
            emsg = data.get("emsg") or data.get("message") or "Authentication failed (stat not Ok)"
            raise AuthenticationError(self.account_id, f"Alice Blue login failed: {emsg}")

        user_session = data.get("userSession")
        if not user_session:
            raise AuthenticationError(self.account_id, "Alice Blue returned Ok but no userSession token was present.")

        # Cache valid JWT token in Redis
        cache_key = f"alice:session:{self.account_id}"
        await self.redis.set(cache_key, user_session, ex=self.SESSION_TTL_SECONDS)
        logger.info(f"[{self.account_id}] A3 session verified and cached in Redis.")
        return user_session

    async def get_session(self, force_refresh: bool = False) -> str:
        """
        Retrieves active cached session token from Redis.
        Raises AuthenticationError if session token is absent or expired, requiring OAuth re-login.
        """
        cache_key = f"alice:session:{self.account_id}"
        if not force_refresh:
            cached_token = await self.redis.get(cache_key)
            if cached_token:
                return cached_token

        raise AuthenticationError(
            self.account_id,
            "No active Alice Blue session found in Redis. Please complete OAuth login via Re-login.",
        )

    async def _get_headers(self, force_refresh: bool = False) -> Dict[str, str]:
        token = await self.get_session(force_refresh=force_refresh)
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _is_auth_failure(resp: httpx.Response) -> bool:
        if resp.status_code == 401:
            return True
        body = str(getattr(resp, "text", "")).lower()
        return resp.status_code != 200 and ("session" in body and ("expired" in body or "invalid" in body))

    async def _invalidate_db_session(self):
        """Marks account session_valid = False in database and removes Redis key."""
        try:
            acc_uuid = uuid.UUID(str(self.account_id))
            async with AsyncSessionLocal() as db:
                acc = await db.get(Account, acc_uuid)
                if acc and acc.session_valid:
                    acc.session_valid = False
                    await db.commit()
            await self.redis.delete(f"alice:session:{self.account_id}")
        except Exception as e:
            logger.error(f"[{self.account_id}] Failed to set offline state in DB: {e}")

    async def _request_with_reauth(self, method: str, path: str, **kwargs) -> httpx.Response:
        """
        Executes a REST call; on a session-expiry response it re-authenticates
        once (force_refresh) and retries exactly once. A second auth failure
        raises SessionExpiredError instead of being swallowed as empty data.
        """
        headers = await self._get_headers()
        resp = await self._http.request(method, f"{self.A3_BASE_URL}{path}", headers=headers, **kwargs)
        if not self._is_auth_failure(resp):
            return resp

        logger.warning(f"[{self.account_id}] Session rejected for {path}; forcing re-authentication.")
        await self.redis.delete(f"alice:funds:{self.account_id}")
        try:
            await self.get_session(force_refresh=True)
        except AuthenticationError:
            await self._invalidate_db_session()
            raise SessionExpiredError(
                self.account_id, f"Re-authentication failed after session rejection on {path}"
            )

        headers = await self._get_headers()
        resp = await self._http.request(method, f"{self.A3_BASE_URL}{path}", headers=headers, **kwargs)
        if self._is_auth_failure(resp):
            await self._invalidate_db_session()
            raise SessionExpiredError(
                self.account_id, f"Session still rejected after forced re-auth on {path}"
            )
        return resp

    async def get_funds(self, max_cache_age_sec: int = 10) -> FundsData:
        """
        Fetches RMS funds/limits from Alice Blue A3 /open-api/od/v1/limits.
        Caches in Redis to avoid hitting broker rate limits.
        """
        cache_key = f"alice:funds:{self.account_id}"
        cached = await self.redis.get(cache_key)
        if cached:
            try:
                parsed = json.loads(cached)
                return FundsData(
                    account_id=self.account_id,
                    available_margin=Decimal(str(parsed["available_margin"])),
                    collateral=Decimal(str(parsed["collateral"])),
                    used_margin=Decimal(str(parsed["used_margin"])),
                    updated_at=datetime.fromisoformat(parsed["updated_at"]),
                )
            except Exception as e:
                logger.warning(f"Failed to parse cached funds: {e}")

        try:
            resp = await self._request_with_reauth("GET", "/open-api/od/v1/limits")
        except httpx.RequestError as exc:
            logger.error(f"[{self.account_id}] Network error fetching limits: {exc}")
            raise

        data = resp.json() if resp.status_code == 200 else {}
        
        # Parse A3 limits format (result array) with safe fallbacks
        res_list = data.get("result", [])
        item = res_list[0] if (isinstance(res_list, list) and len(res_list) > 0) else data

        trading_limit = Decimal(str(item.get("tradingLimit", "0")))
        opening_cash = Decimal(str(item.get("openingCashLimit", "0")))
        collateral = Decimal(str(item.get("collateralMargin", item.get("collateral", "0"))))
        used = Decimal(str(item.get("utilizedMargin", item.get("utilisedAmount", "0"))))

        if trading_limit > Decimal("0"):
            available = trading_limit
        elif opening_cash > Decimal("0"):
            available = max(Decimal("0"), opening_cash + collateral - used)
        else:
            available = Decimal(str(item.get("net", item.get("availableMargin", "0"))))

        now = datetime.now(timezone.utc)

        funds = FundsData(
            account_id=self.account_id,
            available_margin=available,
            collateral=collateral,
            used_margin=used,
            updated_at=now,
        )

        # Store in Redis cache
        cache_payload = {
            "available_margin": str(available),
            "collateral": str(collateral),
            "used_margin": str(used),
            "updated_at": now.isoformat(),
        }
        await self.redis.set(cache_key, json.dumps(cache_payload), ex=max_cache_age_sec)
        return funds

    async def create_ws_session(self) -> None:
        """
        Creates a WebSocket session on Alice Blue A3 servers.
        Required to be called before connecting to NorenWS.
        """
        payload = {
            "source": "API",
            "userId": self.user_id,
        }
        try:
            resp = await self._request_with_reauth(
                "POST",
                "/open-api/od/v1/profile/createWsSess",
                json=payload,
            )
            if resp.status_code != 200:
                logger.warning(f"[{self.account_id}] createWsSess returned {resp.status_code}: {resp.text}")
            else:
                logger.info(f"[{self.account_id}] A3 WebSocket session created successfully.")
        except Exception as exc:
            logger.error(f"[{self.account_id}] Failed to create A3 WS session: {exc}")

    def _parse_position_rows(self, raw_list: List[Dict[str, Any]]) -> List[Position]:
        """Shared row->Position mapping for get_positions()/get_positions_or_none() -
        kept in one place so the two never drift apart. Zero-quantity rows are
        dropped (an already-flattened/closed position is not a "position")."""
        positions: List[Position] = []
        for item in raw_list:
            net_qty = int(item.get("netQuantity", item.get("Netqty", item.get("netQty", 0))))
            if net_qty == 0:
                continue

            symbol = str(item.get("tradingSymbol", item.get("Trsym", item.get("symbol", ""))))
            avg_price = Decimal(str(item.get("netAveragePrice", item.get("Netavgprc", item.get("avgPrice", "0")))))
            side: Literal["BUY", "SELL"] = "BUY" if net_qty > 0 else "SELL"
            exchange = str(item.get("exchange", item.get("Exchange", "NSE")))
            product = str(item.get("product", item.get("Pcode", item.get("productType", "MIS"))))
            instrument_id_raw = item.get("instrumentId", item.get("Token", item.get("token")))
            instrument_id = str(instrument_id_raw).strip() if instrument_id_raw not in (None, "") else None

            positions.append(
                Position(
                    symbol=symbol,
                    quantity=net_qty,
                    side=side,
                    average_price=avg_price,
                    account_id=self.account_id,
                    exchange=exchange,
                    product_type=product,
                    instrument_id=instrument_id,
                )
            )

        return positions

    async def get_positions(self) -> List[Position]:
        """
        Fetches open position book from Alice Blue A3 /open-api/od/v1/positions.
        Returns [] on ANY failure (network, non-200, bad body) - this is the
        long-standing, still-relied-upon contract for this method's existing
        callers (get_open_exposure, reconciliation, the /broker/positions API
        route), which treat "no positions" and "couldn't check" the same way.

        Callers that need to tell those two cases apart (see
        get_available_long's fail-closed requirement) must use
        get_positions_or_none() instead - it shares this exact same fetch/
        parse path, just surfaces failure as None rather than swallowing it.
        """
        raw_list = await self.get_positions_raw()
        if raw_list is None:
            return []
        return self._parse_position_rows(raw_list)

    async def get_positions_or_none(self) -> Optional[List[Position]]:
        """
        Same fetch and parsing as get_positions(), but returns None when the
        broker call itself failed (network error, non-200, unparseable body)
        instead of silently collapsing that into an empty list. A genuinely
        successful call with zero open positions still returns [] - only an
        actual fetch failure returns None. Exists so a safety check (like
        get_available_long) can fail closed on "couldn't verify" without
        conflating it with "verified: follower holds nothing here".
        """
        raw_list = await self.get_positions_raw()
        if raw_list is None:
            return None
        return self._parse_position_rows(raw_list)

    async def get_positions_detail(self, include_closed: bool = False) -> List[Dict[str, Any]]:
        """
        Fetches position book from Alice Blue A3 /open-api/od/v1/positions.
        By default, only active open positions (quantity != 0) are returned.
        """
        try:
            resp = await self._request_with_reauth("GET", "/open-api/od/v1/positions")
        except httpx.RequestError as exc:
            logger.error(f"[{self.account_id}] Network error fetching positions: {exc}")
            return []

        if resp.status_code != 200:
            logger.warning(f"[{self.account_id}] Position book returned status {resp.status_code}: {resp.text}")
            return []

        data = resp.json()
        raw_list = data.get("result", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        if not isinstance(raw_list, list):
            raw_list = []

        detailed: List[Dict[str, Any]] = []
        idx = 1
        for item in raw_list:
            net_qty = int(item.get("netQuantity", item.get("Netqty", item.get("netQty", 0))))
            if not include_closed and net_qty == 0:
                continue

            symbol = str(item.get("tradingSymbol", item.get("Trsym", item.get("symbol", ""))))
            if not symbol:
                continue

            avg_price = float(item.get("netAveragePrice", item.get("Netavgprc", item.get("buyAvgPrc", item.get("avgPrice", 0.0)))) or 0.0)
            ltp = float(item.get("ltp", item.get("Ltp", item.get("lastPrice", 0.0))) or 0.0)
            pnl = float(item.get("m2m", item.get("pnl", item.get("rpnl", item.get("urPnl", 0.0)))) or 0.0)

            if net_qty > 0:
                side = "BUY"
            elif net_qty < 0:
                side = "SELL"
            else:
                side = "CLOSED"

            exchange = str(item.get("exchange", item.get("Exchange", "NSE")))
            product = str(item.get("product", item.get("Pcode", item.get("productType", "INTRADAY"))))
            if product.upper() in ("MIS", "INTRADAY"):
                product_display = "INTRADAY"
            elif product.upper() in ("CNC", "DELIVERY"):
                product_display = "DELIVERY"
            elif product.upper() in ("NRML", "CARRYFORWARD", "MARGIN"):
                product_display = "CARRYFORWARD"
            else:
                product_display = product

            detailed.append({
                "id": str(idx),
                "symbol": symbol,
                "product": product_display,
                "side": side,
                "quantity": net_qty,
                "pnl": pnl,
                "ltp": ltp,
                "avg_price": avg_price,
                "exchange": exchange,
                "raw_product": product,
                "can_square_off": net_qty != 0,
            })
            idx += 1

        return detailed

    def _resolve_square_off_order(
        self,
        symbol: str,
        exchange: str,
        quantity: int,
        side: str,
        product: str = "MIS",
        client_order_id: Optional[str] = None,
    ) -> Tuple[OrderRequest, OrderSide, int]:
        """
        Pure computation, no network call: builds the OrderRequest a
        square-off would submit - the opposite side of the given position,
        absolute quantity - without placing it.

        Exposed separately (not just inlined in square_off_position) so a
        caller that needs to persist an Order record BEFORE contacting the
        broker can get the exact target_side/quantity/client_order_id that
        will actually be submitted, instead of independently re-deriving
        the BUY/SELL flip (which would risk drifting out of sync with this
        method's logic).
        """
        # NOTE: previously this was `side.upper() == "BUY" or quantity > 0`,
        # which always evaluated True (SquareOffRequest.quantity is
        # constrained gt=0) and therefore always resolved to SELL regardless
        # of the actual position side - squaring off a SELL/short position
        # would have placed another SELL instead of a closing BUY. Fixed as
        # part of Issue #6 Finding 3 since it's required for square-off
        # persistence to record the correct side.
        target_side = OrderSide.BUY if side.upper() == "SELL" else OrderSide.SELL
        abs_qty = abs(quantity)

        p_str = str(product).upper().strip()
        if p_str in ("CNC", "LONGTERM", "DELIVERY"):
            prod_type = ProductType.CNC
        elif p_str in ("NRML", "MTF", "CARRYFORWARD", "MARGIN"):
            prod_type = ProductType.NRML
        else:
            prod_type = ProductType.MIS

        req = OrderRequest(
            symbol=symbol.upper(),
            exchange=exchange.upper(),
            side=target_side,
            quantity=abs_qty,
            product_type=prod_type,
            order_type=OrderType.MARKET,
            price=Decimal("0"),
            client_order_id=client_order_id or f"SQ_OFF_{uuid.uuid4().hex[:10]}",
        )
        return req, target_side, abs_qty

    async def square_off_position(
        self,
        symbol: str,
        exchange: str,
        quantity: int,
        side: str,
        product: str = "MIS",
        client_order_id: Optional[str] = None,
    ) -> Tuple[OrderResult, OrderSide, int]:
        """
        Squares off an open position by placing an opposite MARKET order.

        Returns (result, target_side, abs_qty): the actual broker-facing
        side and quantity that were submitted (see _resolve_square_off_order),
        so a caller can persist an accurate Order record without
        independently re-deriving the BUY/SELL flip. client_order_id can be
        supplied by the caller so a pre-write done before this call and the
        order actually submitted here share the same identity; if omitted,
        one is generated internally exactly as before.
        """
        req, target_side, abs_qty = self._resolve_square_off_order(
            symbol, exchange, quantity, side, product, client_order_id
        )

        if abs_qty == 0:
            return (
                OrderResult(
                    category=OrderResultCategory.REJECTED,
                    client_order_id=req.client_order_id,
                    broker_order_id=None,
                    message="Cannot square off closed position (quantity is 0).",
                    raw_response={},
                    timestamp=datetime.now(timezone.utc),
                ),
                target_side,
                0,
            )

        result = await self.place_order(req)
        return result, target_side, abs_qty

    async def place_order(self, order: OrderRequest) -> OrderResult:
        """
        Submits an order to Alice Blue A3 /open-api/od/v1/orders/placeorder.
        Categorizes failures strictly into SUCCESS, REJECTED, TIMEOUT, NETWORK_ERROR, UNKNOWN.
        """
        # Validate order inputs before constructing payload
        if not order.quantity or order.quantity <= 0:
            return OrderResult(
                category=OrderResultCategory.REJECTED,
                client_order_id=order.client_order_id,
                broker_order_id=None,
                message="Invalid order quantity: must be positive integer",
                raw_response={},
                timestamp=datetime.now(timezone.utc),
            )

        if order.order_type == OrderType.LIMIT and (not order.price or order.price <= 0):
            return OrderResult(
                category=OrderResultCategory.REJECTED,
                client_order_id=order.client_order_id,
                broker_order_id=None,
                message="Invalid limit price for LIMIT order: must be greater than 0",
                raw_response={},
                timestamp=datetime.now(timezone.utc),
            )

        # Price type & order type mapping according to Alice Blue A3 Open API specification:
        # priceType: 'MKT' for Market, 'L' for Limit, 'SL' for Stop Loss, 'SL-M' for Stop Loss Market
        # orderType: 'MARKET', 'LIMIT', 'SL', 'SLM' (EC930 requirement)
        raw_otype = getattr(order.order_type, "value", str(order.order_type))
        o_val = str(raw_otype).upper()
        price_type_a3 = {
            "MARKET": "MKT",
            "MKT": "MKT",
            "LIMIT": "L",
            "L": "L",
            "SL": "SL",
            "SL-M": "SL-M",
            "SLM": "SL-M",
        }.get(o_val, "MKT")

        order_type_a3 = {
            "MARKET": "MARKET",
            "MKT": "MARKET",
            "LIMIT": "LIMIT",
            "L": "LIMIT",
            "SL": "SL",
            "SL-M": "SLM",
            "SLM": "SLM",
        }.get(o_val, "MARKET")

        # ProductType is declared as `class ProductType(str, Enum)`, so
        # `isinstance(order.product_type, str)` is True for EVERY enum
        # member - a plain isinstance(str) check here cannot tell "genuine
        # ProductType enum value" apart from "raw string". The production
        # bug this fixes: that ambiguity used to route every ProductType
        # enum instance through a second, string-only reclassification
        # block that computed str(ProductType.CNC) == "ProductType.CNC"
        # (Python's default Enum str(), not the bare value "CNC"), matched
        # no key in that block's own lookup table, and silently fell back
        # to "INTRADAY" - turning every CNC/NRML follower order into an
        # intraday one at the broker regardless of what the master traded
        # (confirmed live: master CNC BUY -> follower order executed as
        # INTRADAY). Checking isinstance(..., ProductType) FIRST resolves
        # the ambiguity correctly; every current production caller always
        # passes a real ProductType value, so this branch is authoritative
        # for them. The plain-string branch is preserved for compatibility
        # with any caller that genuinely passes a bare string instead.
        product_type_map = {
            ProductType.MIS: "INTRADAY",
            ProductType.CNC: "LONGTERM",
            ProductType.NRML: "MTF",
        }
        if isinstance(order.product_type, ProductType):
            product_str = product_type_map.get(order.product_type, "INTRADAY")
        else:
            p_val = str(order.product_type).upper()
            product_str = {
                "MIS": "INTRADAY",
                "INTRADAY": "INTRADAY",
                "CNC": "LONGTERM",
                "LONGTERM": "LONGTERM",
                "DELIVERY": "LONGTERM",
                "NRML": "MTF",
                "MTF": "MTF",
            }.get(p_val, "INTRADAY")

        trans_type_full = "BUY" if order.side in (OrderSide.BUY, "BUY") else "SELL"
        trans_type_short = "B" if order.side in (OrderSide.BUY, "BUY") else "S"

        # Price string formatting per order type
        if order.order_type == OrderType.MARKET:
            price_str = "0"
        else:
            if order.price:
                price_str = f"{order.price:.2f}"
            else:
                price_str = "0"

        # Trigger price formatting
        if order.order_type in (OrderType.SL, OrderType.SL_M):
            trigger_price_str = f"{order.trigger_price:.2f}" if order.trigger_price else "0"
        else:
            trigger_price_str = "0"

        qty_str = str(order.quantity)

        # Resolve numeric instrumentId / token & official tradingSymbol (e.g. RELIANCE-EQ, YESBANK-EQ).
        # Fails closed (OrderResult REJECTED, never a broker call) rather than
        # letting an unresolvable instrument reach Alice Blue - see
        # InstrumentResolutionError's docstring for the production incident
        # this specifically prevents (instrumentId="NIFTY" sent as if numeric).
        try:
            inst_id, official_trading_sym = await resolve_instrument_info(
                redis=self.redis,
                exchange=order.exchange,
                symbol=order.symbol,
                provided_token=order.instrument_token,
            )
        except InstrumentResolutionError as exc:
            logger.warning(f"[{self.account_id}] {exc}")
            return OrderResult(
                category=OrderResultCategory.REJECTED,
                client_order_id=order.client_order_id,
                broker_order_id=None,
                message=str(exc),
                raw_response={},
                timestamp=datetime.now(timezone.utc),
            )

        # Defense in depth: even a resolved value must actually be numeric -
        # Alice Blue's own instrumentId contract requires it, and this catches
        # any future bad entry in KNOWN_INSTRUMENTS or a corrupted Redis
        # cache value the same way, without trusting any one resolution path
        # in isolation.
        if not inst_id.isdigit():
            message = (
                f"Resolved instrumentId '{inst_id}' for {order.exchange}:{order.symbol} is not numeric; "
                "refusing to send it to Alice Blue."
            )
            logger.warning(f"[{self.account_id}] {message}")
            return OrderResult(
                category=OrderResultCategory.REJECTED,
                client_order_id=order.client_order_id,
                broker_order_id=None,
                message=message,
                raw_response={},
                timestamp=datetime.now(timezone.utc),
            )

        payload = [
            {
                "exchange": order.exchange,
                "token": inst_id,
                "instrumentId": inst_id,
                "instrument_id": inst_id,
                "instrumentToken": inst_id,
                "tradingSymbol": official_trading_sym,
                "trading_symbol": official_trading_sym,
                "ltp": "",
                "qty": qty_str,
                "quantity": qty_str,
                "orderQuantity": qty_str,
                "order_quantity": qty_str,
                "price": price_str,
                "product": product_str,
                "productType": product_str,
                "product_type": product_str,
                "priceType": price_type_a3,
                "price_type": price_type_a3,
                "orderType": order_type_a3,
                "order_type": order_type_a3,
                "transactionType": trans_type_full,
                "transType": trans_type_full,
                "orderComplexity": "regular",
                "order_complexity": "regular",
                "completexity": "regular",
                "ret": "DAY",
                "validity": "DAY",
                # Confirmed field name (matches both the live order-book
                # response and modify_order's own documented contract) -
                # "triggerPrice" is not a field Alice Blue's A3 API
                # recognizes for placeorder.
                "slTriggerPrice": trigger_price_str,
                "disclosedQty": "0",
                "discqty": "0",
                "mktProtection": "",
                "target": "",
                "stopLoss": "",
                "trailingStopLoss": "",
            }
        ]

        logger.info(
            f"[{self.account_id}] Submitting order to Alice Blue A3: exchange={order.exchange}, "
            f"symbol={order.symbol}, instrument_id={inst_id}, side={trans_type_full}, "
            f"quantity={qty_str}, order_type={price_type_a3}, price={price_str}"
        )
        logger.info(f"[{self.account_id}] Alice Blue A3 Order Endpoint: {self.A3_BASE_URL}/open-api/od/v1/orders/placeorder")
        logger.info(f"[{self.account_id}] Alice Blue A3 Outgoing Payload (non-secret): {json.dumps(payload)}")

        now = datetime.now(timezone.utc)
        try:
            resp = await self._request_with_reauth(
                "POST",
                "/open-api/od/v1/orders/placeorder",
                json=payload,
                timeout=5.0,  # Strict timeout
            )
        except httpx.TimeoutException:
            logger.error(f"[{self.account_id}] Timeout executing order {order.client_order_id}")
            return OrderResult(
                category=OrderResultCategory.TIMEOUT,
                client_order_id=order.client_order_id,
                broker_order_id=None,
                message="Order submission timed out without confirmation",
                raw_response={},
                timestamp=now,
            )
        except httpx.RequestError as exc:
            logger.error(f"[{self.account_id}] Network error executing order {order.client_order_id}: {exc}")
            return OrderResult(
                category=OrderResultCategory.NETWORK_ERROR,
                client_order_id=order.client_order_id,
                broker_order_id=None,
                message=f"Network error communicating with broker: {exc}",
                raw_response={},
                timestamp=now,
            )

        raw_data = resp.json() if resp.status_code in (200, 400, 422) else {}
        logger.info(f"[{self.account_id}] Alice Blue A3 Response HTTP {resp.status_code}: {raw_data}")

        item = raw_data[0] if (isinstance(raw_data, list) and len(raw_data) > 0) else (raw_data if isinstance(raw_data, dict) else {})
        stat = str(item.get("status", "")).lower()

        # Extract orderNo / brokerOrderId & nested result status from A3 result array or top-level field
        broker_order_id = None
        res_status = ""
        res_msg = ""
        result_list = item.get("result", [])
        if isinstance(result_list, list) and len(result_list) > 0:
            res_item = result_list[0]
            if isinstance(res_item, dict):
                broker_order_id = str(res_item.get("brokerOrderId", res_item.get("orderNo", res_item.get("orderID", "")))).strip() or None
                res_status = str(res_item.get("status", "")).strip()
                res_msg = str(res_item.get("message", "")).strip()

        if not broker_order_id:
            broker_order_id = str(item.get("brokerOrderId", item.get("orderNo", item.get("orderID", item.get("Nstordno", ""))))).strip() or None

        top_msg = item.get("message") or item.get("emsg") or item.get("infoMessage") or ""
        error_msg = res_msg or top_msg

        # Detect error codes (e.g. EC965, EC941) anywhere in status, message or payload body
        raw_str = str(raw_data)
        has_ec_in_body = "EC" in raw_str or "EC" in res_status or "EC" in error_msg
        is_result_error = bool(
            has_ec_in_body or
            (res_status and (res_status.upper().startswith("EC") or res_status.upper() in ("REJECTED", "ERROR", "FAILED"))) or
            (stat and stat in ("rejected", "error", "failed", "not_ok"))
        )

        logger.info(
            f"[{self.account_id}] Alice Blue Parsed Outcome: broker_order_id={broker_order_id}, "
            f"status_code={res_status}, is_error={is_result_error}, error_msg='{error_msg}'"
        )

        if resp.status_code == 200 and broker_order_id and not is_result_error:
            logger.info(f"[{self.account_id}] Order ACCEPTED by Alice Blue: broker_order_id={broker_order_id}")
            return OrderResult(
                category=OrderResultCategory.SUCCESS,
                client_order_id=order.client_order_id,
                broker_order_id=broker_order_id,
                message="Order placed successfully with broker",
                raw_response=raw_data,
                timestamp=now,
            )
        else:
            clean_msg = error_msg.strip() if error_msg else "Invalid order parameters"
            logger.warning(
                f"[{self.account_id}] Order REJECTED by Alice Blue (HTTP {resp.status_code}): "
                f"error='{clean_msg}', raw_status='{res_status}'"
            )
            return OrderResult(
                category=OrderResultCategory.REJECTED,
                client_order_id=order.client_order_id,
                broker_order_id=None,
                message=f"Order rejected by Alice Blue: {clean_msg}",
                raw_response=raw_data,
                timestamp=now,
            )

    async def get_order_book(self) -> List[OrderRecord]:
        """
        Fetches full order book from Alice Blue A3 /open-api/od/v1/orders/book.
        Raises SessionExpiredError if re-authentication fails.
        """
        try:
            resp = await self._request_with_reauth("GET", "/open-api/od/v1/orders/book")
        except httpx.RequestError as exc:
            logger.error(f"[{self.account_id}] Network error fetching order book: {exc}")
            return []

        if resp.status_code != 200:
            return []

        data = resp.json()
        raw_list = data.get("result", []) if isinstance(data, dict) else (data if isinstance(data, list) else [])
        if not isinstance(raw_list, list):
            raw_list = []

        orders: List[OrderRecord] = []
        for item in raw_list:
            # Parse order timestamp if present (e.g. "12:53:26 11-07-2026")
            order_time_raw = item.get("orderTime")
            ts = datetime.now(timezone.utc)
            if order_time_raw:
                try:
                    ts = datetime.strptime(str(order_time_raw).strip(), "%H:%M:%S %d-%m-%Y").replace(tzinfo=timezone.utc)
                except Exception:
                    pass

            # Map transactionType / Trantype
            trans_type = str(item.get("transactionType", item.get("Trantype", ""))).upper()
            side = OrderSide.BUY if trans_type in ("BUY", "B") else OrderSide.SELL

            # Normalize orderStatus / Status
            raw_status = str(item.get("orderStatus", item.get("Status", "UNKNOWN"))).upper()
            status_map = {
                "COMPLETED": "COMPLETE",
                "COMPLETE": "COMPLETE",
                "OPEN": "OPEN",
                "PENDING": "OPEN",
                "REJECTED": "REJECTED",
                "CANCELLED": "CANCELLED",
                "CANCELED": "CANCELLED",
                "PARTIALLY_FILLED": "PARTIAL",
                "PARTIAL": "PARTIAL",
            }
            norm_status = status_map.get(raw_status, raw_status)

            order_type_raw = str(item.get("orderType", item.get("prctyp", "MARKET"))).upper()
            if order_type_raw in ("L", "LIMIT"):
                norm_otype = "LIMIT"
            elif order_type_raw in ("SL", "STOPLOSS"):
                norm_otype = "SL"
            elif order_type_raw in ("SL-M", "SLM"):
                norm_otype = "SLM"
            else:
                norm_otype = "MARKET"

            price_raw = Decimal(str(item.get("price", item.get("prc", "0")) or "0"))
            # Confirmed in production, 2026-09-29: a native SL order's order
            # book row reports its trigger price under "slTriggerPrice", not
            # "triggerPrice"/"trgprc" - those two keys are kept as fallbacks
            # for whichever (if any) response shape actually uses them.
            trigger_price_raw = Decimal(str(
                item.get("slTriggerPrice", item.get("triggerPrice", item.get("trgprc", "0"))) or "0"
            ))
            rej_reason = item.get("rejectionReason") or item.get("rejreason") or None

            # Alice Blue's order book carries its own numeric instrument
            # identity directly (e.g. "74056") even for NFO options whose
            # tradingSymbol is reported as just the bare underlying name
            # (e.g. "NIFTY" - confirmed live: formattedInstrumentName
            # "NIFTY 29th SEP 23900 CE" alongside tradingSymbol "NIFTY").
            # Capturing it here lets callers skip the ambiguous
            # symbol-based NFO resolution entirely instead of re-guessing
            # which of many same-named contracts was actually traded.
            instrument_id_raw = item.get("instrumentId", item.get("Token", item.get("token")))
            instrument_id = str(instrument_id_raw).strip() if instrument_id_raw not in (None, "") else None

            orders.append(
                OrderRecord(
                    broker_order_id=order_book_row_id(item),
                    client_order_id=item.get("orderTag") or None,
                    symbol=str(item.get("tradingSymbol", item.get("Trsym", item.get("symbol", "")))),
                    exchange=str(item.get("exchange", item.get("Exchange", "NSE"))),
                    side=side,
                    quantity=int(item.get("quantity", item.get("Qty", 0))),
                    filled_quantity=int(item.get("filledQuantity", item.get("Fillshares", 0))),
                    average_price=Decimal(str(item.get("averageTradedPrice", item.get("Avgprc", "0")))),
                    status=norm_status,
                    order_timestamp=ts,
                    order_type=norm_otype,
                    product_type=str(item.get("product", item.get("pCode", "MIS"))),
                    price=price_raw,
                    trigger_price=trigger_price_raw,
                    rejection_reason=rej_reason,
                    instrument_id=instrument_id,
                )
            )

        return orders

    async def get_order_book_raw(self) -> Optional[List[Dict[str, Any]]]:
        """
        Raw /orders/book rows for read-only reporting (P&L). Unlike
        get_order_book, a failure returns None - never an empty list that
        would be indistinguishable from "no orders today".
        """
        return await self._get_result_rows("/open-api/od/v1/orders/book")

    async def get_positions_raw(self) -> Optional[List[Dict[str, Any]]]:
        """Raw /positions rows (closed rows included) for read-only reporting (P&L).
        None on failure."""
        return await self._get_result_rows("/open-api/od/v1/positions")

    async def _get_result_rows(self, path: str) -> Optional[List[Dict[str, Any]]]:
        try:
            resp = await self._request_with_reauth("GET", path)
        except httpx.RequestError as exc:
            logger.error(f"[{self.account_id}] Network error reading {path}: {exc}")
            return None

        if resp.status_code != 200:
            logger.warning(f"[{self.account_id}] {path} returned HTTP {resp.status_code}")
            return None

        try:
            data = resp.json()
        except ValueError:
            logger.warning(f"[{self.account_id}] {path} returned a non-JSON body")
            return None

        rows = data.get("result") if isinstance(data, dict) else data
        if rows is None and self._is_empty_book_response(path, data):
            # Alice Blue answers an EMPTY book with HTTP 200 + a status code
            # and result=null (confirmed live 2026-10-13: orders/book ->
            # EC916 "No orders found for this user.", positions -> EC920 "No
            # positions found for this user."). That is a successful read of
            # an empty book, not a failed fetch. Matched by exact code AND
            # message per endpoint - any other non-list body still fails closed.
            return []
        if not isinstance(rows, list):
            # TEMPORARY DIAGNOSTIC (2026-09-24 investigation into "returned no
            # result list" causing POSITION_FETCH_FAILED SELL blocks) - logs
            # ONLY shape metadata, never the raw body, so the exact response
            # Alice Blue sends for this account/path can be characterized
            # before any fix is chosen. Remove once the fix ships. Does not
            # alter the return value below in any way.
            logger.warning(
                f"[{self.account_id}] [DIAG] {path} non-list result: {self._describe_result_shape(data, rows)}"
            )
            logger.warning(f"[{self.account_id}] {path} returned no result list")
            return None
        return [row for row in rows if isinstance(row, dict)]

    # endpoint path -> (status code, message fragment) of Alice Blue's
    # "nothing to show" answer. Verified against the live A3 API.
    _EMPTY_BOOK_RESPONSES = {
        "/open-api/od/v1/orders/book": ("EC916", "no orders found"),
        "/open-api/od/v1/positions": ("EC920", "no positions found"),
    }

    @classmethod
    def _is_empty_book_response(cls, path: str, data: Any) -> bool:
        expected = cls._EMPTY_BOOK_RESPONSES.get(path)
        if expected is None or not isinstance(data, dict):
            return False
        code, fragment = expected
        return (
            str(data.get("status", "")).strip().upper() == code
            and fragment in str(data.get("message", "")).lower()
        )

    _SENSITIVE_KEY_MARKERS = (
        "token", "auth", "password", "secret", "cookie", "session",
        "key", "credential", "otp", "pin",
    )

    @classmethod
    def _redact_diagnostic_value(cls, value: Any, max_len: int = 200) -> str:
        """
        Safe-for-logs representation of an arbitrary JSON value: redacts any
        dict key that looks credential-shaped (by name, not content - this
        function never inspects a value to guess if IT is a secret, since
        that's unreliable; it redacts based on the KEY it's stored under,
        which is deterministic and auditable), then truncates. Used only by
        the temporary diagnostic block above - never on a normal code path.
        """
        if value is None:
            return "None"
        if isinstance(value, bool):
            return str(value)
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, dict):
            redacted = {
                k: ("***REDACTED***" if any(m in str(k).lower() for m in cls._SENSITIVE_KEY_MARKERS) else v)
                for k, v in value.items()
            }
            try:
                text = json.dumps(redacted, default=str)
            except Exception:
                text = str(redacted)
        else:
            text = str(value)
        if len(text) > max_len:
            text = text[:max_len] + f"...<truncated, total {len(str(value))} chars>"
        return text

    @classmethod
    def _describe_result_shape(cls, data: Any, rows: Any) -> str:
        """Metadata-first description of why `rows` wasn't a list - types
        and key names only, plus a redacted/truncated peek at the actual
        `result` value. Never logs headers, auth tokens, or the full body."""
        top_level_type = type(data).__name__
        if isinstance(data, dict):
            top_level_keys = sorted(data.keys())
            result_present = "result" in data
        else:
            top_level_keys = "N/A (top-level JSON is not an object)"
            result_present = False
        result_is_none = rows is None
        result_type = type(rows).__name__
        return (
            f"top_level_type={top_level_type}, top_level_keys={top_level_keys}, "
            f"result_present={result_present}, result_is_none={result_is_none}, "
            f"result_type={result_type}, result_value={cls._redact_diagnostic_value(rows)}"
        )

    async def cancel_order(
        self,
        broker_order_id: str,
        symbol: str = "",
        exchange: str = "NSE",
    ) -> OrderResult:
        """
        Cancels an open or pending order on Alice Blue A3
        POST /open-api/od/v1/orders/cancel.

        The previous path (/orders/cancelorder) is not a POST route on A3 and
        answered every live cancel with HTTP 400 "HTTP method POST is not
        supported by this URL", which is why Master CANCEL never reached the
        child in production. Endpoint and payload now follow the documented
        contract exactly: brokerOrderId is the only documented field, so the
        old orderNumber/nestOrderNumber/tradingSymbol aliases are not sent.

        `symbol` and `exchange` remain in the signature because callers pass
        them and they are used for logging context; they are not part of the
        documented request body.
        """
        now = datetime.now(timezone.utc)
        payload = {"brokerOrderId": broker_order_id}

        try:
            resp = await self._request_with_reauth("POST", "/open-api/od/v1/orders/cancel", json=payload)
        except httpx.RequestError as exc:
            logger.error(f"[{self.account_id}] Network error cancelling order {broker_order_id}: {exc}")
            return OrderResult(
                category=OrderResultCategory.NETWORK_ERROR,
                client_order_id=None,
                broker_order_id=broker_order_id,
                message=f"Network error: {exc}",
                raw_response={},
                timestamp=now,
            )

        raw_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        stat = str(raw_data.get("stat", raw_data.get("status", ""))).upper()

        if resp.status_code == 200 and stat in ("OK", "SUCCESS"):
            return OrderResult(
                category=OrderResultCategory.SUCCESS,
                client_order_id=None,
                broker_order_id=broker_order_id,
                message=f"Order {broker_order_id} cancelled successfully",
                raw_response=raw_data,
                timestamp=now,
            )
        else:
            msg = raw_data.get("emsg") or raw_data.get("message") or resp.text
            return OrderResult(
                category=OrderResultCategory.REJECTED,
                client_order_id=None,
                broker_order_id=broker_order_id,
                message=f"Cancel rejected: {msg}",
                raw_response=raw_data,
                timestamp=now,
            )

    async def modify_order(
        self,
        broker_order_id: str,
        quantity: int,
        price: Decimal,
        trigger_price: Decimal = Decimal("0"),
        order_type: OrderType = OrderType.LIMIT,
        symbol: str = "",
        exchange: str = "NSE",
        product_type: ProductType = ProductType.MIS,
    ) -> OrderResult:
        """
        Modifies a pending order on Alice Blue A3
        POST /open-api/od/v1/orders/modify.

        Same path correction as cancel_order: /orders/modifyorder is not a
        POST route on A3. This path had never actually executed in
        production (the poller never dispatched ORDER_MODIFIED, so
        modify_order was never called once), so its failure was latent
        rather than observed.

        Payload follows the documented A3 modify contract. The previous body
        carried eight non-documented legacy aliases - orderNumber,
        nestOrderNumber, tradingSymbol, exchange, newQuantity, newPrice,
        triggerPrice and priceType - none of which appear in the contract;
        they are no longer sent. For a normal LIMIT modification the
        documented required fields are exactly brokerOrderId, quantity,
        orderType and price.

        slTriggerPrice is documented as conditional, so it is included only
        when a trigger price is actually set. Jettone's own modification
        path never passes one (handle_master_modification calls this with
        broker_order_id/quantity/price/symbol/exchange only, leaving
        trigger_price at 0), so it is not sent for our LIMIT use case; the
        branch exists so an SL modification would use the documented field
        name rather than the old `triggerPrice` alias. Every other
        documented field is optional and deliberately omitted.

        `symbol`, `exchange` and `product_type` remain in the signature
        because callers pass them and they give log context; they are not
        part of the documented request body.
        """
        now = datetime.now(timezone.utc)
        # Documented A3 modify envelope: a single JSON object. A JSON-array
        # envelope was tried in production (commit 62536d4, 2026-09-09) on
        # the theory that modify behaved like placeorder, which does wrap
        # its body in an array; it does not - HTTP 400 with a completely
        # empty body (a deserialization failure). Reverted to the object
        # shape the same day.
        #
        # That same day, the object shape (with only the four fields the
        # A3 docs mark Required - brokerOrderId, quantity, orderType,
        # price) returned EC914 "Invalid parameter: 'Request parameter'
        # cannot be empty or null" despite carrying everything documented
        # as required. Adding the three fields below - validity,
        # disclosedQuantity, trailingSLAmount, all values already proven
        # via place_order's own working payload - resolved it (commit
        # 668c47a, 2026-09-09). Confirmed still correct: numerous
        # successful production modifies since, unrelated to any SL
        # feature (e.g. master order 26092800035805's price modification,
        # 2026-09-28).
        # Native SL/SL-M modification support: previously every non-LIMIT
        # order_type collapsed to "MARKET" here (the child was NEVER
        # actually SL/SL-M before this - see the immediate-SL-replication
        # feature). "SLM" matches the same broker-side spelling
        # place_order already uses for OrderType.SL_M.
        order_type_a3 = {
            OrderType.LIMIT: "LIMIT",
            OrderType.MARKET: "MARKET",
            OrderType.SL: "SL",
            OrderType.SL_M: "SLM",
        }.get(order_type, "LIMIT")
        payload: Dict[str, Any] = {
            "brokerOrderId": broker_order_id,
            "quantity": quantity,
            "orderType": order_type_a3,
            "price": f"{price:.2f}" if price > 0 else "0",
            "validity": "DAY",
            "disclosedQuantity": "0",
            "trailingSLAmount": "",
        }
        if trigger_price > 0:
            payload["slTriggerPrice"] = f"{trigger_price:.2f}"

        # Mirrors place_order's "non-secret" logging: this body carries only
        # order parameters - no tokens, secrets, passwords or session ids
        # (auth travels in headers, which are deliberately NOT logged).
        logger.info(f"[{self.account_id}] A3 Modify Endpoint: POST {self.A3_BASE_URL}/open-api/od/v1/orders/modify")
        logger.info(f"[{self.account_id}] A3 Modify Outgoing Payload (non-secret): {json.dumps(payload)}")

        try:
            resp = await self._request_with_reauth("POST", "/open-api/od/v1/orders/modify", json=payload)
        except httpx.RequestError as exc:
            logger.error(f"[{self.account_id}] Network error modifying order {broker_order_id}: {exc}")
            return OrderResult(
                category=OrderResultCategory.NETWORK_ERROR,
                client_order_id=None,
                broker_order_id=broker_order_id,
                message=f"Network error: {exc}",
                raw_response={},
                timestamp=now,
            )

        raw_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        stat = str(raw_data.get("stat", raw_data.get("status", ""))).upper()

        logger.info(f"[{self.account_id}] A3 Modify Response HTTP {resp.status_code} (stat={stat!r}): {raw_data}")

        if resp.status_code == 200 and stat in ("OK", "SUCCESS"):
            return OrderResult(
                category=OrderResultCategory.SUCCESS,
                client_order_id=None,
                broker_order_id=broker_order_id,
                message=f"Order {broker_order_id} modified successfully",
                raw_response=raw_data,
                timestamp=now,
            )
        else:
            msg = raw_data.get("emsg") or raw_data.get("message") or resp.text
            return OrderResult(
                category=OrderResultCategory.REJECTED,
                client_order_id=None,
                broker_order_id=broker_order_id,
                message=f"Modify rejected: {msg}",
                raw_response=raw_data,
                timestamp=now,
            )

