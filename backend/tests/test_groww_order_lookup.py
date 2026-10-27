"""Groww public per-order queries (offline): read-only, 404 is the only "not found", everything else raises."""
import pytest

from app.services.groww.client import GrowwCapabilityUnavailable, GrowwError
from groww_helpers import FakeGroww, make_client, ok, order_row


async def logged_in(redis, fake):
    c = make_client(redis, fake)
    await c.login()
    return c


@pytest.mark.asyncio
async def test_get_order_reads_detail_and_restores_the_client_order_id(mock_redis):
    fake = FakeGroww()
    c = await logged_in(mock_redis, fake)
    ref = "JTabc123456789012345"
    await mock_redis.set(f"groww:ref:acc-groww:{ref}", "JT_ORIG")
    fake.detail_response = ok(order_row(oid="GRW9", status="OPEN", filled=0, ref=ref))
    rec = await c.get_order("GRW9")
    assert rec.broker_order_id == "GRW9" and rec.client_order_id == "JT_ORIG" and rec.status == "OPEN"
    assert not fake.calls("/v1/order/create") and not fake.calls("/v1/order/modify") and not fake.calls("/v1/order/cancel")


@pytest.mark.asyncio
async def test_get_order_404_is_none_and_other_failures_raise(mock_redis):
    fake = FakeGroww()
    c = await logged_in(mock_redis, fake)
    fake.detail_response = (404, {"status": "FAILURE"})
    assert await c.get_order("GRW9") is None
    fake.detail_response = (500, {"status": "FAILURE"})
    with pytest.raises(GrowwError):
        await c.get_order("GRW9")
    fake.detail_response = ok(order_row(oid="OTHER"))
    with pytest.raises(GrowwCapabilityUnavailable):
        await c.get_order("GRW9")
    bad = order_row(oid="GRW9")
    bad["quantity"] = None
    fake.detail_response = ok(bad)
    with pytest.raises(GrowwCapabilityUnavailable):
        await c.get_order("GRW9")
    with pytest.raises(GrowwError):
        await c.get_order("  ")


@pytest.mark.asyncio
async def test_find_by_client_reference_resolves_then_reads_in_full(mock_redis):
    fake = FakeGroww()
    c = await logged_in(mock_redis, fake)
    assert await c.find_by_client_reference("JTnone") is None            # default fake: 404
    fake.reference_response = ok({"groww_order_id": "GRW7", "order_status": "OPEN"})
    fake.detail_response = ok(order_row(oid="GRW7", status="OPEN", filled=0))
    rec = await c.find_by_client_reference("JTx")
    assert rec.broker_order_id == "GRW7"
    fake.reference_response = ok({})
    assert await c.find_by_client_reference("JTx") is None
    with pytest.raises(GrowwError):
        await c.find_by_client_reference("")
