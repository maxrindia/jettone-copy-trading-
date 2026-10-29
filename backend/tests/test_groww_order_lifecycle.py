"""
The supervised Groww order-lifecycle script: fake transports only. No Groww, no network, no database, no Redis, no real order.

What is proven here is the SCRIPT'S behaviour - that it refuses without complete operator inputs, enforces the risk cap, never resends an
ambiguous placement, sends at most one cancel and never trusts it, redacts its output, and reports fills and mismatches. It is not
evidence about Groww: the fake answers in the shapes the documentation describes.
"""
import argparse
import ast
import importlib.util
import json
import socket
import sys
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from groww_helpers import CSV_HEADER, CSV_ROWS, FakeGroww, ok, order_row, position_row

BACKEND = Path(__file__).resolve().parent.parent
SCRIPT = BACKEND / "scripts" / "groww_order_lifecycle.py"

spec = importlib.util.spec_from_file_location("groww_order_lifecycle", SCRIPT)
lifecycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lifecycle)

PUBLIC_IP = "8.8.8.8"          # a globally routable address, used ONLY as a string by tests that must pass the live-mode IP check
OID = "GRW-NEW-1"
API_KEY, API_SECRET = "KEY-SECRET-123456", "SECRET-VALUE-789012"
# 203.0.113.0/24 is a DOCUMENTATION range (RFC 5737): a placeholder used only with fake transports, never an IP to test with.
GOOD = dict(exchange="NSE", symbol="SBIN", quantity="1", product="CNC", limit_price="780", modify_price="781",
            reference_price="800", max_notional="1000", risk_limit="1000", static_ip="203.0.113.10")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    local = {"127.0.0.1", "::1", "localhost", None, ""}

    def guard(*a, **k):
        raise AssertionError("network access attempted")

    real_connect = socket.socket.connect
    monkeypatch.setattr(socket.socket, "connect",
                        lambda self, address, *a, **k: real_connect(self, address, *a, **k)
                        if (address[0] if isinstance(address, tuple) else address) in local else guard())
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k: guard() if host not in local else [])


@pytest.fixture
def master_csv(tmp_path):
    path = tmp_path / "instruments.csv"
    path.write_text("\n".join([CSV_HEADER] + CSV_ROWS), encoding="utf-8")
    return str(path)


def ns(master_csv=None, **over):
    values = {**GOOD, **over}
    if master_csv is not None and "instrument_master" not in over:
        values["instrument_master"] = master_csv
    values.setdefault("instrument_master", None)
    return argparse.Namespace(**values)


def argv(master_csv, **over):
    values = {**GOOD, "instrument_master": master_csv, **over}
    out = []
    for key, value in values.items():
        if value is not None:
            out += ["--" + key.replace("_", "-"), str(value)]
    return out


# ── a stateful fake of the one order the script manages ──────────────────────────────────────────────────────────────────────

class LifecycleGroww(FakeGroww):
    def __init__(self):
        super().__init__()
        self.token_response = (200, {"token": "TOK-SECRET-1", "tokenRefId": "ref", "sessionName": "n",
                                     "expiry": "2099-01-01T06:00:00", "isActive": True})
        self.status, self.filled, self.price, self.qty = "OPEN", 0, 780, 1
        self.apply_modify = self.apply_cancel = True
        self.reference_found = True
        self.on_cancel = None
        self.create_response = ok({"groww_order_id": OID, "order_status": "OPEN", "order_reference_id": "x"})
        self.cancel_response = ok({"groww_order_id": OID, "order_status": "CANCELLATION_REQUESTED"})
        self.modify_response = ok({"groww_order_id": OID, "order_status": "MODIFICATION_REQUESTED"})

    def count(self, path):
        return sum(1 for r in self.requests if r.url.path == path)

    def row(self):
        return order_row(OID, self.status, self.qty, self.filled, price=self.price, product="CNC")

    async def __call__(self, request):
        path = request.url.path
        if path == "/v1/order/modify" and self.modify_response[0] == 200 and self.apply_modify:
            self.price = json.loads(request.content)["price"]
        if path == "/v1/order/cancel" and self.cancel_response[0] == 200 and self.apply_cancel:
            self.status = "CANCELLED"
            if self.on_cancel:
                self.on_cancel()
        if path.startswith("/v1/order/detail/"):
            self.detail_response = ok(self.row())
        if path.startswith("/v1/order/status/reference/") and self.reference_found:
            self.reference_response = ok({"groww_order_id": OID})
        return await super().__call__(request)


