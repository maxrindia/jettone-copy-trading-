# Angel One: verification runbook (Child support)

Status: **disabled** (`ANGEL_ONE_ENABLED=false`). Even when enabled, Angel One cannot
receive copied orders until `backend/app/services/angel_one/contract.py` is filled in
from a supervised capture. Master (order detection) is not implemented.

## What is already built and tested (offline)

- Registry/flag gating, encrypted credential vault (`broker_credentials`, migration 0012),
  server-side login (client code + PIN + TOTP), daily session expiry, single-flight re-login.
- Order place/modify/cancel using SDK-verified fields; per-account static IP (and its expiry)
  required before any order request; another broker's instrument token is never sent.
- Broker-neutral routing: Child eligibility (`can_be_child`), instrument resolution,
  order label (`orders.broker = ANGEL_ONE`), session keys.
- SELL safety with `SELL_POSITION_SAFETY_BYPASS=false`: exit copied only against a
  broker-confirmed long; shorts, wrong product/instrument, and missing/malformed data rejected.

## What is NOT verified (the contract)

Angel One's public docs and official SDKs do not state: the order-status values, the
position-book keys, the RMS (funds) keys. Nothing guesses them. Until they are confirmed,
every read that safety depends on raises, so the account is never copied to.

## Safe next step (needs the account owner)

1. Pick the **authorized** Angel One account; confirm its own static IP is registered with Angel One.
2. Run from the machine/proxy that owns that IP, in a **PowerShell** terminal (VS Code:
   Terminal > New Terminal), using the repo's `.venv`. Do not run it on the VPS or inside the
   production containers, and never touch the old `/opt/jettone` stack.
   Commands below are for the capture (no orders, no writes); the dry run and import check
   contact nothing.

   **2a. Local import check (no network, no credentials):**

       cd C:\Users\glomi\Music\Project\jettone\backend
       ..\.venv\Scripts\python.exe -c "import app.services.angel_one.client; print('import ok')"

   Expected output: the single line `import ok`. A traceback means a dependency or settings
   problem on this machine; fix it before going further. (The dry run below does NOT test
   imports, because the script imports them only when it actually connects.)

   **2b. Temporary encryption key.** The client encrypts its in-memory session with
   `ALICE_BLUE_ENCRYPTION_KEY`, so the variable must be set. Use a **throwaway** key generated
   for this one terminal; never the production key. Nothing is persisted, so the key can be
   discarded afterwards:

       $env:ALICE_BLUE_ENCRYPTION_KEY = (..\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

   Environment variables set in the terminal override any `.env` file, so the throwaway key
   wins even if a `.env` exists locally. Do not paste the production `ALICE_BLUE_ENCRYPTION_KEY`.

   **2c. Credentials, typed at prompts** (answers typed into `Read-Host` are not saved in the
   PowerShell command history, unlike values written inside a command):

       $env:ANGEL_CLIENT_CODE = Read-Host "Client code"
       $env:ANGEL_API_KEY     = Read-Host "SmartAPI key"
       $env:ANGEL_PIN         = Read-Host "PIN"
       $env:ANGEL_TOTP_SECRET = Read-Host "TOTP secret (base32 seed, not the 6-digit code)"
       $env:ANGEL_STATIC_IP   = Read-Host "Registered public IP of this account"
       $env:ANGEL_PROXY_URL   = Read-Host "Proxy URL for that IP (leave blank if none)"

   **2d. Dry run (contacts nothing):**

       ..\.venv\Scripts\python.exe scripts\angel_capture.py

   Expected: `Plan: log in once, then GET order_book, positions, rms. No orders. No writes.`
   then `Dry run - nothing contacted. Environment looks complete.` If a variable is missing it
   is named in the message.

   **2e. The capture itself - only after the account owner has said go:**

       ..\.venv\Scripts\python.exe scripts\angel_capture.py --execute --i-am-authorized --out angel_capture.json

   It logs in once and issues three GETs (order book, positions, RMS), then logs out. The output
   keeps only response shapes and a small enum vocabulary; amounts, ids, names and tokens are
   redacted. If login fails, stop and investigate - repeated wrong PIN/TOTP attempts can lock the
   account.

   **2f. Clean up the terminal:**

       Remove-Item Env:ANGEL_*, Env:ALICE_BLUE_ENCRYPTION_KEY

   The result is `backend\angel_capture.json` (relative to where you ran it). It is **git-ignored**
   (`angel_capture*.json` in `.gitignore`); confirm with
   `git check-ignore -v backend/angel_capture.json`. Even though it is redacted, do not share or
   commit it without reading it first.
3. For useful order-status vocabulary, the account should have a few orders today in different
   states (placed manually in Angel One's own app - not by Jettone): open, filled, cancelled, rejected.
   For positions: one long, one short, ideally one delivery holding.
4. Review `angel_capture.json` together, then fill `ANGEL_CONTRACT` (status map, `PositionKeys`,
   `RmsKeys`) from it and re-run the backend suite. A partial contract still refuses.
5. Obtain Angel One's instrument master from its documented URL, set `ANGEL_ONE_SCRIP_MASTER_URL`
   (or load a downloaded file), and confirm equity symbol spelling (`SBIN-EQ`) against what Master
   events carry.
6. Still before any live copy: confirm the product pairing (CNC→DELIVERY, MIS→INTRADAY,
   NRML→CARRYFORWARD) and that the proxy egress IP equals the recorded static IP.

## Migration 0012 (not applied by this change)

Additive: creates `broker_credentials` only. Standard deploy backup first (`pg_dump` +
`pg_restore -l`), then `alembic upgrade head`. Rollback: `alembic downgrade
0011_account_static_ip_address` (loses only Angel One credentials, re-entered in the UI).

## Known limits when it is eventually enabled

- Child only. An Angel One account cannot be a strategy Master.
- Equity (NSE/BSE) only. F&O is rejected (no lot-size data).
- Master LIMIT modifications are not propagated to an Angel One Child (the pipeline does not
  pass the product, and the adapter refuses to default it); cancels are.
- Order status sync (`FollowerFillSyncer`) depends on the verified status map.
