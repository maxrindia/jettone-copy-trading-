"""
Broker-aware identity of a Master order event.

Why this exists
---------------
A broker's native order id is only unique WITHIN that broker (Alice Blue ids look like "26090800085772", Zerodha and Angel One ids are
15-digit date-prefixed numbers, Groww ids are strings). The engine's first-line duplicate guard (`copy:seen:{master_order_id}:{seq}`)
and the idempotency key of a copy (`derive_copy_event_id`) were keyed on the native id alone. With one Master that was enough; with
Masters on different brokers - or two Masters of the same broker - an id that happens to coincide would make the second order look
like a redelivery of the first and be SILENTLY skipped.

The scope
---------
`master_scope(master_broker, master_account_id)` is the prefix that makes the key unambiguous:

* Alice Blue (and a missing/unknown broker, i.e. every event produced before this existed): the EMPTY string. Every Alice Blue key is
  therefore byte-for-byte what it always was, so idempotency across a restart or deploy is untouched (pinned by golden-value tests).
* Any other broker: "<broker>:<master account id>:". The account id is part of it, so two Masters of the same non-Alice broker cannot
  collide either.

Nothing here decides whether a broker may be a Master; that is the registry's and the pair gate's job.
"""
from typing import Any, Optional

ALICE_BLUE = "alicebluenew"


def master_scope(master_broker: Optional[str], master_account_id: Any) -> str:
    """"" for Alice Blue / unknown (unchanged legacy keys); "<broker>:<account>:" for every other broker."""
    broker = str(master_broker or "").strip().lower()
    if not broker or broker == ALICE_BLUE:
        return ""
    return f"{broker}:{str(master_account_id or '').strip()}:"
