#!/bin/sh
# Bring the database schema up to date before the engine starts, so a fresh
# deploy (empty database) or an upgrade needs no manual "alembic upgrade head".
# Idempotent: a no-op when already at head. If a migration cannot be applied
# (e.g. 0008 finds an account in two strategies) it fails loudly with the reason
# and the engine does NOT start against a half-migrated schema.
set -e
echo "[entrypoint] applying database migrations (alembic upgrade head)..."
alembic upgrade head
echo "[entrypoint] migrations OK; starting: $*"
exec "$@"
