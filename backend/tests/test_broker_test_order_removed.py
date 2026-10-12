"""
Audit P0 #3 regression guard: backend/app/api/broker.py::place_test_order()
(POST /broker/test-order) placed a real broker order with no
ENGINE_LIVE_TRADING_ENABLED gate, no funds/RiskEngine check, and no Order
persistence of any kind. It had zero callers anywhere in the repo (no
frontend usage, no test usage, no other backend reference) and no relation
to the copy-trading domain, so the smallest safe fix is removing it
entirely rather than reproducing /broker/manual-order's full safety
machinery a second time for an unused endpoint.

This test proves the endpoint and its request schema are both gone, and
that the route is genuinely unreachable (404) even with a valid internal
token - regardless of ENGINE_LIVE_TRADING_ENABLED's value, since the route
no longer exists at all. No live trading is enabled and no real broker
calls are made anywhere in this file.
"""
from app.main import app


def test_broker_test_order_route_no_longer_registered():
    """Check the actual FastAPI route table, not an HTTP status code - a 404
    response can also mean 'account not found' from a route that still
    exists, which would make the test pass for the wrong reason. Asserting
    directly against app.routes proves the route itself is gone."""
    matching = [
        route
        for route in app.routes
        if getattr(route, "path", "") == "/api/v1/broker/test-order"
    ]
    assert matching == [], f"POST /broker/test-order must not be registered, found: {matching}"


def test_place_test_order_symbol_removed_from_broker_module():
    import app.api.broker as broker_module

    assert not hasattr(broker_module, "place_test_order")
    assert not hasattr(broker_module, "TestOrderRequest")
