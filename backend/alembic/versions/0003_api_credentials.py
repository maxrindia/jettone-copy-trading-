"""api_credentials table: broker secrets encrypted at rest

Revision ID: 0003_api_credentials
Revises: 0002_reliability
Create Date: 2026-08-24 15:00:00.000000

Data migration encrypts any existing plaintext secrets from
accounts.encrypted_api_secret using ALICE_BLUE_ENCRYPTION_KEY.

NOTE on downgrade: encrypted values cannot be reverted to plaintext without
the key; downgrade restores the old column with the (still encrypted) values.
"""
from typing import Sequence, Union

import os

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from cryptography.fernet import Fernet

revision: str = "0003_api_credentials"
down_revision: Union[str, None] = "0002_reliability"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _fernet() -> Fernet:
    key = os.environ.get("ALICE_BLUE_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError(
            "ALICE_BLUE_ENCRYPTION_KEY must be set to run this migration."
        )
    return Fernet(key.encode("utf-8"))


def upgrade() -> None:
    fernet = _fernet()

    op.create_table(
        "api_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("encrypted_api_secret", sa.Text(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("account_id", name="uq_api_credentials_account_id"),
    )

    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, encrypted_api_secret FROM accounts WHERE encrypted_api_secret IS NOT NULL")
    ).fetchall()
    for account_id, plaintext_secret in rows:
        conn.execute(
            sa.text(
                "INSERT INTO api_credentials (account_id, encrypted_api_secret) VALUES (:aid, :secret)"
            ),
            {"aid": account_id, "secret": fernet.encrypt(plaintext_secret.encode("utf-8")).decode("utf-8")},
        )

    op.drop_column("accounts", "encrypted_api_secret")


def downgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("encrypted_api_secret", sa.Text(), nullable=True),
    )
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE accounts a
            SET encrypted_api_secret = c.encrypted_api_secret
            FROM api_credentials c
            WHERE c.account_id = a.id
            """
        )
    )
    # Column stays nullable in downgrade because pre-migration values were NOT NULL;
    # restoring NOT NULL is only safe once every account has a credential row.
    op.drop_table("api_credentials")
