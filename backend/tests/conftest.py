import os

from cryptography.fernet import Fernet as TestFernet

os.environ.setdefault("MASTER_CAPITAL", "500000")
os.environ.setdefault("ALICE_BLUE_ENCRYPTION_KEY", TestFernet.generate_key().decode())
os.environ.setdefault("INTERNAL_API_TOKEN", "test-internal-token")

import atexit
import shutil
import tempfile
from pathlib import Path


def _default_test_database_url() -> str:
    """A throwaway SQLite FILE, not ":memory:". An in-memory aiosqlite database gets a StaticPool, i.e. ONE connection shared by every
    session, so two concurrent tasks (a pipeline fanout to several Children, each with its own session) interleave statements on a
    single connection and intermittently fail with "cannot commit transaction - SQL statements in progress". That is a test-harness
    artefact: PostgreSQL gives each session its own connection and never does this. A file database gets a real pool, one connection
    per session, and the same tests pass deterministically. Override with TEST_DATABASE_URL to use another database."""
    directory = tempfile.mkdtemp(prefix="jettone-test-db-")
    atexit.register(shutil.rmtree, directory, ignore_errors=True)
    return "sqlite+aiosqlite:///" + Path(directory, "test.db").as_posix()


TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or _default_test_database_url()

import pytest
import pytest_asyncio
import uuid
from unittest.mock import AsyncMock
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.models.base import Base
from app.models.accounts import Account, AccountType, CopySetting, QuantityMode

_sqlite = TEST_DATABASE_URL.startswith("sqlite")
test_engine = create_async_engine(
    TEST_DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    **({"connect_args": {"timeout": 30}} if _sqlite else {}),      # wait for a competing writer instead of failing at once
)

if _sqlite:
    from sqlalchemy import event

    @event.listens_for(test_engine.sync_engine, "connect")
    def _fast_throwaway_sqlite(dbapi_connection, _record):
        """The database is deleted after the run, so durability is pointless: skip the fsyncs. WAL lets readers and the one writer
        proceed together."""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=OFF")
        cursor.close()

