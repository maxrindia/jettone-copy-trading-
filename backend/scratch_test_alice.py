import asyncio
import hashlib
import json
import logging
import ssl
import sys
from urllib.parse import urlparse
import httpx
import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("alice_test")

A3_BASE_URL = "https://a3.aliceblueonline.com"
WS_URL = "wss://ws1.aliceblueonline.com/NorenWS/"

async def _connect_ws_via_proxy(ws_url: str, proxy_url: str, **ws_kwargs):
    target = urlparse(ws_url)
    target_host = target.hostname
    target_port = target.port or (443 if target.scheme == "wss" else 80)

    proxy = urlparse(proxy_url)
    proxy_host = proxy.hostname
    proxy_port = proxy.port or (443 if proxy.scheme in ("https", "wss") else 80)

    if proxy.scheme == "https":
        ssl_ctx_proxy = ssl.create_default_context()
        reader, writer = await asyncio.open_connection(proxy_host, proxy_port, ssl=ssl_ctx_proxy)
    else:
        reader, writer = await asyncio.open_connection(proxy_host, proxy_port)

    req_lines = [
        f"CONNECT {target_host}:{target_port} HTTP/1.1",
        f"Host: {target_host}:{target_port}",
    ]
    if proxy.username and proxy.password:
        import base64
        auth_str = f"{proxy.username}:{proxy.password}"
        b64_auth = base64.b64encode(auth_str.encode()).decode()
        req_lines.append(f"Proxy-Authorization: Basic {b64_auth}")
    req_lines.append("\r\n")

    writer.write("\r\n".join(req_lines).encode())
    await writer.drain()

    resp_data = await reader.readuntil(b"\r\n\r\n")
    status_line = resp_data.split(b"\r\n")[0].decode()
    if " 200 " not in status_line:
        writer.close()
        await writer.wait_closed()
        raise ConnectionError(f"Proxy CONNECT failed: {status_line}")

    sock = writer.get_extra_info("socket")
    if target.scheme == "wss":
        ssl_ctx_target = ssl.create_default_context()
        sock = ssl_ctx_target.wrap_socket(sock, server_hostname=target_host)

    return await websockets.connect(ws_url, sock=sock, **ws_kwargs)


async def run_diagnostic(user_id: str, api_secret: str, auth_code: str, proxy_url: str = None):
    logger.info("=== STEP 1: EXCHANGING AUTH CODE (getUserDetails) ===")
    checksum_raw = f"{user_id}{auth_code}{api_secret}"
    checksum = hashlib.sha256(checksum_raw.encode("utf-8")).hexdigest()
    logger.info(f"UserId: {user_id}")
    logger.info(f"Checksum: {checksum[:12]}...")

    http_kwargs = {"timeout": 10.0}
    if proxy_url:
        http_kwargs["proxy"] = proxy_url
        logger.info(f"Using proxy: {proxy_url.split('@')[-1] if '@' in proxy_url else proxy_url}")

    async with httpx.AsyncClient(**http_kwargs) as client:
        try:
            resp = await client.post(
                f"{A3_BASE_URL}/open-api/od/v1/vendor/getUserDetails",
                json={"checkSum": checksum},
                headers={"Content-Type": "application/json"},
            )
            logger.info(f"getUserDetails Status: {resp.status_code}")
            data = resp.json()
            logger.info(f"getUserDetails Response: {data}")
        except Exception as e:
            logger.error(f"getUserDetails Failed: {e}")
            return

        stat = str(data.get("stat", "")).lower()
        if stat != "ok":
            logger.error(f"Authentication rejected by Alice Blue: {data}")
            return

        user_session = data.get("userSession")
        if not user_session:
            logger.error("No userSession token present in response!")
            return

        logger.info("=== STEP 2: TESTING REST API (get_limits) ===")
        headers = {
            "Authorization": f"Bearer {user_session}",
            "Content-Type": "application/json",
        }
        try:
            limits_resp = await client.get(f"{A3_BASE_URL}/open-api/od/v1/limits", headers=headers)
            logger.info(f"get_limits Status: {limits_resp.status_code}")
            logger.info(f"get_limits Response: {limits_resp.json()}")
        except Exception as e:
            logger.error(f"get_limits Failed: {e}")

        logger.info("=== STEP 3: CREATING WS SESSION (createWsSess) ===")
        try:
            ws_sess_resp = await client.post(
                f"{A3_BASE_URL}/open-api/od/v1/profile/createWsSess",
                json={"source": "API", "userId": user_id},
                headers=headers,
            )
            logger.info(f"createWsSess Status: {ws_sess_resp.status_code}")
            logger.info(f"createWsSess Response: {ws_sess_resp.json()}")
        except Exception as e:
            logger.error(f"createWsSess Failed: {e}")

        logger.info("=== STEP 4: TESTING WEBSOCKET HANDSHAKE (NorenWS) ===")
        first_hash = hashlib.sha256(user_session.encode("utf-8")).hexdigest()
        susertoken = hashlib.sha256(first_hash.encode("utf-8")).hexdigest()

        auth_msg = {
            "susertoken": susertoken,
            "t": "c",
            "actid": f"{user_id}_API",
            "uid": f"{user_id}_API",
            "source": "API",
        }

        try:
            if proxy_url:
                ws_ctx = _connect_ws_via_proxy(WS_URL, proxy_url, ping_interval=None)
            else:
                ws_ctx = websockets.connect(WS_URL, ping_interval=None)

            async with ws_ctx as ws:
                logger.info("Connected to WebSocket server. Sending auth payload...")
                await ws.send(json.dumps(auth_msg))

                ws_resp_raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                logger.info(f"WebSocket Auth Response: {ws_resp_raw}")

                sub_msg = {"t": "o", "actid": f"{user_id}_API"}
                await ws.send(json.dumps(sub_msg))
                logger.info("Subscribed to order updates. Listening for 5 seconds...")

                ping_msg = {"t": "h"}
                await ws.send(json.dumps(ping_msg))

                for _ in range(3):
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        logger.info(f"WS Recv: {msg}")
                    except asyncio.TimeoutError:
                        logger.info("WS Listening... (no message received yet)")

                logger.info("=== DIAGNOSTIC TEST COMPLETED SUCCESSFULLY! ===")
        except Exception as e:
            logger.error(f"WebSocket Connection Failed: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python scratch_test_alice.py <USER_ID> <API_SECRET> <AUTH_CODE> [PROXY_URL]")
        sys.exit(1)

    u_id = sys.argv[1]
    secret = sys.argv[2]
    code = sys.argv[3]
    proxy = sys.argv[4] if len(sys.argv) > 4 else None

    asyncio.run(run_diagnostic(u_id, secret, code, proxy))
