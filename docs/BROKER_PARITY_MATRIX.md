# Broker feature-parity matrix: Alice Blue vs Groww vs Angel One

Status as of the working tree at `a730b0f` (nothing here is live-verified for Groww or Angel One).
Legend: **Yes** implemented and tested offline; **Gated** implemented but switched off by a flag;
**No** not implemented; **N/A** the broker's API does not offer it (as far as the verified docs say);
**Verify** needs the owner's read-only capture or the broker's official docs before it can be built honestly.
"Evidence" points at the code that decides the cell. Cells marked (not inspected in depth) are a first reading, not a conclusion.

## 1. Account setup and authentication
| Capability | Alice Blue | Groww | Angel One | Evidence / gap |
|---|---|---|---|---|
| Selectable in Add Demat Account | Yes | Gated (`GROWW_ENABLED`) | Gated (`ANGEL_ONE_ENABLED`) | `brokers/registry.py` |
| Encrypted credential storage | Yes (`api_credentials`) | Yes (vault) | Yes (vault) | `broker_credentials`, migration 0012 |
| Login / session | Daily OAuth redirect | Approval-flow token, daily Groww Cloud approval | Server-side client code + PIN + TOTP | per-adapter `login()` |
| Single-flight / throttled login | Partly (existing) | Yes (Redis guard) | Yes (Redis guard) | `brokers/login_guard.py` |
| Corrupt-session handling | Existing | Yes (compare-and-delete) | Yes | `test_session_corruption.py` |
| Never logs secrets | Yes | Yes | Yes | tests + review |
| Startup auto-login | Yes | Deliberately no | Deliberately no | `manager._auto_login_ok` |
| API-approval failure messaging | n/a | Yes (401 mentions daily approval) | n/a | `_vault_login` |
| Remaining | | Live token-response shape (Verify) | Live login response (Verify) | |

## 2. Master account functionality
| Capability | Alice Blue | Groww | Angel One | Evidence / gap |
|---|---|---|---|---|
| May be a strategy Master | Yes | **No** (`as_master=false`) | **No** (`as_master=false`) | `registry.can_be_master` |
| Master order detection | REST order-book polling (+ WebSocket, silent in practice) | Adapter can return `OrderRecord`s, never wired as a Master | Same | `reliability/master_order_poller.py` is generic over `client.get_order_book()` |
| Baseline seed / no replay after restart | Yes | Inherited from the generic poller, untested for these brokers | Same | poller docstring |
| Order-book pagination | n/a | **Verify**: page base undocumented; a full page (100) raises | **Verify** | `groww.get_order_book` |
| Partial fill / cancel / reject / modify events | Yes | Status mapping built, **Verify** on live data | Contract unverified (`ANGEL_CONTRACT.verified=False`) | |
| Master switch preflight | Yes (`get_order_book_raw`) | Alice-only; refuses non-Alice new Master | Same | `strategy_switch.py` |
| WebSocket order feed | Alice only | N/A (polling only) | N/A | |

## 3. Child account and copy trading
| Capability | Alice Blue | Groww | Angel One | Evidence / gap |
|---|---|---|---|---|
| May be a strategy Child | Yes | Gated (`GROWW_CHILD_TRADING_ENABLED`) | **No** while contract unverified | `registry._groww_spec/_angel_spec` |
| Instrument mapping | Alice contract master (NSE+NFO) | Groww master CSV, NSE/BSE equity only | Angel scrip master, NSE/BSE equity only | `*/instruments.py` |
| F&O / derivatives | Yes | **No** (fails closed; needs lot-size validation) | **No** | `contract.SUPPORTED_SEGMENT` |
| Order / product types | MARKET, LIMIT, SL, SL-M; MIS, CNC, NRML | LIMIT, MARKET, SL, SL_M; MIS, CNC, NRML (equity) | Mapped; **Verify** | `build_order_body` |
| Exact Master LIMIT price copied | Yes | Yes (shared pipeline) | Yes (shared pipeline) | pipeline |
| 0.5% buffer only for MARKET-to-LIMIT | Yes | Yes (shared) | Yes (shared) | pipeline |
| Multiplier / fixed quantity | Yes | Yes (shared) | Yes (shared) | `calculator.py` |
| Lot-size validation | **No** (`lot_sizes` defaults to 1) | **Yes (Phase 1)**: quantity must be a whole number of the master CSV's `lot_size`, never rounded (equity lot size is 1, so no change in practice) | Not used (no lot metadata in the Angel store) | `groww.build_order_body`, `test_position_book_ambiguity.py` |
| Tick-size validation | **No** | **No** | **No** | not implemented anywhere |
| Market-session validation | **No** | **No** | **No** | no code found; parity gap shared by all three |
| Funds / margin / risk before placement | Yes | Yes (`get_funds`) | Yes (`get_funds`, contract unverified) | risk_inputs |
| Strategy isolation / role rules | Yes | Yes (shared) | Yes (shared) | |

