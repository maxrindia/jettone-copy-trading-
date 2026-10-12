"""
P3 Finding #14 regression coverage: a follower (non-master) account's own
broker-originated order events must never reach copy-trade replication.

This invariant is enforced by two independent guards:

1. `app/services/copy_engine/detector.py` -
   `MasterTradeDetector.process_order_update`'s very first check:
       if event.account_id != self.master_account_id:
           return None
   An independent, redundant guard even if something ever called the
   detector directly with a non-master event.

2. `app/services/runtime/engine_loop.py` - inside the
   `persist_and_process_event` closure built in `EngineLoop._run()`:
       if event.account_id != master_client.account_id:
           logger.debug(...)
           return
   This runs AFTER the order is persisted to the DB (so a follower's own
   order is still tracked) but BEFORE `detector.process_order_update` is
   ever called - so a follower event never reaches the detector/pipeline at
   all via this path either.

Both guards were verified read-only, against the current code, to already
be logically correct before this file was written. This file adds the
missing dedicated regression coverage so a future refactor of either guard
that silently weakens/removes it gets caught by a test, rather than only
by a read-only audit.

No live trading is enabled and no real broker calls are made anywhere in
this file.
"""
import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.alice_blue.types import OrderUpdateEvent
from app.services.copy_engine.detector import MasterTradeDetector
from app.services.runtime.engine_loop import EngineLoop


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _order_update_event(account_id, status="COMPLETE", order_type="MARKET",
                         quantity=10, filled_quantity=10, broker_order_id="BRK_1",
                         price=Decimal("0")):
    return OrderUpdateEvent(
        broker_order_id=broker_order_id,
        client_order_id=None,
        symbol="RELIANCE-EQ",
        side="BUY",
        quantity=quantity,
        filled_quantity=filled_quantity,
        fill_price=Decimal("2500.00"),
        status=status,
        timestamp=datetime.now(timezone.utc),
        account_id=account_id,
        product_type="MIS",
        order_type=order_type,
        price=price,
    )


# ---------------------------------------------------------------------------
# Guard #1 - MasterTradeDetector.process_order_update (detector.py)
# ---------------------------------------------------------------------------

