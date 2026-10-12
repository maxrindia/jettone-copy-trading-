"""Master dashboard features: visibility, master trading toggle, manual order gating."""
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select

from app.models.accounts import Account, AccountType
from app.services.reliability.emergency_stop import (
    is_master_trading_enabled,
    set_master_trading,
)


@pytest.mark.asyncio
async def test_visibility_defaults_private_and_updates(db_session, sample_accounts):
    master = sample_accounts["master"]
    assert master.visibility == "PRIVATE"
    master.visibility = "PUBLIC"
    db_session.add(master)
    await db_session.commit()
    refreshed = (await db_session.execute(
        select(Account).where(Account.id == master.id)
    )).scalar_one()
    assert refreshed.visibility == "PUBLIC"


@pytest.mark.asyncio
async def test_master_trading_toggle_defaults_on_and_roundtrips(mock_redis):
    assert await is_master_trading_enabled(mock_redis) is True
    await set_master_trading(mock_redis, False)
    assert await is_master_trading_enabled(mock_redis) is False
    await set_master_trading(mock_redis, True)
    assert await is_master_trading_enabled(mock_redis) is True


def test_manual_order_endpoint_gates_registered():
    """The manual-order route exists and is wired into the API router."""
    from app.main import app

    routes = {getattr(r, "path", "") for r in app.routes}
    assert "/api/v1/broker/manual-order" in routes
    assert "/api/v1/reliability/master-trading" in routes
