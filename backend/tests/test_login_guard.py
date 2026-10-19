"""
Cross-instance login protection (Redis-backed) for the explicit Groww / Angel One login path.

POST /accounts/{id}/broker-login builds a fresh client per request, so protection must live in Redis.
All tests use the TTL-aware fake Redis (virtual clock: mock_redis._advance) and mocked broker transports -
no real login, no network.
"""
import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from app.services.brokers import login_guard
from app.services.brokers.login_guard import LoginThrottled, run_guarded_login
from app.services.groww.client import GROWW_DAILY_LOGIN_CAP, GrowwAuthError
from app.services.angel_one.client import AngelAuthError
import angel_helpers as A
import groww_helpers as G
from test_groww_foundation import (  # noqa: F401  (fixtures and helpers shared with the foundation tests)
    TOKEN, SECRETS, client, flag_on, _groww_account, _no_secrets,
)

ACC = "acc-groww"
LOCK = f"login:lock:groww:{ACC}"
LAST = f"login:last:groww:{ACC}"
COUNT_PREFIX = f"login:count:groww:{ACC}:"
SESSION = f"groww:session:{ACC}"


@pytest.fixture(autouse=True)
def quick_joins(monkeypatch):
    """Joiners poll every 50 ms for up to 15 s; shrink that so timeout paths finish instantly."""
    monkeypatch.setattr(login_guard, "JOIN_WAIT_SECONDS", 0.4)
    monkeypatch.setattr(login_guard, "POLL_INTERVAL_SECONDS", 0.01)


def fresh_groww(redis, fake):
    return G.make_client(redis, fake)


def token_requests(fake):
    return len(fake.calls("/v1/token/api/access"))


# ── concurrency across freshly constructed instances ────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("callers", [2, 3, 10])
async def test_concurrent_logins_on_fresh_instances_send_exactly_one_token_request(mock_redis, callers):
    fake = G.FakeGroww()
    clients = [fresh_groww(mock_redis, fake) for _ in range(callers)]      # what POST /broker-login does per request
    await asyncio.gather(*(c.login() for c in clients))
    assert token_requests(fake) == 1
    assert SESSION in mock_redis._storage and LOCK not in mock_redis._storage
    for c in clients:
        assert await c.get_session() == "TOK-1"                           # every caller ends with the shared session


@pytest.mark.asyncio
async def test_concurrent_angel_logins_send_exactly_one_login_request(mock_redis):
    fake = A.FakeAngel()
    clients = [A.make_client(mock_redis, fake) for _ in range(5)]
    await asyncio.gather(*(c.login() for c in clients))
    assert len(fake.calls("loginByPassword")) == 1                        # one TOTP use, not five
    assert "login:lock:angelone:acc-angel" not in mock_redis._storage


@pytest.mark.asyncio
async def test_a_waiting_caller_never_sends_its_own_request_when_the_holder_fails(mock_redis):
    fake = G.FakeGroww()
    fake.token_response = (401, {"status": "FAILURE"})
    clients = [fresh_groww(mock_redis, fake) for _ in range(4)]
    results = await asyncio.gather(*(c.login() for c in clients), return_exceptions=True)
    assert token_requests(fake) == 1
    kinds = sorted(type(r).__name__ for r in results)
    assert kinds.count("GrowwAuthError") == 1 and kinds.count("LoginThrottled") == 3
    assert LOCK not in mock_redis._storage


# ── spacing ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_fresh_instance_right_after_a_successful_login_is_satisfied_without_a_request(mock_redis):
    fake = G.FakeGroww()
    await fresh_groww(mock_redis, fake).login()
    again = fresh_groww(mock_redis, fake)
    await again.login()
    assert token_requests(fake) == 1 and await again.get_session() == "TOK-1"


