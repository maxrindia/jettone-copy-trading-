# 05 · Broker capability matrix and Master → Child compatibility

## 5.1 Status vocabulary

| Status | Meaning | May enable trading? |
|---|---|---|
| **VERIFIED_LIVE** | Implemented, tested offline, and observed against a real account | Yes, behind its gate |
| **VERIFIED_DOCS** | Implemented and tested offline against the official documentation; never seen live | Not for real money until live-verified |
| **UNVERIFIED** | Implemented or partly implemented, contract or behaviour not confirmed | No |
| **NOT_SUPPORTED** | Not implemented in Jettone (says nothing about the broker's own API) | No |
| **NEEDS_OFFICIAL_DOCS** | No evidence in the repository; must come from the broker's official docs before design | No |

Evidence column refers to repository files. Nothing about Zerodha or 5paisa below is inferred from memory of their APIs.

## 5.2 Capability matrix

| Capability | Alice Blue | Groww | Angel One | Zerodha | 5paisa |
|---|---|---|---|---|---|
| Adapter in repo | Yes | Yes | Yes | **No** | **No** |
| Registry status | `SUPPORTED` | `PARTIAL` (`GROWW_ENABLED=true` in prod, Child gate off) | `NOT_SUPPORTED` while `ANGEL_ONE_ENABLED=false` | `NOT_SUPPORTED` | `NOT_SUPPORTED` |
| Authentication | VERIFIED_LIVE — daily OAuth, Redis `alice:session:` | VERIFIED_LIVE for the token call (2026-10-19: HTTP 200, flag `active`, `ttl` logged). Expiry: **06:00 IST daily is PROVISIONAL** (decision D5) — consistent with one observed `ttl`, not documented; the code keeps taking the earlier IST/UTC reading | UNVERIFIED (implemented: client code + PIN + TOTP; never run live) | NEEDS_OFFICIAL_DOCS | NEEDS_OFFICIAL_DOCS |
| **Master detection** | VERIFIED_LIVE (REST poll; WS present) | UNVERIFIED — poller works on mocks; `GrowwContract.master_verified=False`; pagination base, part-fill vocabulary, stable ids unknown | UNVERIFIED — `master_verified=False` | NOT_SUPPORTED | NOT_SUPPORTED |
| **Child placement** | VERIFIED_LIVE | VERIFIED_DOCS (gated by `GROWW_CHILD_TRADING_ENABLED=false`; `live_verified=False`) | UNVERIFIED (contract `verified=False`, every parser refuses) | NOT_SUPPORTED | NOT_SUPPORTED |
| Modify / cancel | VERIFIED_LIVE (EC965 on MARKET modify noted) | VERIFIED_DOCS (modify only untouched pending; qty semantics unclear) | UNVERIFIED | NOT_SUPPORTED | NOT_SUPPORTED |
| Order updates / fill sync | VERIFIED_LIVE (poll) | VERIFIED_DOCS (status map built) | UNVERIFIED | NOT_SUPPORTED | NOT_SUPPORTED |
| Client order reference | **None** (no tag sent) | VERIFIED_DOCS `order_reference_id` | None offered | NEEDS_OFFICIAL_DOCS | NEEDS_OFFICIAL_DOCS |
| Unknown-send safety | **Defect**: post-send errors retried (`test_alice_blue_send_findings.py`) | Not resent; reference lookup | Not resent | — | — |
| Unknown-outcome recovery policy (D6) | Manual review | Reference lookup: operator-confirmed while VERIFIED_DOCS; automatic only once VERIFIED_LIVE | Manual review | — | — |
| Positions | VERIFIED_LIVE (`netQuantity` etc.; NFO LONGTERM↔NRML fold since `3098dbd`) | VERIFIED_DOCS (strict; duplicate rows = unknown) | UNVERIFIED | NOT_SUPPORTED | NOT_SUPPORTED |
| Holdings | NOT_SUPPORTED | NOT_SUPPORTED (no documented schema in repo) | NOT_SUPPORTED | — | — |
| Funds | VERIFIED_LIVE | VERIFIED_DOCS | UNVERIFIED | NOT_SUPPORTED | NOT_SUPPORTED |
| Products | MIS, CNC (`LONGTERM`), NRML (`MTF` on send) | MIS, CNC, NRML (equity) | INTRADAY, DELIVERY, CARRYFORWARD (mapped, UNVERIFIED) | NEEDS_OFFICIAL_DOCS | NEEDS_OFFICIAL_DOCS |
| Order types | MARKET, LIMIT, SL, SL-M | LIMIT, MARKET, SL, SL_M (validity DAY) | NORMAL/LIMIT, MARKET, STOPLOSS_LIMIT, STOPLOSS_MARKET | NEEDS_OFFICIAL_DOCS | NEEDS_OFFICIAL_DOCS |
| Exchanges | NSE, BSE, NFO (others in contract master not exercised) | NSE, BSE | NSE, BSE | — | — |
| Segments | CASH, F&O (no lot validation) | CASH only (`SUPPORTED_SEGMENT="CASH"`) | CASH only | — | — |
| Lot-size validation | No (`lot_sizes` default 1) | Yes (whole multiple, never rounded) | No | — | — |
| Tick-size / session calendar | No | No | No | — | — |
| Static IP | VPS whitelist | Per account, required for orders | Per account | NEEDS_OFFICIAL_DOCS | NEEDS_OFFICIAL_DOCS |
| Rate limits | existing handling | Documented, enforced client-side | None assumed (configurable, empty) | NEEDS_OFFICIAL_DOCS | NEEDS_OFFICIAL_DOCS |

Sources: `services/brokers/registry.py`, `services/groww/contract.py`, `services/angel_one/contract.py`,
`docs/BROKER_PARITY_MATRIX.md`, `docs/groww-verification.md`, `services/copy_engine/risk_inputs.py`,
production log of 2026-10-19 12:51–12:52 UTC (Groww token).

## 5.3 Master → Child compatibility

A pair is enabled only when **all** hold:
1. Master broker `role=MASTER` is VERIFIED_LIVE and its gate is on.
2. Child broker `role=CHILD` is VERIFIED_LIVE and its gate is on.
3. A canonical instrument mapping exists from Master to Child for the order's segment (and both support it).
4. The Child supports the order type and product for that segment.

Conditions 3–4 are evaluated **per order**, so an enabled pair can still skip individual orders (e.g. NFO to Groww).

| Master ↓ / Child → | Alice Blue | Groww | Angel One | Zerodha | 5paisa |
|---|---|---|---|---|---|
| **Alice Blue** | **ENABLED** (current production) | **BLOCKED** today → candidate: CASH only, after Groww Child live verification | BLOCKED (contract unverified) | BLOCKED (no adapter) | BLOCKED (no adapter) |
| **Groww** | BLOCKED (Groww Master unverified) → candidate after Master verification; CASH only | BLOCKED (same) | BLOCKED | BLOCKED | BLOCKED |
| **Angel One** | BLOCKED (Master unverified) | BLOCKED | BLOCKED | BLOCKED | BLOCKED |
| **Zerodha** | BLOCKED (no adapter) | BLOCKED | BLOCKED | BLOCKED | BLOCKED |
| **5paisa** | BLOCKED (no adapter) | BLOCKED | BLOCKED | BLOCKED | BLOCKED |

### Heterogeneous strategy example

Master Alice; Children: Alice A, Groww B, Zerodha C, Angel D.
- **Today:** membership add for B is refused (`CHILD_BROKER_UNSUPPORTED`); C and D are refused. Only A copies.
- **After Groww Child is enabled:** adding B succeeds with a `CHILD_SEGMENT_COVERAGE_WARNING` ("will not
  receive F&O orders") (D1). A copies everything; B copies NSE/BSE equity only and records
  `SEGMENT_NOT_SUPPORTED` on its own row for NFO orders; C and D are still refused at membership time.

## 5.4 Fail-closed errors (API envelope `{success:false, error:{code,message}}`)

| Code | When | Surface |
|---|---|---|
| `CHILD_BROKER_UNSUPPORTED` (exists) | Adding a Child whose broker lacks `as_child` | Membership API 409, UI disabled with reason |
| `NEW_MASTER_BROKER_UNSUPPORTED` (exists) | Master switch to a broker without `as_master` | switch-master 409 |
| `BROKER_PAIR_NOT_SUPPORTED` (new) | Both roles fine individually but the pair is not enabled | Membership / switch 409 |
| `CHILD_SEGMENT_COVERAGE_WARNING` (new, **non-blocking**) | Child's broker lacks some segments, products or order types the strategy may trade | Membership API 200 with a `warnings` list; UI notice (D1) |
| `CHILD_BROKER_NOT_READY` (exists) | Routing-time: gate flipped off after membership | audit + skip |
| `INSTRUMENT_UNMAPPED` (new) | No canonical → Child mapping | per-order skip, audit |
| `SEGMENT_NOT_SUPPORTED` (new) | Child broker lacks the segment | per-order skip, audit |
| `PRODUCT_NOT_SUPPORTED` / `ORDER_TYPE_NOT_SUPPORTED` (new) | No exact equivalent | per-order skip, audit |
| `INSTRUMENT_RESOLUTION_FAILED`, `POSITION_FETCH_FAILED`, `FUNDS_FETCH_FAILED`, `INSUFFICIENT_LONG_POSITION` (exist) | Risk stage | per-order reject, audit |

## 5.5 How the profiles encode this (R1)

- **Per-order lookup is its own feature.** `ORDER_STATUS` means reading the broker's order book; `ORDER_LOOKUP`
  means querying one specific order as a public client method, and is NOT_SUPPORTED for every broker (Groww's
  `_lookup_by_reference` is private).
- **Design bar.** Child: placement, modify, cancel, order status, positions and funds all `VERIFIED_LIVE`.
  Master: Master detection and order status `VERIFIED_LIVE`. In both cases the registry gate must also be open.
  Today only Alice Blue qualifies for either role.
- **Evidence ceilings (Groww).** Only login is live-verified (the token call, 2026-10-19). Placement, modify,
  cancel, order book, positions, funds, the client reference and the NSE/BSE cash segments are capped at
  `VERIFIED_DOCS`; Master detection at `UNVERIFIED`. A contract boolean cannot raise a rating past its ceiling.
- **Angel One.** Nothing is rated above `UNVERIFIED`. `ANGEL_CONTRACT.verified` only records that the response
  keys are filled in, not whether they came from documentation or a capture, so it is not rated `VERIFIED_DOCS`.
- **Angel One modify limitation (blocks enabling it).** The pipeline's modify calls never pass `product_type`.
  Alice Blue's and Groww's modify bodies do not use a product, but `AngelOneClient.modify_order` requires one and
  raises without it, so an Angel One Child order cannot currently be modified through the pipeline. This must be
  resolved and tested before Angel One can be enabled (see 07 §7.2); it is not an R1 change.
- **Segments.** A segment's rating applies to all of its listed products and order types; per-cell evidence is not
  recorded. `supports()` refuses unverified and documentation-only segments unless the caller states the weaker
  level it accepts.

When a gate is turned off after members exist, members stay in the strategy but are skipped at routing (existing behaviour), and the UI shows them as "not receiving copies" with the reason.
