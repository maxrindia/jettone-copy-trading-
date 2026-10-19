"""
A stored broker session that cannot be read must never reach the broker, never leak, never raise an
untyped exception, and never be retried - and only that exact unreadable key may be discarded.

Both new adapters, every corruption mode, every operation. Offline: mocked transports + fake Redis.
"""
import json
from decimal import Decimal

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet

from app.core import crypto
from app.core.config import settings
from app.core.crypto import encrypt_secret
from app.services.alice_blue.types import OrderRequest, OrderResultCategory, OrderSide, OrderType, ProductType
from app.services.angel_one.client import AngelAuthError
from app.services.groww.client import GrowwAuthError
from app.services.reliability.retry import RetryPolicy
import angel_helpers as A
import groww_helpers as G

FOREIGN_KEY_BLOB = Fernet(Fernet.generate_key()).encrypt(b'{"token": "T", "jwt": "T"}').decode()

MODES = {
    "garbage_text": "not-a-fernet-token",
    "non_utf8_bytes": b"\xff\xfe\x00\x01",
    "valid_cipher_not_json": encrypt_secret("{{{ not json"),
    "valid_cipher_json_list": encrypt_secret('["a", "b"]'),
    "valid_cipher_no_token": encrypt_secret('{"other": 1}'),
    "valid_cipher_blank_token": encrypt_secret('{"token": "   ", "jwt": "   "}'),
    "valid_cipher_non_string_token": encrypt_secret('{"token": 123, "jwt": 123}'),
    "encrypted_with_another_key": FOREIGN_KEY_BLOB,
}
LEAKS = ("fernet", "invalidtoken", "ALICE_BLUE_ENCRYPTION_KEY".lower(), "traceback", "gAAAA", "not-a-fernet-token")


def order():
    return OrderRequest(symbol="SBIN", exchange="NSE", side=OrderSide.BUY, quantity=5, product_type=ProductType.MIS,
                        order_type=OrderType.LIMIT, price=Decimal("800.5"), instrument_token="99999",
                        client_order_id="JT_LIM_ORDER123_acc12345")


class Case:
    """Uniform wrapper so one test body covers Groww and Angel One."""

    def __init__(self, name):
        self.name = name

    async def make(self, redis):
        if self.name == "groww":
            self.fake = G.FakeGroww()
            self.client = G.make_client(redis, self.fake)
            self.auth_error = GrowwAuthError
            self.key = "groww:session:acc-groww"
        else:
            self.fake = A.FakeAngel()
            self.client = A.make_client(redis, self.fake)
            self.auth_error = AngelAuthError
            self.key = "angel:session:acc-angel"
        await self.client.login()
        self.baseline_requests = len(self.fake.requests)
        return self

    def corrupt(self, redis, value):
        redis._storage[self.key] = value

    def requests_since_login(self):
        return len(self.fake.requests) - self.baseline_requests


@pytest_asyncio.fixture(params=["groww", "angel"])
async def case(request, mock_redis):
    return await Case(request.param).make(mock_redis)


def assert_clean(text):
    low = str(text).lower()
    for leak in LEAKS:
        assert leak.lower() not in low, f"leaked {leak!r} in {text!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(MODES))
async def test_every_operation_fails_safely_without_a_broker_request(case, mock_redis, mode):
    c = case.client
    case.corrupt(mock_redis, MODES[mode])

    # order operations return a sanitized REJECTED result (never raise)
    res = await c.place_order(order())
    assert res.category == OrderResultCategory.REJECTED and res.broker_order_id is None
    assert_clean(res.message)
    # the session was discarded by the first failure; re-seed so each operation sees the corruption itself
    for op in ("cancel", "modify"):
        case.corrupt(mock_redis, MODES[mode])
        if op == "cancel":
            r = await c.cancel_order("X1")
        else:
            r = await c.modify_order("X1", 5, Decimal("801"), symbol="SBIN", exchange="NSE", product_type=ProductType.MIS)
        assert r.category == OrderResultCategory.REJECTED
        assert_clean(r.message)

    # reads: positions -> None; the rest raise the adapter's typed auth error, never TypeError/ValueError/KeyError
    case.corrupt(mock_redis, MODES[mode])
    assert await c.get_positions_or_none() is None
    for read in (c.get_positions, c.get_funds, c.get_order_book):
        case.corrupt(mock_redis, MODES[mode])
        with pytest.raises(case.auth_error) as ei:
            await read()
        assert_clean(ei.value)
        assert type(ei.value).__name__ in ("GrowwAuthError", "AngelAuthError")
    assert case.requests_since_login() == 0                               # not one HTTP request left the process


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", list(MODES))
async def test_only_the_exact_unreadable_key_is_discarded(case, mock_redis, mode):
    other_key = "groww:session:someone-else"
    mock_redis._storage[other_key] = "other account's good session"
    unrelated = "alice:session:acc-groww"
    mock_redis._storage[unrelated] = "alice token"
    case.corrupt(mock_redis, MODES[mode])
    with pytest.raises(case.auth_error):
        await case.client.get_session()
    assert case.key not in mock_redis._storage                            # the unreadable session is gone ...
    assert mock_redis._storage[other_key] == "other account's good session"   # ... and nothing else was touched
    assert mock_redis._storage[unrelated] == "alice token"