@pytest.mark.asyncio
async def test_a_retry_after_a_failed_login_is_throttled_until_the_window_passes(mock_redis):
    fake = G.FakeGroww()
    fake.token_response = (401, {"status": "FAILURE"})
    with pytest.raises(GrowwAuthError):
        await fresh_groww(mock_redis, fake).login()
    with pytest.raises(LoginThrottled) as ei:
        await fresh_groww(mock_redis, fake).login()
    assert ei.value.code == "LOGIN_TOO_SOON" and 1 <= ei.value.retry_after <= 10
    assert token_requests(fake) == 1                                      # the second attempt never reached Groww
    mock_redis._advance(11)
    fake.token_response = G.TOKEN_OK
    await fresh_groww(mock_redis, fake).login()
    assert token_requests(fake) == 2


@pytest.mark.asyncio
async def test_relogin_after_the_session_vanished_waits_for_the_window(mock_redis):
    fake = G.FakeGroww()
    await fresh_groww(mock_redis, fake).login()
    mock_redis._storage.pop(SESSION)                                      # e.g. the token was rejected
    with pytest.raises(LoginThrottled):
        await fresh_groww(mock_redis, fake).login()
    mock_redis._advance(11)
    await fresh_groww(mock_redis, fake).login()
    assert token_requests(fake) == 2


# ── lock lifecycle ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_lock_is_released_after_success_and_after_failure(mock_redis):
    fake = G.FakeGroww()
    await fresh_groww(mock_redis, fake).login()
    assert LOCK not in mock_redis._storage
    mock_redis._advance(11)
    mock_redis._storage.pop(SESSION)
    fake.token_response = (403, {})
    with pytest.raises(GrowwAuthError):
        await fresh_groww(mock_redis, fake).login()
    assert LOCK not in mock_redis._storage


@pytest.mark.asyncio
async def test_a_live_lock_held_by_another_request_is_never_overridden(mock_redis):
    fake = G.FakeGroww()
    await mock_redis.set(LOCK, "someone-elses-token", ex=30)
    with pytest.raises(LoginThrottled) as ei:
        await fresh_groww(mock_redis, fake).login()
    assert ei.value.code == "LOGIN_IN_PROGRESS" and ei.value.retry_after == 30
    assert token_requests(fake) == 0
    assert mock_redis._storage[LOCK] == "someone-elses-token"             # untouched


@pytest.mark.asyncio
async def test_a_crashed_holders_lock_expires_and_the_next_login_proceeds(mock_redis):
    fake = G.FakeGroww()
    await mock_redis.set(LOCK, "crashed-holder", ex=30)
    mock_redis._advance(31)                                               # the holder died; its lock TTL ran out
    await fresh_groww(mock_redis, fake).login()
    assert token_requests(fake) == 1 and SESSION in mock_redis._storage


@pytest.mark.asyncio
async def test_a_holder_never_deletes_a_lock_that_now_belongs_to_someone_else(mock_redis):
    async def slow_login():
        mock_redis._advance(31)                                           # our lock expired mid-login ...
        await mock_redis.set(LOCK, "second-holder", ex=30, nx=True)       # ... and another request took it
    await run_guarded_login(redis=mock_redis, broker="groww", account_id=ACC, clock=lambda: G.FIXED_NOW,
                            do_login=slow_login, session_key=SESSION)
    assert mock_redis._storage[LOCK] == "second-holder"


# ── daily cap ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_groww_daily_cap_is_120_and_counts_every_attempt(mock_redis):
    assert GROWW_DAILY_LOGIN_CAP == 120
    fake = G.FakeGroww()
    fake.token_response = (401, {"status": "FAILURE"})
    count_key = COUNT_PREFIX + "2026-10-16"
    mock_redis._storage[count_key] = "119"                                # 119 attempts already today
    with pytest.raises(GrowwAuthError):                                   # the 120th is allowed (and fails at the broker)
        await fresh_groww(mock_redis, fake).login()
    assert token_requests(fake) == 1 and mock_redis._storage[count_key] == "120"
    mock_redis._advance(11)
    with pytest.raises(LoginThrottled) as ei:                             # the 121st is refused locally
        await fresh_groww(mock_redis, fake).login()
    assert ei.value.code == "LOGIN_DAILY_CAP" and ei.value.retry_after > 3600
    assert token_requests(fake) == 1


