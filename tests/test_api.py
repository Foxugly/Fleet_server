from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.api import status
from apps.collect import tasks
from apps.collect.models import Snapshot
from tests.mfa_helpers import enroll, login

CREDS = {"username": "fleet-admin", "password": "fleet-password"}

INVENTORY = """
sites:
  - slug: alpha
    repos: [Foxugly/alpha]
    vhosts: [alpha.example.com]
    sentry_projects: []
    python_locks: [Foxugly/alpha:requirements.txt]
    npm_locks: []
    deployed: true
    actions_allowed: false
  - slug: beta
    repos: [Foxugly/beta]
    vhosts: [beta.example.com]
    deployed: true
    actions_allowed: false
"""

GOOD_HEALTH = {"status": 200, "latency_ms": 5, "error": "", "tls_days_left": 60}
BOX_BASE = {
    "apt": [],
    "reboot_required": False,
    "failed_units": [],
    "disk": {"mount": "/", "used_pct": 50},
}


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


def snap(
    source: str,
    subject: str,
    data: dict | None = None,
    *,
    age: int = 10,
    ok: bool = True,
    error: str = "",
) -> Snapshot:
    return Snapshot.objects.create(
        source=source,
        subject=subject,
        ok=ok,
        data=data or {},
        error=error,
        collected_at=timezone.now() - timedelta(seconds=age),
    )


def cell(source: str, subjects: list[str]) -> dict[str, Any]:
    return status.cell(source, subjects, timezone.now())


@pytest.mark.django_db
def test_overview_requires_auth(api_user: Any) -> None:
    assert APIClient().get("/api/v1/overview/").status_code == 401
    c = APIClient()
    first = c.post("/api/v1/auth/login/", CREDS, format="json")
    token = first.json().get("mfa_token")
    assert token
    c.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    assert c.get("/api/v1/overview/").status_code == 401


@pytest.mark.django_db
def test_overview_red_first(client: APIClient, inventory: None) -> None:
    snap("health", "alpha.example.com", GOOD_HEALTH)
    snap("health", "beta.example.com", {**GOOD_HEALTH, "status": 500})
    body = client.get("/api/v1/overview/").json()
    assert body["sites"][0]["slug"] == "beta"
    assert body["sites"][0]["cells"]["health"]["state"] == "red"
    assert body["box"]["box"]["state"] == "stale"
    assert body["sites"][1]["cells"]["sentry"]["state"] == "none"


@pytest.mark.django_db
def test_cell_error_keeps_last_good_age() -> None:
    snap("health", "h", GOOD_HEALTH, age=600)
    snap("health", "h", ok=False, error="boom", age=5)
    c = cell("health", ["h"])
    assert c["state"] == "error" and c["error"] == "boom"
    age = (timezone.now() - datetime.fromisoformat(c["collected_at"])).total_seconds()
    assert 590 < age < 640


@pytest.mark.django_db
def test_cell_stale_after_three_intervals() -> None:
    snap("health", "h", GOOD_HEALTH, age=16 * 60)
    snap("health", "fresh", GOOD_HEALTH, age=14 * 60)
    assert cell("health", ["h"])["state"] == "stale"
    assert cell("health", ["fresh"])["state"] == "green"


@pytest.mark.django_db
def test_cell_never_collected_is_stale_and_empty_is_none() -> None:
    snap("health", "h", GOOD_HEALTH)
    assert cell("health", ["h"])["state"] == "green"
    assert cell("health", ["h", "missing"])["state"] == "stale"
    assert cell("health", [])["state"] == "none"


@pytest.mark.django_db
def test_major_dep_is_orange() -> None:
    outdated = [{"name": "x", "current": "1", "latest": "2", "kind": "major"}]
    snap("deps", "r:req.txt", {"ecosystem": "pypi", "outdated": outdated})
    c = cell("deps", ["r:req.txt"])
    assert (c["state"], c["count"]) == ("orange", 1)


@pytest.mark.django_db
def test_secret_alert_is_red() -> None:
    snap("github", "Foxugly/r", {"secret_scanning": [{"type": "t", "url": "u"}]})
    assert cell("github", ["Foxugly/r"])["state"] == "red"


@pytest.mark.django_db
def test_tls_failure_and_low_days_are_red() -> None:
    snap("health", "a", {**GOOD_HEALTH, "error": "TLS SSLError: bad"})
    snap("health", "b", {**GOOD_HEALTH, "tls_days_left": 13})
    assert cell("health", ["a"])["state"] == "red"
    assert cell("health", ["b"])["state"] == "red"


@pytest.mark.django_db
def test_box_rules() -> None:
    snap("box", "box", {**BOX_BASE, "reboot_required": True}, age=20)
    c = cell("box", ["box"])
    assert (c["state"], c["count"]) == ("orange", 1)
    snap("box", "box", {**BOX_BASE, "disk": {"mount": "/", "used_pct": 90}})
    assert cell("box", ["box"])["state"] == "red"