class Session:
    """Runs the script's `execute` against a LifecycleGroww and collects what it printed and wrote."""

    def __init__(self, fake, master_csv, tmp_path, answers=None, **over):
        self.fake, self.out = fake, str(tmp_path / "report.json")
        self.cfg = lifecycle.build_config(ns(master_csv, **over))
        self.answers = [self.cfg.confirm_place, self.cfg.confirm_modify] if answers is None else list(answers)
        self.said = []
        self.prompts = []

    def ask(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError("no more answers")
        return self.answers.pop(0)

    async def run(self):
        async def no_sleep(_):
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(self.fake)) as http:
            code = await lifecycle.execute(self.cfg, {"GROWW_API_KEY": API_KEY, "GROWW_API_SECRET": API_SECRET}, self.out,
                                           input_fn=self.ask, http_client=http, sleep=no_sleep, say=self.said.append)
        report = json.loads(Path(self.out).read_text(encoding="utf-8")) if Path(self.out).exists() else {}
        return code, report

    @property
    def text(self):
        return "\n".join(self.said) + json.dumps(self.prompts)


async def go(master_csv, tmp_path, configure=None, **kw):
    fake = LifecycleGroww()
    if configure:
        configure(fake)
    session = Session(fake, master_csv, tmp_path, **kw)
    code, report = await session.run()
    return fake, session, code, report


# ── dry run is the default ───────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_default_is_a_dry_run_that_builds_no_client(monkeypatch, master_csv, capsys):
    import app.services.groww.client as groww_client

    def boom(*a, **k):
        raise AssertionError("a client was built in a dry run")

    monkeypatch.setattr(groww_client, "GrowwClient", boom)
    assert lifecycle.main([]) == 0
    assert "nothing contacted" in capsys.readouterr().out.lower()
    assert lifecycle.main(argv(master_csv)) == 0                                  # complete inputs, still no --execute
    out = capsys.readouterr().out
    assert "Plan:" in out and "nothing contacted" in out.lower() and "NOT guaranteed to stay unfilled" in out


def test_the_limitation_is_stated_in_the_script_and_its_plan(master_csv):
    assert "NOT guaranteed" in lifecycle.LIMITATION
    assert "NOT guaranteed" in SCRIPT.read_text(encoding="utf-8")
    assert "NOT guaranteed" in lifecycle.plan_text(lifecycle.build_config(ns(master_csv)))


# ── refusal without complete, valid inputs ───────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", lifecycle.MANDATORY)
def test_a_missing_input_is_refused_on_a_live_run(name, master_csv, capsys):
    values = {**GOOD, "instrument_master": master_csv}
    values[name] = None
    args = []
    for key, value in values.items():
        if value is not None:
            args += ["--" + key.replace("_", "-"), str(value)]
    code = lifecycle.main(args + ["--execute", "--i-am-authorized"],
                          env={"GROWW_API_KEY": API_KEY, "GROWW_API_SECRET": API_SECRET}, stdin_is_tty=True)
    out = capsys.readouterr().out
    assert code == 1 and "--" + name.replace("_", "-") in out


def test_a_live_run_needs_authorisation_credentials_and_a_terminal(master_csv, capsys):
    env = {"GROWW_API_KEY": API_KEY, "GROWW_API_SECRET": API_SECRET}
    assert lifecycle.main(argv(master_csv, static_ip=PUBLIC_IP) + ["--execute"], env=env, stdin_is_tty=True) == 1
    assert "--i-am-authorized" in capsys.readouterr().out
    assert lifecycle.main(argv(master_csv, static_ip=PUBLIC_IP) + ["--execute", "--i-am-authorized"], env={}, stdin_is_tty=True) == 1
    assert "missing environment" in capsys.readouterr().out.lower()
    assert lifecycle.main(argv(master_csv, static_ip=PUBLIC_IP) + ["--execute", "--i-am-authorized"], env=env, stdin_is_tty=False) == 1
    assert "interactive terminal" in capsys.readouterr().out


