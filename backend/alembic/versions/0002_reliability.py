"""reliability schema updates

Revision ID: 0002_reliability
Revises: 0001_initial_schema
Create Date: 2026-08-21 13:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0002_reliability"
down_revision: Union[str, None] = "0001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add SUBMITTED and RECONCILIATION_REQUIRED to copy_trade_status_enum
    op.execute("ALTER TYPE copy_trade_status_enum ADD VALUE IF NOT EXISTS 'SUBMITTED'")
    op.execute("ALTER TYPE copy_trade_status_enum ADD VALUE IF NOT EXISTS 'RECONCILIATION_REQUIRED'")

    # Create reconciliation_records table
    op.create_table(
        "reconciliation_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("symbol", sa.String(length=50), nullable=False),
        sa.Column("expected_qty", sa.Integer(), nullable=False),
        sa.Column("actual_qty", sa.Integer(), nullable=False),
        sa.Column("discrepancy_qty", sa.Integer(), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index(op.f("ix_reconciliation_records_account_id"), "reconciliation_records", ["account_id"], unique=False)
    op.create_index(op.f("ix_reconciliation_records_resolved"), "reconciliation_records", ["resolved"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_reconciliation_records_resolved"), table_name="reconciliation_records")
    op.drop_index(op.f("ix_reconciliation_records_account_id"), table_name="reconciliation_records")
    op.drop_table("reconciliation_records")