@pytest.mark.asyncio
async def test_the_daily_count_lives_in_redis_so_new_clients_cannot_reset_it(mock_redis):
    fake = G.FakeGroww()
    fake.token_response = (401, {"status": "FAILURE"})
    for _ in range(3):
        mock_redis._advance(11)
        with pytest.raises(GrowwAuthError):
            await fresh_groww(mock_redis, fake).login()                   # a brand-new client each time
    assert mock_redis._storage[COUNT_PREFIX + "2026-10-16"] == "3"


@pytest.mark.asyncio
async def test_the_daily_count_rolls_over_with_the_ist_date(mock_redis):
    async def noop():
        return None
    for clock_value in (datetime(2026, 10, 16, 4, 0, tzinfo=timezone.utc), datetime(2026, 10, 17, 4, 0, tzinfo=timezone.utc)):
        mock_redis._advance(11)
        await run_guarded_login(redis=mock_redis, broker="groww", account_id=ACC, clock=lambda c=clock_value: c,
                                do_login=noop, session_key=SESSION, daily_cap=1)
    assert mock_redis._storage[COUNT_PREFIX + "2026-10-16"] == "1" and mock_redis._storage[COUNT_PREFIX + "2026-10-17"] == "1"


@pytest.mark.asyncio
async def test_angel_one_has_no_daily_cap_but_keeps_the_spacing(mock_redis):
    fake = A.FakeAngel()
    fake.routes["loginByPassword"] = (200, {"status": False, "message": "Invalid totp"})
    for _ in range(30):
        mock_redis._advance(11)
        with pytest.raises(AngelAuthError):
            await A.make_client(mock_redis, fake).login()
    assert len(fake.calls("loginByPassword")) == 30
    assert not [k for k in mock_redis._storage if k.startswith("login:count:angelone")]
    with pytest.raises(LoginThrottled):                                   # but no hammering inside the window
        await A.make_client(mock_redis, fake).login()
    assert len(fake.calls("loginByPassword")) == 30


# ── Redis unavailable: fail closed ──────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["set", "get", "incr", "expire"])
async def test_redis_failures_before_the_login_mean_no_login(mock_redis, method):
    fake = G.FakeGroww()
    c = fresh_groww(mock_redis, fake)

    async def down(*a, **k):
        raise ConnectionError("redis down at 10.9.8.7:6379")
    getattr(mock_redis, method).side_effect = down
    with pytest.raises(LoginThrottled) as ei:
        await c.login()
    assert ei.value.code == "LOGIN_GUARD_UNAVAILABLE" and "10.9.8.7" not in ei.value.message
    assert token_requests(fake) == 0


@pytest.mark.asyncio
async def test_redis_failing_while_checking_for_a_recent_session_means_no_login(mock_redis):
    fake = G.FakeGroww()
    await fresh_groww(mock_redis, fake).login()                           # leaves the 'recent success' marker behind

    async def down(*a, **k):
        raise ConnectionError("redis down")
    mock_redis.exists.side_effect = down
    with pytest.raises(LoginThrottled) as ei:
        await fresh_groww(mock_redis, fake).login()
    assert ei.value.code == "LOGIN_GUARD_UNAVAILABLE" and token_requests(fake) == 1


@pytest.mark.asyncio
async def test_a_missing_redis_client_means_no_login():
    with pytest.raises(LoginThrottled) as ei:
        await run_guarded_login(redis=None, broker="groww", account_id=ACC, clock=lambda: G.FIXED_NOW,
                                do_login=lambda: None, session_key=SESSION)
    assert ei.value.code == "LOGIN_GUARD_UNAVAILABLE"


@pytest.mark.asyncio
async def test_a_failure_writing_the_completion_marker_does_not_undo_a_successful_login(mock_redis):
    fake = G.FakeGroww()
    c = fresh_groww(mock_redis, fake)
    original = mock_redis.incr.side_effect

    async def broken(key):
        if str(key).startswith("login:gen"):
            raise ConnectionError("redis hiccup")
        return await original(key)
    mock_redis.incr.side_effect = broken
    await c.login()                                                       # no exception: the session exists
    assert token_requests(fake) == 1 and await c.get_session() == "TOK-1"


