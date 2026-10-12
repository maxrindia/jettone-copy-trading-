"""
Regression coverage for the P0.1 fix in app/services/runtime/engine_loop.py.

Before the fix, `persist_and_process_event()` (a closure inside
`EngineLoop._run()`) used `uuid.UUID(...)`, `OrderStatus`, `OrderSource` and
`OrderSide` without importing any of them. The very first line of that
function's `try` block raised `NameError`, which was swallowed by the
function's own blanket `except Exception`, so:
  - the incoming order event was never persisted via
    `CopyEnginePipeline.save_or_update_order`, and
  - `MasterTradeDetector.process_order_update` was never even called.

This test drives `EngineLoop._run()` with every external dependency
(account manager, WebSocket client, detector, pipeline, reconciliation,
stale-order sweeper, follower fill syncer, contract-master cache) faked out,
captures the `on_order_update` callback the real (unmodified) code wires up,
feeds it one master-account OrderUpdateEvent, and asserts that both the
detector and the pipeline are actually reached with no exception swallowed.

FollowerFillSyncer and MasterOrderPoller are faked the same way
StaleOrderSweeper is: real background loops must never be left running past
a test's own gather of "all pending tasks" below, or that gather blocks
forever waiting on a loop that only exits via explicit .stop().

ENGINE_LIVE_TRADING_ENABLED is left at its default (False) throughout - no
order placement code is exercised or enabled by this test.
"""
import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services.alice_blue.types import OrderUpdateEvent
from app.services.copy_engine.events import MasterTradeEvent
from app.services.runtime.engine_loop import EngineLoop


class _FakeMasterClient:
    def __init__(self, account_id: str):
        self.account_id = account_id


class _FakeWebSocketClient:
    """
    Stand-in for AliceBlueWebSocketClient. Captures whatever the real
    EngineLoop code assigns to `on_order_update` and yields no events itself
    (connect_and_listen is an immediately-exhausted async generator), so
    `EngineLoop._run()` returns promptly instead of blocking on a real
    WebSocket connection.
    """

    def __init__(self, client):
        self.client = client
        self.account_id = client.account_id
        self.on_order_update = None

    async def connect_and_listen(self):
        return
        yield  # pragma: no cover - makes this an async generator with 0 items

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_persist_and_process_event_reaches_detector_and_pipeline(mock_redis, db_session):
    """
    A master-account OrderUpdateEvent delivered through the real
    `on_order_update` callback must reach both
    MasterTradeDetector.process_order_update and
    CopyEnginePipeline.save_or_update_order without raising NameError.
    """
    assert settings.ENGINE_LIVE_TRADING_ENABLED is False  # sanity: live trading stays off

    master_account_id = str(uuid.uuid4())
    master_client = _FakeMasterClient(account_id=master_account_id)

    fake_account_manager = MagicMock()
    fake_account_manager.initialize = AsyncMock()
    fake_account_manager.authenticate_all = AsyncMock(return_value={})
    fake_account_manager.get_master_client = MagicMock(return_value=master_client)
    fake_account_manager.get_follower_clients_map = MagicMock(return_value={})
    fake_account_manager.get_client = MagicMock(return_value=None)

    fake_trade_event = MasterTradeEvent(
        master_order_id="AB123",
        symbol="RELIANCE-EQ",
        exchange="NSE",
        side="BUY",
        quantity=10,
        price=Decimal("2500.00"),
        product_type="MIS",
        timestamp=datetime.now(timezone.utc),
        account_id=master_account_id,
        order_type="LIMIT",
        event_type="LIMIT_CREATED",
        source="BROKER_EXTERNAL",
        status="OPEN",
    )

    detector_instance = MagicMock()
    detector_instance.process_order_update = MagicMock(return_value=fake_trade_event)

    pipeline_instance = MagicMock()
    pipeline_instance.save_or_update_order = AsyncMock()
    pipeline_instance.execute_replication = AsyncMock(return_value={"status": "completed"})

    fake_recon_engine = MagicMock()
    fake_recon_engine.run_crash_recovery = AsyncMock(return_value=0)

    fake_sweeper = MagicMock()
    fake_sweeper.start = AsyncMock()
    fake_sweeper.stop = AsyncMock()

    fake_fill_syncer = MagicMock()
    fake_fill_syncer.start = AsyncMock()
    fake_fill_syncer.stop = AsyncMock()

    fake_master_poller = MagicMock()
    fake_master_poller.start = AsyncMock()
    fake_master_poller.stop = AsyncMock()

    with patch("app.services.runtime.engine_loop.account_manager", fake_account_manager), \
         patch("app.services.runtime.engine_loop.MasterTradeDetector", return_value=detector_instance), \
         patch("app.services.runtime.engine_loop.CopyEnginePipeline", return_value=pipeline_instance), \
         patch("app.services.runtime.engine_loop.AliceBlueWebSocketClient", _FakeWebSocketClient), \
         patch("app.services.runtime.engine_loop.load_and_cache_contract_master", AsyncMock()), \
         patch("app.services.runtime.engine_loop.ReconciliationEngine", fake_recon_engine), \
         patch("app.services.runtime.engine_loop.StaleOrderSweeper", return_value=fake_sweeper), \
         patch("app.services.runtime.engine_loop.FollowerFillSyncer", return_value=fake_fill_syncer), \
         patch("app.services.runtime.engine_loop.MasterOrderPoller", return_value=fake_master_poller):

        loop = EngineLoop(redis_client=mock_redis)
        await loop._run()

        # The real (unmodified) EngineLoop code must have wired the callback.
        assert loop._ws_client is not None
        assert loop._ws_client.on_order_update is not None

        event = OrderUpdateEvent(
            broker_order_id="AB123",
            client_order_id=None,
            symbol="RELIANCE-EQ",
            side="BUY",
            quantity=10,
            filled_quantity=0,
            fill_price=Decimal("0"),
            status="OPEN",
            timestamp=datetime.now(timezone.utc),
            account_id=master_account_id,
            product_type="MIS",
            order_type="LIMIT",
            price=Decimal("2500.00"),
            trigger_price=Decimal("0"),
            rejection_reason=None,
            exchange="NSE",
        )

        # This is exactly what AliceBlueWebSocketClient.connect_and_listen()
        # does on a real message: `self.on_order_update(event)`.
        loop._ws_client.on_order_update(event)

        # handle_order_update() schedules persist_and_process_event() via
        # asyncio.create_task - let the loop actually run it to completion
        # before asserting anything.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)

        await loop.stop()

    # Before the fix: NameError inside persist_and_process_event's own
    # try/except meant neither of the calls below ever happened.
    pipeline_instance.save_or_update_order.assert_awaited()
    detector_instance.process_order_update.assert_called_once()

    called_event = detector_instance.process_order_update.call_args.args[0]
    assert called_event.account_id == master_account_id
    assert called_event.broker_order_id == "AB123"

    # ENGINE_LIVE_TRADING_ENABLED is False by default, so the pipeline's
    # replication step must NOT have fired - only detection/persistence did.
    pipeline_instance.execute_replication.assert_not_called()


