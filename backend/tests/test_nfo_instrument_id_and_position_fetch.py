"""
Phase 1 of the 2026-09-23 root-cause investigation (three production
incidents: intraday SELL, positional BUY->SELL, NIFTY options).

Covers two independent, minimal fixes:

1. NFO/options instrument-id propagation. Confirmed root cause: Alice
   Blue's order-book response for master order 26092300036420 carried the
   real numeric instrumentId ("74056") directly, alongside a bare,
   ambiguous tradingSymbol ("NIFTY" - the underlying name, shared by every
   strike/expiry/CE-PE contract). Jettone discarded the numeric id and
   re-resolved "NIFTY" from a Redis cache that - independently, via
   load_and_cache_contract_master's known per-symbol-collision issue
   (NOT touched in this phase) - held a completely unrelated contract
   ("89649:NIFTY26JUN29P33000"). The follower's order was placed against
   that wrong contract and broker-rejected. instrument_id now flows
   OrderRecord -> OrderUpdateEvent -> MasterTradeEvent -> the follower's
   OrderRequest.instrument_token, which resolve_instrument_info already
   treats as authoritative (bypasses symbol/cache lookup entirely).

2. get_positions() fail-open ambiguity. get_positions() returns [] for
   BOTH a genuine empty position book AND a network/HTTP failure -
   get_available_long() could not tell "follower holds 0" apart from
   "couldn't check". get_positions_or_none() shares the identical fetch/
   parse path but returns None specifically on fetch failure, and
   get_available_long() now uses it, raising PositionFetchError
   (-> POSITION_FETCH_FAILED) instead of silently treating a fetch failure
   as a flat position.

No live trading is enabled and no real broker calls are made anywhere in
this file - every HTTP client is a mocked httpx.AsyncClient / AsyncMock,
and Redis is the in-memory mock_redis fixture.
"""
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.types import (
    FundsData,
    OrderRecord,
    OrderResult,
    OrderResultCategory,
    OrderSide,
    Position,
)
from app.services.copy_engine.detector import MasterTradeDetector
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.risk_inputs import PositionFetchError, get_available_long
from app.services.copy_engine.validator import EventValidator
from app.services.reliability.master_order_poller import MasterOrderPoller
from strategy_helpers import SAMPLE_MASTER_ACCOUNT_ID


def _client(mock_redis, mock_http):
    return AliceBlueClient(
        account_id="acc-test", user_id="AB00101", api_secret="secret",
        redis=mock_redis, http_client=mock_http,
    )


def _order_book_response(rows):
    resp = AsyncMock()
    resp.status_code = 200
    resp.json = lambda: {"result": rows}
    return resp


# ---------------------------------------------------------------------------
# A. OrderRecord.instrument_id parsing (get_order_book)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_order_book_parses_instrument_id_from_broker_payload(mock_redis):
    """The exact production payload for master order 26092300036420: Alice
    Blue reports tradingSymbol as the bare underlying "NIFTY" but ALSO
    carries the real numeric instrumentId directly (verified live,
    2026-09-23) - this must now be captured, not discarded."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
    client = _client(mock_redis, mock_http)

    mock_http.request = AsyncMock(return_value=_order_book_response([{
        "brokerOrderId": "26092300036420",
        "tradingSymbol": "NIFTY",
        "formattedInstrumentName": "NIFTY 29th SEP 23900 CE",
        "instrumentId": "74056",
        "exchange": "NFO",
        "transactionType": "BUY",
        "quantity": 65,
        "filledQuantity": 65,
        "averageTradedPrice": 6.7,
        "orderStatus": "COMPLETE",
        "product": "LONGTERM",
        "orderType": "MARKET",
        "price": 0.0,
        "orderTime": "2026-09-23 10:04:27",
    }]))

    orders = await client.get_order_book()
    assert len(orders) == 1
    assert orders[0].instrument_id == "74056"
    assert orders[0].symbol == "NIFTY"  # unchanged - still ambiguous on its own


@pytest.mark.asyncio
async def test_get_order_book_leaves_instrument_id_none_when_absent(mock_redis):
    """Existing equity-style payloads never carried this field - it must
    stay None, never fabricated."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
    client = _client(mock_redis, mock_http)

    mock_http.request = AsyncMock(return_value=_order_book_response([{
        "brokerOrderId": "1", "tradingSymbol": "CANBK", "exchange": "NSE",
        "transactionType": "BUY", "quantity": 1, "filledQuantity": 1,
        "averageTradedPrice": 100.0, "orderStatus": "COMPLETE",
        "product": "INTRADAY", "orderType": "LIMIT", "price": 100.0,
        "orderTime": "2026-09-23 10:00:00",
    }]))

    orders = await client.get_order_book()
    assert orders[0].instrument_id is None


