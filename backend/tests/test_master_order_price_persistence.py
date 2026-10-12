"""
Issue A regression coverage: a modified master LIMIT price must be persisted.

Live evidence (2026-09-09, order 26090900100077 CANBK):
  - master LIMIT moved 125.00 -> 124.00 on the Alice Blue mobile app
  - MasterTradeDetector logged "price 125.0->124.0" correctly
  - the Jettone orders row still read price = 125.00

Root cause: CopyEnginePipeline.save_or_update_order's UPDATE branch refreshed
only the fill/status fields (filled_quantity, pending_quantity,
average_price, status, broker_status, rejection_reason). It never wrote
`price` or `quantity`, so once an Order row existed its working price was
frozen for the row's lifetime and the UI could not show the live master
price.

The fix writes both, but only when the incoming event actually carries a
meaningful value (> 0). Alice Blue reports price 0 for MARKET orders, so a 0
means "not reported by this event", never "set it to zero" - assigning it
blindly would wipe a LIMIT order's price.

`price` (requested/working LIMIT price) and `average_price` (actual fill
price) stay semantically distinct: each is assigned only from its own source
field, so a fill can never overwrite the limit price with the fill price.

No live trading is enabled and no real broker calls are made anywhere in this
file - nothing here touches a broker at all.
"""
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.trades import Order, OrderSide, OrderSource, OrderStatus
from app.services.copy_engine.pipeline import CopyEnginePipeline
from app.services.copy_engine.validator import EventValidator

MASTER_ACCOUNT = uuid.uuid4()
BROKER_ORDER_ID = "26090900100077"
INTERNAL_ID = f"EXT_{BROKER_ORDER_ID}"


def _pipeline(mock_redis):
    return CopyEnginePipeline(validator=EventValidator(redis=mock_redis))


async def _seed_limit_order(pipeline, price=Decimal("125.00"), quantity=1):
    """Create the initial master LIMIT row exactly as the poller would."""
    return await pipeline.save_or_update_order(
        internal_order_id=INTERNAL_ID,
        broker_order_id=BROKER_ORDER_ID,
        account_id=MASTER_ACCOUNT,
        source=OrderSource.BROKER_EXTERNAL,
        exchange="NSE",
        symbol="CANBK",
        side=OrderSide.BUY,
        order_type="LIMIT",
        product_type="INTRADAY",
        quantity=quantity,
        filled_quantity=0,
        pending_quantity=quantity,
        price=price,
        average_price=Decimal("0"),
        status=OrderStatus.OPEN,
        broker_status="OPEN",
    )


async def _row(db_session):
    return (
        await db_session.execute(
            select(Order).where(Order.broker_order_id == BROKER_ORDER_ID)
        )
    ).scalar_one()


# ── 1 & 2: the reported bug ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_1_2_modified_master_limit_price_is_persisted(db_session, mock_redis):
    """125.00 -> broker reports 124.00 -> the row must read 124.00."""
    pipeline = _pipeline(mock_redis)
    await _seed_limit_order(pipeline)

    row = await _row(db_session)
    assert row.price == Decimal("125.00")

    # Poller observes the modified order on a later pass.
    await pipeline.save_or_update_order(
        internal_order_id=INTERNAL_ID,
        broker_order_id=BROKER_ORDER_ID,
        account_id=MASTER_ACCOUNT,
        source=OrderSource.BROKER_EXTERNAL,
        exchange="NSE",
        symbol="CANBK",
        side=OrderSide.BUY,
        order_type="LIMIT",
        product_type="INTRADAY",
        quantity=1,
        filled_quantity=0,
        pending_quantity=1,
        price=Decimal("124.00"),
        average_price=Decimal("0"),
        status=OrderStatus.OPEN,
        broker_status="OPEN",
    )

    await db_session.refresh(row)
    assert row.price == Decimal("124.00"), "modified master LIMIT price was not persisted"


@pytest.mark.asyncio
async def test_modified_master_quantity_is_persisted(db_session, mock_redis):
    pipeline = _pipeline(mock_redis)
    await _seed_limit_order(pipeline, quantity=10)
    row = await _row(db_session)
    assert row.quantity == 10

    await pipeline.save_or_update_order(
        internal_order_id=INTERNAL_ID,
        broker_order_id=BROKER_ORDER_ID,
        account_id=MASTER_ACCOUNT,
        source=OrderSource.BROKER_EXTERNAL,
        exchange="NSE",
        symbol="CANBK",
        side=OrderSide.BUY,
        order_type="LIMIT",
        product_type="INTRADAY",
        quantity=25,
        filled_quantity=0,
        pending_quantity=25,
        price=Decimal("125.00"),
        average_price=Decimal("0"),
        status=OrderStatus.OPEN,
        broker_status="OPEN",
    )

    await db_session.refresh(row)
    assert row.quantity == 25


