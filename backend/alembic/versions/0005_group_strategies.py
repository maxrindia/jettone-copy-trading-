"""group_strategies and strategy_members tables

Revision ID: 0005_group_strategies
Revises: 0004_master_visibility
Create Date: 2026-08-27

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0005_group_strategies'
down_revision: Union[str, None] = '0004_master_visibility'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Group Strategies Table
    op.create_table(
        'group_strategies',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('visibility', sa.String(10), nullable=False, server_default='PRIVATE'),
        sa.Column('master_account_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('accounts.id', ondelete='SET NULL'), nullable=True),
        sa.Column('trading_enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('place_rejected_enabled', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('allocated_amount', sa.Numeric(15, 2), nullable=False, server_default='100000'),
        sa.Column('max_subscribers', sa.Integer(), nullable=False, server_default='10'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('idx_group_strategies_active', 'group_strategies', ['deleted_at'], postgresql_where=sa.text('deleted_at IS NULL'))

    # 2. Strategy Members Table (Child Accounts linked to a Strategy)
    op.create_table(
        'strategy_members',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('strategy_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('group_strategies.id', ondelete='CASCADE'), nullable=False),
        sa.Column('account_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('multiplier', sa.Numeric(6, 2), nullable=False, server_default='1.00'),
        sa.Column('trading_enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('strategy_id', 'account_id', name='uq_strategy_members'),
    )
    op.create_index('idx_strategy_members_strategy', 'strategy_members', ['strategy_id'])
    op.create_index('idx_strategy_members_account', 'strategy_members', ['account_id'])


def downgrade() -> None:
    op.drop_table('strategy_members')
    op.drop_table('group_strategies')
