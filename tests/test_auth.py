from __future__ import annotations

from io import StringIO
from typing import Any
from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from rest_framework.test import APIClient

from tests.mfa_helpers import enroll, login


@pytest.mark.django_db
def test_login_returns_jwt_pair_after_the_totp_step(api_user: object) -> None:
    client = APIClient()
    secret = enroll(api_user)

    response = login(
        client, {"username": "fleet-admin", "password": "fleet-password"}, secret
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["access"]
    assert payload["refresh"]
    assert "token" not in payload
    assert payload["user"]["username"] == "fleet-admin"


@pytest.mark.django_db
def test_me_requires_authentication() -> None:
    assert APIClient().get("/api/v1/auth/me/").status_code == 401


@pytest.mark.django_db
def test_drf_token_scheme_is_gone(api_user: Any) -> None:
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION="Token 0123456789abcdef")

    assert client.get("/api/v1/auth/me/").status_code == 401


def test_default_permission_and_authentication() -> None:
    from rest_framework.settings import api_settings

    assert [c.__name__ for c in api_settings.DEFAULT_PERMISSION_CLASSES] == [
        "IsAuthenticated"
    ]
    assert [c.__name__ for c in api_settings.DEFAULT_AUTHENTICATION_CLASSES] == [
        "JWTAuthentication"
    ]


@pytest.mark.django_db
def test_no_signup_route() -> None:
    response = APIClient().post(
        "/api/v1/auth/register/",
        {"username": "x", "password": "a-long-enough-password"},
        format="json",
    )

    assert response.status_code == 404


@pytest.mark.django_db
def test_health_stays_public() -> None:
    assert APIClient().get("/health/").status_code == 200


# --- create_operator -----------------------------------------------------------

PASSWORD = "a-long-enough-password"


def _create(username: str, *passwords: str) -> str:
    out = StringIO()
    with mock.patch(
        "apps.api_auth.management.commands.create_operator.getpass.getpass",
        side_effect=list(passwords),
    ):
        call_command("create_operator", username, stdout=out)
    return out.getvalue()


@pytest.mark.django_db
def test_create_operator_creates_a_plain_active_user() -> None:
    _create("renaud", PASSWORD, PASSWORD)

    user = get_user_model().objects.get(username="renaud")
    assert user.check_password(PASSWORD)
    assert user.is_active and not user.is_staff and not user.is_superuser


@pytest.mark.django_db
def test_create_operator_refuses_second_user() -> None:
    _create("renaud", PASSWORD, PASSWORD)

    with pytest.raises(CommandError, match="already exists"):
        _create("someone-else", PASSWORD, PASSWORD)
    assert get_user_model().objects.count() == 1


@pytest.mark.django_db
def test_create_operator_refuses_when_any_user_exists(api_user: Any) -> None:
    with pytest.raises(CommandError):
        _create("renaud", PASSWORD, PASSWORD)


@pytest.mark.django_db
def test_create_operator_requires_matching_passwords() -> None:
    with pytest.raises(CommandError, match="match"):
        _create("renaud", PASSWORD, PASSWORD + "x")
    assert get_user_model().objects.count() == 0


@pytest.mark.django_db
def test_create_operator_applies_the_password_validators() -> None:
    with pytest.raises(CommandError):
        _create("renaud", "short", "short")
    assert get_user_model().objects.count() == 0
