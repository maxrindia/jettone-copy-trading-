"""The engine must refuse to start with .env.example placeholder secrets."""
from types import SimpleNamespace

from app.core.config import find_placeholder_secrets


def _cfg(**over):
    base = dict(
        INTERNAL_API_TOKEN="a" * 64,
        ALICE_BLUE_ENCRYPTION_KEY="b" * 44,
        DATABASE_URL="postgresql+asyncpg://u:realpw@postgres:5432/d",
        REDIS_URL="redis://:realpw@redis:6379/0",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_real_secrets_pass():
    assert find_placeholder_secrets(_cfg()) == []


def test_each_placeholder_is_reported_case_insensitively():
    cfg = _cfg(
        INTERNAL_API_TOKEN="CHANGE_ME_generate_with_openssl_rand_hex_32",
        ALICE_BLUE_ENCRYPTION_KEY="change_me_key",
        DATABASE_URL="postgresql+asyncpg://u:CHANGE_ME_same_as_POSTGRES_PASSWORD@postgres/d",
        REDIS_URL="redis://:CHANGE_ME_same_as_REDIS_PASSWORD@redis:6379/0",
    )
    assert find_placeholder_secrets(cfg) == [
        "INTERNAL_API_TOKEN", "ALICE_BLUE_ENCRYPTION_KEY", "DATABASE_URL", "REDIS_URL",
    ]


def test_unset_values_are_not_reported_here():
    # Missing token / key are rejected elsewhere (fail-closed 503 / EncryptionKeyError).
    assert find_placeholder_secrets(_cfg(INTERNAL_API_TOKEN=None, ALICE_BLUE_ENCRYPTION_KEY=None)) == []
