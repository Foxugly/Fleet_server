from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from rest_framework.test import APIClient

from tests.test_api import BOX_BASE, GOOD_HEALTH, cell, client, inventory, snap

__all__ = ["client", "inventory"]  # re-exported pytest fixtures


def reason(source: str, subjects: list[str]) -> dict[str, Any] | None:
    return cell(source, subjects)["reason"]


@pytest.mark.django_db
def test_green_has_no_reason() -> None:
    snap("health", "h", GOOD_HEALTH)
    assert reason("health", ["h"]) is None
    assert cell("health", [])["reason"] is None


@pytest.mark.django_db
def test_health_reasons() -> None:
    snap("health", "a", {**GOOD_HEALTH, "status": 502})
    assert reason("health", ["a"]) == {"code": "http_status", "params": {"status": 502}}
    snap("health", "b", {**GOOD_HEALTH, "status": None, "error": "boom"})
    assert reason("health", ["b"]) == {"code": "unreachable", "params": {}}
    snap("health", "c", {**GOOD_HEALTH, "error": "TLS SSLError: x"})
    assert reason("health", ["c"]) == {"code": "tls_error", "params": {}}
    snap("health", "d", {**GOOD_HEALTH, "tls_days_left": 12})
    assert reason("health", ["d"]) == {"code": "tls_expiring", "params": {"days": 12}}


@pytest.mark.django_db
def test_github_reason_priority() -> None:
    prs = [{"number": 1}, {"number": 2}]
    snap(
        "github",
        "r1",
        {
            "main_run": {"conclusion": "failure", "workflow": "CI"},
            "secret_scanning": [{}],
            "prs": prs,
        },
    )
    assert reason("github", ["r1"]) == {
        "code": "main_ci_failed",
        "params": {"workflow": "CI"},
    }
    snap("github", "r2", {"secret_scanning": [{}]})
    assert reason("github", ["r2"]) == {"code": "secret_alert", "params": {"n": 1}}
    snap("github", "r3", {"dependabot_alerts": [{"severity": "high"}]})
    assert reason("github", ["r3"]) == {"code": "high_alert", "params": {"n": 1}}
    snap("github", "r4", {"prs": [{}, {}, {}]})
    assert reason("github", ["r4"]) == {"code": "open_items", "params": {"n": 3}}


@pytest.mark.django_db
def test_count_reason_aggregates_subjects() -> None:
    snap("github", "r1", {"prs": [{}, {}]})
    snap("github", "r2", {"prs": [{}, {}, {}]})
    c = cell("github", ["r1", "r2"])
    assert c["count"] == 5
    assert c["reason"] == {"code": "open_items", "params": {"n": 5}}


@pytest.mark.django_db
def test_sentry_deps_box_reasons() -> None:
    snap("sentry", "s", {"unresolved": 4})
    assert reason("sentry", ["s"]) == {"code": "unresolved", "params": {"n": 4}}
    snap("deps", "d", {"outdated": [{}, {}]})
    assert reason("deps", ["d"]) == {"code": "outdated", "params": {"n": 2}}

    def box(subject: str, **extra: Any) -> dict[str, Any] | None:
        snap("box", subject, {**BOX_BASE, **extra})
        return reason("box", [subject])

    assert box("b1", failed_units=["x.service"]) == {
        "code": "failed_units",
        "params": {"n": 1},
    }
    assert box("b2", apt=[{"security": True}]) == {
        "code": "security_updates",
        "params": {"n": 1},
    }
    assert box("b3", disk={"mount": "/", "used_pct": 95}) == {
        "code": "disk_full",
        "params": {"pct": 95},
    }
    assert box("b4", reboot_required=True) == {"code": "reboot_required", "params": {}}
    assert box("b5", apt=[{"security": False}, {"security": False}]) == {
        "code": "updates",
        "params": {"n": 2},
    }


@pytest.mark.django_db
def test_meta_reasons() -> None:
    snap("health", "e", ok=False, error="boom")
    assert reason("health", ["e"]) == {"code": "collect_error", "params": {}}

    fresh = snap("health", "old", GOOD_HEALTH, age=10)
    stale = snap("health", "stale", GOOD_HEALTH, age=10**6)
    r = reason("health", ["stale"])
    assert r is not None and r["code"] == "stale"
    assert datetime.fromisoformat(r["params"]["collected_at"]) == stale.collected_at
    assert fresh.pk != stale.pk

    snap("health", "redold", {**GOOD_HEALTH, "status": 500}, age=10**6)
    assert reason("health", ["redold"])["code"] == "http_status"  # type: ignore[index]


@pytest.mark.django_db
def test_overview_payload_has_reason(client: APIClient, inventory: None) -> None:
    snap("health", "alpha.example.com", {**GOOD_HEALTH, "status": 500})
    body = client.get("/api/v1/overview/").json()
    cells = [c for s in body["sites"] for c in s["cells"].values()]
    cells.append(body["box"]["box"])
    assert cells and all("reason" in c for c in cells)
    alpha = next(s for s in body["sites"] if s["slug"] == "alpha")
    assert alpha["cells"]["health"]["reason"]["code"] == "http_status"