TestingSessionLocal = async_sessionmaker(
    bind=test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _dispose_test_engine():
    """
    Without this, leftover pooled aiosqlite/asyncpg connection worker threads
    keep the interpreter alive after the session ends and pytest hangs in
    threading._shutdown.
    """
    yield
    await test_engine.dispose()


@pytest.fixture
def mock_redis():
    """Mock Redis client with in-memory storage."""
    storage = {}
    expiry = {}          # key -> virtual-clock time at which it expires
    clock = [0.0]        # virtual seconds; only moves when a test calls mock._advance(...)
    mock = AsyncMock()

    def _purge():
        for k in [k for k, t in expiry.items() if t <= clock[0]]:
            storage.pop(k, None)
            expiry.pop(k, None)

    async def _get(key):
        _purge()
        return storage.get(key)

    async def _set(key, val, ex=None, nx=False, px=None):
        _purge()
        if nx and key in storage:
            return False
        storage[key] = str(val)
        expiry.pop(key, None)
        # Only a positive TTL is emulated. A zero/negative one (which real Redis would reject, and which the
        # original mock simply ignored - e.g. AliceBlueClient.get_funds(max_cache_age_sec=0)) stores the key
        # without an expiry, exactly as before.
        if ex is not None and ex > 0:
            expiry[key] = clock[0] + ex
        elif px is not None and px > 0:
            expiry[key] = clock[0] + px / 1000.0
        return True

    async def _delete(key):
        _purge()
        existed = key in storage
        storage.pop(key, None)
        expiry.pop(key, None)
        return 1 if existed else 0

    async def _incr(key):
        _purge()
        storage[key] = str(int(storage.get(key, "0")) + 1)
        return int(storage[key])

    async def _expire(key, seconds):
        _purge()
        if key not in storage:
            return False
        expiry[key] = clock[0] + seconds
        return True

    async def _ttl(key):
        _purge()
        if key not in storage:
            return -2
        return int(expiry[key] - clock[0]) if key in expiry else -1

    async def _exists(key):
        _purge()
        return 1 if key in storage else 0

    mock.get = AsyncMock(side_effect=_get)
    mock.set = AsyncMock(side_effect=_set)
    mock.delete = AsyncMock(side_effect=_delete)
    mock.incr = AsyncMock(side_effect=_incr)
    mock.expire = AsyncMock(side_effect=_expire)
    mock.ttl = AsyncMock(side_effect=_ttl)
    mock.exists = AsyncMock(side_effect=_exists)
    mock.ping = AsyncMock(return_value=True)
    mock._storage = storage
    mock._expiry = expiry
    mock._advance = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
    return mock


@pytest_asyncio.fixture
async def db_session(monkeypatch):
    """Creates fresh in-memory database tables and patches AsyncSessionLocal for tests."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Patch AsyncSessionLocal in engine components
    monkeypatch.setattr("app.core.database.AsyncSessionLocal", TestingSessionLocal)
    monkeypatch.setattr("app.services.copy_engine.pipeline.AsyncSessionLocal", TestingSessionLocal)
    monkeypatch.setattr("app.services.reliability.stale_order_sweeper.AsyncSessionLocal", TestingSessionLocal)
    monkeypatch.setattr("app.services.reliability.follower_fill_syncer.AsyncSessionLocal", TestingSessionLocal)
    monkeypatch.setattr("app.services.reliability.master_order_poller.AsyncSessionLocal", TestingSessionLocal)
    monkeypatch.setattr("app.services.runtime.engine_loop.AsyncSessionLocal", TestingSessionLocal)

    async with TestingSessionLocal() as session:
        yield session

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture
async def sample_accounts(db_session):
    from strategy_helpers import SAMPLE_MASTER_ID

    master = Account(
        id=SAMPLE_MASTER_ID,
        account_type=AccountType.MASTER,
        display_name="Master Account",
        alice_user_id="AB00100",
        is_active=True,
    )
    follower1 = Account(
        id=uuid.uuid4(),
        account_type=AccountType.FOLLOWER,
        display_name="Follower 1",
        alice_user_id="AB00201",
        is_active=True,
    )
    db_session.add(master)
    db_session.add(follower1)
    await db_session.commit()
    await db_session.refresh(master)
    await db_session.refresh(follower1)
    return {"master": master, "follower1": follower1}


@pytest_asyncio.fixture
async def sample_strategy(db_session, sample_accounts):
    """Explicit strategy linking sample_accounts' master -> follower1 at 1.0x.

    Replaces the removed engine fallback (copy to every active FOLLOWER at
    1.0x when no strategy matches) for tests that relied on it - same
    accounts, same 1.0x multiplier, so their assertions are unchanged."""
    from strategy_helpers import link_sample_accounts

    return await link_sample_accounts(db_session, sample_accounts)


@pytest_asyncio.fixture
async def sample_copy_settings(db_session, sample_accounts):
    follower = sample_accounts["follower1"]
    setting = CopySetting(
        follower_account_id=follower.id,
        copy_enabled=True,
        quantity_mode=QuantityMode.FIXED,
        fixed_qty=25,
        allowed_symbols=["NIFTY24AUG23500CE"],
    )
    db_session.add(setting)
    await db_session.commit()
    await db_session.refresh(setting)
    return setting


@pytest.fixture
def cross_broker_pairs_enabled(monkeypatch):
    """R5: the pipeline refuses every Master -> Child broker pair that is not on the allowlist in
    app/services/brokers/pairs.py, and today only Alice -> Alice is. The tests that use this fixture exercise what a Groww or
    Angel One Child does INSIDE the pipeline (SELL guard, funds, strict parsing, ambiguous sends, reconciliation), which only
    runs once a pair has been enabled. It lists those pairs and waives the pair gate's OWN role-gate and live-evidence checks for this test only.
    The routing-time gate in the pipeline (`can_be_child`, CHILD_BROKER_NOT_READY) is a separate binding and is left exactly as
    the test set it; nothing outside the test is affected."""
    from app.services.brokers import pairs

    monkeypatch.setattr(pairs, "SUPPORTED_PAIRS", {**pairs.SUPPORTED_PAIRS,
                                                   ("alicebluenew", "groww"): "test only", ("alicebluenew", "angelone"): "test only"})
    monkeypatch.setattr(pairs, "_feature_gap", lambda *args, **kwargs: None)
    monkeypatch.setattr(pairs, "can_be_master", lambda *args, **kwargs: True)
    monkeypatch.setattr(pairs, "can_be_child", lambda *args, **kwargs: True)


def pytest_collection_modifyitems(items):
    """
    Run every async TEST in the same event loop as the async FIXTURES.

    pytest.ini puts async fixtures on one session-scoped loop (asyncio_default_fixture_loop_scope = session), but pytest-asyncio 0.24
    runs each async test in its own function-scoped loop. The two differ, and an asyncpg connection is bound to the loop that created
    it. The shared `db_session` fixture is used in both: fixtures such as `sample_accounts` leave it holding an open transaction on a
    connection made in the fixture loop, and a test that then passes the session to application code uses that connection from a
    different loop ("Future attached to a different loop"). On PostgreSQL that made the Live Trading read fail closed (HTTP 409), left
    the connection `idle in transaction`, and deadlocked the next fixture teardown (`DROP TABLE` waiting on it). SQLite (aiosqlite)
    does not care which loop calls it, so only PostgreSQL showed it. A test that sets its own loop_scope is left alone.
    """
    for item in items:
        marker = item.get_closest_marker("asyncio")
        if marker is not None and "loop_scope" not in marker.kwargs and "scope" not in marker.kwargs:
            item.add_marker(pytest.mark.asyncio(loop_scope="session"), append=False)
