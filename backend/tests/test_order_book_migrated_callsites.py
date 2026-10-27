"""
Regression tests for the call sites migrated onto the explicit order-book read (brokers/order_book_read.py):

  * the Master-switch preflight (strategy_switch.run_preflight)           - read_order_book_rows, one raw call per account
  * crash recovery (ReconciliationEngine._submitted_verdict)               - read_order_book_rows
  * the stale-order sweeper's provider (EngineLoop._fetch_order_book_for_account) - read_order_book_single, one GET

What is pinned: the blocker CODES and MESSAGES are byte-identical to what the preflight always said (they are an API contract, and the
acknowledged-override path keys on the code); `UNVERIFIABLE` is still the only answer to an unreadable book; an unknown or failed read is
never read as "the order is absent" / "nothing is open"; an empty book that was PROVEN read still is. Offline only: broker doubles and a
scripted HTTP layer, no network, credentials or orders. Nothing here can send, modify or cancel an order.
"""
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.models.accounts import AccountType
from app.services import strategy_switch
from app.services.reliability.reconciliation import ReconciliationEngine
from app.services.runtime.engine_loop import EngineLoop
from strategy_helpers import ensure_account
from test_alice_order_book_parser import _client as alice_client
from test_alice_order_book_parser import row
from test_strategy_master_switching import _Broker, _codes, _preflight, _provider, trio  # noqa: F401

NO_SESSION = ("The engine has no active broker session for this account, so its orders and positions cannot be verified. "
              "Log the account in and try again.")
NOT_SUPPORTED = ("This account's broker does not support the order-book verification a Master switch needs, so its working "
                 "orders cannot be ruled out.")
NOT_FETCHED = "Alice Blue order book could not be fetched, so working orders cannot be ruled out."


class NoRawRead:
    """A broker whose client has no raw order-book read (what a Groww / Angel One client looks like to the preflight)."""
    get_positions_or_none = AsyncMock(return_value=[])


def _messages(pre, account_id, code="BROKER_VERIFICATION_UNAVAILABLE"):
    return [b.message for b in pre.blockers if b.code == code and b.account_id == str(account_id)]


# ── Master-switch preflight ─────────────────────────────────────────────────────────────────────────────────────

UNREADABLE_BOOKS = {
    "returns_none": lambda b: setattr(b, "get_order_book_raw", AsyncMock(return_value=None)),
    "raises": lambda b: setattr(b, "get_order_book_raw", AsyncMock(side_effect=RuntimeError("Bearer SECRET-TOKEN"))),
    "not_awaitable": lambda b: setattr(b, "get_order_book_raw", lambda: "not awaitable"),
    "returns_a_dict": lambda b: setattr(b, "get_order_book_raw", AsyncMock(return_value={"result": []})),
    "returns_a_string": lambda b: setattr(b, "get_order_book_raw", AsyncMock(return_value="oops")),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(UNREADABLE_BOOKS))
async def test_an_unreadable_book_blocks_with_the_unchanged_code_and_message(db_session, trio, kind):
    key = str(trio.vikash.id)
    UNREADABLE_BOOKS[kind](trio.brokers[key])
    pre = await _preflight(db_session, trio, trio.vikash)
    assert not pre.allowed
    assert _messages(pre, key) == [NOT_FETCHED]                              # exactly one blocker, exactly this text
    assert not [b for b in pre.blockers if b.account_id == key and b.code.startswith("PENDING_")]   # never "nothing open"
    assert pre.activations() == {}                                   # no snapshot is taken from a book that was not read


@pytest.mark.asyncio
async def test_no_client_and_no_raw_read_keep_their_own_messages(db_session, trio):
    trio.brokers.pop(str(trio.vikash.id))
    trio.brokers[str(trio.dhinesh.id)] = NoRawRead()
    pre = await _preflight(db_session, trio, trio.vikash)
    assert _messages(pre, trio.vikash.id) == [NO_SESSION]
    assert _messages(pre, trio.dhinesh.id) == [NOT_SUPPORTED]
    assert not pre.allowed


@pytest.mark.asyncio
async def test_the_failure_log_names_the_exception_type_and_never_the_broker_message(db_session, trio, caplog):
    UNREADABLE_BOOKS["raises"](trio.brokers[str(trio.vikash.id)])
    with caplog.at_level(logging.WARNING):
        await _preflight(db_session, trio, trio.vikash)
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "Order book fetch raised" in text and "RuntimeError" in text and "SECRET" not in text


