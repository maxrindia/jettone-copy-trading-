"""
R4e: the Master order's instrument token is persisted together with its exchange.

The identity of an instrument is (exchange, token): the same token value exists on more than one exchange, so a token is
stored only when the broker's own order-book row REPORTED the exchange (not when the parser fell back to "NSE") and
that exchange equals the row's. A stored token is never overwritten or erased, historical rows stay NULL, WebSocket
events (which carry no token) change nothing, and Child-order paths never pass one. No broker, Redis or network call is
made; no ISIN or mapping is involved.
"""
import ast
import asyncio
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import select

from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.types import OrderRecord, OrderSide as BrokerOrderSide, OrderUpdateEvent
from app.services.copy_engine.events import MasterTradeEvent
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator
from app.services.reliability.master_order_poller import MasterOrderPoller
from app.services.runtime.engine_loop import EngineLoop
from test_engine_loop import _FakeMasterClient, _FakeWebSocketClient

MASTER = str(uuid.uuid4())


def pipeline_for(mock_redis):
    return CopyEnginePipeline(validator=EventValidator(redis=mock_redis))


async def save(pipeline, *, broker_order_id="B1", internal=None, exchange="NSE", token=None, token_exchange=None,
               source=OrderSource.BROKER_EXTERNAL, account=None, price=Decimal("100"), status=OrderStatus.OPEN, **extra):
    kwargs = dict(
        internal_order_id=internal or f"EXT_{broker_order_id}", broker_order_id=broker_order_id,
        account_id=account or uuid.UUID(MASTER), source=source, exchange=exchange, symbol="CANBK", side=OrderSide.BUY,
        order_type="LIMIT", product_type="INTRADAY", quantity=1, price=price, status=status,
    )
    if token is not None or token_exchange is not None:
        kwargs.update(instrument_token=token, instrument_exchange=token_exchange)
    kwargs.update(extra)
    return await pipeline.save_or_update_order(**kwargs)


async def stored(db, broker_order_id="B1"):
    db.expire_all()
    return (await db.execute(select(Order).where(Order.broker_order_id == broker_order_id))).scalar_one()


# ── the order-book parser ────────────────────────────────────────────────────────────────

def _client(mock_redis, http):
    return AliceBlueClient(account_id="acc-test", user_id="AB00101", api_secret="secret", redis=mock_redis, http_client=http)


def _book(rows):
    resp = AsyncMock()
    resp.status_code = 200
    resp.json = lambda: {"result": rows}
    return resp


def _row(**over):
    row = {"brokerOrderId": "1", "tradingSymbol": "CANBK", "formattedInstrumentName": "CANBK-EQ", "instrumentId": "10794",
           "exchange": "NSE", "transactionType": "BUY", "quantity": 1, "filledQuantity": 1, "averageTradedPrice": 100.0,
           "orderStatus": "COMPLETE", "product": "INTRADAY", "orderType": "LIMIT", "price": 100.0, "lotsize": 1,
           "orderTime": "2026-09-23 10:00:00"}
    row.update(over)
    return {k: v for k, v in row.items() if v is not ...}


async def parse(mock_redis, **over):
    http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
    http.request = AsyncMock(return_value=_book([_row(**over)]))
    return (await _client(mock_redis, http).get_order_book())[0]


@pytest.mark.asyncio
async def test_a_reported_exchange_and_token_are_both_captured(mock_redis):
    rec = await parse(mock_redis)
    assert (rec.instrument_id, rec.instrument_exchange, rec.exchange) == ("10794", "NSE", "NSE")
    nfo = await parse(mock_redis, tradingSymbol="NIFTY", instrumentId="74056", exchange="NFO")
    assert (nfo.instrument_id, nfo.instrument_exchange, nfo.symbol) == ("74056", "NFO", "NIFTY")


