import uuid
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ─── Strategy Member Schemas ────────────────────────────────────────────────

class MemberAccountInfo(BaseModel):
    """Account info embedded inside a strategy member response."""
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    display_name: str
    alice_user_id: str
    account_type: Optional[str] = None
    broker: Optional[str] = None
    is_active: bool
    session_valid: bool


class MemberAdd(BaseModel):
    account_id: uuid.UUID
    multiplier: Decimal = Field(default=Decimal("1.00"), ge=Decimal("0.01"), le=Decimal("100.00"))
    trading_enabled: bool = True


class MemberUpdate(BaseModel):
    multiplier: Optional[Decimal] = Field(default=None, ge=Decimal("0.01"), le=Decimal("100.00"))
    trading_enabled: Optional[bool] = None


class MemberResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    strategy_id: uuid.UUID
    account_id: uuid.UUID
    multiplier: Decimal
    trading_enabled: bool
    created_at: datetime
    account: MemberAccountInfo


# ─── Strategy Schemas ────────────────────────────────────────────────────────

class StrategyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: Optional[str] = None
    visibility: str = Field(default="PRIVATE", pattern="^(PRIVATE|PUBLIC)$")
    master_account_id: Optional[uuid.UUID] = None
    allocated_amount: Decimal = Field(default=Decimal("100000"), ge=Decimal("0"))
    max_subscribers: int = Field(default=10, ge=1, le=1000)


class StrategyUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    description: Optional[str] = None
    visibility: Optional[str] = Field(default=None, pattern="^(PRIVATE|PUBLIC)$")
    master_account_id: Optional[uuid.UUID] = None
    trading_enabled: Optional[bool] = None
    place_rejected_enabled: Optional[bool] = None
    allocated_amount: Optional[Decimal] = Field(default=None, ge=Decimal("0"))
    max_subscribers: Optional[int] = Field(default=None, ge=1, le=1000)


class MasterSwitchRequest(BaseModel):
    new_master_account_id: uuid.UUID
    # What happens to the current Master: stays in the strategy as a Child
    # (copying off until enabled) or leaves the strategy entirely.
    old_master_action: str = Field(default="REMOVE", pattern="^(CHILD|REMOVE)$")


class StrategyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    description: Optional[str]
    visibility: str
    master_account_id: Optional[uuid.UUID]
    trading_enabled: bool
    place_rejected_enabled: bool
    allocated_amount: Decimal
    max_subscribers: int
    created_at: datetime
    updated_at: datetime
    # Computed
    member_count: int = 0
    master_account: Optional[MemberAccountInfo] = None
    members: List[MemberResponse] = []
