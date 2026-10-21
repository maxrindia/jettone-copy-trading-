"""
R2B on a REAL PostgreSQL: duplicate create events delivered genuinely concurrently.

tests/test_duplicate_create_db_backstop.py proves the behaviour on in-memory SQLite, but that engine uses a
StaticPool (ONE shared connection for every session), so two truly simultaneous deliveries cannot be exercised
there: a loser's failed insert + rollback also discards the winner's pending write (the unchanged MARKET path
fails a gather test identically). Here every session gets its OWN PostgreSQL connection, and the two deliveries
are held at a barrier so that both reach the CopyTrade claim COMMIT at the same instant - the unique constraint
on copy_trades (master_order_id, follower_account_id, copy_event_id) must then admit exactly one.

HOW TO RUN (skipped unless configured; CI has no PostgreSQL service):

    R2B_POSTGRES_TEST_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/jettone_test  pytest tests/test_duplicate_create_postgres_concurrency.py

Safety (two independent guards, both fail closed):
  1. OFFLINE URL guard (`validate_url`, parsed with SQLAlchemy's `make_url`): the driver must be exactly
     postgresql+asyncpg; the URL must carry NO query string at all (`?host=`, `?database=`, `?ssl=` ... can
     silently redirect the connection); the host must be an explicitly configured loopback literal
     (127.0.0.1, localhost or ::1); the database name must be a strict whole-token test name (`test`, `test_x`,
     `x_test`, `x_test_y`; never `contest` or `latest_orders`). Anything else FAILS the test before any engine exists.
  2. SERVER-SIDE guard (`assert_server_is_disposable`): before the first write, the admin connection reports what it
     is REALLY connected to - `inet_server_addr()` must be loopback and `current_database()` must pass the same
     strict test-name check. A unix-socket connection (NULL address) is refused.
All objects live in a throw-away schema that is dropped afterwards, so nothing else in that database is created,
altered or dropped. The broker is a mock, Redis dedupe is bypassed (so only the database layer is in play), and
nothing here reads or writes a real environment. The guard unit tests below are offline and never open a connection.
The shared conftest engine is deliberately NOT used: it is built at import time and its pooled asyncpg
connections are bound to the first event loop; this module builds its engine inside each test, per loop.
"""
import asyncio
import ipaddress
import os
import re
import socket
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, IntegrityError
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models.accounts import Account, AccountType
from app.models.base import Base
from app.models.trades import CopyTrade, Order, OrderStatus
from strategy_helpers import SAMPLE_MASTER_ID, link_sample_accounts
from test_duplicate_create_db_backstop import KINDS, broker, deliver, make_event

URL_ENV = "R2B_POSTGRES_TEST_URL"
requires_postgres = pytest.mark.skipif(
    not os.environ.get(URL_ENV),
    reason=f"needs a disposable local PostgreSQL: set {URL_ENV}=postgresql+asyncpg://user@127.0.0.1:PORT/<a test database name, e.g. jettone_test>",
)

DRIVER = "postgresql+asyncpg"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")          # explicit literals only; 127.0.0.2, 0.0.0.0 ... are refused
# A WHOLE `test` token delimited by underscores or the ends of the name: test, test_x, x_test, x_test_y.
# Never contest, latest_orders, testing. Lower-case letters, digits and underscores only.
TEST_DB_NAME = re.compile(r"^(?:[a-z0-9_]*_)?test(?:_[a-z0-9_]*)?$")
# The only connect arguments the asyncpg dialect may derive from a query-free URL.
EXPECTED_CONNECT_KEYS = {"host", "port", "database", "user", "password"}


class UnsafePostgresTarget(ValueError):
    """The configured PostgreSQL target is not provably a disposable local test database."""


def is_test_database_name(name) -> bool:
    return isinstance(name, str) and bool(TEST_DB_NAME.fullmatch(name))


