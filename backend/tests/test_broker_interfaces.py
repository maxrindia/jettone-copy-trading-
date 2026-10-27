"""
R1 broker-neutral interfaces (docs/architecture/multi-broker-copy-trading, 02 §2.2).

Three separate questions, three kinds of test:

  1. Do the real clients expose the methods?        isinstance on the runtime_checkable Protocols. This proves
                                                     NAMES ONLY, and any mock passes it - never read it as more.
  2. Do their SIGNATURES conform?                    inspect.signature against one explicit contract, with every
                                                     known client difference pinned so a change is noticed.
  3. Do the pipeline's REAL call shapes bind?        an AST scan of the pipeline's place/modify/cancel calls,
                                                     bound against each client's signature.

Structural conformance never grants a role. Offline: mocked transports only, no broker request.
"""
import ast
import inspect
import textwrap
from decimal import Decimal
from pathlib import Path
from typing import List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import angel_helpers as A
import groww_helpers as G
from app.services.alice_blue.client import AliceBlueClient
from app.services.alice_blue.types import FundsData, OrderRecord, OrderRequest, OrderResult, OrderType, Position, ProductType
from app.services.angel_one.client import AngelOneClient
from app.services.brokers.interfaces import (
    ChildOrderExecutor,
    FundsProvider,
    InstrumentMapper,
    MasterOrderSource,
    OrderStatusProvider,
    PositionProvider,
)
from app.services.brokers.registry import can_be_master, get_broker_spec
from app.services.groww.client import GrowwClient

PIPELINE = Path(__file__).resolve().parents[1] / "app" / "services" / "copy_engine" / "pipeline.py"
CLIENT_CLASSES = {"alice": AliceBlueClient, "groww": GrowwClient, "angel": AngelOneClient}
SHARED = (MasterOrderSource, ChildOrderExecutor, PositionProvider, FundsProvider)


def alice(mock_redis):
    return AliceBlueClient(account_id="acc-test", user_id="AB00101", api_secret="secret",
                           redis=mock_redis, http_client=AsyncMock(spec=httpx.AsyncClient))


def groww(mock_redis):
    return G.make_client(mock_redis, G.FakeGroww())


def angel(mock_redis):
    return A.make_client(mock_redis, A.FakeAngel())


CLIENTS = {"alice": alice, "groww": groww, "angel": angel}


# ── 1. names only ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", list(CLIENTS))
@pytest.mark.parametrize("protocol", SHARED, ids=lambda p: p.__name__)
def test_every_existing_client_exposes_the_shared_method_names(mock_redis, name, protocol):
    """NAME presence only. Signatures are checked below."""
    assert isinstance(CLIENTS[name](mock_redis), protocol)


def test_isinstance_is_name_only_and_ignores_signatures():
    """Why section 2 exists. `runtime_checkable` looks the names up statically (Python 3.12), so a bare mock does
    NOT satisfy a Protocol - but a class with the right names and WRONG signatures does."""
    class WrongShapes:
        async def place_order(self): ...                       # no order parameter
        async def cancel_order(self, a, b, c, d, e): ...       # nonsense arity
        async def modify_order(self): ...

    assert isinstance(WrongShapes(), ChildOrderExecutor)
    assert not isinstance(MagicMock(), ChildOrderExecutor)     # bare mocks have no static attributes
    assert not isinstance(AsyncMock(), PositionProvider)


def test_a_partial_stub_does_not_satisfy_a_protocol():
    class OnlyFunds:
        async def get_funds(self):
            return None

    assert isinstance(OnlyFunds(), FundsProvider)
    assert not isinstance(OnlyFunds(), PositionProvider)
    assert not isinstance(OnlyFunds(), ChildOrderExecutor)
    assert not isinstance(object(), MasterOrderSource)