# ── 3 & 5: requested price vs fill price stay distinct ───────────────────
@pytest.mark.asyncio
async def test_3_5_fill_does_not_replace_limit_price_with_average_price(
    db_session, mock_redis
):
    """THE dangerous case: a LIMIT order fills at a different average price.
    price must remain the LIMIT price; average_price carries the fill."""
    pipeline = _pipeline(mock_redis)
    await _seed_limit_order(pipeline, price=Decimal("125.00"))

    await pipeline.save_or_update_order(
        internal_order_id=INTERNAL_ID,
        broker_order_id=BROKER_ORDER_ID,
        account_id=MASTER_ACCOUNT,
        source=OrderSource.BROKER_EXTERNAL,
        exchange="NSE",
        symbol="CANBK",
        side=OrderSide.BUY,
        order_type="LIMIT",
        product_type="INTRADAY",
        quantity=1,
        filled_quantity=1,
        pending_quantity=0,
        price=Decimal("125.00"),          # limit price, unchanged
        average_price=Decimal("124.55"),  # actual fill
        status=OrderStatus.COMPLETE,
        broker_status="COMPLETE",
    )

    row = await _row(db_session)
    await db_session.refresh(row)
    assert row.price == Decimal("125.00"), "LIMIT price was overwritten by the fill price"
    assert row.average_price == Decimal("124.55")
    assert row.price != row.average_price


@pytest.mark.asyncio
async def test_zero_price_event_does_not_wipe_the_limit_price(db_session, mock_redis):
    """A payload that does not report a price (0) must leave price intact."""
    pipeline = _pipeline(mock_redis)
    await _seed_limit_order(pipeline, price=Decimal("125.00"))

    await pipeline.save_or_update_order(
        internal_order_id=INTERNAL_ID,
        broker_order_id=BROKER_ORDER_ID,
        account_id=MASTER_ACCOUNT,
        source=OrderSource.BROKER_EXTERNAL,
        exchange="NSE",
        symbol="CANBK",
        side=OrderSide.BUY,
        order_type="LIMIT",
        product_type="INTRADAY",
        quantity=0,
        filled_quantity=0,
        pending_quantity=1,
        price=Decimal("0"),
        average_price=Decimal("0"),
        status=OrderStatus.OPEN,
        broker_status="OPEN",
    )

    row = await _row(db_session)
    await db_session.refresh(row)
    assert row.price == Decimal("125.00")
    assert row.quantity == 1


# ── 4: everything else keeps its existing behaviour ──────────────────────
@pytest.mark.asyncio
async def test_4_unrelated_lifecycle_fields_unchanged(db_session, mock_redis):
    """The fill/status fields must still update exactly as before, and
    identity fields must not move."""
    pipeline = _pipeline(mock_redis)
    created = await _seed_limit_order(pipeline, price=Decimal("125.00"), quantity=10)
    created_id = created.id

    await pipeline.save_or_update_order(
        internal_order_id=INTERNAL_ID,
        broker_order_id=BROKER_ORDER_ID,
        account_id=MASTER_ACCOUNT,
        source=OrderSource.BROKER_EXTERNAL,
        exchange="NSE",
        symbol="CANBK",
        side=OrderSide.BUY,
        order_type="LIMIT",
        product_type="INTRADAY",
        quantity=10,
        filled_quantity=4,
        pending_quantity=6,
        price=Decimal("124.00"),
        average_price=Decimal("123.90"),
        status=OrderStatus.PARTIAL,
        broker_status="PARTIAL",
    )

    row = await _row(db_session)
    await db_session.refresh(row)

    assert row.id == created_id            # updated in place, not duplicated
    assert row.price == Decimal("124.00")
    assert row.filled_quantity == 4
    assert row.pending_quantity == 6
    assert row.average_price == Decimal("123.90")
    assert row.status == OrderStatus.PARTIAL
    assert row.broker_status == "PARTIAL"
    assert row.symbol == "CANBK"
    assert row.side == OrderSide.BUY
    assert row.source == OrderSource.BROKER_EXTERNAL
    assert row.order_type == "LIMIT"

    rows = (
        await db_session.execute(
            select(Order).where(Order.broker_order_id == BROKER_ORDER_ID)
        )
    ).scalars().all()
    assert len(rows) == 1
