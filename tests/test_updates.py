from __future__ import annotations

from typing import Any

import pytest
from rest_framework.test import APIClient

from apps.api.updates import group_updates
from apps.inventory.loader import Site
from tests.test_api import snap


def site(slug: str, locks: tuple[str, ...] = ()) -> Site:
    repos = tuple(dict.fromkeys(lk.partition(":")[0] for lk in locks))
    return Site(
        slug=slug,
        repos=repos or (f"Foxugly/{slug}",),
        vhosts=(),
        health_path="/health/",
        sentry_projects=(),
        python_locks=tuple(lk for lk in locks if not lk.endswith(".json")),
        npm_locks=tuple(lk for lk in locks if lk.endswith(".json")),
        deployed=True,
        actions_allowed=False,
    )


def dep(name: str, current: str, latest: str, kind: str, eco: str = "python") -> dict:
    return {
        "ecosystem": eco,
        "checked": 1,
        "outdated": [
            {"name": name, "current": current, "latest": latest, "kind": kind}
        ],
    }


def multi(*names: str) -> dict:
    return {
        "ecosystem": "python",
        "checked": len(names),
        "outdated": [
            {"name": n, "current": "1", "latest": "2", "kind": "patch"} for n in names
        ],
    }


A_LOCK = "Foxugly/a:requirements.txt"
B_LOCK = "Foxugly/b:requirements.txt"
C_LOCK = "Foxugly/c:requirements.txt"


def test_groups_across_two_sites_with_worst_kind() -> None:
    sites = [site("a", (A_LOCK,)), site("b", (B_LOCK,))]
    deps = {
        A_LOCK: dep("django", "6.0.1", "6.1.2", "minor"),
        B_LOCK: dep("django", "5.2", "6.1.2", "major"),
    }
    groups = group_updates(sites, deps, {})
    assert len(groups) == 1
    g = groups[0]
    assert (g["ecosystem"], g["package"], g["latest"], g["kind"]) == (
        "python",
        "django",
        "6.1.2",
        "major",
    )
    assert g["security"] is None
    assert g["members"] == [
        {"site": "a", "repo": "Foxugly/a", "lock": A_LOCK, "current": "6.0.1"},
        {"site": "b", "repo": "Foxugly/b", "lock": B_LOCK, "current": "5.2"},
    ]


def test_single_site_is_not_a_group() -> None:
    lock2 = "Foxugly/a:backend/requirements.txt"
    sites = [site("a", (A_LOCK, lock2)), site("b", (B_LOCK,))]
    deps = {
        A_LOCK: dep("django", "6.0", "6.1", "minor"),
        lock2: dep("django", "6.0", "6.1", "minor"),
    }
    assert group_updates(sites, deps, {}) == []
    deps[B_LOCK] = dep("django", "6.0", "6.1", "minor")
    groups = group_updates(sites, deps, {})
    assert len(groups) == 1
    assert len(groups[0]["members"]) == 3


def test_name_normalisation_merges_spellings() -> None:
    sites = [site("a", (A_LOCK,)), site("b", (B_LOCK,))]
    deps = {
        A_LOCK: dep("Typing_Extensions", "4.0", "4.1", "minor"),
        B_LOCK: dep("typing-extensions", "4.0", "4.1", "minor"),
    }
    groups = group_updates(sites, deps, {})
    assert [g["package"] for g in groups] == ["typing-extensions"]


def test_security_attaches_and_sorts_first() -> None:
    sites = [site("a", (A_LOCK,)), site("b", (B_LOCK,)), site("c", (C_LOCK,))]
    deps = {
        A_LOCK: multi("requests", "zlib"),
        B_LOCK: multi("requests", "zlib"),
        C_LOCK: multi("zlib"),
    }
    alerts = {"Foxugly/a": [{"package": "requests", "severity": "high"}]}
    groups = group_updates(sites, deps, alerts)
    assert [g["package"] for g in groups] == ["requests", "zlib"]
    assert groups[0]["security"] == {"alerts": 1, "max_severity": "high"}
    assert groups[1]["security"] is None