@pytest.mark.parametrize("over,fragment", [
    (dict(quantity="2"), "exactly 1"),
    (dict(quantity="one"), "integer 1"),
    (dict(limit_price="800"), "BELOW the reference"),
    (dict(limit_price="900"), "BELOW the reference"),
    (dict(modify_price="800"), "BELOW the reference"),
    (dict(modify_price="780"), "must differ"),
    (dict(max_notional="700"), "exceeds the cap"),
    (dict(risk_limit="700"), "exceeds the cap"),
    (dict(max_notional="5000", risk_limit="700"), "exceeds the cap"),
    (dict(limit_price="-1"), "positive"),
    (dict(limit_price="0"), "positive"),
    (dict(limit_price="NaN"), "positive"),
    (dict(limit_price="Infinity"), "positive"),
    (dict(limit_price="abc"), "not a number"),
    (dict(product="NRML"), "product must be"),
    (dict(exchange="NFO"), "exchange must be"),
    (dict(symbol="bad symbol!"), "trading symbol"),
    (dict(static_ip="not-an-ip"), "valid IP"),
])
def test_invalid_inputs_and_risk_limit_breaches_are_refused(over, fragment, master_csv):
    with pytest.raises(lifecycle.Refusal) as err:
        lifecycle.build_config(ns(master_csv, **over))
    assert fragment in str(err.value)


def test_the_cap_is_the_smaller_of_the_two_operator_limits(master_csv):
    cfg = lifecycle.build_config(ns(master_csv, max_notional="900", risk_limit="790"))
    assert cfg.cap == Decimal("790")
    with pytest.raises(lifecycle.Refusal):
        lifecycle.build_config(ns(master_csv, max_notional="900", risk_limit="779", limit_price="780"))


def test_there_are_no_defaults_for_any_risk_input():
    defaults = {a.dest: a.default for a in lifecycle._parser()._actions if a.dest in lifecycle.MANDATORY}
    assert set(defaults) == set(lifecycle.MANDATORY) and all(v is None for v in defaults.values())


def test_the_instrument_must_resolve_and_allow_buy(master_csv, tmp_path):
    cfg = lifecycle.build_config(ns(master_csv))
    assert lifecycle.load_store(cfg) is not None
    for over, fragment in ((dict(symbol="NOSUCH"), "does not resolve"), (dict(symbol="NIFTY26OCT23500CE"), "does not resolve"),
                           (dict(instrument_master=str(tmp_path / "missing.csv")), "does not exist")):
        with pytest.raises(lifecycle.Refusal) as err:
            lifecycle.load_store(lifecycle.build_config(ns(master_csv, **over)))
        assert fragment in str(err.value)
    nobuy = tmp_path / "nobuy.csv"
    nobuy.write_text(CSV_HEADER + "\nNSE,1,NOBUY,NSE-NOBUY,No Buy,EQ,CASH,EQ,INE0,,,,,1,0.05,0,False,False,True\n", encoding="utf-8")
    with pytest.raises(lifecycle.Refusal) as err:
        lifecycle.load_store(lifecycle.build_config(ns(str(nobuy), symbol="NOBUY")))
    assert "BUY" in str(err.value)


# ── the live sequence, against the fake ──────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_clean_run_places_looks_up_modifies_and_cancels_once_each(master_csv, tmp_path):
    fake, session, code, report = await go(master_csv, tmp_path)
    assert code == 0, session.said
    assert (fake.count("/v1/order/create"), fake.count("/v1/order/modify"), fake.count("/v1/order/cancel")) == (1, 1, 1)
    assert fake.status == "CANCELLED" and float(fake.price) == 781.0
    outcomes = {s["step"]: s["outcome"] for s in report["steps"]}
    assert outcomes["lookup by client reference"] == "OK" and outcomes["modify confirmed"] == "OK"
    assert outcomes["cancel confirmed"] == "OK" and outcomes["reconciliation"] == "OK"
    assert report["reconciliation"] == {"position_unchanged": True, "funds_unchanged": True}
    assert report["final_order_state"] == "CANCELLED"
    body = json.loads([r for r in fake.requests if r.url.path == "/v1/order/create"][0].content)
    assert body["transaction_type"] == "BUY" and body["quantity"] == 1 and body["order_type"] == "LIMIT" and body["product"] == "CNC"


