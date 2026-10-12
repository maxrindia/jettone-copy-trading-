"""master account visibility (PUBLIC/PRIVATE)

Revision ID: 0004_master_visibility
Revises: 0003_api_credentials
Create Date: 2026-08-25 06:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004_master_visibility"
down_revision: Union[str, None] = "0003_api_credentials"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "accounts",
        sa.Column("visibility", sa.String(length=10), nullable=False, server_default="PRIVATE"),
    )
    op.create_check_constraint(
        "ck_accounts_visibility",
        "accounts",
        "visibility IN ('PUBLIC', 'PRIVATE')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_accounts_visibility", "accounts", type_="check")
    op.drop_column("accounts", "visibility")
