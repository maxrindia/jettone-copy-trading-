"""
The Groww F&O Child follows the Alice Blue Child's policies - nothing new, nothing weaker. OFFLINE: fakes only.

Alice Blue's F&O Child is guarded by (a) the SELL position guard (`get_available_long`, fail closed, one position book for every segment),
(b) the per-Child RiskEngine limits (max_trade_value, max_exposure, margin, daily loss), (c) the Copy Quantity settings, and (d) the broker's own
lot / freeze rules. There is NO lot cap. These tests show the Groww Child is held to exactly the same, through the same shared code:

  * the SELL guard sees the Child's F&O position in the same book (exchange NFO, exact symbol, carry / intraday product), refuses on a flat,
    short, wrong-product, too-small or unreadable book, and is skipped by SELL_POSITION_SAFETY_BYPASS exactly as it is for Alice Blue;
  * the RiskEngine limits apply to a Groww F&O order;
  * the reconciliation sees the F&O position;
  * the position book of a Groww Child with every F&O gate closed is the cash book it always was.
"""
import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.accounts import CopySetting
from app.models.trades import CopyTrade
from app.services.groww.client import GrowwError
from groww_helpers import make_client as make_groww_client, ok, position_row
from test_fno_child_validation import (CHILD_IP, GROWW_SYMBOL, GrowwFnoChild, alice_event, groww_child, groww_open,  # noqa: F401
                                       groww_send, new_pipeline, set_policy, skipped)


class GrowwWithPositions(GrowwFnoChild):
    """A Groww account that answers the position book per segment, as Groww's does."""

    def __init__(self):
        super().__init__()
        self.positions_by_segment = {"CASH": ok({"positions": []}), "FNO": ok({"positions": []})}
        self.position_failure = None

    async def __call__(self, request):
        if request.url.path == "/v1/positions/user":
            self.requests.append(request)
            segment = request.url.params.get("segment")
            if segment == "FNO" and self.position_failure is not None:
                import httpx
                return httpx.Response(self.position_failure[0], json=self.position_failure[1])
            import httpx
            status, body = self.positions_by_segment.get(segment, ok({"positions": []}))
            return httpx.Response(status, json=body)
        return await super().__call__(request)

    def fno_position(self, qty, *, symbol=GROWW_SYMBOL, product="NRML", exchange="NSE"):
        row = {**position_row(symbol=symbol, exchange=exchange, product=product, qty=qty), "segment": "FNO"}
        self.positions_by_segment["FNO"] = ok({"positions": [*self.positions_by_segment["FNO"][1]["payload"]["positions"], row]})

    def segments_read(self):
        return [r.url.params.get("segment") for r in self.requests if r.url.path == "/v1/positions/user"]


def sell(master, oid, *, qty=75, product="LONGTERM"):
    return alice_event(master, oid, side="SELL", qty=qty, product=product)


async def groww_sell(db_session, mock_redis, store, *, prepare=None, qty=75, product="LONGTERM", oid="S-1"):
    fake = GrowwWithPositions()
    if prepare:
        prepare(fake)
    master, child, fake, client, pipeline = await groww_send(db_session, mock_redis, store, None, fake=fake)
    res = await pipeline.execute_replication(event=sell(master, oid, qty=qty, product=product), clients={str(child.id): client})
    return fake, client, child, res["results"][str(child.id)]


# ── the position book ─────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_f_and_o_position_is_in_the_same_book_as_an_alice_blue_derivative_position(db_session, mock_redis, groww_open):
    fake = GrowwWithPositions()
    fake.fno_position(75)
    client = make_groww_client(mock_redis, fake, account_id="acc-g", static_ip=CHILD_IP, store=groww_open)
    await client.login()
    (pos,) = await client.get_positions()
    assert (pos.exchange, pos.symbol, pos.product_type, pos.quantity, pos.side, pos.instrument_id) == ("NFO", GROWW_SYMBOL, "NRML", 75, "BUY", None)
    assert set(fake.segments_read()) == {"CASH", "FNO"}


@pytest.mark.asyncio
async def test_a_closed_f_and_o_row_and_the_cash_rows_are_read_as_before(db_session, mock_redis, groww_open):
    fake = GrowwWithPositions()
    fake.positions_by_segment["CASH"] = ok({"positions": [{**position_row(symbol="SBIN", exchange="NSE", product="MIS", qty=3), "segment": "CASH"}]})
    fake.fno_position(0)
    client = make_groww_client(mock_redis, fake, account_id="acc-g", static_ip=CHILD_IP, store=groww_open)
    await client.login()
    positions = await client.get_positions()
    assert sorted((p.exchange, p.quantity) for p in positions) == [("NFO", 0), ("NSE", 3)]