# ---------------------------------------------------------------------------
# B. Propagation: OrderRecord -> OrderUpdateEvent -> MasterTradeEvent
# ---------------------------------------------------------------------------

def test_poller_to_event_threads_instrument_id():
    """MasterOrderPoller._to_event() must carry instrument_id through into
    the OrderUpdateEvent it hands to the detector."""
    poller = MasterOrderPoller(
        master_account_id="M1", client_provider=lambda _acc: None,
        on_order_update=lambda _event: None, pipeline=None,
    )
    record = OrderRecord(
        broker_order_id="26092300036420", client_order_id=None, symbol="NIFTY",
        exchange="NFO", side=OrderSide.BUY, quantity=65, filled_quantity=65,
        average_price=Decimal("6.70"), status="COMPLETE",
        order_timestamp=datetime.now(timezone.utc), order_type="MARKET",
        product_type="LONGTERM", instrument_id="74056",
    )

    event = poller._to_event(record)
    assert event.instrument_id == "74056"


def test_detector_propagates_instrument_id_into_master_trade_event():
    """Every MasterTradeEvent the detector produces must carry the incoming
    OrderUpdateEvent's instrument_id straight through - checked here via the
    fill-classification branch, the one that fires for a NIFTY MARKET
    BUY/SELL (the exact path in the production incident)."""
    from app.services.alice_blue.types import OrderUpdateEvent

    detector = MasterTradeDetector(master_account_id="M1")
    event = OrderUpdateEvent(
        broker_order_id="26092300036420", client_order_id=None, symbol="NIFTY",
        side="BUY", quantity=65, filled_quantity=65, fill_price=Decimal("6.70"),
        status="COMPLETE", timestamp=datetime.now(timezone.utc), account_id="M1",
        product_type="LONGTERM", order_type="MARKET", exchange="NFO",
        instrument_id="74056",
    )

    trade_event = detector.process_order_update(event)
    assert trade_event is not None
    assert trade_event.event_type == "MARKET_ACCEPTED"
    assert trade_event.instrument_id == "74056"


# ---------------------------------------------------------------------------
# C/D. provided_token used at follower placement - the ambiguous "NIFTY"
# cache must NOT be consulted when instrument_id is present. This is the
# end-to-end regression for the exact production incident.
# ---------------------------------------------------------------------------

def _nifty_event(instrument_id, side="BUY", event_type="MARKET_ACCEPTED"):
    return MasterTradeEvent(
        account_id=SAMPLE_MASTER_ACCOUNT_ID,
        master_order_id="26092300036420", symbol="NIFTY", exchange="NFO",
        side=side, quantity=65, price=Decimal("6.70"), product_type="LONGTERM",
        timestamp=datetime.now(timezone.utc), fill_sequence=65,
        order_type="MARKET", event_type=event_type,
        total_quantity=65, filled_quantity=65, pending_quantity=0,
        status="COMPLETE", instrument_id=instrument_id,
    )


@pytest.mark.asyncio
async def test_follower_buy_uses_provided_instrument_id_not_poisoned_cache(db_session, mock_redis, sample_accounts, sample_strategy):
    """Reproduces the exact incident: the Redis cache for NFO:NIFTY is
    poisoned with an unrelated contract (mirrors the real corrupted
    production value), but the master event carries the CORRECT
    instrument_id. The follower's order must use 74056, never 89649."""
    await mock_redis.set("alice:instrument:NFO:NIFTY", "89649:NIFTY26JUN29P33000")

    follower = sample_accounts["follower1"]
    client = AsyncMock()
    client.redis = mock_redis
    client.get_funds.return_value = FundsData(
        account_id="f", available_margin=Decimal("500000"), collateral=Decimal("0"),
        used_margin=Decimal("0"), updated_at=datetime.now(timezone.utc),
    )
    client.place_order.return_value = OrderResult(
        category=OrderResultCategory.SUCCESS, client_order_id=None,
        broker_order_id="BRK_NIFTY_OK", message="ok", raw_response={"stat": "Ok"},
        timestamp=datetime.now(timezone.utc),
    )

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    event = _nifty_event(instrument_id="74056")
    await pipeline.execute_replication(event=event, clients={str(follower.id): client})

    client.place_order.assert_awaited_once()
    sent_order = client.place_order.call_args.args[0]
    assert sent_order.instrument_token == "74056"


