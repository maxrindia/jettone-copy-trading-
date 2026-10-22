# Per-Child execution tracking (R3)

For every Master order that Jettone copies, you can see what happened to **each Child**, in one place.

## What is recorded

Every Child the strategy **routes** an order to gets exactly one `copy_trades` row for that Master event. The unique key is
the existing `(master_order_id, follower_account_id, copy_event_id)`.

| Where the Child left | `copy_trades.status` | Notes |
|---|---|---|
| Before the send: paused or disabled, zero lot, funds or risk rejection, missing reference price | `SKIPPED` (local decision) or `REJECTED` (funds / risk) | `quantity` 0, no `client_order_id`, never sent. The reason is in `failure_reason`. |
| Before the send: SELL-safety block (no long position to sell) | `RECONCILIATION_REQUIRED` | Written by the existing SELL guard, not by R3, and left as it is (it needs a human look). The copy's quantity and the safety code are in the row. |
| Sent, broker accepted | `SUBMITTED` | `broker_order_id` set. Fills live on the Child's `Order`; the row moves to `SUCCESS` / `FAILED` / `REJECTED` when the order finishes. |
| Sent, broker refused | `REJECTED` | `failure_reason`: `REJECTED: <broker message>`. |
| Sent, provably never left | `FAILED` | `failure_reason`: `NETWORK_ERROR: …`. |
| Sent, outcome unknown | `TIMEOUT` | Never re-sent. Verify at the broker (see `AMBIGUOUS_ORDER_RECOVERY_RUNBOOK.md`). |
| Task raised after the claim | stays `PENDING` | Not failed automatically (it may have reached the broker). The sweeper flags it; a `COPY_CHILD_EXCEPTION` event is raised. |

**Routed vs excluded.** "Routed" Children are strategy members whose trading is enabled, whose account is active, not
emergency-stopped, not deleted, and whose broker may receive copies. Every other member (except the Master) is
**excluded**: it gets **no** `copy_trades` row, so deliberately disabled accounts do not distort the per-account counts.
Excluded members are listed with their reason in the fanout summary.

## Reason codes

`failure_reason` is `CODE` or `CODE: detail`. Codes the pipeline's own gates write are kept as they are
(`FUNDS_FETCH_FAILED`, `RISK_INPUTS_UNAVAILABLE`, `REFERENCE_PRICE_UNAVAILABLE`, `INSUFFICIENT_MARGIN`, the SELL-safety
codes such as `INSUFFICIENT_LONG_POSITION`, …). R3 adds:

| Code | Meaning |
|---|---|
| `TRADING_DISABLED` | the Child was paused (runtime stop) when the order arrived |
| `ZERO_LOT_RESULT` | the Child's quantity rounded to zero |
| `RISK_REJECTED` | several risk checks failed at once; the detail lists them |
| `NO_CLIENT` | claimed but no broker client existed, so nothing was sent (`FAILED`) |

Members excluded from a fanout (summary only): `MEMBER_TRADING_DISABLED`, `ACCOUNT_INACTIVE`,
`ACCOUNT_EMERGENCY_STOPPED`, `ACCOUNT_DELETED`, `ACCOUNT_MISSING`, `CHILD_BROKER_NOT_READY`.

## Reading it

`GET /api/v1/trades/fanout/{master_order_id}` (same internal-token authentication as the other trade endpoints) returns:

- `children`: one entry per `copy_trades` row (so a MARKET order filled in parts has one per fill) with `status`,
  `reason_code`, `reason_detail`, `broker_order_id`, timestamps and the Child's own `orders` (status and filled quantity);
- `counts`: how many rows per status;
- `summaries`: advisory per-event records listing the excluded members. For each routed Child they give the
  pipeline's `status`/`code` for the attempt and the row's actual `copy_trade_status` (which can differ, for example
  `REJECTED` vs `RECONCILIATION_REQUIRED` for a SELL block, or `SUCCESS` vs `SUBMITTED` for an accepted order).

`GET /api/v1/trades?master_order_id=…` lists the raw rows.

**The `copy_trades` rows are the authority.** The summary is an advisory `audit_logs` row (`COPY_FANOUT_SUMMARY`). It is
**not exactly-once**: if the same event is processed again after Redis lost its dedupe key, a second summary can be
written (a pure replay where every Child answers `DUPLICATE_EVENT` writes none).

## Duplicates and re-claiming

- A redelivered event is stopped first by the Redis `copy:seen` key (set per Master event, 24 hours), before any Child is
  looked at, so a recorded decision is **not** re-evaluated while that key exists.
- If the key was lost, the database still decides: a Child that **claimed** its row (R2B) is a duplicate and is never
  sent again.
- Only a **pre-send** `SKIPPED` / `REJECTED` row (no `client_order_id`, no broker id, never submitted) can be taken over,
  once, by compare-and-set, and only within 10 minutes of the first decision. Afterwards it is claimed like any other
  row (`retry_count` becomes 1). An older pre-send decision stands.

## Not covered

Modify and cancel propagation (still recorded on the child orders and system events), copying Master orders that were
missed while the engine was down, a user interface for this view, and any change to the sweeper (a resting LIMIT row is
still `SUBMITTED` and may be flagged stale after 120 seconds until the order finishes).
