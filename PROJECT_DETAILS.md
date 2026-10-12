# 🚀 Jettone — Real-Time Alice Blue Multi-Account Copy Trading Platform
## Complete Technical Architecture, Operational Guide & System Specifications

---

## 📌 1. Executive Summary

**Jettone** is an ultra-low-latency, containerized multi-account copy trading terminal engineered specifically for Indian equities, derivatives, and commodity trading on **Alice Blue** (ANT / A3 API).

The platform bridges a central **Master Trading Account** to multiple **Follower / Child Accounts**, replicating order executions (Buy, Sell, Modify, Cancel) in real time with custom quantity multipliers and sub-50ms execution latency.

---

## 🏗️ 2. High-Level Architecture & 5-Service Container Stack

The entire system is orchestrated via **Docker Compose** across 5 isolated, high-performance services:

```
                                  [ Browser / Client ]
                                           │
                                  ( Port 8080 / HTTP )
                                           ▼
                              ┌────────────────────────┐
                              │     Nginx (Proxy)      │
                              └───────────┬────────────┘
                                          │
                  ┌───────────────────────┴───────────────────────┐
                  ▼                                               ▼
     ┌─────────────────────────┐                     ┌─────────────────────────┐
     │   Next.js 14 Frontend   │                     │     FastAPI Engine      │
     │     (React / SSR)       │                     │    (Python 3.12 Async)  │
     └─────────────────────────┘                     └────────────┬────────────┘
                                                                  │
                                  ┌───────────────────────────────┴───────────────────────────────┐
                                  ▼                                                               ▼
                     ┌─────────────────────────┐                                     ┌─────────────────────────┐
                     │   PostgreSQL Database   │                                     │       Redis Cache       │
                     │  (Accounts / Trades)    │                                     │  (Tokens / Realtime)    │
                     └─────────────────────────┘                                     └─────────────────────────┘
```

### Service Breakdown:

| Service Name | Technology | Internal Port | Description |
| :--- | :--- | :--- | :--- |
| **`copytrade_nginx`** | Nginx Alpine | `80` (Host: `8080`) | Reverse proxy routing web traffic to Next.js and `/api/v1` traffic to FastAPI. |
| **`copytrade_nextjs`** | Next.js 14 (React 18 / TypeScript) | `3000` | User interface for Account Management, Strategy Builder, Order Manager, and Metrics. |
| **`copytrade_fastapi`** | Python 3.12 + FastAPI + AsyncIO | `8000` | Asynchronous trading engine, WebSocket listeners, order replication, and risk checks. |
| **`copytrade_postgres`**| PostgreSQL 16 Alpine | `5432` | Relational storage for accounts, encrypted API secrets, group strategies, copy trades, and audit logs. |
| **`copytrade_redis`** | Redis 7 Alpine | `6379` | In-memory token store, real-time rate limiting, and order-event distribution. |

---

## ⚙️ 3. Core Copy Trading Execution Flow

```
[ Master Account ] ──( Places Order )──► [ Alice Blue Exchange ]
                                                    │
                                         ( WebSocket Feed )
                                                    ▼
                                         ┌─────────────────────┐
                                         │  FastAPI WS Stream  │
                                         └──────────┬──────────┘
                                                    ▼
                                         ┌─────────────────────┐
                                         │  Group Strategy &   │
                                         │  Multiplier Engine  │
                                         └──────────┬──────────┘
                                                    ▼
                        ┌───────────────────────────┴───────────────────────────┐
                        ▼                                                       ▼
            [ Follower 1 (Qty x 1.0) ]                             [ Follower 2 (Qty x 2.0) ]
```

1. **Trade Signal Ingestion**:
   - The FastAPI engine maintains a persistent WebSocket connection (`wss://ws1.aliceblueonline.com/NorenWS/`) for the Master account.
   - When the Master places, modifies, or cancels an order on Alice Blue (via web, mobile, or API), an `OrderUpdateEvent` is streamed to the engine.
2. **Strategy Matching & Fan-Out**:
   - The engine checks active **Group Strategies** where the Master account is designated as the signal leader.
   - It identifies all linked **Child Members** where `trading_enabled = True`.
3. **Quantity Calculation & Risk Validation**:
   - Computes follower quantities using defined multipliers (e.g., Master 50 qty * 1.00x = 50 qty).
   - Validates available margin and risk limits before dispatching.
4. **Parallel Order Placement**:
   - Submits parallel REST API orders (`/placeOrder/executePlaceOrder`) to Alice Blue for all followers asynchronously.
5. **Database Audit Recording**:
   - Writes the completed execution record into the `copy_trades` table with execution latency (ms), broker order IDs, and fill prices.

---

## 🔐 4. Alice Blue Authentication & Token Isolation

Alice Blue ANT / A3 API uses daily OAuth Web Login with single-day tokens valid for up to 18 hours.

### Authentication Safeguards:
1. **Per-Account Token Isolation**:
   - Tokens are stored strictly in Redis under individual keys: `alice:session:{account_id}`.
   - Tokens are never shared or cross-assigned between accounts.
