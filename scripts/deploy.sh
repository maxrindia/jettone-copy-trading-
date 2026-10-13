#!/usr/bin/env bash
# Update a running Jettone server to the latest origin/main.
#
#   scripts/deploy.sh          # fetch, fast-forward, rebuild, restart, health-check
#
# What it does NOT touch: .env, the Postgres/Redis volumes, TLS certificates.
#
# TLS: nginx and certbot are the opt-in "tls" Compose profile. They are only
# built/started when COMPOSE_PROFILES contains "tls" (set in .env after
# nginx/init-letsencrypt.sh has issued a certificate). A default HTTP-only
# server (reached on :8080) never starts them.
# Database migrations run automatically when the backend container starts.
#
# NOTE: this restarts the trading backend. Orders that exist at restart are
# baselined by the Master order poller and are never copied, and their later
# modifications/cancellations are not propagated - deploy when no order is open
# (outside market hours is safest).
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || { echo ".env missing - run scripts/bootstrap-env.sh first." >&2; exit 1; }
if grep -qi "CHANGE_ME" .env; then
  echo ".env still contains CHANGE_ME placeholders - refusing to deploy." >&2
  exit 1
fi

# Report the TLS mode; refuse an unfinished TLS setup (placeholder domain).
profiles="${COMPOSE_PROFILES:-$(grep -E '^COMPOSE_PROFILES=' .env | tail -1 | cut -d= -f2- | tr -d "\"'\r" || true)}"
domain="$(grep -E '^DOMAIN_NAME=' .env | tail -1 | cut -d= -f2- | tr -d "\"'\r" || true)"
case ",${profiles}," in
  *,tls,*)
    case "$domain" in
      ""|*example.com|*example.org)
        echo "COMPOSE_PROFILES enables TLS but DOMAIN_NAME is still a placeholder ('${domain}')." >&2
        echo "Set a real domain and run nginx/init-letsencrypt.sh first, or remove tls from COMPOSE_PROFILES." >&2
        exit 1 ;;
    esac
    echo "tls profile: ENABLED (nginx + certbot, domain ${domain})" ;;
  *)
    echo "tls profile: disabled (HTTP only; nginx/certbot are not started)" ;;
esac

before="$(git rev-parse --short HEAD)"
git fetch -q origin
git merge --ff-only origin/main
after="$(git rev-parse --short HEAD)"
echo "code: ${before} -> ${after} (v$(cat VERSION))"

docker compose build
docker compose up -d

echo "waiting for the backend to become healthy..."
for i in $(seq 1 60); do
  if docker compose exec -T fastapi python -c \
    "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)" \
    >/dev/null 2>&1; then
    echo "backend healthy."
    docker compose ps
    exit 0
  fi
  sleep 2
done
echo "backend did not become healthy in time; recent logs:" >&2
docker compose logs --tail 40 fastapi >&2
exit 1