def test_only_groww_and_angel_map_instruments_themselves(mock_redis):
    """`get_available_long` already branches on a client class having resolve_instrument. Alice trusts the
    Master's token and resolves through module functions, so it is deliberately NOT an InstrumentMapper."""
    assert not isinstance(alice(mock_redis), InstrumentMapper)
    assert isinstance(groww(mock_redis), InstrumentMapper)
    assert isinstance(angel(mock_redis), InstrumentMapper)
    assert getattr(type(alice(mock_redis)), "resolve_instrument", None) is None


def test_satisfying_an_interface_never_grants_a_role(mock_redis, monkeypatch):
    """Groww's client has get_order_book, so it satisfies MasterOrderSource structurally. It still cannot be a
    Master: the role is decided by the registry gates and the contract's master_verified flag."""
    from app.core.config import settings
    monkeypatch.setattr(settings, "GROWW_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_MASTER_ENABLED", True)
    assert isinstance(groww(mock_redis), MasterOrderSource)
    assert isinstance(angel(mock_redis), MasterOrderSource)
    assert get_broker_spec("groww").capabilities.as_master is False
    assert can_be_master("groww") is False


# ── order book versus order lookup ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", ["alice", "angel"])
def test_alice_and_angel_expose_no_public_per_order_query(mock_redis, name):
    """Reading the order book (`get_order_book`) is not a query for one order. Alice Blue and Angel One have no PUBLIC method
    for the latter, so neither satisfies OrderStatusProvider. When one is added, update this test."""
    client = CLIENTS[name](mock_redis)
    assert not isinstance(client, OrderStatusProvider)
    public = {n for n in dir(type(client)) if not n.startswith("_")}
    assert not {n for n in public if "lookup" in n or "by_reference" in n or n in ("get_order", "get_order_status")}


def test_groww_exposes_the_documented_per_order_query(mock_redis):
    """Groww (GET /v1/order/detail + status-by-reference) has public, read-only per-order queries built from its documentation.
    They have not been observed live (profile ORDER_LOOKUP = VERIFIED_DOCS)."""
    assert isinstance(groww(mock_redis), OrderStatusProvider)


def test_groww_private_reference_lookup_still_backs_ambiguous_create_resolution(mock_redis):
    client = groww(mock_redis)
    assert callable(getattr(client, "_lookup_by_reference", None))


# ── 2. signature conformance ───────────────────────────────────────────────────────────────

NO_DEFAULT = inspect.Parameter.empty


def sig_params(cls, method):
    """Parameters of cls.method without `self`."""
    return list(inspect.signature(getattr(cls, method)).parameters.values())[1:]


# (name, default, annotation); name None = any name (place_order's parameter is `order` or `req`).
COMMON = {
    "place_order": ([(None, NO_DEFAULT, OrderRequest)], OrderResult),
    "cancel_order": ([("broker_order_id", NO_DEFAULT, str), ("symbol", "", str), ("exchange", "NSE", str)], OrderResult),
    "modify_order": ([
        ("broker_order_id", NO_DEFAULT, str), ("quantity", NO_DEFAULT, int), ("price", NO_DEFAULT, Decimal),
        ("trigger_price", Decimal("0"), Decimal), ("order_type", OrderType.LIMIT, OrderType),
        ("symbol", "", str), ("exchange", "NSE", str), ("product_type", "per-client", None),
    ], OrderResult),
    "get_order_book": ([], List[OrderRecord]),
    "get_positions_or_none": ([], Optional[List[Position]]),
    "get_funds": ([], FundsData),
    "resolve_instrument": ([("exchange", NO_DEFAULT, str), ("symbol", NO_DEFAULT, str)], Tuple[str, str]),
}
# Documented, unavoidable differences. Each is pinned exactly: a change must be a conscious one.
PRODUCT_DEFAULT = {"AliceBlueClient": ProductType.MIS, "GrowwClient": None, "AngelOneClient": None}
KNOWN_EXTRAS = {
    ("AngelOneClient", "cancel_order"): {"variety": "NORMAL"},
    ("AliceBlueClient", "get_funds"): {"max_cache_age_sec": 10},
}
PLAIN_METHODS = ("place_order", "cancel_order", "modify_order", "get_order_book", "get_positions_or_none", "get_funds")


