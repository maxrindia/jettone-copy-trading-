# Multi-broker copy trading: design proposal

**Status:** Phase 1 design, documentation only. Nothing here is implemented, enabled or migrated.
**Baseline:** repository `ebf2472`, production `a64c29d`. Live Trading off; Groww Child/Master gates off; Angel One off.

## Goal

One explicit Master account per strategy and N Child accounts, each Child on any broker whose Child role is
verified, each Master on any broker whose Master role is verified. Every broker, role, segment and feature carries
an explicit verification status; anything unverified fails closed.

## Documents

| File | Contents |
|---|---|
| [01-current-state.md](01-current-state.md) | Current architecture, pipeline, broker-specific dependencies, data model |
| [02-target-architecture.md](02-target-architecture.md) | Target component diagram, UML class diagram, per-Child planner rules |
| [03-sequences.md](03-sequences.md) | Ingestion, heterogeneous fan-out, partial fills, cancel, duplicates, unknown outcome, reconciliation |
| [04-order-state-machine.md](04-order-state-machine.md) | Per-Child execution states, mapping to existing columns, recovery rules |
| [05-capability-and-compatibility.md](05-capability-and-compatibility.md) | Broker capability matrix, Master → Child compatibility, fail-closed error codes |
| [06-data-model-and-migration.md](06-data-model-and-migration.md) | Additive schema proposal, idempotency keys, migration plan (not run) |
| [07-risk-testing-rollout.md](07-risk-testing-rollout.md) | Invariants, failure analysis, test plan, staged rollout and rollback |
| [08-order-book-read-contract.md](08-order-book-read-contract.md) | Explicit order-book read result (ORDERS / EMPTY / UNKNOWN / FAILED) with raw-rows and single-response readers; migrated call sites (poller, sweeper, Master-switch preflight, crash recovery), remaining Alice-specific assumptions, next stages |
| [09-multi-broker-implementation.md](09-multi-broker-implementation.md) | Implementation record: Zerodha adapter, Master-source contract, broker-aware identity, canonical contracts, 4x4 compatibility matrix, per-broker status, unverified items and blockers |

All diagrams are Mermaid source inside the Markdown files.

## Decisions taken in this proposal

1. **Additive, not a rewrite.** Interfaces are `typing.Protocol`s that the current clients already satisfy; the pipeline's routing, pricing, SELL guard, funds checks and idempotency are reused as-is.
2. **Role-separated interfaces.** A Child-only broker never implements Master detection.
3. **Canonical instrument identity** is required for any cross-broker pair. A Master broker's token is never sent to a different Child broker.
4. **Capabilities are code + env gated.** Data can only disable, never enable.
5. **Per-Child isolation.** Each Child's execution, failure and reconciliation are independent; no sibling-triggered retries.
6. **No automatic resend** of an ambiguous submission, for any broker.
7. **One strategy per account** stays (migration 0008).

## Finalized owner decisions (D1–D6)

| # | Decision | Where it is reflected |
|---|---|---|
| D1 | A segment, product or order type a Child's broker cannot handle **skips that Child's execution only**, with a recorded reason. Membership shows a non-blocking coverage warning. | 02 §2.3, 05 §5.3–5.4, 07 tests |
| D2 | Restart recovery is a **supervised resume**: fan-out ledger, reconciliation first, operator approval per Child, full planner re-run, same trading day. An ambiguous submission is never retried blindly. | 03 §3.8, 04, 06 §6.2, 07 I12, R3b |
| D3 | Combined P&L is labelled `COMPLETE`, `PARTIAL` or `UNAVAILABLE`; an incomplete total is never shown as complete. | 06 (Combined P&L status), 07 I11 |
| D4 | The one-strategy-per-account rule is kept. | 06 §6.1, 6.4; 07 I9 |
| D5 | Groww expiry at 06:00 IST is **provisional** until reliable documentation or safe evidence; the code keeps the earlier IST/UTC reading. | 05 §5.2, 07 §7.2 |
| D6 | Unknown submissions follow a **broker-capability-based** recovery policy; manual review is the fallback; nothing is resent automatically. | 03 §3.6, 04 §4.3, 05 §5.2 |

## Current answer per combination

| Pair | Today | Earliest realistic |
|---|---|---|
| Alice → Alice | **Enabled** (production) | — |
| Alice → Groww | Blocked | After R1–R5, CASH (NSE/BSE equity) only |
| Groww → Alice | Blocked | After Groww Master verification (R6), CASH only |
| Groww → Groww | Blocked | After R5 + R6 |
| Any → Angel One / Zerodha / 5paisa, or those as Master | Blocked | After an adapter + contract + verification for each |

## Implementation roadmap (each stage separately approved)

