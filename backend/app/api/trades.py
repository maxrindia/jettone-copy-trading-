import uuid
from typing import List, Optional
from datetime import datetime, date
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.accounts import Account
from app.models.audit import AuditLog
from app.models.trades import CopyTrade, CopyTradeStatus, Order
from app.schemas.trades import CopyTradeResponse, FanoutChild, FanoutOrder, FanoutResponse, FanoutSummary, TradeStatsResponse
from app.services.copy_engine.child_outcomes import SUMMARY_EVENT_TYPE, parse_reason

router = APIRouter(prefix="/trades", tags=["Trades & Order History"])


@router.get("", response_model=List[CopyTradeResponse])
async def list_trades(
    status_filter: Optional[CopyTradeStatus] = Query(None, alias="status"),
    follower_id: Optional[uuid.UUID] = Query(None, alias="follower_id"),
    master_order_id: Optional[str] = Query(None, max_length=100),
    limit: int = Query(50, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    query = select(CopyTrade).order_by(CopyTrade.created_at.desc()).limit(limit).offset(offset)
    if status_filter:
        query = query.where(CopyTrade.status == status_filter)
    if follower_id:
        query = query.where(CopyTrade.follower_account_id == follower_id)
    if master_order_id:
        query = query.where(CopyTrade.master_order_id == master_order_id)

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


@router.get("/fanout/{master_order_id}", response_model=FanoutResponse)
async def get_fanout(master_order_id: str, db: AsyncSession = Depends(get_db)):
    """
    What happened to every Child that was routed a Master order, one entry per CopyTrade row (so one per Child and
    Master event; a MARKET order filled in parts has one per fill), with the Child's own Order alongside for fills.
    The CopyTrade rows are the authority. `summaries` are advisory AuditLog rows - they list the members that were
    excluded from the fanout and may appear more than once if an event was reprocessed.
    """
    trades = (
        await db.execute(
            select(CopyTrade)
            .where(CopyTrade.master_order_id == master_order_id)
            .order_by(CopyTrade.queued_at.asc(), CopyTrade.created_at.asc())
            .limit(200)
        )
    ).scalars().all()
    follower_ids = {t.follower_account_id for t in trades}
    names: dict = {}
    orders_by_account: dict = {}
    if follower_ids:
        for acc in (await db.execute(select(Account).where(Account.id.in_(follower_ids)))).scalars().all():
            names[acc.id] = acc.display_name
        for o in (
            await db.execute(
                select(Order)
                .where(Order.master_broker_order_id == master_order_id, Order.account_id.in_(follower_ids))
                .order_by(Order.created_at.asc())
            )
        ).scalars().all():
            orders_by_account.setdefault(o.account_id, []).append(o)

    children = []
    counts: dict = {}
    for t in trades:
        code, detail = parse_reason(t.failure_reason)
        status_value = t.status.value if hasattr(t.status, "value") else str(t.status)
        counts[status_value] = counts.get(status_value, 0) + 1
        children.append(FanoutChild(
            copy_trade_id=t.id,
            follower_account_id=t.follower_account_id,
            follower_name=names.get(t.follower_account_id),
            copy_event_id=t.copy_event_id,
            status=t.status,
            reason_code=code,
            reason_detail=detail or (t.failure_reason if code is None else None),
            quantity=t.quantity,
            symbol=t.symbol,
            side=t.side,
            estimated_price=t.estimated_price,
            client_order_id=t.client_order_id,
            broker_order_id=t.broker_order_id,
            retry_count=t.retry_count,
            queued_at=t.queued_at,
            submitted_at=t.submitted_at,
            completed_at=t.completed_at,
            orders=[
                FanoutOrder(
                    internal_order_id=o.internal_order_id, broker_order_id=o.broker_order_id, order_type=o.order_type,
                    status=o.status.value if hasattr(o.status, "value") else str(o.status), broker_status=o.broker_status,
                    quantity=o.quantity, filled_quantity=o.filled_quantity, pending_quantity=o.pending_quantity,
                    average_price=float(o.average_price),
                )
                for o in orders_by_account.get(t.follower_account_id, [])
            ],
        ))

    audit_rows = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.event_type == SUMMARY_EVENT_TYPE, AuditLog.master_order_id == master_order_id)
            .order_by(AuditLog.created_at.desc())
            .limit(20)
        )
    ).scalars().all()
    summaries = []
    for row in audit_rows:
        meta = row.metadata_payload or {}
        summaries.append(FanoutSummary(
            recorded_at=meta.get("recorded_at"),
            event_type=meta.get("event_type"),
            fill_sequence=meta.get("fill_sequence"),
            routed_count=int(meta.get("routed_count") or 0),
            excluded=list(meta.get("excluded") or []),
            counts=dict(meta.get("counts") or {}),
        ))

    return FanoutResponse(
        master_order_id=master_order_id,
        children=children,
        counts=counts,
        summaries=summaries,
        note="copy_trades rows are the authority; summaries are advisory and may repeat if an event was reprocessed.",
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

