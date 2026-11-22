from __future__ import annotations

from typing import Any

import pytest
from rest_framework.test import APIClient

from tests.helpers import make_apps
from tests.mfa_helpers import enroll, login
from tests.test_api import CREDS, INVENTORY


@pytest.fixture
def inventory(db: Any) -> None:
    make_apps(INVENTORY)


@pytest.fixture
def auth_client(api_user: Any) -> APIClient:
    c = APIClient()
    resp = login(c, CREDS, enroll(api_user))
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.json()['access']}")
    return c