@pytest.mark.django_db
def test_site_detail_and_404(client: APIClient, inventory: None) -> None:
    snap("health", "alpha.example.com", GOOD_HEALTH)
    body = client.get("/api/v1/sites/alpha/").json()
    item = body["sources"]["health"]["items"]["alpha.example.com"]
    assert item["data"]["status"] == 200 and item["ok"] is True
    assert body["sources"]["sentry"]["cell"]["state"] == "none"
    assert client.get("/api/v1/sites/nope/").status_code == 404


@pytest.mark.django_db
def test_box_endpoint(client: APIClient) -> None:
    body = client.get("/api/v1/box/").json()
    assert body["cell"]["state"] == "stale" and body["data"] is None


@pytest.mark.django_db
def test_refresh_queues(client: APIClient, monkeypatch: Any) -> None:
    called: list[str] = []
    monkeypatch.setattr(tasks.collect_health, "delay", lambda: called.append("h"))
    resp = client.post("/api/v1/refresh/health/")
    assert resp.status_code == 202 and resp.json() == {"queued": "health"}
    assert called == ["h"]


@pytest.mark.django_db
def test_refresh_unknown_source_404(client: APIClient) -> None:
    assert client.post("/api/v1/refresh/nope/").status_code == 404


@pytest.mark.django_db
def test_refresh_is_throttled(client: APIClient, monkeypatch: Any) -> None:
    monkeypatch.setattr(tasks.collect_box_task, "delay", lambda: None)
    codes = [client.post("/api/v1/refresh/box/").status_code for _ in range(7)]
    assert codes[:6] == [202] * 6 and codes[6] == 429


@pytest.mark.django_db
def test_schema_served(client: APIClient) -> None:
    assert APIClient().get("/api/v1/schema/").status_code == 401
    resp = client.get("/api/v1/schema/?format=json")
    assert resp.status_code == 200
    schema = resp.json()
    assert schema["info"]["title"] == "Fleet API"
    assert "/api/v1/overview/" in schema["paths"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "conclusion", ["failure", "startup_failure", "cancelled", "timed_out", None]
)
def test_main_run_not_ok_is_red(conclusion: str | None) -> None:
    snap("github", "Foxugly/r", {"main_run": {"conclusion": conclusion}})
    c = cell("github", ["Foxugly/r"])
    assert (c["state"], c["count"]) == ("red", 1)


@pytest.mark.django_db
@pytest.mark.parametrize("conclusion", ["success", "skipped", "neutral"])
def test_main_run_ok_is_green(conclusion: str) -> None:
    snap("github", "Foxugly/r", {"main_run": {"conclusion": conclusion}})
    assert cell("github", ["Foxugly/r"])["state"] == "green"


@pytest.mark.django_db
def test_no_main_run_is_not_red() -> None:
    snap("github", "Foxugly/r", {"main_run": None})
    assert cell("github", ["Foxugly/r"])["state"] == "green"


@pytest.mark.django_db
def test_stale_red_stays_red_stale_green_is_stale() -> None:
    snap("health", "down", {**GOOD_HEALTH, "status": 500}, age=16 * 60)
    snap("health", "up", GOOD_HEALTH, age=16 * 60)
    assert cell("health", ["down"])["state"] == "red"
    assert cell("health", ["up"])["state"] == "stale"


@pytest.mark.django_db
def test_stale_orange_becomes_stale() -> None:
    outdated = [{"name": "x", "current": "1", "latest": "2", "kind": "major"}]
    snap("deps", "r:req.txt", {"outdated": outdated}, age=10 * 86400)
    assert cell("deps", ["r:req.txt"])["state"] == "stale"


@pytest.mark.django_db
def test_red_cell_count_is_at_least_one() -> None:
    snap("box", "box", {**BOX_BASE, "disk": {"mount": "/", "used_pct": 95}})
    c = cell("box", ["box"])
    assert (c["state"], c["count"]) == ("red", 1)


@pytest.mark.django_db
def test_refresh_dedupes_per_source(client: APIClient, monkeypatch: Any) -> None:
    called: list[str] = []
    monkeypatch.setattr(tasks.collect_github, "delay", lambda: called.append("g"))
    monkeypatch.setattr(tasks.collect_sentry, "delay", lambda: called.append("s"))
    first = client.post("/api/v1/refresh/github/")
    second = client.post("/api/v1/refresh/github/")
    other = client.post("/api/v1/refresh/sentry/")
    assert first.json() == {"queued": "github"}
    assert second.status_code == 202
    assert second.json() == {"queued": "github", "already_queued": True}
    assert other.json() == {"queued": "sentry"}
    assert called == ["g", "s"]
