from __future__ import annotations

from typing import Any

import pytest
from rest_framework.test import APIClient

from tests.mfa_helpers import enroll, login
from tests.test_api import CREDS, INVENTORY


@pytest.fixture
def inventory(settings: Any, tmp_path: Any) -> None:
    path = tmp_path / "fleet.yaml"
    path.write_text(INVENTORY, encoding="utf-8")
    settings.FLEET_INVENTORY_PATH = str(path)


@pytest.fixture
def client(api_user: Any) -> APIClient:
    c = APIClient()
    resp = login(c, CREDS, enroll(api_user))
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.json()['access']}")
    return c
