"""A refused Groww token request logs only Groww's own error.errorCode / error.errorMessage (so approval, IP and key problems can be
told apart) and nothing sensitive. Offline: a mocked transport, no request ever leaves the process."""
import logging

import httpx
import pytest

import groww_helpers as G
from app.services.groww.client import GrowwAuthError, GrowwClient

ACTUAL_SHAPE = {"header": None, "error": {"errorCode": "GA005", "errorMessage": "User not authorised to perform this operation"},
                "response": None}
PROXY = "http://sip_user:p4ssw0rd@dc-mum-006.staticip.in:8080"


async def _refuse(mock_redis, caplog, body, status=403, *, proxy_url=None):
    caplog.set_level(logging.WARNING, logger="groww_client")
    fake = G.FakeGroww()
    fake.token_response = (status, body)
    client = G.make_client(mock_redis, fake)
    if proxy_url:
        client._proxy_url = proxy_url
    with pytest.raises(GrowwAuthError) as raised:
        await client.login()
    assert len(fake.requests) == 1                                       # one mocked request, nothing resent
    lines = [r.getMessage() for r in caplog.records if "token request refused" in r.getMessage()]
    return lines, str(raised.value)


@pytest.mark.asyncio
async def test_the_actual_groww_failure_shape_logs_its_error_code_and_message(mock_redis, caplog):
    lines, _ = await _refuse(mock_redis, caplog, ACTUAL_SHAPE)
    assert len(lines) == 1
    assert "http=403" in lines[0]
    assert '"errorCode": "GA005"' in lines[0] and "User not authorised to perform this operation" in lines[0]
    assert "'errorCode': 'str'" in lines[0]                              # the shape is still logged, values-free


@pytest.mark.asyncio
async def test_only_the_two_allowlisted_fields_are_ever_logged_as_values(mock_redis, caplog):
    body = {"header": "H-VALUE", "response": {"x": "R-VALUE"}, "status": "S-VALUE", "message": "TOP-MESSAGE", "code": "TOP-CODE",
            "error": {"errorCode": "GA001", "errorMessage": "Bad request", "metadata": {"k": "META-VALUE"}, "other": "OTHER-VALUE"}}
    lines, _ = await _refuse(mock_redis, caplog, body)
    text = lines[0]
    for leaked in ("H-VALUE", "R-VALUE", "S-VALUE", "TOP-MESSAGE", "TOP-CODE", "META-VALUE", "OTHER-VALUE"):
        assert leaked not in text, leaked
    assert "GA001" in text and "Bad request" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "bad key GROWW-API-KEY",                                             # our API key
    "secret GROWW-API-SECRET rejected",                                  # our API secret
    "token ey" + "A1b2C3d4" * 6,                                         # a long token-like run
    "contact https://example.com/path?x=1",                              # any URL
    "via http://sip_user:p4ssw0rd@dc-mum-006.staticip.in:8080",          # a proxy URL with credentials
    "user@host.example",                                                 # user@host
])
async def test_sensitive_looking_values_are_redacted(mock_redis, caplog, message):
    body = {"header": None, "error": {"errorCode": "GA000", "errorMessage": message}, "response": None}
    lines, error = await _refuse(mock_redis, caplog, body, proxy_url=PROXY)
    text = lines[0] + error
    for leaked in ("GROWW-API-KEY", "GROWW-API-SECRET", "A1b2C3d4", "example.com", "p4ssw0rd", "sip_user", "staticip.in", "host.example"):
        assert leaked not in text, leaked
    assert "[redacted]" in lines[0] and "GA000" in lines[0]


@pytest.mark.asyncio
async def test_the_exact_proxy_url_is_redacted_even_without_a_url_shape(mock_redis, caplog):
    body = {"header": None, "error": {"errorCode": "GA000", "errorMessage": "denied for proxyuser"}, "response": None}
    lines, _ = await _refuse(mock_redis, caplog, body, proxy_url="proxyuser")        # a bare value that appears in a message
    assert "proxyuser" not in lines[0] and "[redacted]" in lines[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["<html>Forbidden</html>", ["a", "b"], {"error": "plain string"}, {"error": {"errorCode": 5}}])
async def test_unexpected_bodies_log_no_content(mock_redis, caplog, body):
    lines, _ = await _refuse(mock_redis, caplog, body)
    assert len(lines) == 1 and "http=403" in lines[0] and "fields={}" in lines[0]
    assert "Forbidden" not in lines[0] and "plain string" not in lines[0]


@pytest.mark.asyncio
async def test_the_raw_body_and_authorization_never_reach_the_log(mock_redis, caplog):
    lines, error = await _refuse(mock_redis, caplog, ACTUAL_SHAPE)
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "Bearer" not in joined and "GROWW-API-KEY" not in joined and "GROWW-API-SECRET" not in joined
    assert '"header"' not in lines[0]                                    # not the raw JSON body
    assert "GROWW-API" not in error


def test_the_client_is_only_ever_built_over_a_mock_transport_in_these_tests():
    client = G.make_client(object(), G.FakeGroww())
    assert isinstance(client, GrowwClient) and isinstance(client._http, httpx.AsyncClient)
