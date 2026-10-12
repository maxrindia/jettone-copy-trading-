"""R3 hardening tests:
- deterministic copy_event_id / idempotency without Redis
- credential encryption at rest
- fail-closed funds fetch
- session-expiry re-auth retry
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.core.crypto import decrypt_secret, encrypt_secret
from app.models.accounts import Account, AccountType, CopySetting, ApiCredential
from app.models.trades import CopyTrade
from app.services.alice_blue.client import AliceBlueClient, SessionExpiredError
from app.services.alice_blue.types import FundsData
from app.services.copy_engine.events import MasterTradeEvent, derive_copy_event_id
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from strategy_helpers import SAMPLE_MASTER_ACCOUNT_ID


def _make_event(master_order_id="ORD_R3_1", fill_sequence=1):
    return MasterTradeEvent(
        account_id=SAMPLE_MASTER_ACCOUNT_ID,
        master_order_id=master_order_id,
        symbol="NIFTY",
        exchange="NFO",
        side="BUY",
        quantity=25,
        price=Decimal("100.00"),
        product_type="MIS",
        timestamp=datetime.now(timezone.utc),
        fill_sequence=fill_sequence,
    )


def _make_funds(account_id="a"):
    return FundsData(
        account_id=account_id,
        available_margin=Decimal("500000"),
        collateral=Decimal("0"),
        used_margin=Decimal("0"),
        updated_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Deterministic idempotency
# ---------------------------------------------------------------------------

def test_derive_copy_event_id_is_deterministic():
    a1 = derive_copy_event_id("ORD_X", "acc-1", 1)
    a2 = derive_copy_event_id("ORD_X", "acc-1", 1)
    assert a1 == a2


def test_derive_copy_event_id_varies_by_follower_and_sequence():
    base = derive_copy_event_id("ORD_X", "acc-1", 1)
    assert base != derive_copy_event_id("ORD_X", "acc-2", 1)
    assert base != derive_copy_event_id("ORD_X", "acc-1", 2)


@pytest.mark.asyncio
async def test_duplicate_event_with_empty_redis_still_yields_one_row(mock_redis, db_session, sample_accounts, sample_strategy):
    """
    Even when Redis dedup is completely bypassed (fresh/flushed Redis),
    the deterministic copy_event_id makes the DB unique constraint reject
    the redelivered event -> exactly one copy_trades row per follower.
    """
    sample_copy_settings_enabled = CopySetting(
        follower_account_id=sample_accounts["follower1"].id,
        copy_enabled=True,
        quantity_mode="FIXED",
        fixed_qty=25,
    )
    db_session.add(sample_copy_settings_enabled)
    await db_session.commit()

    mock_client = AsyncMock()
    mock_client.get_funds.return_value = _make_funds()

    from app.services.alice_blue.types import OrderResult, OrderResultCategory

    async def _successful_place(order):
        from app.services.alice_blue.types import OrderRequest

        return OrderResult(
            category=OrderResultCategory.SUCCESS,
            client_order_id=order.client_order_id,
            broker_order_id="BRK_R3_OK",
            message="Order placed successfully",
            raw_response={"status": "success"},
            timestamp=datetime.now(timezone.utc),
        )

    mock_client.place_order = _successful_place

    validator = EventValidator(redis=mock_redis)
    pipeline = CopyEnginePipeline(validator=validator)
    event = _make_event()

    fid = str(sample_accounts["follower1"].id)
    res1 = await pipeline.execute_replication(event=event, clients={fid: mock_client})
    assert res1["status"] == "completed"
    assert res1["results"][fid]["status"] == "SUCCESS"

    # Simulate Redis flush: brand-new empty mock storage
    for key in list(mock_redis._storage.keys()):
        del mock_redis._storage[key]

    res2 = await pipeline.execute_replication(event=event, clients={fid: mock_client})
    assert res2["results"][fid]["reason"] == "DUPLICATE_EVENT"

    rows = (
        await db_session.execute(
            select(CopyTrade).where(
                CopyTrade.master_order_id == "ORD_R3_1",
                CopyTrade.follower_account_id == sample_accounts["follower1"].id,
            )
        )
    ).scalars().all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Credential encryption at rest
# ---------------------------------------------------------------------------

def test_encrypt_decrypt_roundtrip():
    secret = "my-broker-api-secret-123"
    ciphertext = encrypt_secret(secret)
    assert ciphertext != secret
    assert "secret" not in ciphertext.lower()
    assert decrypt_secret(ciphertext) == secret


def test_tampered_ciphertext_raises():
    from app.core.crypto import EncryptionKeyError

    ciphertext = encrypt_secret("another-secret")
    tampered = ciphertext[:-6] + ("AAAAAA" if not ciphertext.endswith("AAAAAA") else "BBBBBB")
    with pytest.raises(EncryptionKeyError):
        decrypt_secret(tampered)


@pytest.mark.asyncio
async def test_api_credential_model_roundtrips_through_db(db_session, sample_accounts):
    cred = ApiCredential(
        account_id=sample_accounts["follower1"].id,
        encrypted_api_secret=encrypt_secret("plaintext-secret"),
    )
    db_session.add(cred)
    await db_session.commit()

    loaded = (
        await db_session.execute(select(ApiCredential).where(ApiCredential.account_id == sample_accounts["follower1"].id))
    ).scalar_one()
    assert loaded.encrypted_api_secret != "plaintext-secret"
    assert decrypt_secret(loaded.encrypted_api_secret) == "plaintext-secret"


# ---------------------------------------------------------------------------
# Fail-closed funds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_funds_fetch_failure_rejects_copy(db_session, sample_accounts, sample_strategy):
    setting = CopySetting(
        follower_account_id=sample_accounts["follower1"].id,
        copy_enabled=True,
        quantity_mode="FIXED",
        fixed_qty=25,
    )
    db_session.add(setting)
    await db_session.commit()

    failing_client = AsyncMock()
    failing_client.get_funds.side_effect = RuntimeError("broker unreachable")

    mock_validator_redis = AsyncMock()
    mock_validator_redis.set.return_value = True
    validator = EventValidator(redis=mock_validator_redis)
    pipeline = CopyEnginePipeline(validator=validator)

    res = await pipeline.execute_replication(
        event=_make_event(),
        clients={str(sample_accounts["follower1"].id): failing_client},
    )

    fid = str(sample_accounts["follower1"].id)
    assert res["results"][fid]["status"] == "REJECTED"
    assert "FUNDS_FETCH_FAILED" in res["results"][fid]["reasons"]
    failing_client.place_order.assert_not_called()


# ---------------------------------------------------------------------------
# Session-expiry re-auth retry
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


@pytest.mark.asyncio
async def test_reauth_retry_recovers_from_expired_session(mock_redis):
    client = AliceBlueClient(
        account_id="acc-r3",
        user_id="USER1",
        api_secret="secret",
        redis=mock_redis,
    )

    auth_calls = []

    async def fake_get_session(force_refresh=False):
        auth_calls.append(force_refresh)
        return "token"

    client.get_session = fake_get_session

    responses = [
        _FakeResponse(401, {"emsg": "Session expired"}),
        _FakeResponse(200, {"net": "100000", "collateral": "0", "utilisedAmount": "0"}),
    ]

    async def fake_request(method, url, **kwargs):
        return responses.pop(0)

    client._http = AsyncMock()
    client._http.request = AsyncMock(side_effect=fake_request)

    funds = await client.get_funds(max_cache_age_sec=0)

    assert funds.available_margin == Decimal("100000")
    assert auth_calls.count(True) == 1
    assert auth_calls[1] is True


@pytest.mark.asyncio
async def test_second_auth_failure_raises_session_expired(mock_redis):
    client = AliceBlueClient(
        account_id="acc-r3b",
        user_id="USER1",
        api_secret="secret",
        redis=mock_redis,
    )

    async def fake_get_session(force_refresh=False):
        return "token"

    client.get_session = fake_get_session

    responses = [
        _FakeResponse(401, {"emsg": "Session expired"}),
        _FakeResponse(401, {"emsg": "Session expired"}),
    ]

    async def fake_request(method, url, **kwargs):
        return responses.pop(0)

    client._http = AsyncMock()
    client._http.request = AsyncMock(side_effect=fake_request)

    with pytest.raises(SessionExpiredError):
        await client.get_positions()