@pytest.mark.asyncio
async def test_a_proven_empty_book_is_not_a_blocker_and_takes_an_empty_snapshot(db_session, trio):
    pre = await _preflight(db_session, trio, trio.vikash)                    # every double returns []
    assert pre.allowed and pre.blockers == []
    assert pre.activations()[str(trio.vikash.id)][0] == set()


@pytest.mark.asyncio
async def test_a_book_with_only_finished_orders_does_not_block_and_is_snapshotted(db_session, trio):
    trio.brokers[str(trio.vikash.id)]._book = [{"brokerOrderId": "D1", "orderStatus": "COMPLETE"},
                                               {"Nstordno": "D2", "Status": "CANCELED"}, {"orderId": "D3", "orderStatus": "REJECTED"}]
    pre = await _preflight(db_session, trio, trio.vikash)
    assert pre.allowed
    assert pre.activations()[str(trio.vikash.id)][0] == {"D1", "D2", "D3"}


@pytest.mark.asyncio
async def test_a_working_order_blocks_with_the_unchanged_message_and_items(db_session, trio):
    trio.brokers[str(trio.vikash.id)]._book = [{"brokerOrderId": "W1", "orderStatus": "OPEN", "tradingSymbol": "ITC"},
                                               {"brokerOrderId": "D1", "orderStatus": "COMPLETE"}]
    pre = await _preflight(db_session, trio, trio.vikash)
    (blocker,) = [b for b in pre.blockers if b.account_id == str(trio.vikash.id)]
    assert blocker.code == "PENDING_MASTER_ORDERS" and blocker.count == 1 and blocker.items == ["W1 ITC OPEN"]
    assert blocker.message == "1 working order(s) at Alice Blue on the Master account. Wait for them to fill or cancel them in Alice Blue."


@pytest.mark.asyncio
async def test_the_preflight_makes_exactly_one_raw_call_per_account_in_scope(db_session, trio):
    for broker in trio.brokers.values():
        broker.get_order_book_raw = AsyncMock(return_value=[])
    pre = await _preflight(db_session, trio, trio.vikash)
    checked = {a["account_id"] for a in pre.checked_accounts}
    assert checked and all(trio.brokers[k].get_order_book_raw.await_count == 1 for k in checked)


@pytest.mark.asyncio
async def test_an_unreadable_book_can_still_only_be_overridden_through_the_acknowledged_path(db_session, trio):
    key = str(trio.vikash.id)
    UNREADABLE_BOOKS["returns_none"](trio.brokers[key])
    pre = await _preflight(db_session, trio, trio.vikash)
    assert {b.code for b in pre.blockers} == {"BROKER_VERIFICATION_UNAVAILABLE"}      # only the unverifiable kind is overridable


# ── crash recovery ─────────────────────────────────────────────────────────────────────────────────────────────

def _trade(broker_order_id="B1", follower="acc-1"):
    return SimpleNamespace(broker_order_id=broker_order_id, follower_account_id=follower)


class _Provider:
    def __init__(self, client):
        self.client, self.calls = client, 0

    def __call__(self, key):
        self.calls += 1
        return self.client


def _raw_client(**kw):
    c = SimpleNamespace()
    if "returns" in kw:
        c.get_order_book_raw = AsyncMock(return_value=kw["returns"])
    elif "raises" in kw:
        c.get_order_book_raw = AsyncMock(side_effect=kw["raises"])
    elif kw.get("not_awaitable"):
        c.get_order_book_raw = lambda: "not awaitable"
    return c


async def _verdict(client, trade=None, cache=None, provider=None):
    provider = provider or _Provider(client)
    return await ReconciliationEngine._submitted_verdict(trade or _trade(), provider, {} if cache is None else cache)


