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
    "'apps': s.INSTALLED_APPS}))"
)

_CLEARED = (
    "STATE",
    "DEBUG",
    "SECRET_KEY",
    "ALLOWED_HOSTS",
    "REDIS_URL",
    "SENTRY_DSN",
    "DB_ENGINE",
)


def _load(**env: str) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if k not in _CLEARED}
    return subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=ROOT,
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _ok(**env: str) -> dict:
    proc = _load(**env)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _fails(**env: str) -> str:
    proc = _load(**env)
    assert proc.returncode != 0
    assert "ImproperlyConfigured" in proc.stderr
    return proc.stderr


def test_unknown_state_is_refused():
    assert "STATE must be DEV or PROD" in _fails(STATE="PRODUCTION")


def test_dev_default_key_is_random_never_a_constant():
    first, second = _ok(), _ok()
    assert first["debug"] is True
    assert first["key"] != second["key"]
    assert len(first["key"]) >= 50
    assert first["key"] != "dev-secret-key"


def test_prod_requires_secret_key():
    assert "SECRET_KEY" in _fails(STATE="PROD", ALLOWED_HOSTS="fleet.example.com")


def test_prod_requires_allowed_hosts():
    assert "ALLOWED_HOSTS" in _fails(STATE="PROD", SECRET_KEY="x" * 50)


def test_prod_ignores_debug_true_and_has_no_testserver():
    out = _ok(
        STATE="PROD",
        DEBUG="true",
        SECRET_KEY="x" * 50,
        ALLOWED_HOSTS="fleet.example.com",
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


def test_empty_state_means_dev():
    assert _ok(STATE="")["debug"] is True
