# 08 · Order-book read contract (Master detection, Phase 1)

Status: **implemented for Alice Blue (VERIFIED_LIVE path, behaviour unchanged) and migrated into the poller, the stale-order sweeper, the
Master-switch preflight and crash recovery; Groww and Angel One read through the same contract offline only; no new broker is eligible as a Master; the broker-pair allowlist is unchanged
(`alicebluenew → alicebluenew` only).**

## 1. What problem it solves

02 §Interface evaluation requires `MasterOrderSource` to return **Unknown, not empty, on a failed read**. The R1 Protocol
(`brokers/interfaces.py`) only declares `get_order_book()`, and the clients disagree on what `[]` means:

| Client | `get_order_book()` on failure | Proof that the book was read |
|---|---|---|
| Alice Blue | returns `[]` (network error, non-200) | `get_order_book_raw()` returns a list (None on any failure) |
| Groww | raises (`GrowwCapabilityUnavailable`: no `order_list`, truncated page, unknown status) | a returned list |
| Angel One | raises (`AngelOneError` on a failed status flag; unknown statuses rejected by the status map) | a returned list |

The R1 Protocol is **kept** as the low-level client capability; it is not duplicated. The contract is an adapter over it.

## 2. The contract (`backend/app/services/brokers/order_book_read.py`)

One result type, `OrderBookRead(state, records, reason, detail, checked, rows)`, and three readers. None of them raises.

| Reader | Used by | Broker calls | Returns |
|---|---|---|---|
| `read_order_book(client, *, checked)` | `MasterOrderPoller` | `checked` seed pass: raw probe + `get_order_book()` (2 for an Alice client); later polls: `get_order_book()` (1) | parsed `records` |
| `read_order_book_rows(client)` | Master-switch preflight, crash recovery | exactly 1 (`get_order_book_raw`) | the raw broker `rows`, verbatim |
| `read_order_book_single(client)` | stale-order sweeper provider | exactly 1 for a client with `get_order_book_raw` **and** `parse_order_book_rows` (the real Alice client); anything else takes the `read_order_book(checked=True)` path unchanged | `rows` **and** `records` from the same response |

| State | Meaning | Reason codes |
|---|---|---|
| `ORDERS` | the book was read and has rows | `READ_OK` |
| `EMPTY` | the book was read and is genuinely empty | `READ_OK` |
| `UNKNOWN` | the state of the book cannot be established | `NO_CLIENT`, `RAW_READ_NOT_SUPPORTED`, `RAW_READ_UNAVAILABLE`, `RAW_NOT_A_LIST`, `RAW_AND_PARSED_DISAGREE`, `EMPTY_NOT_PROVEN` |
| `FAILED` | the read (or the parse) raised | `READ_RAISED` (`detail` = exception type name only; broker messages are never copied) |

`is_known` is true only for `ORDERS` and a proven `EMPTY`. **UNKNOWN and FAILED are never "no orders", and they never carry rows.**
A parse failure in the single read is `FAILED`, never a shortened book. The convention is inferred as it always was: a client with a
callable `get_order_book_raw` follows the Alice convention; a raw probe whose result is not awaitable (bare `MagicMock` clients) is
ignored by `read_order_book`/`read_order_book_single` and is `UNKNOWN / RAW_READ_UNAVAILABLE` for `read_order_book_rows`.

The pure parser is `alice_blue/order_book.py::parse_order_book_rows`, moved verbatim out of `AliceBlueClient.get_order_book`
(the client re-exports `order_book_row_id` and exposes the parser as `AliceBlueClient.parse_order_book_rows`). It is pinned
against a verbatim copy of the old loop and wrapper (`tests/test_alice_order_book_parser.py`), including the behaviours that look
odd and are preserved on purpose: `get_order_book` returns `[]` for a failed read, a missing `result` and a non-list `result`; a
non-dict row or a non-numeric quantity/price raises; an unrecognised status passes through upper-cased.

The module makes no eligibility decision and imports no client or registry. Eligibility stays with `registry.can_be_master`,
the capability profile (`MASTER_DETECTION` must be `VERIFIED_LIVE`) and the pair allowlist.

## 3. Call sites

| Call site | Reads | Status | Preserved / changed |
|---|---|---|---|
| `MasterOrderPoller.poll_once` | Master book, seed + later polls | **Migrated** (milestone 1) | Behaviour identical (oracle test; all poller suites unchanged). Only the "fetch failed" log carries the exception type instead of the broker message. Still two GETs on the seed pass. |
| `strategy_switch.run_preflight` | raw rows of every account in scope | **Migrated** (milestone 2) | Same blocker codes and messages byte for byte (`BROKER_VERIFICATION_UNAVAILABLE` x3 texts, `PENDING_MASTER_ORDERS`, `PENDING_CHILD_ORDERS`), same acknowledged-override behaviour, one raw call per account. `NO_CLIENT` / `RAW_READ_NOT_SUPPORTED` keep their own messages; every other unproven read keeps "order book could not be fetched". Only stricter edge: a raw read returning a non-list non-None value (never the real client) is now "could not be fetched" instead of iterating whatever it returned. The failure log names the exception type only. |
| `ReconciliationEngine._submitted_verdict` (crash recovery) | raw rows of the Child's book | **Migrated** (milestone 2) | Same `KNOWN` / `NOT_IN_BOOK` / `NO_BROKER_ID` / `UNVERIFIABLE`; ids only from a PROVEN read (an empty list is `NOT_IN_BOOK`); no client, no raw read, `None`, non-list, non-awaitable and a raise are all `UNVERIFIABLE`. One read per account (cached). |
| `EngineLoop._fetch_order_book_for_account` -> `StaleOrderSweeper` | Child books for TIMEOUT trades | **Migrated, then optimised** | Milestone 1 fixed a false CRITICAL "not found in broker order book" for an unread book (unproven read -> `None`). Milestone 2 reads it with `read_order_book_single`: one GET instead of two for a real Alice client, same verdicts (parity test over 13 payload shapes). |
| `api/broker._resolve_fill_status_and_price` | Master book after a manual order | Not migrated | Order-placement path; a failed read already reads as "not filled yet". |
| `follower_fill_syncer` | Child book | Not migrated, safe | A missing record is skipped, so a failed read changes nothing. |
| `pipeline` SL-trigger read-back | Child book | Not migrated | One specific order: belongs to `OrderStatusProvider`, not this contract. |
| `api/strategies` member positions (UI) | Child book | Not migrated | Display only. |
| `pnl/service` | raw rows | Not migrated | Reporting; Alice raw format. |

