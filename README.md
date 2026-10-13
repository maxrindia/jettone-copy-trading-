# 🚀 Jettone - Alice Blue Copy Trading Platform

**v1.0.0** · see [CHANGELOG.md](CHANGELOG.md)

A real-time, containerized copy trading system for **Alice Blue**: one Master demat account's orders are copied to the Child accounts of its strategy. Built for personal use (single owner login).

> ⚠️ This system places **real orders with real money**. Keep this repository **private**, never commit `.env`, and read the *Operations* notes below before deploying.

```
backend/    FastAPI trading backend (Python 3.12) + Alembic migrations
frontend/   Next.js 14 web app
nginx/      reverse proxy / TLS helpers
scripts/    bootstrap-env.sh (create .env), deploy.sh (update a server)
```

---

## 🏗️ System Architecture & Stack

The platform runs as a **4-service containerized stack** powered by Docker Compose (plus an optional, opt-in TLS proxy):

1. **`copytrade_nextjs`** (Port `3000`): Next.js 14 App Router Frontend with clean visual styling, live API hooks, dynamic drawers, and responsive trading dashboards.
2. **`copytrade_fastapi`** (Port `8000`, internal only): Python FastAPI trading backend (`backend/`) with async order dispatching, multiplier calculators, risk validation, and Alice Blue ANT API integration.
3. **`copytrade_postgres`** (Port `5432`): PostgreSQL database storing Demat accounts, group strategies, child member links, order execution logs, and audit trails.
4. **`copytrade_redis`** (Port `6379`): In-memory cache & pub/sub messaging queue for low-latency signal distribution.
5. *(optional, `tls` profile)* **`copytrade_nginx`** + **`copytrade_certbot`**: TLS reverse proxy for a server with its own domain. Not started by default; see [TLS](#tls-optional).

---

## 🔄 Core Copy Trading Workflow

The platform follows a streamlined **3-Step Setup Workflow**:

```
[ Step 1: Add Demat Accounts ] ➔ [ Step 2: Create Group Strategy & Assign Master/Children ] ➔ [ Step 3: Live Order Execution & Audit ]
```

### Step 1: Demat Account Management (`/demat`)
- Add all Demat trading accounts (Alice Blue credentials, User ID, API Key, API Secret).
- Accounts are created as neutral broker endpoints without hardcoded roles.

### Step 2: Group Strategy Configuration (`/group-strategy`)
- **Create Strategy**: Define strategy name, visibility (`PRIVATE`/`PUBLIC`), and allocated capital.
- **Assign Master**: Pick which Demat account acts as the trade signal provider (**Master Account**).
- **Link Children**: Open the **Account Picker Drawer** to link child follower accounts with custom **Lot Multipliers** (e.g., `1.0x`, `2.0x`, `0.5x`) and toggle trading status.

### Step 3: Automated Fan-out Execution & Audit (`/orders` & `/reconciliation`)
- **Order Manager (`/orders`)**: Real-time logging of master order triggers and parallel child execution fan-out.
- **Reconciliation (`/reconciliation`)**: Automated safety checks comparing Master and Child open positions to prevent discrepancies.

---

## 🌐 Application Navigation & Routes

| Route | Page | Description | Status |
| :--- | :--- | :--- | :--- |
| `/` | **Dashboard** | Platform summary, P&L overview, active accounts | Active |
| `/group-strategy` | **Group Strategy** | Manage strategies, set Master account & link Child followers | Active |
| `/demat` | **Demat Accounts** | Add/edit broker Demat accounts and API credentials | Active |
| `/orders` | **Order Manager** | Real-time order fan-out logs & execution statuses | Active |
| `/reconciliation` | **Reconciliation** | Safety position audit check | Active |
| `/followers` | Followers | Legacy route | *Redirects to `/group-strategy`* |
| `/copy-config` | Copy Config | Legacy route | *Redirects to `/group-strategy`* |
| `/watchlist` | Watch List | Legacy route | *Redirects to `/group-strategy`* |
| `/static-ip` | Static IP | Legacy route | *Redirects to `/demat`* |

---

## 🗄️ Database Schema & Alembic Migrations

The database schema is managed via **Alembic** (`backend/alembic/`):

- **`accounts`**: Stores Demat credentials, API tokens, and broker user IDs.
- **`group_strategies`**: Stores strategy metadata, allocated capital, and `master_account_id` foreign key link.
- **`strategy_members`**: Junction table linking child accounts to group strategies with `multiplier` and `trading_enabled` status.
- **`orders` / `copy_trades`**: Stores trade signal payloads, fan-out timestamps, execution status, and audit logs.

### Running Migrations:
Migrations are applied **automatically** every time the engine container starts
(`backend/docker-entrypoint.sh` runs `alembic upgrade head` first), so a fresh database
or an upgrade needs no manual step. To run them by hand:
```bash
docker compose exec fastapi alembic upgrade head
```

---

## ⚡ How to Run the Application

### Option 1: Full Docker Stack (Recommended)

**Fresh install** (new machine, empty database):

```bash
git clone https://github.com/maxrindia/jettone-copy-trading-.git jettone && cd jettone
scripts/bootstrap-env.sh --url http://<server-ip>:8080   # creates .env with fresh secrets
docker compose up -d --build                              # schema migrations run automatically
```

Then log in as `admin` and add your demat accounts in the app. `.env` is never committed;
back it up (losing `ALICE_BLUE_ENCRYPTION_KEY` makes stored broker credentials undecryptable).

On an existing install, run in the project root directory:

```bash
docker compose up -d
```

#### Access Points:
- **Web App**: `http://<server-ip>:8080` (or `http://localhost:8080` locally; `:3005` is a second mapping of the same app)
- **Group Strategy**: `http://<server-ip>:8080/group-strategy`
- **FastAPI**: internal only (Docker network); it is never published on a host port.

### Updating a running server
```bash
scripts/deploy.sh      # git fast-forward, rebuild, restart, health check (migrations run on start)
```
It keeps `.env` and the data volumes. It restarts the trading backend: **orders open at restart are not copied and their later changes are not propagated**, so deploy when no order is open.

### TLS (optional)
A default install is plain HTTP on `:8080` and does **not** start nginx/certbot (nginx cannot start without a
certificate). To use TLS on a server that has its own domain and free ports 80/443:

1. In `.env` set a real `DOMAIN_NAME` and `LETSENCRYPT_EMAIL`; in `docker-compose.yml` publish ports `80:80`/`443:443` on `nginx`.
2. Run `nginx/init-letsencrypt.sh` once (it starts/uses nginx and certbot explicitly, which activates the `tls` profile for that run).
3. Add `COMPOSE_PROFILES=tls` to `.env` so `docker compose up -d` and `scripts/deploy.sh` keep nginx/certbot running.
   `scripts/deploy.sh` refuses to run with `tls` enabled while `DOMAIN_NAME` is still a placeholder.

### Operations notes
- **Live trading is off by default** (`ENGINE_LIVE_TRADING_ENABLED=false`, observe-only). Turn it on in `.env` only when ready.
- The backend **refuses to start** if `.env` still holds `CHANGE_ME` placeholders; generate real secrets with `scripts/bootstrap-env.sh`.
- Alice Blue whitelists calls by IP: register the server's static IP (or each account's Static IP proxy) in the Alice Blue developer portal.
- Back up `.env` (losing `ALICE_BLUE_ENCRYPTION_KEY` makes stored broker credentials undecryptable) and the `jettone_postgres_data` volume.
- Only the frontend is exposed; the backend, Postgres and Redis stay on internal Docker networks.

### Development & tests
```bash
cd backend && pip install -r requirements-dev.txt && pytest -q     # in-memory SQLite, mocked brokers
cd frontend && npm ci && npx tsc --noEmit && npx vitest run && npm run build
```
GitHub Actions (`.github/workflows/ci.yml`) runs the same checks plus Docker image builds on every push.

### Useful Operational Commands:
```bash
# Check container status
docker compose ps

# View live logs
docker compose logs -f

# Rebuild Next.js container after frontend changes
docker compose build nextjs && docker compose up -d nextjs

# Rebuild FastAPI container after backend changes
docker compose build fastapi && docker compose up -d fastapi
```