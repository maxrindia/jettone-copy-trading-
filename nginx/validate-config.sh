#!/usr/bin/env bash
#
# Validates nginx/nginx.conf + the rendered nginx/templates/default.conf.template
# with nginx's own `nginx -t`, without needing a real domain or a real
# certificate: renders the template with a placeholder DOMAIN_NAME, generates
# a throwaway self-signed cert at the path the config expects, and runs the
# check inside a disposable nginx:alpine container. Nothing here talks to
# Let's Encrypt or touches the real certbot volumes/certificates.
#
# Requires: docker, envsubst (gettext), openssl.
set -euo pipefail

cd "$(dirname "$0")/.."

TEST_DOMAIN="nginx-config-test.invalid"
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

mkdir -p "$WORKDIR/conf.d" "$WORKDIR/certs/live/$TEST_DOMAIN" "$WORKDIR/webroot"

DOMAIN_NAME="$TEST_DOMAIN" envsubst '${DOMAIN_NAME}' \
  < nginx/templates/default.conf.template \
  > "$WORKDIR/conf.d/default.conf"

openssl req -x509 -nodes -newkey rsa:2048 -days 1 \
  -keyout "$WORKDIR/certs/live/$TEST_DOMAIN/privkey.pem" \
  -out "$WORKDIR/certs/live/$TEST_DOMAIN/fullchain.pem" \
  -subj "/CN=$TEST_DOMAIN" >/dev/null 2>&1

docker run --rm \
  -v "$(pwd)/nginx/nginx.conf:/etc/nginx/nginx.conf:ro" \
  -v "$WORKDIR/conf.d:/etc/nginx/conf.d:ro" \
  -v "$WORKDIR/certs:/etc/letsencrypt:ro" \
  -v "$WORKDIR/webroot:/var/www/certbot:ro" \
  nginx:alpine nginx -t

echo "nginx configuration is valid."
