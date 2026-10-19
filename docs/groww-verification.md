# Groww: Child support and verification runbook

Status: **disabled** (`GROWW_ENABLED=false`). Groww can only be a **Child**; it can never be a
strategy Master (its orders cannot be detected). Nothing here has been run against a real Groww
account. Source of every field below: Groww's official Trading API documentation
(groww.in/trade-api/docs - cURL orders / portfolio / margin / annexures / instruments, and the
Python SDK pages), read 2026-10-16. The same table lives in `backend/app/services/groww/contract.py`.

## Verified from the documentation (and used)

| Area | Used as documented |
|---|---|
| Base / headers | `https://api.groww.in/v1/...`; `Accept: application/json`, `Authorization: Bearer <token>`, `X-API-VERSION: 1.0` |
| Session | `POST /v1/token/api/access`, `key_type: "approval"`, `checksum = SHA-256(secret + timestamp)`, `timestamp` = epoch seconds; response `token`, `expiry`, `isActive`. The API key must be **approved on Groww Cloud each day** first |
| Orders | `POST /v1/order/create`, `/modify`, `/cancel`; `GET /v1/order/list`, `/detail/{id}`, `/status/reference/{ref}`; fields `trading_symbol, quantity, price, trigger_price, validity, exchange, segment, product, order_type, transaction_type, order_reference_id` |
| Vocabularies | exchange NSE/BSE; segment CASH/FNO; product CNC/MIS/NRML; order type LIMIT/MARKET/SL/SL_M; BUY/SELL; validity DAY; the 12 order statuses |
| Positions | `GET /v1/positions/user?segment=CASH` → `payload.positions[]` (`trading_symbol, exchange, segment, product, quantity ("Net quantity"), net_price, symbol_isin, ...`) |
| Funds | `GET /v1/margins/detail/user` → `payload.clear_cash, net_margin_used, collateral_available` |
| Instruments | CSV master (`exchange, trading_symbol, instrument_type, segment, isin, lot_size, freeze_quantity, buy_allowed, sell_allowed, ...`); `trading_symbol` is the order identifier |
| Limits | auth 5/s, 30/min, 150/day; orders 10/s, 250/min; reads 20/s, 500/min |

## Implemented behaviour

- Equity only (CASH, NSE/BSE). F&O, NRML and non-EQ instruments are refused.
- Instrument resolved from Groww's own master by exchange + symbol (Alice's `-EQ` suffix stripped). The
  Master's instrument id is **never** sent. A symbol Groww does not allow for that side, or a quantity above the
  freeze quantity, is refused (never split).
- A recorded, unexpired static IP is required before every create / modify / cancel.
- `order_reference_id` is derived deterministically from Jettone's order id. After a timeout / network error /
  5xx the order is looked up by that reference; it is **never resent**.
- Modify only for an untouched, pending order (the docs do not say whether `quantity` is total or remaining).
- Status normalisation: `EXECUTED`/`COMPLETED`/`DELIVERY_AWAITED` count as a fill only when `filled_quantity`
  agrees; any unknown status, missing quantity or contradiction raises (fail closed).
- Sessions are created only by an explicit `POST /accounts/{id}/broker-login`, never by the engine's startup.
- Credentials: encrypted vault (`broker_credentials`, `api_key` + `api_secret`), write-only, never in `notes`.
- **Duplicate-order safety.** The pipeline retries only `NETWORK_ERROR` results and any raised exception, so a create
  is re-sent only when the error *proves* it never left (`ConnectError`, `ConnectTimeout`, `PoolTimeout`, `ProxyError`,
  `UnsupportedProtocol`, `LocalProtocolError`). Read/write failures, protocol errors, 5xx, an unreadable or
  order-id-less response and any unexpected error after sending are an ambiguous `TIMEOUT`: one read-only lookup by our
  reference, then never a resend. `place_order` never raises. The reference -> Jettone order id mapping is written to
  Redis *before* sending and applied to `get_order_book()`, which is what lets the stale-order sweeper match a timed-out
  order. (Angel One has no client reference in `placeOrder`, so an ambiguous Angel One order is manual review.)
- **Corrupt sessions** (undecryptable, wrong key, bad JSON, no token) give a sanitized auth error with no broker
  request, and discard only that account's exact unreadable key. A missing encryption key is reported as such and
  discards nothing.