@pytest.mark.asyncio
async def test_nothing_sensitive_is_printed_or_written(master_csv, tmp_path):
    fake, session, code, report = await go(master_csv, tmp_path)
    blob = session.text + json.dumps(report) + Path(session.out).read_text(encoding="utf-8")
    for secret in (API_KEY, API_SECRET, "TOK-SECRET-1", OID, "1000000", "203.0.113.10"):
        assert secret not in blob, secret
    assert "GRW...W-1" in session.text                                              # the order id appears only masked


@pytest.mark.asyncio
async def test_no_confirmation_means_no_order(master_csv, tmp_path):
    fake, session, code, report = await go(master_csv, tmp_path, answers=["PLACE something else"])
    assert code == 1 and fake.count("/v1/order/create") == 0
    fake, session, code, _ = await go(master_csv, tmp_path, answers=[])             # no terminal answer at all
    assert code != 0 and fake.count("/v1/order/create") == 0


@pytest.mark.asyncio
async def test_a_declined_modify_is_skipped_but_the_order_is_still_cancelled_once(master_csv, tmp_path):
    session = Session(LifecycleGroww(), master_csv, tmp_path)
    session.answers = [session.cfg.confirm_place, "no"]
    code, report = await session.run()
    assert session.fake.count("/v1/order/modify") == 0 and session.fake.count("/v1/order/cancel") == 1
    assert code == 6                                                               # the modify was never observed


@pytest.mark.asyncio
async def test_a_rejected_placement_leaves_nothing_to_manage(master_csv, tmp_path):
    def configure(f):
        f.create_response = (400, {"status": "FAILURE"})
    fake, session, code, report = await go(master_csv, tmp_path, configure)
    assert code == 6 and fake.count("/v1/order/create") == 1
    assert fake.count("/v1/order/modify") == 0 and fake.count("/v1/order/cancel") == 0


@pytest.mark.asyncio
async def test_an_ambiguous_placement_is_never_resent_or_cancelled(master_csv, tmp_path):
    def configure(f):
        f.create_exception = httpx.ReadTimeout("no answer after send")
        f.reference_found = False
        f.reference_response = (404, {"status": "FAILURE"})
    fake, session, code, report = await go(master_csv, tmp_path, configure)
    assert code == 3
    assert fake.count("/v1/order/create") == 1                                     # exactly one send, never a second
    assert fake.count("/v1/order/modify") == 0 and fake.count("/v1/order/cancel") == 0
    assert "MANUAL" in session.text.upper() or "BY HAND" in session.text.upper()
    assert report["verdict"].startswith("placement ambiguous")


@pytest.mark.asyncio
async def test_an_ambiguous_placement_found_by_reference_continues_without_a_resend(master_csv, tmp_path):
    def configure(f):
        f.create_exception = httpx.ReadTimeout("no answer after send")
    fake, session, code, report = await go(master_csv, tmp_path, configure)
    assert fake.count("/v1/order/create") == 1
    assert fake.count("/v1/order/cancel") == 1


@pytest.mark.asyncio
async def test_a_fill_is_reported_loudly_and_nothing_is_modified_or_cancelled(master_csv, tmp_path):
    def configure(f):
        f.status, f.filled = "EXECUTED", 1
    fake, session, code, report = await go(master_csv, tmp_path, configure)
    assert code == 4 and report["final_order_state"] == "FILLED"
    assert fake.count("/v1/order/modify") == 0 and fake.count("/v1/order/cancel") == 0
    assert "square" in session.text.lower()


@pytest.mark.asyncio
async def test_a_fill_during_the_modify_stops_the_cancel(master_csv, tmp_path):
    class FillOnModify(LifecycleGroww):
        async def __call__(self, request):
            response = await super().__call__(request)
            if request.url.path == "/v1/order/modify":
                self.status, self.filled = "EXECUTED", 1
            return response

    session = Session(FillOnModify(), master_csv, tmp_path)
    code, report = await session.run()
    assert code == 4 and session.fake.count("/v1/order/cancel") == 0


@pytest.mark.asyncio
async def test_a_part_fill_stops_the_sequence_for_manual_handling(master_csv, tmp_path):
    def configure(f):
        f.qty, f.filled, f.status = 2, 1, "ACKED"
    fake, session, code, report = await go(master_csv, tmp_path, configure)
    assert code == 4 and report["final_order_state"] == "PARTIAL"
    assert fake.count("/v1/order/modify") == 0 and fake.count("/v1/order/cancel") == 0       # nothing further is sent
    assert "by hand" in session.text.lower() and "part-filled" in session.text.lower()


