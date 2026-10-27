"""Broker-neutral Master source: detection facts, normalization with broker-aware identity, and the fail-closed adapter."""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.core.config import settings
from app.services.alice_blue.types import OrderRecord, OrderSide
from app.services.brokers import master_source as ms
from app.services.brokers.order_book_read import OrderBookState


def rec(oid="123", status="OPEN", **kw):
    base = dict(broker_order_id=oid, client_order_id=None, symbol="SBIN", exchange="NSE", side=OrderSide.BUY, quantity=10,
                filled_quantity=0, average_price=Decimal("0"), status=status,
                order_timestamp=datetime(2026, 10, 27, 4, 0, tzinfo=timezone.utc), order_type="LIMIT", product_type="CNC",
                price=Decimal("800"), instrument_id="74056")
    base.update(kw)
    return OrderRecord(**base)


class Client:
    def __init__(self, book=None, exc=None):
        self.book, self.exc = book or [], exc

    async def get_order_book(self):
        if self.exc:
            raise self.exc
        return self.book


def test_dispatchable_statuses_match_the_poller():
    from app.services.reliability.master_order_poller import BROKER_STATUS_MAP
    assert ms.DISPATCHABLE_STATUSES == frozenset(BROKER_STATUS_MAP)


def test_every_non_alice_master_is_unverified_with_a_recorded_blocker():
    assert ms.MASTER_DETECTION["alicebluenew"].external_orders_verified and ms.MASTER_DETECTION["alicebluenew"].blocker is None
    for b in ("groww", "angelone", "zerodha"):
        info = ms.MASTER_DETECTION[b]
        assert not info.external_orders_verified and info.blocker and ms.detection_info(b) is info
    assert ms.detection_info("dhan") is None and ms.detection_info("kite") is ms.MASTER_DETECTION["zerodha"]


def test_normalization_scopes_non_alice_ids_and_keeps_alice_unchanged():
    a = ms.normalize_order("alicebluenew", "A1", rec())
    z = ms.normalize_order("zerodha", "Z1", rec(status="PARTIAL"))
    assert a.event_scope == "" and a.dedupe_key == "123" and a.broker_id == "alicebluenew"
    assert z.event_scope == "zerodha:Z1:" and z.dedupe_key == "zerodha:Z1:123"
    assert a.dedupe_key != z.dedupe_key
    assert z.side == "BUY" and z.dispatchable and z.instrument_id == "74056"


@pytest.mark.parametrize("status,ok", [("OPEN", True), ("partial", True), ("TRIGGER_PENDING", True), ("VALIDATION PENDING", False),
                                       ("UNSUPPORTED_VARIETY_AMO", False), ("", False)])
def test_dispatchable_flag(status, ok):
    assert ms.normalize_order("zerodha", "Z1", rec(status=status)).dispatchable is ok


@pytest.mark.asyncio
async def test_adapter_refuses_every_closed_master_gate():
    for b in ("groww", "angelone", "zerodha", "dhan", "bogus"):
        got = await ms.MasterSourceAdapter(b, "X", Client([rec()])).read()
        assert got.state == OrderBookState.UNKNOWN and got.reason == ms.MASTER_NOT_OPEN and not got.records


@pytest.mark.asyncio
async def test_adapter_reads_for_an_open_master():
    got, orders = await ms.MasterSourceAdapter("alicebluenew", "A1", Client([rec("7"), rec("8", "COMPLETE", filled_quantity=10)])).read_normalized(checked=False)
    assert got.state == OrderBookState.ORDERS and [o.native_order_id for o in orders] == ["7", "8"]
    empty, none = await ms.MasterSourceAdapter("alicebluenew", "A1", Client([])).read_normalized(checked=False)
    assert not empty.state == OrderBookState.ORDERS and none == ()


@pytest.mark.asyncio
async def test_adapter_surfaces_failures_and_missing_client():
    failed = await ms.MasterSourceAdapter("alicebluenew", "A1", Client(exc=RuntimeError("boom secret"))).read()
    assert failed.state == OrderBookState.FAILED and "secret" not in failed.detail
    nc = await ms.MasterSourceAdapter("alicebluenew", "A1", None).read()
    assert nc.state == OrderBookState.UNKNOWN


@pytest.mark.asyncio
async def test_a_master_gate_opened_for_zerodha_reads_through_the_same_path(monkeypatch):
    from app.services.brokers import registry
    from app.services.zerodha.contract import ZerodhaContract
    monkeypatch.setattr(settings, "ZERODHA_ENABLED", True)
    monkeypatch.setattr(settings, "ZERODHA_MASTER_ENABLED", True)
    monkeypatch.setattr(registry, "ZERODHA_CONTRACT", ZerodhaContract(master_verified=True))
    got, orders = await ms.MasterSourceAdapter("zerodha", "Z1", Client([rec("9")])).read_normalized(checked=False)
    assert got.state == OrderBookState.ORDERS and orders[0].dedupe_key == "zerodha:Z1:9"


@pytest.mark.asyncio
async def test_unnormalizable_record_fails_the_read():
    bad = rec()
    bad.quantity = "not-a-number"
    got, orders = await ms.MasterSourceAdapter("alicebluenew", "A1", Client([bad])).read_normalized(checked=False)
    assert got.state == OrderBookState.FAILED and got.reason == ms.NORMALIZE_FAILED and orders == ()
