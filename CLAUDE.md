# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Jettone is a real-money copy trading platform for Indian equities/derivatives, currently scoped to **personal use only**: each strategy has one explicit Master Alice Blue demat account whose orders fan out to the Child accounts of that strategy (an account belongs to at most one strategy, as Master or Child - enforced by migration 0008 and the API). It is a 5-service Docker Compose stack:

- `nextjs` (port 3000) — Next.js 14 App Router + TypeScript UI, NextAuth session auth. The only service exposed to the internet (behind nginx).
- `fastapi` (port 8000, internal only, `expose` not `ports`) — Python 3.12 async trading engine: WebSocket order-event ingestion, copy pipeline, risk checks, broker adapters.
- `postgres` 16 — accounts, strategies, orders, copy trades, audit logs.
- `redis` 7 — broker session tokens, dedupe/idempotency keys, pub/sub.
- `nginx` — reverse proxy to `nextjs`, but only wired for TLS+domain deployments; it has no host port bound by default (see the `nginx` service comment in `docker-compose.yml`) because the VPS's own host-level nginx already owns 80/443 for other domains. In that mode `nextjs` is reached directly on host ports `3000` and `8080` (the latter because Alice Blue's registered OAuth redirect URI points at `:8080`).

**Critical architectural invariant: the Next.js layer is the only thing the internet can reach.** FastAPI is never given a host port mapping — only `expose`. Nothing in the browser calls FastAPI directly.

## Commands

### Docker (primary way to run this project)
```bash
docker compose up -d --build          # build & start full stack
docker compose ps                     # container health status
docker compose logs -f fastapi        # engine logs
docker compose logs -f nextjs         # frontend logs
docker compose build fastapi && docker compose up -d fastapi   # rebuild just the engine after backend changes
docker compose build nextjs && docker compose up -d nextjs     # rebuild just the frontend after frontend changes
docker compose exec fastapi alembic upgrade head                # run DB migrations
```

### Backend (Python/FastAPI) — `backend/`
Local venv already exists at repo root `.venv`.
```bash
cd backend
pytest                                          # full suite (config: pytest.ini, asyncio session-scoped loop)
pytest tests/test_copy_engine.py                # single file
pytest tests/test_copy_engine.py -k test_name   # single test
pytest --cov=app tests/                         # with coverage
alembic revision -m "description"               # new migration
alembic upgrade head                            # apply migrations
```
Tests use an in-memory SQLite DB (`sqlite+aiosqlite:///:memory:`) by default via `tests/conftest.py`, not Postgres — no live DB needed to run the suite. The Alice Blue broker API is always mocked in tests; never hit the real broker from a test.

### Frontend (Next.js) — `frontend/`
```bash
cd frontend
npm run dev      # dev server
npm run build    # production build
npm run lint     # eslint
```

## Architecture

### Request path: browser → Next.js → FastAPI
The frontend never talks to FastAPI directly from client code. Every engine call goes through:
1. `frontend/src/app/api/v1/[...path]/route.ts` — a catch-all proxy route that forwards any `/api/v1/*` request to the engine.
2. `frontend/src/lib/engine.ts` (`engineFetch`) — attaches the `X-Internal-Token` header (from `INTERNAL_API_TOKEN` env var) and `TRADING_ENGINE_URL`, normalizes FastAPI error bodies into `{ ok, data }` / `{ ok, status, error }`.
3. `backend/app/core/security.py` (`require_internal_token`) — FastAPI dependency that **fails closed**: if `INTERNAL_API_TOKEN` is unset, every request gets `503`; if the header doesn't match, `401`. This is the only auth boundary in front of the engine — treat it as load-bearing.

Page auth is separate: `frontend/src/middleware.ts` uses NextAuth (`withAuth`) to gate all routes except `/auth/signin` and static assets.

### Copy engine pipeline (`backend/app/services/copy_engine/`)
Core replication logic lives in `pipeline.py` (`CopyEnginePipeline`). Order flow is **dual-ingestion**: a master LIMIT order replicates to children immediately on creation (no wait for master fill); a master MARKET order replicates to children on fill. Each child tracks fills independently, and master cancel/modify propagates to children's still-pending quantity. Supporting modules:
- `detector.py` — detects master trade events from the WS/order feed.
- `validator.py` (`EventValidator`) — pre-copy validation.
- `calculator.py` (`QuantityCalculator`) — turns a master quantity + `StrategyMember.multiplier` into a follower quantity.
- `risk.py` / `risk_inputs.py` (`RiskEngine`) — margin/exposure/daily-loss checks; fails closed (reject + log) on uncertainty, never guesses.
- `events.py` — `MasterTradeEvent`, `derive_copy_event_id` (idempotency key derivation).

**Routing is strict:** `CopyEnginePipeline._get_eligible_followers_for_master` copies only to the Children of the non-deleted strategy whose Master placed the order, and only while that strategy's trading is enabled. There is no "copy to all followers" fallback: no matching/disabled strategy means no copy (audited as `NO_MATCHING_STRATEGY` / `STRATEGY_TRADING_DISABLED`). Modification/cancel of already-copied orders keep the original lookup (`existing_children_lookup=True`) because they never create children.

A strategy's Master is changed only through `POST /strategies/{id}/switch-master` (live preflight: working orders, open positions, in-flight copies, broker verification; never cancels or squares off). `EngineLoop` runs one detector + order-book poller per strategy Master and re-syncs them after a switch without a restart.

Idempotency is enforced at the DB level via a unique constraint on `(master_order_id, follower_account_id, copy_event_id)` on `copy_trades` — not just application logic. Do not weaken or bypass this.