## 4. Order lifecycle and reconciliation
| Capability | Alice Blue | Groww | Angel One | Evidence / gap |
|---|---|---|---|---|
| Place | Yes | Yes | Yes | |
| Modify | Yes | Yes, only an untouched pending order (docs unclear whether qty is total or remaining) | Yes (product required) | |
| Cancel | Yes | Yes | Yes | |
| Ambiguous placement never resent | **Defect on GitHub**: post-send transport errors are `NETWORK_ERROR` and retried (see `test_alice_blue_send_findings.py`) | Yes | Yes | adapter + `retry.py` |
| Client order reference | No (no order tag sent) | Yes (`order_reference_id`) | No (not offered by `placeOrder`) | |
| Timeout reconciliation | Flags for manual review only (no tag to match) | Automatic via reference map | Manual review | `stale_order_sweeper.py` |
| Unresolved outcome kept unresolved | Yes (shared) | Yes | Yes | `test_order_state_transitions.py` |
| Fill vs submission distinguished | Yes (fill syncer) | Yes | Yes | `follower_fill_syncer.py` |
| Ambiguous cancel/modify | recorded REJECTED | recorded REJECTED | recorded REJECTED | known; display only, not a duplicate-order risk |

## 5. Positions, holdings, margin, SELL safety
| Capability | Alice Blue | Groww | Angel One | Evidence / gap |
|---|---|---|---|---|
| Positions | Yes | Yes (strict parsing) | Yes (contract unverified) | |
| Intraday vs carried vs delivery | Position-book based | `quantity` only; carried field **Verify** | **Verify** | docs/groww-verification.md |
| Holdings (delivery shares) | **No** | **No - BLOCKER**: no holdings endpoint or response schema is documented in this repository, so none is built (an earlier draft of this table said Groww's docs list one; that was not backed by repo evidence) | **No - BLOCKER** (same) | CNC SELL of long-held shares is refused unless the position book shows it |
| Funds / margin | Yes | Yes | Yes | |
| SELL safety (no short, no over-sell) | Yes | Yes | Yes | `test_*_sell_safety.py` |
| Unknown position book blocks SELL | Yes | Yes | Yes | |

## 6. Static IP, rate limits, outages
| Capability | Alice Blue | Groww | Angel One |
|---|---|---|---|
| Static IP | VPS whitelist | Required per account before create/modify/cancel | Required |
| Rate limiting | existing | Documented limits enforced client-side | **Phase 1**: configurable client-side limiter (`ANGEL_ONE_ORDER_RATE_LIMITS` / `ANGEL_ONE_READ_RATE_LIMITS`), **empty by default**: no figure is assumed. BLOCKER: Angel One's documented limits are not in this repository |
| Outage / malformed responses fail closed | Yes | Yes | Yes |

## 7. Features that exist only around Alice Blue (outside the adapter)
| Feature | Where | Groww | Angel One |
|---|---|---|---|
| P&L service (raw order book, raw/detail positions) | `pnl/service.py`, `pnl/broker_data.py` | No | No |
| Strategy "square off" action | `api/strategies.py` | No | No |
| Master switch preflight | `strategy_switch.py` | No | No |
| WebSocket feed | `alice_blue/websocket.py` | N/A | N/A |
| Reconnect / login UI flows | `lib/alice-blue-login.ts`, AccountCard, AccountsTab | Partly (explicit login in modal) | Partly |
| Account metrics, dashboard, orders table labels | `account-metrics.ts`, OverviewTab, OrdersTable | (not inspected in depth; P&L comes from the Alice-only P&L service, so Groww/Angel accounts have no verified P&L) | (same) |
| Strategy page: Master picker and "Add child" list | `app/(app)/group-strategy/[groupId]/page.tsx` | **Phase 2 (frontend)**: offered as Master/Child only when the engine's catalog says so, otherwise shown disabled with the engine's reason | same |
| Add Demat Account: capabilities | `AddAccountModal.tsx` | **Phase 2**: a strip shows setup/login, Master and Child separately for any selectable non-Alice broker | same |
| Holdings, margin, square-off screens | none exist in the frontend today | n/a | n/a |

## 8. Honest summary
* The **Child** side is close to parity for equity on both brokers, behind flags, with stronger duplicate-order safety than Alice Blue.
* The **Master** side is the largest gap. The poller is broker-generic, so the engineering is mostly tests plus wiring, but it cannot be switched on honestly before the captures: order-book paging, status vocabulary for part-fills, and stable order ids are unverified.
* **Not supported by any broker today** (so not an Alice-vs-others gap): lot/tick validation, market-session validation, holdings.
* **Alice-only today**: F&O, P&L service, square-off, Master-switch preflight, WebSocket.
* **Angel One** additionally needs its response contract verified from the official docs or a capture before any Child or Master capability is true.


## 9. Phase 1 (offline) - what changed and what is still blocked

Implemented and tested offline, all default-off or behaviour-preserving for Alice Blue:

| Item | Result |
|---|---|
| Separate capability gates | Credential setup/login (`*_ENABLED`), Child (`GROWW_CHILD_TRADING_ENABLED`, `ANGEL_ONE_CHILD_TRADING_ENABLED`), Master (`GROWW_MASTER_ENABLED`, `ANGEL_ONE_MASTER_ENABLED`) are independent. Child needs its flag **and** a usable contract; Master needs its flag **and** the contract's `master_verified` (False in code, so Master cannot be enabled by configuration alone). `test_capability_gates.py` |
| Master poller with real adapters | `MasterOrderPoller` driven by the real Groww and Angel One adapters on mocked transports: baseline seed, new order, repeated polls, partial then complete, duplicate rows, restart re-baseline, Jettone-originated orders, unreadable/unknown responses, Groww full-page boundary. `test_master_poller_other_brokers.py`. This does **not** make either broker Master-eligible |
| Order-type vocabulary | Groww `SL_M` (was emitted as `SL-M`) and Angel One `STOPLOSS_LIMIT`/`STOPLOSS_MARKET` (was raw) are now normalised to the `SL`/`SLM` vocabulary the Master detector and Alice Blue use. Without this, a Master stop-loss from either broker would have been misclassified |
| Ambiguous position book | Duplicate rows for one instrument and product make the book unknown for Groww and Angel One; SELL stays blocked |
| Groww lot size | Enforced as a whole multiple, never rounded |
| Angel One rate limiter | Configurable, off by default, refuses before sending, never retried; an invalid value blocks every non-login request (fail closed) |

Blockers (not implemented, on purpose):

| Item | Why |
|---|---|
| Groww / Angel One holdings | No documented endpoint or schema in the repository. Needs the official schema supplied (or a reviewed capture) |
| Tick-size validation | The unit of the Groww master's `tick_size` column (rupees vs paise) is not confirmed anywhere in the repository, and Angel One's store carries no tick data. A wrong unit would refuse every order |
| Market-session validation | There is no exchange-holiday/session calendar source in the repository. Fixed weekday hours would wrongly block special sessions and wrongly allow holidays |
| Groww order-list pagination | The page base (0 or 1) is undocumented. A full page of 100 is treated as possibly truncated (the poll is skipped, never trusted); no paging loop is added |
| Groww Master | Needs, from a reviewed capture: page base, stable `groww_order_id` across polls, part-fill `order_status` values. Then set `GrowwContract.master_verified` |
| Angel One Child and Master | Needs its response contract (`status_map`, position keys, RMS keys) verified, then `master_verified` for Master |
| Staleness of position data | Positions are fetched live at the moment of each SELL decision and never cached, so there is no stale copy to guard against |
| Alice Blue duplicate-send defect | Out of scope; see `test_alice_blue_send_findings.py` |

## 10. Broker-specific requirements (as implemented)

| | Alice Blue | Groww | Angel One |
|---|---|---|---|
| Authentication | Daily OAuth web login (token valid up to about 18 h), stored per account in Redis | `POST /v1/token/api/access`, `key_type: approval`, `checksum = SHA-256(secret + timestamp)`; the API key must be approved on Groww Cloud **each day** | Server-side `loginByPassword` with client code, PIN and a locally generated TOTP |
| Session handling | Per-account Redis key `alice:session:{id}`; treated expired if not from today | Encrypted blob in Redis, TTL from the earlier reading of the token expiry (timezone unspecified), capped at 24 h | Encrypted JWT in Redis, expires at IST midnight |
| Credentials | `api_credentials` (encrypted) | `broker_credentials` vault: `api_key`, `api_secret` | Vault: `api_key`, `pin`, `totp_secret` (+ client code on the account) |
| Instrument master | Alice contract master (NSE + NFO) | Groww CSV, URL from `GROWW_INSTRUMENT_MASTER_URL`; NSE/BSE equity only; fails closed when not loaded | Angel scrip master, URL from `ANGEL_ONE_SCRIP_MASTER_URL`; NSE/BSE equity only |
| API version header | n/a | `X-API-VERSION: 1.0` | SmartAPI headers incl. client IPs |
| Order types | MARKET, LIMIT, SL, SL-M | LIMIT, MARKET, SL, SL_M (validity DAY) | NORMAL/LIMIT, MARKET, STOPLOSS/STOPLOSS_LIMIT, STOPLOSS/STOPLOSS_MARKET |
| Products | MIS, CNC, NRML | MIS, CNC, NRML | INTRADAY, DELIVERY, CARRYFORWARD |
| Static IP | VPS whitelist | Required per account for create/modify/cancel | Required per account for orders |
| Client order reference | none | `order_reference_id` (deterministic, 20 alphanumerics) | none |
| Rate limits | existing handling | 5/s + 30/min + 150/day token; 10/s + 250/min orders; 20/s + 500/min reads (documented) | none assumed; configurable only |


## 11. Outstanding API evidence (explicit blockers; nothing here is guessed)

| # | Blocker | Evidence needed | How it can be obtained | Unlocks |
|---|---|---|---|---|
| 1 | Groww holdings endpoint and response schema | Path, parameters, field names and meaning | Official Groww docs supplied to the developer; then a reviewed read-only capture | Counting delivery shares for CNC SELL safety; separate holdings model |
| 2 | Groww pagination base and full-page behaviour | Page parameter name, first page index (0 or 1), max page size, total/next fields, behaviour when a page is full | Official docs plus scenario C of `OWNER_CAPTURE_INSTRUCTIONS.md` | A paging loop; until then a full page (100) is a skipped poll |
| 3 | Groww part-fill status vocabulary and stable order ids | `order_status` of a part-filled and a part-filled-then-cancelled order; `groww_order_id` constant across polls | Official docs plus scenario B | `GrowwContract.master_verified`, so Groww Master can be considered |
| 4 | Groww tick-size unit and reliable instrument metadata | Unit of `tick_size` (rupees/paise), and that `lot_size`/`freeze_quantity` are reliable for the segments used | Official docs for the instruments file; a reviewed sample of the file | Tick-size validation (a wrong unit would refuse every order) |
| 5 | Angel One official rate limits | Documented per-second/minute/day limits per endpoint class | Official SmartAPI docs | Setting `ANGEL_ONE_ORDER_RATE_LIMITS` / `ANGEL_ONE_READ_RATE_LIMITS` (no default exists) |
| 6 | Angel One response contract | Order-status values, position keys, RMS keys | Official docs plus scenarios A and B | `AngelContract.verified`, so Child can be considered |
| 7 | Angel One Master/Child API behaviour | Whether polling the order book detects orders placed elsewhere, part-fill reporting, modify semantics | Official docs plus scenario B | `AngelContract.master_verified` |
| 8 | Exchange session and holiday calendar source | Official hours/holiday list in a form the system can maintain | NSE/BSE circulars; the owner decides on and maintains a source | Market-session validation for all three brokers |

## 12. Alice Blue duplicate-send defect (separate decision; not changed by this work)

Where: `AliceBlueClient.place_order` and `RetryPolicy`. Both are identical to `origin/main` and untouched.

- A transport error after the request left (`ReadError`, `RemoteProtocolError`, `WriteError`) is classified as
  `NETWORK_ERROR`, the one result the retry policy re-sends.
- A 200 response with an unparsable body makes `place_order` raise, and the retry policy also retries exceptions.
- Risk: if the broker accepted the first request, the retry places the same order again (up to 3 transmissions in
  the mocked reproduction): a duplicate position on a live account. Pinned by `tests/test_alice_blue_send_findings.py`.
- Any future fix must separate a definitive rejection (safe to report) from an ambiguous submission (never
  blindly retried: look the order up and reconcile first, as the Groww and Angel One adapters already do), and
  must not weaken duplicate protection to make tests pass.
- **Status: waiting for separate approval.** No change is made in this phase.