UNVERIFIABLE_CLIENTS = {
    "no_client": None,
    "no_raw_read": SimpleNamespace(),
    "returns_none": _raw_client(returns=None),
    "raises": _raw_client(raises=RuntimeError("Bearer SECRET-TOKEN")),
    "not_awaitable": _raw_client(not_awaitable=True),
    "returns_a_dict": _raw_client(returns={"result": [{"brokerOrderId": "B1"}]}),
    "returns_a_string": _raw_client(returns="B1"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(UNVERIFIABLE_CLIENTS))
async def test_an_unreadable_book_is_unverifiable_never_not_in_book(name):
    assert await _verdict(UNVERIFIABLE_CLIENTS[name]) == "UNVERIFIABLE"


@pytest.mark.asyncio
async def test_known_not_in_book_and_no_broker_id_outcomes_are_unchanged():
    assert await _verdict(_raw_client(returns=[{"brokerOrderId": "B1"}, {"brokerOrderId": "B2"}])) == "KNOWN"
    assert await _verdict(_raw_client(returns=[{"Nstordno": "B1"}])) == "KNOWN"                    # the legacy id key
    assert await _verdict(_raw_client(returns=[{"brokerOrderId": "OTHER"}])) == "NOT_IN_BOOK"
    assert await _verdict(_raw_client(returns=[])) == "NOT_IN_BOOK"                               # a PROVEN empty book
    assert await _verdict(_raw_client(returns=[{"brokerOrderId": "B1"}, "stray", None])) == "KNOWN"   # non-dict rows are ignored
    assert await _verdict(_raw_client(returns=[{"brokerOrderId": "B1"}]), trade=_trade(broker_order_id=None)) == "NO_BROKER_ID"
    assert await _verdict(_raw_client(returns=[{"brokerOrderId": "B1"}]), trade=_trade(broker_order_id="")) == "NO_BROKER_ID"


@pytest.mark.asyncio
async def test_without_a_client_provider_the_verdict_is_unverifiable():
    assert await ReconciliationEngine._submitted_verdict(_trade(), None, {}) == "UNVERIFIABLE"


@pytest.mark.asyncio
async def test_the_book_is_read_once_per_account_however_many_trades_ask():
    client = _raw_client(returns=[{"brokerOrderId": "B1"}])
    provider, cache = _Provider(client), {}
    for boid, expected in (("B1", "KNOWN"), ("B2", "NOT_IN_BOOK"), ("B1", "KNOWN")):
        assert await _verdict(client, trade=_trade(broker_order_id=boid), cache=cache, provider=provider) == expected
    assert client.get_order_book_raw.await_count == 1 and provider.calls == 1
    assert await _verdict(client, trade=_trade(follower="acc-2"), cache=cache, provider=provider) == "KNOWN"
    assert client.get_order_book_raw.await_count == 2                                             # a second account is a second read


@pytest.mark.asyncio
async def test_an_unreadable_book_is_cached_as_unverifiable_and_not_retried_per_trade():
    client = _raw_client(returns=None)
    cache = {}
    for _ in range(3):
        assert await _verdict(client, cache=cache) == "UNVERIFIABLE"
    assert client.get_order_book_raw.await_count == 1


@pytest.mark.asyncio
async def test_the_recovery_failure_log_names_the_exception_type_only(caplog):
    with caplog.at_level(logging.WARNING):
        assert await _verdict(UNVERIFIABLE_CLIENTS["raises"]) == "UNVERIFIABLE"
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "could not read the order book" in text and "RuntimeError" in text and "SECRET" not in text


# ── the sweeper's provider on a REAL Alice Blue client over a scripted HTTP layer ─────────────────────────────────

@pytest.fixture
def real_alice(monkeypatch, mock_redis):
    holder = {}
    monkeypatch.setattr("app.services.runtime.engine_loop.account_manager.get_client", lambda acc_id: holder.get("client"))

    async def use(**kw):
        await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
        client, http = alice_client(mock_redis, **kw)
        holder["client"] = client
        return http
    return use


@pytest.mark.asyncio
async def test_the_sweeper_reads_a_real_alice_book_with_one_get_and_returns_parsed_rows(real_alice):
    http = await real_alice(payload={"result": [row(orderTag="JT_SW_1"), row(brokerOrderId="2", orderTag="JT_SW_2")]})
    rows = await EngineLoop._fetch_order_book_for_account("acc")
    assert [r.client_order_id for r in rows] == ["JT_SW_1", "JT_SW_2"] and http.request.await_count == 1


@pytest.mark.asyncio
async def test_the_sweeper_gets_a_proven_empty_book_as_a_list_and_everything_unproven_as_none(real_alice):
    for payload in (dict(payload={"result": []}), dict(payload={"status": "EC916", "message": "No orders found for this user.",
                                                                "result": None})):
        http = await real_alice(**payload)
        assert await EngineLoop._fetch_order_book_for_account("acc") == [] and http.request.await_count == 1
    for payload in (dict(payload={"status": "Ok"}), dict(status=500, payload={}), dict(request_raises=httpx.ConnectError("down")),
                    dict(json_raises=True), dict(payload={"result": [row(), row(brokerOrderId="2", quantity="abc")]})):
        http = await real_alice(**payload)
        assert await EngineLoop._fetch_order_book_for_account("acc") is None, payload
        assert http.request.await_count == 1                                  # never a second GET, unreadable or not