def test_alert_only_group() -> None:
    sites = [site("a", (A_LOCK,)), site("b", (B_LOCK,))]
    alerts = {
        "Foxugly/a": [{"package": "Pillow", "severity": "critical"}],
        "Foxugly/b": [{"package": "pillow", "severity": "low"}],
    }
    assert group_updates(sites, {}, alerts) == [
        {
            "ecosystem": None,
            "package": "pillow",
            "latest": None,
            "kind": None,
            "members": [
                {"site": "a", "repo": "Foxugly/a", "lock": None, "current": None},
                {"site": "b", "repo": "Foxugly/b", "lock": None, "current": None},
            ],
            "security": {"alerts": 2, "max_severity": "critical"},
        }
    ]


def test_alert_in_one_site_only_is_not_a_group() -> None:
    sites = [site("a", (A_LOCK,)), site("b", (B_LOCK,))]
    alerts = {"Foxugly/a": [{"package": "pillow", "severity": "high"}]}
    assert group_updates(sites, {}, alerts) == []


def test_sort_by_sites_then_name() -> None:
    sites = [site("a", (A_LOCK,)), site("b", (B_LOCK,)), site("c", (C_LOCK,))]
    deps = {
        A_LOCK: multi("zeta", "alpha", "wide"),
        B_LOCK: multi("zeta", "alpha", "wide"),
        C_LOCK: multi("wide"),
    }
    groups = group_updates(sites, deps, {})
    assert [g["package"] for g in groups] == ["wide", "alpha", "zeta"]


NPM_A = "Foxugly/a:package-lock.json"
NPM_B = "Foxugly/b:package-lock.json"


def test_npm_names_are_not_python_normalised() -> None:
    sites = [site("a", (NPM_A,)), site("b", (NPM_B,))]
    deps = {
        NPM_A: dep("chart.js", "4.0", "4.1", "minor", eco="npm"),
        NPM_B: dep("chart.js", "4.0", "4.1", "minor", eco="npm"),
    }
    assert [g["package"] for g in group_updates(sites, deps, {})] == ["chart.js"]


def test_npm_case_merges_and_keeps_first_spelling() -> None:
    sites = [site("a", (NPM_A,)), site("b", (NPM_B,))]
    deps = {
        NPM_A: dep("Foo", "1.0", "1.1", "minor", eco="npm"),
        NPM_B: dep("foo", "1.0", "1.1", "minor", eco="npm"),
    }
    groups = group_updates(sites, deps, {})
    assert [g["package"] for g in groups] == ["Foo"]
    assert len(groups[0]["members"]) == 2


def test_python_spellings_still_merge() -> None:
    sites = [site("a", (A_LOCK,)), site("b", (B_LOCK,))]
    deps = {
        A_LOCK: dep("typing_extensions", "4.0", "4.1", "minor"),
        B_LOCK: dep("Typing-Extensions", "4.0", "4.1", "minor"),
    }
    assert [g["package"] for g in group_updates(sites, deps, {})] == [
        "typing-extensions"
    ]


def test_alert_in_non_member_site_is_not_counted() -> None:
    sites = [site("a", (A_LOCK,)), site("b", (B_LOCK,)), site("c", (C_LOCK,))]
    deps = {A_LOCK: multi("x"), B_LOCK: multi("x")}
    alerts = {"Foxugly/c": [{"package": "x", "severity": "critical"}]}
    groups = group_updates(sites, deps, alerts)
    assert len(groups) == 1
    assert groups[0]["security"] is None
    assert {m["site"] for m in groups[0]["members"]} == {"a", "b"}