class TestDetectorRejectsNonMasterAccountEvents:
    """Directly exercises the detector's own account_id guard, independent
    of anything in engine_loop.py."""

    @pytest.mark.parametrize(
        "status,order_type,quantity,filled_quantity",
        [
            ("COMPLETE", "MARKET", 10, 10),   # would-be MARKET_ACCEPTED fill
            ("PARTIAL", "MARKET", 10, 5),      # would-be incremental fill
            ("OPEN", "LIMIT", 10, 0),          # would-be LIMIT_CREATED
            ("CANCELLED", "LIMIT", 10, 0),     # would-be ORDER_CANCELLED
            ("REJECTED", "MARKET", 10, 0),     # would-be ORDER_REJECTED
        ],
    )
    def test_returns_none_for_mismatched_account_id(self, status, order_type, quantity, filled_quantity):
        """For every event shape that would otherwise produce a
        MasterTradeEvent, an account_id that doesn't match the detector's
        configured master_account_id must short-circuit to None before any
        of that per-status logic runs."""
        master_account_id = str(uuid.uuid4())
        follower_account_id = str(uuid.uuid4())
        detector = MasterTradeDetector(master_account_id=master_account_id)

        event = _order_update_event(
            account_id=follower_account_id,
            status=status,
            order_type=order_type,
            quantity=quantity,
            filled_quantity=filled_quantity,
        )

        result = detector.process_order_update(event)

        assert result is None

    def test_mismatched_account_event_does_not_mutate_detector_state(self):
        """The guard must fire before any state is touched - a follower
        event must leave _seen_fills and _limit_order_state exactly as they
        were (verifiable without touching detector.py, since both are
        plain public-ish attributes on the instance)."""
        master_account_id = str(uuid.uuid4())
        follower_account_id = str(uuid.uuid4())
        detector = MasterTradeDetector(master_account_id=master_account_id)

        # Seed some pre-existing state to prove it's untouched afterward, not
        # just that it stays at its empty default.
        detector._seen_fills["BRK_PRIOR"] = (5, 5)
        detector._limit_order_state["BRK_PRIOR_LIMIT"] = (100, Decimal("50.00"))
        seen_fills_before = dict(detector._seen_fills)
        limit_state_before = dict(detector._limit_order_state)

        fill_event = _order_update_event(
            account_id=follower_account_id,
            status="COMPLETE",
            order_type="MARKET",
            quantity=10,
            filled_quantity=10,
            broker_order_id="BRK_FOLLOWER_FILL",
        )
        limit_event = _order_update_event(
            account_id=follower_account_id,
            status="OPEN",
            order_type="LIMIT",
            quantity=10,
            filled_quantity=0,
            broker_order_id="BRK_FOLLOWER_LIMIT",
            price=Decimal("123.45"),
        )

        assert detector.process_order_update(fill_event) is None
        assert detector.process_order_update(limit_event) is None

        assert detector._seen_fills == seen_fills_before
        assert detector._limit_order_state == limit_state_before
        assert "BRK_FOLLOWER_FILL" not in detector._seen_fills
        assert "BRK_FOLLOWER_LIMIT" not in detector._limit_order_state

    def test_matching_master_account_id_is_not_blocked(self):
        """Contrast case: the exact same event shape, but with the account_id
        the guard is supposed to allow through, must NOT return None (proves
        the None results above come from the account_id mismatch specifically,
        not from some unrelated aspect of the event)."""
        master_account_id = str(uuid.uuid4())
        detector = MasterTradeDetector(master_account_id=master_account_id)

        event = _order_update_event(
            account_id=master_account_id,
            status="OPEN",
            order_type="LIMIT",
            quantity=10,
            filled_quantity=0,
            broker_order_id="BRK_MASTER_LIMIT",
            price=Decimal("123.45"),
        )

        result = detector.process_order_update(event)

        assert result is not None
        assert result.event_type == "LIMIT_CREATED"


# ---------------------------------------------------------------------------
# Guard #2 - persist_and_process_event closure (engine_loop.py)
# ---------------------------------------------------------------------------

class _FakeMasterClient:
    def __init__(self, account_id: str):
        self.account_id = account_id


class _FakeWebSocketClient:
    """Stand-in for AliceBlueWebSocketClient - captures on_order_update and
    yields no events itself, matching the pattern already established in
    test_engine_loop.py and test_detector_restart_safety.py."""

    def __init__(self, client):
        self.client = client
        self.account_id = client.account_id
        self.on_order_update = None

    async def connect_and_listen(self):
        return
        yield  # pragma: no cover - makes this an async generator with 0 items

    async def close(self):
        pass


def _build_patched_engine_loop(mock_redis, master_client, detector_instance, pipeline_instance):
    fake_account_manager = MagicMock()
    fake_account_manager.initialize = AsyncMock()
    fake_account_manager.authenticate_all = AsyncMock(return_value={})
    fake_account_manager.get_master_client = MagicMock(return_value=master_client)
    fake_account_manager.get_follower_clients_map = MagicMock(return_value={})
    fake_account_manager.get_client = MagicMock(return_value=None)

    fake_recon_engine = MagicMock()
    fake_recon_engine.run_crash_recovery = AsyncMock(return_value=0)

    # Every real background loop must be faked out: the tests below gather
    # "all pending tasks", which would otherwise block forever on a loop that
    # only exits via an explicit .stop().
    fake_sweeper = MagicMock(start=AsyncMock(), stop=AsyncMock())
    fake_fill_syncer = MagicMock(start=AsyncMock(), stop=AsyncMock())
    fake_master_poller = MagicMock(start=AsyncMock(), stop=AsyncMock())

    return patch.multiple(
        "app.services.runtime.engine_loop",
        account_manager=fake_account_manager,
        MasterTradeDetector=MagicMock(return_value=detector_instance),
        CopyEnginePipeline=MagicMock(return_value=pipeline_instance),
        AliceBlueWebSocketClient=_FakeWebSocketClient,
        load_and_cache_contract_master=AsyncMock(),
        ReconciliationEngine=fake_recon_engine,
        StaleOrderSweeper=MagicMock(return_value=fake_sweeper),
        FollowerFillSyncer=MagicMock(return_value=fake_fill_syncer),
        MasterOrderPoller=MagicMock(return_value=fake_master_poller),
    )