@pytest.mark.asyncio
async def test_a_session_replaced_by_a_fresh_login_is_not_erased(case, mock_redis):
    """The discard is compare-and-delete: if a concurrent login wrote a fresh session between the failed read
    and the cleanup, that fresh session survives."""
    stale = "garbage-that-was-read"
    case.corrupt(mock_redis, stale)
    mock_redis._storage[case.key] = "FRESH-SESSION-WRITTEN-BY-ANOTHER-REQUEST"
    await case.client._discard_unreadable_session(stale)
    assert mock_redis._storage[case.key] == "FRESH-SESSION-WRITTEN-BY-ANOTHER-REQUEST"
    await case.client._discard_unreadable_session("FRESH-SESSION-WRITTEN-BY-ANOTHER-REQUEST")
    assert case.key not in mock_redis._storage


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["garbage_text", "encrypted_with_another_key", "valid_cipher_not_json"])
async def test_a_local_session_error_is_not_retried_by_the_real_retry_policy(case, mock_redis, mode):
    case.corrupt(mock_redis, MODES[mode])
    session_reads = []
    original = mock_redis.get.side_effect

    async def counting(key):
        if key == case.key:
            session_reads.append(key)
        return await original(key)
    mock_redis.get.side_effect = counting
    res = await RetryPolicy(max_attempts=3, base_delay_sec=0.0).execute(case.client.place_order, order())
    assert res.category == OrderResultCategory.REJECTED
    assert len(session_reads) <= 2                                        # one read + the compare-and-delete, never 3 attempts
    assert case.requests_since_login() == 0


@pytest.mark.asyncio
async def test_a_missing_encryption_key_is_a_configuration_error_not_a_corrupt_session(case, mock_redis, monkeypatch):
    """With no usable encryption key nothing can be decrypted - that says nothing about the stored session, so
    it must be kept (the key may simply be restored) and the failure must say what is wrong, safely."""
    good = mock_redis._storage[case.key]
    monkeypatch.setattr(settings, "ALICE_BLUE_ENCRYPTION_KEY", None)
    crypto._get_fernet.cache_clear()
    try:
        with pytest.raises(case.auth_error) as ei:
            await case.client.get_session()
        assert "encryption key" in str(ei.value)
        assert mock_redis._storage[case.key] == good                      # kept
        res = await case.client.place_order(order())
        assert res.category == OrderResultCategory.REJECTED
        assert case.requests_since_login() == 0
    finally:
        monkeypatch.undo()
        crypto._get_fernet.cache_clear()


@pytest.mark.asyncio
async def test_the_session_store_being_down_is_a_typed_failure_with_no_request(case, mock_redis):
    original = mock_redis.get.side_effect

    async def down(key):
        if key == case.key:
            raise ConnectionError("redis is down at 10.0.0.5:6379")
        return await original(key)
    mock_redis.get.side_effect = down
    with pytest.raises(case.auth_error) as ei:
        await case.client.get_session()
    assert "10.0.0.5" not in str(ei.value)
    res = await case.client.place_order(order())
    assert res.category == OrderResultCategory.REJECTED and "10.0.0.5" not in res.message
    assert await case.client.get_positions_or_none() is None
    assert case.requests_since_login() == 0


@pytest.mark.asyncio
async def test_a_valid_session_still_works(case, mock_redis):
    assert await case.client.get_session() in ("TOK-1", "JWT1")
    res = await case.client.place_order(order())
    assert res.category == OrderResultCategory.SUCCESS