@pytest.mark.asyncio
async def test_persist_and_process_event_skips_replication_for_child_account(mock_redis, db_session):
    """
    An order event for a non-master (child) account must still persist
    without a NameError, but must never reach the detector/pipeline
    replication path - replication loop prevention.
    """
    master_account_id = str(uuid.uuid4())
    child_account_id = str(uuid.uuid4())
    master_client = _FakeMasterClient(account_id=master_account_id)

    fake_account_manager = MagicMock()
    fake_account_manager.initialize = AsyncMock()
    fake_account_manager.authenticate_all = AsyncMock(return_value={})
    fake_account_manager.get_master_client = MagicMock(return_value=master_client)
    fake_account_manager.get_follower_clients_map = MagicMock(return_value={})
    fake_account_manager.get_client = MagicMock(return_value=None)

    detector_instance = MagicMock()
    detector_instance.process_order_update = MagicMock(return_value=None)

    pipeline_instance = MagicMock()
    pipeline_instance.save_or_update_order = AsyncMock()
    pipeline_instance.execute_replication = AsyncMock(return_value={"status": "completed"})

    fake_recon_engine = MagicMock()
    fake_recon_engine.run_crash_recovery = AsyncMock(return_value=0)

    fake_sweeper = MagicMock()
    fake_sweeper.start = AsyncMock()
    fake_sweeper.stop = AsyncMock()

    fake_fill_syncer = MagicMock()
    fake_fill_syncer.start = AsyncMock()
    fake_fill_syncer.stop = AsyncMock()

    fake_master_poller = MagicMock()
    fake_master_poller.start = AsyncMock()
    fake_master_poller.stop = AsyncMock()

    with patch("app.services.runtime.engine_loop.account_manager", fake_account_manager), \
         patch("app.services.runtime.engine_loop.MasterTradeDetector", return_value=detector_instance), \
         patch("app.services.runtime.engine_loop.CopyEnginePipeline", return_value=pipeline_instance), \
         patch("app.services.runtime.engine_loop.AliceBlueWebSocketClient", _FakeWebSocketClient), \
         patch("app.services.runtime.engine_loop.load_and_cache_contract_master", AsyncMock()), \
         patch("app.services.runtime.engine_loop.ReconciliationEngine", fake_recon_engine), \
         patch("app.services.runtime.engine_loop.StaleOrderSweeper", return_value=fake_sweeper), \
         patch("app.services.runtime.engine_loop.FollowerFillSyncer", return_value=fake_fill_syncer), \
         patch("app.services.runtime.engine_loop.MasterOrderPoller", return_value=fake_master_poller):

        loop = EngineLoop(redis_client=mock_redis)
        await loop._run()

        event = OrderUpdateEvent(
            broker_order_id="CHILD456",
            client_order_id=None,
            symbol="TCS-EQ",
            side="SELL",
            quantity=5,
            filled_quantity=5,
            fill_price=Decimal("3800.00"),
            status="COMPLETE",
            timestamp=datetime.now(timezone.utc),
            account_id=child_account_id,
            product_type="MIS",
            order_type="MARKET",
            price=Decimal("0"),
            trigger_price=Decimal("0"),
            rejection_reason=None,
            exchange="NSE",
        )

        loop._ws_client.on_order_update(event)

        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)

        await loop.stop()

    # Persisted (no NameError)...
    pipeline_instance.save_or_update_order.assert_awaited()
    # ...but never treated as a master trade to replicate.
    detector_instance.process_order_update.assert_not_called()
    pipeline_instance.execute_replication.assert_not_called()


