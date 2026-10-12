import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncIterator, Callable, Dict, Optional
import base64
import socket
import ssl
from urllib.parse import urlparse
import websockets
from websockets.frames import Close
import hashlib

from app.core.database import AsyncSessionLocal
from app.models.accounts import Account
from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.exceptions import AuthenticationError
from app.services.alice_blue.types import OrderUpdateEvent

logger = logging.getLogger("alice_blue_ws")


async def _connect_ws_via_proxy(ws_url: str, proxy_url: str, **ws_kwargs):
    """
    Establishes an HTTP CONNECT tunnel through an HTTPS/HTTP proxy (e.g. StaticIP.in)
    and passes the tunneled socket to websockets.connect().
    """
    target = urlparse(ws_url)
    target_host = target.hostname
    target_port = target.port or (443 if target.scheme == "wss" else 80)

    proxy = urlparse(proxy_url)
    proxy_host = proxy.hostname
    proxy_port = proxy.port or (443 if proxy.scheme in ("https", "wss") else 80)

    # Step 1: Establish TCP/TLS connection to the proxy server
    if proxy.scheme == "https":
        ssl_ctx_proxy = ssl.create_default_context()
        reader, writer = await asyncio.open_connection(proxy_host, proxy_port, ssl=ssl_ctx_proxy)
    else:
        reader, writer = await asyncio.open_connection(proxy_host, proxy_port)

    # Step 2: Send HTTP CONNECT request
    req_lines = [
        f"CONNECT {target_host}:{target_port} HTTP/1.1",
        f"Host: {target_host}:{target_port}",
    ]
    if proxy.username and proxy.password:
        auth_str = f"{proxy.username}:{proxy.password}"
        b64_auth = base64.b64encode(auth_str.encode()).decode()
        req_lines.append(f"Proxy-Authorization: Basic {b64_auth}")
    req_lines.append("\r\n")

    writer.write("\r\n".join(req_lines).encode())
    await writer.drain()

    # Step 3: Verify HTTP 200 Connection Established
    resp_data = await reader.readuntil(b"\r\n\r\n")
    status_line = resp_data.split(b"\r\n")[0].decode()
    if " 200 " not in status_line:
        writer.close()
        await writer.wait_closed()
        raise ConnectionError(f"Proxy CONNECT failed: {status_line}")

    # Step 4: Extract socket & wrap with TLS for target WSS
    sock = writer.get_extra_info("socket")
    if target.scheme == "wss":
        ssl_ctx_target = ssl.create_default_context()
        sock = ssl_ctx_target.wrap_socket(sock, server_hostname=target_host)

    # Step 5: Delegate to websockets
    return await websockets.connect(ws_url, sock=sock, **ws_kwargs)



