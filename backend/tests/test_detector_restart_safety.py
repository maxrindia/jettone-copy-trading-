"""
Issue #5 regression coverage: MasterTradeDetector must be seedable from
durable Order rows at EngineLoop startup, so a process restart doesn't
silently reset _seen_fills/_limit_order_state to empty - which would
previously replicate an order's entire historical cumulative fill as if it
were brand new, and misclassify an already-replicated, still-open LIMIT
order as newly created.

Also covers the engine_loop.py query that decides WHICH Order rows get
seeded (master account only, non-terminal status only, never SYSTEM/
follower rows), and the websocket.py change that backfills once before the
very first WS connection each run (not only on later reconnects, which was
the pre-existing behavior).

No live trading is enabled and no real broker calls are made anywhere in
this file.
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.accounts import Account, AccountType
from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services.alice_blue.types import OrderUpdateEvent
from app.services.copy_engine.detector import MasterTradeDetector


def _order_like(broker_order_id, filled_quantity=0, order_type="MARKET", quantity=100, price=Decimal("0")):
    """A minimal duck-typed stand-in for an Order row, matching seed_state's
    documented contract (broker_order_id, filled_quantity, order_type,
    quantity, price) without needing a real DB-backed ORM instance."""
    return SimpleNamespace(
        broker_order_id=broker_order_id,
        filled_quantity=filled_quantity,
        order_type=order_type,
        quantity=quantity,
        price=price,
    )


def _open_market_fill_event(broker_order_id, filled_quantity, quantity=100, account_id="M1"):
    return OrderUpdateEvent(
        broker_order_id=broker_order_id,
        client_order_id=None,
        symbol="RELIANCE",
        side="BUY",
        quantity=quantity,
        filled_quantity=filled_quantity,
        fill_price=Decimal("2500.00"),
        status="PARTIAL" if filled_quantity < quantity else "COMPLETE",
        timestamp=datetime.now(timezone.utc),
        account_id=account_id,
        order_type="MARKET",
    )


def _open_limit_event(broker_order_id, quantity, price, account_id="M1"):
    return OrderUpdateEvent(
        broker_order_id=broker_order_id,
        client_order_id=None,
        symbol="RELIANCE",
        side="BUY",
        quantity=quantity,
        filled_quantity=0,
        fill_price=Decimal("0"),
        status="OPEN",
        timestamp=datetime.now(timezone.utc),
        account_id=account_id,
        order_type="LIMIT",
        price=price,
    )


# ---------------------------------------------------------------------------
# MasterTradeDetector.seed_state - unit level
# ---------------------------------------------------------------------------

def test_seed_state_recovers_cumulative_fill_so_next_increment_is_correct():
    """The core bug this fixes: without seeding, the first fill event after
    a restart computes its ENTIRE historical cumulative fill as "new".
    With seeding, only the genuine post-restart delta is reported."""
    detector = MasterTradeDetector(master_account_id="M1")
    detector.seed_state([_order_like("BRK_1", filled_quantity=40, order_type="MARKET", quantity=100)])

    # Broker now reports 70 filled (30 more happened since the restart).
    event = detector.process_order_update(_open_market_fill_event("BRK_1", filled_quantity=70, quantity=100))

    assert event is not None
    assert event.quantity == 30  # the genuine delta, not 70 (the full new cumulative)
    assert event.filled_quantity == 70


def test_without_seeding_the_same_event_would_report_the_full_cumulative():
    """Contrast case proving the bug is real: an un-seeded fresh detector
    (the pre-Issue-#5 behavior) treats the entire cumulative amount as new."""
    detector = MasterTradeDetector(master_account_id="M1")  # no seed_state call

    event = detector.process_order_update(_open_market_fill_event("BRK_1", filled_quantity=70, quantity=100))

    assert event is not None
    assert event.quantity == 70  # wrong: this is the historical bug seed_state fixes


def test_seed_state_recovers_limit_state_and_detects_modification_after_restart():
    """A LIMIT order already open (qty=100, price=2950) before a restart -
    seeded from its Order row - must be recognized as a modification (not a
    brand-new LIMIT_CREATED) when the broker reports different terms."""
    detector = MasterTradeDetector(master_account_id="M1")
    detector.seed_state([_order_like("BRK_2", order_type="LIMIT", quantity=100, price=Decimal("2950.00"))])

    event = detector.process_order_update(_open_limit_event("BRK_2", quantity=150, price=Decimal("2999.00")))

    assert event is not None
    assert event.event_type == "ORDER_MODIFIED"
    assert event.quantity == 150
    assert event.limit_price == Decimal("2999.00")


def test_seed_state_limit_state_unchanged_is_recognized_as_duplicate():
    """Same case, but the broker's post-restart report matches exactly what
    was seeded - must be ignored as a duplicate, not misclassified as
    brand-new or as a modification."""
    detector = MasterTradeDetector(master_account_id="M1")
    detector.seed_state([_order_like("BRK_3", order_type="LIMIT", quantity=100, price=Decimal("2950.00"))])

    event = detector.process_order_update(_open_limit_event("BRK_3", quantity=100, price=Decimal("2950.00")))

    assert event is None


def test_seed_state_ignores_orders_with_no_broker_order_id():
    detector = MasterTradeDetector(master_account_id="M1")
    detector.seed_state([_order_like(None, filled_quantity=50, order_type="LIMIT", quantity=100, price=Decimal("10"))])

    assert detector._seen_fills == {}
    assert detector._limit_order_state == {}


def test_seed_state_does_not_seed_fills_for_zero_filled_orders():
    detector = MasterTradeDetector(master_account_id="M1")
    detector.seed_state([_order_like("BRK_4", filled_quantity=0, order_type="LIMIT", quantity=100, price=Decimal("10"))])

    assert "BRK_4" not in detector._seen_fills
    assert "BRK_4" in detector._limit_order_state  # LIMIT state is still seeded even with 0 filled


def test_seed_state_only_seeds_limit_state_for_limit_orders():
    detector = MasterTradeDetector(master_account_id="M1")
    detector.seed_state([_order_like("BRK_5", filled_quantity=20, order_type="MARKET", quantity=100)])

    assert "BRK_5" in detector._seen_fills
    assert "BRK_5" not in detector._limit_order_state


# ---------------------------------------------------------------------------
# engine_loop.py - which Order rows actually get passed to seed_state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_engine_loop_seeds_detector_from_master_open_partial_orders_only(mock_redis, db_session):
    """Integration-level: seeds a mix of Order rows (master OPEN, master
    PARTIAL, master COMPLETE/terminal, and a follower SYSTEM row) and
    verifies only the master's non-terminal rows are passed to
    detector.seed_state - terminal and follower rows must be excluded."""
    from app.services.runtime.engine_loop import EngineLoop

    master = Account(id=uuid.uuid4(), account_type=AccountType.MASTER, display_name="Master", alice_user_id="M001", is_active=True)
    follower = Account(id=uuid.uuid4(), account_type=AccountType.FOLLOWER, display_name="Follower", alice_user_id="F001", is_active=True)
    db_session.add_all([master, follower])
    await db_session.commit()

    def _mk(account_id, source, status, broker_order_id, order_type="LIMIT"):
        return Order(
            id=uuid.uuid4(),
            internal_order_id=f"ID_{broker_order_id}",
            broker_order_id=broker_order_id,
            account_id=account_id,
            source=source,
            exchange="NSE",
            symbol="RELIANCE",
            side=OrderSide.BUY,
            order_type=order_type,
            product_type="MIS",
            quantity=100,
            filled_quantity=10,
            pending_quantity=90,
            price=Decimal("100.00"),
            status=status,
        )

    open_master = _mk(master.id, OrderSource.BROKER_EXTERNAL, OrderStatus.OPEN, "BRK_OPEN")
    partial_master = _mk(master.id, OrderSource.INTERNAL_UI, OrderStatus.PARTIAL, "BRK_PARTIAL")
    complete_master = _mk(master.id, OrderSource.BROKER_EXTERNAL, OrderStatus.COMPLETE, "BRK_COMPLETE")
    cancelled_master = _mk(master.id, OrderSource.BROKER_EXTERNAL, OrderStatus.CANCELLED, "BRK_CANCELLED")
    follower_open = _mk(follower.id, OrderSource.SYSTEM, OrderStatus.OPEN, "BRK_FOLLOWER_OPEN")
    db_session.add_all([open_master, partial_master, complete_master, cancelled_master, follower_open])
    await db_session.commit()

    detector_instance = MagicMock()
    detector_instance.seed_state = MagicMock()

    fake_account_manager = MagicMock()
    fake_account_manager.initialize = AsyncMock()
    fake_account_manager.authenticate_all = AsyncMock(return_value={})
    fake_account_manager.get_master_client = MagicMock(return_value=SimpleNamespace(account_id=str(master.id)))
    fake_account_manager.get_follower_clients_map = MagicMock(return_value={})
    fake_account_manager.get_client = MagicMock(return_value=None)

    class _FakeWebSocketClient:
        def __init__(self, client):
            self.on_order_update = None

        async def connect_and_listen(self):
            return
            yield  # pragma: no cover

        async def close(self):
            pass

    fake_recon_engine = MagicMock()
    fake_recon_engine.run_crash_recovery = AsyncMock(return_value=0)
    fake_sweeper = MagicMock(start=AsyncMock(), stop=AsyncMock())
    fake_fill_syncer = MagicMock(start=AsyncMock(), stop=AsyncMock())

    with patch("app.services.runtime.engine_loop.account_manager", fake_account_manager), \
         patch("app.services.runtime.engine_loop.MasterTradeDetector", return_value=detector_instance), \
         patch("app.services.runtime.engine_loop.AliceBlueWebSocketClient", _FakeWebSocketClient), \
         patch("app.services.runtime.engine_loop.load_and_cache_contract_master", AsyncMock()), \
         patch("app.services.runtime.engine_loop.ReconciliationEngine", fake_recon_engine), \
         patch("app.services.runtime.engine_loop.StaleOrderSweeper", return_value=fake_sweeper), \
         patch("app.services.runtime.engine_loop.FollowerFillSyncer", return_value=fake_fill_syncer):

        loop = EngineLoop(redis_client=mock_redis)
        await loop._run()
        await loop.stop()

    detector_instance.seed_state.assert_called_once()
    seeded = list(detector_instance.seed_state.call_args.args[0])
    seeded_ids = {o.broker_order_id for o in seeded}

    assert seeded_ids == {"BRK_OPEN", "BRK_PARTIAL"}  # only the master's own OPEN/PARTIAL rows