def test_shared_repo_alerts_count_once() -> None:
    shared = Site(**{**site("a", (A_LOCK,)).__dict__, "slug": "a2"})
    sites = [site("a", (A_LOCK,)), shared, site("b", (B_LOCK,))]
    deps = {A_LOCK: multi("x"), B_LOCK: multi("x")}
    alerts = {"Foxugly/a": [{"package": "x", "severity": "high"}]}
    groups = group_updates(sites, deps, alerts)
    assert groups[0]["security"] == {"alerts": 1, "max_severity": "high"}


def test_severity_normalised() -> None:
    sites = [site("a", (A_LOCK,)), site("b", (B_LOCK,))]
    deps = {A_LOCK: multi("x", "y"), B_LOCK: multi("x", "y")}
    alerts = {
        "Foxugly/a": [
            {"package": "x", "severity": "MODERATE"},
            {"package": "y", "severity": ""},
        ]
    }
    by_pkg = {g["package"]: g for g in group_updates(sites, deps, alerts)}
    assert by_pkg["x"]["security"]["max_severity"] == "medium"
    assert by_pkg["y"]["security"]["max_severity"] == "low"


def test_kind_ranking_minor_over_patch_over_unknown() -> None:
    sites = [site("a", (A_LOCK,)), site("b", (B_LOCK,)), site("c", (C_LOCK,))]
    kinds = {
        ("minor", "patch", "unknown"): "minor",
        ("patch", "unknown", "unknown"): "patch",
        ("unknown", "unknown", "unknown"): "unknown",
    }
    for combo, expected in kinds.items():
        deps = {
            lock: dep("p", "1", "2", k)
            for lock, k in zip((A_LOCK, B_LOCK, C_LOCK), combo, strict=True)
        }
        assert group_updates(sites, deps, {})[0]["kind"] == expected


def test_alert_only_suppressed_when_outdated_group_kept() -> None:
    sites = [site("a", (A_LOCK,)), site("b", (B_LOCK,))]
    deps = {A_LOCK: multi("pillow"), B_LOCK: multi("pillow")}
    alerts = {
        "Foxugly/a": [{"package": "pillow", "severity": "high"}],
        "Foxugly/b": [{"package": "Pillow", "severity": "high"}],
    }
    groups = group_updates(sites, deps, alerts)
    assert len(groups) == 1
    assert groups[0]["latest"] == "2"
    assert groups[0]["security"] == {"alerts": 2, "max_severity": "high"}


@pytest.mark.django_db
def test_updates_endpoint_shape_and_auth(
    auth_client: APIClient, inventory: None
) -> None:
    assert APIClient().get("/api/v1/updates/").status_code == 401
    resp = auth_client.get("/api/v1/updates/")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"generated_at", "groups"}
    assert body["groups"] == []


THREE_SITES = """
sites:
""" + "".join(
    f"""  - slug: {s}
    repos: [Foxugly/{s}]
    python_locks: [Foxugly/{s}:requirements.txt]
    deployed: true
    actions_allowed: false
"""
    for s in ("alpha", "beta", "gamma")
)


@pytest.mark.django_db
def test_updates_uses_last_good_snapshot(
    auth_client: APIClient, settings: Any, tmp_path: Any
) -> None:
    inv = tmp_path / "inv.yaml"
    inv.write_text(THREE_SITES, encoding="utf-8")
    settings.FLEET_INVENTORY_PATH = str(inv)
    lock = "Foxugly/alpha:requirements.txt"
    snap("deps", lock, dep("django", "6.0", "6.1", "minor"), age=100)
    snap("deps", lock, ok=False, error="boom", age=10)
    snap("deps", "Foxugly/beta:requirements.txt", dep("django", "6.0", "6.1", "minor"))
    # gamma was never collected: skipped, no crash
    body = auth_client.get("/api/v1/updates/").json()
    assert len(body["groups"]) == 1
    g = body["groups"][0]
    assert g["package"] == "django"
    assert [m["site"] for m in g["members"]] == ["alpha", "beta"]
