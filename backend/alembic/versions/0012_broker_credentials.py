"""broker_credentials: encrypted multi-field credentials for non-Alice brokers

Revision ID: 0012_broker_credentials
Revises: 0011_account_static_ip_address
Create Date: 2026-10-16

ADDITIVE ONLY. Creates one new table; no existing table, column or row is read,
changed or dropped, and Alice Blue accounts keep using api_credentials. The table
stays empty until an Angel One account's credentials are saved through the API,
which itself requires ANGEL_ONE_ENABLED=true.

Migration plan (not run by this change):
  1. Back up first (pg_dump + pg_restore -l validation), as for every deploy.
  2. `alembic upgrade head` creates the table (safe to run with the engine up).
  3. Rollback: `alembic downgrade 0011_account_static_ip_address` drops the table;
     the only data lost is Angel One credentials, which are re-entered in the UI.
Encrypted with the same Fernet key (ALICE_BLUE_ENCRYPTION_KEY) as api_credentials,
so rotating that key makes both unreadable together.
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0012_broker_credentials'
down_revision: Union[str, None] = '0011_account_static_ip_address'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS broker_credentials (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            broker VARCHAR(50) NOT NULL,
            encrypted_payload TEXT NOT NULL,
            key_version INTEGER NOT NULL DEFAULT 1,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_broker_credentials_account_id UNIQUE (account_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS broker_credentials")