The one documented divergence of the single read: `get_order_book_raw` has always dropped non-dict rows while the parsed read
raises on them, and the single read follows the raw read (as the preflight and recovery already did). Alice Blue never sends such
rows; `tests/test_order_book_read.py` pins it so a change is visible.

## 4. Remaining Alice Blue-specific assumptions

1. `OrderRecord` / `OrderUpdateEvent` live in `alice_blue/types.py` and are the engine's normalized types.
2. `MasterOrderPoller.BROKER_STATUS_MAP` is keyed by Alice's status strings (`TRIGGER_PENDING`, `COMPLETE`, ...); Groww and Angel
   One map into the same vocabulary inside their clients.
3. The preflight and crash recovery still work on Alice's RAW rows: ids through `order_book_row_id`
   (`brokerOrderId` / `Nstordno` / `orderId`), statuses and labels through `_raw_status` / `_raw_label` / `TERMINAL_BROKER_STATUSES`
   in `strategy_switch`. The read state is now explicit; the row format is not yet neutral. Moving them to normalized `records`
   needs a lenient identity extraction (a malformed row must not turn a verifiable book into FAILED) and is the next stage.
4. The "hides failures" convention is inferred from `get_order_book_raw` being present, not declared per broker.
5. `OrderRecord.instrument_id` is the Master broker's own token; it is meaningless to another broker.
6. Products are Alice strings (`INTRADAY`, `LONGTERM`, `MTF`); `exchange` defaults to `"NSE"` when a row reports none.
7. The WebSocket path exists only for the legacy Alice Master and delivers no order frames in production; REST polling is the
   proven detection path.
8. Master-switch and recovery user-facing texts name Alice Blue ("Alice Blue order book could not be fetched ...").
9. The preflight requires `get_order_book_raw`: a broker without it can never pass a switch (`BROKER_VERIFICATION_UNAVAILABLE`,
   "does not support the order-book verification a Master switch needs") - correct for now, because none is Master-eligible.
10. The seed-pass checked read of the POLLER still makes two GETs (raw, then parsed). Switching it to `read_order_book_single`
    is a one-line change but alters what the seed pass sees when the two responses would differ, on a path that decides which
    orders are ever copied; it needs its own review.

## 5. Broker status (evidence in the repository)

| Broker | Master detection | Child | Evidence | Pairs |
|---|---|---|---|---|
| Alice Blue | `VERIFIED_LIVE` | `VERIFIED_LIVE` | production | `alicebluenew → alicebluenew` **allowed** |
| Groww | `UNVERIFIED` (profile ceiling); `master_verified=False` | placement/modify/cancel `VERIFIED_DOCS` | documentation + offline fakes | blocked (Master and Child) |
| Angel One | `UNVERIFIED`; `master_verified=False` | `UNVERIFIED` | community field names, offline fakes | blocked (Master and Child) |
| Zerodha | not implemented (catalog only, `NOT_SUPPORTED`) | not implemented | none | blocked |

A passing contract or fake test is not evidence. Raising a broker's `MASTER_DETECTION` to `VERIFIED_LIVE` requires a reviewed
capture of real order-book reads (successful, empty, failed and truncated) from a real account, then the contract flag, the
profile and the allowlist entry — each a separate, reviewed change.

## 6. The two strict over-freeze xfails

`test_c7_alice_blue_…` and `test_c7_angel_one_refuses_an_over_freeze_quantity_before_sending` stay strict xfails. Resolving them
needs an authoritative freeze quantity (none of the available sources carries one — 06/R4 instrument terms record freeze as
UNKNOWN) or a placement-time refusal, which would change the order-placement path and needs a verified threshold. No affected
order path is enabled by this phase; quantity validation remains observe-only.

## 7. Next stages (each separately reviewable)

1. Move the preflight and crash recovery from raw rows to normalized `records` taken from the SAME response
   (`read_order_book_single` already returns both): needs a lenient identity extraction so a malformed row cannot turn a verifiable
   book into `FAILED`, and `_raw_status`/`TERMINAL_BROKER_STATUSES` re-expressed on the normalized status vocabulary.
2. Review switching the poller's seed pass to `read_order_book_single` (halves its GETs; see assumption 10).
3. Declare the read convention per broker in the capability profile instead of inferring it from a method name.
4. Move `OrderRecord` / `OrderUpdateEvent` / the status vocabulary to a broker-neutral module (re-exported from
   `alice_blue/types.py` for compatibility).
5. Per broker, gather live evidence for Master detection; only then flip `master_verified`, the profile and the allowlist.
6. Cross-broker Children additionally need canonical instrument mapping enforced (currently shadow-only) and verified Child
   features; until then every non-Alice pair stays blocked.
