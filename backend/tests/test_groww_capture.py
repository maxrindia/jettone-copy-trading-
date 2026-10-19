"""The supervised Groww capture must never leak account values and must never run by accident."""
import json
import subprocess
import sys
from pathlib import Path

from app.services.angel_one.capture import redact_response
from groww_helpers import SBIN_ISIN, order_row, position_row

BACKEND = Path(__file__).resolve().parent.parent
SCRIPT = BACKEND / "scripts" / "groww_capture.py"


def test_order_list_values_are_redacted_but_the_vocabulary_is_kept():
    body = {"status": "SUCCESS", "payload": {"order_list": [
        order_row("GRW-SECRET-ID-1", "EXECUTED", 10, 10, ref="JTREFSECRET0000000001"),
        order_row("GRW-SECRET-ID-2", "ACKED", 10, 3, side="SELL", otype="SL_M", product="CNC"),
    ]}}
    out = redact_response(200, body)
    # the order list sits one level down (payload); the top-level shape is what is captured
    text = json.dumps(out)
    assert out["top_level_keys"] == ["payload", "status"] and out["data_kind"] in ("none", "object")
    for leaked in ("GRW-SECRET-ID-1", "GRW-SECRET-ID-2", "JTREFSECRET0000000001", "SBIN"):
        assert leaked not in text


def test_payload_fields_are_captured_as_types_with_enum_values_only():
    payload = order_row("GRW-SECRET-ID-1", "EXECUTED", 10, 10)
    out = redact_response(200, {"status": "SUCCESS", "data": payload})
    text = json.dumps(out)
    assert out["fields"]["order_status"] == {"type": "string", "value": "EXECUTED"}
    assert out["fields"]["product"] == {"type": "string", "value": "MIS"}
    assert out["fields"]["transaction_type"]["value"] == "BUY" and out["fields"]["segment"]["value"] == "CASH"
    for leaked in ("GRW-SECRET-ID-1", "SBIN", "2026-10-16T09:31:00"):
        assert leaked not in text
    assert out["fields"]["quantity"] == {"type": "number"} and out["fields"]["groww_order_id"] == {"type": "string"}
    assert out["fields"]["remark"] == {"type": "string"} and out["fields"]["trading_symbol"] == {"type": "string"}


def test_position_rows_and_tokens_are_redacted():
    rows = {"status": "SUCCESS", "data": [position_row(), position_row("TCS", qty=-4)]}
    out = redact_response(200, rows)
    text = json.dumps(out)
    assert SBIN_ISIN not in text and "SBIN" not in text and out["row_count"] == 2
    assert out["rows"][0]["quantity"] == {"type": "number"}
    assert out["observed_vocabulary"]["product"] == ["MIS"] and out["observed_vocabulary"]["segment"] == ["CASH"]
    tok = redact_response(200, {"token": "eyJ.secret.token", "tokenRefId": "ref-1", "sessionName": "mine",
                                "expiry": "2026-10-17T06:00:00", "isActive": True})
    assert "eyJ.secret.token" not in json.dumps(tok) and "ref-1" not in json.dumps(tok) and "mine" not in json.dumps(tok)


def test_failure_responses_keep_only_their_shape():
    out = redact_response(400, {"status": "FAILURE", "error": {"code": "GA001", "message": "Bad request for 4242"}})
    text = json.dumps(out)
    assert out["top_level_keys"] == ["error", "status"] and "4242" not in text and "Bad request" not in text


def test_script_is_a_dry_run_by_default_and_refuses_without_authorization():
    env = {"PATH": "", "SYSTEMROOT": "C:\\Windows"}      # no Groww credentials at all
    base = [sys.executable, "-I", str(SCRIPT)]
    dry = subprocess.run(base, capture_output=True, text=True, env=env, timeout=60)
    assert dry.returncode == 0 and "nothing contacted" in dry.stdout.lower()
    no_auth = subprocess.run(base + ["--execute"], capture_output=True, text=True, env=env, timeout=60)
    assert no_auth.returncode == 1 and "--i-am-authorized" in no_auth.stdout
    no_env = subprocess.run(base + ["--execute", "--i-am-authorized"], capture_output=True, text=True, env=env, timeout=60)
    assert no_env.returncode == 1 and "missing environment" in no_env.stdout.lower()