| Stage | Work | Depends on | Size |
|---|---|---|---|
| R1 | Protocols + capability profiles beside `BrokerSpec` (the `BrokerAdapter` identity/profile/session wrapper is deferred); golden tests pinning Alice payloads; nothing wired | — | M |
| R2 | Alice Blue duplicate-send fix | Separate approval | M |
| R3 | Migration 0013 (nullable broker context on `copy_trades`) | R1 | S |
| R3b | Fan-out ledger (0014) + supervised resume (D2); P&L `combined_status` (D3) | R3 | M |
| R4 | Canonical instrument mapper; per-order segment/product/order-type checks; shadow compare | R1, R3 | L |
| R5 | Alice → Groww CASH Child, pair gate | R2, R3b, R4, Groww live captures | M |
| R6 | Groww Master | Groww pagination/part-fill/stable-id captures | M |
| R7 | Angel One, then Zerodha / 5paisa | Official docs + adapters | L each |

## Dependencies (external)

- Groww official docs / reviewed captures: holdings schema, pagination base, part-fill statuses, stable order ids, tick-size unit, expiry timezone.
- Angel One response contract and rate limits.
- Zerodha and 5paisa official API documentation (nothing in the repository today).
- An exchange session/holiday calendar source (shared gap).

## Remaining open decisions

Decisions D1–D6 above closed the earlier questions. Still open:

1. **SELL bypass and new pairs.** Production runs `SELL_POSITION_SAFETY_BYPASS=true`, which skips the follower SELL position check. Acceptance criterion 4 proves the guard on the Child broker, but with the bypass on it would not run in production. Decide whether a new pair may be enabled while the bypass is on.
2. **Groww automatic timeout reconciliation.** The existing Groww reference-map reconciliation already resolves automatically (it doesn't run today, because the Child gate is off). Under D6 it should be operator-confirmed until Groww is VERIFIED_LIVE. Decide before R5: gate it, or accept it.
3. **Resume window.** D2 offers resume for the same trading day "while the Master order or fill is still relevant". The exact cut-off (minutes after the Master event, or Master order still open) is not fixed.
4. **Groww Child: gate versus live verification.** The registry opens the Groww Child gate as soon as `GROWW_CHILD_TRADING_ENABLED` is on and the contract is documentation-verified (`registry.py`, pinned by `test_capability_gates.py`). This design requires a role to be `VERIFIED_LIVE` before a pair is enabled (acceptance criteria 1–2). The capability profile reports the mismatch (`gate_open` true, `meets_design_bar` false) and R1 changes nothing. Not decided here. Options:
   - **A. Tighten the registry.** Groww's `as_child` additionally requires every Child feature to be `VERIFIED_LIVE` in the profile (or the contract's `live_verified` plus a recorded live capture). Flipping the flag alone can then never route real orders on documentation evidence. Costs a registry change and updates to the existing gate tests; the safest default.
   - **B. Keep the flag as the explicit owner opt-in.** `GROWW_CHILD_TRADING_ENABLED` stays the single switch and the owner accepts documentation-level evidence for it, supervised, with the pair gate and live-smoke criteria of R5 still required before use. No registry change, but a configuration change alone can start real Groww orders.
   Decide before R5 (and before turning the flag on).

## Regression risks for Alice → Alice

| Risk | Mitigation |
|---|---|
| Canonical mapping skips an order Alice copied before | Shadow-compare in observe-only mode before enforcing; Alice mapping must be 1:1 |
| Namespaced idempotency key changes existing keys | New derivation only for new Master brokers |
| Protocol wrappers change semantics (Unknown vs empty) | Wrap only where needed; golden payload tests; full suite |
| Migration locks large tables | Nullable columns only; tested on restored dump |
| Extra checks add latency | Per-order checks are in-memory; no new broker calls for Alice |

## Acceptance criteria to enable a pair (all required)

1. **Master role** of the Master broker is VERIFIED_LIVE: detection of new, part-fill, fill, modify, cancel and reject observed live and matched against the broker's own app for ≥ 5 trading days in observe-only mode, with zero missed or phantom events.
2. **Child role** of the Child broker is VERIFIED_LIVE: place/modify/cancel/status/positions/funds parsed from live responses; supervised smoke order per claimed order type and product.
3. **Instrument mapping** for every segment the pair claims is tested with real instrument-master samples; an unmapped or ambiguous instrument provably skips.
4. **SELL guard** proven on the Child broker with raw position rows (covered, uncovered, wrong product, unknown book).
5. **Funds** unknown provably rejects.
6. **Pricing:** LIMIT exact; MARKET buffer only on conversion; tested per pair.
7. **Idempotency:** duplicate event, restart, and unknown-outcome tests pass; the only resend path is the operator-approved supervised resume (D2).
8. **Isolation:** a failing Child on this broker does not alter siblings' outcomes (integration test).
9. **Reconciliation** runs for the Child broker and alerts on mismatch without trading.
10. **Alice → Alice** suite and golden payload tests unchanged.
11. **Rollback** rehearsed: the pair gate off stops new placements immediately.
12. Explicit owner approval for the pair, recorded.
