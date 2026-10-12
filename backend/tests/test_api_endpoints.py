"""Endpoint-level tests: auth gate, accounts CRUD, trades, reliability routes.
Runs the real FastAPI app via ASGI transport with overridden dependencies;
no network and no lifespan (EngineLoop never starts)."""
import httpx
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from app.api.accounts import get_broker_client_provider, get_runtime_client_manager
from app.services.alice_blue.manager import AliceBlueAccountManager
from app.core.database import get_db
from app.core.redis import get_redis
from app.main import app
from app.models.accounts import ApiCredential
from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.exceptions import IPNotWhitelistedError
from sqlalchemy import select
from conftest import TestingSessionLocal

TOKEN = "test-internal-token"
AUTH_HEADER = {"X-Internal-Token": TOKEN}

ACCOUNT_PAYLOAD = {
    "account_type": "FOLLOWER",
    "display_name": "Endpoint Follower",
    "alice_user_id": "AB_EP_1",
    "api_secret": "endpoint-secret-123",
}


@pytest_asyncio.fixture
async def api_client(mock_redis, db_session):
    async def override_db():
        async with TestingSessionLocal() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_redis] = lambda: mock_redis
    # Isolated registry per test so account endpoints never touch the
    # process-wide engine manager.
    runtime_manager = AliceBlueAccountManager()
    app.dependency_overrides[get_runtime_client_manager] = lambda: runtime_manager

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Internal auth gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_endpoint_rejects_missing_token(api_client):
    resp = await api_client.get("/api/v1/accounts")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_endpoint_rejects_wrong_token(api_client):
    resp = await api_client.get("/api/v1/accounts", headers={"X-Internal-Token": "nope"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_health_is_open_without_token(api_client):
    resp = await api_client.get("/health")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Accounts endpoints
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_account_roundtrip_and_encryption(api_client, db_session):
    resp = await api_client.post(
        "/api/v1/accounts", json=ACCOUNT_PAYLOAD, headers=AUTH_HEADER
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["alice_user_id"] == "AB_EP_1"
    assert "endpoint-secret" not in resp.text

    row = (
        await db_session.execute(select(ApiCredential))
    ).scalars().first()
    assert row is not None
    assert "endpoint-secret-123" not in row.encrypted_api_secret


@pytest.mark.asyncio
async def test_create_duplicate_account_returns_409(api_client):
    first = await api_client.post(
        "/api/v1/accounts", json=ACCOUNT_PAYLOAD, headers=AUTH_HEADER
    )
    assert first.status_code == 201

    dup = await api_client.post(
        "/api/v1/accounts", json=ACCOUNT_PAYLOAD, headers=AUTH_HEADER
    )
    assert dup.status_code == 409


@pytest.mark.asyncio
async def test_list_accounts_includes_created(api_client):
    await api_client.post("/api/v1/accounts", json=ACCOUNT_PAYLOAD, headers=AUTH_HEADER)
    resp = await api_client.get("/api/v1/accounts", headers=AUTH_HEADER)
    assert resp.status_code == 200
    ids = [a["alice_user_id"] for a in resp.json()]
    assert "AB_EP_1" in ids


@pytest.mark.asyncio
async def test_create_account_validation_failure(api_client):
    bad = {**ACCOUNT_PAYLOAD, "alice_user_id": ""}
    resp = await api_client.post("/api/v1/accounts", json=bad, headers=AUTH_HEADER)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Per-account order history: GET /accounts/{account_id}/orders
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_account_orders_returns_only_that_accounts_orders(api_client):
    import uuid
    from decimal import Decimal
    from app.models.trades import Order, OrderSide, OrderSource, OrderStatus

    acc_resp = await api_client.post("/api/v1/accounts", json=ACCOUNT_PAYLOAD, headers=AUTH_HEADER)
    account_id = acc_resp.json()["id"]

    other_resp = await api_client.post(
        "/api/v1/accounts",
        json={**ACCOUNT_PAYLOAD, "alice_user_id": "AB_EP_OTHER"},
        headers=AUTH_HEADER,
    )
    other_id = other_resp.json()["id"]

    async with TestingSessionLocal() as session:
        session.add(Order(
            id=uuid.uuid4(), internal_order_id="ACC_HIST_1", broker_order_id="BRK_ACC_HIST_1",
            account_id=uuid.UUID(account_id), source=OrderSource.INTERNAL_UI, broker="ALICE_BLUE",
            exchange="NSE", symbol="RELIANCE-EQ", side=OrderSide.BUY, order_type="MARKET",
            product_type="MIS", quantity=10, filled_quantity=10, pending_quantity=0,
            price=Decimal("2950.00"), average_price=Decimal("2950.00"), status=OrderStatus.COMPLETE,
        ))
        session.add(Order(
            id=uuid.uuid4(), internal_order_id="ACC_HIST_OTHER", broker_order_id="BRK_ACC_HIST_OTHER",
            account_id=uuid.UUID(other_id), source=OrderSource.INTERNAL_UI, broker="ALICE_BLUE",
            exchange="NSE", symbol="TCS-EQ", side=OrderSide.SELL, order_type="MARKET",
            product_type="MIS", quantity=5, filled_quantity=5, pending_quantity=0,
            price=Decimal("3900.00"), average_price=Decimal("3900.00"), status=OrderStatus.COMPLETE,
        ))
        await session.commit()

    resp = await api_client.get(f"/api/v1/accounts/{account_id}/orders", headers=AUTH_HEADER)
    assert resp.status_code == 200
    orders = resp.json()
    assert len(orders) == 1
    assert orders[0]["internal_order_id"] == "ACC_HIST_1"
    assert orders[0]["account_id"] == account_id


@pytest.mark.asyncio
async def test_account_orders_404_for_unknown_account(api_client):
    import uuid
    resp = await api_client.get(f"/api/v1/accounts/{uuid.uuid4()}/orders", headers=AUTH_HEADER)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Trades endpoints
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trade_stats_shape(api_client):
    resp = await api_client.get("/api/v1/trades/stats", headers=AUTH_HEADER)
    assert resp.status_code == 200
    body = resp.json()
    for key in ("total_trades_today", "success_count", "rejected_count", "failed_count", "pending_count"):
        assert key in body


@pytest.mark.asyncio
async def test_list_trades_empty(api_client):
    resp = await api_client.get("/api/v1/trades", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Reliability endpoints
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_emergency_stop_toggle_roundtrip(api_client):
    on = await api_client.post(
        "/api/v1/reliability/emergency-stop",
        json={"active": True, "reason": "endpoint test"},
        headers=AUTH_HEADER,
    )
    assert on.status_code == 200

    status = await api_client.get(
        "/api/v1/reliability/emergency-stop", headers=AUTH_HEADER
    )
    assert status.status_code == 200
    assert status.json()["data"]["global_active"] is True

    off = await api_client.post(
        "/api/v1/reliability/emergency-stop",
        json={"active": False, "reason": "endpoint test"},
        headers=AUTH_HEADER,
    )
    assert off.status_code == 200


@pytest.mark.asyncio
async def test_system_emergency_stop_rerouted_to_service(api_client, db_session):
    """R4 unification: /system/emergency-stop must drive the Redis authority."""
    resp = await api_client.post(
        "/api/v1/system/emergency-stop",
        json={"stop": True, "reason": "system route test"},
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 200

    status = await api_client.get(
        "/api/v1/reliability/emergency-stop", headers=AUTH_HEADER
    )
    assert status.json()["data"]["global_active"] is True

    await api_client.post(
        "/api/v1/system/emergency-stop",
        json={"stop": False, "reason": "system route test"},
        headers=AUTH_HEADER,
    )


@pytest.mark.asyncio
async def test_reconciliation_records_empty(api_client):
    resp = await api_client.get(
        "/api/v1/reliability/reconciliation/records?resolved=false",
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == []


# ---------------------------------------------------------------------------
# OAuth Callback Isolation & Verification tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_demat_callback_rejects_missing_account_context(api_client, mock_redis):
    """
    A callback with neither account_id nor target_user_id can only be
    resolved by whatever user Alice Blue returned, which makes the mismatch
    guard vacuous (the target account is derived from the same value it
    would be compared against). This is the exact shape of a stale/leftover
    Alice Blue tab silently re-authenticating whichever account matches the
    browser's current session, without ever going through Jettone's
    Reconnect button (which always sends account_id/target_user_id).
    """
    acc_resp = await api_client.post(
        "/api/v1/accounts",
        json={
            "account_type": "FOLLOWER",
            "display_name": "Stale Tab Target",
            "alice_user_id": "AB_STALE_TARGET",
            "api_secret": "secret_stale",
        },
        headers=AUTH_HEADER,
    )
    assert acc_resp.status_code == 201
    acc = acc_resp.json()

    cb_resp = await api_client.post(
        "/api/v1/accounts/callback",
        json={
            "auth_code": "AUTH_CODE_NO_CONTEXT",
            "returned_user_id": "AB_STALE_TARGET",
        },
        headers=AUTH_HEADER,
    )
    assert cb_resp.status_code == 400
    assert "Reconnect" in cb_resp.json()["detail"]

    # Prior to the fix this would have matched by returned_user_id alone and
    # silently authenticated the account. Confirm that did NOT happen.
    get_acc = await api_client.get(f"/api/v1/accounts/{acc['id']}", headers=AUTH_HEADER)
    assert get_acc.json()["session_valid"] is False
    assert mock_redis._storage.get(f"alice:session:{acc['id']}") is None


@pytest.mark.asyncio
async def test_reconnect_intent_lets_cross_port_callback_resolve(api_client, mock_redis):
    """
    Jettone is reachable on two host ports (3000 and 8080) but Alice Blue's
    redirect_uri is fixed to one of them, so a browser-storage value written
    before the redirect can be invisible on the callback's origin. Calling
    POST /{account_id}/reconnect-intent first (the server-side equivalent,
    unaffected by which port the browser is on) must let a callback with
    NO account_id/target_user_id — only returned_user_id, exactly what a
    cross-port browser produces — still resolve correctly and safely.
    """
    acc_resp = await api_client.post(
        "/api/v1/accounts",
        json={
            "account_type": "FOLLOWER",
            "display_name": "Cross Port Target",
            "alice_user_id": "AB_CROSS_PORT",
            "api_secret": "secret_cross_port",
        },
        headers=AUTH_HEADER,
    )
    assert acc_resp.status_code == 201
    acc = acc_resp.json()

    intent_resp = await api_client.post(
        f"/api/v1/accounts/{acc['id']}/reconnect-intent",
        headers=AUTH_HEADER,
    )
    assert intent_resp.status_code == 204
    assert mock_redis._storage.get("alice:reconnect_intent:AB_CROSS_PORT") == acc["id"]

    with patch.object(AliceBlueClient, "authenticate_with_auth_code", new_callable=AsyncMock) as mock_auth:
        mock_auth.return_value = "JWT_CROSS_PORT_VALID"

        cb_resp = await api_client.post(
            "/api/v1/accounts/callback",
            json={
                "auth_code": "AUTH_CODE_CROSS_PORT",
                "returned_user_id": "AB_CROSS_PORT",
            },
            headers=AUTH_HEADER,
        )

    assert cb_resp.status_code == 200

    get_acc = await api_client.get(f"/api/v1/accounts/{acc['id']}", headers=AUTH_HEADER)
    assert get_acc.json()["session_valid"] is True

    # Single-use: the intent must not still be usable for a second callback.
    assert mock_redis._storage.get("alice:reconnect_intent:AB_CROSS_PORT") is None


@pytest.mark.asyncio
async def test_reconnect_intent_does_not_bypass_account_mismatch(api_client, mock_redis):
    """
    A reconnect-intent is keyed by alice_user_id, so it can only ever
    resolve to the account that ID actually belongs to — it must not be
    usable to authenticate a DIFFERENT account than the one Alice Blue
    actually returned a session for.
    """
    acc_a_resp = await api_client.post(
        "/api/v1/accounts",
        json={
            "account_type": "MASTER",
            "display_name": "Intent Master",
            "alice_user_id": "AB_INTENT_MASTER",
            "api_secret": "secret_a",
        },
        headers=AUTH_HEADER,
    )
    acc_a = acc_a_resp.json()

    acc_b_resp = await api_client.post(
        "/api/v1/accounts",
        json={
            "account_type": "FOLLOWER",
            "display_name": "Intent Follower",
            "alice_user_id": "AB_INTENT_FOLLOWER",
            "api_secret": "secret_b",
        },
        headers=AUTH_HEADER,
    )
    acc_b = acc_b_resp.json()

    # Record intent to reconnect Follower B...
    await api_client.post(f"/api/v1/accounts/{acc_b['id']}/reconnect-intent", headers=AUTH_HEADER)

    # ...but the browser's Alice Blue session actually belongs to Master A,
    # for which no intent was recorded.
    cb_resp = await api_client.post(
        "/api/v1/accounts/callback",
        json={
            "auth_code": "AUTH_CODE_WRONG_USER",
            "returned_user_id": "AB_INTENT_MASTER",
        },
        headers=AUTH_HEADER,
    )
    # No pending intent exists for AB_INTENT_MASTER, so this is rejected as
    # ambiguous rather than silently authenticating either account.
    assert cb_resp.status_code == 400

    get_a = await api_client.get(f"/api/v1/accounts/{acc_a['id']}", headers=AUTH_HEADER)
    get_b = await api_client.get(f"/api/v1/accounts/{acc_b['id']}", headers=AUTH_HEADER)
    assert get_a.json()["session_valid"] is False
    assert get_b.json()["session_valid"] is False


@pytest.mark.asyncio
async def test_sign_out_clears_session_and_pending_intent(api_client, mock_redis):
    """
    Sign Out is a Jettone-side-only reset (it can't touch Alice Blue's own
    browser session — see record_reconnect_intent's docstring). It must
    clear session_valid, last_authenticated_at, the cached Redis session
    token, and any pending reconnect-intent for this account.
    """
    acc_resp = await api_client.post(
        "/api/v1/accounts",
        json={
            "account_type": "FOLLOWER",
            "display_name": "Sign Out Target",
            "alice_user_id": "AB_SIGNOUT",
            "api_secret": "secret_signout",
        },
        headers=AUTH_HEADER,
    )
    assert acc_resp.status_code == 201
    acc = acc_resp.json()

    # Get it into a genuinely authenticated state first: a Redis token must
    # exist before update_account will honor session_valid=True (it refuses
    # premature session_valid without one — see update_account).
    await mock_redis.set(f"alice:session:{acc['id']}", "FAKE_SESSION_TOKEN", ex=18 * 3600)
    put_resp = await api_client.put(
        f"/api/v1/accounts/{acc['id']}",
        json={"session_valid": True, "last_authenticated_at": "2026-09-07T06:00:00+00:00"},
        headers=AUTH_HEADER,
    )
    assert put_resp.json()["session_valid"] is True
    assert mock_redis._storage.get(f"alice:session:{acc['id']}") is not None

    # A pending reconnect-intent should also be cleared by sign-out.
    await api_client.post(f"/api/v1/accounts/{acc['id']}/reconnect-intent", headers=AUTH_HEADER)
    assert mock_redis._storage.get("alice:reconnect_intent:AB_SIGNOUT") is not None

    signout_resp = await api_client.post(f"/api/v1/accounts/{acc['id']}/sign-out", headers=AUTH_HEADER)
    assert signout_resp.status_code == 200
    body = signout_resp.json()
    assert body["session_valid"] is False
    assert body["last_authenticated_at"] is None

    get_resp = await api_client.get(f"/api/v1/accounts/{acc['id']}", headers=AUTH_HEADER)
    assert get_resp.json()["session_valid"] is False
    assert mock_redis._storage.get(f"alice:session:{acc['id']}") is None
    assert mock_redis._storage.get("alice:reconnect_intent:AB_SIGNOUT") is None


@pytest.mark.asyncio
async def test_sign_out_missing_account_404(api_client, mock_redis):
    resp = await api_client.post(
        "/api/v1/accounts/00000000-0000-0000-0000-000000000000/sign-out",
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_demat_callback_account_mismatch_fails_400(api_client, mock_redis):
    # Create Account A and Account B
    acc_a_resp = await api_client.post(
        "/api/v1/accounts",
        json={
            "account_type": "MASTER",
            "display_name": "Master A",
            "alice_user_id": "AB_MASTER_A",
            "api_secret": "secret_a",
        },
        headers=AUTH_HEADER,
    )
    assert acc_a_resp.status_code == 201
    acc_a = acc_a_resp.json()

    acc_b_resp = await api_client.post(
        "/api/v1/accounts",
        json={
            "account_type": "FOLLOWER",
            "display_name": "Follower B",
            "alice_user_id": "AB_FOLLOWER_B",
            "api_secret": "secret_b",
        },
        headers=AUTH_HEADER,
    )
    assert acc_b_resp.status_code == 201
    acc_b = acc_b_resp.json()

    # Browser returns Account A's user ID while trying to authenticate Account B
    cb_resp = await api_client.post(
        "/api/v1/accounts/callback",
        json={
            "auth_code": "AUTH_CODE_XYZ",
            "account_id": acc_b["id"],
            "target_user_id": "AB_FOLLOWER_B",
            "returned_user_id": "AB_MASTER_A",
        },
        headers=AUTH_HEADER,
    )
    assert cb_resp.status_code == 400
    assert "Account mismatch" in cb_resp.json()["detail"]
    assert "AB_MASTER_A" in cb_resp.json()["detail"]

    # Verify Account B is NOT marked session_valid and Redis key is not set
    get_b = await api_client.get(f"/api/v1/accounts/{acc_b['id']}", headers=AUTH_HEADER)
    assert get_b.json()["session_valid"] is False
    assert mock_redis._storage.get(f"alice:session:{acc_b['id']}") is None


@pytest.mark.asyncio
async def test_demat_callback_success_updates_only_target_account(api_client, mock_redis):
    # Create Account A and Account B
    acc_a_resp = await api_client.post(
        "/api/v1/accounts",
        json={
            "account_type": "MASTER",
            "display_name": "Master A2",
            "alice_user_id": "AB_MASTER_A2",
            "api_secret": "secret_a",
        },
        headers=AUTH_HEADER,
    )
    acc_a = acc_a_resp.json()

    acc_b_resp = await api_client.post(
        "/api/v1/accounts",
        json={
            "account_type": "FOLLOWER",
            "display_name": "Follower B2",
            "alice_user_id": "AB_FOLLOWER_B2",
            "api_secret": "secret_b",
        },
        headers=AUTH_HEADER,
    )
    acc_b = acc_b_resp.json()

    with patch.object(AliceBlueClient, "authenticate_with_auth_code", new_callable=AsyncMock) as mock_auth:
        mock_auth.return_value = "JWT_AUTH_CODE_B_VALID"
        
        cb_resp = await api_client.post(
            "/api/v1/accounts/callback",
            json={
                "auth_code": "AUTH_CODE_B_VALID",
                "account_id": acc_b["id"],
                "target_user_id": "AB_FOLLOWER_B2",
                "returned_user_id": "AB_FOLLOWER_B2",
            },
            headers=AUTH_HEADER,
        )

    assert cb_resp.status_code == 200
    assert cb_resp.json()["status"] == "success"

    # Verify Account B is live
    get_b = await api_client.get(f"/api/v1/accounts/{acc_b['id']}", headers=AUTH_HEADER)
    assert get_b.json()["session_valid"] is True

    # Verify Account A is still unaffected (offline)
    get_a = await api_client.get(f"/api/v1/accounts/{acc_a['id']}", headers=AUTH_HEADER)
    assert get_a.json()["session_valid"] is False


@pytest.mark.asyncio
async def test_demat_callback_probe_fail_keeps_account_offline(api_client, mock_redis):
    """When Alice Blue rejects the session probe (IP not whitelisted), account must stay Offline."""
    acc_resp = await api_client.post(
        "/api/v1/accounts",
        json={
            "account_type": "FOLLOWER",
            "display_name": "Follower IP Blocked",
            "alice_user_id": "AB_IP_BLOCKED",
            "api_secret": "secret_blocked",
        },
        headers=AUTH_HEADER,
    )
    acc = acc_resp.json()

    with patch.object(AliceBlueClient, "authenticate_with_auth_code", new_callable=AsyncMock) as mock_auth:
        mock_auth.side_effect = IPNotWhitelistedError(acc["id"], "VPS_IP", "IP not whitelisted")
        cb_resp = await api_client.post(
            "/api/v1/accounts/callback",
            json={
                "auth_code": "BAD_AUTH_CODE",
                "account_id": acc["id"],
                "target_user_id": "AB_IP_BLOCKED",
                "returned_user_id": "AB_IP_BLOCKED",
            },
            headers=AUTH_HEADER,
        )

    assert cb_resp.status_code == 403
    assert "IP restriction" in cb_resp.json()["detail"]

    # Account must remain Offline and Redis must not have a session key
    get_acc = await api_client.get(f"/api/v1/accounts/{acc['id']}", headers=AUTH_HEADER)
    assert get_acc.json()["session_valid"] is False
    assert mock_redis._storage.get(f"alice:session:{acc['id']}") is None


@pytest.mark.asyncio
async def test_renew_session_with_redis_token(api_client, mock_redis):
    acc_resp = await api_client.post(
        "/api/v1/accounts",
        json={
            "account_type": "FOLLOWER",
            "display_name": "Follower Renew Test",
            "alice_user_id": "AB_RENEW_1",
            "api_secret": "secret_renew",
        },
        headers=AUTH_HEADER,
    )
    acc = acc_resp.json()

    # Pre-set mock redis key so renew passes
    await mock_redis.set(f"alice:session:{acc['id']}", "ACTIVE_SESSION_TOKEN", ex=18 * 3600)

    renew_resp = await api_client.post(
        f"/api/v1/accounts/{acc['id']}/renew-session",
        headers=AUTH_HEADER,
    )
    assert renew_resp.status_code == 200
    assert renew_resp.json()["session_valid"] is True


# ---------------------------------------------------------------------------
# Demat card metrics (get_all_account_metrics) - master own-order visibility
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_metrics_reflects_master_own_order_activity(api_client, mock_redis):
    """
    CopyTrade.follower_account_id is never a master's own id (it only ever
    records follower-side copies), so before this fix a master account's
    Demat card metrics always showed 0 orders/pending/completed regardless
    of real order activity - whether placed via Jettone's UI or externally
    via Alice Blue Mobile/Web. get_all_account_metrics must also read the
    master's own Order rows.
    """
    import uuid
    from decimal import Decimal
    from app.models.trades import Order, OrderSide, OrderSource, OrderStatus

    master_resp = await api_client.post(
        "/api/v1/accounts",
        json={
            "account_type": "MASTER",
            "display_name": "Metrics Master",
            "alice_user_id": "AB_METRICS_MASTER",
            "api_secret": "secret_master",
        },
        headers=AUTH_HEADER,
    )
    assert master_resp.status_code == 201
    master = master_resp.json()

    async with TestingSessionLocal() as session:
        session.add(Order(
            id=uuid.uuid4(),
            internal_order_id="EXT_TEST_METRICS_1",
            broker_order_id="BRK_METRICS_1",
            account_id=uuid.UUID(master["id"]),
            source=OrderSource.BROKER_EXTERNAL,
            broker="ALICE_BLUE",
            exchange="NSE",
            symbol="RELIANCE-EQ",
            side=OrderSide.BUY,
            order_type="MARKET",
            product_type="MIS",
            quantity=10,
            filled_quantity=10,
            pending_quantity=0,
            price=Decimal("2950.00"),
            average_price=Decimal("2950.00"),
            status=OrderStatus.COMPLETE,
        ))
        session.add(Order(
            id=uuid.uuid4(),
            internal_order_id="EXT_TEST_METRICS_2",
            broker_order_id="BRK_METRICS_2",
            account_id=uuid.UUID(master["id"]),
            source=OrderSource.INTERNAL_UI,
            broker="ALICE_BLUE",
            exchange="NSE",
            symbol="TCS-EQ",
            side=OrderSide.BUY,
            order_type="LIMIT",
            product_type="MIS",
            quantity=5,
            filled_quantity=0,
            pending_quantity=5,
            price=Decimal("3900.00"),
            status=OrderStatus.OPEN,
        ))
        await session.commit()

    app.dependency_overrides[get_broker_client_provider] = _no_broker_clients
    metrics_resp = await api_client.get("/api/v1/accounts/metrics", headers=AUTH_HEADER)
    assert metrics_resp.status_code == 200
    m = metrics_resp.json()["metrics"][master["id"]]
    assert m["orders_count"] == 2
    assert m["completed_count"] == 1
    assert m["pending_count"] == 1
    # The old turnover "pnl" (29500 for a single BUY) is gone; P&L comes only
    # from broker fills, and with no broker client it is unknown, not zero.
    assert "pnl" not in m and "positions_count" not in m
    assert m["pnl_status"] == "UNAVAILABLE"
    assert m["total_pnl"] is None


@pytest.mark.asyncio
async def test_metrics_does_not_double_count_follower_own_order_rows(api_client, mock_redis):
    """
    A follower's own SYSTEM-sourced Order row (created by the copy engine)
    must NOT be added on top of its CopyTrade-derived counts - the Order-
    table query is scoped to MASTER accounts only specifically to avoid this.
    """
    import uuid
    from decimal import Decimal
    from app.models.trades import CopyTrade, CopyTradeStatus, Order, OrderSide, OrderSource, OrderStatus

    follower_resp = await api_client.post(
        "/api/v1/accounts",
        json={
            "account_type": "FOLLOWER",
            "display_name": "Metrics Follower",
            "alice_user_id": "AB_METRICS_FOLLOWER",
            "api_secret": "secret_follower",
        },
        headers=AUTH_HEADER,
    )
    assert follower_resp.status_code == 201
    follower = follower_resp.json()
    follower_uuid = uuid.UUID(follower["id"])

    async with TestingSessionLocal() as session:
        session.add(CopyTrade(
            id=uuid.uuid4(),
            master_order_id="M_METRICS_1",
            follower_account_id=follower_uuid,
            copy_event_id=uuid.uuid4(),
            symbol="RELIANCE-EQ",
            exchange="NSE",
            side=OrderSide.BUY,
            quantity=10,
            product_type="MIS",
            estimated_price=Decimal("2950.00"),
            client_order_id="JT__METRICS_1",
            status=CopyTradeStatus.SUCCESS,
        ))
        session.add(Order(
            id=uuid.uuid4(),
            internal_order_id="JT_CH_METRICS_1",
            broker_order_id="BRK_FOLLOWER_METRICS_1",
            account_id=follower_uuid,
            source=OrderSource.SYSTEM,
            broker="ALICE_BLUE",
            exchange="NSE",
            symbol="RELIANCE-EQ",
            side=OrderSide.BUY,
            order_type="MARKET",
            product_type="MIS",
            quantity=10,
            filled_quantity=10,
            pending_quantity=0,
            price=Decimal("2950.00"),
            status=OrderStatus.COMPLETE,
        ))
        await session.commit()

    app.dependency_overrides[get_broker_client_provider] = _no_broker_clients
    metrics_resp = await api_client.get("/api/v1/accounts/metrics", headers=AUTH_HEADER)
    assert metrics_resp.status_code == 200
    m = metrics_resp.json()["metrics"][follower["id"]]
    assert m["orders_count"] == 1
    assert m["completed_count"] == 1


@pytest.mark.asyncio
async def test_metrics_orders_count_always_equals_the_bucket_sum(api_client, mock_redis):
    """A CopyTrade left in SUBMITTED or RECONCILIATION_REQUIRED (the engine
    couldn't confirm its outcome, e.g. after a crash-recovery reconciliation
    flag) was counted into orders_count but fell through every P/C/R/Cancel
    bucket, so the Demat card's headline "Orders" number silently exceeded
    its own breakdown - production case: a follower showed Orders 7 but
    Pending+Complete+Reject+Cancel only summed to 5. Both statuses must land
    in needs_review_count, and the buckets must always foot to the total."""
    import uuid
    from decimal import Decimal
    from app.models.trades import CopyTrade, CopyTradeStatus, OrderSide

    follower = await _create_account(api_client, "FOLLOWER", "Needs Review Follower", "AB_NEEDS_REVIEW")
    follower_uuid = uuid.UUID(follower["id"])

    async with TestingSessionLocal() as session:
        for i, status in enumerate([CopyTradeStatus.SUCCESS, CopyTradeStatus.SUBMITTED, CopyTradeStatus.RECONCILIATION_REQUIRED]):
            session.add(CopyTrade(
                id=uuid.uuid4(),
                master_order_id=f"M_NEEDS_REVIEW_{i}",
                follower_account_id=follower_uuid,
                copy_event_id=uuid.uuid4(),
                symbol="RELIANCE-EQ",
                exchange="NSE",
                side=OrderSide.BUY,
                quantity=10,
                product_type="MIS",
                estimated_price=Decimal("2950.00"),
                client_order_id=f"JT__NEEDS_REVIEW_{i}",
                status=status,
            ))
        await session.commit()

    app.dependency_overrides[get_broker_client_provider] = _no_broker_clients
    metrics_resp = await api_client.get("/api/v1/accounts/metrics", headers=AUTH_HEADER)
    m = metrics_resp.json()["metrics"][follower["id"]]
    assert m["orders_count"] == 3
    assert m["completed_count"] == 1
    assert m["needs_review_count"] == 2
    bucket_sum = m["pending_count"] + m["completed_count"] + m["rejected_count"] + m["cancelled_count"] + m["needs_review_count"]
    assert bucket_sum == m["orders_count"]


def _no_broker_clients():
    return lambda _account_id: None


async def _create_account(api_client, account_type, name, alice_user_id):
    resp = await api_client.post(
        "/api/v1/accounts",
        json={
            "account_type": account_type,
            "display_name": name,
            "alice_user_id": alice_user_id,
            "api_secret": f"secret_{alice_user_id}",
        },
        headers=AUTH_HEADER,
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.mark.asyncio
async def test_metrics_pnl_comes_from_each_accounts_own_broker_fills(api_client, mock_redis):
    """
    Regression for the +4200.16 bug: the Demat card showed turnover (price x
    quantity summed with buys and sells both positive) as P&L for both
    accounts, with the child priced from master-derived copy_trades
    estimated_price. Each account's P&L must come from its own broker fills -
    master -1.52, child -1.48 on the 2026-09-11 CANBK live test.
    """
    import uuid
    from decimal import Decimal
    from app.core.trading_day import trading_day
    from app.models.trades import CopyTrade, CopyTradeStatus, OrderSide
    from pnl_live_fixtures import (
        CHILD_ORDER_BOOK,
        CHILD_POSITIONS,
        MASTER_ORDER_BOOK,
        MASTER_POSITIONS,
        FakeBrokerClient,
        on_day,
    )

    master = await _create_account(api_client, "MASTER", "PnL Master", "AB_PNL_MASTER")
    child = await _create_account(api_client, "FOLLOWER", "PnL Child", "AB_PNL_CHILD")

    # A master-priced copy_trade on the child must not influence its P&L.
    async with TestingSessionLocal() as session:
        session.add(CopyTrade(
            master_order_id="26091100064718",
            follower_account_id=uuid.UUID(child["id"]),
            copy_event_id=uuid.uuid4(),
            symbol="CANBK",
            exchange="NSE",
            side=OrderSide.BUY,
            quantity=1,
            product_type="INTRADAY",
            estimated_price=Decimal("123.54"),
            client_order_id="JT__API_PNL_1",
            status=CopyTradeStatus.SUCCESS,
        ))
        await session.commit()

    day = trading_day()
    clients = {
        master["id"]: FakeBrokerClient(on_day(MASTER_ORDER_BOOK, day), MASTER_POSITIONS),
        child["id"]: FakeBrokerClient(on_day(CHILD_ORDER_BOOK, day), CHILD_POSITIONS),
    }
    app.dependency_overrides[get_broker_client_provider] = lambda: clients.get

    resp = await api_client.get("/api/v1/accounts/metrics", headers=AUTH_HEADER)
    assert resp.status_code == 200
    assert "4200.16" not in resp.text
    metrics = resp.json()["metrics"]
    m, c = metrics[master["id"]], metrics[child["id"]]

    assert (m["realized_pnl"], m["unrealized_pnl"], m["total_pnl"]) == ("-1.52", "0.00", "-1.52")
    assert (c["realized_pnl"], c["unrealized_pnl"], c["total_pnl"]) == ("-1.48", "0.00", "-1.48")
    for row in (m, c):
        assert "pnl" not in row and "positions_count" not in row
        assert row["pnl_basis"] == "GROSS"
        assert row["pnl_status"] == "OK"
        assert row["open_positions_count"] == 0
        assert row["broker_check"]["status"] == "MATCH"
        assert row["trading_day"] == day.isoformat()
    assert c["orders_count"] == 1 and c["completed_count"] == 1  # counts are unchanged


@pytest.mark.asyncio
async def test_metrics_pnl_is_null_not_zero_when_broker_unavailable(api_client):
    master = await _create_account(api_client, "MASTER", "PnL Offline", "AB_PNL_OFFLINE")
    app.dependency_overrides[get_broker_client_provider] = _no_broker_clients

    resp = await api_client.get("/api/v1/accounts/metrics", headers=AUTH_HEADER)
    assert resp.status_code == 200
    m = resp.json()["metrics"][master["id"]]
    assert m["pnl_status"] == "UNAVAILABLE"
    assert (m["realized_pnl"], m["unrealized_pnl"], m["total_pnl"]) == (None, None, None)
    assert m["open_positions_count"] is None
    assert m["broker_check"]["status"] == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_metrics_counts_use_the_ist_trading_day(api_client):
    import uuid
    from datetime import timedelta
    from decimal import Decimal
    from app.core.trading_day import trading_day, trading_day_start_utc
    from app.models.trades import Order, OrderSide, OrderSource, OrderStatus

    master = await _create_account(api_client, "MASTER", "Counts Master", "AB_COUNTS_MASTER")
    start = trading_day_start_utc(trading_day())

    async with TestingSessionLocal() as session:
        for suffix, created_at in (("PREV", start - timedelta(minutes=10)), ("TODAY", start + timedelta(minutes=10))):
            session.add(Order(
                id=uuid.uuid4(),
                internal_order_id=f"EXT_IST_{suffix}",
                broker_order_id=f"BRK_IST_{suffix}",
                account_id=uuid.UUID(master["id"]),
                source=OrderSource.BROKER_EXTERNAL,
                broker="ALICE_BLUE",
                exchange="NSE",
                symbol="CANBK",
                side=OrderSide.BUY,
                order_type="MARKET",
                product_type="INTRADAY",
                quantity=1,
                filled_quantity=1,
                pending_quantity=0,
                price=Decimal("123.10"),
                average_price=Decimal("123.10"),
                status=OrderStatus.COMPLETE,
                created_at=created_at,
            ))
        await session.commit()

    app.dependency_overrides[get_broker_client_provider] = _no_broker_clients
    resp = await api_client.get("/api/v1/accounts/metrics", headers=AUTH_HEADER)
    m = resp.json()["metrics"][master["id"]]
    assert m["orders_count"] == 1  # 23:50 IST yesterday is not today
    assert m["completed_count"] == 1
