#!/usr/bin/env bash
#
# One-time bootstrap for Let's Encrypt certificate issuance.
#
# Run this ONCE on the real VPS, after:
#   1. DOMAIN_NAME in .env points to this server (DNS A record verified),
#   2. ports 80 and 443 are reachable from the internet (firewall + provider),
#   3. LETSENCRYPT_EMAIL is set in .env.
#
# This only solves the first-issuance chicken-and-egg problem: nginx's
# HTTPS server block needs a certificate file to exist before nginx can even
# start, and certbot needs nginx already serving the ACME HTTP-01 challenge
# on port 80 before it can issue one. It is NOT part of ongoing renewal -
# once a real certificate exists, the `certbot` service in docker-compose.yml
# keeps it renewed automatically (checks every 12h, renews when <30 days
# from expiry), and the `nginx` service's own reload loop (also every 12h)
# picks up the renewed cert without needing this script again.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "Error: .env not found. Copy .env.example to .env and fill in DOMAIN_NAME / LETSENCRYPT_EMAIL first." >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

if [ -z "${DOMAIN_NAME:-}" ] || [ "$DOMAIN_NAME" = "localhost" ] || [ "$DOMAIN_NAME" = "copytrade.example.com" ]; then
  echo "Error: DOMAIN_NAME in .env ('${DOMAIN_NAME:-unset}') is not a real, publicly-resolvable domain." >&2
  echo "Let's Encrypt cannot issue a certificate for a placeholder or local domain." >&2
  exit 1
fi

if [ -z "${LETSENCRYPT_EMAIL:-}" ]; then
  echo "Error: LETSENCRYPT_EMAIL is not set in .env." >&2
  exit 1
fi

STAGING="${LETSENCRYPT_STAGING:-0}"

echo "==> [1/5] Creating a temporary self-signed certificate so nginx can start..."
docker compose run --rm --entrypoint "/bin/sh -c \"\
  mkdir -p /etc/letsencrypt/live/$DOMAIN_NAME && \
  openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
    -keyout /etc/letsencrypt/live/$DOMAIN_NAME/privkey.pem \
    -out /etc/letsencrypt/live/$DOMAIN_NAME/fullchain.pem \
    -subj '/CN=localhost'\"" certbot

echo "==> [2/5] Starting nginx with the dummy certificate..."
docker compose up -d nginx

echo "==> [3/5] Deleting the dummy certificate..."
docker compose run --rm --entrypoint "/bin/sh -c \"\
  rm -rf /etc/letsencrypt/live/$DOMAIN_NAME && \
  rm -rf /etc/letsencrypt/archive/$DOMAIN_NAME && \
  rm -rf /etc/letsencrypt/renewal/$DOMAIN_NAME.conf\"" certbot

STAGING_ARG=""
if [ "$STAGING" = "1" ]; then
  STAGING_ARG="--staging"
  echo "    (using Let's Encrypt STAGING CA - the issued cert will NOT be trusted by browsers; use this for a first dry run before requesting a real one)"
fi

echo "==> [4/5] Requesting the real Let's Encrypt certificate for $DOMAIN_NAME..."
docker compose run --rm --entrypoint "/bin/sh -c \"\
  certbot certonly --webroot -w /var/www/certbot \
    --email $LETSENCRYPT_EMAIL \
    -d $DOMAIN_NAME \
    --rsa-key-size 4096 \
    --agree-tos \
    --no-eff-email \
    $STAGING_ARG\"" certbot

echo "==> [5/5] Reloading nginx with the real certificate..."
docker compose exec nginx nginx -s reload

echo ""
echo "Done. Verify with: curl -I https://$DOMAIN_NAME"
echo "If you used LETSENCRYPT_STAGING=1, unset it in .env and rerun this script to get a browser-trusted certificate."