@pytest.mark.asyncio
async def test_persist_and_process_event_never_calls_detector_for_follower_event(mock_redis, db_session):
    """A follower-account OrderUpdateEvent delivered through the real
    on_order_update callback must:
      - still be persisted (save_or_update_order awaited) - tracking a
        follower's own order is intentional and must keep happening, and
      - never reach detector.process_order_update or
        pipeline.execute_replication at all - the replication-loop-
        prevention guard this file targets.
    """
    master_account_id = str(uuid.uuid4())
    follower_account_id = str(uuid.uuid4())
    master_client = _FakeMasterClient(account_id=master_account_id)

    detector_instance = MagicMock()
    detector_instance.process_order_update = MagicMock(return_value=None)

    pipeline_instance = MagicMock()
    pipeline_instance.save_or_update_order = AsyncMock()
    pipeline_instance.execute_replication = AsyncMock(return_value={"status": "completed"})

    with _build_patched_engine_loop(mock_redis, master_client, detector_instance, pipeline_instance):
        loop = EngineLoop(redis_client=mock_redis)
        await loop._run()

        event = _order_update_event(
            account_id=follower_account_id,
            status="COMPLETE",
            order_type="MARKET",
            quantity=5,
            filled_quantity=5,
            broker_order_id="BRK_FOLLOWER_EVENT",
        )

        loop._ws_client.on_order_update(event)

        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)

        await loop.stop()

    # Persistence still happens for a follower's own order...
    pipeline_instance.save_or_update_order.assert_awaited_once()
    persisted_kwargs = pipeline_instance.save_or_update_order.call_args.kwargs
    assert persisted_kwargs["account_id"] == uuid.UUID(follower_account_id)
    assert persisted_kwargs["broker_order_id"] == "BRK_FOLLOWER_EVENT"

    # ...but replication is never even attempted to be detected, let alone executed.
    detector_instance.process_order_update.assert_not_called()
    pipeline_instance.execute_replication.assert_not_called()


@pytest.mark.asyncio
async def test_persist_and_process_event_calls_detector_for_master_event(mock_redis, db_session):
    """Contrast case in the same harness: a matching master-account event
    for the SAME EngineLoop instance/guard DOES reach the detector. Proves
    the previous test's "never called" result is specifically caused by the
    account_id mismatch, not some unrelated wiring problem that would make
    the detector unreachable regardless of account_id."""
    master_account_id = str(uuid.uuid4())
    master_client = _FakeMasterClient(account_id=master_account_id)

    detector_instance = MagicMock()
    detector_instance.process_order_update = MagicMock(return_value=None)

    pipeline_instance = MagicMock()
    pipeline_instance.save_or_update_order = AsyncMock()
    pipeline_instance.execute_replication = AsyncMock(return_value={"status": "completed"})

    with _build_patched_engine_loop(mock_redis, master_client, detector_instance, pipeline_instance):
        loop = EngineLoop(redis_client=mock_redis)
        await loop._run()

        event = _order_update_event(
            account_id=master_account_id,
            status="COMPLETE",
            order_type="MARKET",
            quantity=5,
            filled_quantity=5,
            broker_order_id="BRK_MASTER_EVENT",
        )

        loop._ws_client.on_order_update(event)

        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)

        await loop.stop()

    pipeline_instance.save_or_update_order.assert_awaited_once()
    detector_instance.process_order_update.assert_called_once()
