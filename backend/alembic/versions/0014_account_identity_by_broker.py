"""accounts: identity is (broker, external user id), not the user id alone

Revision ID: 0014_account_identity_by_broker
Revises: 0013_orders_instrument_token
Create Date: 2026-10-28

Replaces the single-column UNIQUE on accounts.alice_user_id with a UNIQUE index on
(coalesce(broker, 'alicebluenew'), lower(alice_user_id)). Two different brokers can issue the same id string, and with the old
constraint the second account could not be added; the same broker's ids are now also compared case-insensitively in the
database, as the API already did.

It also adds accounts.broker if missing (the same idempotent statement the engine runs at startup), because no earlier
migration creates that column. No existing row is modified or deleted. The change refuses to run (and changes nothing) if existing rows would violate the new
rule, so a collision is reported instead of silently resolved.

Migration plan (not run by this change):
  1. Back up first (pg_dump + pg_restore -l validation), as for every deploy.
  2. `alembic upgrade head` drops the old constraint and creates the index (a short lock on the small accounts table).
  3. Rollback: `alembic downgrade 0013_orders_instrument_token` restores the single-column UNIQUE. It refuses if two accounts
     now share a user id across brokers, because the old rule cannot hold them; remove or rename one first.
"""
from typing import Sequence, Union

from alembic import op

revision: str = '0014_account_identity_by_broker'
down_revision: Union[str, None] = '0013_orders_instrument_token'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_CONSTRAINT = "accounts_alice_user_id_key"
NEW_INDEX = "uq_accounts_broker_external_id"


def upgrade() -> None:
    # accounts.broker is not created by any earlier migration; the engine adds it at startup (main.py lifespan) with exactly this
    # statement. Adding it here too, idempotently, lets a database built purely from migrations reach this revision, and is a
    # no-op where the column already exists.
    op.execute("ALTER TABLE accounts ADD COLUMN IF NOT EXISTS broker VARCHAR(50) DEFAULT 'alicebluenew'")
    # Fail before changing anything if the new rule would be violated by data that already exists.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM accounts
            GROUP BY coalesce(broker, 'alicebluenew'), lower(alice_user_id)
            HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION 'accounts has rows sharing (broker, lower(alice_user_id)); resolve them before migrating';
          END IF;
        END $$;
        """
    )
    op.execute(f"ALTER TABLE accounts DROP CONSTRAINT IF EXISTS {OLD_CONSTRAINT}")
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {NEW_INDEX} "
        "ON accounts ((coalesce(broker, 'alicebluenew')), (lower(alice_user_id)))"
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1 FROM accounts GROUP BY alice_user_id HAVING count(*) > 1
          ) THEN
            RAISE EXCEPTION 'two accounts share an alice_user_id across brokers; the old single-column rule cannot hold them';
          END IF;
        END $$;
        """
    )
    op.execute(f"DROP INDEX IF EXISTS {NEW_INDEX}")
    op.execute(f"ALTER TABLE accounts ADD CONSTRAINT {OLD_CONSTRAINT} UNIQUE (alice_user_id)")
