# Owner-run, read-only broker captures

Purpose: collect the facts Jettone cannot safely assume about Groww and Angel One (see "Outstanding API
evidence" in `BROKER_PARITY_MATRIX.md`). Nothing in Jettone's code or in any assistant session runs these for
you. **Do not run anything below until you have explicitly decided to, for your own account.**

## 0. Authorization (required before every run)

Write this down (a note, a ticket, a chat message to yourself) before you start. If you cannot truthfully write
all of it, stop.

- [ ] This is **my own** broker account and I am entitled to use its API.
- [ ] I will run the script myself, from the machine that owns the account's registered static IP (not the VPS,
      not inside production containers; never touch `/opt/jettone`).
- [ ] I understand it creates a **login session** (details in section 2) and, for Angel One, sends a **logout**.
- [ ] It places, modifies and cancels **no** orders. Any orders/positions needed for scenario B I will create
      myself in the broker's own app, with amounts I am happy to risk.
- [ ] I will review the output file before sharing it (section 4).
- [ ] Live Trading in Jettone stays OFF throughout.

## 1. Read the official documentation first

Use the broker's own current documentation, not memory, forum posts or third-party SDKs. Write the answers down
next to the capture; they decide how the capture is interpreted and which script extensions are needed.

| Open question | Look for in the official docs |
|---|---|
| Groww holdings | Is there a holdings endpoint? Its path, parameters and the exact response fields (quantity vs. T+1 vs. pledged) |
| Groww order-list paging | The name of the page parameter, whether the first page is `0` or `1`, the maximum page size, what the response says about the total/next page, and what happens when a page is full |
| Groww part-fill status | The exact `order_status` value(s) of a part-filled order and of one that is part-filled then cancelled; whether `groww_order_id` is stable for the life of an order |
| Groww tick size | The unit of the `tick_size` column in the instruments file (rupees or paise) and any rule for price steps |
| Angel One rate limits | Per-second / per-minute / per-day limits for order placement, order book, positions, RMS and login |
| Angel One contract | The order-book `status` values, the position-book field names, the RMS (funds) field names, how a part-fill and a cancelled-after-part-fill are reported, and whether Master order detection by polling the order book is supported |
| Exchange sessions | The exchange's official trading hours and holiday calendar (NSE/BSE circulars) and a machine-readable source you are willing to maintain |

## 2. What the two scripts do (exactly)

Both scripts are dry runs unless you pass `--execute --i-am-authorized`. The dry run prints the plan and contacts
nothing.

| | `backend/scripts/groww_capture.py` | `backend/scripts/angel_capture.py` |
|---|---|---|
| Session creation | 1 x `POST /v1/token/api/access` (counts toward Groww's 150 token requests/day; needs the API key approved on Groww Cloud **that day**) | 1 x `POST .../loginByPassword` (client code + PIN + a TOTP generated locally; repeated wrong TOTPs can lock the account) |
| Reads (all GET) | order list (`segment=CASH`, `page_size=100`), positions (`segment=CASH`), margin | order book, positions, RMS (funds) |
| Session end | **None.** Groww documents no logout; the script only forgets the token locally and it expires on its own | 1 x `POST .../logout` (ends the session; this is a state change, but not an order) |
| Orders | none | none |
| Writes on your machine | one JSON file (git-ignored), shapes only | one JSON file (git-ignored), shapes only |

Throttle protection: the scripts use the same login guard as the app (one login in flight, 10 s spacing between
attempts; Groww also has a daily attempt cap), on an in-process store that is discarded when the script ends.

**Not covered by the scripts, on purpose:** the Groww holdings endpoint, a Groww paging parameter, the instruments
file, and anything that needs a request name Jettone does not yet know. They will be added only after section 1
supplies the official names, and each addition will be reviewed before it is run. Nothing is guessed.

## 3. Scenarios (run each only if it is safe and you are authorized)

- **A. Empty account.** An account with no positions and no orders today. Records the shape of an empty order
  list, an empty positions list and the margin response. This is the undocumented "nothing there" shape.
- **B. Populated account.** After you have created them yourself in the broker's app: an open position, a
  closed (flat) position, and several orders in different states: open, completed, rejected, cancelled and,
  if you can arrange it, **partially filled** (a limit order for a thinly traded stock, or a larger quantity).
  Records the status vocabulary and field names.
- **C. Full page (only if it genuinely happens).** If the account has at least a full page of orders in a day
  (Groww: 100), the capture records the true `row_count` and any paging fields. Do **not** create 100 orders to
  force this; if it does not occur naturally, the full-page behaviour stays an open blocker.

## 4. Running it (Windows PowerShell, from `backend\`)

Use a **throwaway** encryption key, never the production one. The script needs its settings import to succeed;
nothing is stored.

    cd backend
    $env:ALICE_BLUE_ENCRYPTION_KEY = (..\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

Groww:

    $env:GROWW_API_KEY    = Read-Host "API key"
    $env:GROWW_API_SECRET = Read-Host "API secret"
    ..\.venv\Scripts\python.exe scripts\groww_capture.py                                   # dry run: contacts nothing
    ..\.venv\Scripts\python.exe scripts\groww_capture.py --execute --i-am-authorized --out groww_capture_empty.json
    Remove-Item Env:GROWW_*

Angel One:

    $env:ANGEL_CLIENT_CODE = Read-Host "Client code"
    $env:ANGEL_API_KEY     = Read-Host "API key"
    $env:ANGEL_PIN         = Read-Host "PIN"
    $env:ANGEL_TOTP_SECRET = Read-Host "TOTP secret"
    $env:ANGEL_STATIC_IP   = "<the registered static IP>"
    ..\.venv\Scripts\python.exe scripts\angel_capture.py                                   # dry run
    ..\.venv\Scripts\python.exe scripts\angel_capture.py --execute --i-am-authorized --out angel_capture_empty.json
    Remove-Item Env:ANGEL_*

Finally: `Remove-Item Env:ALICE_BLUE_ENCRYPTION_KEY`. Name the output files per scenario (`..._empty.json`,
`..._populated.json`). They match the git-ignore patterns `groww_capture*.json` / `angel_capture*.json`.

## 5. What is recorded, and the review before you share

Recorded per endpoint: HTTP status, top-level keys, whether the data sits under `data` or `payload`, for every
list its **true row count** and, for the first 50 rows, each field's name and **type**; the values of a small
allow-list of enum-like fields (order status, product, order type, side, exchange, segment, validity); and
integer/boolean paging counters (`page`, `page_size`, `total`, `has_next`, ...). Nothing else: no quantities,
prices, ids, symbols, names, tokens, account numbers, remarks or timestamps.

Before sharing, open the file and check:

- [ ] No token, key, secret, client code, name, e-mail, phone, PAN, order id or symbol appears.
- [ ] Only the expected endpoints are present (Groww: `order_list`, `positions`, `margin`; Angel One:
      `order_book`, `positions`, `rms`).
- [ ] `row_count`, `observed_vocabulary` and `paging_values` are present where you expect them.

If anything sensitive is present, delete the file and tell the developer; do not share it.

## 6. After the capture

The developer compares it with the official documentation you read in section 1. Only then can the response
contracts be filled in (`angel_one/contract.py`, `groww/contract.py`) and `master_verified` considered. Even then
nothing is switched on automatically: each capability has its own default-off flag and needs its own approval.

## 7. Stop conditions

Stop immediately and do not retry if: the login is refused or asks for a new approval (do not loop), you are
rate-limited (HTTP 429), the script prints anything other than its plan, or you are unsure the account is yours.
