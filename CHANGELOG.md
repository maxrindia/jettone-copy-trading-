# Changelog

## v1.0.0

First production release.

### Platform
- Next.js 14 frontend, FastAPI backend (`backend/`), PostgreSQL 16, Redis 7, nginx.
- Alice Blue integration (REST order book polling, order placement/modify/cancel, positions, funds).
- Single-owner login (NextAuth, bcrypt password hash).

### Copy trading
- Strategy-specific Masters: one strategy per account, one Master per strategy, enforced by the
  database (migration 0008) and the API. A strategy's Master is switched at runtime, no restart.
- Safe Master switching: live preflight blocks on working orders, open positions, in-flight copies
  and unverifiable broker state; never cancels orders or squares off positions.
- Strict routing: an order is copied only to Children of the strategy whose Master placed it; no
  matching or disabled strategy copies nothing (audited).
- LIMIT / MARKET / SL / SL-M copying, modification and cancellation propagation, partial fills,
  per-child quantity modes and risk checks, emergency stop, follower fill sync, reconciliation.
- Static IP / proxy management with expiry tracking.
- Manual Place Order on the selected strategy's Master.

### Operations
- The backend applies database migrations on start (`backend/docker-entrypoint.sh`).
- `scripts/bootstrap-env.sh` creates `.env` with freshly generated secrets; the backend refuses to
  start while `.env.example` placeholders remain.
- `scripts/deploy.sh` updates a server; GitHub Actions CI runs backend tests, frontend typecheck,
  tests and build, and Docker image builds.

### Known limitations
- Delivery (CNC/NRML) holdings cannot be verified against Alice Blue (no holdings API verified).
- The WebSocket proxy tunnel is not working; Masters are detected by order-book polling.
- Dashboard assumes a single live Master.
