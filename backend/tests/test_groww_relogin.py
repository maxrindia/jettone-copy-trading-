"""
Groww Re-login under the PRODUCTION flag set (GROWW_ENABLED=true, Child/Master off): the server-side broker-login
route logs in exactly the requested Groww account, never falls back to Alice Blue, and when Groww answers 200 with a
token response Jettone cannot use, the reason and the response SHAPE (never its values) are logged for diagnosis.

Offline: mocked Groww transport, in-memory DB, fake Redis. No real login.
"""
import logging
import uuid

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.accounts import Account, AccountType, ApiCredential
from app.core.crypto import encrypt_secret
from app.services.groww.client import GrowwAuthError
from conftest import TestingSessionLocal
import groww_helpers as G
from test_groww_foundation import TOKEN, _groww_account, client  # noqa: F401

SECRET_TOKEN = "TOK-SUPER-SECRET-0001"


@pytest.fixture
def production_flags(monkeypatch):
    monkeypatch.setattr(settings, "GROWW_ENABLED", True)
    monkeypatch.setattr(settings, "GROWW_CHILD_TRADING_ENABLED", False)
    monkeypatch.setattr(settings, "GROWW_MASTER_ENABLED", False)
    monkeypatch.setattr(settings, "ANGEL_ONE_ENABLED", False)


def patch_builder(monkeypatch, fake):
    monkeypatch.setattr("app.api.accounts.build_groww_client",
                        lambda a, r, http_client=None: G.make_client(r, fake, account_id=str(a.id)))


async def row(account_id):
    async with TestingSessionLocal() as s:
        return await s.get(Account, account_id)


async def alice_account(db, name="alice"):
    acc = Account(id=uuid.uuid4(), account_type=AccountType.FOLLOWER, display_name=name, alice_user_id=f"AB_{name}",
                  broker="alicebluenew", is_active=True)
    db.add(acc)
    await db.flush()
    db.add(ApiCredential(account_id=acc.id, encrypted_api_secret=encrypt_secret("alice-secret")))
    await db.commit()
    return acc


# ── Groww authenticates while its Child/Master capabilities stay off ──────────────────────────

@pytest.mark.asyncio
async def test_groww_logs_in_with_child_and_master_disabled_and_only_that_account_changes(client, db_session, mock_redis, monkeypatch, production_flags):
    target = await _groww_account(db_session, "vikash1")
    other = await _groww_account(db_session, "other", ip="151.245.182.71")
    alice = await alice_account(db_session)
    fake = G.FakeGroww()
    patch_builder(monkeypatch, fake)
    resp = await client.post(f"/api/v1/accounts/{target.id}/broker-login", headers=TOKEN)
    assert resp.status_code == 200 and resp.json()["session_valid"] is True
    assert [r.url.path for r in fake.requests] == ["/v1/token/api/access"]            # one token request, nothing else
    assert f"groww:session:{target.id}" in mock_redis._storage
    assert f"groww:session:{other.id}" not in mock_redis._storage
    assert f"alice:session:{target.id}" not in mock_redis._storage                     # never an Alice Blue session
    t, o, a = await row(target.id), await row(other.id), await row(alice.id)
    assert t.session_valid is True and t.last_authenticated_at is not None
    assert o.session_valid in (False, None) and o.last_authenticated_at is None        # untouched
    assert a.last_authenticated_at is None and a.broker == "alicebluenew"              # untouched
    assert t.account_type == AccountType.FOLLOWER and t.broker == "groww"              # role and broker unchanged
    assert SECRET_TOKEN not in resp.text and "TOK-1" not in resp.text


@pytest.mark.asyncio
async def test_a_failed_groww_login_marks_only_that_account_and_creates_no_session(client, db_session, mock_redis, monkeypatch, production_flags):
    target = await _groww_account(db_session, "vikash1")
    other = await _groww_account(db_session, "other", ip="151.245.182.71")
    fake = G.FakeGroww()
    fake.token_response = (200, {"token": SECRET_TOKEN, "isActive": False, "expiry": "2026-10-20T06:00:00"})
    patch_builder(monkeypatch, fake)
    resp = await client.post(f"/api/v1/accounts/{target.id}/broker-login", headers=TOKEN)
    assert resp.status_code == 401 and SECRET_TOKEN not in resp.text
    assert f"groww:session:{target.id}" not in mock_redis._storage
    assert (await row(target.id)).session_valid is False
    assert (await row(other.id)).last_authenticated_at is None


