import uuid
from datetime import datetime
from decimal import Decimal
from typing import Optional
from pydantic import BaseModel, ConfigDict
from app.models.trades import OrderSide, CopyTradeStatus


class CopyTradeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    master_order_id: str
    follower_account_id: uuid.UUID
    copy_event_id: uuid.UUID
    symbol: str
    exchange: str
    side: OrderSide
    quantity: int
    product_type: str
    estimated_price: Optional[Decimal] = None
    client_order_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    status: CopyTradeStatus
    failure_reason: Optional[str] = None
    retry_count: int
    queued_at: datetime
    submitted_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime


class TradeStatsResponse(BaseModel):
    total_trades_today: int
    success_count: int
    rejected_count: int
    failed_count: int
    pending_count: int


class OrderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    internal_order_id: str
    broker_order_id: Optional[str] = None
    account_id: uuid.UUID
    group_strategy_id: Optional[uuid.UUID] = None
    parent_order_id: Optional[uuid.UUID] = None
    master_broker_order_id: Optional[str] = None
    source: str
    broker: str
    exchange: str
    symbol: str
    side: str
    order_type: str
    product_type: str
    quantity: int
    filled_quantity: int
    pending_quantity: int
    price: Decimal
    trigger_price: Decimal
    average_price: Decimal
    status: str
    broker_status: Optional[str] = None
    rejection_reason: Optional[str] = None
    correlation_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

