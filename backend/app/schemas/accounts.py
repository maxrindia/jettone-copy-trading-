import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, computed_field
from app.models.accounts import AccountType, QuantityMode
from app.services.static_ip import account_static_ip_host, static_ip_status


class CopySettingBase(BaseModel):
    copy_enabled: bool = False
    quantity_mode: QuantityMode = QuantityMode.FIXED
    fixed_qty: Optional[int] = None
    ratio: Optional[Decimal] = None
    max_trade_value: Optional[Decimal] = None
    max_daily_loss: Optional[Decimal] = None
    max_exposure: Optional[Decimal] = None
    allowed_symbols: Optional[List[str]] = None
    margin_cache_ttl_sec: int = 10
    notes: Optional[str] = None


class CopySettingUpdate(BaseModel):
    copy_enabled: Optional[bool] = None
    quantity_mode: Optional[QuantityMode] = None
    fixed_qty: Optional[int] = None
    ratio: Optional[Decimal] = None
    max_trade_value: Optional[Decimal] = None
    max_daily_loss: Optional[Decimal] = None
    max_exposure: Optional[Decimal] = None
    allowed_symbols: Optional[List[str]] = None
    margin_cache_ttl_sec: Optional[int] = None
    notes: Optional[str] = None


class CopySettingResponse(CopySettingBase):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    follower_account_id: uuid.UUID
    created_at: datetime
    updated_at: datetime


class AccountBase(BaseModel):
    account_type: AccountType
    display_name: str = Field(min_length=1, max_length=100)
    alice_user_id: str = Field(min_length=1, max_length=50)
    broker: Optional[str] = "alicebluenew"
    is_active: bool = True
    notes: Optional[str] = None
    # Last valid day of the Static IP (date only, IST). Omitted/None = expiry not tracked.
    proxy_expires_on: Optional[date] = None


class AccountCreate(AccountBase):
    api_secret: str  # Plaintext incoming, encrypted at service layer


class AccountUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    broker: Optional[str] = None
    is_active: Optional[bool] = None
    session_valid: Optional[bool] = None
    last_authenticated_at: Optional[datetime] = None
    is_emergency_stopped: Optional[bool] = None
    notes: Optional[str] = None
    proxy_expires_on: Optional[date] = None
    visibility: Optional[Literal["PUBLIC", "PRIVATE"]] = None


class AccountResponse(AccountBase):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    is_emergency_stopped: bool
    session_valid: bool
    last_authenticated_at: Optional[datetime] = None
    ping_ms: Optional[int] = None
    # Set only by endpoints that can change whether the running engine holds
    # a broker client for this account (see AliceBlueAccountManager.sync_account).
    runtime_client_status: Optional[str] = None
    copy_settings: Optional[CopySettingResponse] = None
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def static_ip_host(self) -> Optional[str]:
        """Normalised host of the account's Static IP (no credentials/port), or None."""
        return account_static_ip_host(self.notes)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def static_ip_status(self) -> Optional[str]:
        """ACTIVE / EXPIRED / UNTRACKED, or None when the account has no Static IP."""
        return static_ip_status(self.notes, self.proxy_expires_on)