@pytest.mark.asyncio
async def test_throttle_messages_are_fixed_text_without_identifiers_or_secrets(mock_redis):
    fake = G.FakeGroww()
    fake.token_response = (401, {"status": "FAILURE"})
    with pytest.raises(GrowwAuthError):
        await fresh_groww(mock_redis, fake).login()
    with pytest.raises(LoginThrottled) as ei:
        await fresh_groww(mock_redis, fake).login()
    for forbidden in (ACC, "GROWW-API", "login:", "redis", "TOK"):
        assert forbidden.lower() not in ei.value.message.lower()


# ── API level ───────────────────────────────────────────────────────────────

def patch_builder(monkeypatch, redis, fake):
    monkeypatch.setattr("app.api.accounts.build_groww_client",
                        lambda a, r, http_client=None: G.make_client(r, fake, account_id=str(a.id)))


@pytest.mark.asyncio
async def test_concurrent_broker_login_posts_send_one_token_request(client, db_session, mock_redis, flag_on, monkeypatch):
    acc = await _groww_account(db_session)
    fake = G.FakeGroww()
    patch_builder(monkeypatch, mock_redis, fake)
    responses = await asyncio.gather(*(client.post(f"/api/v1/accounts/{acc.id}/broker-login", headers=TOKEN) for _ in range(5)))
    assert [r.status_code for r in responses] == [200] * 5
    assert token_requests(fake) == 1
    for r in responses:
        _no_secrets(r.text)
    again = await client.post(f"/api/v1/accounts/{acc.id}/broker-login", headers=TOKEN)   # a later click: still no new request
    assert again.status_code == 200 and token_requests(fake) == 1


@pytest.mark.asyncio
async def test_a_throttled_broker_login_is_a_429_with_retry_after_and_no_session_change(client, db_session, mock_redis, flag_on, monkeypatch):
    acc = await _groww_account(db_session)
    fake = G.FakeGroww()
    fake.token_response = (401, {"status": "FAILURE"})
    patch_builder(monkeypatch, mock_redis, fake)
    first = await client.post(f"/api/v1/accounts/{acc.id}/broker-login", headers=TOKEN)
    assert first.status_code == 401 and "approval" in first.text
    second = await client.post(f"/api/v1/accounts/{acc.id}/broker-login", headers=TOKEN)
    assert second.status_code == 429
    assert 1 <= int(second.headers["Retry-After"]) <= 10
    assert "moments ago" in second.json()["detail"]
    for forbidden in (str(acc.id), "login:", "groww:session", "GROWW-API"):
        assert forbidden not in second.text
    assert token_requests(fake) == 1


@pytest.mark.asyncio
async def test_the_daily_cap_is_a_429_with_a_long_retry_after(client, db_session, mock_redis, flag_on, monkeypatch):
    acc = await _groww_account(db_session)
    fake = G.FakeGroww()
    patch_builder(monkeypatch, mock_redis, fake)
    mock_redis._storage[f"login:count:groww:{acc.id}:2026-10-16"] = "120"
    resp = await client.post(f"/api/v1/accounts/{acc.id}/broker-login", headers=TOKEN)
    assert resp.status_code == 429 and int(resp.headers["Retry-After"]) > 3600
    assert token_requests(fake) == 0 and "daily login limit" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_redis_being_down_is_a_429_and_nothing_is_sent(client, db_session, mock_redis, flag_on, monkeypatch):
    acc = await _groww_account(db_session)
    fake = G.FakeGroww()
    patch_builder(monkeypatch, mock_redis, fake)
    original = mock_redis.set.side_effect

    async def down(key, *a, **k):
        if str(key).startswith("login:"):
            raise ConnectionError("redis down")
        return await original(key, *a, **k)
    mock_redis.set.side_effect = down
    resp = await client.post(f"/api/v1/accounts/{acc.id}/broker-login", headers=TOKEN)
    assert resp.status_code == 429 and "Retry-After" in resp.headers and token_requests(fake) == 0
    assert "redis" not in resp.text.lower()