def client_cases():
    cases = [(cls, m) for cls in CLIENT_CLASSES.values() for m in PLAIN_METHODS]
    cases += [(GrowwClient, "resolve_instrument"), (AngelOneClient, "resolve_instrument")]
    return cases


@pytest.mark.parametrize("cls,method", client_cases(), ids=lambda v: v if isinstance(v, str) else v.__name__)
def test_real_client_signature_matches_the_contract_with_pinned_differences(cls, method):
    expected, returns = COMMON[method]
    actual = sig_params(cls, method)
    extras = actual[len(expected):]
    for (name, default, annotation), param in zip(expected, actual):
        if name is not None:
            assert param.name == name, (cls.__name__, method)
        if name == "product_type":
            assert param.default == PRODUCT_DEFAULT[cls.__name__], (cls.__name__, "product_type default")
            continue
        assert param.default == default, (cls.__name__, method, param.name)
        assert param.annotation == annotation, (cls.__name__, method, param.name)
        assert param.kind in (param.POSITIONAL_OR_KEYWORD, param.POSITIONAL_ONLY)
    assert len(actual) >= len(expected), (cls.__name__, method, "missing parameters")
    assert {p.name: p.default for p in extras} == KNOWN_EXTRAS.get((cls.__name__, method), {}), (cls.__name__, method)
    assert all(p.default is not NO_DEFAULT for p in extras)                    # an extra is always optional
    assert inspect.signature(getattr(cls, method)).return_annotation == returns, (cls.__name__, method)
    assert inspect.iscoroutinefunction(getattr(cls, method))


def test_the_known_client_differences_are_exactly_these():
    """The documented list in interfaces.py, in one place."""
    assert PRODUCT_DEFAULT == {"AliceBlueClient": ProductType.MIS, "GrowwClient": None, "AngelOneClient": None}
    assert {(c, m): set(v) for (c, m), v in KNOWN_EXTRAS.items()} == {
        ("AngelOneClient", "cancel_order"): {"variety"}, ("AliceBlueClient", "get_funds"): {"max_cache_age_sec"}}
    # place_order's parameter name differs and nothing else about it does.
    assert [sig_params(c, "place_order")[0].name for c in CLIENT_CLASSES.values()] == ["order", "req", "req"]


PROTOCOL_METHODS = [(ChildOrderExecutor, "place_order"), (ChildOrderExecutor, "cancel_order"),
                    (ChildOrderExecutor, "modify_order"), (MasterOrderSource, "get_order_book"),
                    (PositionProvider, "get_positions_or_none"), (FundsProvider, "get_funds"),
                    (InstrumentMapper, "resolve_instrument")]


@pytest.mark.parametrize("protocol,method", PROTOCOL_METHODS, ids=lambda v: v if isinstance(v, str) else v.__name__)
def test_protocol_signatures_state_the_contract(protocol, method):
    expected, returns = COMMON[method]
    params = list(inspect.signature(getattr(protocol, method)).parameters.values())[1:]
    assert len(params) == len(expected)
    for (name, default, annotation), param in zip(expected, params):
        if name is not None:
            assert param.name == name
        if name == "product_type":
            assert param.default is None                                       # the common, non-Alice default
            continue
        assert param.default == default and param.annotation == annotation
    assert inspect.signature(getattr(protocol, method)).return_annotation == returns


def test_place_order_is_positional_only_in_the_protocol():
    (param,) = list(inspect.signature(ChildOrderExecutor.place_order).parameters.values())[1:]
    assert param.kind is param.POSITIONAL_ONLY