@pytest.mark.asyncio
@pytest.mark.parametrize("exchange,old_value", [(..., "NSE"), ("", ""), (None, "None")])
async def test_a_row_that_reports_no_exchange_has_no_instrument_exchange_but_keeps_its_old_defaults(mock_redis, exchange, old_value):
    rec = await parse(mock_redis, exchange=exchange)
    assert rec.instrument_exchange is None
    assert rec.exchange == old_value                      # the existing parsing of `exchange` is untouched (absent -> "NSE")
    assert rec.instrument_id == "10794"                   # the token is still parsed as before


@pytest.mark.asyncio
async def test_a_row_without_a_token_still_has_no_token(mock_redis):
    rec = await parse(mock_redis, instrumentId=...)
    assert rec.instrument_id is None and rec.instrument_exchange == "NSE"


# ── the shared upsert ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_new_order_stores_the_token_when_the_reported_exchange_matches(db_session, mock_redis):
    await save(pipeline_for(mock_redis), token="10794", token_exchange="NSE")
    row = await stored(db_session)
    assert (row.exchange, row.instrument_token) == ("NSE", "10794")


@pytest.mark.asyncio
async def test_the_reported_exchange_is_compared_case_insensitively_and_the_token_trimmed(db_session, mock_redis):
    await save(pipeline_for(mock_redis), token="  10794 ", token_exchange=" nse ")
    assert (await stored(db_session)).instrument_token == "10794"


@pytest.mark.asyncio
@pytest.mark.parametrize("token,token_exchange,why", [
    ("10794", None, "the exchange was not reported (it is only a default)"),
    ("10794", "", "an empty reported exchange"),
    ("10794", "BSE", "the reported exchange differs from the row's"),
    (None, "NSE", "no token"),
    ("", "NSE", "an empty token"),
    ("   ", "NSE", "a blank token"),
    (10794, "NSE", "a non-string token is not coerced"),
    ("9" * 51, "NSE", "a token that does not fit the column"),
])
async def test_the_token_is_not_stored_without_trustworthy_evidence(db_session, mock_redis, token, token_exchange, why):
    pipeline = pipeline_for(mock_redis)
    kwargs = dict(instrument_token=token, instrument_exchange=token_exchange)
    await pipeline.save_or_update_order(
        internal_order_id="EXT_B1", broker_order_id="B1", account_id=uuid.UUID(MASTER), source=OrderSource.BROKER_EXTERNAL,
        exchange="NSE", symbol="CANBK", side=OrderSide.BUY, order_type="LIMIT", product_type="INTRADAY", quantity=1,
        price=Decimal("100"), status=OrderStatus.OPEN, **kwargs)
    assert (await stored(db_session)).instrument_token is None, why


@pytest.mark.asyncio
async def test_a_later_report_fills_a_missing_token_but_never_erases_or_replaces_one(db_session, mock_redis, caplog):
    pipeline = pipeline_for(mock_redis)
    await save(pipeline)                                                           # historical / WebSocket-style: no token
    assert (await stored(db_session)).instrument_token is None
    await save(pipeline, token="10794", token_exchange="NSE", status=OrderStatus.COMPLETE)
    row = await stored(db_session)
    assert (row.instrument_token, row.status) == ("10794", OrderStatus.COMPLETE)   # filled; other fields still update
    await save(pipeline)                                                           # an event with no token never erases it
    assert (await stored(db_session)).instrument_token == "10794"
    with caplog.at_level(logging.WARNING, logger="copy_engine_pipeline"):
        await save(pipeline, token="99999", token_exchange="NSE")                  # a different token is never accepted
    assert (await stored(db_session)).instrument_token == "10794"
    assert "Kept the stored one" in caplog.text
    await save(pipeline, token="10794", token_exchange="NSE")                      # the same token is a no-op
    assert (await stored(db_session)).instrument_token == "10794"


@pytest.mark.asyncio
async def test_a_token_is_not_attached_to_a_row_whose_persisted_exchange_differs(db_session, mock_redis):
    pipeline = pipeline_for(mock_redis)
    await save(pipeline, exchange="NSE")
    await save(pipeline, exchange="NSE", token="74056", token_exchange="NFO")      # the broker now says NFO; the row says NSE
    assert (await stored(db_session)).instrument_token is None