def validate_url(url) -> URL:
    """Offline guard (never opens a connection). Returns the parsed URL or raises UnsafePostgresTarget."""
    try:
        parsed = make_url(url)
    except (ArgumentError, TypeError, ValueError) as exc:
        raise UnsafePostgresTarget(f"not a parsable database URL ({type(exc).__name__})") from None
    if parsed.drivername != DRIVER:
        raise UnsafePostgresTarget(f"driver must be exactly {DRIVER}, got {parsed.drivername!r}")
    if parsed.query:
        raise UnsafePostgresTarget(f"query-string parameters are not allowed (they can override the host/database): {sorted(parsed.query)}")
    if parsed.host not in LOOPBACK_HOSTS:
        raise UnsafePostgresTarget(f"host must be an explicit loopback literal {LOOPBACK_HOSTS}, got {parsed.host!r}")
    if not is_test_database_name(parsed.database):
        raise UnsafePostgresTarget(f"database name must be a strict whole-token test name, got {parsed.database!r}")
    # What the asyncpg dialect would REALLY connect to must be exactly what was validated.
    _, kwargs = PGDialect_asyncpg().create_connect_args(parsed)
    if kwargs.get("host") != parsed.host or kwargs.get("database") != parsed.database or not set(kwargs) <= EXPECTED_CONNECT_KEYS:
        raise UnsafePostgresTarget(f"the dialect would connect somewhere other than the validated URL: {sorted(kwargs)}")
    return parsed


def assert_server_is_disposable(server_address, database_name) -> None:
    """Server-side guard on what the admin connection REPORTS (inet_server_addr(), current_database())."""
    try:
        address = ipaddress.ip_address(str(server_address).split("/")[0]) if server_address else None
    except ValueError:
        address = None
    if address is None or not address.is_loopback:
        raise UnsafePostgresTarget(f"the server reports address {server_address!r}: not a loopback TCP connection")
    if not is_test_database_name(database_name):
        raise UnsafePostgresTarget(f"the server reports database {database_name!r}: not a strict test database name")


def safe_url() -> URL:
    try:
        return validate_url(os.environ[URL_ENV])
    except UnsafePostgresTarget as exc:
        pytest.fail(f"refusing to run: {URL_ENV} is not a safe disposable local test database: {exc}")


class ClaimBarrierSession(AsyncSession):
    """Holds every session that is about to COMMIT a CopyTrade claim at a barrier until all parties arrive, so the
    INSERTs reach PostgreSQL concurrently from separate connections. Records which backend and what happened."""
    barrier = None
    claim_log: list = []

    @classmethod
    def arm(cls, parties):
        cls.barrier = asyncio.Barrier(parties)
        cls.claim_log = []

    @classmethod
    def disarm(cls):
        cls.barrier = None

    async def commit(self):
        cls = type(self)
        if cls.barrier is None or not any(isinstance(o, CopyTrade) for o in self.new):
            return await super().commit()
        pid = (await self.execute(text("select pg_backend_pid()"))).scalar_one()    # this session's connection
        await asyncio.wait_for(cls.barrier.wait(), timeout=20)                      # both deliveries are here
        try:
            await super().commit()
        except IntegrityError:
            cls.claim_log.append((pid, "IntegrityError"))
            raise
        cls.claim_log.append((pid, "committed"))


@asynccontextmanager
async def postgres_environment(monkeypatch):
    url = safe_url()
    schema = f"r2b_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(url, poolclass=NullPool)
    try:                                                    # read-only identity check; nothing is written yet
        async with admin.connect() as conn:
            address, database = (await conn.execute(text("select inet_server_addr()::text, current_database()"))).one()
        assert_server_is_disposable(address, database)
    except UnsafePostgresTarget as exc:
        await admin.dispose()
        pytest.fail(f"refusing to create anything: connected server is not a disposable local test database: {exc}")
    except BaseException:
        await admin.dispose()
        raise
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(url, poolclass=NullPool, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(bind=engine, class_=ClaimBarrierSession, expire_on_commit=False,
                                     autoflush=False, autocommit=False)
        monkeypatch.setattr("app.services.copy_engine.pipeline.AsyncSessionLocal", factory)
        monkeypatch.setattr("app.core.database.AsyncSessionLocal", factory)
        ClaimBarrierSession.disarm()
        async with factory() as s:
            backend = (await s.execute(text("select version()"))).scalar_one()
            master = Account(id=SAMPLE_MASTER_ID, account_type=AccountType.MASTER, display_name="Master Account",
                             alice_user_id="AB00100", is_active=True)
            follower = Account(id=uuid.uuid4(), account_type=AccountType.FOLLOWER, display_name="Follower 1",
                               alice_user_id="AB00201", is_active=True)
            s.add_all([master, follower])
            await s.commit()
            await link_sample_accounts(s, {"master": master, "follower1": follower})
        assert backend.startswith("PostgreSQL"), backend                           # never silently another backend
        yield factory, follower
    finally:
        ClaimBarrierSession.disarm()
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.dispose()


async def rows(factory, master_order_id):
    async with factory() as s:
        trades = (await s.execute(select(CopyTrade).where(CopyTrade.master_order_id == master_order_id))).scalars().all()
        orders = (await s.execute(select(Order).where(Order.master_broker_order_id == master_order_id))).scalars().all()
    return trades, orders


# ── 1. two simultaneous deliveries of the same create event ───────────────────────────────

@requires_postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)          # MARKET is the unchanged control: it must behave the same
async def test_two_simultaneous_deliveries_make_one_claim_and_one_send(monkeypatch, kind):
    async with postgres_environment(monkeypatch) as (factory, follower):
        client, mid = broker(), f"R2B_PG_{kind}"
        ClaimBarrierSession.arm(2)
        results = await asyncio.gather(deliver(make_event(kind, mid), client, follower),
                                       deliver(make_event(kind, mid), client, follower))
        log = list(ClaimBarrierSession.claim_log)
        ClaimBarrierSession.disarm()

        assert len({pid for pid, _ in log}) == 2                                   # two DIFFERENT PostgreSQL backends
        assert sorted(outcome for _, outcome in log) == ["IntegrityError", "committed"]   # a real constraint race
        assert sorted(r["status"] for r in results) == ["SKIPPED", "SUCCESS"]
        assert [r for r in results if r["status"] == "SKIPPED"][0]["reason"] == "DUPLICATE_EVENT"
        assert client.place_order.await_count == 1                                 # at most one send attempt
        trades, orders = await rows(factory, mid)
        assert len(trades) == 1 and len(orders) == 1                               # one claim, one order
        assert orders[0].broker_order_id == "BRK_FIRST"


