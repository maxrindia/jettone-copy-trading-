import uuid
from datetime import datetime
from decimal import Decimal
from typing import List, Optional
from sqlalchemy import (
    JSON,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
import enum

from app.models.base import Base, TimestampMixin


class OrderSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderSource(str, enum.Enum):
    INTERNAL_UI = "INTERNAL_UI"
    BROKER_EXTERNAL = "BROKER_EXTERNAL"
    SYSTEM = "SYSTEM"


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    PARTIAL = "PARTIAL"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class CopyTradeStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    SUCCESS = "SUCCESS"
    REJECTED = "REJECTED"
    TIMEOUT = "TIMEOUT"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class Order(Base, TimestampMixin):
    """
    Unified normalized order record across all accounts (Master & Followers)
    and all sources (INTERNAL_UI, BROKER_EXTERNAL, SYSTEM).
    """
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    internal_order_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    broker_order_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    group_strategy_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("group_strategies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    parent_order_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="SET NULL"),
        nullable=True,
    )
    master_broker_order_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)

    source: Mapped[OrderSource] = mapped_column(
        SQLEnum(OrderSource, name="order_source_enum"),
        default=OrderSource.INTERNAL_UI,
        nullable=False,
    )
    broker: Mapped[str] = mapped_column(String(50), default="ALICE_BLUE", nullable=False)
    exchange: Mapped[str] = mapped_column(String(20), default="NSE", nullable=False)
    # The broker's own instrument token for this order, exactly as its order book reported it (R4e). It identifies an
    # instrument only TOGETHER with `exchange` (the same value exists on more than one exchange). NULL means not
    # reported or not known: it is never inferred, never backfilled, and no ISIN or mapping is stored here.
    instrument_token: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    symbol: Mapped[str] = mapped_column(String(100), nullable=False)
    side: Mapped[OrderSide] = mapped_column(SQLEnum(OrderSide, name="order_side_enum"), nullable=False)
    order_type: Mapped[str] = mapped_column(String(20), default="MARKET", nullable=False)  # MARKET, LIMIT, SL, SLM
    product_type: Mapped[str] = mapped_column(String(20), default="MIS", nullable=False)   # MIS, NRML, CNC

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    pending_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"), nullable=False)
    trigger_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"), nullable=False)
    average_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=Decimal("0.00"), nullable=False)

    status: Mapped[OrderStatus] = mapped_column(
        SQLEnum(OrderStatus, name="order_status_enum"),
        default=OrderStatus.PENDING,
        nullable=False,
        index=True,
    )
    broker_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)

    # Relationships
    account: Mapped["Account"] = relationship("Account", foreign_keys=[account_id])  # type: ignore[name-defined]
    parent_order: Mapped[Optional["Order"]] = relationship("Order", remote_side=[id], back_populates="child_orders")
    child_orders: Mapped[List["Order"]] = relationship("Order", back_populates="parent_order")

    __table_args__ = (
        UniqueConstraint("broker", "account_id", "broker_order_id", name="uq_orders_broker_account_broker_order"),
    )


class CopyTrade(Base, TimestampMixin):
    __tablename__ = "copy_trades"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    
    # Idempotency Triple
    master_order_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    follower_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    copy_event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    # Order details
    symbol: Mapped[str] = mapped_column(String(50), nullable=False)
    exchange: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[OrderSide] = mapped_column(SQLEnum(OrderSide, name="order_side_enum"), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    product_type: Mapped[str] = mapped_column(String(20), nullable=False)
    estimated_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2), nullable=True)

    # Submission & Broker tracking
    client_order_id: Mapped[Optional[str]] = mapped_column(String(150), unique=True, nullable=True)
    broker_order_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    broker_response: Mapped[Optional[dict]] = mapped_column(JSONB().with_variant(JSON(), "sqlite"), nullable=True)

    # Status lifecycle
    status: Mapped[CopyTradeStatus] = mapped_column(
        SQLEnum(CopyTradeStatus, name="copy_trade_status_enum"),
        default=CopyTradeStatus.PENDING,
        nullable=False,
        index=True,
    )
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Timestamps
    queued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Hard Idempotency Constraint across the entire table
    __table_args__ = (
        UniqueConstraint(
            "master_order_id",
            "follower_account_id",
            "copy_event_id",
            name="uq_copy_trades_triple",
        ),
    )
