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
            "branch": "deps/pillow-security",
            "prs": [],
            "progress": "none",
            "blocked": None,
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


def test_group_branch_names() -> None:
    sites = [site("a", (A_LOCK, NPM_A)), site("b", (B_LOCK, NPM_B))]
    deps = {
        A_LOCK: dep("Django", "6.0", "6.1.2", "minor"),
        B_LOCK: dep("django", "6.0", "6.1.2", "minor"),
        NPM_A: dep("chart.js", "4.0", "4.5.0", "minor", eco="npm"),
        NPM_B: dep("chart.js", "4.0", "4.5.0", "minor", eco="npm"),
    }
    alerts = {
        "Foxugly/a": [{"package": "pillow", "severity": "high"}],
        "Foxugly/b": [{"package": "pillow", "severity": "high"}],
    }
    got = {g["package"]: g["branch"] for g in group_updates(sites, deps, alerts)}
    assert got == {
        "django": "deps/django-6.1.2",
        "chart.js": "deps/chart.js-4.5.0",
        "pillow": "deps/pillow-security",
    }


def pr(number: int, branch: str, checks: str, **extra: Any) -> dict:
    return {
        "number": number,
        "url": f"https://x/{number}",
        "branch": branch,
        "checks": checks,
        **extra,
    }


def test_progress_states() -> None:
    sites = [site("a", (A_LOCK,)), site("b", (B_LOCK,))]
    deps = {A_LOCK: multi("x"), B_LOCK: multi("x")}
    br = "deps/x-2"

    def run(prs: dict) -> dict:
        return group_updates(sites, deps, {}, prs)[0]

    assert run({})["progress"] == "none"
    assert run({})["prs"] == []
    # a PR on another branch, or without a branch (old snapshot), is ignored
    other = {"Foxugly/a": [pr(1, "deps/y-2", "success"), {"number": 2, "url": "u"}]}
    assert run(other)["progress"] == "none"
    one = run({"Foxugly/a": [pr(1, br, "success")]})
    assert one["progress"] == "in_progress"
    assert one["prs"] == [
        {"repo": "Foxugly/a", "number": 1, "url": "https://x/1", "checks": "success"}
    ]
    both = {"Foxugly/a": [pr(1, br, "success")], "Foxugly/b": [pr(2, br, "success")]}
    assert run(both)["progress"] == "possible"
    draft = {
        "Foxugly/a": [pr(1, br, "success", draft=True)],
        "Foxugly/b": [pr(2, br, "success")],
    }
    assert run(draft)["progress"] == "possible"
    pending = {"Foxugly/a": [pr(1, br, "pending")], "Foxugly/b": [pr(2, br, "success")]}
    assert run(pending)["progress"] == "in_progress"
    nochecks = {"Foxugly/a": [pr(1, br, "none")], "Foxugly/b": [pr(2, br, "success")]}
    assert run(nochecks)["progress"] == "in_progress"
    red = {"Foxugly/a": [pr(1, br, "failure")]}  # blocked beats a missing PR
    assert run(red)["progress"] == "blocked"


def test_prs_only_from_member_repos() -> None:
    sites = [site("a", (A_LOCK,)), site("b", (B_LOCK,)), site("c", (C_LOCK,))]
    deps = {A_LOCK: multi("x"), B_LOCK: multi("x")}
    prs = {"Foxugly/c": [pr(9, "deps/x-2", "success")]}
    g = group_updates(sites, deps, {}, prs)[0]
    assert g["prs"] == [] and g["progress"] == "none"


def fleet_issue(number: int, body: str) -> dict:
    return {
        "number": number,
        "title": "[fleet] django 6.1.2 bloque",
        "url": f"https://x/issues/{number}",
        "body": body,
    }


def blocked_setup(extra_deps: dict | None = None):
    sites = [site("a", (A_LOCK,)), site("b", (B_LOCK,))]
    deps = {A_LOCK: dep("django", "6.0", "6.1.2", "minor"), B_LOCK: multi("x")}
    deps[B_LOCK] = dep("django", "6.0", "6.1.2", "minor")
    deps.update(extra_deps or {})
    return sites, deps