@pytest.mark.asyncio
async def test_other_order_updates_are_unchanged_by_a_stored_token(db_session, mock_redis):
    pipeline = pipeline_for(mock_redis)
    await save(pipeline, token="10794", token_exchange="NSE", price=Decimal("125.00"))
    await save(pipeline, token="10794", token_exchange="NSE", price=Decimal("124.00"), status=OrderStatus.PARTIAL, filled_quantity=1)
    row = await stored(db_session)
    assert (row.price, row.status, row.filled_quantity, row.instrument_token) == (Decimal("124.00"), OrderStatus.PARTIAL, 1, "10794")


@pytest.mark.asyncio
async def test_child_order_writes_never_carry_a_token(db_session, mock_redis):
    """Child orders are written without the new arguments, exactly as before."""
    await save(pipeline_for(mock_redis), broker_order_id="C1", source=OrderSource.SYSTEM, internal="JT_child1",
               master_broker_order_id="M1")
    assert (await stored(db_session, "C1")).instrument_token is None


@pytest.mark.asyncio
async def test_the_same_token_value_on_two_exchanges_stays_two_distinct_pairs(db_session, mock_redis):
    pipeline = pipeline_for(mock_redis)
    await save(pipeline, broker_order_id="N1", exchange="NSE", token="3045", token_exchange="NSE")
    await save(pipeline, broker_order_id="X1", exchange="BSE", token="3045", token_exchange="BSE")
    a = await stored(db_session, "N1")
    first = (a.exchange, a.instrument_token)
    b = await stored(db_session, "X1")
    assert (first, (b.exchange, b.instrument_token)) == (("NSE", "3045"), ("BSE", "3045"))


# ── the Master poller ────────────────────────────────────────────────────────────────────

def record(broker_order_id="P1", status="COMPLETE", instrument_id="10794", instrument_exchange="NSE", **over):
    return OrderRecord(
        broker_order_id=broker_order_id, client_order_id=None, symbol="CANBK", exchange="NSE", side=BrokerOrderSide.BUY,
        quantity=1, filled_quantity=1 if status == "COMPLETE" else 0, average_price=Decimal("100"), status=status,
        order_timestamp=datetime.now(timezone.utc), order_type="LIMIT", product_type="INTRADAY", price=Decimal("100"),
        instrument_id=instrument_id, instrument_exchange=instrument_exchange, **over)


def poller_for(book, dispatched, mock_redis):
    client = MagicMock()
    client.get_order_book = AsyncMock(return_value=book)
    return MasterOrderPoller(master_account_id=MASTER, client_provider=lambda _a: client,
                             on_order_update=lambda e: dispatched.append(e), pipeline=pipeline_for(mock_redis),
                             check_interval_seconds=5)


@pytest.mark.asyncio
async def test_the_seed_pass_persists_the_token_and_exchange_without_dispatching(db_session, mock_redis):
    dispatched = []
    poller = poller_for([record("P1"), record("P2", instrument_id=None), record("P3", instrument_exchange=None)], dispatched, mock_redis)
    await poller.poll_once()
    assert dispatched == []
    assert (await stored(db_session, "P1")).instrument_token == "10794"
    assert (await stored(db_session, "P2")).instrument_token is None            # the row had no token
    assert (await stored(db_session, "P3")).instrument_token is None            # the exchange was not reported
    assert {(await stored(db_session, b)).exchange for b in ("P1", "P2", "P3")} == {"NSE"}