# ── 2. a pre-existing partial fill survives simultaneous duplicate deliveries ─────────────

@requires_postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_simultaneous_duplicates_do_not_overwrite_a_partial_fill(monkeypatch, kind):
    async with postgres_environment(monkeypatch) as (factory, follower):
        client, mid = broker(), f"R2B_PG_STATE_{kind}"
        await deliver(make_event(kind, mid), client, follower)                     # the original delivery
        async with factory() as s:                                                 # 3 of 10 filled, recorded by the syncer
            await s.execute(update(Order).where(Order.master_broker_order_id == mid)
                            .values(status=OrderStatus.PARTIAL, filled_quantity=3, pending_quantity=7))
            await s.commit()
        before = (await rows(factory, mid))[1][0]

        ClaimBarrierSession.arm(2)
        results = await asyncio.gather(deliver(make_event(kind, mid), client, follower),
                                       deliver(make_event(kind, mid), client, follower))
        log = list(ClaimBarrierSession.claim_log)
        ClaimBarrierSession.disarm()

        assert len({pid for pid, _ in log}) == 2 and [o for _, o in log] == ["IntegrityError"] * 2
        assert all(r == {"status": "SKIPPED", "reason": "DUPLICATE_EVENT"} for r in results)
        assert client.place_order.await_count == 1                                 # only the original send
        trades, orders = await rows(factory, mid)
        after = orders[0]
        assert len(trades) == 1 and len(orders) == 1
        assert (after.status, after.broker_order_id, after.filled_quantity, after.pending_quantity) == \
               (OrderStatus.PARTIAL, "BRK_FIRST", 3, 7)
        assert after.updated_at == before.updated_at                               # not even touched


# ── 3. a later, distinct event still processes after a concurrent duplicate ───────────────

@requires_postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_a_later_distinct_event_processes_after_a_concurrent_duplicate(monkeypatch, kind):
    async with postgres_environment(monkeypatch) as (factory, follower):
        client = broker()
        ClaimBarrierSession.arm(2)
        await asyncio.gather(deliver(make_event(kind, f"R2B_PG_A_{kind}"), client, follower),
                             deliver(make_event(kind, f"R2B_PG_A_{kind}"), client, follower))
        ClaimBarrierSession.disarm()
        later = await deliver(make_event(kind, f"R2B_PG_B_{kind}"), client, follower)
        assert later["status"] == "SUCCESS" and client.place_order.await_count == 2
        assert len((await rows(factory, f"R2B_PG_A_{kind}"))[0]) == 1
        trades_b, orders_b = await rows(factory, f"R2B_PG_B_{kind}")
        assert len(trades_b) == 1 and orders_b[0].broker_order_id == "BRK_SECOND"