def test_script_only_ever_issues_read_requests():
    src = SCRIPT.read_text(encoding="utf-8")
    calls = src.split("READ_ONLY_CALLS = ", 1)[1].split("\n)\n", 1)[0]
    for forbidden in ("order_create", "order_modify", "order_cancel", "token"):
        assert forbidden not in calls
    # every data request goes through the read-only helper with GET
    assert src.count('client._call("read", "GET"') == 1
    assert '"POST"' not in src and "place_order" not in src and "cancel_order" not in src and "modify_order" not in src


# ── Groww nests its data under `payload`; the capture must still learn the shapes it exists to learn ──

def _page(n, statuses=("OPEN", "EXECUTED", "REJECTED")):
    return [order_row(f"GRW-SECRET-{i}", statuses[i % len(statuses)], 10, 10 if statuses[i % len(statuses)] == "EXECUTED" else (4 if i % 5 == 0 else 0))
            for i in range(n)]


def test_a_payload_order_list_reports_its_true_row_count_and_status_vocabulary():
    out = redact_response(200, {"status": "SUCCESS", "payload": {"order_list": _page(100)}})
    lst = out["lists"]["order_list"]
    assert out["data_source"] == "payload"
    assert lst["row_count"] == 100 and len(lst["rows"]) == 50            # a full page is visible even though only 50 rows are described
    assert set(lst["observed_vocabulary"]["order_status"]) == {"OPEN", "EXECUTED", "REJECTED"}
    assert lst["rows"][0]["filled_quantity"] == {"type": "number"}       # field NAMES and types are learned
    assert "GRW-SECRET" not in json.dumps(out) and "SBIN" not in json.dumps(out)


def test_an_empty_list_is_distinguishable_from_a_missing_list():
    empty = redact_response(200, {"status": "SUCCESS", "payload": {"order_list": []}})
    missing = redact_response(200, {"status": "SUCCESS", "payload": {}})
    assert empty["lists"]["order_list"]["row_count"] == 0
    assert "lists" not in missing                                        # the undocumented 'no orders' shape is observable either way


def test_paging_counters_are_kept_but_nothing_else_numeric():
    out = redact_response(200, {"status": "SUCCESS", "payload": {"order_list": [], "page": 0, "page_size": 100, "total": 3,
                                                                   "balance": 12345, "has_next": False, "page_token": "abc"}})
    assert out["paging_values"] == {"page": 0, "page_size": 100, "total": 3, "has_next": False}
    assert "12345" not in json.dumps(out) and "abc" not in json.dumps(out)
    odd = redact_response(200, {"status": "SUCCESS", "payload": {"page": "1", "total": 1.5, "count": 10 ** 9}})
    assert "paging_values" not in odd                                    # strings, floats and huge numbers are not kept


def test_payload_positions_and_margin_are_described_without_values():
    pos = redact_response(200, {"status": "SUCCESS", "payload": {"positions": [position_row(), position_row("TCS", qty=-4)]}})
    assert pos["lists"]["positions"]["row_count"] == 2 and SBIN_ISIN not in json.dumps(pos)
    margin = redact_response(200, {"status": "SUCCESS", "payload": {"clear_cash": 1000000.0, "net_margin_used": 5.0, "collateral_available": 0}})
    assert margin["fields"]["clear_cash"] == {"type": "number"} and "1000000" not in json.dumps(margin)


def test_the_existing_data_shapes_are_unchanged():
    out = redact_response(200, {"status": True, "data": [{"status": "open", "orderid": "X1", "quantity": "5"}]})
    assert out["row_count"] == 1 and "lists" not in out and "data_source" not in out
