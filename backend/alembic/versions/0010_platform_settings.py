"""platform_settings: durable runtime switches (Live Trading ON/OFF)

Revision ID: 0010_platform_settings
Revises: 0009_quantity_mode_multiplier
Create Date: 2026-10-14

Adds one small table. No existing table or row is read or changed. With no row
in it the Live Trading state falls back to ENGINE_LIVE_TRADING_ENABLED, so an
upgraded install behaves exactly as before until someone uses the toggle.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = '0010_platform_settings'
down_revision: Union[str, None] = '0009_quantity_mode_multiplier'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table('platform_settings'):
        return
    op.create_table(
        'platform_settings',
        sa.Column('key', sa.String(100), primary_key=True),
        sa.Column('value', sa.String(500), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_by', sa.String(100), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('platform_settings')
