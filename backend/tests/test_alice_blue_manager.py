"""
P2 Finding #10 regression coverage: "Failed follower authentication is
never retried."

Background: AliceBlueAccountManager.authenticate_all() attempts
client.get_session(force_refresh=True) once per account (concurrently) at
EngineLoop startup. A client whose auth failed is NOT removed from
self._clients and is NOT retried - it is left in the map forever, still
returned by get_client()/get_follower_clients_map(), and handed to the copy
pipeline as if usable. Every subsequent trade for that follower then fails
closed (e.g. FUNDS_FETCH_FAILED) - correct/safe, but with no mechanism to
ever recover a follower whose token was only transiently bad at startup.

This file has two parts:
1. A test documenting the current baseline gap (a failed account stays in
   `_clients` with no automatic recovery from authenticate_all()/get_client()
   alone).
2. Tests for the new periodic re-authentication retry mechanism added to
   AliceBlueAccountManager: reauth_failed_accounts() (single pass, callable
   directly - mirrors StaleOrderSweeper.sweep_once() /
   FollowerFillSyncer.sync_once()) and start_reauth_loop()/stop_reauth_loop()
   (background task - mirrors those same classes' start()/stop() shape).

No live trading and no real broker calls anywhere in this file - all
AliceBlueClient instances are AsyncMock/MagicMock test doubles, and no
network/DB access is required since these methods operate purely on
in-memory manager state.
"""
import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.services.alice_blue.manager import AliceBlueAccountManager


def _manager_with_clients(clients: dict, master_id: str = None) -> AliceBlueAccountManager:
    mgr = AliceBlueAccountManager()
    mgr._clients = dict(clients)
    mgr._master_account_id = master_id
    return mgr


def _mock_client(get_session_side_effect=None) -> MagicMock:
    client = MagicMock()
    client.get_session = AsyncMock(side_effect=get_session_side_effect)
    client.get_funds = AsyncMock(return_value={"available_margin": 1000})
    return client


# ---------------------------------------------------------------------------
# 1. Documents the current (still-present, by design) baseline behavior:
#    authenticate_all() alone never removes or retries a failed account.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failed_account_stays_in_clients_with_no_automatic_removal():
    """
    A follower whose auth attempt fails via authenticate_all() is left in
    _clients forever - get_client()/get_follower_clients_map() keep handing
    it out. This is the money-safety-preserving baseline: the pipeline must
    still see this client (and fail closed on it) rather than the account
    silently vanishing. Only the NEW reauth mechanism (tested below) gives
    it a chance to recover; authenticate_all()/get_client() themselves do
    not retry or evict it.
    """
    bad_client = _mock_client(get_session_side_effect=RuntimeError("token expired"))
    good_client = _mock_client()

    mgr = _manager_with_clients(
        {"master-1": good_client, "follower-bad": bad_client},
        master_id="master-1",
    )

    results = await mgr.authenticate_all()

    assert results == {"master-1": True, "follower-bad": False}
    # Still present and still handed out - the gap this finding describes.
    assert mgr.get_client("follower-bad") is bad_client
    assert "follower-bad" in mgr.get_follower_clients_map()
    assert bad_client in mgr.get_follower_clients()


# ---------------------------------------------------------------------------
# 2a. reauth_failed_accounts() eventually re-authenticates an account that
#     failed once and would succeed on a later attempt.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reauth_failed_accounts_recovers_previously_failed_account():
    call_count = {"n": 0}

    async def flaky_get_session(force_refresh=False):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("token not yet valid")
        return "session-token"

    client = MagicMock()
    client.get_session = AsyncMock(side_effect=flaky_get_session)

    mgr = _manager_with_clients({"follower-1": client})

    # Initial attempt (as EngineLoop startup would do) fails.
    initial = await mgr.authenticate_all()
    assert initial == {"follower-1": False}

    # A later retry pass (simulating time having passed, called directly
    # rather than via real sleep) now succeeds.
    retry_results = await mgr.reauth_failed_accounts()

    assert retry_results == {"follower-1": True}
    assert client.get_session.await_count == 2


