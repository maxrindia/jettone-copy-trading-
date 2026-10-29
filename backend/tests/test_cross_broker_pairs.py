"""
The twelve cross-broker Master -> Child pairs (every ordered pair of Alice Blue, Groww, Zerodha and Angel One with different brokers).

Fixtures and mocks only - no broker, Redis, network or production access (the `no_network` guard is reused). The pair gate is waived inside
each test by `all_pairs_open_for_test`; the shipped allowlist still refuses all twelve (test_broker_pair_gate.py). Passing here is CODE-LEVEL
evidence about the engine, not proof that any real broker accepts an order.

Covered per pair: a derivative is never copied across brokers (no verified identity exists, whatever CANONICAL_MAPPING_ENFORCED says);
cash equity still reaches the Child without the Master's token; a broker rejection, an ambiguous send, a repeated Master event and a Child
with no session each behave the same on every pair.
"""
import itertools

import pytest

from app.core.config import settings
from app.models.trades import CopyTradeStatus
from app.services.alice_blue.types import OrderResultCategory
from app.services.copy_engine import pipeline as _pipeline  # noqa: F401  (import before planner: planner <-> copy_engine cycle)
from app.services.instruments.planner import INSTRUMENT_UNMAPPED, cross_broker_derivative_skip
from test_multi_broker_fanout import (  # noqa: F401  (fixtures are used by name)
    BROKERS, ORDERS, _result, all_pairs_open_for_test, build_strategy, copies_of, entry, no_network, pipeline,
)
from test_order_handling_matrix import _event
from test_strategy_master_switching import _child_client

CROSS_PAIRS = [(m, c) for m, c in itertools.product(BROKERS, BROKERS) if m != c]
SAME_PAIRS = [(b, b) for b in BROKERS]
DERIVATIVE_EXCHANGES = ("NFO", "BFO", "MCX", "CDS", "BCD")


def test_there_are_exactly_twelve_cross_broker_pairs():
    assert len(CROSS_PAIRS) == 12


def nfo_entry(master, oid, order_type, trigger="0"):
    return _event(master, oid, side="BUY", order_type=order_type, product="NRML", exchange="NFO", symbol="NIFTY24NOV24500CE",
                  qty=75, price="100", trigger=trigger, token="45100")


# ── the pure check ────────────────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("master_broker,child_broker", CROSS_PAIRS)
@pytest.mark.parametrize("exchange", DERIVATIVE_EXCHANGES + ("", "XYZ"))
def test_a_derivative_or_unknown_exchange_is_unmapped_across_brokers(master_broker, child_broker, exchange):
    skip = cross_broker_derivative_skip(master_broker=master_broker, child_broker=child_broker, exchange=exchange)
    assert skip is not None and skip.code == INSTRUMENT_UNMAPPED
    assert skip.detail.startswith(f"CROSS_BROKER_DERIVATIVE_UNMAPPED:{master_broker}->{child_broker}:")


@pytest.mark.parametrize("master_broker,child_broker", CROSS_PAIRS)
@pytest.mark.parametrize("exchange", ("NSE", "BSE", " nse "))
def test_cash_equity_is_left_to_the_existing_checks(master_broker, child_broker, exchange):
    assert cross_broker_derivative_skip(master_broker=master_broker, child_broker=child_broker, exchange=exchange) is None


@pytest.mark.parametrize("master_broker,child_broker", SAME_PAIRS)
@pytest.mark.parametrize("exchange", DERIVATIVE_EXCHANGES)
def test_same_broker_derivatives_are_unchanged(master_broker, child_broker, exchange):
    assert cross_broker_derivative_skip(master_broker=master_broker, child_broker=child_broker, exchange=exchange) is None


def test_a_null_stored_broker_is_alice_blue_on_either_side():
    assert cross_broker_derivative_skip(master_broker="alicebluenew", child_broker=None, exchange="NFO") is None
    assert cross_broker_derivative_skip(master_broker="groww", child_broker=None, exchange="NFO").code == INSTRUMENT_UNMAPPED


def test_an_unknown_master_broker_is_left_to_the_pair_gate():
    # check_pair refuses MASTER_BROKER_UNKNOWN before the planner runs; this check does not duplicate it
    assert cross_broker_derivative_skip(master_broker=None, child_broker="groww", exchange="NFO") is None


# ── through the pipeline, for every cross-broker pair ────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("master_broker,child_broker", CROSS_PAIRS)
@pytest.mark.parametrize("order_type,trigger", [("LIMIT", "0"), ("MARKET", "0"), ("SL", "95")])
async def test_a_cross_broker_derivative_is_never_sent_even_with_mapping_enforcement_off(
        db_session, mock_redis, all_pairs_open_for_test, monkeypatch, master_broker, child_broker, order_type, trigger):
    monkeypatch.setattr(settings, "CANONICAL_MAPPING_ENFORCED", False)
    master, children = await build_strategy(db_session, master_broker, [child_broker], f"xd-{master_broker}-{child_broker}-{order_type}")
    child = children[0]
    client = _child_client("BRK_D")
    res = await pipeline(mock_redis).execute_replication(
        event=nfo_entry(master, f"XD-{master_broker}-{child_broker}-{order_type}", order_type, trigger), clients={str(child.id): client})

    out = res["results"][str(child.id)]
    assert out["status"] == "SKIPPED" and out["reason"] == INSTRUMENT_UNMAPPED, out
    assert client.place_order.await_count == 0                                   # nothing reached the Child's broker
    copies = await copies_of(child)
    assert len(copies) == 1 and copies[0].status != CopyTradeStatus.SUCCESS      # the skip is recorded for that Child