@pytest.mark.asyncio
async def test_a_new_order_after_the_baseline_is_persisted_with_its_token_and_the_event_carries_both(db_session, mock_redis):
    dispatched = []
    book = [record("OLD")]
    poller = poller_for(book, dispatched, mock_redis)
    await poller.poll_once()                                                    # baseline
    book.append(record("NEW", status="OPEN", instrument_id="74056", instrument_exchange="NFO"))
    await poller.poll_once()
    assert len(dispatched) == 1
    event = dispatched[0]
    assert (event.broker_order_id, event.instrument_id, event.instrument_exchange) == ("NEW", "74056", "NFO")


@pytest.mark.asyncio
async def test_a_later_poll_that_loses_the_token_does_not_erase_it(db_session, mock_redis):
    dispatched = []
    book = [record("P1", status="OPEN")]
    poller = poller_for(book, dispatched, mock_redis)
    await poller.poll_once()
    book[0] = record("P1", status="COMPLETE", instrument_id=None, instrument_exchange=None)
    await poller.poll_once()
    row = await stored(db_session, "P1")
    assert (row.status, row.instrument_token) == (OrderStatus.COMPLETE, "10794")


@pytest.mark.asyncio
async def test_a_manual_ui_order_gets_its_token_when_the_poller_reads_it_back(db_session, mock_redis):
    pipeline = pipeline_for(mock_redis)
    await save(pipeline, broker_order_id="UI1", internal="UI_manual_1", source=OrderSource.INTERNAL_UI)
    assert (await stored(db_session, "UI1")).instrument_token is None
    client = MagicMock()
    client.get_order_book = AsyncMock(return_value=[record("UI1")])
    poller = MasterOrderPoller(master_account_id=MASTER, client_provider=lambda _a: client, on_order_update=lambda e: None,
                               pipeline=pipeline, check_interval_seconds=5)
    await poller.poll_once()
    row = await stored(db_session, "UI1")
    assert (row.source, row.instrument_token) == (OrderSource.INTERNAL_UI, "10794")           # source is never rewritten


# ── the engine loop (REST-sourced events and WebSocket events) ───────────────────────────

async def drive_engine_loop(mock_redis, event):
    master_account_id = event.account_id
    fake_manager = MagicMock()
    fake_manager.initialize = AsyncMock()
    fake_manager.authenticate_all = AsyncMock(return_value={})
    fake_manager.get_master_client = MagicMock(return_value=_FakeMasterClient(master_account_id))
    fake_manager.get_follower_clients_map = MagicMock(return_value={})
    fake_manager.get_client = MagicMock(return_value=None)
    detector = MagicMock()
    detector.process_order_update = MagicMock(return_value=None)
    pipeline = MagicMock()
    pipeline.save_or_update_order = AsyncMock()
    pipeline.execute_replication = AsyncMock()
    recon = MagicMock()
    recon.run_crash_recovery = AsyncMock(return_value=0)
    idle = MagicMock()
    idle.start, idle.stop = AsyncMock(), AsyncMock()
    with patch("app.services.runtime.engine_loop.account_manager", fake_manager), \
         patch("app.services.runtime.engine_loop.MasterTradeDetector", return_value=detector), \
         patch("app.services.runtime.engine_loop.CopyEnginePipeline", return_value=pipeline), \
         patch("app.services.runtime.engine_loop.AliceBlueWebSocketClient", _FakeWebSocketClient), \
         patch("app.services.runtime.engine_loop.load_and_cache_contract_master", AsyncMock()), \
         patch("app.services.runtime.engine_loop.ReconciliationEngine", recon), \
         patch("app.services.runtime.engine_loop.StaleOrderSweeper", return_value=idle), \
         patch("app.services.runtime.engine_loop.FollowerFillSyncer", return_value=idle), \
         patch("app.services.runtime.engine_loop.MasterOrderPoller", return_value=idle):
        loop = EngineLoop(redis_client=mock_redis)
        await loop._run()
        loop._ws_client.on_order_update(event)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)
        await loop.stop()
    return pipeline.save_or_update_order