class AliceBlueWebSocketClient:
    """
    WebSocket client for Alice Blue NorenWS order status updates.
    Handles authentication, 60s heartbeat, exponential backoff reconnection,
    and REST reconciliation poll on reconnect.
    """

    WS_URL = "wss://ws1.aliceblueonline.com/NorenWS/"
    HEARTBEAT_INTERVAL_SEC = 30  # Send ping every 30s to prevent 60s idle timeout
    RECONNECT_DELAYS = [1, 2, 4, 8, 16, 30]  # Exponential backoff, capped at 30s

    def __init__(self, client: AliceBlueClient):
        self.client = client
        self.account_id = client.account_id
        self.user_id = client.user_id
        self._running = False
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self.on_order_update: Optional[Callable[[OrderUpdateEvent], Any]] = None

    async def _invalidate_session(self, reason: str = "Policy Violation / Unauthorized"):
        """Marks account session_valid = False in PostgreSQL and deletes cached Redis token."""
        logger.warning(f"[{self.account_id}] Invalidating session: {reason}")
        try:
            acc_uuid = uuid.UUID(str(self.account_id))
            async with AsyncSessionLocal() as db:
                acc = await db.get(Account, acc_uuid)
                if acc and acc.session_valid:
                    acc.session_valid = False
                    await db.commit()
        except ValueError:
            pass
        except Exception as inv_err:
            logger.error(f"[{self.account_id}] Error setting offline state in DB: {inv_err}")

        try:
            r = self.client.redis
            if asyncio.iscoroutine(r):
                r = await r
            if r:
                await r.delete(f"alice:session:{self.account_id}")
        except Exception as r_err:
            logger.error(f"[{self.account_id}] Error clearing Redis token on invalidation: {r_err}")

    async def _send_heartbeat(self):
        """Sends mandatory heartbeat ping {"t": "h"} every 60 seconds."""
        while self._running:
            try:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL_SEC)
                if self._ws and self._ws.open:
                    await self._ws.send(json.dumps({"t": "h"}))
                    logger.debug(f"[{self.account_id}] WS Heartbeat ping sent.")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[{self.account_id}] Failed to send WS heartbeat: {e}")

    def _normalize_event(self, raw: Dict[str, Any]) -> Optional[OrderUpdateEvent]:
        """Normalizes raw NorenWS JSON payload into standardized OrderUpdateEvent."""
        # Check if message is an order update packet
        t = raw.get("t")
        if t not in ("om", "o"):  # Order message type
            return None

        status_raw = str(raw.get("status", raw.get("Status", ""))).upper()
        if not status_raw:
            return None

        # Normalize status
        status_map = {
            "COMPLETE": "COMPLETE",
            "COMPLETED": "COMPLETE",
            "OPEN": "OPEN",
            "TRIGGER_PENDING": "OPEN",
            "PENDING": "OPEN",
            "REJECTED": "REJECTED",
            "CANCELLED": "CANCELLED",
            "PARTIALLY_FILLED": "PARTIAL",
            "PARTIAL": "PARTIAL",
        }
        status = status_map.get(status_raw, "OPEN")

        side = "BUY" if str(raw.get("trantype", raw.get("Trantype", ""))).upper() in ("BUY", "B") else "SELL"
        qty = int(raw.get("qty", raw.get("Qty", 0)))
        filled_qty = int(raw.get("fillshares", raw.get("Fillshares", 0)))
        fill_price = Decimal(str(raw.get("avgprc", raw.get("Avgprc", "0")) or "0"))

        prctyp_raw = str(raw.get("prctyp", raw.get("priceType", raw.get("orderType", "MKT")))).upper()
        if prctyp_raw in ("L", "LIMIT"):
            order_type = "LIMIT"
        elif prctyp_raw in ("SL", "STOPLOSS"):
            order_type = "SL"
        elif prctyp_raw in ("SL-M", "SLM"):
            order_type = "SLM"
        else:
            order_type = "MARKET"

        price = Decimal(str(raw.get("prc", raw.get("price", "0")) or "0"))
        trigger_price = Decimal(str(raw.get("trgprc", raw.get("triggerPrice", "0")) or "0"))
        rej_reason = raw.get("rejreason") or raw.get("rejectionReason") or None
        exchange = str(raw.get("exch", raw.get("exchange", "NSE"))).upper()

        return OrderUpdateEvent(
            broker_order_id=str(raw.get("nOrdNo", raw.get("Nstordno", ""))),
            client_order_id=raw.get("orderTag") or raw.get("remarks") or None,
            symbol=str(raw.get("tsym", raw.get("Trsym", ""))),
            side=side,
            quantity=qty,
            filled_quantity=filled_qty,
            fill_price=fill_price,
            status=status,
            timestamp=datetime.now(timezone.utc),
            account_id=self.account_id,
            product_type=str(raw.get("pCode", raw.get("Pcode", "MIS"))),
            order_type=order_type,
            price=price,
            trigger_price=trigger_price,
            rejection_reason=rej_reason,
            exchange=exchange,
        )

    @staticmethod
    def _orderbook_to_event(record) -> Optional[OrderUpdateEvent]:
        """Maps a REST OrderRecord into an OrderUpdateEvent for back-fill."""
        status_map = {
            "COMPLETE": "COMPLETE",
            "COMPLETED": "COMPLETE",
            "OPEN": "OPEN",
            "PENDING": "OPEN",
            "REJECTED": "REJECTED",
            "CANCELLED": "CANCELLED",
            "PARTIALLY_FILLED": "PARTIAL",
            "PARTIAL": "PARTIAL",
        }
        status = status_map.get(str(record.status or "").upper())
        if not status:
            return None
        return OrderUpdateEvent(
            broker_order_id=record.broker_order_id,
            client_order_id=record.client_order_id,
            symbol=record.symbol,
            side="BUY" if str(getattr(record.side, "value", record.side)).upper() in ("BUY", "ORDERSIDE.BUY") else "SELL",
            quantity=record.quantity,
            filled_quantity=record.filled_quantity,
            fill_price=record.average_price,
            status=status,
            timestamp=record.order_timestamp or datetime.now(timezone.utc),
            account_id="",  # set below by caller
            product_type=getattr(record, "product_type", "MIS"),
            order_type=getattr(record, "order_type", "MARKET"),
            price=getattr(record, "price", Decimal("0")),
            trigger_price=getattr(record, "trigger_price", Decimal("0")),
            rejection_reason=getattr(record, "rejection_reason", None),
            exchange=getattr(record, "exchange", "NSE"),
        )

    async def _backfill_missed_fills(self) -> int:
        """
        RETIRED - superseded by MasterOrderPoller, and unsafe to keep.

        This used to pull the REST order book on connect/reconnect and push
        every row through on_order_update before resuming the live stream,
        on the stated assumption that "the detector's incremental-fill dedup
        makes replays idempotent". That assumption only holds for orders
        MasterTradeDetector.seed_state() already knows about, and seed_state
        seeds exclusively from persisted Order rows in OPEN/PARTIAL status
        (see engine_loop.py). An order that is already COMPLETE - or that was
        never persisted at all, which is precisely the case for every
        Mobile/Web-originated master order before MasterOrderPoller existed -
        is invisible to seeding. The detector then sees its full cumulative
        fill as brand-new incremental quantity and emits a replication event,
        so a restart could fire real follower orders for master activity that
        happened hours earlier.

        app/services/reliability/master_order_poller.py now owns master
        order-book ingestion end to end, with the baseline-seed rule this
        path never had: orders present at startup are persisted but
        permanently excluded from replication. It also runs continuously
        rather than only at connect time, so nothing is lost by retiring
        this - the first poll covers exactly what a reconnect backfill used
        to cover, and covers it safely.

        Kept as an explicit no-op (rather than deleted outright) so the two
        call sites in connect_and_listen below remain readable as the
        deliberate decision they are, and so any future caller gets the
        reasoning instead of a NameError. It deliberately makes no broker
        call at all - the point is that no order-book replay reaches the
        copy pipeline from this path any more.
        """
        logger.debug(
            f"[{self.account_id}] Backfill is retired; MasterOrderPoller owns master "
            "order-book ingestion (see this method's docstring)."
        )
        return 0

    async def connect_and_listen(self) -> AsyncIterator[OrderUpdateEvent]:
        """
        Connects to the WebSocket feed with auto-reconnect and exponential backoff.
        Yields normalized OrderUpdateEvent instances.
        """
        self._running = True
        reconnect_idx = 0

        # Issue #5: back-fill once before the very first connection this
        # run, so fills/modifications/cancellations that happened while
        # this process was down are actually discovered. Every later
        # reconnect already back-fills (see the except branch below) -
        # this covers the one case that path never reached: a clean
        # restart where the WS connects successfully on the first try, so
        # the except branch (and its backfill) never runs at all.
        logger.info(f"[{self.account_id}] Performing startup REST order-book backfill...")
        await self._backfill_missed_fills()

        while self._running:
            try:
                session_token = await self.client.get_session()
                
                # A3 requires creating the WS session via REST before connecting
                await self.client.create_ws_session()
                
                proxy_url = getattr(self.client, "proxy_url", None)
                if proxy_url:
                    safe_host = proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url
                    logger.info(f"[{self.account_id}] Connecting to Alice Blue WebSocket via proxy ({safe_host})...")
                    ws_ctx = _connect_ws_via_proxy(self.WS_URL, proxy_url, ping_interval=None)
                else:
                    logger.info(f"[{self.account_id}] Connecting to Alice Blue WebSocket: {self.WS_URL}...")
                    ws_ctx = websockets.connect(self.WS_URL, ping_interval=None)

                async with ws_ctx as ws:
                    self._ws = ws
                    reconnect_idx = 0  # Reset backoff on successful connect

                    # Step 1: Send Authentication payload (NorenWS requires double SHA-256 of the session token)
                    first_hash = hashlib.sha256(session_token.encode('utf-8')).hexdigest()
                    susertoken = hashlib.sha256(first_hash.encode('utf-8')).hexdigest()
                    
                    auth_msg = {
                        "susertoken": susertoken,
                        "t": "c",
                        "actid": f"{self.user_id}_API",
                        "uid": f"{self.user_id}_API",
                        "source": "API",
                    }
                    await ws.send(json.dumps(auth_msg))

                    # Step 1b: Wait for auth acknowledgment from server before subscribing
                    auth_not_ok_resp = None
                    try:
                        auth_resp_raw = await asyncio.wait_for(ws.recv(), timeout=10)
                        auth_resp = json.loads(auth_resp_raw)
                        auth_status = auth_resp.get("s", auth_resp.get("stat", ""))
                        logger.info(f"[{self.account_id}] WS auth response: {auth_resp}")
                        if str(auth_status).upper() not in ("OK", ""):
                            logger.warning(f"[{self.account_id}] WS auth NOT OK: {auth_resp}")
                            auth_not_ok_resp = auth_resp
                    except asyncio.TimeoutError:
                        logger.warning(f"[{self.account_id}] WS auth response timeout (10s), proceeding anyway...")
                    except Exception as auth_read_err:
                        logger.warning(f"[{self.account_id}] WS auth response read error: {auth_read_err}")

                    if auth_not_ok_resp is not None:
                        # Treat a non-OK auth ack exactly like the 1008
                        # policy-violation bad-session case below: don't
                        # trust/keep using this connection (it may be
                        # silently unauthenticated), so raise the same
                        # exception type/code that branch already handles -
                        # this reuses the existing invalidate-session +
                        # backoff-reconnect mechanism instead of proceeding
                        # to subscribe on a possibly-unauthenticated socket.
                        raise websockets.ConnectionClosed(
                            Close(1008, f"WS auth not OK: {auth_not_ok_resp}"), None
                        )

                    # Step 2: Subscribe to Order Feed
                    subscribe_msg = {"t": "o", "actid": f"{self.user_id}_API"}
                    await ws.send(json.dumps(subscribe_msg))
                    logger.info(f"[{self.account_id}] Subscribed to order updates.")

                    # Step 3: Send immediate heartbeat, then start periodic heartbeat task
                    await ws.send(json.dumps({"t": "h"}))
                    logger.debug(f"[{self.account_id}] Initial WS heartbeat sent.")
                    self._heartbeat_task = asyncio.create_task(self._send_heartbeat())

                    # Step 4: Stream incoming messages
                    async for raw_str in ws:
                        try:
                            msg = json.loads(raw_str)
                            t_val = msg.get("t")
                            if t_val != "h":  # Don't clutter logs with heartbeat responses
                                logger.info(f"[{self.account_id}] WS recv raw: {raw_str}")
                            event = self._normalize_event(msg)
                            if event:
                                logger.info(f"[{self.account_id}] WS Order Event Detected: {event}")
                                if self.on_order_update:
                                    result = self.on_order_update(event)
                                    if asyncio.iscoroutine(result):
                                        await result
                                yield event
                        except json.JSONDecodeError:
                            continue

            except (websockets.ConnectionClosed, websockets.WebSocketException, OSError) as exc:
                if not self._running:
                    break

                delay = self.RECONNECT_DELAYS[min(reconnect_idx, len(self.RECONNECT_DELAYS) - 1)]
                reconnect_idx += 1

                # Cancel previous heartbeat
                if self._heartbeat_task and not self._heartbeat_task.done():
                    self._heartbeat_task.cancel()

                # If connection was closed due to 1008 policy violation (expired/invalid session)
                is_policy_violation = False
                if isinstance(exc, websockets.ConnectionClosed) and exc.code == 1008:
                    is_policy_violation = True
                    logger.warning(
                        f"[{self.account_id}] Alice Blue WebSocket session rejected (Policy Violation 1008). Invalidating session..."
                    )
                    await self._invalidate_session("WebSocket 1008 Policy Violation")
                    await asyncio.sleep(20)
                else:
                    logger.warning(
                        f"[{self.account_id}] WS disconnected ({exc}). Reconnecting in {delay}s with REST reconciliation..."
                    )
                    await asyncio.sleep(delay)

                if not is_policy_violation:
                    logger.info(f"[{self.account_id}] Performing REST orderbook back-fill before resuming WS stream...")
                    await self._backfill_missed_fills()

            except asyncio.CancelledError:
                break
            except AuthenticationError as auth_err:
                logger.warning(f"[{self.account_id}] Waiting for active Alice Blue web session: {auth_err}")
                await self._invalidate_session(f"AuthenticationError: {auth_err}")
                await asyncio.sleep(20)
            except Exception as e:
                logger.error(f"[{self.account_id}] Unexpected WS error: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def close(self):
        """Cleanly closes WebSocket connection and stops background tasks."""
        self._running = False
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        if self._ws and self._ws.open:
            await self._ws.close()
        logger.info(f"[{self.account_id}] WS client closed.")

