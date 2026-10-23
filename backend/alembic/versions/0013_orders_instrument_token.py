"""orders.instrument_token: the broker's instrument token for an order (R4e)

Revision ID: 0013_orders_instrument_token
Revises: 0012_broker_credentials
Create Date: 2026-10-23

ADDITIVE ONLY. Adds one nullable column; no existing column or row is read, changed or dropped, and no default is set,
so existing rows keep NULL and the change is metadata-only in PostgreSQL. The token is meaningful only together with
orders.exchange. Nothing is backfilled: a historical order has no token unless the broker reports it again.

Migration plan (not run by this change):
  1. Back up first (pg_dump + pg_restore -l validation), as for every deploy.
  2. `alembic upgrade head` adds the column (safe to run with the engine up).
  3. Rollback: `alembic downgrade 0012_broker_credentials` drops the column; the only data lost is the recorded tokens,
     which the poller records again from the broker's order book.
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0013_orders_instrument_token'
down_revision: Union[str, None] = '0012_broker_credentials'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS instrument_token VARCHAR(50)")


def downgrade() -> None:
    op.execute("ALTER TABLE orders DROP COLUMN IF EXISTS instrument_token")