# ---------------------------------------------------------------------------
# P2 #11: per-order task-ordering race
# ---------------------------------------------------------------------------
#
# `handle_order_update()` spawns an independent, uncoordinated asyncio.Task
# (via `asyncio.create_task(persist_and_process_event(event))`) for EVERY
# incoming WS order update. Inside that coroutine, `await
# pipeline.save_or_update_order(...)` (a real DB round-trip) happens BEFORE
# the synchronous, stateful `detector.process_order_update(event)` call
# (which mutates `MasterTradeDetector._limit_order_state`, keyed by
# broker_order_id). Two rapid back-to-back WS updates for the SAME
# broker_order_id (e.g. a LIMIT order created, then almost immediately
# modified) are two independent tasks - if the SECOND task's DB write
# happens to resolve before the FIRST's (plausible under real Postgres
# network latency, not reproducible against the fast in-memory SQLite this
# suite normally runs against), the detector processes the modification
# BEFORE the creation, corrupting its per-order state and emitting
# replication events in the wrong order (followers would receive the
# creation and modification quantities/prices exactly swapped).
#
# This test uses a REAL MasterTradeDetector (not mocked) so we can observe
# its actual state/emitted events, and forces the race deterministically by
# giving the mocked `pipeline.save_or_update_order` an artificial
# asyncio.sleep() that is LONGER for the creation event than for the
# modification event - simulating the creation event's DB write resolving
# slower under real network latency even though it was fired first.

class _RealDetectorSpy:
    """
    Wraps a REAL MasterTradeDetector instance (not a MagicMock) so this test
    can both exercise the actual stateful detection logic under test and
    record the order + result of every process_order_update() call, without
    needing engine_loop.py to expose its local `detector` variable.
    """

    def __init__(self):
        from app.services.copy_engine.detector import MasterTradeDetector as RealDetector

        self._RealDetector = RealDetector
        self.instance = None
        self.calls = []  # list of (input_quantity, input_price, result_event_type, result_quantity, result_price)

    def __call__(self, *args, **kwargs):
        self.instance = self._RealDetector(*args, **kwargs)
        original = self.instance.process_order_update

        def spy(event):
            result = original(event)
            self.calls.append((
                event.quantity,
                event.price,
                result.event_type if result else None,
                result.quantity if result else None,
                result.price if result else None,
            ))
            return result

        self.instance.process_order_update = spy
        return self.instance


