"""Root pytest configuration and shared fixtures."""

from __future__ import annotations

from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache


@pytest.fixture(autouse=True)
def _clear_throttle_state(settings: Any) -> Any:
    # DRF throttling counts in the cache: leaking counters across tests would
    # make them order-dependent. Always a local-memory cache, never a shared one.
    settings.CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}
    }
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _fast_password_hasher(settings: Any) -> None:
    # PBKDF2 at production cost makes every create_user/login take seconds.
    settings.PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


@pytest.fixture
def api_user(db: Any) -> Any:
    return get_user_model().objects.create_user(
        username="fleet-admin", password="fleet-password"
    )
