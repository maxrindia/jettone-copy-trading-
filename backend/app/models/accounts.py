import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import List, Optional
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
import enum

from app.models.base import Base, TimestampMixin


class AccountType(str, enum.Enum):
    MASTER = "MASTER"
    FOLLOWER = "FOLLOWER"


class QuantityMode(str, enum.Enum):
    FIXED = "FIXED"
    RATIO = "RATIO"
    CAPITAL_PROPORTIONAL = "CAPITAL_PROPORTIONAL"
    RISK_BASED = "RISK_BASED"


class Account(Base, TimestampMixin):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    account_type: Mapped[AccountType] = mapped_column(
        SQLEnum(AccountType, name="account_type_enum"),
        nullable=False,
    )
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    alice_user_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    broker: Mapped[Optional[str]] = mapped_column(String(50), default="alicebluenew", nullable=True)
    visibility: Mapped[str] = mapped_column(String(10), default="PRIVATE", nullable=False)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_emergency_stopped: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    session_valid: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_authenticated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Last valid day (IST calendar date) of the Static IP in notes' ProxyURL; NULL = expiry not tracked.
    proxy_expires_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationship to CopySetting
    copy_settings: Mapped[Optional["CopySetting"]] = relationship(
        "CopySetting",
        back_populates="account",
        uselist=False,
        cascade="all, delete-orphan",
    )
    api_credential: Mapped[Optional["ApiCredential"]] = relationship(
        "ApiCredential",
        back_populates="account",
        uselist=False,
        cascade="all, delete-orphan",
    )


class ApiCredential(Base, TimestampMixin):
    """
    Broker API secret, encrypted at rest with Fernet (ALICE_BLUE_ENCRYPTION_KEY).
    Plaintext never touches the database; decrypted only in-memory at client construction.
    """
    __tablename__ = "api_credentials"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    encrypted_api_secret: Mapped[str] = mapped_column(Text, nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    account: Mapped["Account"] = relationship("Account", back_populates="api_credential")


class CopySetting(Base, TimestampMixin):
    __tablename__ = "copy_settings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    follower_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("accounts.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    copy_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    quantity_mode: Mapped[QuantityMode] = mapped_column(
        SQLEnum(QuantityMode, name="quantity_mode_enum"),
        default=QuantityMode.FIXED,
        nullable=False,
    )
    
    fixed_qty: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    ratio: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 4), nullable=True)
    
    max_trade_value: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2), nullable=True)
    max_daily_loss: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2), nullable=True)
    max_exposure: Mapped[Optional[Decimal]] = mapped_column(Numeric(15, 2), nullable=True)
    allowed_symbols: Mapped[Optional[List[str]]] = mapped_column(ARRAY(String).with_variant(JSON(), "sqlite"), nullable=True)
    margin_cache_ttl_sec: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Relationship to Account
    account: Mapped["Account"] = relationship("Account", back_populates="copy_settings")