@pytest.mark.asyncio
async def test_an_exception_during_the_send_is_treated_as_an_unknown_placement(master_csv, tmp_path, monkeypatch):
    import app.services.groww.client as groww_client

    async def explode(self, req):
        raise RuntimeError("adapter blew up mid-send")

    monkeypatch.setattr(groww_client.GrowwClient, "place_order", explode)
    fake, session, code, report = await go(master_csv, tmp_path)
    assert code == 3 and fake.count("/v1/order/create") == 0
    assert fake.count("/v1/order/modify") == 0 and fake.count("/v1/order/cancel") == 0
    assert "by hand" in session.text.lower()


def test_order_ids_are_never_printed_in_full():
    assert lifecycle._mask("GRW1") == "***" and lifecycle._mask("GRW-NEW") == "***"
    assert lifecycle._mask("GMK39038RDS4") == "GMK...DS4" and "39038" not in lifecycle._mask("GMK39038RDS4")


def test_a_live_run_refuses_a_documentation_or_private_ip(master_csv, capsys):
    env = {"GROWW_API_KEY": API_KEY, "GROWW_API_SECRET": API_SECRET}
    for placeholder in ("203.0.113.10", "198.51.100.7", "192.0.2.1", "10.0.0.5", "127.0.0.1", "192.168.1.9"):
        code = lifecycle.main(argv(master_csv, static_ip=placeholder) + ["--execute", "--i-am-authorized"], env=env, stdin_is_tty=True)
        out = capsys.readouterr().out
        assert code == 1 and "not a public address" in out and "placeholder" in out, placeholder
    assert "203.0.113" in SCRIPT.read_text(encoding="utf-8")                          # the docstring names it as a placeholder


@pytest.mark.asyncio
async def test_a_failed_cancel_is_sent_once_and_reported_as_manual(master_csv, tmp_path):
    def configure(f):
        f.cancel_response = (400, {"status": "FAILURE"})
        f.apply_cancel = False
    fake, session, code, report = await go(master_csv, tmp_path, configure)
    assert code == 5 and fake.count("/v1/order/cancel") == 1                       # never repeated
    assert "CANCEL BY HAND" in session.text.upper()


@pytest.mark.asyncio
async def test_a_cancel_that_says_success_but_did_not_take_effect_is_not_believed(master_csv, tmp_path):
    def configure(f):
        f.apply_cancel = False                                                     # the response says SUCCESS; the order stays OPEN
    fake, session, code, report = await go(master_csv, tmp_path, configure)
    assert code == 5 and fake.count("/v1/order/cancel") == 1
    assert {s["step"]: s["outcome"] for s in report["steps"]}["cancel confirmed"] == "NOT CONFIRMED"


@pytest.mark.asyncio
async def test_a_modify_that_says_success_but_changed_nothing_is_not_believed(master_csv, tmp_path):
    def configure(f):
        f.apply_modify = False
    fake, session, code, report = await go(master_csv, tmp_path, configure)
    assert code == 6 and fake.count("/v1/order/cancel") == 1
    assert {s["step"]: s["outcome"] for s in report["steps"]}["modify confirmed"] == "NOT CONFIRMED"


@pytest.mark.asyncio
async def test_an_unreadable_order_state_cancels_once_and_never_assumes_success(master_csv, tmp_path):
    def configure(f):
        f.reference_found = False
        f.reference_response = (404, {"status": "FAILURE"})
        f.apply_cancel = False
    class Unreadable(LifecycleGroww):
        async def __call__(self, request):
            if request.url.path.startswith("/v1/order/detail/"):
                self.requests.append(request)
                return httpx.Response(500, json={"status": "FAILURE"})
            return await super().__call__(request)
    fake = Unreadable()
    configure(fake)
    session = Session(fake, master_csv, tmp_path)
    code, report = await session.run()
    assert code == 5 and fake.count("/v1/order/cancel") == 1 and fake.count("/v1/order/modify") == 0


@pytest.mark.asyncio
async def test_a_reconciliation_mismatch_is_reported(master_csv, tmp_path):
    def configure(f):
        f.on_cancel = lambda: setattr(f, "margin_response", ok({"clear_cash": 999000.0, "net_margin_used": 0.0, "collateral_available": 0.0}))
    fake, session, code, report = await go(master_csv, tmp_path, configure)
    assert code == 6 and report["reconciliation"]["funds_unchanged"] is False
    assert fake.count("/v1/order/cancel") == 1


