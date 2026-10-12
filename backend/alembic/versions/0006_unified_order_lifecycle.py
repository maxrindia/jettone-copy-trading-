"""unified order lifecycle table and enums

Revision ID: 0006_unified_order_lifecycle
Revises: 0005_group_strategies
Create Date: 2026-09-03

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0006_unified_order_lifecycle'
down_revision: Union[str, None] = '0005_group_strategies'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Idempotently create enums using DO block
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'order_source_enum') THEN
                CREATE TYPE order_source_enum AS ENUM ('INTERNAL_UI', 'BROKER_EXTERNAL', 'SYSTEM');
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'order_status_enum') THEN
                CREATE TYPE order_status_enum AS ENUM ('PENDING', 'OPEN', 'PARTIAL', 'COMPLETE', 'CANCELLED', 'REJECTED', 'FAILED');
            END IF;
        END $$;
    """)

    # 2. Create unified orders table (using create_type=False to avoid DuplicateObjectError)
    op.create_table(
        'orders',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('internal_order_id', sa.String(100), unique=True, nullable=False),
        sa.Column('broker_order_id', sa.String(100), nullable=True),
        sa.Column('account_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('group_strategy_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('group_strategies.id', ondelete='SET NULL'), nullable=True),
        sa.Column('parent_order_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('orders.id', ondelete='SET NULL'), nullable=True),
        sa.Column('master_broker_order_id', sa.String(100), nullable=True),
        
        sa.Column('source', postgresql.ENUM('INTERNAL_UI', 'BROKER_EXTERNAL', 'SYSTEM', name='order_source_enum', create_type=False), nullable=False, server_default='INTERNAL_UI'),
        sa.Column('broker', sa.String(50), nullable=False, server_default='ALICE_BLUE'),
        sa.Column('exchange', sa.String(20), nullable=False, server_default='NSE'),
        sa.Column('symbol', sa.String(100), nullable=False),
        sa.Column('side', postgresql.ENUM('BUY', 'SELL', name='order_side_enum', create_type=False), nullable=False),
        sa.Column('order_type', sa.String(20), nullable=False, server_default='MARKET'),
        sa.Column('product_type', sa.String(20), nullable=False, server_default='MIS'),
        
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('filled_quantity', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('pending_quantity', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('price', sa.Numeric(15, 2), nullable=False, server_default='0.00'),
        sa.Column('trigger_price', sa.Numeric(15, 2), nullable=False, server_default='0.00'),
        sa.Column('average_price', sa.Numeric(15, 2), nullable=False, server_default='0.00'),
        
        sa.Column('status', postgresql.ENUM('PENDING', 'OPEN', 'PARTIAL', 'COMPLETE', 'CANCELLED', 'REJECTED', 'FAILED', name='order_status_enum', create_type=False), nullable=False, server_default='PENDING'),
        sa.Column('broker_status', sa.String(50), nullable=True),
        sa.Column('rejection_reason', sa.Text(), nullable=True),
        sa.Column('correlation_id', sa.String(150), nullable=True),

        
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        
        sa.UniqueConstraint('broker', 'account_id', 'broker_order_id', name='uq_orders_broker_account_broker_order'),
    )

    op.create_index('idx_orders_master_broker_order', 'orders', ['master_broker_order_id'])
    op.create_index('idx_orders_account_status', 'orders', ['account_id', 'status'])
    op.create_index('idx_orders_strategy_created', 'orders', ['group_strategy_id', 'created_at'])
    op.create_index('idx_orders_broker_order_id', 'orders', ['broker_order_id'])


def downgrade() -> None:
    op.drop_table('orders')
    op.execute('DROP TYPE IF EXISTS order_status_enum')
    op.execute('DROP TYPE IF EXISTS order_source_enum')
