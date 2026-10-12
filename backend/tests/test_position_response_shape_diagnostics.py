"""
Diagnostic-only regression coverage for the 2026-09-24 investigation into
"AliceBlueClient._get_result_rows() returned no result list" causing
POSITION_FETCH_FAILED SELL blocks.

This file does NOT test or assert any new functional behavior - the
return value of get_positions_or_none()/get_positions_raw()/get_order_book_raw()
for a non-list `result` is unchanged (still None) in every case below,
exactly as before this diagnostic was added. What's tested here is only:

1. The temporary diagnostic log line fires for a "result is not a list"
   response, and captures useful shape metadata.
2. The diagnostic never logs anything that looks like a credential/token/
   secret, even if one happens to be present elsewhere in the body (a
   defensive test - real Alice Blue responses for /positions and
   /orders/book are not expected to carry one, but the redaction is
   keyed on JSON key name, not on the specific endpoint, so it's cheap to
   verify it can't leak one regardless).
3. The functional return value (None) is completely unchanged for every
   shape variant (null, missing key, empty string, object, scalar).

No live trading is enabled and no real broker calls are made anywhere in
this file - the HTTP client is always a mocked httpx.AsyncClient, and
Redis is the in-memory mock_redis fixture.
"""
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.alice_blue.client import AliceBlueClient


def _client(mock_redis, mock_http):
    return AliceBlueClient(
        account_id="acc-test", user_id="AB00101", api_secret="secret",
        redis=mock_redis, http_client=mock_http,
    )


async def _positions_with_body(mock_redis, body):
    mock_http = AsyncMock(spec=httpx.AsyncClient)
    await mock_redis.set("alice:session:acc-test", "A3_SESSION_JWT_TOKEN")
    resp = AsyncMock()
    resp.status_code = 200
    resp.json = lambda: body
    mock_http.request = AsyncMock(return_value=resp)
    client = _client(mock_redis, mock_http)
    return client, await client.get_positions_raw()


# ---------------------------------------------------------------------------
# 1. Functional return value is unchanged for every non-list `result` shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_result_null_still_returns_none(mock_redis, caplog):
    client, result = await _positions_with_body(mock_redis, {"stat": "Ok", "result": None})
    assert result is None


@pytest.mark.asyncio
async def test_result_missing_key_still_returns_none(mock_redis):
    client, result = await _positions_with_body(mock_redis, {"stat": "Ok"})
    assert result is None


@pytest.mark.asyncio
async def test_result_empty_string_still_returns_none(mock_redis):
    client, result = await _positions_with_body(mock_redis, {"stat": "Ok", "result": ""})
    assert result is None


@pytest.mark.asyncio
async def test_result_object_still_returns_none(mock_redis):
    client, result = await _positions_with_body(mock_redis, {"stat": "Ok", "result": {"count": 0}})
    assert result is None


@pytest.mark.asyncio
async def test_result_scalar_still_returns_none(mock_redis):
    client, result = await _positions_with_body(mock_redis, {"stat": "Ok", "result": 0})
    assert result is None


@pytest.mark.asyncio
async def test_genuine_list_still_parses_normally_unaffected(mock_redis):
    """Control case: the diagnostic branch must never fire, and normal
    parsing must be completely untouched, for an actual list response."""
    client, result = await _positions_with_body(mock_redis, {"stat": "Ok", "result": [{"a": 1}]})
    assert result == [{"a": 1}]


# ---------------------------------------------------------------------------
# 2. Diagnostic metadata correctness
# ---------------------------------------------------------------------------

def test_describe_result_shape_null_result():
    data = {"stat": "Ok", "result": None}
    desc = AliceBlueClient._describe_result_shape(data, data.get("result"))
    assert "top_level_type=dict" in desc
    assert "result_present=True" in desc
    assert "result_is_none=True" in desc
    assert "result_type=NoneType" in desc


def test_describe_result_shape_missing_key():
    data = {"stat": "Ok"}
    desc = AliceBlueClient._describe_result_shape(data, data.get("result"))
    assert "result_present=False" in desc
    assert "result_is_none=True" in desc


def test_describe_result_shape_empty_string():
    data = {"stat": "Ok", "result": ""}
    desc = AliceBlueClient._describe_result_shape(data, data.get("result"))
    assert "result_type=str" in desc
    assert "result_present=True" in desc


def test_describe_result_shape_object():
    data = {"stat": "Ok", "result": {"count": 0, "note": "no rows"}}
    desc = AliceBlueClient._describe_result_shape(data, data.get("result"))
    assert "result_type=dict" in desc
    assert '"count": 0' in desc


def test_describe_result_shape_non_dict_top_level():
    """A top-level JSON array or scalar (not an object) must be described
    safely too, without crashing on .keys()."""
    data = "unexpected string body"
    desc = AliceBlueClient._describe_result_shape(data, data)
    assert "top_level_type=str" in desc
    assert "N/A" in desc


# ---------------------------------------------------------------------------
# 3. Redaction never leaks anything credential-shaped
# ---------------------------------------------------------------------------

def test_redaction_masks_token_like_keys():
    value = {"accessToken": "SECRET_ABC123", "sessionId": "SECRET_XYZ", "count": 2}
    redacted = AliceBlueClient._redact_diagnostic_value(value)
    assert "SECRET_ABC123" not in redacted
    assert "SECRET_XYZ" not in redacted
    assert "***REDACTED***" in redacted
    assert '"count": 2' in redacted  # non-sensitive keys are preserved


def test_redaction_masks_various_sensitive_key_names():
    value = {
        "authHeader": "x", "password": "x", "apiSecret": "x",
        "cookie": "x", "otp": "x", "pin": "x", "credential": "x",
    }
    redacted = AliceBlueClient._redact_diagnostic_value(value)
    assert "x" not in redacted.replace("***REDACTED***", "")


def test_redaction_truncates_long_values():
    long_value = "a" * 5000
    redacted = AliceBlueClient._redact_diagnostic_value(long_value)
    assert len(redacted) < 300
    assert "truncated" in redacted


def test_redaction_handles_none_and_scalars_without_crashing():
    assert AliceBlueClient._redact_diagnostic_value(None) == "None"
    assert AliceBlueClient._redact_diagnostic_value(0) == "0"
    assert AliceBlueClient._redact_diagnostic_value(True) == "True"


# ---------------------------------------------------------------------------
# 4. The diagnostic log line itself fires exactly once per non-list result,
#    and never on the success path.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_diagnostic_log_line_fires_for_non_list_result(mock_redis, caplog):
    import logging
    caplog.set_level(logging.WARNING, logger="alice_blue_client")
    await _positions_with_body(mock_redis, {"stat": "Ok", "result": None})
    diag_lines = [r.message for r in caplog.records if "[DIAG]" in r.message]
    assert len(diag_lines) == 1
    assert "result_is_none=True" in diag_lines[0]


@pytest.mark.asyncio
async def test_diagnostic_log_line_does_not_fire_for_a_real_list(mock_redis, caplog):
    import logging
    caplog.set_level(logging.WARNING, logger="alice_blue_client")
    await _positions_with_body(mock_redis, {"stat": "Ok", "result": []})
    diag_lines = [r.message for r in caplog.records if "[DIAG]" in r.message]
    assert len(diag_lines) == 0
