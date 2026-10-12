"""accounts.proxy_expires_on: optional Static IP expiry date

Revision ID: 0007_account_proxy_expires_on
Revises: 0006_unified_order_lifecycle
Create Date: 2026-10-08

NULL means "expiry not tracked" (every pre-existing account). Idempotent
because main.py's startup safety net may already have added the column.
"""
from typing import Sequence, Union
from alembic import op

revision: str = '0007_account_proxy_expires_on'
down_revision: Union[str, None] = '0006_unified_order_lifecycle'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS proxy_expires_on DATE")


def downgrade() -> None:
    op.execute("ALTER TABLE accounts DROP COLUMN IF EXISTS proxy_expires_on")
