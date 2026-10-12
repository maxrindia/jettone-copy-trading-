"""
Follower SELL position-safety gate (Option B) regression coverage.

Production incident this exists for (2026-09-22): a master BUY was
replicated to a follower whose child BUY never filled (stale LIMIT at
122.00 while the market ran to 123.24), leaving the follower flat. The
master then squared off with a SELL, Jettone replicated that SELL normally,
and it FILLED - putting the follower SHORT 1 CANBK it never owned, held for
~38 minutes until someone closed it manually. A second, identical case on
YESBANK the same hour was blocked only because Alice Blue happened to
reject it ("Intraday Direct Sell not allowed"), so the broker's own guard is
demonstrably not dependable.

The rule under test (Option B): an automatic follower SELL is permitted only
when the follower's broker-confirmed LONG position for that exact instrument
AND product fully covers the requested quantity. Otherwise the ENTIRE copy is
rejected - never a partial sell, never a short. Automatic new short entries
are not supported yet.

No live trading is enabled and no real broker calls are made anywhere in
this file - every broker client is an AsyncMock test double and Redis is the
in-memory mock_redis fixture.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from app.models.accounts import Account, AccountType
from app.models.audit import AuditLog, SystemEvent
from app.models.trades import CopyTrade, CopyTradeStatus
from app.services.alice_blue.types import (
    FundsData,
    OrderResult,
    OrderResultCategory,
    Position,
)
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from strategy_helpers import SAMPLE_MASTER_ACCOUNT_ID, add_child


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _sell_market_event(quantity=1, symbol="YESBANK", product_type="MIS",
                       master_order_id=None, price=Decimal("23.18")):
    return MasterTradeEvent(
        account_id=SAMPLE_MASTER_ACCOUNT_ID,
        master_order_id=master_order_id or f"ORD_SELL_{uuid.uuid4().hex[:10]}",
        symbol=symbol, exchange="NSE", side="SELL", quantity=quantity,
        price=price, product_type=product_type,
        timestamp=datetime.now(timezone.utc), fill_sequence=quantity,
        order_type="MARKET", event_type="MARKET_ACCEPTED", status="COMPLETE",
        total_quantity=quantity, filled_quantity=quantity, pending_quantity=0,
    )


def _sell_limit_event(quantity=1, symbol="YESBANK", product_type="MIS",
                      master_order_id=None, price=Decimal("23.18")):
    return MasterTradeEvent(
        account_id=SAMPLE_MASTER_ACCOUNT_ID,
        master_order_id=master_order_id or f"ORD_SELLLIM_{uuid.uuid4().hex[:10]}",
        symbol=symbol, exchange="NSE", side="SELL", quantity=quantity,
        price=price, limit_price=price, product_type=product_type,
        timestamp=datetime.now(timezone.utc), fill_sequence=1,
        order_type="LIMIT", event_type="LIMIT_CREATED", status="OPEN",
    )


def _buy_market_event(quantity=1, symbol="YESBANK", master_order_id=None):
    return MasterTradeEvent(
        account_id=SAMPLE_MASTER_ACCOUNT_ID,
        master_order_id=master_order_id or f"ORD_BUY_{uuid.uuid4().hex[:10]}",
        symbol=symbol, exchange="NSE", side="BUY", quantity=quantity,
        price=Decimal("23.18"), product_type="MIS",
        timestamp=datetime.now(timezone.utc), fill_sequence=quantity,
        order_type="MARKET", event_type="MARKET_ACCEPTED", status="COMPLETE",
        total_quantity=quantity, filled_quantity=quantity, pending_quantity=0,
    )


def _position(symbol="YESBANK-EQ", quantity=1, product_type="MIS", exchange="NSE"):
    return Position(
        symbol=symbol,
        quantity=quantity,
        side="BUY" if quantity > 0 else "SELL",
        average_price=Decimal("23.00"),
        account_id="acc",
        exchange=exchange,
        product_type=product_type,
    )


def _client(mock_redis, positions=None, positions_error=None):
    """AsyncMock broker client wired with a real in-memory redis (so the
    project's own resolve_instrument_info works) and a controllable
    position book."""
    client = AsyncMock()
    client.redis = mock_redis
    client.get_funds.return_value = FundsData(
        account_id="f", available_margin=Decimal("500000"), collateral=Decimal("0"),
        used_margin=Decimal("0"), updated_at=datetime.now(timezone.utc),
    )
    client.get_positions.return_value = []
    # get_available_long() reads get_positions_or_none() (the fail-safe
    # variant that returns None on fetch failure instead of []); mirror the
    # same configuration onto it so these tests exercise the real call path.
    client.get_positions_or_none.return_value = []
    if positions_error is not None:
        client.get_positions.side_effect = positions_error
        client.get_positions_or_none.side_effect = positions_error
    elif positions is not None:
        client.get_positions.return_value = positions
        client.get_positions_or_none.return_value = positions
    client.place_order.return_value = OrderResult(
        category=OrderResultCategory.SUCCESS, client_order_id=None,
        broker_order_id="BRK_SELL_OK", message="ok", raw_response={"stat": "Ok"},
        timestamp=datetime.now(timezone.utc),
    )
    return client


async def _audit_reasons(db_session):
    rows = (await db_session.execute(select(AuditLog))).scalars().all()
    return [r.reason_code for r in rows]


async def _system_event_types(db_session):
    rows = (await db_session.execute(select(SystemEvent))).scalars().all()
    return [r.event_type for r in rows]


# ---------------------------------------------------------------------------
# 1-5. The Option B quantity rule
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sell_allowed_when_long_exactly_covers(db_session, mock_redis, sample_accounts, sample_strategy):
    """Follower +1, master SELL 1 -> ALLOW."""
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[_position(quantity=1)])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_sell_market_event(quantity=1), clients={str(follower.id): client}
    )

    assert res["results"][str(follower.id)]["status"] == OrderResultCategory.SUCCESS.value
    client.place_order.assert_awaited_once()
    assert client.place_order.call_args.args[0].quantity == 1


@pytest.mark.asyncio
async def test_sell_rejected_when_follower_is_flat(db_session, mock_redis, sample_accounts, sample_strategy):
    """Follower 0, master SELL 1 -> REJECT, no broker order. THE incident."""
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_sell_market_event(quantity=1), clients={str(follower.id): client}
    )

    assert res["results"][str(follower.id)] == {
        "status": "REJECTED", "reasons": ["INSUFFICIENT_LONG_POSITION"]
    }
    client.place_order.assert_not_called()

    assert "INSUFFICIENT_LONG_POSITION" in await _audit_reasons(db_session)
    assert "INSUFFICIENT_LONG_POSITION" in await _system_event_types(db_session)
    trades = (await db_session.execute(select(CopyTrade))).scalars().all()
    assert len(trades) == 1
    assert trades[0].status == CopyTradeStatus.RECONCILIATION_REQUIRED


@pytest.mark.asyncio
async def test_partial_long_rejects_entire_copy_and_never_sells_the_partial(db_session, mock_redis, sample_accounts, sample_strategy):
    """Follower +6, master SELL 10 -> REJECT ENTIRE. Explicitly NOT SELL 6.
    This is the whole point of Option B over partial capping."""
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[_position(quantity=6)])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_sell_market_event(quantity=10), clients={str(follower.id): client}
    )

    assert res["results"][str(follower.id)]["reasons"] == ["INSUFFICIENT_LONG_POSITION"]
    client.place_order.assert_not_called()  # not SELL 10, and NOT SELL 6 either


@pytest.mark.asyncio
async def test_sell_rejected_when_follower_already_short(db_session, mock_redis, sample_accounts, sample_strategy):
    """Follower -2, master SELL 1 -> REJECT. Shorts must never deepen."""
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[_position(quantity=-2)])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_sell_market_event(quantity=1), clients={str(follower.id): client}
    )

    assert res["results"][str(follower.id)]["reasons"] == ["INSUFFICIENT_LONG_POSITION"]
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_sell_allowed_when_long_exceeds_requested(db_session, mock_redis, sample_accounts, sample_strategy):
    """Follower +10, master SELL 6 -> ALLOW 6 (partial close is fine)."""
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[_position(quantity=10)])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_sell_market_event(quantity=6), clients={str(follower.id): client}
    )

    assert res["results"][str(follower.id)]["status"] == OrderResultCategory.SUCCESS.value
    assert client.place_order.call_args.args[0].quantity == 6


# ---------------------------------------------------------------------------
# 6-7. Symbol normalization via the project's own resolver
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_event_symbol_matches_eq_suffixed_position_yesbank(db_session, mock_redis, sample_accounts, sample_strategy):
    """Event says YESBANK (order-book spelling), position book says
    YESBANK-EQ. resolve_instrument_info maps both to token 11915, so the
    +2 long must be seen. A naive symbol==symbol comparison would read 0
    and wrongly block a legitimate close."""
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[_position(symbol="YESBANK-EQ", quantity=2)])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_sell_market_event(quantity=2, symbol="YESBANK"),
        clients={str(follower.id): client},
    )

    assert res["results"][str(follower.id)]["status"] == OrderResultCategory.SUCCESS.value
    assert client.place_order.call_args.args[0].quantity == 2


@pytest.mark.asyncio
async def test_event_symbol_matches_eq_suffixed_position_canbk_via_cache(db_session, mock_redis, sample_accounts, sample_strategy):
    """CANBK is not in the static table, so it resolves through the Redis
    contract-master cache exactly as it does in production after the
    startup sync. CANBK event vs CANBK-EQ position must still match."""
    await mock_redis.set("alice:instrument:NSE:CANBK", "10794:CANBK-EQ")
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[_position(symbol="CANBK-EQ", quantity=1)])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_sell_market_event(quantity=1, symbol="CANBK"),
        clients={str(follower.id): client},
    )

    assert res["results"][str(follower.id)]["status"] == OrderResultCategory.SUCCESS.value


# ---------------------------------------------------------------------------
# 8-9. Product separation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cnc_long_does_not_cover_a_mis_sell(db_session, mock_redis, sample_accounts, sample_strategy):
    """A CNC (delivery) holding must never license an MIS (intraday) SELL."""
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[_position(quantity=5, product_type="CNC")])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_sell_market_event(quantity=1, product_type="MIS"),
        clients={str(follower.id): client},
    )

    assert res["results"][str(follower.id)]["reasons"] == ["INSUFFICIENT_LONG_POSITION"]
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_nrml_long_does_not_cover_a_mis_sell(db_session, mock_redis, sample_accounts, sample_strategy):
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[_position(quantity=5, product_type="NRML")])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_sell_market_event(quantity=1, product_type="MIS"),
        clients={str(follower.id): client},
    )

    assert res["results"][str(follower.id)]["reasons"] == ["INSUFFICIENT_LONG_POSITION"]
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_intraday_alias_is_recognized_as_mis(db_session, mock_redis, sample_accounts, sample_strategy):
    """Alice Blue reports the SAME position as product 'MIS' on the position
    book but 'INTRADAY' on the order book. Both spellings must resolve to
    one bucket or a legitimate close gets blocked."""
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[_position(quantity=1, product_type="INTRADAY")])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_sell_market_event(quantity=1, product_type="MIS"),
        clients={str(follower.id): client},
    )

    assert res["results"][str(follower.id)]["status"] == OrderResultCategory.SUCCESS.value


# ---------------------------------------------------------------------------
# 10-12. Fail-closed conditions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_product_string_fails_closed(db_session, mock_redis, sample_accounts, sample_strategy):
    """An unrecognized product on a row for THIS instrument must reject, not
    be silently treated as MIS."""
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[_position(quantity=5, product_type="WEIRD_BO")])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_sell_market_event(quantity=1), clients={str(follower.id): client}
    )

    assert res["results"][str(follower.id)]["reasons"] == ["UNKNOWN_PRODUCT_TYPE"]
    client.place_order.assert_not_called()
    assert "UNKNOWN_PRODUCT_TYPE" in await _system_event_types(db_session)


@pytest.mark.asyncio
async def test_unknown_product_on_an_unrelated_symbol_is_ignored(db_session, mock_redis, sample_accounts, sample_strategy):
    """One exotic unrelated holding must not block every SELL - only an
    unrecognized product on the instrument being sold is fatal."""
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[
        _position(symbol="INFY-EQ", quantity=5, product_type="WEIRD_BO"),
        _position(symbol="YESBANK-EQ", quantity=1, product_type="MIS"),
    ])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_sell_market_event(quantity=1), clients={str(follower.id): client}
    )

    assert res["results"][str(follower.id)]["status"] == OrderResultCategory.SUCCESS.value


@pytest.mark.asyncio
async def test_instrument_resolution_failure_fails_closed(db_session, mock_redis, sample_accounts, sample_strategy):
    """An unresolvable instrument means we cannot know the position at all."""
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[_position(quantity=10)])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_sell_market_event(quantity=1, symbol="NOT_A_REAL_SYMBOL_XYZ"),
        clients={str(follower.id): client},
    )

    assert res["results"][str(follower.id)]["reasons"] == ["INSTRUMENT_RESOLUTION_FAILED"]
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_position_fetch_failure_fails_closed_and_is_not_read_as_zero(db_session, mock_redis, sample_accounts, sample_strategy):
    """'Position unavailable' must reject as a SAFETY failure with its own
    reason code - never be silently collapsed into 'position is 0'."""
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions_error=RuntimeError("broker 503"))

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_sell_market_event(quantity=1), clients={str(follower.id): client}
    )

    assert res["results"][str(follower.id)]["reasons"] == ["POSITION_FETCH_FAILED"]
    client.place_order.assert_not_called()
    assert "POSITION_FETCH_FAILED" in await _system_event_types(db_session)


# ---------------------------------------------------------------------------
# 13-14. Unconditional execution + multi-follower independence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gate_runs_for_follower_without_copysetting(db_session, mock_redis, sample_accounts, sample_strategy):
    """sample_accounts' follower has NO CopySetting, so the RiskEngine block
    (guarded by `if settings_obj:`) never runs for it. The safety gate must
    still fire - that hole is exactly why this check is not inside
    RiskEngine."""
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_sell_market_event(quantity=1), clients={str(follower.id): client}
    )

    assert res["results"][str(follower.id)]["reasons"] == ["INSUFFICIENT_LONG_POSITION"]
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_gate_runs_for_follower_with_copysetting(db_session, mock_redis, sample_accounts, sample_strategy, sample_copy_settings):
    """And it must also fire when a CopySetting DOES exist (RiskEngine path
    active), so neither configuration is a gap. Uses the one symbol this
    fixture's allowed_symbols permits, cached so it resolves, so the event
    reaches the safety gate instead of being stopped earlier by RiskEngine."""
    await mock_redis.set("alice:instrument:NSE:NIFTY24AUG23500CE", "45100:NIFTY24AUG23500CE")
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_sell_market_event(quantity=1, symbol="NIFTY24AUG23500CE"),
        clients={str(follower.id): client},
    )

    assert res["results"][str(follower.id)]["reasons"] == ["INSUFFICIENT_LONG_POSITION"]
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_one_follower_rejection_does_not_block_a_valid_follower(db_session, mock_redis, sample_accounts, sample_strategy):
    """Follower A holds +1 and must still get its order even though
    follower B is flat and is rejected."""
    follower_a = sample_accounts["follower1"]
    follower_b = Account(
        id=uuid.uuid4(), account_type=AccountType.FOLLOWER, display_name="Follower B",
        alice_user_id="AB00202", is_active=True,
    )
    db_session.add(follower_b)
    await db_session.commit()
    await add_child(db_session, sample_strategy, follower_b)

    client_a = _client(mock_redis, positions=[_position(quantity=1)])
    client_b = _client(mock_redis, positions=[])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_sell_market_event(quantity=1),
        clients={str(follower_a.id): client_a, str(follower_b.id): client_b},
    )

    assert res["results"][str(follower_a.id)]["status"] == OrderResultCategory.SUCCESS.value
    client_a.place_order.assert_awaited_once()

    assert res["results"][str(follower_b.id)]["reasons"] == ["INSUFFICIENT_LONG_POSITION"]
    client_b.place_order.assert_not_called()


# ---------------------------------------------------------------------------
# 15-17. Both SELL paths gated; BUY untouched
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_master_sell_limit_path_is_also_gated(db_session, mock_redis, sample_accounts, sample_strategy):
    """A master SELL LIMIT reaches followers through replicate_limit_order,
    NOT _execute_market_replication. Gating only the market path would leave
    this route open - a real master SELL LIMIT 12 was replicated this way in
    production."""
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_sell_limit_event(quantity=1), clients={str(follower.id): client}
    )

    assert res["results"][str(follower.id)]["reasons"] == ["INSUFFICIENT_LONG_POSITION"]
    client.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_master_sell_limit_allowed_when_covered(db_session, mock_redis, sample_accounts, sample_strategy):
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[_position(quantity=5)])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_sell_limit_event(quantity=5), clients={str(follower.id): client}
    )

    client.place_order.assert_awaited_once()
    assert client.place_order.call_args.args[0].quantity == 5


@pytest.mark.asyncio
async def test_buy_replication_is_completely_unaffected(db_session, mock_redis, sample_accounts, sample_strategy):
    """A BUY must place normally with a flat follower and must never even
    consult the position book."""
    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    res = await pipeline.execute_replication(
        event=_buy_market_event(quantity=3), clients={str(follower.id): client}
    )

    assert res["results"][str(follower.id)]["status"] == OrderResultCategory.SUCCESS.value
    assert client.place_order.call_args.args[0].quantity == 3
    client.get_positions.assert_not_called()


@pytest.mark.asyncio
async def test_allowed_sell_keeps_existing_marketable_limit_pricing(db_session, mock_redis, sample_accounts, sample_strategy):
    """An allowed SELL must still price exactly as before the gate existed
    (marketable LIMIT at the master fill price with the default 0% buffer)."""
    from app.services.alice_blue.types import OrderType

    follower = sample_accounts["follower1"]
    client = _client(mock_redis, positions=[_position(quantity=1)])

    pipeline = CopyEnginePipeline(validator=EventValidator(redis=mock_redis))
    await pipeline.execute_replication(
        event=_sell_market_event(quantity=1, price=Decimal("125.89")),
        clients={str(follower.id): client},
    )

    sent = client.place_order.call_args.args[0]
    assert sent.order_type == OrderType.LIMIT
    assert sent.price == Decimal("125.89")
