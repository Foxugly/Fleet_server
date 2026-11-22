"""Inventory write API: TOTP step-up, validation, audit, throttle."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api_auth.models import MfaDevice
from apps.inventory.models import App, InventoryChange
from tests.helpers import make_apps
from tests.mfa_helpers import current_code, enroll, login
from tests.test_api import CREDS

URL = "/api/v1/apps/"
NO_CODE = {"code": "totp_invalid"}


def _body(**over: Any) -> dict[str, Any]:
    base = {
        "slug": "new-app",
        "repos": ["Foxugly/New"],
        "vhosts": ["new.foxugly.com"],
        "health_path": "/health/",
        "sentry_projects": ["new-backend"],
        "python_locks": ["Foxugly/New:requirements.txt"],
        "npm_locks": [],
        "deployed": True,
        "actions_allowed": False,
    }
    return {**base, **over}


@pytest.fixture
def secret(api_user: Any) -> str:
    return enroll(api_user)


@pytest.fixture
def client(api_user: Any, secret: str) -> APIClient:
    c = APIClient()
    resp = login(c, CREDS, secret)
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {resp.json()['access']}")
    return c


@pytest.fixture
def code(secret: str):
    """A fresh valid code per call (replay guard reset: it has its own test)."""

    def make() -> str:
        MfaDevice.objects.update(last_used_step=None)
        return current_code(secret)

    return make


@pytest.fixture
def apps_db(db: Any) -> None:
    make_apps(
        "sites:\n"
        "  - slug: alpha\n    repos: [Foxugly/alpha]\n"
        "    vhosts: [alpha.example.com]\n"
        "    deployed: true\n    actions_allowed: false\n"
    )


def _post(client: APIClient, url: str, code_value: str, **body: Any) -> Any:
    return client.post(url, {**body, "totp_code": code_value}, format="json")


# --- reads -------------------------------------------------------------------


@pytest.mark.django_db
def test_unauthenticated_gets_401(apps_db: None) -> None:
    c = APIClient()
    for method, url in [
        ("get", URL),
        ("get", f"{URL}alpha/"),
        ("get", f"{URL}alpha/changes/"),
        ("post", URL),
        ("put", f"{URL}alpha/"),
        ("post", f"{URL}alpha/archive/"),
        ("post", f"{URL}alpha/restore/"),
    ]:
        assert getattr(c, method)(url).status_code == 401, (method, url)


@pytest.mark.django_db
def test_list_and_retrieve_include_archived(client: APIClient, apps_db: None) -> None:
    App.objects.create(slug="old", archived_at=timezone.now())
    body = client.get(URL).json()
    assert [a["slug"] for a in body] == ["alpha", "old"]
    assert [a["archived"] for a in body] == [False, True]
    one = client.get(f"{URL}alpha/").json()
    assert one["repos"] == ["Foxugly/alpha"] and one["archived"] is False
    assert client.get(f"{URL}old/").json()["archived"] is True
    assert client.get(f"{URL}nope/").status_code == 404


# --- writes with a valid code ------------------------------------------------


@pytest.mark.django_db
def test_create_writes_row_and_audit(client: APIClient, apps_db: None, code) -> None:
    resp = _post(client, URL, code(), **_body())
    assert resp.status_code == 201, resp.content
    assert resp.json()["slug"] == "new-app" and resp.json()["archived"] is False
    assert App.objects.get(slug="new-app").as_dict() == _body()
    change = InventoryChange.objects.get()
    assert (change.action, change.app_slug) == ("create", "new-app")
    assert change.before is None and change.after == _body()
    assert change.user.username == "fleet-admin"


@pytest.mark.django_db
def test_update_writes_row_and_audit(client: APIClient, apps_db: None, code) -> None:
    before = App.objects.get(slug="alpha").as_dict()
    new = _body(
        slug="alpha",
        repos=["Foxugly/alpha"],
        python_locks=[],
        vhosts=["a2.example.com"],
    )
    resp = client.put(f"{URL}alpha/", {**new, "totp_code": code()}, format="json")
    assert resp.status_code == 200, resp.content
    assert App.objects.get(slug="alpha").as_dict() == new
    change = InventoryChange.objects.get()
    assert change.action == "update"
    assert change.before == before and change.after == new


@pytest.mark.django_db
def test_archive_then_restore(client: APIClient, apps_db: None, code) -> None:
    fields = App.objects.get(slug="alpha").as_dict()
    resp = _post(client, f"{URL}alpha/archive/", code())
    assert resp.status_code == 200 and resp.json()["archived"] is True
    assert App.objects.get(slug="alpha").archived_at is not None
    resp = _post(client, f"{URL}alpha/restore/", code())
    assert resp.status_code == 200 and resp.json()["archived"] is False
    assert App.objects.get(slug="alpha").archived_at is None
    rows = InventoryChange.objects.order_by("id")
    assert [(c.action, c.before, c.after) for c in rows] == [
        ("archive", fields, fields),
        ("restore", fields, fields),
    ]


@pytest.mark.django_db
def test_archive_twice_and_restore_active_are_400(
    client: APIClient, apps_db: None, code
) -> None:
    assert _post(client, f"{URL}alpha/restore/", code()).status_code == 400
    assert _post(client, f"{URL}alpha/archive/", code()).status_code == 200
    assert _post(client, f"{URL}alpha/archive/", code()).status_code == 400
    assert InventoryChange.objects.count() == 1
    assert _post(client, f"{URL}nope/archive/", code()).status_code == 404


@pytest.mark.django_db
def test_changes_newest_first(client: APIClient, apps_db: None, code) -> None:
    _post(client, f"{URL}alpha/archive/", code())
    _post(client, f"{URL}alpha/restore/", code())
    old = InventoryChange.objects.get(action="archive")
    day_ago = timezone.now() - timedelta(days=1)
    InventoryChange.objects.filter(pk=old.pk).update(at=day_ago)
    rows = client.get(f"{URL}alpha/changes/").json()
    assert [r["action"] for r in rows] == ["restore", "archive"]
    assert set(rows[0]) == {"at", "user", "action", "before", "after"}
    assert rows[0]["user"] == "fleet-admin"
    assert client.get(f"{URL}nope/changes/").status_code == 404


# --- TOTP step-up (Review Focus 3) -------------------------------------------


def _assert_all_writes_refused(client: APIClient, code_value: str | None) -> None:
    before = App.objects.get(slug="alpha").as_dict()
    extra = {} if code_value is None else {"totp_code": code_value}
    calls = [
        ("post", URL, _body()),
        ("put", f"{URL}alpha/", _body(slug="alpha")),
        ("post", f"{URL}alpha/archive/", {}),
        ("post", f"{URL}alpha/restore/", {}),
    ]
    for method, url, body in calls:
        resp = getattr(client, method)(url, {**body, **extra}, format="json")
        assert resp.status_code == 403, (method, url, resp.content)
        assert resp.json() == NO_CODE
    assert App.objects.count() == 1
    assert App.objects.get(slug="alpha").as_dict() == before
    assert App.objects.get(slug="alpha").archived_at is None
    assert InventoryChange.objects.count() == 0


@pytest.mark.django_db
def test_wrong_code_writes_nothing(
    client: APIClient, apps_db: None, secret: str
) -> None:
    good = current_code(secret)
    wrong = "000000" if good != "000000" else "111111"
    _assert_all_writes_refused(client, wrong)


@pytest.mark.django_db
def test_missing_code_writes_nothing(client: APIClient, apps_db: None) -> None:
    _assert_all_writes_refused(client, None)


@pytest.mark.django_db
def test_replayed_code_writes_nothing(client: APIClient, apps_db: None, code) -> None:
    used = code()
    assert _post(client, f"{URL}alpha/archive/", used).status_code == 200
    InventoryChange.objects.all().delete()
    App.objects.filter(slug="alpha").update(archived_at=None)
    resp = _post(client, f"{URL}alpha/archive/", used)
    assert resp.status_code == 403 and resp.json() == NO_CODE
    assert App.objects.get(slug="alpha").archived_at is None
    assert InventoryChange.objects.count() == 0


@pytest.mark.django_db
def test_locked_factor_writes_nothing(client: APIClient, apps_db: None, code) -> None:
    valid = code()
    MfaDevice.objects.update(locked_until=timezone.now() + timedelta(minutes=5))
    _assert_all_writes_refused(client, valid)


@pytest.mark.django_db
def test_code_is_checked_before_validation(client: APIClient, apps_db: None) -> None:
    resp = _post(client, URL, "000000", slug="Bad Slug")
    assert resp.status_code == 403 and resp.json() == NO_CODE


# --- validation (Review Focus 4) ---------------------------------------------


def _post_invalid(client: APIClient, code, **over: Any) -> Any:
    resp = _post(client, URL, code(), **_body(**over))
    assert resp.status_code == 400, resp.content
    assert InventoryChange.objects.count() == 0
    return resp.json()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("over", "field"),
    [
        ({"python_locks": ["Foxugly/Other:x.txt"]}, "python_locks"),
        ({"repos": "Foxugly/New"}, "repos"),
        ({"slug": "alpha"}, "slug"),  # duplicate
        ({"vhosts": ["https://x.com"]}, "vhosts"),
        ({"slug": "x" * 65}, "slug"),
        ({"health_path": "/" + "h" * 200}, "health_path"),
        ({"repos": ["Foxugly/" + "r" * 200]}, "repos"),
        ({"vhosts": ["a.com"] * 51}, "vhosts"),
    ],
)
def test_validation_errors_are_field_keyed(
    client: APIClient, apps_db: None, code, over: dict[str, Any], field: str
) -> None:
    assert field in _post_invalid(client, code, **over)
    assert App.objects.count() == 1


@pytest.mark.django_db
def test_put_cannot_change_slug(client: APIClient, apps_db: None, code) -> None:
    body = _body(slug="renamed", repos=["Foxugly/alpha"], python_locks=[])
    resp = client.put(f"{URL}alpha/", {**body, "totp_code": code()}, format="json")
    assert resp.status_code == 400 and "slug" in resp.json()
    assert App.objects.filter(slug="renamed").count() == 0
    assert InventoryChange.objects.count() == 0
    missing = client.put(f"{URL}nope/", {**body, "totp_code": code()}, format="json")
    assert missing.status_code == 404


@pytest.mark.django_db
def test_put_without_slug_uses_the_url(client: APIClient, apps_db: None, code) -> None:
    body = _body(repos=["Foxugly/alpha"], python_locks=[])
    del body["slug"]
    resp = client.put(f"{URL}alpha/", {**body, "totp_code": code()}, format="json")
    assert resp.status_code == 200, resp.content


# --- throttle ----------------------------------------------------------------


@pytest.mark.django_db
def test_writes_are_throttled(client: APIClient, apps_db: None) -> None:
    statuses = [
        _post(client, f"{URL}alpha/archive/", "000000").status_code for _ in range(7)
    ]
    assert statuses[0] == 403 and statuses[-1] == 429
    assert InventoryChange.objects.count() == 0