@pytest.mark.asyncio
async def test_concurrent_updates_for_same_broker_order_id_are_processed_in_arrival_order(mock_redis, db_session):
    """
    Regression test for P2 #11. Fires a LIMIT "creation" event (qty=10 @100)
    immediately followed by a LIMIT "modification" event (qty=20 @105) for
    the SAME broker_order_id, with the creation event's DB write rigged to
    resolve slower than the modification event's.

    Fixed behavior: the detector must still process the creation before the
    modification (arrival order preserved per broker_order_id), leaving
    MasterTradeDetector._limit_order_state at the true final state (20, 105)
    - not swapped back to (10, 100) by a reordered modification being
    "undone" by a late-arriving creation.
    """
    master_account_id = str(uuid.uuid4())
    master_client = _FakeMasterClient(account_id=master_account_id)
    broker_order_id = "AB999"

    fake_account_manager = MagicMock()
    fake_account_manager.initialize = AsyncMock()
    fake_account_manager.authenticate_all = AsyncMock(return_value={})
    fake_account_manager.get_master_client = MagicMock(return_value=master_client)
    fake_account_manager.get_follower_clients_map = MagicMock(return_value={})
    fake_account_manager.get_client = MagicMock(return_value=None)

    detector_factory = _RealDetectorSpy()

    async def _save_or_update_side_effect(*args, **kwargs):
        # The creation event's DB write is rigged to resolve LAST, simulating
        # real Postgres network latency variance - the modification event's
        # (fired second, on the wire) DB write resolves first.
        if kwargs.get("quantity") == 10:
            await asyncio.sleep(0.05)
        else:
            await asyncio.sleep(0)
        return MagicMock()

    pipeline_instance = MagicMock()
    pipeline_instance.save_or_update_order = AsyncMock(side_effect=_save_or_update_side_effect)
    pipeline_instance.execute_replication = AsyncMock(return_value={"status": "completed"})

    fake_recon_engine = MagicMock()
    fake_recon_engine.run_crash_recovery = AsyncMock(return_value=0)

    fake_sweeper = MagicMock()
    fake_sweeper.start = AsyncMock()
    fake_sweeper.stop = AsyncMock()

    fake_fill_syncer = MagicMock()
    fake_fill_syncer.start = AsyncMock()
    fake_fill_syncer.stop = AsyncMock()

    fake_master_poller = MagicMock()
    fake_master_poller.start = AsyncMock()
    fake_master_poller.stop = AsyncMock()

    with patch("app.services.runtime.engine_loop.account_manager", fake_account_manager), \
         patch("app.services.runtime.engine_loop.MasterTradeDetector", detector_factory), \
         patch("app.services.runtime.engine_loop.CopyEnginePipeline", return_value=pipeline_instance), \
         patch("app.services.runtime.engine_loop.AliceBlueWebSocketClient", _FakeWebSocketClient), \
         patch("app.services.runtime.engine_loop.load_and_cache_contract_master", AsyncMock()), \
         patch("app.services.runtime.engine_loop.ReconciliationEngine", fake_recon_engine), \
         patch("app.services.runtime.engine_loop.StaleOrderSweeper", return_value=fake_sweeper), \
         patch("app.services.runtime.engine_loop.FollowerFillSyncer", return_value=fake_fill_syncer), \
         patch("app.services.runtime.engine_loop.MasterOrderPoller", return_value=fake_master_poller):

        loop = EngineLoop(redis_client=mock_redis)
        await loop._run()

        creation_event = OrderUpdateEvent(
            broker_order_id=broker_order_id,
            client_order_id=None,
            symbol="RELIANCE-EQ",
            side="BUY",
            quantity=10,
            filled_quantity=0,
            fill_price=Decimal("0"),
            status="OPEN",
            timestamp=datetime.now(timezone.utc),
            account_id=master_account_id,
            product_type="MIS",
            order_type="LIMIT",
            price=Decimal("100.00"),
            trigger_price=Decimal("0"),
            rejection_reason=None,
            exchange="NSE",
        )
        modification_event = OrderUpdateEvent(
            broker_order_id=broker_order_id,
            client_order_id=None,
            symbol="RELIANCE-EQ",
            side="BUY",
            quantity=20,
            filled_quantity=0,
            fill_price=Decimal("0"),
            status="OPEN",
            timestamp=datetime.now(timezone.utc),
            account_id=master_account_id,
            product_type="MIS",
            order_type="LIMIT",
            price=Decimal("105.00"),
            trigger_price=Decimal("0"),
            rejection_reason=None,
            exchange="NSE",
        )

        # Fired back-to-back, exactly as connect_and_listen() would deliver
        # two rapid WS messages for the same order - both create_task() calls
        # happen before either task gets a chance to run.
        loop._ws_client.on_order_update(creation_event)
        loop._ws_client.on_order_update(modification_event)

        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)

        await loop.stop()

    assert pipeline_instance.save_or_update_order.await_count == 2
    detector = detector_factory.instance
    assert detector is not None

    # The detector must have been reached in ARRIVAL order (creation, then
    # modification), regardless of which task's DB write resolved first.
    assert [c[0] for c in detector_factory.calls] == [10, 20], (
        f"detector.process_order_update() was reached out of arrival order: {detector_factory.calls}"
    )
    assert detector_factory.calls[0][2] == "LIMIT_CREATED"
    assert detector_factory.calls[1][2] == "ORDER_MODIFIED"

    # Final in-memory state must reflect the TRUE final order state (the
    # modification, qty=20 @105) - not be clobbered back to (10, 100) by a
    # reordered creation being processed after the modification.
    assert detector._limit_order_state[broker_order_id] == (20, Decimal("105.00"))