# ══ OFFLINE guard tests: no network, no database, never skipped ═══════════════════════════
#
# The first version of the guard compared the netloc host and looked for the substring "test" in the database
# name. Both were bypassable: `?host=prod.example.com` and `?database=production` in the query string are honoured
# by SQLAlchemy's asyncpg dialect (so the connection went somewhere the guard never looked), and `latest_orders` /
# `contest` contain the letters "test". These tests pin the corrected behaviour.

LEGIT_URLS = [
    "postgresql+asyncpg://postgres@127.0.0.1:61131/jettone_test",
    "postgresql+asyncpg://postgres:secret@localhost:5432/test",
    "postgresql+asyncpg://postgres@[::1]:5432/test_db",
    "postgresql+asyncpg://postgres@127.0.0.1/a_test_b",
    "postgresql+asyncpg://postgres@127.0.0.1/my_test",
]

BYPASS_URLS = {
    "remote host": "postgresql+asyncpg://u@db.example.com/jettone_test",
    "private address": "postgresql+asyncpg://u@10.0.0.5/jettone_test",
    "any-address 0.0.0.0": "postgresql+asyncpg://u@0.0.0.0/jettone_test",
    "other loopback address": "postgresql+asyncpg://u@127.0.0.2/jettone_test",
    "no 'test' in the name": "postgresql+asyncpg://u@127.0.0.1/jettone",
    "latest_orders (has the letters)": "postgresql+asyncpg://u@127.0.0.1/latest_orders",
    "contest (has the letters)": "postgresql+asyncpg://u@localhost/contest",
    "testing (not a whole token)": "postgresql+asyncpg://u@127.0.0.1/testing",
    "upper-case name": "postgresql+asyncpg://u@127.0.0.1/JETTONE_TEST",
    "query host override": "postgresql+asyncpg://u@127.0.0.1/jettone_test?host=prod.example.com",
    "query database override": "postgresql+asyncpg://u@127.0.0.1/jettone_test?database=production",
    "query dbname override": "postgresql+asyncpg://u@127.0.0.1/jettone_test?dbname=production",
    "query port override": "postgresql+asyncpg://u@127.0.0.1/jettone_test?port=5432",
    "harmless-looking query (ssl)": "postgresql+asyncpg://u@127.0.0.1/jettone_test?ssl=disable",
    "harmless-looking query (app name)": "postgresql+asyncpg://u@127.0.0.1/jettone_test?application_name=x",
    "plain postgresql driver": "postgresql://u@127.0.0.1/jettone_test",
    "psycopg2 driver": "postgresql+psycopg2://u@127.0.0.1/jettone_test",
    "'+asyncpg' hidden in the db name": "postgresql://u@127.0.0.1/x+asyncpg_test",
    "userinfo trick": "postgresql+asyncpg://127.0.0.1@evil.example.com/jettone_test",
    "multi-host": "postgresql+asyncpg://u@127.0.0.1,prod.example.com/jettone_test",
    "no host (unix socket)": "postgresql+asyncpg:///jettone_test",
    "no database": "postgresql+asyncpg://u@127.0.0.1",
    "sqlite": "sqlite+aiosqlite:///:memory:",
    "empty string": "",
    "garbage": "not a url",
}


def test_legitimate_urls_pass_and_connect_exactly_where_validated():
    for url in LEGIT_URLS:
        parsed = validate_url(url)
        _, kwargs = PGDialect_asyncpg().create_connect_args(parsed)
        assert kwargs["host"] == parsed.host and kwargs["database"] == parsed.database


@pytest.mark.parametrize("label,url", list(BYPASS_URLS.items()), ids=list(BYPASS_URLS))
def test_every_reviewed_bypass_is_refused(label, url):
    with pytest.raises(UnsafePostgresTarget):
        validate_url(url)


def test_the_root_cause_the_dialect_honours_query_overrides():
    """Why a query string must be refused outright: the asyncpg dialect lets it redirect the connection."""
    _, kwargs = PGDialect_asyncpg().create_connect_args(
        make_url("postgresql+asyncpg://u@127.0.0.1/jettone_test?host=prod.example.com&database=production"))
    assert kwargs["host"] == "prod.example.com" and kwargs["database"] == "production"


def test_none_and_non_string_inputs_are_refused():
    for bad in (None, 123, b"postgresql+asyncpg://u@127.0.0.1/jettone_test"):
        with pytest.raises(UnsafePostgresTarget):
            validate_url(bad)