2. **Account Mismatch Detection**:
   - When completing Web OAuth login (`/platform/dematconnected`), the system validates that the user ID authenticated by Alice Blue matches the target account being re-logged.
   - Prevents browser cookie sharing from polluting follower sessions.
3. **Strict Daily Expiration**:
   - Accounts require fresh authentication each trading day (`auth_dt.date() == today()`).
   - If a session is from a previous calendar day, the system automatically marks it `🔴 Offline`.
4. **WebSocket Policy Violation Handling**:
   - If Alice Blue rejects a token (`1008 Policy Violation` / `401 Unauthorized`), the engine immediately invalidates the database session and updates the UI status to `Offline`.

---

## 🖥️ 5. Application Modules & Page Directory

### 1. **Demat Accounts (`/demat`)**
- Register broker accounts (Nickname, Alice Blue User ID, API Key, API Secret, Mobile, Email).
- Real-time connection status badges: `🟢 Live (35ms)` / `🔴 Offline`.
- Interactive **Re-login** button triggering Alice Blue Web OAuth popup.
- 3-Dot menu for editing credentials (`Reconnect Demat Account`) or deleting accounts.

### 2. **Group Strategy (`/group-strategy`)**
- Strategy configuration builder:
  - Select Master Account.
  - Link 1 or more Follower Accounts.
  - Set custom Lot Multipliers (e.g., `1.00x`, `2.00x`, `0.50x`).
  - Independent `Trading Status` toggle per member.
- Master `Trading Enabled` master switch.

### 3. **Order Manager (`/order-manager` or `/orders`)**
- Live tabular view of all executed trades.
- Master order vs. Follower copy trade execution side-by-side.
- Statuses: `COMPLETE`, `PENDING`, `REJECTED`, `CANCELLED`.
- Latency tracking display in milliseconds (ms).

### 4. **Dashboard (`/dashboard`)**
- Real-time P&L analytics, total daily orders, fill rates, and active positions summary.

### 5. **Reconciliation & Safety (`/reconciliation`)**
- Daily audit tool comparing broker positions with engine database records.
- **Emergency Stop (Kill Switch)**: Instantly halts all copy trading activity platform-wide.

---

## 🗄️ 6. Database Models & Schema

- **`Account`** (`accounts`):
  - `id`: UUID Primary Key
  - `display_name`: Account Nickname (e.g., "Dhinesh", "Vikash")
  - `alice_user_id`: Broker Client ID (e.g., `2679246`)
  - `account_type`: `MASTER` | `FOLLOWER`
  - `session_valid`: Boolean session health flag
  - `last_authenticated_at`: Timestamp of latest OAuth verification
  - `is_active`: Global active flag
- **`ApiCredential`** (`api_credentials`):
  - `api_key`: App Code
  - `encrypted_api_secret`: AES-256 encrypted broker secret
- **`GroupStrategy`** (`group_strategies`):
  - `name`: Strategy name
  - `master_account_id`: Foreign Key link to Master `Account`
  - `is_active`: Strategy toggle
- **`StrategyMember`** (`strategy_members`):
  - `strategy_id`: Foreign Key link to `GroupStrategy`
  - `follower_account_id`: Foreign Key link to Follower `Account`
  - `multiplier`: Decimal quantity multiplier (e.g., `1.00`)
  - `is_active`: Member trade toggle
- **`CopyTrade`** (`copy_trades`):
  - `master_order_id`, `follower_account_id`, `follower_order_id`, `symbol`, `side`, `quantity`, `price`, `status`, `execution_latency_ms`, `created_at`.

---

## 🚀 7. Operational Runbook & Server Commands

### Starting the Platform (VPS / Server):
```bash
# Pull latest updates
git pull origin main

# Build and start all 5 containers
docker compose up -d --build

# Check container health status
docker compose ps
```

### Viewing Logs:
```bash
# Live FastAPI engine logs
docker compose logs -f fastapi

# Live Next.js frontend logs
docker compose logs -f nextjs

# Live WebSocket & trade activity
docker compose logs -f fastapi | grep -i -E "ws|order|trade|callback"
```

### Resetting Sessions (Morning Preparation):
```bash
docker compose exec fastapi python -c "
import asyncio
from app.core.database import AsyncSessionLocal
from app.models.accounts import Account
from app.core.redis import get_redis
from sqlalchemy import select

async def reset():
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(Account))
        for a in res.scalars():
            a.session_valid = False
            a.last_authenticated_at = None
        await db.commit()
    r = await get_redis()
    if r:
        keys = await r.keys('alice:session:*')
        if keys: await r.delete(*keys)
    print('All sessions reset to offline.')

asyncio.run(reset())
"
```

---

## 🛡️ 8. Security Best Practices

1. **Zero Secret Leakage**: API secrets are encrypted using AES-256 (`app/core/crypto.py`) before writing to disk.
2. **Strict IP Binding on Production**: Host firewall rules isolate PostgreSQL (`5432`) and Redis (`6379`) to Docker internal network only.
3. **Idempotency & Replay Protection**: Trade events contain unique signal hashes to prevent duplicate order placement during network retries.

---

*Documentation Version 2.4.0 — Maintained for Jettone Copy Trading Platform.*
