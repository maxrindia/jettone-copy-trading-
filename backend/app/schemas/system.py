import uuid
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict
from app.models.audit import EventSeverity


class SystemEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    event_type: str
    severity: EventSeverity
    account_id: Optional[uuid.UUID] = None
    message: str
    resolved: bool
    resolved_at: Optional[datetime] = None
    created_at: datetime


class EmergencyStopRequest(BaseModel):
    account_id: Optional[uuid.UUID] = None  # None means global emergency stop
    stop: bool = True
    reason: str


class StaticIPStatusResponse(BaseModel):
    vps_ip: str
    is_whitelisted: bool
    last_verified_at: datetime
    # Distinct Static IP hosts configured on the demat accounts (see services/static_ip.py).
    # available = active + untracked; an IP with no stored expiry is never counted as expired.
    available_count: int = 0
    active_count: int = 0
    untracked_count: int = 0
    expired_count: int = 0
    broker: str = "Alice Blue ANT/A3"