@pytest.mark.asyncio
async def test_place_order_payload_carries_the_provided_instrument_id_not_the_cache(mock_redis):
    """One level deeper than the pipeline test above: confirms
    AliceBlueClient.place_order actually sends instrumentId=74056 in the
    real outgoing broker payload, with the same poisoned cache present."""
    await mock_redis.set("alice:instrument:NFO:NIFTY", "89649:NIFTY26JUN29P33000")
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")

    resp = AsyncMock()
    resp.status_code = 200
    resp.json = lambda: [
        {"status": "Ok", "message": "Success", "result": [{"status": "OK", "brokerOrderId": "NFO1", "orderNo": "NFO1"}]}
    ]
    called_payload = []

    async def fake_request(method, url, **kwargs):
        called_payload.append(kwargs.get("json", []))
        return resp

    mock_http.request = AsyncMock(side_effect=fake_request)
    client = _client(mock_redis, mock_http)

    from app.services.alice_blue.types import OrderRequest, OrderType, ProductType

    req = OrderRequest(
        symbol="NIFTY", exchange="NFO", side=OrderSide.BUY, quantity=65,
        product_type=ProductType.NRML, order_type=OrderType.MARKET,
        client_order_id="CLIENT_NIFTY_1", instrument_token="74056",
    )
    result = await client.place_order(req)

    assert result.category == OrderResultCategory.SUCCESS
    assert called_payload[0][0]["instrumentId"] == "74056"  # never "89649"


@pytest.mark.asyncio
async def test_follower_without_instrument_id_still_falls_back_to_symbol_resolution(db_session, mock_redis, sample_accounts, sample_strategy):
    """Preserve existing behavior when instrument_id is genuinely
    unavailable (e.g. an older/legacy event, or a symbol that never had
    one): equities must keep resolving exactly as before - via the
    existing static-table/cache path, no instrument_id required."""
    follower = sample_accounts["follower1"]
    client = AsyncMock()
    client.redis = mock_redis
    client.get_funds.return_value = FundsData(
        account_id="f", available_margin=Decimal("500000"), collateral=Decimal("0"),
        used_margin=Decimal("0"), updated_at=datetime.now(timezone.utc),
    )
    client.place_order.return_value = OrderResult(
        category=OrderResultCategory.SUCCESS, client_order_id=None,
        broker_order_id="BRK_EQ_OK", message="ok", raw_response={"stat": "Ok"},
        timestamp=datetime.now(timezone.utc),
    )

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    event = MasterTradeEvent(
        account_id=SAMPLE_MASTER_ACCOUNT_ID,
        master_order_id="ORD_EQ_1", symbol="RELIANCE", exchange="NSE",
        side="BUY", quantity=1, price=Decimal("2900.00"), product_type="MIS",
        timestamp=datetime.now(timezone.utc), fill_sequence=1,
        order_type="MARKET", event_type="MARKET_ACCEPTED",
        total_quantity=1, filled_quantity=1, pending_quantity=0,
        status="COMPLETE", instrument_id=None,
    )
    await pipeline.execute_replication(event=event, clients={str(follower.id): client})

    client.place_order.assert_awaited_once()
    assert client.place_order.call_args.args[0].instrument_token is None