BODY = (
    "Fleet-Update: django 6.1.2\nFleet-Blocked-By: djangorestframework 3.16.1\nraison"
)


def test_waiting_when_blocker_not_outdated() -> None:
    sites, deps = blocked_setup()
    issues = {"Foxugly/a": [fleet_issue(8, BODY)]}
    g = group_updates(sites, deps, {}, {}, issues)[0]
    assert g["progress"] == "waiting"
    assert g["blocked"] == {
        "issues": [{"repo": "Foxugly/a", "number": 8, "url": "https://x/issues/8"}],
        "by": [{"package": "djangorestframework", "version": "3.16.1", "newer": None}],
    }


def test_retry_when_newer_blocker_exists() -> None:
    sites, deps = blocked_setup()
    deps[C_LOCK] = dep("djangorestframework", "3.16.1", "3.17.0", "minor")
    sites.append(site("c", (C_LOCK,)))
    issues = {"Foxugly/a": [fleet_issue(8, BODY)]}
    g = [
        g
        for g in group_updates(sites, deps, {}, {}, issues)
        if g["package"] == "django"
    ][0]
    assert g["progress"] == "retry"
    assert g["blocked"]["by"][0]["newer"] == "3.17.0"


def test_parsing_tolerates_crlf_case_and_noise() -> None:
    sites, deps = blocked_setup()
    body = (
        "Bonjour\r\nFLEET-UPDATE: Django 6.1.2\r\n"
        "fleet-blocked-by: drf 1.0\r\nfleet-blocked-by: drf 1.0\r\nfin"
    )
    g = group_updates(sites, deps, {}, {}, {"Foxugly/b": [fleet_issue(1, body)]})[0]
    assert g["progress"] == "waiting"
    assert g["blocked"]["by"] == [{"package": "drf", "version": "1.0", "newer": None}]


def test_issue_for_other_version_or_non_member_ignored() -> None:
    sites, deps = blocked_setup()
    sites.append(site("c", (C_LOCK,)))
    other = BODY.replace("6.1.2", "6.1.1")
    issues = {
        "Foxugly/a": [fleet_issue(1, other)],
        "Foxugly/c": [fleet_issue(2, BODY)],
    }
    g = group_updates(sites, deps, {}, {}, issues)[0]
    assert g["progress"] == "none" and g["blocked"] is None


def test_pr_wins_over_issue() -> None:
    sites, deps = blocked_setup()
    issues = {"Foxugly/a": [fleet_issue(8, BODY)]}
    prs = {"Foxugly/a": [pr(3, "deps/django-6.1.2", "pending")]}
    g = group_updates(sites, deps, {}, prs, issues)[0]
    assert g["progress"] == "in_progress"
    assert g["blocked"] is not None


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


@pytest.mark.django_db
def test_updates_view_links_prs(
    auth_client: APIClient, settings: Any, tmp_path: Any
) -> None:
    inv = tmp_path / "inv.yaml"
    inv.write_text(THREE_SITES, encoding="utf-8")
    settings.FLEET_INVENTORY_PATH = str(inv)
    for s in ("alpha", "beta"):
        snap(
            "deps",
            f"Foxugly/{s}:requirements.txt",
            dep("django", "6.0", "6.1", "minor"),
        )
    snap("github", "Foxugly/alpha", {"prs": [pr(5, "deps/django-6.1", "success")]})
    g = auth_client.get("/api/v1/updates/").json()["groups"][0]
    assert g["branch"] == "deps/django-6.1"
    assert g["progress"] == "in_progress"
    assert [p["number"] for p in g["prs"]] == [5]


@pytest.mark.django_db
def test_latest_ok_many() -> None:
    from apps.collect import store

    snap("deps", "a", {"v": 1}, age=100)
    snap("deps", "a", ok=False, error="boom", age=10)
    snap("deps", "b", {"v": 1}, age=100)
    newest = snap("deps", "b", {"v": 2}, age=50)
    snap("deps", "other", {"v": 9})
    snap("github", "a", {"v": 7})
    got = store.latest_ok_many("deps", ["a", "b", "never"])
    assert set(got) == {"a", "b"}
    assert got["a"].data == {"v": 1}
    assert got["b"].id == newest.id
