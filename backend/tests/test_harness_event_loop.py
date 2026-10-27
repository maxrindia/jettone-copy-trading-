"""
Harness check: async tests must run in the same event loop as the async fixtures.

pytest.ini runs async fixtures on a session-scoped loop; pytest-asyncio 0.24 runs async tests on a function-scoped loop unless told
otherwise (tests/conftest.py applies loop_scope="session" to every async test). An asyncpg connection belongs to the loop that created
it, so a fixture-made connection used from a test loop fails on PostgreSQL ("Future attached to a different loop"), which made
get_live_trading_state fail closed, left a session `idle in transaction` and deadlocked the next fixture teardown. SQLite never
shows it, so these checks assert the loop identity directly instead of relying on the database to complain.
"""
import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.models.accounts import Account
from app.services.live_trading import get_live_trading_state


@pytest_asyncio.fixture(scope="session")
async def fixture_loop():
    return asyncio.get_running_loop()


@pytest.mark.asyncio
async def test_an_async_test_runs_in_the_loop_the_async_fixtures_run_in(fixture_loop):
    assert asyncio.get_running_loop() is fixture_loop


@pytest.mark.asyncio
async def test_every_async_test_is_marked_with_the_session_loop_scope(request):
    marker = request.node.get_closest_marker("asyncio")
    assert marker is not None and marker.kwargs.get("loop_scope") == "session"


@pytest.mark.asyncio
async def test_the_session_a_fixture_left_a_transaction_open_on_is_usable_in_the_test(db_session, sample_accounts, fixture_loop):
    """sample_accounts ends with refresh(), so db_session holds an open transaction on a connection made in the fixture loop. Using
    that same session here is what failed on PostgreSQL. The loop assertion makes this fail on SQLite too if the loops diverge."""
    assert asyncio.get_running_loop() is fixture_loop
    rows = (await db_session.execute(select(Account.id))).scalars().all()
    assert len(rows) >= 2


@pytest.mark.asyncio
async def test_the_live_trading_read_through_the_fixture_session_does_not_fail_closed_on_a_loop_error(db_session, sample_accounts):
    state = await get_live_trading_state(db_session)
    assert state.source != "error"