# ---------------------------------------------------------------------------
# 2b. A persistently-failing account keeps being retried at the configured
#     interval - not spammed in a tight loop.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reauth_loop_retries_persistently_failing_account_at_configured_interval(monkeypatch):
    client = MagicMock()
    client.get_session = AsyncMock(side_effect=RuntimeError("still broken"))

    mgr = _manager_with_clients({"follower-1": client})
    mgr.check_interval_seconds = 42  # arbitrary distinctive value

    await mgr.authenticate_all()  # seeds _last_auth_status as failed
    assert client.get_session.await_count == 1

    sleep_calls = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 3:
            # Stop the loop after a few passes instead of looping forever -
            # equivalent to letting 3 intervals' worth of "time" pass.
            mgr._reauth_running = False

    monkeypatch.setattr("app.services.alice_blue.manager.asyncio.sleep", fake_sleep)

    await mgr.start_reauth_loop()
    task = mgr._reauth_task
    await task

    # Every sleep between passes used the configured interval - proves the
    # loop paces itself instead of tight-looping (which would show up as
    # sleep(0) or no sleep between calls at all).
    assert sleep_calls == [42, 42, 42]
    # One pass from authenticate_all() + 3 loop passes, since the account
    # never recovers in this test.
    assert client.get_session.await_count == 1 + 3
    assert mgr._reauth_running is False


# ---------------------------------------------------------------------------
# 2c. A currently-healthy account is not needlessly re-authenticated.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reauth_failed_accounts_skips_healthy_accounts():
    healthy_client = _mock_client()
    mgr = _manager_with_clients({"follower-1": healthy_client})

    initial = await mgr.authenticate_all()
    assert initial == {"follower-1": True}

    retry_results = await mgr.reauth_failed_accounts()

    assert retry_results == {}
    # get_session was called exactly once (the initial authenticate_all()
    # call) - the healthy account was never touched again.
    assert healthy_client.get_session.await_count == 1


@pytest.mark.asyncio
async def test_reauth_failed_accounts_is_noop_when_nothing_has_failed():
    a = _mock_client()
    b = _mock_client()
    mgr = _manager_with_clients({"a": a, "b": b})
    await mgr.authenticate_all()

    result = await mgr.reauth_failed_accounts()

    assert result == {}
    assert a.get_session.await_count == 1
    assert b.get_session.await_count == 1


# ---------------------------------------------------------------------------
# 2d. start_reauth_loop()/stop_reauth_loop() start and stop cleanly, mirroring
#     StaleOrderSweeper/FollowerFillSyncer's own start()/stop() pattern.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_and_stop_reauth_loop_cleanly():
    client = _mock_client()
    mgr = _manager_with_clients({"follower-1": client})
    mgr.check_interval_seconds = 3600  # long enough that stop() must cancel, not wait

    await mgr.start_reauth_loop()
    assert mgr._reauth_running is True
    assert mgr._reauth_task is not None
    assert not mgr._reauth_task.done()

    await mgr.stop_reauth_loop()

    assert mgr._reauth_running is False
    assert mgr._reauth_task.done()


# ---------------------------------------------------------------------------
# 3. Existing method signatures/behavior are unchanged (guards against
#    accidental breakage of every current caller across the codebase).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_existing_accessor_methods_unchanged():
    master = _mock_client()
    follower = _mock_client()
    mgr = _manager_with_clients({"m": master, "f": follower}, master_id="m")

    assert mgr.get_client("m") is master
    assert mgr.get_client("nonexistent") is None
    assert mgr.get_master_client() is master
    assert mgr.get_follower_clients() == [follower]
    assert mgr.get_follower_clients_map() == {"f": follower}
    assert mgr.get_follower_ids() == ["f"]


@pytest.mark.asyncio
async def test_fetch_all_funds_unchanged():
    master = _mock_client()
    mgr = _manager_with_clients({"m": master}, master_id="m")

    funds = await mgr.fetch_all_funds()

    assert funds == {"m": {"available_margin": 1000}}


@pytest.mark.asyncio
async def test_initialize_clears_auth_status(monkeypatch):
    """initialize() rebuilds _clients from scratch; any stale auth-status
    bookkeeping from a previous initialize()/authenticate_all() cycle must
    not leak into the new client set."""
    mgr = AliceBlueAccountManager()
    mgr._last_auth_status = {"stale-account": False}

    fake_redis = object()

    async def fake_get_redis():
        return fake_redis

    class FakeResult:
        def scalars(self):
            class S:
                def all(self_inner):
                    return []
            return S()

    class FakeSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def execute(self, *a, **kw):
            return FakeResult()

    monkeypatch.setattr("app.services.alice_blue.manager.get_redis", fake_get_redis)
    monkeypatch.setattr("app.services.alice_blue.manager.AsyncSessionLocal", lambda: FakeSession())

    await mgr.initialize()

    assert mgr._last_auth_status == {}
