"""quantity_mode_enum: add MULTIPLIER (Child qty = Master qty x strategy multiplier)

Revision ID: 0009_quantity_mode_multiplier
Revises: 0008_strategy_account_roles
Create Date: 2026-10-13

Only makes the new mode AVAILABLE. No copy_settings row is read or changed:
every existing Child keeps its current quantity_mode / fixed_qty and its
copy_enabled flag exactly as they are. A Child is moved to MULTIPLIER only
by an explicit change (the strategy page, or a separately approved data
migration).
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0009_quantity_mode_multiplier'
down_revision: Union[str, None] = '0008_strategy_account_roles'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        # SQLite (tests) stores the enum as plain text - nothing to alter.
        return
    # Postgres 12+ allows ADD VALUE inside a transaction as long as the new
    # value is not USED in that same transaction (it is not - this migration
    # touches no rows). Production runs Postgres 16.
    op.execute("ALTER TYPE quantity_mode_enum ADD VALUE IF NOT EXISTS 'MULTIPLIER'")


def downgrade() -> None:
    # Postgres cannot drop a value from an enum type, and rows may use it.
    # Refuse rather than silently rewrite anyone's quantity policy.
    bind = op.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    in_use = bind.exec_driver_sql(
        "SELECT count(*) FROM copy_settings WHERE quantity_mode::text = 'MULTIPLIER'"
    ).scalar()
    if in_use:
        raise RuntimeError(
            f"Cannot downgrade: {in_use} copy_settings row(s) use quantity_mode MULTIPLIER. "
            "Change them to another mode explicitly first."
        )
    # The unused enum label is left in place (harmless; Postgres cannot remove it).
