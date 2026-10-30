"""A refused Groww token request logs Groww's own error code / message (so approval, IP and key problems can be told apart) and
nothing sensitive."""
import logging

import pytest

import groww_helpers as G
from app.services.groww.client import GrowwAuthError


async def _refuse(mock_redis, caplog, body, status=403):
    caplog.set_level(logging.WARNING, logger="groww_client")
    fake = G.FakeGroww()
    fake.token_response = (status, body)
    client = G.make_client(mock_redis, fake)
    with pytest.raises(GrowwAuthError) as raised:
        await client.login()
    return [r.getMessage() for r in caplog.records if "token request refused" in r.getMessage()], str(raised.value)


@pytest.mark.asyncio
async def test_the_refusal_code_and_message_are_logged(mock_redis, caplog):
    lines, _ = await _refuse(mock_redis, caplog, {"error": {"code": "GA003", "message": "IP not whitelisted for this key"}})
    assert len(lines) == 1
    assert "http=403" in lines[0] and "GA003" in lines[0] and "IP not whitelisted" in lines[0]


@pytest.mark.asyncio
async def test_the_key_the_secret_and_token_like_text_are_never_logged(mock_redis, caplog):
    body = {"message": "bad key GROWW-API-KEY", "error": {"code": "X", "message": "ey" + "A1b2C3d4" * 6}, "extra": "ZZZ-private"}
    lines, error = await _refuse(mock_redis, caplog, body)
    text = "\n".join(lines)
    for leaked in ("GROWW-API-KEY", "GROWW-API-SECRET", "A1b2C3d4", "ZZZ-private"):
        assert leaked not in text and leaked not in error
    assert "[redacted]" in text


@pytest.mark.asyncio
async def test_a_non_json_body_is_still_reported_without_content(mock_redis, caplog):
    lines, _ = await _refuse(mock_redis, caplog, "<html>Forbidden</html>")
    assert len(lines) == 1 and "http=403" in lines[0] and "Forbidden" not in lines[0]