@pytest.mark.asyncio
@pytest.mark.parametrize("unit,raw,expected", [("UNITS", 75, 75), ("AUTO", 150, 150), ("LOTS", 2, 150)])
async def test_the_position_quantity_follows_the_configured_unit(db_session, mock_redis, groww_open, monkeypatch, unit, raw, expected):
    monkeypatch.setattr(settings, "GROWW_FNO_QUANTITY_UNIT", unit)
    fake = GrowwWithPositions()
    fake.fno_position(raw)
    client = make_groww_client(mock_redis, fake, account_id="acc-g", static_ip=CHILD_IP, store=groww_open)
    await client.login()
    assert [p.quantity for p in await client.get_positions()] == [expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("prepare", [
    lambda f: f.fno_position(75, symbol="NIFTY26NOV99999CE"),                       # an open position the instrument master does not know
    lambda f: f.fno_position(74),                                                   # not a whole number of lots
    lambda f: f.fno_position(75, product="CNC"),                                    # not an F&O product
    lambda f: f.fno_position(75, exchange="BSE"),                                   # not an NSE F&O row
    lambda f: setattr(f, "position_failure", (500, {"status": "FAILURE"})),         # the F&O book cannot be read
], ids=["unknown-instrument", "not-lots", "cash-product", "bse", "unreadable"])
async def test_an_f_and_o_position_book_that_cannot_be_read_with_confidence_is_unknown_never_flat(db_session, mock_redis, groww_open, prepare):
    fake = GrowwWithPositions()
    prepare(fake)
    client = make_groww_client(mock_redis, fake, account_id="acc-g", static_ip=CHILD_IP, store=groww_open)
    await client.login()
    with pytest.raises(GrowwError):
        await client.get_positions()
    assert await client.get_positions_or_none() is None                              # the guard's fail-closed answer


@pytest.mark.asyncio
async def test_with_every_f_and_o_gate_closed_the_position_book_is_the_cash_book_it_always_was(db_session, mock_redis, groww_open, monkeypatch):
    monkeypatch.setattr(settings, "GROWW_FNO_CHILD_ENABLED", False)
    monkeypatch.setattr(settings, "GROWW_FNO_MASTER_ENABLED", False)
    fake = GrowwWithPositions()
    fake.fno_position(75)
    client = make_groww_client(mock_redis, fake, account_id="acc-g", static_ip=CHILD_IP, store=groww_open)
    await client.login()
    assert await client.get_positions() == [] and set(fake.segments_read()) == {"CASH"}


# ── the SELL guard: the same shared code as Alice Blue ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_sell_covered_by_a_confirmed_long_is_sent(db_session, mock_redis, groww_open):
    fake, client, child, res = await groww_sell(db_session, mock_redis, groww_open, prepare=lambda f: f.fno_position(75))
    (body,) = fake.bodies("/v1/order/create")
    assert res["status"] == "SUCCESS" and (body["transaction_type"], body["segment"], body["product"], body["quantity"]) == ("SELL", "FNO", "NRML", 75)


@pytest.mark.asyncio
@pytest.mark.parametrize("prepare,label", [
    (lambda f: None, "flat"),
    (lambda f: f.fno_position(-75), "net short"),
    (lambda f: f.fno_position(75, product="MIS"), "long in another product"),
    (lambda f: f.fno_position(75, symbol="NIFTY26NOV23500PE"), "long in another contract"),
    (lambda f: setattr(f, "position_failure", (500, {"status": "FAILURE"})), "unreadable book"),
], ids=lambda v: v if isinstance(v, str) else "")
async def test_a_sell_with_no_confirmed_long_is_refused_and_nothing_is_sent(db_session, mock_redis, groww_open, prepare, label):
    fake, client, child, res = await groww_sell(db_session, mock_redis, groww_open, prepare=prepare, oid=f"S-{label}")
    assert fake.bodies("/v1/order/create") == [] and res["status"] != "SUCCESS", (label, res)


@pytest.mark.asyncio
async def test_a_sell_larger_than_the_confirmed_long_is_refused_whole(db_session, mock_redis, groww_open):
    fake, client, child, res = await groww_sell(db_session, mock_redis, groww_open, prepare=lambda f: f.fno_position(75), qty=150, oid="S-big")
    assert fake.bodies("/v1/order/create") == [] and res["status"] != "SUCCESS"


@pytest.mark.asyncio
async def test_the_existing_bypass_flag_skips_the_guard_exactly_as_it_does_for_alice_blue(db_session, mock_redis, groww_open, monkeypatch):
    monkeypatch.setattr(settings, "SELL_POSITION_SAFETY_BYPASS", True)
    fake, client, child, res = await groww_sell(db_session, mock_redis, groww_open, oid="S-bypass")                  # flat book, guard bypassed
    assert res["status"] == "SUCCESS" and len(fake.bodies("/v1/order/create")) == 1


# ── the other Alice Blue Child policies apply unchanged ─────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_per_child_risk_limits_apply_to_a_groww_f_and_o_order(db_session, mock_redis, groww_open):
    master, child = await groww_child(db_session)
    async with __import__("conftest").TestingSessionLocal() as s:
        row = (await s.execute(select(CopySetting).where(CopySetting.follower_account_id == child.id))).scalar_one()
        row.max_trade_value = Decimal("1")                                           # the order is worth about 75 x 2.35
        await s.commit()
    fake = GrowwWithPositions()
    client = make_groww_client(mock_redis, fake, account_id=str(child.id), static_ip=CHILD_IP, store=groww_open)
    await client.login()
    res = await new_pipeline(mock_redis).execute_replication(event=alice_event(master, "R-1"), clients={str(child.id): client})
    assert fake.bodies("/v1/order/create") == [] and "MAX_TRADE_VALUE_BREACHED" in json.dumps(res, default=str)


@pytest.mark.asyncio
async def test_the_reconciliation_sees_the_groww_f_and_o_position(db_session, mock_redis, groww_open):
    from app.services.reliability.follower_fill_syncer import FollowerFillSyncer
    from app.services.reliability.reconciliation import ReconciliationEngine
    from conftest import TestingSessionLocal

    fake = GrowwWithPositions()
    master, child, fake, client, pipeline = await groww_send(db_session, mock_redis, groww_open, None, fake=fake)
    await pipeline.execute_replication(event=alice_event(master, "REC-1"), clients={str(child.id): client})
    fake.fill(fake.only, 75)
    await FollowerFillSyncer(follower_clients_provider=lambda: {str(child.id): client}, pipeline=pipeline).sync_once()
    fake.fno_position(75)
    async with TestingSessionLocal() as db:
        assert await ReconciliationEngine.run_for_account(child.id, db, broker_client=client) == []
    fake.positions_by_segment["FNO"] = ok({"positions": []})                         # the broker shows nothing: a real mismatch
    async with TestingSessionLocal() as db:
        records = await ReconciliationEngine.run_for_account(child.id, db, broker_client=client)
    assert [(r.expected_qty, r.actual_qty) for r in records] == [(75, 0)]


# ── audit hardening ──────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_master_only_f_and_o_flag_never_couples_the_childs_position_book_to_the_f_and_o_endpoint(db_session, mock_redis, groww_open, monkeypatch):
    monkeypatch.setattr(settings, "GROWW_FNO_CHILD_ENABLED", False)
    monkeypatch.setattr(settings, "GROWW_FNO_MASTER_ENABLED", True)
    fake = GrowwWithPositions()
    fake.position_failure = (500, {"status": "FAILURE"})                              # the F&O list is down
    client = make_groww_client(mock_redis, fake, account_id="acc-g", static_ip=CHILD_IP, store=groww_open)
    await client.login()
    assert await client.get_positions() == [] and set(fake.segments_read()) == {"CASH"}


@pytest.mark.asyncio
@pytest.mark.parametrize("segment", [None, "", "SPOT", "fno"])
async def test_an_f_and_o_row_that_does_not_say_which_segment_it_is_in_is_never_silently_dropped(db_session, mock_redis, groww_open, segment):
    fake = GrowwWithPositions()
    row = position_row(symbol=GROWW_SYMBOL, exchange="NSE", product="NRML", qty=75)
    row.pop("segment", None)
    if segment is not None:
        row["segment"] = segment
    fake.positions_by_segment["FNO"] = ok({"positions": [row]})
    client = make_groww_client(mock_redis, fake, account_id="acc-g", static_ip=CHILD_IP, store=groww_open)
    await client.login()
    if segment == "fno":                                                              # case differences are the same segment
        assert [p.quantity for p in await client.get_positions()] == [75]
    else:
        assert await client.get_positions_or_none() is None                           # an unattributable row makes the book unknown, never flat