@pytest.mark.asyncio
@pytest.mark.parametrize("master_broker,child_broker", SAME_PAIRS)
async def test_a_same_broker_derivative_still_copies_with_the_masters_token(
        db_session, mock_redis, all_pairs_open_for_test, monkeypatch, master_broker, child_broker):
    monkeypatch.setattr(settings, "CANONICAL_MAPPING_ENFORCED", False)
    master, children = await build_strategy(db_session, master_broker, [child_broker], f"sd-{master_broker}")
    child = children[0]
    client = _child_client("BRK_S")
    res = await pipeline(mock_redis).execute_replication(
        event=nfo_entry(master, f"SD-{master_broker}", "LIMIT"), clients={str(child.id): client})
    assert res["results"][str(child.id)]["status"] == "SUCCESS", res["results"]
    assert client.place_order.await_args.args[0].instrument_token == "45100"


@pytest.mark.asyncio
@pytest.mark.parametrize("master_broker,child_broker", CROSS_PAIRS)
async def test_cash_equity_reaches_the_child_without_the_masters_token(
        db_session, mock_redis, all_pairs_open_for_test, master_broker, child_broker):
    master, children = await build_strategy(db_session, master_broker, [child_broker], f"xe-{master_broker}-{child_broker}")
    child = children[0]
    client = _child_client("BRK_E")
    res = await pipeline(mock_redis).execute_replication(
        event=entry(master, f"XE-{master_broker}-{child_broker}", "LIMIT", "0"), clients={str(child.id): client})
    assert res["results"][str(child.id)]["status"] == "SUCCESS", res["results"]
    request = client.place_order.await_args.args[0]
    assert request.instrument_token is None and request.symbol == "SBIN"          # resolved from the Child's own catalog


@pytest.mark.asyncio
@pytest.mark.parametrize("master_broker,child_broker", CROSS_PAIRS)
async def test_a_broker_rejection_is_recorded_and_not_retried(db_session, mock_redis, all_pairs_open_for_test, master_broker, child_broker):
    master, children = await build_strategy(db_session, master_broker, [child_broker], f"xr-{master_broker}-{child_broker}")
    child = children[0]
    client = _child_client("BRK_R")
    client.place_order.return_value = _result(OrderResultCategory.REJECTED, None, "RMS: insufficient")
    res = await pipeline(mock_redis).execute_replication(
        event=entry(master, f"XR-{master_broker}-{child_broker}", "LIMIT", "0"), clients={str(child.id): client})
    assert res["results"][str(child.id)]["status"] == "REJECTED"
    assert client.place_order.await_count == 1
    copies = await copies_of(child)
    assert len(copies) == 1 and copies[0].status != CopyTradeStatus.SUCCESS


@pytest.mark.asyncio
@pytest.mark.parametrize("master_broker,child_broker", CROSS_PAIRS)
async def test_an_ambiguous_send_is_never_resent(db_session, mock_redis, all_pairs_open_for_test, master_broker, child_broker):
    master, children = await build_strategy(db_session, master_broker, [child_broker], f"xt-{master_broker}-{child_broker}")
    child = children[0]
    client = _child_client("BRK_T")
    client.place_order.return_value = _result(OrderResultCategory.TIMEOUT, None, "no answer")
    res = await pipeline(mock_redis).execute_replication(
        event=entry(master, f"XT-{master_broker}-{child_broker}", "MARKET", "0"), clients={str(child.id): client})
    assert res["results"][str(child.id)]["status"] == "TIMEOUT"
    assert client.place_order.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("master_broker,child_broker", CROSS_PAIRS)
@pytest.mark.parametrize("order_type,trigger", ORDERS)
async def test_a_repeated_master_event_places_one_order(
        db_session, mock_redis, all_pairs_open_for_test, master_broker, child_broker, order_type, trigger):
    master, children = await build_strategy(db_session, master_broker, [child_broker], f"xu-{master_broker}-{child_broker}-{order_type}")
    child = children[0]
    client = _child_client("BRK_U")
    pipe = pipeline(mock_redis)
    event = entry(master, f"XU-{master_broker}-{child_broker}-{order_type}", order_type, trigger)
    await pipe.execute_replication(event=event, clients={str(child.id): client})
    await pipe.execute_replication(event=event, clients={str(child.id): client})
    assert client.place_order.await_count == 1
    assert len(await copies_of(child)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("master_broker,child_broker", CROSS_PAIRS)
async def test_a_child_without_a_session_gets_no_order(db_session, mock_redis, all_pairs_open_for_test, master_broker, child_broker):
    """No broker client is built for an account without a live session. The first gate that needs the client - the funds read -
    fails closed (FUNDS_FETCH_FAILED), so the Child is rejected before any send and its siblings are unaffected."""
    master, children = await build_strategy(db_session, master_broker, [child_broker], f"xs-{master_broker}-{child_broker}")
    child = children[0]
    res = await pipeline(mock_redis).execute_replication(
        event=entry(master, f"XS-{master_broker}-{child_broker}", "LIMIT", "0"), clients={})
    out = res["results"][str(child.id)]
    assert out["status"] == "REJECTED", out
    copies = await copies_of(child)
    assert len(copies) == 1 and copies[0].status == CopyTradeStatus.REJECTED, [(c.status, c.failure_reason) for c in copies]
    assert copies[0].failure_reason == "FUNDS_FETCH_FAILED", copies[0].failure_reason