- **Strict numbers.** Missing, null, blank, boolean, NaN, Infinity, overflowing, negative-where-invalid or
  non-integral values refuse the response; a fill cannot exceed its order; a filled order needs a positive average
  price; margin and the modify guard are as strict as positions.
- **Login protection** is in Redis, so it holds across client instances, concurrent requests and restarts: one login
  in flight per account (30 s lock; others share its result), 10 s spacing, a Groww daily cap of 120, fail-closed if
  Redis is down. A throttled request is HTTP 429 with `Retry-After` (the Next.js proxy forwards the status and
  message, not that header).
- An account's broker cannot be changed after creation (409).

## NOT documented - Jettone fails closed on each

1. The **FAILURE response body / error codes** (only `status: "SUCCESS" | "FAILURE"` is documented).
2. The **TOTP-flow token response** (so only the approval flow is implemented).
3. The shape of an **empty** positions / order list. An unexpected shape is treated as "unknown", never "flat" -
   which means a genuinely flat account might be treated as unreadable until the capture confirms the shape.
4. The **paging base** of the order list: a full first page (100) is treated as possibly truncated.
5. Whether `quantity` or `net_carry_forward_quantity` is the sellable long for carried positions; only `quantity`
   ("Net quantity") is used. **CNC holdings** (delivery shares from the holdings endpoint) are not counted, so a
   SELL of long-held delivery shares is rejected unless the position book shows it.
6. Static-IP enforcement on **reads**, IPv4/IPv6, and change limits (Groww's own blog only says order placement
   needs a whitelisted static IP; up to a primary + secondary IP).
7. Timezone of timestamps and token expiry (the earlier reading is used for the session lifetime).
8. `order_reference_id` duplicate behaviour on Groww's side.
9. **Timeout reconciliation (fixed, shared with Alice Blue).** `CopyTrade.submitted_at` is now written immediately
   before every LIMIT / SL / MARKET send, and the stale-order sweeper selects TIMEOUT trades on
   `coalesce(submitted_at, completed_at, updated_at)` so older rows with a NULL stamp are still reconciled. An
   ambiguous LIMIT / SL send is no longer recorded as a REJECTED order: the CopyTrade is TIMEOUT and the child
   Order stays PENDING (`broker_status` TIMEOUT, no broker id) until the sweeper reads the broker order book (Groww
   via the reference map; Angel One cannot be matched and stays a manual review). Nothing is ever resent. The MARKET
   path behaves the same way, and an `UNKNOWN` result (RetryPolicy: `place_order` raised) is treated as unresolved too,
   with a fixed sanitized reason instead of the raw exception text.

## Safe next step (needs the account owner)

1. Registered static IP for the Groww account (Groww > API Keys > "Add static IP"); record the same IP on the
   account in Jettone.
2. **Approve the API key on Groww Cloud today**, then in PowerShell from `backend\`
   (throwaway encryption key, never the production one; see `docs/angel-one-verification.md` for the same pattern):

       $env:ALICE_BLUE_ENCRYPTION_KEY = (..\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
       $env:GROWW_API_KEY    = Read-Host "API key"
       $env:GROWW_API_SECRET = Read-Host "API secret"
       ..\.venv\Scripts\python.exe scripts\groww_capture.py                      # dry run, contacts nothing
       ..\.venv\Scripts\python.exe scripts\groww_capture.py --execute --i-am-authorized --out groww_capture.json
       Remove-Item Env:GROWW_*, Env:ALICE_BLUE_ENCRYPTION_KEY

   It spends one token request and makes three GETs (order list, positions, margin). Output is shapes plus the
   status/product vocabulary only; `groww_capture*.json` is git-ignored.
3. Run it twice: once on a **flat account with no orders** (captures the empty shapes), once with a position and a
   few orders in different states placed manually in the Groww app.
4. Review together. Anything that differs from the table above becomes a code change before enabling.
5. Only then: set `GROWW_INSTRUMENT_MASTER_URL`, `GROWW_ENABLED=true` on a non-production stack first, add the
   account, store credentials, log in, and verify with Live Trading OFF.

## Deploying this change

It adds no migration (the `broker_credentials` table already exists from 0012). With `GROWW_ENABLED` absent or
false, Groww accounts cannot be added, logged in or copied to.
