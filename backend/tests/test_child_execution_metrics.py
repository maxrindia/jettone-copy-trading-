"""
R3 and the Demat-card counters (GET /accounts/metrics, which reads copy_trades).

What changes, and what must not:
  * members the strategy did NOT route (member trading disabled, inactive account) get no copy_trades row, so their
    counters stay at zero;
  * a Child rejected before the send now shows under `rejected_count` (it was invisible before R3);
  * an accepted LIMIT/SL child is SUBMITTED ("in flight"), counted under `needs_review_count` like an accepted MARKET
    child already was; it is no longer counted as `pending_count`;
  * an ambiguous send is TIMEOUT, under `cancelled_count`, exactly as before.
"""
import pytest

from test_child_execution_tracking import fan, make_event, run  # noqa: F401  (fan is a fixture)
from test_dashboard_api import AUTH_HEADER, api_client  # noqa: F401  (api_client is a fixture)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["LIMIT", "SL", "MARKET"])
async def test_the_demat_counters_after_a_mixed_fanout(api_client, db_session, fan, kind):
    await run(make_event(kind, f"R3_METRICS_{kind}"), fan["clients"])
    resp = await api_client.get("/api/v1/accounts/metrics", headers=AUTH_HEADER)
    assert resp.status_code == 200, resp.text
    metrics = resp.json()["metrics"]

    a, b, c = (metrics[str(fan[k].id)] for k in ("a", "b", "c"))
    assert (a["orders_count"], a["needs_review_count"], a["pending_count"], a["completed_count"]) == (1, 1, 0, 0)
    assert (b["orders_count"], b["rejected_count"], b["pending_count"]) == (1, 1, 0)
    assert (c["orders_count"], c["cancelled_count"], c["pending_count"]) == (1, 1, 0)
    for excluded in ("d", "e"):                                    # not routed: no copy_trades row, nothing counted
        m = metrics.get(str(fan[excluded].id))
        assert m is None or (m["orders_count"], m["rejected_count"], m["cancelled_count"], m["needs_review_count"]) == (0, 0, 0, 0)