def test_url_validation_never_opens_a_connection(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the offline guard attempted network access")

    for target in ("connect", "connect_ex", "bind"):
        monkeypatch.setattr(socket.socket, target, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    for url in [*LEGIT_URLS, *BYPASS_URLS.values()]:
        try:
            validate_url(url)
        except UnsafePostgresTarget:
            pass


@pytest.mark.parametrize("name", ["test", "test_x", "x_test", "x_test_y", "jettone_test", "a_b_test_c_d", "test_1", "_test", "test_"])
def test_strict_test_database_names_are_accepted(name):
    assert is_test_database_name(name)


@pytest.mark.parametrize("name", ["contest", "latest_orders", "testing", "mytest", "testdb", "test-db", "test db", "Test", "TEST_DB",
                                  "production", "jettone", "", None, 7])
def test_everything_else_is_refused_as_a_database_name(name):
    assert not is_test_database_name(name)


@pytest.mark.parametrize("address,database", [("127.0.0.1/32", "jettone_test"), ("::1/128", "test"), ("127.0.0.1", "x_test_y")])
def test_server_side_guard_accepts_a_loopback_test_database(address, database):
    assert_server_is_disposable(address, database)


@pytest.mark.parametrize("address,database", [
    ("10.0.0.5/32", "jettone_test"),          # reachable but not loopback (e.g. a ?host= override that slipped through)
    ("203.0.113.9/32", "jettone_test"),
    (None, "jettone_test"),                   # NULL = unix-socket connection: cannot be proven local TCP
    ("", "jettone_test"),
    ("not-an-ip", "jettone_test"),
    ("127.0.0.1/32", "production"),
    ("127.0.0.1/32", "latest_orders"),
    ("127.0.0.1/32", None),
])
def test_server_side_guard_refuses_anything_that_is_not_a_loopback_test_database(address, database):
    with pytest.raises(UnsafePostgresTarget):
        assert_server_is_disposable(address, database)


def test_safe_url_fails_the_test_for_an_unsafe_environment_value(monkeypatch):
    monkeypatch.setenv(URL_ENV, BYPASS_URLS["query host override"])
    with pytest.raises(pytest.fail.Exception, match="not a safe disposable local test database"):
        safe_url()
    monkeypatch.setenv(URL_ENV, LEGIT_URLS[0])
    assert safe_url().database == "jettone_test"


class _Row:
    def __init__(self, row):
        self._row = row

    def one(self):
        return self._row


class _FakeConnection:
    def __init__(self, row):
        self._row = row

    async def execute(self, statement):
        return _Row(self._row)


class _FakeConnect:
    def __init__(self, row):
        self._row = row

    async def __aenter__(self):
        return _FakeConnection(self._row)

    async def __aexit__(self, *exc):
        return False


class _FakeAdminEngine:
    """Reports a server identity and records whether anything tried to WRITE (begin a transaction)."""
    def __init__(self, row):
        self.row, self.disposed, self.write_attempted = row, False, False

    def connect(self):
        return _FakeConnect(self.row)

    def begin(self):
        self.write_attempted = True
        raise AssertionError("a write transaction was started before the server-side guard passed")

    async def dispose(self):
        self.disposed = True


@pytest.mark.asyncio
async def test_an_unsafe_url_is_refused_before_any_engine_is_created(monkeypatch):
    monkeypatch.setenv(URL_ENV, BYPASS_URLS["query host override"])

    def forbidden(*args, **kwargs):
        raise AssertionError("an engine was created for an unsafe URL")

    monkeypatch.setitem(globals(), "create_async_engine", forbidden)
    with pytest.raises(pytest.fail.Exception, match="not a safe disposable local test database"):
        async with postgres_environment(monkeypatch):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("row", [("10.0.0.5/32", "jettone_test"), (None, "jettone_test"),
                                 ("127.0.0.1/32", "production"), ("127.0.0.1/32", "latest_orders")])
async def test_a_server_that_fails_the_identity_check_is_refused_before_any_write(monkeypatch, row):
    """The URL looks fine, but the server reports otherwise: fail closed, dispose, and never begin a write."""
    monkeypatch.setenv(URL_ENV, LEGIT_URLS[0])
    admin = _FakeAdminEngine(row)
    monkeypatch.setitem(globals(), "create_async_engine", lambda *args, **kwargs: admin)
    with pytest.raises(pytest.fail.Exception, match="connected server is not a disposable local test database"):
        async with postgres_environment(monkeypatch):
            pass
    assert admin.disposed is True and admin.write_attempted is False
