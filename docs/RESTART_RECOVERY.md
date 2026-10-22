# Restart recovery (R3b)

What the engine does after a restart, and what it will never do. The rule throughout: **recovery fails closed**. If it
cannot prove that acting is safe, it reports and sends nothing, and it never resends a Child order that was claimed or
whose outcome is unknown.

## What the engine can and cannot know

| Evidence | Where | Strength |
|---|---|---|
| An order has Children | `copy_trades` rows or Child `orders` for the Master order id | Strong: it was copied. |
| A fanout began | `audit_logs` `COPY_FANOUT_STARTED` (written before any Child is processed): the routed Children at that moment and the order's attributes | Strong: tells a crash before fanout apart from observe-only history, which look identical (a Master order row and nothing else). |
| Current state | A freshly and successfully read broker order book | Used to confirm the order is still working and unchanged. |
| Broker order times | the order book | **Never used.** The parsed time is labelled UTC and falls back to "now". |

It cannot know whether an order was placed and then cancelled or filled during the downtime, or how long the engine was down.

## The first poll after a start

The poller reads the order book. If that read cannot be shown to have succeeded (the client returns an empty list on a
network error), the poller **does not seed** and tries again; it used to seed an empty baseline, after which every
existing order looked new.

Then each Master order in the book is classified from the evidence above:

| Case | Result |
|---|---|
| Already has Children | Stays under normal change detection (it is **not** baselined), so a later cancel or modify still reaches the Children. See below. |
| Has a `COPY_FANOUT_STARTED` record and a routed Child with no decision | Candidate for **resume** (off by default). |
| No persisted row | Reported `MASTER_ORDER_MISSED_WHILE_DOWN`. **Not copied.** |
| Persisted and working, no fanout record | Reported `MASTER_ORDER_OBSERVED_NOT_FANNED_OUT`. **Not copied.** |

All reports are deduplicated per order, so repeated restarts do not repeat alerts.

## Orders that were already copied

Decided from the **Children's** state, so repeating a startup repeats nothing once a change is applied:

- **Cancel** is dispatched only while a Child of a cancelled Master order is still working. Children already cancelled: nothing.
- **Modify** is dispatched only when a working Child's price (LIMIT) or trigger (SL) differs from the book.
  Once applied the Child matches, so the next restart does nothing. A Child whose modify failed is retried at the next restart.
- Fill progress is just persisted.
- A dispatch produced here may only become an event that acts on existing Children (`ORDER_CANCELLED`, `ORDER_MODIFIED`,
  `SL_PENDING_MODIFIED`). Anything else is not applied and is reported (`RECOVERY_LIFECYCLE_NOT_APPLIED`).
- **Never replayed, reported `MASTER_ORDER_CHANGE_NEEDS_REVIEW` instead** (and permanently baselined): a LIMIT turned
  MARKET, an SL that triggered while down, a MARKET fill delta, a quantity-only change. These would place or reprice
  orders at stale prices. Review the Children by hand.
- Orders placed from the Jettone UI are never re-dispatched, exactly as in a normal run.

## Resuming a partial fanout (`ENGINE_RECOVERY_RESUME_ENABLED`, default **false**)

Only when the flag is on **and** all of these hold; otherwise it reports `RECOVERY_RESUME_SKIPPED` with the reason:

1. a `COPY_FANOUT_STARTED` record exists for a **LIMIT creation** or **SL-pending creation** (never MARKET), written within the
   last **10 minutes of the engine's own clock**;
2. the Child is in the recorded routed-Child snapshot and still has **no** `copy_trades` row for the event;
3. the record, the persisted Master row and the fresh book agree on type, symbol, side, exchange, quantity, price and trigger;
4. the order is still `OPEN` (LIMIT) or `TRIGGER_PENDING` (SL) with nothing filled;
5. live trading and the master toggle are on, and the Child has a broker client (a Child with none is left without a
   decision rather than given a spurious rejection).

The missing Children then go through the **normal** path: the runtime pause, funds and risk checks, the SELL guard, R3
outcome tracking, and the database claim before the send. The only thing skipped is the Redis duplicate check. A Child
that already has any row (including `TIMEOUT`, `SKIPPED`, `REJECTED`) is never touched, so a second run, two runs at once,
or a lost Redis key cannot send a second order.

If the start record could not be written (the fanout still ran), that event never qualifies for resume.

## Child records at startup

| Record | Result |
|---|---|
| `PENDING`, never submitted | `RECONCILIATION_REQUIRED`, `CRASH_BEFORE_SEND`. Not proof nothing was sent (the submit stamp is best effort). |
| `PENDING`, submitted | `RECONCILIATION_REQUIRED`, `CRASH_DURING_SEND`. |
| `SUBMITTED`, id found in the Child's freshly read book | Left alone; the fill syncer owns it. |
| `SUBMITTED`, id missing from a successfully read book | `RECONCILIATION_REQUIRED`, `SUBMITTED_NOT_IN_BOOK`. |
| `SUBMITTED`, book unreadable or no client | Left as it is, reported once (`CRASH_RECOVERY_CHILD_BOOK_UNAVAILABLE`). A failed read is not proof the order is missing. |
| `TIMEOUT` | Unchanged: the R2 unresolved-send handling. |

Nothing here resends anything. Verify flagged orders at the broker (see `AMBIGUOUS_ORDER_RECOVERY_RUNBOOK.md`).

## Not covered

An operator-confirmed replay of a missed order, MARKET fill replay, a heartbeat or downtime measurement, multi-worker
locking, and modify/cancel results on `copy_trades`.
