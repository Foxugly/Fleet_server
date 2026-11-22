"""Settings guards, each checked in a fresh interpreter with its own env."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

PROBE = (
    "import json, config.settings as s; "
    "c = s.CACHES['default'] if hasattr(s, 'CACHES') else {}; "
    "print(json.dumps({'debug': s.DEBUG, 'key': s.SECRET_KEY, "
    "'hosts': s.ALLOWED_HOSTS, 'cache': c.get('BACKEND', ''), "
    "'prefix': c.get('KEY_PREFIX', ''), 'location': c.get('LOCATION', ''), "
    "'apps': s.INSTALLED_APPS, 'hsts': getattr(s, 'SECURE_HSTS_SECONDS', None), "
    "'conn_max_age': s.DATABASES['default'].get('CONN_MAX_AGE'), "
    "'proxies': s.REST_FRAMEWORK['NUM_PROXIES'], 'release': s.RELEASE}))"
)

_CLEARED = (
    "STATE",
    "DEBUG",
    "SECRET_KEY",
    "ALLOWED_HOSTS",
    "REDIS_URL",
    "SENTRY_DSN",
    "DB_ENGINE",
    "DB_CONN_MAX_AGE",
    "CORS_ALLOWED_ORIGINS",
)


def _load(**env: str | None) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if k not in _CLEARED}
    merged = {"STATE": "DEV", **env}
    return subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=ROOT,
        env={**base, **{k: v for k, v in merged.items() if v is not None}},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _ok(**env: str | None) -> dict:
    proc = _load(**env)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _fails(**env: str | None) -> str:
    proc = _load(**env)
    assert proc.returncode != 0
    assert "ImproperlyConfigured" in proc.stderr
    return proc.stderr


def test_unknown_state_is_refused():
    assert "STATE must be set to DEV or PROD" in _fails(STATE="PRODUCTION")


def test_missing_state_is_refused():
    assert "STATE must be set to DEV or PROD" in _fails(STATE=None)


def test_empty_state_is_refused():
    assert "STATE must be set to DEV or PROD" in _fails(STATE="")


def test_dev_default_key_is_random_never_a_constant():
    first, second = _ok(), _ok()
    assert first["debug"] is True
    assert first["key"] != second["key"]
    assert len(first["key"]) >= 50
    assert first["key"] != "dev-secret-key"


def test_prod_requires_secret_key():
    assert "SECRET_KEY" in _fails(
        STATE="PROD", ALLOWED_HOSTS="fleet.example.com", REDIS_URL="redis://r/12"
    )


def test_prod_requires_allowed_hosts():
    assert "ALLOWED_HOSTS" in _fails(
        STATE="PROD", SECRET_KEY="x" * 50, REDIS_URL="redis://r/12"
    )


def test_prod_ignores_debug_true_and_has_no_testserver():
    out = _ok(
        STATE="PROD",
        DEBUG="true",
        SECRET_KEY="x" * 50,
        ALLOWED_HOSTS="fleet.example.com",
        REDIS_URL="redis://r/12",
        CORS_ALLOWED_ORIGINS="https://fleet.example.com",
    )
    assert out["debug"] is False
    assert out["hosts"] == ["fleet.example.com"]


def test_dev_debug_can_be_turned_off():
    assert _ok(STATE="DEV", DEBUG="false", SECRET_KEY="x" * 50)["debug"] is False


@pytest.mark.parametrize("url", ["redis://127.0.0.1:6379/12"])
def test_redis_url_selects_shared_redis_cache(url):
    out = _ok(REDIS_URL=url)
    assert out["cache"] == "django.core.cache.backends.redis.RedisCache"
    assert out["location"] == url
    assert out["prefix"] == "fleet-cache"


def test_no_redis_url_keeps_locmem_cache():
    out = _ok()
    assert out["cache"] == "django.core.cache.backends.locmem.LocMemCache"


def test_admin_app_not_installed():
    assert "django.contrib.admin" not in _ok()["apps"]


def test_prod_requires_redis_url_for_the_shared_throttle_cache():
    err = _fails(STATE="PROD", SECRET_KEY="x" * 50, ALLOWED_HOSTS="fleet.example.com")
    assert "REDIS_URL must be set when STATE=PROD" in err


_PROD = {
    "STATE": "PROD",
    "SECRET_KEY": "x" * 50,
    "ALLOWED_HOSTS": "fleet.example.com",
    "REDIS_URL": "redis://r/12",
    "CORS_ALLOWED_ORIGINS": "https://fleet.example.com",
}


def test_prod_requires_cors_allowed_origins():
    env = {k: v for k, v in _PROD.items() if k != "CORS_ALLOWED_ORIGINS"}
    assert "CORS_ALLOWED_ORIGINS must be set when STATE=PROD" in _fails(**env)


def test_prod_hsts_is_left_to_nginx():
    assert _ok(**_PROD)["hsts"] == 0


def test_db_conn_max_age_defaults_to_zero_and_is_overridable():
    assert _ok(DB_ENGINE="postgresql")["conn_max_age"] == 0
    assert _ok(DB_ENGINE="postgresql", DB_CONN_MAX_AGE="60")["conn_max_age"] == 60


def test_throttle_trusts_exactly_one_proxy():
    assert _ok()["proxies"] == 1


def test_release_matches_the_release_file_or_dev():
    release_file = ROOT / "RELEASE"
    expected = release_file.read_text().strip() if release_file.exists() else "dev"
    assert _ok()["release"] == (expected or "dev")
