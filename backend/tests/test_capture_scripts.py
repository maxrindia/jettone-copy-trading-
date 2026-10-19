"""
The supervised capture scripts (groww_capture.py, angel_capture.py) log in through the real login guard on a
throw-away in-process Redis (scripts/_capture_memory_redis.py). These tests run the REAL capture() coroutine
against mocked HTTP transports - no network, no credentials - and prove: the login succeeds, only the fixed
read-only GETs follow it, no order operation can be reached, and a failed login cleans up after itself.
"""
import asyncio
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from app.services.brokers.login_guard import (
    DAILY_CAP, LOCK_TTL_SECONDS, MIN_SPACING_SECONDS, TOO_SOON, LoginThrottled, run_guarded_login,
)
import angel_helpers as A
import groww_helpers as G

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
from _capture_memory_redis import MemoryRedis  # noqa: E402

NOW = datetime(2026, 10, 19, 5, 0, tzinfo=timezone.utc)


def load_script(name):
    spec = importlib.util.spec_from_file_location(f"cap_{name}", SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Tick:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ── the in-memory store: Redis semantics ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_nx_has_exactly_one_winner_even_under_concurrency():
    r = MemoryRedis()
    results = await asyncio.gather(*(r.set("lock", f"owner-{i}", ex=30, nx=True) for i in range(50)))
    assert results.count(True) == 1 and results.count(None) == 49
    assert (await r.get("lock")).startswith("owner-")


@pytest.mark.asyncio
async def test_set_nx_succeeds_again_only_after_the_key_expires_or_is_deleted():
    t = Tick()
    r = MemoryRedis(monotonic=t)
    assert await r.set("k", "a", ex=30, nx=True) is True
    assert await r.set("k", "b", ex=30, nx=True) is None and await r.get("k") == "a"
    t.t += 29.9
    assert await r.set("k", "b", ex=30, nx=True) is None
    t.t += 0.2
    assert await r.set("k", "b", ex=30, nx=True) is True and await r.get("k") == "b"
    assert await r.delete("k") == 1 and await r.delete("k") == 0
    assert await r.set("k", "c", nx=True) is True


@pytest.mark.asyncio
async def test_ttl_and_expiry_follow_redis_semantics():
    t = Tick()
    r = MemoryRedis(monotonic=t)
    assert await r.ttl("missing") == -2
    await r.set("forever", "1")
    assert await r.ttl("forever") == -1
    await r.set("k", "v", ex=10)
    assert await r.ttl("k") == 10
    t.t += 3.5
    assert await r.ttl("k") == 7                                   # rounded up, like a whole-second TTL
    assert await r.exists("k") == 1 and await r.exists("k", "forever", "missing") == 2
    t.t += 6.6
    assert await r.get("k") is None and await r.exists("k") == 0 and await r.ttl("k") == -2


@pytest.mark.asyncio
async def test_set_replaces_the_value_and_clears_the_expiry_unless_one_is_given():
    t = Tick()
    r = MemoryRedis(monotonic=t)
    await r.set("k", "a", ex=5)
    await r.set("k", "b")
    t.t += 100
    assert await r.get("k") == "b" and await r.ttl("k") == -1
    await r.set("p", "x", px=1500)
    assert await r.ttl("p") == 2
    assert await r.set("absent", "v", xx=True) is None and await r.set("k", "c", xx=True) is True
    with pytest.raises(ValueError):
        await r.set("bad", "v", ex=0)


@pytest.mark.asyncio
async def test_incr_and_expire_behave_like_redis():
    t = Tick()
    r = MemoryRedis(monotonic=t)
    assert [await r.incr("n"), await r.incr("n"), await r.incr("n")] == [1, 2, 3]
    assert await r.expire("missing", 5) is False
    assert await r.expire("n", 10) is True and await r.ttl("n") == 10
    assert await r.incr("n") == 4 and await r.ttl("n") == 10         # INCR keeps the expiry
    t.t += 11
    assert await r.incr("n") == 1                                    # expired counter starts over
    await r.set("s", "text")
    with pytest.raises(ValueError):
        await r.incr("s")
    assert await r.expire("n", 0) is True and await r.exists("n") == 0


# ── the real login guard on the in-memory store ──────────────────────────────────────────────

def guarded(r, do_login, *, session_key="sess", cap=None, broker="groww", sleep=None):
    kwargs = {"sleep": sleep} if sleep else {}
    return run_guarded_login(redis=r, broker=broker, account_id="capture", clock=lambda: NOW, do_login=do_login,
                             session_key=session_key, daily_cap=cap, **kwargs)


@pytest.mark.asyncio
async def test_guarded_login_runs_once_and_releases_its_lock():
    r = MemoryRedis()
    calls = []

    async def login():
        calls.append(1)
        await r.set("sess", "token")
    await guarded(r, login)
    assert calls == [1]
    assert await r.exists("login:lock:groww:capture") == 0           # lock released
    await guarded(r, login)                                          # session present, just logged in: nothing is re-sent
    assert calls == [1]


@pytest.mark.asyncio
async def test_a_second_attempt_is_throttled_then_allowed_after_the_spacing_window():
    t = Tick()
    r = MemoryRedis(monotonic=t)
    calls = []

    async def login():
        calls.append(1)
    await guarded(r, login)                                          # no session was written, so spacing (not "done") applies
    with pytest.raises(LoginThrottled) as ei:
        await guarded(r, login)
    assert ei.value.code == TOO_SOON and 1 <= ei.value.retry_after <= MIN_SPACING_SECONDS
    assert calls == [1]
    t.t += MIN_SPACING_SECONDS + 0.5
    await guarded(r, login)
    assert calls == [1, 1]


@pytest.mark.asyncio
async def test_the_daily_cap_blocks_further_attempts():
    t = Tick()
    r = MemoryRedis(monotonic=t)
    calls = []

    async def login():
        calls.append(1)
    for _ in range(2):
        await guarded(r, login, cap=2)
        t.t += MIN_SPACING_SECONDS + 1
    with pytest.raises(LoginThrottled) as ei:
        await guarded(r, login, cap=2)
    assert ei.value.code == DAILY_CAP and calls == [1, 1]


@pytest.mark.asyncio
async def test_concurrent_logins_send_exactly_one_request_and_share_the_result():
    r = MemoryRedis()
    calls = []

    async def login():
        calls.append(1)
        await asyncio.sleep(0.15)
        await r.set("sess", "token")
    await asyncio.gather(*(guarded(r, login) for _ in range(5)))
    assert calls == [1]
    assert await r.exists("login:lock:groww:capture") == 0


@pytest.mark.asyncio
async def test_a_failed_login_releases_the_lock_and_still_counts_against_the_spacing():
    r = MemoryRedis()

    async def boom():
        raise RuntimeError("refused")
    with pytest.raises(RuntimeError):
        await guarded(r, boom)
    assert await r.exists("login:lock:groww:capture") == 0           # no lock left behind
    assert await r.exists("login:last:groww:capture") == 1           # but the attempt was counted
    with pytest.raises(LoginThrottled) as ei:
        await guarded(r, boom)
    assert ei.value.code == TOO_SOON


@pytest.mark.asyncio
async def test_a_crashed_holders_lock_expires_instead_of_blocking_forever():
    t = Tick()
    r = MemoryRedis(monotonic=t)
    await r.set("login:lock:groww:capture", "dead-process", ex=LOCK_TTL_SECONDS, nx=True)
    calls = []

    async def login():
        calls.append(1)

    async def no_wait(_):
        t.t += 20                                                    # time passes while the joiner polls
    with pytest.raises(LoginThrottled):
        await guarded(r, login, sleep=no_wait)
    assert calls == []                                               # the joiner never sent a login of its own
    t.t += LOCK_TTL_SECONDS
    await guarded(r, login)
    assert calls == [1]


# ── the real capture() against mocked transports ─────────────────────────────────────────────

def future_token(fake):
    expiry = (datetime.now(timezone.utc) + timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%S")
    fake.token_response = (200, {"token": "TOK-SECRET-1", "tokenRefId": "ref", "sessionName": "s", "expiry": expiry,
                                 "isActive": True})


def forbid_order_methods(monkeypatch, cls):
    def refuse(*a, **k):
        raise AssertionError("a capture must never reach an order operation")
    for name in ("place_order", "modify_order", "cancel_order"):
        monkeypatch.setattr(cls, name, refuse)


@pytest.fixture
def groww_env(monkeypatch):
    monkeypatch.setenv("GROWW_API_KEY", "TEST-KEY-NOT-REAL")
    monkeypatch.setenv("GROWW_API_SECRET", "TEST-SECRET-NOT-REAL")
    monkeypatch.delenv("GROWW_PROXY_URL", raising=False)


@pytest.fixture
def angel_env(monkeypatch):
    for k, v in {"ANGEL_CLIENT_CODE": "A123", "ANGEL_API_KEY": "TEST-KEY-NOT-REAL", "ANGEL_PIN": "0000",
                 "ANGEL_TOTP_SECRET": "JBSWY3DPEHPK3PXP", "ANGEL_STATIC_IP": "151.245.182.10"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ANGEL_PROXY_URL", raising=False)


class SpyRedis(MemoryRedis):
    instances = []

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        SpyRedis.instances.append(self)


@pytest.mark.asyncio
async def test_groww_capture_logs_in_then_issues_only_the_three_fixed_gets(tmp_path, groww_env, monkeypatch):
    from app.services.groww.client import GrowwClient
    forbid_order_methods(monkeypatch, GrowwClient)
    script = load_script("groww_capture")
    SpyRedis.instances.clear()
    monkeypatch.setattr(script, "MemoryRedis", SpyRedis)
    fake = G.FakeGroww()
    future_token(fake)
    fake.positions_response = G.ok({"positions": [G.position_row()]})
    out = tmp_path / "groww_capture.json"
    rc = await script.capture(str(out), http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake)))
    assert rc == 0 and out.exists()
    seen = [(r.method, r.url.path) for r in fake.requests]
    assert seen == [("POST", "/v1/token/api/access"), ("GET", "/v1/order/list"), ("GET", "/v1/positions/user"),
                    ("GET", "/v1/margins/detail/user")]
    assert all(r.url.host == "api.groww.in" for r in fake.requests)
    for path in ("/v1/order/create", "/v1/order/modify", "/v1/order/cancel"):
        assert fake.calls(path) == []
    text = out.read_text(encoding="utf-8")
    for secret in ("TOK-SECRET-1", "TEST-KEY-NOT-REAL", "TEST-SECRET-NOT-REAL", G.SBIN_ISIN):
        assert secret not in text                                    # shapes only
    assert set(json.loads(text)["endpoints"]) == {"order_list", "positions", "margin"}
    spy = SpyRedis.instances[0]
    assert await spy.exists("groww:session:capture") == 0            # logout wiped the session
    assert await spy.exists("login:lock:groww:capture") == 0         # and no lock is left behind


@pytest.mark.asyncio
@pytest.mark.parametrize("token_response", [(403, {"status": "FAILURE"}), (200, {"token": "", "isActive": True}),
                                            (500, {})], ids=["refused", "no_token", "server_error"])
async def test_groww_capture_failed_login_sends_no_reads_writes_nothing_and_cleans_up(tmp_path, groww_env, monkeypatch, token_response):
    script = load_script("groww_capture")
    SpyRedis.instances.clear()
    monkeypatch.setattr(script, "MemoryRedis", SpyRedis)
    fake = G.FakeGroww()
    fake.token_response = token_response
    out = tmp_path / "groww_capture.json"
    rc = await script.capture(str(out), http_client=httpx.AsyncClient(transport=httpx.MockTransport(fake)))
    assert rc == 2 and not out.exists()
    assert [(r.method, r.url.path) for r in fake.requests] == [("POST", "/v1/token/api/access")]
    spy = SpyRedis.instances[0]
    assert await spy.exists("groww:session:capture") == 0 and await spy.exists("login:lock:groww:capture") == 0


@pytest.mark.asyncio
async def test_angel_capture_logs_in_then_issues_only_the_fixed_gets(tmp_path, angel_env, monkeypatch):
    from app.services.angel_one.client import AngelOneClient
    forbid_order_methods(monkeypatch, AngelOneClient)
    script = load_script("angel_capture")
    SpyRedis.instances.clear()
    monkeypatch.setattr(script, "MemoryRedis", SpyRedis)
    fake = A.FakeAngel()
    fake.routes["getOrderBook"] = (200, {"status": True, "data": []})
    fake.routes["getPosition"] = (200, {"status": True, "data": []})
    fake.routes["getRMS"] = (200, {"status": True, "data": {"availablecash": "1"}})

    async def handler(request):
        await asyncio.sleep(0)
        return fake(request)
    out = tmp_path / "angel_capture.json"
    rc = await script.capture(str(out), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert rc == 0 and out.exists()
    seen = [(r.method, r.url.path.rsplit("/", 1)[-1]) for r in fake.requests]
    assert seen[0] == ("POST", "loginByPassword")
    assert sorted(seen[1:4]) == sorted([("GET", "getOrderBook"), ("GET", "getPosition"), ("GET", "getRMS")])
    # Angel One documents a logout call; the capture's cleanup ends the session with it (a session
    # termination, not an order operation). Nothing else is ever sent.
    assert seen[4:] == [("POST", "logout")] and len(seen) == 5
    assert fake.calls("placeOrder") == [] and fake.calls("modifyOrder") == [] and fake.calls("cancelOrder") == []
    text = out.read_text(encoding="utf-8")
    for secret in ("JWT1", "TEST-KEY-NOT-REAL", "JBSWY3DPEHPK3PXP"):
        assert secret not in text
    spy = SpyRedis.instances[0]
    assert await spy.exists("angel:session:capture") == 0 and await spy.exists("login:lock:angelone:capture") == 0


@pytest.mark.asyncio
async def test_angel_capture_failed_login_sends_no_reads_writes_nothing_and_cleans_up(tmp_path, angel_env, monkeypatch):
    script = load_script("angel_capture")
    SpyRedis.instances.clear()
    monkeypatch.setattr(script, "MemoryRedis", SpyRedis)
    fake = A.FakeAngel()
    fake.routes["loginByPassword"] = (200, {"status": False, "message": "Invalid", "data": None})

    async def handler(request):
        return fake(request)
    out = tmp_path / "angel_capture.json"
    rc = await script.capture(str(out), http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert rc == 2 and not out.exists()
    assert [r.url.path.rsplit("/", 1)[-1] for r in fake.requests] == ["loginByPassword"]
    spy = SpyRedis.instances[0]
    assert await spy.exists("login:lock:angelone:capture") == 0


# ── safeguards that must not have moved ──────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["groww_capture", "angel_capture"])
def test_authorization_and_dry_run_safeguards_are_intact_and_no_order_call_exists(name):
    src = (SCRIPTS / f"{name}.py").read_text(encoding="utf-8")
    assert "--i-am-authorized" in src and "Dry run - nothing contacted" in src and "args.execute" in src
    assert "place_order" not in src and "modify_order" not in src and "cancel_order" not in src
    assert '"POST"' not in src and "http_client=None" in src
    assert "MemoryRedis()" in src and "class _MemoryRedis" not in src