# ---------------------------------------------------------------------------
# E/F. get_positions() vs get_positions_or_none() - the fail-open fix
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_positions_returns_empty_list_on_genuine_empty_book(mock_redis):
    """A real, successful, empty position book must still mean []."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
    client = _client(mock_redis, mock_http)

    resp = AsyncMock()
    resp.status_code = 200
    resp.json = lambda: {"result": []}
    mock_http.request = AsyncMock(return_value=resp)

    assert await client.get_positions() == []
    assert await client.get_positions_or_none() == []


@pytest.mark.asyncio
async def test_get_positions_or_none_returns_none_on_http_failure(mock_redis):
    """An HTTP/API failure must be distinguishable from a genuine empty
    book. get_positions() keeps its existing [] contract for its other
    callers; get_positions_or_none() must surface None instead."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
    client = _client(mock_redis, mock_http)

    resp = AsyncMock()
    resp.status_code = 500
    resp.text = "Internal Server Error"
    mock_http.request = AsyncMock(return_value=resp)

    assert await client.get_positions() == []          # unchanged existing contract
    assert await client.get_positions_or_none() is None  # NEW: explicit failure signal


@pytest.mark.asyncio
async def test_get_positions_or_none_returns_none_on_network_error(mock_redis):
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
    client = _client(mock_redis, mock_http)
    mock_http.request = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

    assert await client.get_positions() == []
    assert await client.get_positions_or_none() is None


@pytest.mark.asyncio
async def test_get_positions_and_get_positions_or_none_agree_on_success(mock_redis):
    """Both methods must parse identically on the success path - only their
    failure behavior differs."""
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
    client = _client(mock_redis, mock_http)

    resp = AsyncMock()
    resp.status_code = 200
    resp.json = lambda: {"result": [{
        "tradingSymbol": "CANBK-EQ", "exchange": "NSE", "product": "MIS",
        "netQuantity": 3, "netAveragePrice": 124.50,
    }]}
    mock_http.request = AsyncMock(return_value=resp)

    via_old = await client.get_positions()
    via_new = await client.get_positions_or_none()
    assert len(via_old) == len(via_new) == 1
    assert via_old[0].symbol == via_new[0].symbol == "CANBK-EQ"
    assert via_old[0].quantity == via_new[0].quantity == 3


# ---------------------------------------------------------------------------
# G. get_available_long() converts fetch failure into POSITION_FETCH_FAILED
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_available_long_raises_position_fetch_failed_when_fetch_fails(mock_redis):
    """The direct unit-level proof: when the broker call itself fails
    (get_positions_or_none() -> None), get_available_long() must raise
    PositionFetchError, never silently return 0."""
    client = AsyncMock()
    client.redis = mock_redis
    client.get_positions_or_none.return_value = None

    with pytest.raises(PositionFetchError):
        await get_available_long(client=client, exchange="NSE", symbol="RELIANCE", product_type="MIS")


@pytest.mark.asyncio
async def test_get_available_long_returns_zero_for_genuine_empty_book(mock_redis):
    """The companion case: a genuinely empty (not failed) position book
    must still resolve to 0, not an error - this is Option B's normal
    "follower is flat" path, unaffected by this fix."""
    client = AsyncMock()
    client.redis = mock_redis
    client.get_positions_or_none.return_value = []

    available = await get_available_long(client=client, exchange="NSE", symbol="RELIANCE", product_type="MIS")
    assert available == 0


@pytest.mark.asyncio
async def test_get_available_long_still_matches_exact_product_on_success(mock_redis):
    """Unchanged behavior check: exact-match logic (exchange, canonical
    symbol, normalized product) is untouched by this fix - only the
    SOURCE of `positions` changed, not the matching loop."""
    client = AsyncMock()
    client.redis = mock_redis
    client.get_positions_or_none.return_value = [
        Position(symbol="RELIANCE-EQ", quantity=10, side="BUY",
                 average_price=Decimal("2900"), account_id="a",
                 exchange="NSE", product_type="MIS"),
        Position(symbol="RELIANCE-EQ", quantity=5, side="BUY",
                 average_price=Decimal("2900"), account_id="a",
                 exchange="NSE", product_type="CNC"),
    ]

    mis_available = await get_available_long(client=client, exchange="NSE", symbol="RELIANCE", product_type="MIS")
    cnc_available = await get_available_long(client=client, exchange="NSE", symbol="RELIANCE", product_type="CNC")
    assert mis_available == 10  # never combined with the CNC row
    assert cnc_available == 5
