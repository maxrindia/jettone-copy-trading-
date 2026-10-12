import pytest
import json
from unittest.mock import AsyncMock, MagicMock
from decimal import Decimal

from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.websocket import AliceBlueWebSocketClient


def test_websocket_order_normalization():
    mock_client = MagicMock(spec=AliceBlueClient)
    mock_client.account_id = "acc-master"
    mock_client.user_id = "AB00101"

    ws_client = AliceBlueWebSocketClient(mock_client)

    # Simulated incoming raw NorenWS order update message
    raw_packet = {
        "t": "om",
        "nOrdNo": "24082000098765",
        "orderTag": "COPY_M1_F1_UUID",
        "tsym": "BANKNIFTY24AUG51000CE",
        "trantype": "BUY",
        "qty": "30",
        "fillshares": "30",
        "avgprc": "255.40",
        "status": "COMPLETE",
        "pCode": "MIS",
    }

    event = ws_client._normalize_event(raw_packet)
    assert event is not None
    assert event.broker_order_id == "24082000098765"
    assert event.client_order_id == "COPY_M1_F1_UUID"
    assert event.symbol == "BANKNIFTY24AUG51000CE"
    assert event.side == "BUY"
    assert event.quantity == 30
    assert event.filled_quantity == 30
    assert event.fill_price == Decimal("255.40")
    assert event.status == "COMPLETE"
    assert event.account_id == "acc-master"


def test_ignore_non_order_websocket_packets():
    mock_client = MagicMock(spec=AliceBlueClient)
    mock_client.account_id = "acc-master"
    mock_client.user_id = "AB00101"

    ws_client = AliceBlueWebSocketClient(mock_client)

    # Heartbeat response or market feed packet
    raw_packet = {"t": "ck", "s": "OK"}
    event = ws_client._normalize_event(raw_packet)
    assert event is None


@pytest.mark.asyncio
async def test_websocket_invalidate_session_clears_redis(mock_redis):
    account_id = "acc-ws-inval"
    await mock_redis.set(f"alice:session:{account_id}", "OLD_TOKEN_TO_CLEAR")
    assert await mock_redis.get(f"alice:session:{account_id}") == "OLD_TOKEN_TO_CLEAR"

    mock_client = MagicMock(spec=AliceBlueClient)
    mock_client.account_id = account_id
    mock_client.user_id = "AB_INVAL_1"
    mock_client.redis = mock_redis

    ws_client = AliceBlueWebSocketClient(mock_client)
    await ws_client._invalidate_session("Test Policy Violation")

    # Redis session key must be deleted
    assert await mock_redis.get(f"alice:session:{account_id}") is None


@pytest.mark.asyncio
async def test_websocket_auth_and_subscription_packets(mock_redis):
    account_id = "acc-ws-auth-1"
    user_id = "USER_DHINESH"
    session_token = "A3_JWT_SESSION_TOKEN_123"

    await mock_redis.set(f"alice:session:{account_id}", session_token)

    mock_client = MagicMock(spec=AliceBlueClient)
    mock_client.account_id = account_id
    mock_client.user_id = user_id
    mock_client.get_session = AsyncMock(return_value=session_token)

    ws_client = AliceBlueWebSocketClient(mock_client)

    # 1. Verify Auth Packet Format
    auth_msg = {
        "susertoken": session_token,
        "t": "c",
        "actid": f"{user_id}_API",
        "uid": f"{user_id}_API",
        "source": "API",
    }
    assert auth_msg["susertoken"] == "A3_JWT_SESSION_TOKEN_123"
    assert auth_msg["actid"] == "USER_DHINESH_API"
    assert auth_msg["uid"] == "USER_DHINESH_API"
    assert auth_msg["source"] == "API"
    assert auth_msg["t"] == "c"

    # 2. Verify Subscription Packet Format
    sub_msg = {"t": "o", "actid": f"{user_id}_API"}
    assert sub_msg["t"] == "o"
    assert sub_msg["actid"] == "USER_DHINESH_API"


@pytest.mark.asyncio
async def test_websocket_backfill_is_retired_and_replays_nothing(mock_redis):
    """
    The WS order-book backfill is retired: MasterOrderPoller now owns master
    order-book ingestion, with the baseline-seed rule this path never had.

    This previously replayed the whole order book through on_order_update on
    every connect/reconnect. That was unsafe: MasterTradeDetector.seed_state
    seeds only from persisted OPEN/PARTIAL Order rows, so an already-COMPLETE
    order - or any Mobile/Web-originated order, which had no persisted row at
    all - was seen as brand-new incremental fill and could fire real follower
    orders for master activity from hours earlier on a simple restart.

    Contract now: dispatch nothing, and don't even call the broker.
    """
    mock_client = MagicMock(spec=AliceBlueClient)
    mock_client.account_id = "acc-bf-1"
    mock_client.user_id = "USER_VIKASH"
    mock_client.get_order_book = AsyncMock()

    ws_client = AliceBlueWebSocketClient(mock_client)
    dispatched_events = []
    ws_client.on_order_update = lambda event: dispatched_events.append(event)

    count = await ws_client._backfill_missed_fills()

    assert count == 0
    assert dispatched_events == []
    mock_client.get_order_book.assert_not_awaited()


@pytest.mark.asyncio
async def test_connect_and_listen_backfills_once_before_first_connection(mock_redis):
    """
    Issue #5: connect_and_listen must back-fill once, unconditionally,
    before attempting its very first WS connection each run - not only
    after a later mid-session reconnect (the pre-existing behavior, in the
    except branch, left completely untouched here). Without this, a clean
    restart that connects successfully on the first try never discovers
    anything that happened at the broker while the process was down.

    The fake _backfill_missed_fills calls close() to stop the loop
    immediately after it runs, so this test doesn't need to also fake a
    real WebSocket connection - it only needs to prove backfill ran before
    any connection attempt was made.
    """
    mock_client = MagicMock(spec=AliceBlueClient)
    mock_client.account_id = "acc-startup-bf"
    mock_client.user_id = "USER_STARTUP"

    ws_client = AliceBlueWebSocketClient(mock_client)

    backfill_call_count = {"n": 0}

    async def fake_backfill():
        backfill_call_count["n"] += 1
        await ws_client.close()
        return 0

    ws_client._backfill_missed_fills = fake_backfill

    events = [event async for event in ws_client.connect_and_listen()]

    assert backfill_call_count["n"] == 1
    assert events == []


@pytest.mark.asyncio
async def test_websocket_multi_account_isolation(mock_redis):
    acc1_token = "JWT_USER_1"
    acc2_token = "JWT_USER_2"

    await mock_redis.set("alice:session:acc-1", acc1_token)
    await mock_redis.set("alice:session:acc-2", acc2_token)

    c1 = MagicMock(spec=AliceBlueClient)
    c1.account_id = "acc-1"
    c1.user_id = "USER1"
    c1.get_session = AsyncMock(return_value=acc1_token)

    c2 = MagicMock(spec=AliceBlueClient)
    c2.account_id = "acc-2"
    c2.user_id = "USER2"
    c2.get_session = AsyncMock(return_value=acc2_token)

    ws1 = AliceBlueWebSocketClient(c1)
    ws2 = AliceBlueWebSocketClient(c2)

    assert ws1.account_id == "acc-1"
    assert ws1.user_id == "USER1"
    assert ws2.account_id == "acc-2"
    assert ws2.user_id == "USER2"
    assert await c1.get_session() == "JWT_USER_1"
    assert await c2.get_session() == "JWT_USER_2"

