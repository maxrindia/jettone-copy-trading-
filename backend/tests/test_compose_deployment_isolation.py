"""
Deployment-shape guards for docker-compose.yml (no Docker is started).

* nginx/certbot are the opt-in "tls" profile: a plain `docker compose up -d`
  on an HTTP-only server (reached on :8080, no certificate) must never start
  them - nginx cannot start without a certificate.
* v1 owns its database/Redis volumes and networks, so a fresh install can
  never attach to the data of an older "copytrade_*" stack.
* The backend builds from backend/ and stays internal (no host port).
"""
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
SERVICES = COMPOSE["services"]


def test_core_services_start_by_default():
    for name in ("postgres", "redis", "fastapi", "nextjs"):
        assert "profiles" not in SERVICES[name], f"{name} must not be behind a profile"


def test_nginx_and_certbot_are_opt_in_tls_profile_only():
    for name in ("nginx", "certbot"):
        assert SERVICES[name].get("profiles") == ["tls"], f"{name} must be in the 'tls' profile only"


def test_no_default_service_depends_on_a_tls_service():
    for name in ("postgres", "redis", "fastapi", "nextjs"):
        depends = SERVICES[name].get("depends_on") or {}
        names = depends if isinstance(depends, list) else list(depends)
        assert not {"nginx", "certbot"} & set(names), f"{name} must not depend on a TLS service"


def test_database_and_redis_use_v1_volume_names_not_the_old_stack():
    volumes = COMPOSE["volumes"]
    assert volumes["postgres_data"]["name"] == "jettone_postgres_data"
    assert volumes["redis_data"]["name"] == "jettone_redis_data"
    assert not any(
        str(v.get("name", "")).startswith("copytrade_") and "postgres" in str(v.get("name"))
        for v in volumes.values()
    )


def test_networks_are_v1_names():
    names = {n["name"] for n in COMPOSE["networks"].values()}
    assert names == {"jettone_frontend_net", "jettone_backend_net"}


def test_backend_builds_from_backend_dir_and_is_not_published():
    fastapi = SERVICES["fastapi"]
    assert fastapi["build"]["context"] == "./backend"
    assert "ports" not in fastapi, "the engine must never be given a host port"
    assert "8000" in [str(p) for p in fastapi.get("expose", [])]


def test_only_the_web_app_is_published_on_the_host():
    for name, svc in SERVICES.items():
        if name == "nextjs":
            assert svc["ports"] == ["3005:3000", "8080:3000"]
        else:
            assert "ports" not in svc, f"{name} must not publish host ports by default"