def _event(**over):
    base = dict(broker_order_id="E1", client_order_id=None, symbol="CANBK", side="BUY", quantity=1, filled_quantity=0,
                fill_price=Decimal("0"), status="OPEN", timestamp=datetime.now(timezone.utc), account_id=MASTER,
                product_type="INTRADAY", order_type="LIMIT", price=Decimal("100"), exchange="NSE")
    base.update(over)
    return OrderUpdateEvent(**base)


@pytest.mark.asyncio
async def test_the_engine_loop_passes_a_rest_events_token_and_reported_exchange_to_the_upsert(mock_redis, db_session):
    save_mock = await drive_engine_loop(mock_redis, _event(instrument_id="10794", instrument_exchange="NSE"))
    kwargs = save_mock.await_args.kwargs
    assert (kwargs["instrument_token"], kwargs["instrument_exchange"], kwargs["exchange"]) == ("10794", "NSE", "NSE")


@pytest.mark.asyncio
async def test_a_websocket_event_passes_no_token(mock_redis, db_session):
    """WebSocket events carry no instrument token and no reported instrument exchange."""
    save_mock = await drive_engine_loop(mock_redis, _event())
    kwargs = save_mock.await_args.kwargs
    assert (kwargs["instrument_token"], kwargs["instrument_exchange"]) == (None, None)


# ── scope ────────────────────────────────────────────────────────────────────────────────

def test_the_event_and_record_defaults_keep_existing_constructors_valid():
    event = _event()
    assert event.instrument_id is None and event.instrument_exchange is None
    assert OrderRecord("1", None, "X", "NSE", BrokerOrderSide.BUY, 1, 0, Decimal("0"), "OPEN",
                       datetime.now(timezone.utc)).instrument_exchange is None


def test_master_trade_events_and_child_placement_still_use_instrument_id_only():
    """Child orders keep receiving MasterTradeEvent.instrument_id; the new fields never reach them."""
    root = Path(__file__).resolve().parent.parent / "app" / "services" / "copy_engine"
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "instrument_exchange" not in text or path.name == "pipeline.py", path
    detector = (root / "detector.py").read_text(encoding="utf-8")
    assert "instrument_exchange" not in detector
    assert "instrument_exchange" not in MasterTradeEvent.__dataclass_fields__


def test_nothing_reads_the_stored_token_yet():
    """Only the pipeline's upsert writes it, and (R4i) the observe-only shadow job reads it for crosswalk evidence."""
    app =Path(__file__).resolve().parent.parent / "app"
    for path in app.rglob("*.py"):
        if path.name in ("trades.py",):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "instrument_token" and isinstance(node.value, ast.Name) \
                    and node.value.id in ("Order", "existing", "row"):
                # the only writer/reader is the upsert itself
                assert path.name in ("pipeline.py", "shadow.py"), (path, node.lineno)


# ── schema and migration ─────────────────────────────────────────────────────────────────

def test_the_column_is_nullable_and_fits_a_token():
    col = Order.__table__.c.instrument_token
    assert col.nullable is True and col.type.length == 50 and col.server_default is None and col.default is None


def test_the_migration_is_one_idempotent_additive_step_after_the_previous_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    backend = Path(__file__).resolve().parent.parent
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == ["0013_orders_instrument_token"]
    rev = script.get_revision("0013_orders_instrument_token")
    assert rev.down_revision == "0012_broker_credentials"
    text = (backend / "alembic" / "versions" / "0013_orders_instrument_token.py").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS instrument_token VARCHAR(50)" in text and "DROP COLUMN IF EXISTS instrument_token" in text
    for forbidden in ("DEFAULT", "NOT NULL", "UPDATE ", "DROP TABLE", "CREATE INDEX"):
        assert forbidden not in text.split("def upgrade")[1].split("def downgrade")[0], forbidden


def test_the_startup_safety_net_adds_the_same_column():
    main = (Path(__file__).resolve().parent.parent / "app" / "main.py").read_text(encoding="utf-8")
    assert "ALTER TABLE orders ADD COLUMN IF NOT EXISTS instrument_token VARCHAR(50)" in main
