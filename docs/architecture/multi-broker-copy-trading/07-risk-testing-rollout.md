# 07 · Risk and failure analysis, test plan, rollout

## 7.1 Invariants that must survive every phase

| # | Invariant | Where enforced today | Multi-broker rule |
|---|---|---|---|
| I1 | No SELL without a broker-confirmed long covering the full qty, same instrument **and product bucket** (unless `SELL_POSITION_SAFETY_BYPASS`) | `_enforce_sell_position_safety`, `get_available_long` | Evaluated on the **Child's** broker via its `PositionProvider` and the Child's mapped instrument. Unknown book / unmapped instrument / unknown product = reject. |
| I2 | Funds unknown = reject | `RiskEngine`, `FUNDS_FETCH_FAILED` | Per Child `FundsProvider`; one Child's funds outage never affects another. |
| I3 | Master LIMIT copied at the **exact** limit price | `replicate_limit_order` | Unchanged for every Child broker. No buffer, no tick rounding. If the Child broker would require a different price format, skip with a reason, never adjust. |
| I4 | Marketable-LIMIT buffer only on Master MARKET → LIMIT conversion | `_process_single_market_follower`, `MARKET_ORDER_BUFFER_PERCENT` (default 0) | Unchanged; the buffer is never applied to LIMIT/SL copies on any broker. |
| I5 | One copy per (master order, child, event) | DB unique triple | Kept; namespaced key for new Master brokers (06 §6.3). |
| I6 | Ambiguous submission never resent | `_record_unresolved_send`, sweeper | Kept for all adapters. Alice `RetryPolicy` defect must be fixed **before** any multi-broker rollout that increases Alice child volume. |
| I7 | Reconciliation alerts only; broker book is truth | `reconciliation.py` | Kept per Child. |
| I8 | Routing strictly by strategy; no "copy to all" | `_get_eligible_followers_for_master` | Kept. |
| I9 | Account in at most one strategy | `strategy_account_roles` | Kept. |
| I10 | Live trading off by default, observe-only mode | `ENGINE_LIVE_TRADING_ENABLED` | Kept; each new pair also has its own gate. |
| I11 | An incomplete combined P&L is never shown as complete (D3) | `pnl/service.py`, dashboard summary (`combined_total` null when any account unavailable) | Explicit `combined_status` COMPLETE / PARTIAL / UNAVAILABLE; a partial figure only with its excluded accounts listed. |
| I12 | Restart never resends; resume is supervised (D2) | crash recovery in `reconciliation.py` | Fan-out ledger + reconciliation first + operator approval per Child (03 §3.8). |

**Production note on I1.** Production currently runs with `SELL_POSITION_SAFETY_BYPASS=true`, so the follower SELL
position check is skipped on the main path today (`pipeline._enforce_sell_position_safety`). This design does not
change that flag. Whether a new broker pair may be enabled while the bypass is on is an open owner decision (README).

## 7.2 Failure analysis

| Failure | Effect without design | Design response |
|---|---|---|
| Child broker outage (one of N) | Could be mistaken for platform failure | Per-Child task; that Child's rows `BROKER_REJECTED`/`SUBMISSION_UNKNOWN`; siblings unaffected; alert names the broker. |
| Master broker poll failure | Missed or phantom events | `Unknown` snapshot → poll skipped; never infer cancels from a missing row. |
| Truncated order-book page (Groww full page 100) | Missed orders | Treat as Unknown (exists); no paging loop until base is verified. |
| Cross-broker instrument mismatch (wrong strike/expiry) | Wrong contract bought | Canonical key must include expiry, strike, option type; mismatch or ambiguity = `INSTRUMENT_UNMAPPED`. Bare underlying names ("NIFTY") are never mapped by name alone. |
| Product vocabulary drift (LONGTERM vs NRML) | Covered SELL rejected (production 2026-10-14) | Per-broker, per-exchange product buckets in the capability profile; fixed for Alice NFO in `3098dbd`; same rule must be declared per broker. |
| Duplicate event from WS + poll | Double order | Unique triple. |
| Restart mid-fan-out | Half the Children submitted; today the rest are silently never copied | Fan-out ledger marks the event interrupted. Rows with `submitted_at` set are reconciled (never resent). Children never attempted become resume candidates only after reconciliation confirms nothing exists at their broker, and are placed only on operator approval, same trading day, with the full planner re-run (D2, 03 §3.8). |
| Partial success across Children | Inconsistent exposure across accounts | Accepted and visible: dashboard shows per-Child status; no compensating trades are placed automatically. |
| Gate turned off mid-day | Orphan open child orders | Gate blocks **new** placements only; cancels/modifies for already-placed children still go through the same broker (needs `existing_children_lookup` to bypass the eligibility gate only for cancel/modify — verify current behaviour before enabling). |
| Session expiry on a Child broker (Groww: 06:00 IST daily, **provisional**, D5) | Rejections | Session status checked before submit; expired = skip `SESSION_EXPIRED`, no login attempt from the pipeline. The stored TTL stays the earlier of the IST/UTC readings until documentation or safe evidence confirms the timezone. |
| Angel One modify has no product (pipeline omits `product_type`; `AngelOneClient.modify_order` requires one and raises `AngelOneError` when it is None) | Master modify/reprice propagation to an Angel One Child would fail, leaving a stale working order | Not an issue today: Angel One is disabled and the failure is fail-closed (an exception, no request). **Must be resolved and tested before Angel One can be enabled** — e.g. supply the product from the Child order's own record in the modify path, with a test for every product — under its own approved phase. R1 changes neither the pipeline nor the client; a pinned test records the limitation. |
| Rate limits | 429 storms | Adapter-local limiter; a refused request is a definitive "not sent" (`BROKER_REJECTED`), never queued for blind retry. |