@pytest.mark.asyncio
async def test_a_changed_position_is_a_reconciliation_mismatch(master_csv, tmp_path):
    def configure(f):
        f.on_cancel = lambda: setattr(f, "positions_response", ok({"positions": [position_row("SBIN", product="CNC", qty=1)]}))
    fake, session, code, report = await go(master_csv, tmp_path, configure)
    assert code == 6 and report["reconciliation"]["position_unchanged"] is False


@pytest.mark.asyncio
async def test_a_failed_login_stops_before_any_order_request(master_csv, tmp_path):
    def configure(f):
        f.token_response = (401, {"status": "FAILURE"})
    fake, session, code, report = await go(master_csv, tmp_path, configure)
    assert code == 2
    assert not any(r.url.path.startswith("/v1/order/") for r in fake.requests)
    assert API_KEY not in session.text and API_SECRET not in session.text


# ── structural guarantees about the script itself ────────────────────────────────────────────────────────────────────────────

def test_the_script_has_one_send_site_for_each_order_action():
    src = SCRIPT.read_text(encoding="utf-8")
    assert src.count("client.place_order(") == 1 and src.count("client.cancel_order(") == 1 and src.count("client.modify_order(") == 1
    assert 'client._call("read", "GET"' in src and '"POST"' not in src


def test_the_script_never_touches_the_engine_database_redis_or_gates():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    forbidden = ("app.db", "app.core.database", "app.services.copy_engine", "app.services.runtime", "app.services.brokers.pairs",
                 "app.services.brokers.registry", "app.services.brokers.profiles", "app.core.config")
    assert not [m for m in imported if any(m == f or m.startswith(f + ".") for f in forbidden)]
    code = SCRIPT.read_text(encoding="utf-8").split('"""', 2)[2]                    # everything after the module docstring
    for name in ("SUPPORTED_PAIRS", "live_verified", "master_verified", "AsyncSessionLocal", "ENGINE_LIVE_TRADING_ENABLED", "GROWW_ENABLED"):
        assert name not in code, name


def test_no_loop_re_sends_an_order_request():
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    for loop in (n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.While, ast.AsyncFor))):
        called = {n.func.attr for n in ast.walk(loop) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert not called & {"place_order", "modify_order", "cancel_order"}, ast.dump(loop)[:80]


@pytest.mark.asyncio
async def test_the_cancel_step_cannot_send_a_second_cancel(master_csv):
    """Called twice (as an error path could), the cancel step still sends exactly one cancel request."""
    from unittest.mock import AsyncMock, MagicMock

    from app.services.alice_blue.types import OrderResultCategory

    cfg = lifecycle.build_config(ns(master_csv))
    client = MagicMock()
    client.cancel_order = AsyncMock(return_value=MagicMock(category=OrderResultCategory.SUCCESS))
    client.get_order = AsyncMock(return_value=None)                                # state stays unreadable: never confirmed
    run = lifecycle.Run(lambda _: None)
    run.order_id = "GRW-X"

    async def no_sleep(_):
        return None

    await lifecycle._cancel_once(run, client, cfg, no_sleep)
    await lifecycle._cancel_once(run, client, cfg, no_sleep)
    assert client.cancel_order.await_count == 1
    assert run.cancel_confirmed is False and run.exit_code == 5                    # unreadable is never read as cancelled
    run2 = lifecycle.Run(lambda _: None)
    run2.order_id, run2.final_state = "GRW-X", "PARTIAL"
    await lifecycle._cancel_once(run2, client, cfg, no_sleep)
    assert client.cancel_order.await_count == 1                                    # a part-filled order is never blindly cancelled


@pytest.mark.asyncio
async def test_the_cancel_step_does_nothing_for_an_order_already_known_to_be_filled(master_csv):
    from unittest.mock import AsyncMock, MagicMock

    cfg = lifecycle.build_config(ns(master_csv))
    client = MagicMock()
    client.cancel_order = AsyncMock()
    run = lifecycle.Run(lambda _: None)
    run.order_id, run.final_state = "GRW-X", "FILLED"
    await lifecycle._cancel_once(run, client, cfg, AsyncMock())
    assert client.cancel_order.await_count == 0
