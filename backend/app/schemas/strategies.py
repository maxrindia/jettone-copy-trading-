import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

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


class QuantityPolicyUpdate(BaseModel):
    """How a Child's copy quantity is derived. Only the two policies the
    strategy page offers are accepted here."""
    quantity_mode: str = Field(pattern="^(MULTIPLIER|FIXED)$")
    # MULTIPLIER: Child qty = Master qty x multiplier (omit to keep the current one).
    multiplier: Optional[Decimal] = Field(default=None, ge=Decimal("0.01"), le=Decimal("100.00"))
    # FIXED: the same quantity for every copied order (omit to keep the current one).
    fixed_qty: Optional[int] = Field(default=None, ge=1, le=1_000_000)


class QuantityPolicyResponse(BaseModel):
    account_id: uuid.UUID
    quantity_mode: str
    multiplier: Decimal
    fixed_qty: Optional[int] = None


class BrokerPairInfo(BaseModel):
    """R6: whether the order-time broker-pair gate (R5) accepts this Master -> Child pair. Read-only diagnostics: broker ids and a
    reason only. Membership is not execution support: when `supported` is not true, orders are NOT copied to this Child."""
    master_broker: Optional[str] = None
    child_broker: Optional[str] = None
    supported: Optional[bool] = None            # None only when the strategy has no Master (unknown, never "supported")
    code: Optional[str] = None                  # BROKER_PAIR_NOT_SUPPORTED when unsupported
    requirement: Optional[str] = None           # the missing requirement, e.g. PAIR_NOT_ALLOWLISTED
    detail: str = ""
    message: str = ""


class MemberResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    strategy_id: uuid.UUID
    account_id: uuid.UUID
    multiplier: Decimal
    trading_enabled: bool
    created_at: datetime
    account: MemberAccountInfo
    broker_pair: Optional[BrokerPairInfo] = None     # R6, additive


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
    # Only honored when the sole blockers are "broker verification unavailable".
    acknowledge_unverified: bool = False
    # Required when the preflight reports open CNC positions: the user
    # acknowledges they stay open in Alice Blue and are not closed or
    # transferred by Jettone. Enforced by the engine, not just the UI.
    acknowledge_cnc_positions: bool = False


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
    # R6, additive: pair support and warnings for this strategy (see services/strategy_readiness.py).
    readiness: Optional[Dict[str, Any]] = None