## 7.3 Test plan

| Layer | What | Notes |
|---|---|---|
| Unit | Capability registry evaluation (role × segment × feature × gate); compatibility matrix; canonical instrument mapping per broker; namespaced `copy_event_id` | Table-driven; every cell of 05 §5.3 has a test asserting enabled/blocked |
| Contract | Each adapter against recorded/official sample payloads: order book, order status, positions, funds, place/modify/cancel responses, error bodies | Parsers fail closed on unknown shapes; no live calls |
| Protocol conformance | Each client exposes the methods of the `Protocol`s it claims (names, via `isinstance`), its **signatures** match one explicit contract (`inspect.signature`, known differences pinned), and the pipeline's real place/modify/cancel call shapes bind on every client. A Child-only broker's client may structurally satisfy `MasterOrderSource`; the profile and registry, not the class, decide it is not a Master | `runtime_checkable` + `inspect.signature` + AST scan of pipeline calls + capability-profile cross-check |
| Pipeline integration | One Master, heterogeneous Children (Alice + Groww + unsupported): per-Child outcomes independent; one Child broker raising does not change others; duplicate event → no second call; NFO to Groww → `SEGMENT_NOT_SUPPORTED` | In-memory SQLite, fake adapters |
| Unknown outcome | Post-send error on Child B: B → `SUBMISSION_UNKNOWN`, never resent; recovery follows the broker's capability policy (D6); Alice stays manual | Extends `test_order_state_transitions.py` |
| Supervised resume (D2) | Restart between Child A's send and Child B's planning: A reconciled, B a resume candidate only after a negative lookup; nothing placed without approval; approved resume re-runs the planner and keeps the same `copy_event_id`; next-day candidates expire to `NOT_COPIED` | New |
| Membership warning (D1) | Adding a Groww Child to a strategy that trades NFO returns 200 + `CHILD_SEGMENT_COVERAGE_WARNING`; NFO order → that Child `SEGMENT_NOT_SUPPORTED`, siblings copy | New |
| P&L completeness (D3) | Any unavailable account → `combined_status` is not `COMPLETE`; a partial figure always carries `excluded_accounts` | Extends `test_unsupported_broker_dashboard.py` |
| Regression (must stay green) | Entire existing suite (2582), especially `test_sell_*`, `test_limit_*`, `test_order_handling_matrix.py`, `test_raw_position_row_sell_guard.py`, `test_capability_gates.py`, Alice WS tests | Alice → Alice byte-identical behaviour: golden tests on outgoing payloads |
| Broker validation (owner-run, read-only first) | Groww: holdings? positions carried field, part-fill statuses, pagination base, stable order ids (scenarios in `OWNER_CAPTURE_INSTRUCTIONS.md`) | Supervised; no orders until read-only captures are reviewed |
| Live smoke (per pair) | One small, supervised order per order type/product the pair claims | Only after all above, with explicit approval |

## 7.4 Rollout plan

| Stage | Change | Gate | Exit criteria | Rollback |
|---|---|---|---|---|
| R0 | Docs (this) | — | Reviewed | — |
| R1 | Declare Protocols; existing clients satisfy them; capability profile objects alongside current `BrokerSpec` (registry output unchanged) | none | Full suite green; Alice payload golden tests identical | Revert commit |
| R2 | Fix Alice duplicate-send (separate approval) | none | `test_alice_blue_send_findings.py` flipped to assert no resend; production unchanged otherwise | Revert + rollback image |
| R3 | Additive migration `0013` (nullable columns); pipeline writes them | none | Rows populated for Alice → Alice; no behaviour change | `alembic downgrade` (drops only new columns) |
| R3b | Fan-out ledger (`0014`) + supervised resume UI/API (D2); P&L `combined_status` (D3) | none | Interrupted events listed; no resume without approval; Alice → Alice unchanged | Feature flag off; migration downgrade |
| R4 | Canonical instrument mapper + per-order segment/product/order-type checks (applies to every pair, Alice → Alice must map 1:1) | none | Alice → Alice skips nothing it copied before (shadow-compare in observe-only mode for ≥ 1 week) | Feature flag `CANONICAL_MAPPING_ENFORCED=false` |
| R5 | Alice → Groww Child, CASH only | `GROWW_CHILD_TRADING_ENABLED` + pair gate `PAIR_ALICE_GROWW` | README acceptance criteria for Alice → Groww met; supervised live smoke passed | Flags off (new placements stop; existing children reconciled manually) |
| R6 | Groww Master | `GROWW_MASTER_ENABLED` + `master_verified=True` in code | Captures reviewed; observe-only shadow detection matches Groww app for ≥ 5 trading days | Flags off; poller stops |
| R7 | Further brokers (Angel One, Zerodha, 5paisa) | per broker | Adapter + contract + same criteria | Flags off |

Global rollback criteria (any one): an unexpected duplicate order; a SELL placed that the guard should have
refused; a Child copy at a price other than the rule; an Alice → Alice behaviour difference; an unmapped
instrument copied. Response: turn the pair gate off, keep Live Trading off, investigate before re-enabling.
