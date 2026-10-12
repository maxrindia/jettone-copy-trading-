"""strategy_account_roles: one strategy per account, one Master per strategy

Revision ID: 0008_strategy_account_roles
Revises: 0007_account_proxy_expires_on
Create Date: 2026-10-09

Creates the membership registry and backfills it from every NON-deleted
strategy's master_account_id (role MASTER) and strategy_members rows (role
CHILD). Soft-deleted strategies are not backfilled, which releases their
accounts.

Existing data is validated BEFORE anything is created. If any account is
assigned more than once (two strategies, or Master and Child of the same
strategy), the migration aborts and lists every conflict - it never picks a
winner. Resolve the listed conflicts in the UI (remove the account from all
but one strategy), then run the migration again.
"""
import uuid
from collections import defaultdict
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '0008_strategy_account_roles'
down_revision: Union[str, None] = '0007_account_proxy_expires_on'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _collect_assignments(bind):
    rows = bind.execute(sa.text(
        """
        SELECT gs.id AS strategy_id, gs.name AS strategy_name, gs.master_account_id AS account_id, 'MASTER' AS role
        FROM group_strategies gs
        WHERE gs.deleted_at IS NULL AND gs.master_account_id IS NOT NULL
        UNION ALL
        SELECT gs.id, gs.name, sm.account_id, 'CHILD'
        FROM strategy_members sm
        JOIN group_strategies gs ON gs.id = sm.strategy_id
        WHERE gs.deleted_at IS NULL
        """
    )).fetchall()
    # Raw text SELECTs return UUIDs as uuid.UUID (asyncpg) or str (other
    # drivers); normalize so the typed bulk insert below accepts either.
    return [(uuid.UUID(str(r[0])), r[1], uuid.UUID(str(r[2])), r[3]) for r in rows]


def upgrade() -> None:
    bind = op.get_bind()
    assignments = _collect_assignments(bind)

    by_account = defaultdict(list)
    for strategy_id, strategy_name, account_id, role in assignments:
        by_account[str(account_id)].append((strategy_name, str(strategy_id), role))

    conflicts = {acc: entries for acc, entries in by_account.items() if len(entries) > 1}
    if conflicts:
        names = {
            str(uuid.UUID(str(r[0]))): r[1]
            for r in bind.execute(sa.text("SELECT id, display_name FROM accounts")).fetchall()
        }
        lines = []
        for acc, entries in sorted(conflicts.items()):
            where = "; ".join(f"{role} of '{name}' ({sid})" for name, sid, role in entries)
            lines.append(f"  - account '{names.get(acc, acc)}' ({acc}): {where}")
        raise RuntimeError(
            "Migration 0008 aborted: these accounts belong to more than one strategy (or are both "
            "Master and Child of one strategy). Each account may belong to exactly one strategy. "
            "Remove the extra memberships, then run the migration again. Nothing was changed.\n"
            + "\n".join(lines)
        )

    op.create_table(
        "strategy_account_roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "strategy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("group_strategies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("account_id", name="uq_strategy_account_roles_account"),
        sa.CheckConstraint("role IN ('MASTER', 'CHILD')", name="ck_strategy_account_roles_role"),
    )
    op.create_index(
        "uq_strategy_account_roles_one_master",
        "strategy_account_roles",
        ["strategy_id"],
        unique=True,
        postgresql_where=sa.text("role = 'MASTER'"),
    )

    table = sa.table(
        "strategy_account_roles",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("strategy_id", postgresql.UUID(as_uuid=True)),
        sa.column("account_id", postgresql.UUID(as_uuid=True)),
        sa.column("role", sa.String),
    )
    if assignments:
        op.bulk_insert(
            table,
            [
                {"id": uuid.uuid4(), "strategy_id": sid, "account_id": acc, "role": role}
                for sid, _name, acc, role in assignments
            ],
        )

    # Every Child must have a CopySetting so the pipeline's RiskEngine gate
    # runs for it. Children that lack one (e.g. an account created as MASTER
    # and later added as a Child) get one with copying DISABLED - the same
    # defaults account creation uses. Existing CopySettings are untouched.
    op.execute(
        """
        INSERT INTO copy_settings (
            id, follower_account_id, copy_enabled, quantity_mode, fixed_qty,
            margin_cache_ttl_sec, created_at, updated_at
        )
        SELECT gen_random_uuid(), r.account_id, false, 'FIXED', 1, 10, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        FROM strategy_account_roles r
        LEFT JOIN copy_settings cs ON cs.follower_account_id = r.account_id
        WHERE r.role = 'CHILD' AND cs.id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("uq_strategy_account_roles_one_master", table_name="strategy_account_roles")
    op.drop_table("strategy_account_roles")
