"""
Client-side rate limiting (brokers/rate_limit.py) and its use by the Angel One adapter.

No limit is assumed anywhere: windows come from ANGEL_ONE_ORDER_RATE_LIMITS / ANGEL_ONE_READ_RATE_LIMITS, empty by
default. The limiter acts strictly BEFORE a request is sent, so it can never duplicate or hide an order outcome.
Offline: mocked transport, virtual clock.
"""
import httpx
import pytest

from app.core.config import Settings, settings
from app.services.alice_blue.types import OrderResultCategory
from app.services.brokers.rate_limit import (
    RateLimitConfigError, RateLimitExceeded, SlidingWindowLimiter, parse_rate_limits,
)
from app.services.reliability.retry import RetryPolicy
import angel_helpers as A
from test_order_send_safety import angel, angel_creates, order, policy


# ── parsing ─────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    (None, ()), ("", ()), ("   ", ()), ("10/1", ((10, 1.0),)), ("10/1,200/60", ((10, 1.0), (200, 60.0))),
    (" 5 / 1 , 100/30 ".replace(" / ", "/"), ((5, 1.0), (100, 30.0))), ("3/0.5", ((3, 0.5),)),
])
def test_valid_configuration(text, expected):
    assert parse_rate_limits(text) == expected


@pytest.mark.parametrize("text", ["10", "10/", "/1", "a/b", "10/1/2", "0/1", "-1/1", "10/0", "10/-5", "10/nan", "10/inf",
                                  "10/1,", ",10/1", "10/1;20/2", "10/100000"])
def test_malformed_configuration_raises_and_is_never_ignored(text):
    with pytest.raises(RateLimitConfigError):
        parse_rate_limits(text)


def test_no_limit_is_configured_by_default():
    assert Settings.model_fields["ANGEL_ONE_ORDER_RATE_LIMITS"].default == ""
    assert Settings.model_fields["ANGEL_ONE_READ_RATE_LIMITS"].default == ""
    assert settings.ANGEL_ONE_ORDER_RATE_LIMITS == "" and settings.ANGEL_ONE_READ_RATE_LIMITS == ""


# ── the limiter ─────────────────────────────────────────────────────────────────────────────

class VClock:
    def __init__(self):
        self.t = 100.0
        self.slept = []

    def now(self):
        return self.t

    async def sleep(self, s):
        self.slept.append(s)
        self.t += s


@pytest.mark.asyncio
async def test_empty_configuration_never_limits_or_waits():
    clock = VClock()
    lim = SlidingWindowLimiter((), now=clock.now, sleep=clock.sleep)
    assert lim.enabled is False
    for _ in range(1000):
        await lim.acquire()
    assert clock.slept == []


@pytest.mark.asyncio
async def test_waits_for_a_slot_within_the_allowed_time_then_proceeds():
    clock = VClock()
    lim = SlidingWindowLimiter(((2, 1.0),), max_wait=2.0, now=clock.now, sleep=clock.sleep)
    await lim.acquire()
    await lim.acquire()
    await lim.acquire()                       # needs ~1s: within max_wait, so it waits (virtually) and proceeds
    assert clock.slept and 0 < clock.slept[0] <= 1.0


@pytest.mark.asyncio
async def test_refuses_when_the_wait_would_exceed_the_limit():
    clock = VClock()
    lim = SlidingWindowLimiter(((2, 60.0),), max_wait=2.0, now=clock.now, sleep=clock.sleep)
    await lim.acquire()
    await lim.acquire()
    with pytest.raises(RateLimitExceeded):
        await lim.acquire()
    assert clock.slept == []                  # it refused rather than sleeping a minute


@pytest.mark.asyncio
async def test_every_configured_window_applies():
    clock = VClock()
    lim = SlidingWindowLimiter(((10, 1.0), (3, 60.0)), max_wait=2.0, now=clock.now, sleep=clock.sleep)
    for _ in range(3):
        await lim.acquire()
        clock.t += 0.2
    with pytest.raises(RateLimitExceeded):    # well under 10/s, but the 3-per-minute window is full
        await lim.acquire()
    clock.t += 61
    await lim.acquire()                       # the window slid


