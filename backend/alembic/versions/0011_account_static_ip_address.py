"""accounts.static_ip_address: the public IP assigned to the account's Static IP

Revision ID: 0011_account_static_ip_address
Revises: 0010_platform_settings
Create Date: 2026-10-14

Adds one nullable column. No existing row is read or changed: until an address
is entered, an account's proxy is shown as "not mapped" and is not counted as a
verified Static IP.
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0011_account_static_ip_address'
down_revision: Union[str, None] = '0010_platform_settings'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS static_ip_address VARCHAR(45)")


def downgrade() -> None:
    op.execute("ALTER TABLE accounts DROP COLUMN IF EXISTS static_ip_address")