@pytest.mark.asyncio
async def test_broker_login_never_falls_back_to_alice_blue(client, db_session, mock_redis, production_flags):
    alice = await alice_account(db_session)
    resp = await client.post(f"/api/v1/accounts/{alice.id}/broker-login", headers=TOKEN)
    assert resp.status_code == 409                                                       # Alice Blue logs in by its own OAuth flow
    assert not any(k.startswith("alice:session:") for k in mock_redis._storage)


@pytest.mark.asyncio
@pytest.mark.parametrize("broker", ["zerodha", "made-up-broker"])
async def test_an_unsupported_broker_fails_closed(client, db_session, mock_redis, production_flags, broker):
    acc = Account(id=uuid.uuid4(), account_type=AccountType.FOLLOWER, display_name="x", alice_user_id=f"X_{broker}",
                  broker=broker, is_active=True)
    db_session.add(acc)
    await db_session.commit()
    resp = await client.post(f"/api/v1/accounts/{acc.id}/broker-login", headers=TOKEN)
    assert resp.status_code == 409
    assert not any(k.startswith(("alice:session:", "groww:session:")) for k in mock_redis._storage)


# ── diagnosis: an unusable 200 token response is described by SHAPE, never by value ─────────

UNUSABLE = [
    ("no_token", {"isActive": True, "expiry": "2026-10-20T06:00:00"}, "no token"),
    ("inactive", {"token": SECRET_TOKEN, "isActive": False, "expiry": "2026-10-20T06:00:00"}, "isActive is not true"),
    ("isActive_as_string", {"token": SECRET_TOKEN, "isActive": "true", "expiry": "2026-10-20T06:00:00"}, "isActive is not true"),
    ("expiry_as_epoch_number", {"token": SECRET_TOKEN, "isActive": True, "expiry": 1792476000}, "no readable expiry"),
    ("expiry_other_format", {"token": SECRET_TOKEN, "isActive": True, "expiry": "20/10/2026 06:00"}, "no readable expiry"),
    ("already_expired", {"token": SECRET_TOKEN, "isActive": True, "expiry": "2026-10-15T06:00:00"}, "already expired"),
    ("nested_payload", {"status": "SUCCESS", "payload": {"token": SECRET_TOKEN, "isActive": False}}, "isActive is not true"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,body,reason", UNUSABLE, ids=[u[0] for u in UNUSABLE])
async def test_an_unusable_token_response_is_logged_by_shape_only(mock_redis, caplog, name, body, reason):
    fake = G.FakeGroww()
    fake.token_response = (200, body)
    c = G.make_client(mock_redis, fake)
    with caplog.at_level(logging.WARNING, logger="groww_client"):
        with pytest.raises(GrowwAuthError):
            await c.login()
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "token response not usable" in text and reason in text
    assert SECRET_TOKEN not in text and "GROWW-API-SECRET" not in text and "GROWW-API-KEY" not in text
    assert "1792476000" not in text and "2026-10-" not in text                         # no expiry VALUE either
    assert "token" in text or "payload" in text                                        # key names are kept
    assert c.session_key not in mock_redis._storage


@pytest.mark.asyncio
async def test_the_expiry_format_is_visible_as_a_mask():
    from app.services.groww.client import _shape_of
    shape = _shape_of({"token": SECRET_TOKEN, "isActive": True, "expiry": "2026-10-20T06:00:00", "n": 5, "x": None, "l": [1, 2]})
    assert shape == {"token": "str", "isActive": "bool", "expiry": "str[9999-99-99a99:99:99]", "n": "int", "x": "null", "l": "list[2]"}


@pytest.mark.asyncio
async def test_a_good_login_logs_no_warning(mock_redis, caplog):
    fake = G.FakeGroww()
    c = G.make_client(mock_redis, fake)
    with caplog.at_level(logging.WARNING, logger="groww_client"):
        await c.login()
    assert not [r for r in caplog.records if "not usable" in r.getMessage()]
    assert c.session_key in mock_redis._storage


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# Hardened diagnostics: only safe key names, body kind, sanitized Content-Type; no value ever logged
# ═══════════════════════════════════════════════════════════════════════════════════════════════
import httpx  # noqa: E402

API_SENTINELS = (SECRET_TOKEN, "GROWW-API-KEY", "GROWW-API-SECRET", "SENTINEL-SECRET-KEY-NAME")


def client_returning(mock_redis, status=200, *, json_body=None, content=None, headers=None):
    """A real GrowwClient whose transport returns exactly this response (no network)."""
    def handler(request):
        if content is not None:
            return httpx.Response(status, content=content, headers=headers)
        return httpx.Response(status, json=json_body, headers=headers)
    c = G.make_client(mock_redis, G.FakeGroww())
    c._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


async def login_and_capture(c, caplog):
    with caplog.at_level(logging.WARNING, logger="groww_client"):
        with pytest.raises(GrowwAuthError) as ei:
            await c.login()
    text = "\n".join(r.getMessage() for r in caplog.records)
    return text, str(ei.value)


def assert_clean(text, error, extra=()):
    for s in API_SENTINELS + tuple(extra):
        assert s not in text, f"{s!r} leaked into the log"
        assert s not in error, f"{s!r} leaked into the exception"


@pytest.mark.asyncio
async def test_a_secret_used_as_a_key_name_is_replaced_by_a_numbered_placeholder(mock_redis, caplog):
    body = {SECRET_TOKEN: "x", "isActive": True, "expiry": "2026-10-20T06:00:00"}
    c = client_returning(mock_redis, json_body=body)
    text, err = await login_and_capture(c, caplog)
    assert_clean(text, err)
    assert "<redacted-key-1>" in text and "'isActive': 'bool'" in text and err == "token response contained no token"


SUSPICIOUS_KEYS = [
    ("punctuation", "SENTINEL-SECRET-KEY-NAME"),
    ("longer_than_32", "a" * 33),
    ("hex_digest", "0123456789abcdef0123456789abcdef"),
    ("jwt_shaped", "eyJhbGciOiJIUzI1NiJ9"),
    ("five_digits", "key12345"),
    ("has_space", "my key"),
    ("has_dot", "a.b"),
    ("non_ascii", "tökén"),
    ("empty", ""),
    ("unicode_digits", "k٣٤٥٦٧"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,key", SUSPICIOUS_KEYS, ids=[n for n, _ in SUSPICIOUS_KEYS])
async def test_suspicious_key_names_are_never_logged(mock_redis, caplog, name, key):
    c = client_returning(mock_redis, json_body={key: "v", "tokenRefId": "r", "isActive": True})
    text, err = await login_and_capture(c, caplog)
    assert_clean(text, err)
    if key:
        assert f"'{key}'" not in text
    assert "<redacted-key-1>" in text
    assert "'tokenRefId': 'str'" in text and "'isActive': 'bool'" in text


@pytest.mark.asyncio
async def test_ordinary_key_names_are_kept_and_placeholders_are_distinct(mock_redis, caplog):
    body = {"token": SECRET_TOKEN, "tokenRefId": "r", "sessionName": "s", "isActive": False, "expiry": "2026-10-20T06:00:00",
            "a" * 40: 1, "SENTINEL-SECRET-KEY-NAME": 2}
    c = client_returning(mock_redis, json_body=body)
    text, err = await login_and_capture(c, caplog)
    assert_clean(text, err)
    for k in ("token", "tokenRefId", "sessionName", "isActive", "expiry"):
        assert f"'{k}'" in text
    assert "<redacted-key-1>" in text and "<redacted-key-2>" in text


@pytest.mark.asyncio
async def test_nested_secret_looking_keys_are_replaced_too(mock_redis, caplog):
    c = client_returning(mock_redis, json_body={"payload": {SECRET_TOKEN: "x", "isActive": False}})
    text, err = await login_and_capture(c, caplog)
    assert_clean(text, err)
    assert "'payload'" in text and "<redacted-key-1>" in text


JSON_CT = {"content-type": "application/json"}
NO_TOKEN = "token response contained no token"
BODY_KINDS = [
    ("html_page", dict(content=b"<html>gateway " + SECRET_TOKEN.encode() + b"</html>", headers={"content-type": "text/html; charset=utf-8"}),
     "empty-or-non-json", "text/html; charset=utf-8"),
    ("plain_text", dict(content=b"service unavailable " + SECRET_TOKEN.encode(), headers={"content-type": "text/plain"}),
     "empty-or-non-json", "text/plain"),
    ("malformed_json", dict(content=b'{"token": "' + SECRET_TOKEN.encode() + b'"', headers=JSON_CT), "empty-or-non-json", "application/json"),
    ("empty_body", dict(content=b"", headers=JSON_CT), "empty-or-non-json", "application/json"),
    ("whitespace_body", dict(content=b"  \n ", headers=JSON_CT), "empty-or-non-json", "application/json"),
    ("json_list", dict(json_body=[SECRET_TOKEN, {"token": SECRET_TOKEN}], headers=JSON_CT), "json-list", "application/json"),
    ("empty_object", dict(json_body={}, headers=JSON_CT), "json-object", "application/json"),
    ("json_string", dict(json_body=SECRET_TOKEN, headers=JSON_CT), "json-other", "application/json"),
    ("json_number", dict(json_body=12345, headers=JSON_CT), "json-other", "application/json"),
    ("json_null", dict(content=b"null", headers=JSON_CT), "json-other", "application/json"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,kwargs,kind,ctype", BODY_KINDS, ids=[b[0] for b in BODY_KINDS])
async def test_the_log_tells_apart_html_lists_empty_objects_and_other_json_without_exposing_the_body(mock_redis, caplog, name, kwargs, kind, ctype):
    c = client_returning(mock_redis, **kwargs)
    text, err = await login_and_capture(c, caplog)
    assert_clean(text, err, extra=("gateway", "service unavailable"))
    assert f"body={kind}" in text and "http=200" in text and f"content_type={ctype!r}" in text
    assert err == NO_TOKEN                                                      # the fixed error message is unchanged
    assert c.session_key not in mock_redis._storage


@pytest.mark.asyncio
async def test_a_missing_content_type_is_reported_as_none(mock_redis, caplog):
    c = client_returning(mock_redis, content=b"", headers={})
    text, _ = await login_and_capture(c, caplog)
    assert "content_type='(none)'" in text


@pytest.mark.asyncio
async def test_the_content_type_is_sanitized_and_length_limited(mock_redis, caplog):
    ctype = 'application/json; x="<tag>"; ' + "q" * 200
    c = client_returning(mock_redis, content=b"", headers={"content-type": ctype})
    text, _ = await login_and_capture(c, caplog)
    logged = text.split("content_type='", 1)[1].split("'", 1)[0]
    assert len(logged) <= 64
    assert "<" not in logged and ">" not in logged and '"' not in logged
    assert logged.startswith("application/json; x=")
    assert "q" * 60 not in logged


@pytest.mark.asyncio
async def test_the_expiry_mask_keeps_its_approved_form_and_hides_every_value(mock_redis, caplog):
    c = client_returning(mock_redis, json_body={"token": SECRET_TOKEN, "isActive": False, "expiry": "2026-10-20T06:00:00"})
    text, err = await login_and_capture(c, caplog)
    assert "'expiry': 'str[9999-99-99a99:99:99]'" in text
    assert_clean(text, err, extra=("2026-10-20", "06:00"))


@pytest.mark.asyncio
async def test_an_exotic_expiry_string_cannot_smuggle_a_value_through_the_mask(mock_redis, caplog):
    c = client_returning(mock_redis, json_body={"token": SECRET_TOKEN, "isActive": True,
                                                "expiry": "Tök3n=SENTINEL٣٤ <b>x</b>"})
    text, err = await login_and_capture(c, caplog)
    assert_clean(text, err, extra=("SENTINEL", "Tök3n"))
    masked = text.split("'expiry': 'str[", 1)[1].split("]'", 1)[0]
    assert set(masked) <= set("9a-:+./ ,()?")                                    # only the mask alphabet


@pytest.mark.asyncio
@pytest.mark.parametrize("body,expected", [
    ({"isActive": True, "expiry": "2026-10-20T06:00:00"}, "token response contained no token"),
    ({"token": SECRET_TOKEN, "isActive": "true", "expiry": "2026-10-20T06:00:00"}, "Groww reports the session is not active"),
    ({"token": SECRET_TOKEN, "isActive": True, "expiry": 1792476000}, "token response has no readable expiry"),
    ({"token": SECRET_TOKEN, "isActive": True, "expiry": 1792476000000}, "token response has no readable expiry"),
    ({"token": SECRET_TOKEN, "isActive": True, "expiry": "2026-10-15T06:00:00"}, "token response is already expired"),
], ids=["no_token", "isActive_string", "epoch_seconds", "epoch_millis", "already_expired"])
async def test_validation_and_its_fixed_messages_are_unchanged_and_no_undocumented_format_is_accepted(mock_redis, caplog, body, expected):
    c = client_returning(mock_redis, json_body=body)
    text, err = await login_and_capture(c, caplog)
    assert err == expected and c.session_key not in mock_redis._storage
    assert_clean(text, err)


@pytest.mark.asyncio
async def test_a_genuinely_valid_response_still_creates_a_session_and_logs_no_diagnostic(mock_redis, caplog):
    c = client_returning(mock_redis, json_body={"token": SECRET_TOKEN, "isActive": True, "expiry": "2026-10-17T06:00:00"},
                         headers=JSON_CT)
    with caplog.at_level(logging.WARNING, logger="groww_client"):
        await c.login()
    assert c.session_key in mock_redis._storage
    assert not [r for r in caplog.records if "not usable" in r.getMessage()]
    assert SECRET_TOKEN not in mock_redis._storage[c.session_key]                # stored encrypted, never plain


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# Immutability: a Groww login (success or failure) may change ONLY the target account's session fields
# ═══════════════════════════════════════════════════════════════════════════════════════════════
from decimal import Decimal  # noqa: E402

from app.models.accounts import CopySetting, QuantityMode  # noqa: E402
from app.models.platform_settings import PlatformSetting  # noqa: E402
from app.models.strategies import StrategyMember  # noqa: E402
from strategy_helpers import link_strategy  # noqa: E402

import importlib  # noqa: E402
import pkgutil  # noqa: E402

import app.models as _models_package  # noqa: E402
from app.models.base import Base  # noqa: E402  (NOT app.core.database.Base: that one has no tables - this is the
#                                                 declarative base every model uses and the test database is built from)

# Make sure every model module is imported so the metadata is the COMPLETE schema, whatever this file imported.
for _module in pkgutil.iter_modules(_models_package.__path__):
    importlib.import_module(f"app.models.{_module.name}")

# Tables deliberately left out of the snapshot, each with the reason. A table is excluded only if it cannot or
# should not take part in a before/after comparison. Today that is none: every application table is compared.
EXCLUDED_TABLES: dict = {}


def snapshot_tables():
    """Every table in the schema (minus EXCLUDED_TABLES), in a deterministic order: by name."""
    return [t for t in sorted(Base.metadata.sorted_tables, key=lambda t: t.name) if t.name not in EXCLUDED_TABLES]


# The only columns a login may touch, and only on the account being logged in.
AUTH_COLUMNS = {"session_valid", "last_authenticated_at", "updated_at"}


async def dump_state():
    """Every column of every row of every table in the schema, ordered by table name then primary key.
    Derived from the SQLAlchemy metadata, so a table added to the application later is covered automatically."""
    out = {}
    async with TestingSessionLocal() as s:
        for table in snapshot_tables():
            pk_columns = list(table.primary_key.columns)
            rows = (await s.execute(select(table).order_by(*pk_columns))).mappings().all()
            key_names = [c.name for c in pk_columns]
            out[table.name] = {tuple(r[k] for k in key_names): dict(r) for r in rows}
    return out


def changes_between(before, after):
    """{(table, pk): {column: (old, new)}} plus rows added/removed."""
    changed, added, removed = {}, [], []
    for table in before:
        for pk in set(before[table]) | set(after[table]):
            if pk not in before[table]:
                added.append((table, pk))
            elif pk not in after[table]:
                removed.append((table, pk))
            else:
                diff = {c: (before[table][pk][c], after[table][pk][c]) for c in before[table][pk]
                        if before[table][pk][c] != after[table][pk][c]}
                if diff:
                    changed[(table, pk)] = diff
    return changed, added, removed


class World:
    pass


async def strategy_world(db):
    w = World()
    w.master = await alice_account(db, "master")
    w.alice_child = await alice_account(db, "alice_child")
    w.target = await _groww_account(db, "vikash1")
    w.other_in = await _groww_account(db, "other_in", ip="151.245.182.71")
    w.outside = await _groww_account(db, "outside", ip="151.245.182.72")
    w.strategy = await link_strategy(db, w.master, [(w.target, "2.5"), (w.alice_child, "1.5"), (w.other_in, "3.0")], name="Immut")
    for acc, qty in ((w.target, 7), (w.alice_child, 3), (w.other_in, 5)):
        db.add(CopySetting(follower_account_id=acc.id, copy_enabled=True, quantity_mode=QuantityMode.FIXED, fixed_qty=qty))
    await db.commit()
    return w


@pytest.mark.asyncio
async def test_a_successful_groww_login_changes_only_the_targets_authentication_fields(client, db_session, mock_redis, monkeypatch, production_flags):
    w = await strategy_world(db_session)
    patch_builder(monkeypatch, G.FakeGroww())
    before = await dump_state()
    resp = await client.post(f"/api/v1/accounts/{w.target.id}/broker-login", headers=TOKEN)
    assert resp.status_code == 200
    after = await dump_state()
    changed, added, removed = changes_between(before, after)
    assert added == [] and removed == []                                        # no row created or deleted anywhere
    assert set(changed) == {("accounts", (w.target.id,))}                       # exactly ONE row changed in the whole database
    diff = changed[("accounts", (w.target.id,))]
    assert set(diff) <= AUTH_COLUMNS
    assert diff["session_valid"] == (False, True)
    assert diff["last_authenticated_at"][0] is None and diff["last_authenticated_at"][1] is not None
    t = after["accounts"][(w.target.id,)]
    assert (t["broker"], t["account_type"], t["is_active"]) == ("groww", AccountType.FOLLOWER, True)
    assert {v["account_id"]: v["multiplier"] for v in after["strategy_members"].values()} == {
        w.target.id: Decimal("2.5"), w.alice_child.id: Decimal("1.5"), w.other_in.id: Decimal("3.0")}
    assert {v["strategy_id"] for v in after["strategy_members"].values()} == {w.strategy.id}
    assert after["group_strategies"][(w.strategy.id,)]["master_account_id"] == w.master.id
    assert len(after["strategy_account_roles"]) == 4                           # master + three children, unchanged
    assert after["accounts"][(w.outside.id,)] == before["accounts"][(w.outside.id,)]


@pytest.mark.asyncio
async def test_a_failed_groww_login_changes_nothing_but_the_targets_session_flag(client, db_session, mock_redis, monkeypatch, production_flags):
    w = await strategy_world(db_session)
    fake = G.FakeGroww()
    fake.token_response = (200, {"token": SECRET_TOKEN, "isActive": False, "expiry": "2026-10-20T06:00:00"})
    patch_builder(monkeypatch, fake)
    before = await dump_state()
    resp = await client.post(f"/api/v1/accounts/{w.target.id}/broker-login", headers=TOKEN)
    assert resp.status_code == 401 and SECRET_TOKEN not in resp.text
    after = await dump_state()
    changed, added, removed = changes_between(before, after)
    assert added == [] and removed == []
    assert set(changed) <= {("accounts", (w.target.id,))}                       # at most the target row, nothing else
    for diff in changed.values():
        assert set(diff) <= AUTH_COLUMNS
    t = after["accounts"][(w.target.id,)]
    assert t["session_valid"] is False and t["last_authenticated_at"] is None  # still unauthenticated
    assert (t["broker"], t["account_type"]) == ("groww", AccountType.FOLLOWER)
    assert after["strategy_members"] == before["strategy_members"]              # membership + multipliers byte-identical
    assert after["copy_settings"] == before["copy_settings"]
    assert after["group_strategies"] == before["group_strategies"]
    assert after["strategy_account_roles"] == before["strategy_account_roles"]
    assert f"groww:session:{w.target.id}" not in mock_redis._storage


@pytest.mark.asyncio
async def test_a_throttled_groww_login_changes_nothing_at_all(client, db_session, mock_redis, monkeypatch, production_flags):
    w = await strategy_world(db_session)
    fake = G.FakeGroww()
    fake.token_response = (200, {"token": SECRET_TOKEN, "isActive": False, "expiry": "2026-10-20T06:00:00"})
    patch_builder(monkeypatch, fake)
    first = await client.post(f"/api/v1/accounts/{w.target.id}/broker-login", headers=TOKEN)
    assert first.status_code == 401
    before = await dump_state()
    second = await client.post(f"/api/v1/accounts/{w.target.id}/broker-login", headers=TOKEN)   # inside the 10 s spacing window
    assert second.status_code == 429
    assert len(fake.calls("/v1/token/api/access")) == 1                          # no second token request was sent
    assert await dump_state() == before                                          # and not one column anywhere changed


def test_the_snapshot_covers_the_whole_schema_and_every_exclusion_is_justified():
    """Guards the guard: no table can be silently skipped, and the schema being snapshotted is not an empty one."""
    all_tables = {t.name for t in Base.metadata.sorted_tables}
    covered = {t.name for t in snapshot_tables()}
    assert len(all_tables) >= 14                                                # the real schema, not an empty metadata
    assert {"app_users", "platform_settings", "reconciliation_records"} <= covered   # the tables the hand-kept list missed
    assert {"accounts", "strategy_members", "copy_settings", "strategy_account_roles", "orders", "audit_logs"} <= covered
    assert covered == all_tables - set(EXCLUDED_TABLES)
    for name, reason in EXCLUDED_TABLES.items():
        assert name in all_tables, f"{name} is excluded but is not a table"
        assert isinstance(reason, str) and len(reason.strip()) >= 20, f"exclusion of {name} needs a documented reason"
    for table in snapshot_tables():
        assert len(table.primary_key.columns) > 0, f"{table.name} has no primary key to key its rows by"
    assert [t.name for t in snapshot_tables()] == sorted(covered)               # deterministic order
    assert Base.metadata is PlatformSetting.metadata                            # the models' own metadata, not another Base


@pytest.mark.asyncio
async def test_the_snapshot_reads_every_table_in_a_fixed_order(db_session):
    first, second = await dump_state(), await dump_state()
    assert list(first) == sorted(first) == [t.name for t in snapshot_tables()]
    assert first == second                                                      # reading twice, nothing changes
    assert "platform_settings" in first and "app_users" in first and "reconciliation_records" in first


@pytest.mark.asyncio
async def test_a_change_in_a_previously_omitted_table_is_now_noticed(db_session):
    before = await dump_state()
    async with TestingSessionLocal() as s:
        s.add(PlatformSetting(key="live_trading_enabled", value="true"))
        await s.commit()
    changed, added, removed = changes_between(before, await dump_state())
    assert [table for table, _ in added] == ["platform_settings"] and changed == {} and removed == []


@pytest.mark.asyncio
async def test_the_snapshot_helper_would_notice_a_change(db_session):
    """Guards the guard: if a login ever altered a multiplier, the diff above would catch it."""
    w = await strategy_world(db_session)
    before = await dump_state()
    async with TestingSessionLocal() as s:
        m = (await s.execute(select(StrategyMember).where(StrategyMember.account_id == w.target.id))).scalar_one()
        m.multiplier = Decimal("9.9")
        await s.commit()
    changed, added, removed = changes_between(before, await dump_state())
    assert any(table == "strategy_members" for table, _ in changed) and added == [] and removed == []
