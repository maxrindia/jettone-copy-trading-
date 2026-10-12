"""
Focused regression tests for two P2 findings in
app/services/alice_blue/websocket.py:

- Finding #8: the LIVE message loop inside connect_and_listen() invoked
  on_order_update without the same asyncio.iscoroutine()/await handling
  that _backfill_missed_fills() already has, so an async callback would be
  silently dropped (created as a coroutine, never awaited/executed).
- Finding #9: a non-OK WS auth acknowledgment was only logged as a warning
  and the code proceeded to subscribe/stream anyway, instead of being
  treated like the existing 1008-policy-violation bad-session case
  (invalidate session + force a reconnect via the existing backoff loop).

These tests follow the mocking style already used in
test_alice_blue_websocket.py (MagicMock(spec=AliceBlueClient), mock_redis
fixture) but add a minimal fake WebSocket connection object since these two
findings live inside the live connect_and_listen() message loop, which the
existing test file didn't previously need to exercise end-to-end.
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

import app.services.alice_blue.websocket as ws_module
from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.websocket import AliceBlueWebSocketClient


class FakeWS:
    """Minimal stand-in for a websockets.WebSocketClientProtocol instance."""

    def __init__(self, auth_resp, messages):
        self._auth_resp = auth_resp
        self._messages = list(messages)
        self.sent = []
        self.open = True
        self.close = AsyncMock()

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        # Only the auth handshake calls recv() directly in this code path.
        return json.dumps(self._auth_resp)

    def __aiter__(self):
        return self._iter_messages()

    async def _iter_messages(self):
        for m in self._messages:
            yield m


class FakeWSContext:
    """Stand-in for the object returned by websockets.connect(...)."""

    def __init__(self, fake_ws):
        self._fake_ws = fake_ws

    async def __aenter__(self):
        return self._fake_ws

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _build_client(account_id="acc-live", user_id="USER_LIVE"):
    mock_client = MagicMock(spec=AliceBlueClient)
    mock_client.account_id = account_id
    mock_client.user_id = user_id
    mock_client.get_session = AsyncMock(return_value="SESSION_TOKEN")
    mock_client.create_ws_session = AsyncMock(return_value=None)
    mock_client.proxy_url = None
    return mock_client


ORDER_PACKET = {
    "t": "om",
    "nOrdNo": "999",
    "tsym": "ACC",
    "trantype": "BUY",
    "qty": "10",
    "fillshares": "10",
    "avgprc": "100.00",
    "status": "COMPLETE",
    "pCode": "MIS",
}


# ---------------------------------------------------------------------------
# Finding #8: live callback must handle async on_order_update the same way
# _backfill_missed_fills() already does.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_live_stream_awaits_async_on_order_update_callback(monkeypatch):
    mock_client = _build_client()
    ws_client = AliceBlueWebSocketClient(mock_client)

    # Skip the unrelated startup backfill entirely - not what this test covers.
    ws_client._backfill_missed_fills = AsyncMock(return_value=0)

    fake_ws = FakeWS(auth_resp={"s": "OK"}, messages=[json.dumps(ORDER_PACKET)])
    monkeypatch.setattr(
        ws_module.websockets, "connect", lambda *a, **kw: FakeWSContext(fake_ws)
    )

    received = []

    async def async_callback(event):
        # Proves the coroutine body actually ran (was awaited), not merely
        # created-and-discarded.
        received.append(event)

    ws_client.on_order_update = async_callback

    gen = ws_client.connect_and_listen()
    try:
        event = await gen.__anext__()
    finally:
        if ws_client._heartbeat_task:
            ws_client._heartbeat_task.cancel()
        await gen.aclose()

    assert event.broker_order_id == "999"
    assert len(received) == 1, (
        "async on_order_update callback was not awaited/executed by the "
        "live message loop - it was silently dropped"
    )
    assert received[0].broker_order_id == "999"


# ---------------------------------------------------------------------------
# Finding #9: a non-OK WS auth response must be treated like the existing
# 1008 policy-violation bad-session handling: invalidate session, do not
# subscribe/stream, and let the existing reconnect loop retry.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_non_ok_auth_response_invalidates_session_and_does_not_subscribe(
    monkeypatch, mock_redis
):
    account_id = "acc-auth-bad"
    await mock_redis.set(f"alice:session:{account_id}", "STALE_TOKEN")

    mock_client = _build_client(account_id=account_id, user_id="USER_BADAUTH")
    mock_client.redis = mock_redis

    ws_client = AliceBlueWebSocketClient(mock_client)
    ws_client._backfill_missed_fills = AsyncMock(return_value=0)

    # Speed up the test: the existing 1008 path sleeps 20s before retrying.
    sleep_calls = []
    real_sleep = ws_module.asyncio.sleep

    async def fast_sleep(delay):
        sleep_calls.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(ws_module.asyncio, "sleep", fast_sleep)

    # First connection attempt: broker acks auth with a non-OK status.
    bad_auth_ws = FakeWS(auth_resp={"s": "NOT_OK", "emsg": "Invalid Session"}, messages=[])
    # Second connection attempt (after the forced reconnect): stop the loop
    # cleanly instead of looping forever.
    good_auth_ws = FakeWS(auth_resp={"s": "OK"}, messages=[])

    contexts = [FakeWSContext(bad_auth_ws), FakeWSContext(good_auth_ws)]

    def fake_connect(*a, **kw):
        return contexts.pop(0)

    monkeypatch.setattr(ws_module.websockets, "connect", fake_connect)

    gen = ws_client.connect_and_listen()

    async def stop_after_second_connect():
        # Once the second (post-reconnect) connection is attempted, close
        # the generator so the test doesn't hang in an infinite reconnect
        # loop with an empty message stream.
        await ws_client.close()

    # Drive the generator manually: nothing will ever be yielded because
    # neither fake connection produces order messages, so pull with a
    # timeout-guarded approach instead of `async for`.
    import asyncio as _asyncio

    async def drive():
        try:
            await gen.__anext__()
        except StopAsyncIteration:
            pass

    task = _asyncio.create_task(drive())
    # Give the loop enough turns to: send bad auth -> detect non-OK ->
    # invalidate session -> sleep(20, faked to ~0) -> attempt reconnect.
    for _ in range(50):
        await _asyncio.sleep(0)
        if not contexts:  # second connect() call has already happened
            break

    await ws_client.close()
    task.cancel()
    try:
        await task
    except (_asyncio.CancelledError, Exception):
        pass
    if ws_client._heartbeat_task:
        ws_client._heartbeat_task.cancel()
    await gen.aclose()

    # 1. Must NOT have proceeded to subscribe on the bad-auth connection.
    subscribe_payloads = [json.loads(m) for m in bad_auth_ws.sent if m]
    assert not any(p.get("t") == "o" for p in subscribe_payloads), (
        "client sent a subscribe message on a connection whose auth "
        "response was NOT OK - it should have invalidated and reconnected "
        "instead of proceeding"
    )

    # 2. Must have invalidated the session (mirrors the 1008 bad-session path).
    assert await mock_redis.get(f"alice:session:{account_id}") is None, (
        "non-OK auth response did not invalidate the cached session, "
        "unlike the existing 1008 policy-violation handling"
    )

    # 3. Must have actually attempted a second connection (i.e. retried),
    # exactly like the 1008 case triggers a retry via the existing loop.
    assert not contexts, "client never retried the connection after non-OK auth"
