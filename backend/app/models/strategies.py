import uuid
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class GroupStrategy(Base, TimestampMixin):
    """
    A named copy-trading strategy that links one master account
    to zero or more child (follower) accounts.
    """
    __tablename__ = "group_strategies"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    visibility: Mapped[str] = mapped_column(String(10), default="PRIVATE", nullable=False)

    master_account_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    trading_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    place_rejected_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allocated_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=100000, nullable=False)
    max_subscribers: Mapped[int] = mapped_column(Integer, default=10, nullable=False)

    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    master_account: Mapped[Optional["Account"]] = relationship(  # type: ignore[name-defined]
        "Account",
        foreign_keys=[master_account_id],
    )
    members: Mapped[List["StrategyMember"]] = relationship(
        "StrategyMember",
        back_populates="strategy",
        cascade="all, delete-orphan",
    )


class StrategyMember(Base):
    """
    Junction table linking a child account to a group strategy.
    """
    __tablename__ = "strategy_members"
    __table_args__ = (
        UniqueConstraint("strategy_id", "account_id", name="uq_strategy_members"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    strategy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("group_strategies.id", ondelete="CASCADE"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    multiplier: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=1.0, nullable=False)
    trading_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        nullable=False,
    )

    # Relationships
    strategy: Mapped["GroupStrategy"] = relationship("GroupStrategy", back_populates="members")
    account: Mapped["Account"] = relationship("Account", foreign_keys=[account_id])  # type: ignore[name-defined]


STRATEGY_ROLE_MASTER = "MASTER"
STRATEGY_ROLE_CHILD = "CHILD"


class StrategyAccountRole(Base):
    """
    Membership registry: the one strategy an account currently belongs to,
    and its role there. Enforced by the database, not just the API:

    - UNIQUE(account_id): an account belongs to at most one strategy at a
      time, as Master OR Child - never both, never two strategies.
    - Partial UNIQUE(strategy_id) WHERE role = 'MASTER': at most one Master
      per strategy.

    group_strategies.master_account_id and strategy_members remain the rows
    every existing reader uses; this table is kept in step with them in the
    same transaction by app/services/strategy_membership.py. A soft-deleted
    strategy's rows here are deleted, which releases its accounts.
    """
    __tablename__ = "strategy_account_roles"
    __table_args__ = (
        UniqueConstraint("account_id", name="uq_strategy_account_roles_account"),
        CheckConstraint("role IN ('MASTER', 'CHILD')", name="ck_strategy_account_roles_role"),
        Index(
            "uq_strategy_account_roles_one_master",
            "strategy_id",
            unique=True,
            postgresql_where=text("role = 'MASTER'"),
            sqlite_where=text("role = 'MASTER'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    strategy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("group_strategies.id", ondelete="CASCADE"),
        nullable=False,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=datetime.utcnow,
        nullable=False,
    )
