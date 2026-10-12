import uuid
from typing import List, Optional
from datetime import datetime, date
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.trades import CopyTrade, CopyTradeStatus, Order
from app.schemas.trades import CopyTradeResponse, TradeStatsResponse

router = APIRouter(prefix="/trades", tags=["Trades & Order History"])


@router.get("", response_model=List[CopyTradeResponse])
async def list_trades(
    status_filter: Optional[CopyTradeStatus] = Query(None, alias="status"),
    follower_id: Optional[uuid.UUID] = Query(None, alias="follower_id"),
    limit: int = Query(50, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    query = select(CopyTrade).order_by(CopyTrade.created_at.desc()).limit(limit).offset(offset)
    if status_filter:
        query = query.where(CopyTrade.status == status_filter)
    if follower_id:
        query = query.where(CopyTrade.follower_account_id == follower_id)

    res = await db.execute(query)
    return res.scalars().all()


@router.get("/stats", response_model=TradeStatsResponse)
async def get_trade_stats(db: AsyncSession = Depends(get_db)):
    today_start = datetime.combine(date.today(), datetime.min.time())
    
    total_query = select(func.count(CopyTrade.id)).where(CopyTrade.created_at >= today_start)
    success_query = total_query.where(CopyTrade.status == CopyTradeStatus.SUCCESS)
    rejected_query = total_query.where(CopyTrade.status == CopyTradeStatus.REJECTED)
    failed_query = total_query.where(CopyTrade.status == CopyTradeStatus.FAILED)
    pending_query = total_query.where(CopyTrade.status == CopyTradeStatus.PENDING)

    total = (await db.execute(total_query)).scalar() or 0
    success = (await db.execute(success_query)).scalar() or 0
    rejected = (await db.execute(rejected_query)).scalar() or 0
    failed = (await db.execute(failed_query)).scalar() or 0
    pending = (await db.execute(pending_query)).scalar() or 0

    return TradeStatsResponse(
        total_trades_today=total,
        success_count=success,
        rejected_count=rejected,
        failed_count=failed,
        pending_count=pending,
    )


def serialize_order(o: Order) -> dict:
    """Shared Order -> API dict shape, used by both the unified /trades/orders
    listing and the per-account /accounts/{account_id}/orders endpoint."""
    return {
        "id": str(o.id),
        "internal_order_id": o.internal_order_id,
        "broker_order_id": o.broker_order_id,
        "account_id": str(o.account_id),
        "group_strategy_id": str(o.group_strategy_id) if o.group_strategy_id else None,
        "parent_order_id": str(o.parent_order_id) if o.parent_order_id else None,
        "master_broker_order_id": o.master_broker_order_id,
        "source": o.source.value if hasattr(o.source, "value") else str(o.source),
        "broker": o.broker,
        "exchange": o.exchange,
        "symbol": o.symbol,
        "side": o.side.value if hasattr(o.side, "value") else str(o.side),
        "order_type": o.order_type,
        "product_type": o.product_type,
        "quantity": o.quantity,
        "filled_quantity": o.filled_quantity,
        "pending_quantity": o.pending_quantity,
        "price": float(o.price),
        "trigger_price": float(o.trigger_price),
        "average_price": float(o.average_price),
        "status": o.status.value if hasattr(o.status, "value") else str(o.status),
        "broker_status": o.broker_status,
        "rejection_reason": o.rejection_reason,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "updated_at": o.updated_at.isoformat() if o.updated_at else None,
    }


@router.get("/orders")
async def list_orders(
    account_id: Optional[uuid.UUID] = Query(None, alias="account_id"),
    group_strategy_id: Optional[uuid.UUID] = Query(None, alias="group_strategy_id"),
    limit: int = Query(50, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    query = select(Order).order_by(Order.created_at.desc()).limit(limit).offset(offset)
    if account_id:
        query = query.where(Order.account_id == account_id)
    if group_strategy_id:
        query = query.where(Order.group_strategy_id == group_strategy_id)

    res = await db.execute(query)
    orders = res.scalars().all()
    return [serialize_order(o) for o in orders]