@pytest.mark.parametrize("cls,method", client_cases(), ids=lambda v: v if isinstance(v, str) else v.__name__)
def test_every_call_the_protocol_allows_binds_on_every_real_client(cls, method):
    """The protocol's minimal and fullest call shapes (positional-only as positional, the rest by keyword) must
    bind on the real signature. This is what a caller written against the Protocol will actually do."""
    protocol = next(p for p, m in PROTOCOL_METHODS if m == method)
    proto_params = list(inspect.signature(getattr(protocol, method)).parameters.values())[1:]
    real = inspect.signature(getattr(cls, method))
    positional = [None for p in proto_params if p.kind is p.POSITIONAL_ONLY]
    required = {p.name: None for p in proto_params if p.default is NO_DEFAULT and p.kind is not p.POSITIONAL_ONLY}
    full = {p.name: None for p in proto_params if p.kind is not p.POSITIONAL_ONLY}
    real.bind(None, *positional, **required)                                   # minimal call
    real.bind(None, *positional, **full)                                       # every parameter given


# ── 3. the pipeline's real call shapes ─────────────────────────────────────────────────────

def pipeline_calls():
    """(method, positional_count, keyword_names) for every place/modify/cancel call in the pipeline.
    `place_order` is passed to RetryPolicy.execute(client.place_order, order_req)."""
    tree = ast.parse(PIPELINE.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
            continue
        if node.func.attr in ("modify_order", "cancel_order"):
            found.append((node.func.attr, len(node.args), tuple(k.arg for k in node.keywords)))
        elif (node.func.attr == "execute" and node.args and isinstance(node.args[0], ast.Attribute)
              and node.args[0].attr == "place_order"):
            found.append(("place_order", len(node.args) - 1, tuple(k.arg for k in node.keywords)))
    return found


def test_the_scan_finds_the_pipelines_real_calls():
    methods = [m for m, _, _ in pipeline_calls()]
    assert methods.count("place_order") >= 3 and methods.count("modify_order") >= 3 and methods.count("cancel_order") >= 1


@pytest.mark.parametrize("cls", list(CLIENT_CLASSES.values()), ids=lambda c: c.__name__)
def test_every_pipeline_call_binds_on_every_real_client(cls):
    for method, n_positional, keywords in pipeline_calls():
        signature = inspect.signature(getattr(cls, method))
        signature.bind(None, *([None] * n_positional), **{k: None for k in keywords})   # TypeError = mismatch


def test_pipeline_supplies_product_type_when_modifying_and_alice_blue_ignores_it():
    """Every pipeline modify call supplies product_type (from the Child order's persisted product), and Alice Blue's
    modify_order body never reads the parameter (its MIS default is signature-only; no product field is sent). So supplying it
    changes nothing for Alice; Angel One, whose body requires a product (see the next test), now receives the order's own."""
    modify_calls = [kw for m, _, kw in pipeline_calls() if m == "modify_order"]
    assert len(modify_calls) == 3 and all("product_type" in kw for kw in modify_calls)

    tree = ast.parse(textwrap.dedent(inspect.getsource(AliceBlueClient.modify_order)))
    (func,) = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]
    reads = [n.lineno for stmt in func.body for n in ast.walk(stmt) if isinstance(n, ast.Name) and n.id == "product_type"]
    assert reads == []


def test_angel_one_modify_requires_a_product_and_the_pipeline_now_supplies_it():
    """Angel One's modify refuses without a product (it never defaults one), and the pipeline now passes the Child order's
    own persisted product at all three modify call sites (broker_product_for_modify; behaviour in
    test_modify_product_propagation.py). This was a recorded blocker (architecture documents 07 §7.2, 05 §5.5); the other Angel
    One blockers are unchanged."""
    src = inspect.getsource(AngelOneClient.modify_order)
    assert "product type is required to modify an Angel One order" in src
    assert sig_params(AngelOneClient, "modify_order")[-1].default is None
    assert all("product_type" in kw for m, _, kw in pipeline_calls() if m == "modify_order")
