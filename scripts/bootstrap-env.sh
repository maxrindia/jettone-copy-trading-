#!/usr/bin/env bash
# Creates .env for a fresh Jettone install: copies .env.example and fills in
# newly generated secrets, so a clean machine can run:
#
#   git clone <repo> && cd jettone
#   scripts/bootstrap-env.sh --url http://<server-ip>:8080
#   docker compose up -d --build        # migrations run automatically on start
#
# Generated here (never reused from another install): Postgres/Redis passwords,
# NEXTAUTH_SECRET, INTERNAL_API_TOKEN, ALICE_BLUE_ENCRYPTION_KEY and the admin
# password hash. Broker (Alice Blue) credentials are NOT part of .env - add the
# demat accounts in the app after logging in.
#
# Options:
#   --url URL    public URL of the app (NEXTAUTH_URL), e.g. http://1.2.3.4:8080
#   --vps-ip IP  the server's static IP (STATIC_VPS_IP), for reference
#   --force      overwrite an existing .env (its secrets are lost!)
# The admin password is read from $ADMIN_PASSWORD, or prompted for (hidden).
set -euo pipefail
cd "$(dirname "$0")/.."

URL="http://localhost:8080"
VPS_IP=""
FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --url) URL="$2"; shift 2 ;;
    --vps-ip) VPS_IP="$2"; shift 2 ;;
    --force) FORCE=1; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }
command -v docker >/dev/null || { echo "docker is required (used once to hash the admin password)" >&2; exit 1; }
[ -f .env.example ] || { echo ".env.example not found (run from the repo)" >&2; exit 1; }
if [ -f .env ] && [ "$FORCE" != "1" ]; then
  echo ".env already exists - not touching it. Use --force to replace it (this discards its secrets)." >&2
  exit 1
fi

PASSWORD="${ADMIN_PASSWORD:-}"
if [ -z "$PASSWORD" ]; then
  read -r -s -p "Choose the admin login password: " PASSWORD; echo
  read -r -s -p "Repeat it: " PASSWORD2; echo
  [ "$PASSWORD" = "$PASSWORD2" ] || { echo "Passwords do not match." >&2; exit 1; }
fi
[ ${#PASSWORD} -ge 8 ] || { echo "Use at least 8 characters." >&2; exit 1; }

# bcrypt hash via Apache's htpasswd (no local bcrypt install needed).
HASH="$(printf '%s' "$PASSWORD" | docker run --rm -i httpd:2.4-alpine htpasswd -niBC 10 admin | cut -d: -f2- | tr -d '\r\n')"
case "$HASH" in '$2'*) ;; *) echo "Could not generate the admin password hash." >&2; exit 1 ;; esac
unset PASSWORD PASSWORD2

PG_PASS="$(openssl rand -hex 24)"
REDIS_PASS="$(openssl rand -hex 24)"
NEXTAUTH_SECRET="$(openssl rand -hex 32)"
INTERNAL_TOKEN="$(openssl rand -hex 32)"
# Fernet key = urlsafe base64 of 32 random bytes.
FERNET_KEY="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n')"
# .env/compose treat "$" as a variable marker, so every "$" in the hash is doubled
# (the app undoes this - see frontend/src/lib/auth.ts).
HASH_ESCAPED="${HASH//\$/\$\$}"

cp .env.example .env
set_kv() { # KEY VALUE  ('|' is the sed delimiter; generated values never contain it)
  if ! grep -q "^$1=" .env; then echo "$1 missing from .env.example" >&2; exit 1; fi
  sed -i "s|^$1=.*|$1=$2|" .env
}
set_kv POSTGRES_PASSWORD "$PG_PASS"
set_kv DATABASE_URL "postgresql+asyncpg://copytrade_admin:${PG_PASS}@postgres:5432/copytrading"
set_kv REDIS_PASSWORD "$REDIS_PASS"
set_kv REDIS_URL "redis://:${REDIS_PASS}@redis:6379/0"
set_kv NEXTAUTH_SECRET "$NEXTAUTH_SECRET"
set_kv NEXTAUTH_URL "$URL"
set_kv INTERNAL_API_TOKEN "$INTERNAL_TOKEN"
set_kv ALICE_BLUE_ENCRYPTION_KEY "$FERNET_KEY"
set_kv ADMIN_PASSWORD_HASH "$HASH_ESCAPED"
[ -n "$VPS_IP" ] && set_kv STATIC_VPS_IP "$VPS_IP"
chmod 600 .env

cat <<EOF

.env created (permissions 600). Next:
  1. Review .env: DOMAIN_NAME / STATIC_VPS_IP, MASTER_CAPITAL, TELEGRAM_*.
  2. docker compose up -d --build      (schema migrations run automatically)
  3. Open ${URL}, log in as "admin" with the password you chose, then add your
     demat accounts. ENGINE_LIVE_TRADING_ENABLED stays false until you set it.
  4. Back up .env somewhere safe. Losing ALICE_BLUE_ENCRYPTION_KEY makes stored
     broker credentials undecryptable.
EOF
