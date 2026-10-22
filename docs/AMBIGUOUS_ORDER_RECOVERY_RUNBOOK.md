# Ambiguous order recovery runbook

When Alice Blue does not give a usable answer to an order, Jettone cannot know whether the order exists. It
**never re-sends it** and **blocks new orders for that account and symbol** until you check the broker and record what
you found. This page is the manual procedure. It covers manual master orders and manual square-offs. (A *copy* order that
was sent to a Child is handled differently, see the last section.)

## 1. Do not retry

An order that timed out, got a 5xx / 408 / 429, returned an unreadable answer, or came back "accepted" without an order
id **may be live**. Do not place it again, and do not click the button twice. Re-sending can double a position or
reverse a square-off. Each of those cases leaves the order *unresolved* and the endpoint answers "outcome UNKNOWN: do
NOT retry".

A **definite** refusal is different and needs no recovery: an explicit Alice Blue error code (for example `EC941`),
or an HTTP 401 (session refused; log in again). A 401 is treated as "not placed" because an unauthenticated request is
not executed; that is an inference from the status code, so if you doubt it, check the order book before re-sending.

## 2. Find the unresolved order

- The app shows the order's `client_order_id` (`JT_MANUAL_…` for a master order, or the square-off id) in the
  "outcome UNKNOWN" message. New orders for the same account and symbol are refused with HTTP 409 until it is resolved.
- List everything outstanding (internal token required, so run it inside the engine container):

```bash
docker compose exec -T fastapi python - <<'PY'
import os, httpx
r = httpx.get("http://localhost:8000/api/v1/broker/unresolved-orders",
              headers={"X-Internal-Token": os.environ["INTERNAL_API_TOKEN"]})
print(r.status_code, r.text)
PY
```

Each entry has `client_order_id`, account, symbol, exchange, side, quantity, order type and `created_at` (UTC; IST is
UTC + 5:30). The block never expires on its own. After 60 minutes a CRITICAL `UNRESOLVED_SEND_ESCALATED` event is
raised, but nothing is unblocked.

## 3. Verify at the broker

Log in to Alice Blue **as the account named in the entry** (the Master, or the Child for a square-off) and check:

1. **Order book** (all of today's orders, including rejected and cancelled). Look for the same symbol, side, quantity
   and product, placed around `created_at`. Alice Blue's order book carries no Jettone tag, so matching is by these
   details. Note the broker order number.
2. **Positions**: did the net quantity of that symbol change by the expected amount?

If several similar orders exist, the order is older than today's book, or you cannot tell, **do not resolve it**.
Leave it blocked and investigate (trade history / contract note).

## 4. Resolve it

Call the resolve endpoint with the fields below. All validation is enforced by the engine; a request that does not meet
it is refused (422 / 404 / 409) and **changes nothing**.

| Field | Rule |
|---|---|
| `client_order_id` | the unresolved order from step 2 |
| `outcome` | `NOT_PLACED` or `PLACED` |
| `verified_at_broker` | must be `true`: you did step 3 |
| `note` | 5 to 300 characters: what you checked |
| `broker_order_id` | **required** for `PLACED`, **forbidden** for `NOT_PLACED` |

```bash
docker compose exec -T fastapi python - <<'PY'
import os, httpx
body = {"client_order_id": "JT_MANUAL_…", "outcome": "PLACED", "verified_at_broker": True,
        "note": "found in order book as OPEN, order no 26100100…", "broker_order_id": "26100100…"}
r = httpx.post("http://localhost:8000/api/v1/broker/unresolved-orders/resolve", json=body,
               headers={"X-Internal-Token": os.environ["INTERNAL_API_TOKEN"]})
print(r.status_code, r.text)
PY
```

**Choose `PLACED`** when the order exists at the broker, in any state (open, filled, even rejected or cancelled by the
exchange). Give its broker order number. The order becomes `OPEN` with that id and the normal order sync records its
real state. Extra checks the engine makes:

- the id must not belong to another account;
- if the id is already recorded for this account, it must be the one external order the master poller recorded, with
  the same symbol (ignoring `-EQ`), side and quantity. Then the unresolved row is closed as `CANCELLED` ("superseded")
  and the real row is left untouched. Any other already-recorded id is refused.

**Choose `NOT_PLACED`** only when the order book (including rejected and cancelled orders) shows **no** such order and
the position did not change. The order becomes `REJECTED` with your note, and orders for that symbol are allowed again.
Never choose it just because the request timed out.

A resolved order cannot be resolved again (409). Copy-pipeline child orders are refused (409).

## 5. Resolution does not copy anything

Resolving only records what you found. It **does not place, cancel or replicate** any order, and does not touch the
Children.

- A Master LIMIT order that was really placed is normally picked up by the master order poller as an external order and
  copied to the Children within a poll interval. Check each Child's order book.
- If you resolved `PLACED` before the poller saw the order, the poller will never copy it (it ignores Jettone-originated
  orders). The Children were **not** copied; place their orders by hand if you want them to match.
- A resolved square-off does not square anything off. Check the Child's positions and square off again only if the
  position is still open.

## Copy orders sent to a Child

An ambiguous *copy* order is not resolved here (the endpoint refuses it). It stays `TIMEOUT` and the sweeper raises a
CRITICAL `TIMEOUT_UNRESOLVED` alert. Verify it in that Child's Alice Blue order book and act on the Child account by
hand; do not re-send it from Jettone.