### Broker abstraction (`backend/app/services/brokers/` + `alice_blue/`)
`brokers/base.py` defines `BaseBrokerClient`, an abstract async interface (`authenticate`, `place_order`, `cancel_order`, `get_order_book`, `get_positions`, `get_funds`) intended to support multiple brokers. `brokers/catalog.py` lists many broker metadata entries (Zerodha, Angel One, Dhan, Fyers, etc.) for the account-add UI, but `brokers/factory.py` currently routes **every** broker ID to `AliceBlueClient` — only Alice Blue (`alice_blue/client.py`, `manager.py`, `websocket.py`) is actually implemented. Don't assume other brokers are wired up just because they appear in the catalog.

Alice Blue auth uses daily OAuth web login (tokens valid up to ~18h). Sessions are stored per-account in Redis (`alice:session:{account_id}`), never shared across accounts, and treated as expired if `last_authenticated_at` isn't from the current calendar day.

### Runtime engine loop
`backend/app/services/runtime/engine_loop.py` (`EngineLoop`) is started/stopped in `main.py`'s lifespan — it owns the persistent Alice Blue WebSocket connection(s) and drives the copy pipeline. `ENGINE_LIVE_TRADING_ENABLED=false` (the default) puts it in **observe-only mode**: the WS feed and risk pipeline still run, but no broker orders are ever placed. Only flip this after Phases 1–5 (see below) are manually verified — do not enable live order placement casually.

### Reliability layer (`backend/app/services/reliability/`)
`reconciliation.py` compares expected position (derived from `copy_trades`) against actual broker-reported positions and raises alerts on mismatch — never trust the DB alone as position truth. `emergency_stop.py` is a platform-wide kill switch. `retry.py` / `stale_order_sweeper.py` handle broker call retries and cleanup of orders stuck pending.

### Database
Schema is managed via Alembic (`backend/alembic/versions/`), currently at `0008_strategy_account_roles.py` (applied automatically by `backend/docker-entrypoint.sh` on container start). Note `main.py`'s lifespan also runs a couple of `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements directly at startup as a lightweight safety net for columns that may be missing — this is supplementary to Alembic, not a replacement; new schema changes should still go through a proper Alembic migration. Key tables: `accounts` (demat credentials, encrypted secrets via `app/core/crypto.py` AES/Fernet, `broker` column), `group_strategies` (`master_account_id` FK), `strategy_members` (child links with `multiplier`, `trading_enabled`), `orders` / `copy_trades` (execution + audit trail), `audit_logs` / `system_events`.

### Frontend structure
- `frontend/src/app/(app)/` — authenticated route group: `/`, `/demat`, `/group-strategy` (+ `[groupId]` detail), `/orders`, `/reconciliation`, `/master`. Several routes (`/followers`, `/copy-config`, `/watchlist`, `/static-ip`) are legacy redirects kept for old links.
- `frontend/src/components/dashboard/` — tab components (`OverviewTab`, `AccountsTab`, `CopyConfigTab`, `OrdersTab`, `StaticIPTab`, `EmergencyStopModal`).
- `frontend/src/lib/use-engine-data.ts` — client-side data-fetching hooks against the `/api/v1/*` proxy.
- `frontend/src/lib/auth.ts` — NextAuth config (single admin user, bcrypt password hash from `ADMIN_PASSWORD_HASH` env var, generated via `frontend/scripts/hash-password.mjs`).

## Project-specific engineering rules

Rules that most affect day-to-day changes here:

- **Scope discipline**: this is single-user/personal (1 owner, 6 demat accounts they own). Do not add multi-tenant/SaaS features (user growth stats, credit/wallet systems, public strategy visibility, admin panels over other people's accounts) unless explicitly asked.
- **Fail closed, not open**: if risk validation, margin check, or account/session status is uncertain, reject/skip the copy order and log it — never guess and proceed. Mirrors the `require_internal_token` and `ENGINE_LIVE_TRADING_ENABLED` patterns above.
- **No plaintext secrets**: broker API keys/secrets are encrypted at rest (`app/core/crypto.py`); never log tokens, secrets, or passwords.
- **Never assume Alice Blue's API contract from memory** — if a needed endpoint's shape or multi-account/IP-whitelisting behavior isn't confirmed, flag it rather than guessing an implementation.
- **Full-page scroll layout pattern** (frontend): root is `height:100dvh; overflow:hidden`; the single scrollable element is `<main>` with `flex:1; min-height:0; overflow-y:auto`. `overflow:hidden` on cards/forms/tables/modals is forbidden except for text truncation or avatar circles — it clips buttons at 110%+ zoom.
- **Response envelope**: engine JSON errors follow `{ success: false, error: { code, message } }` (see `main.py`'s global exception handler and `route.ts`'s proxy error mapping) — keep new endpoints consistent with this shape.

## Environment

`.env` (never commit; `.env.example` documents every var) drives both Docker Compose and local runs. Notable ones beyond the obvious DB/Redis URLs: `INTERNAL_API_TOKEN` (engine fails closed without it), `ALICE_BLUE_ENCRYPTION_KEY` (Fernet key for credential encryption — rotating it makes stored credentials undecryptable), `MASTER_CAPITAL` (engine refuses to start if ≤ 0), `ENGINE_LIVE_TRADING_ENABLED`, `STATIC_VPS_IP` (Alice Blue whitelists by IP — the whole system assumes calls originate from one static VPS IP), `TWO_FACTOR_ENABLED` (TOTP gate, not yet fully implemented).
