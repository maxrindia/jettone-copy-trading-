"""initial_schema

Revision ID: 0001_initial_schema
Revises: 
Create Date: 2026-08-20 18:25:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0001_initial_schema'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Accounts Table
    op.create_table(
        'accounts',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('account_type', sa.Enum('MASTER', 'FOLLOWER', name='account_type_enum'), nullable=False),
        sa.Column('display_name', sa.String(100), nullable=False),
        sa.Column('alice_user_id', sa.String(50), nullable=False, unique=True),
        sa.Column('encrypted_api_secret', sa.Text(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('is_emergency_stopped', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('session_valid', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('last_authenticated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('idx_accounts_type', 'accounts', ['account_type'], postgresql_where=sa.text('deleted_at IS NULL'))
    op.create_index('idx_accounts_active', 'accounts', ['is_active'], postgresql_where=sa.text('deleted_at IS NULL'))

    # 2. Copy Settings Table
    op.create_table(
        'copy_settings',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('follower_account_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False, unique=True),
        sa.Column('copy_enabled', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('quantity_mode', sa.Enum('FIXED', 'RATIO', 'CAPITAL_PROPORTIONAL', 'RISK_BASED', name='quantity_mode_enum'), nullable=False, server_default='FIXED'),
        sa.Column('fixed_qty', sa.Integer(), nullable=True),
        sa.Column('ratio', sa.Numeric(6, 4), nullable=True),
        sa.Column('max_trade_value', sa.Numeric(15, 2), nullable=True),
        sa.Column('max_daily_loss', sa.Numeric(15, 2), nullable=True),
        sa.Column('max_exposure', sa.Numeric(15, 2), nullable=True),
        sa.Column('allowed_symbols', postgresql.ARRAY(sa.String()), nullable=True),
        sa.Column('margin_cache_ttl_sec', sa.Integer(), nullable=False, server_default='10'),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # 3. Copy Trades Table
    op.create_table(
        'copy_trades',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('master_order_id', sa.String(100), nullable=False, index=True),
        sa.Column('follower_account_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False, index=True),
        sa.Column('copy_event_id', postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column('symbol', sa.String(50), nullable=False),
        sa.Column('exchange', sa.String(20), nullable=False),
        sa.Column('side', sa.Enum('BUY', 'SELL', name='order_side_enum'), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('product_type', sa.String(20), nullable=False),
        sa.Column('estimated_price', sa.Numeric(12, 2), nullable=True),
        sa.Column('client_order_id', sa.String(150), unique=True, nullable=True),
        sa.Column('broker_order_id', sa.String(100), nullable=True),
        sa.Column('broker_response', postgresql.JSONB(), nullable=True),
        sa.Column('status', sa.Enum('PENDING', 'SUCCESS', 'REJECTED', 'TIMEOUT', 'FAILED', 'SKIPPED', name='copy_trade_status_enum'), nullable=False, server_default='PENDING', index=True),
        sa.Column('failure_reason', sa.Text(), nullable=True),
        sa.Column('retry_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('queued_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('master_order_id', 'follower_account_id', 'copy_event_id', name='uq_copy_trades_triple'),
    )
    op.create_index('idx_copy_trades_created', 'copy_trades', [sa.text('created_at DESC')])

    # 4. Audit Logs Table
    op.create_table(
        'audit_logs',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('event_type', sa.String(50), nullable=False, index=True),
        sa.Column('account_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('accounts.id', ondelete='SET NULL'), nullable=True, index=True),
        sa.Column('master_order_id', sa.String(100), nullable=True, index=True),
        sa.Column('copy_event_id', postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column('symbol', sa.String(50), nullable=True),
        sa.Column('exchange', sa.String(20), nullable=True),
        sa.Column('side', sa.String(10), nullable=True),
        sa.Column('quantity', sa.Integer(), nullable=True),
        sa.Column('price', sa.Numeric(12, 2), nullable=True),
        sa.Column('reason_code', sa.String(100), nullable=True),
        sa.Column('broker_order_id', sa.String(100), nullable=True),
        sa.Column('broker_response', postgresql.JSONB(), nullable=True),
        sa.Column('metadata', postgresql.JSONB(), nullable=True),
        sa.Column('ip_address', postgresql.INET(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, index=True),
    )

    # 5. System Events Table
    op.create_table(
        'system_events',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('event_type', sa.String(50), nullable=False, index=True),
        sa.Column('severity', sa.Enum('INFO', 'WARNING', 'ERROR', 'CRITICAL', name='event_severity_enum'), nullable=False, server_default='INFO', index=True),
        sa.Column('account_id', postgresql.UUID(as_uuid=True), sa.ForeignKey('accounts.id', ondelete='SET NULL'), nullable=True),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('metadata', postgresql.JSONB(), nullable=True),
        sa.Column('resolved', sa.Boolean(), nullable=False, server_default=sa.text('false'), index=True),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, index=True),
    )

    # 6. App Users Table
    op.create_table(
        'app_users',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text('gen_random_uuid()')),
        sa.Column('email', sa.String(255), nullable=False, unique=True, index=True),
        sa.Column('password_hash', sa.Text(), nullable=False),
        sa.Column('role', sa.Enum('admin', 'viewer', name='user_role_enum'), nullable=False, server_default='viewer'),
        sa.Column('totp_secret', sa.Text(), nullable=True),
        sa.Column('totp_enabled', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('failed_attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table('app_users')
    op.drop_table('system_events')
    op.drop_table('audit_logs')
    op.drop_table('copy_trades')
    op.drop_table('copy_settings')
    op.drop_table('accounts')

    op.execute('DROP TYPE IF EXISTS user_role_enum')
    op.execute('DROP TYPE IF EXISTS event_severity_enum')
    op.execute('DROP TYPE IF EXISTS copy_trade_status_enum')
    op.execute('DROP TYPE IF EXISTS order_side_enum')
    op.execute('DROP TYPE IF EXISTS quantity_mode_enum')
    op.execute('DROP TYPE IF EXISTS account_type_enum')