# ── Angel One adapter ───────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_without_configuration_the_adapter_is_not_limited(mock_redis):
    c, fake = await angel(mock_redis)
    for i in range(25):
        res = await c.place_order(order(client_order_id=f"JT_LIM_NOLIMIT{i}_acc12345"))
        assert res.category == OrderResultCategory.SUCCESS
    assert len(angel_creates(fake)) == 25


@pytest.mark.asyncio
async def test_a_refused_order_is_rejected_not_sent_and_not_retried(mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "ANGEL_ONE_ORDER_RATE_LIMITS", "2/60")
    c, fake = await angel(mock_redis)
    results = [await c.place_order(order(client_order_id=f"JT_LIM_RL{i}_acc12345")) for i in range(3)]
    assert [r.category for r in results] == [OrderResultCategory.SUCCESS, OrderResultCategory.SUCCESS, OrderResultCategory.REJECTED]
    assert "not sent" in results[2].message.lower()
    assert len(angel_creates(fake)) == 2                                   # the third never left the process


@pytest.mark.asyncio
async def test_the_real_retry_policy_does_not_retry_a_refusal(mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "ANGEL_ONE_ORDER_RATE_LIMITS", "1/60")
    c, fake = await angel(mock_redis)
    await c.place_order(order(client_order_id="JT_LIM_FIRST_acc12345"))
    out = await policy().execute(c.place_order, order(client_order_id="JT_LIM_SECOND_acc12345"))
    assert out.category == OrderResultCategory.REJECTED
    assert len(angel_creates(fake)) == 1


@pytest.mark.asyncio
async def test_an_ambiguous_send_is_unchanged_by_the_limiter_and_still_never_resent(mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "ANGEL_ONE_ORDER_RATE_LIMITS", "5/60")
    c, fake = await angel(mock_redis)
    fake.place_exception = httpx.ReadError("reset after send")
    out = await policy().execute(c.place_order, order())
    assert out.category == OrderResultCategory.TIMEOUT
    assert len(angel_creates(fake)) == 1                                   # one slot used, one request, no resend


@pytest.mark.asyncio
async def test_order_and_read_limits_are_separate(mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "ANGEL_ONE_ORDER_RATE_LIMITS", "1/60")
    monkeypatch.setattr(settings, "ANGEL_ONE_READ_RATE_LIMITS", "")
    c, fake = await angel(mock_redis)
    fake.routes["getPosition"] = (200, {"status": True, "data": []})
    await c.place_order(order(client_order_id="JT_LIM_SEP_acc12345"))
    for _ in range(5):                                                      # reads are not charged to the order budget
        assert await c.get_positions_or_none() == []


@pytest.mark.asyncio
async def test_a_read_over_its_limit_is_unknown_so_a_sell_stays_blocked(mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "ANGEL_ONE_READ_RATE_LIMITS", "1/60")
    c, fake = await angel(mock_redis)
    fake.routes["getPosition"] = (200, {"status": True, "data": []})
    assert await c.get_positions_or_none() == []
    assert await c.get_positions_or_none() is None                          # refused -> "unknown", never "flat"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["10", "a/b", "0/1", "10/1,"])
async def test_an_invalid_configuration_fails_closed_for_orders_and_reads(mock_redis, monkeypatch, bad):
    monkeypatch.setattr(settings, "ANGEL_ONE_ORDER_RATE_LIMITS", bad)
    c, fake = await angel(mock_redis)
    before = len(fake.requests)
    res = await c.place_order(order())
    assert res.category == OrderResultCategory.REJECTED and "configuration is invalid" in res.message
    assert await c.get_positions_or_none() is None
    assert (await c.cancel_order("X1")).category == OrderResultCategory.REJECTED
    assert len(fake.requests) == before                                     # nothing at all was sent
