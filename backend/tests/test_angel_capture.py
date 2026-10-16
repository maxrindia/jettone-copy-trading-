"""The supervised capture must never leak account values, and must never run by accident."""
import json
import subprocess
import sys
from pathlib import Path

from app.services.angel_one.capture import redact_response

BACKEND = Path(__file__).resolve().parent.parent


def test_values_are_redacted_but_enum_vocabulary_is_kept():
    body = {"status": True, "message": "SUCCESS", "data": [
        {"orderid": "201020000000080", "tradingsymbol": "SBIN-EQ", "symboltoken": "3045", "status": "complete",
         "producttype": "INTRADAY", "transactiontype": "BUY", "quantity": "10", "averageprice": 812.35,
         "text": "client 9876 rejected", "exchange": "NSE", "clientcode": "A123"},
        {"orderid": "2", "status": "rejected", "producttype": "DELIVERY", "text": "x"},
    ]}
    out = redact_response(200, body)
    text = json.dumps(out)
    for leaked in ("201020000000080", "SBIN-EQ", "3045", "812.35", "9876", "A123", "SUCCESS", "client 9876"):
        assert leaked not in text, leaked
    assert out["observed_vocabulary"]["status"] == ["complete", "rejected"]
    assert out["observed_vocabulary"]["producttype"] == ["DELIVERY", "INTRADAY"]
    assert out["row_count"] == 2 and "averageprice" in out["rows"][0]
    assert out["rows"][0]["averageprice"] == {"type": "number"} and out["rows"][0]["quantity"] == {"type": "numeric-string"}
    assert out["rows"][0]["text"] == {"type": "string"} and out["rows"][0]["orderid"] == {"type": "numeric-string"}


def test_object_responses_and_tokens_are_redacted():
    body = {"status": True, "data": {"net": "100000.55", "availablecash": "5000", "jwtToken": "eyJ.abc.def", "utiliseddebits": "0"}}
    out = redact_response(200, body)
    text = json.dumps(out)
    for leaked in ("100000.55", "5000", "eyJ.abc.def"):
        assert leaked not in text
    assert sorted(out["fields"]) == ["availablecash", "jwtToken", "net", "utiliseddebits"]
    assert out["data_kind"] == "object"


def test_error_and_non_object_responses():
    assert redact_response(500, "oops")["data_kind"].startswith("non-object")
    out = redact_response(403, {"status": False, "message": "Invalid Token", "error_type": "TokenException"})
    assert "Invalid Token" not in json.dumps(out) and out["status_flag"] is False


def test_script_is_a_dry_run_by_default_and_refuses_without_authorization():
    env = {"PATH": "", "SYSTEMROOT": "C:\\Windows"}      # no Angel credentials at all
    base = [sys.executable, "-I", str(BACKEND / "scripts" / "angel_capture.py")]
    dry = subprocess.run(base, capture_output=True, text=True, env=env, timeout=60)
    assert dry.returncode == 0 and "nothing contacted" in dry.stdout.lower()
    no_auth = subprocess.run(base + ["--execute"], capture_output=True, text=True, env=env, timeout=60)
    assert no_auth.returncode == 1 and "--i-am-authorized" in no_auth.stdout
    no_env = subprocess.run(base + ["--execute", "--i-am-authorized"], capture_output=True, text=True, env=env, timeout=60)
    assert no_env.returncode == 1 and "missing environment" in no_env.stdout.lower()


def test_script_only_issues_read_only_calls():
    src = (BACKEND / "scripts" / "angel_capture.py").read_text(encoding="utf-8")
    calls = src.split("READ_ONLY_CALLS = ", 1)[1].split("\n", 1)[0]
    assert "place" not in calls and "modify" not in calls and "cancel" not in calls
    assert all(m in ("GET",) for m in ("GET",)) and '"POST"' not in calls
