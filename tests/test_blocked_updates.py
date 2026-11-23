"""Outdated items another installed package holds back (`blocked_by`)."""

from __future__ import annotations

from typing import Any

import pytest
from rest_framework.test import APIClient

from apps.api.updates import group_updates
from tests.helpers import make_apps
from tests.test_api import cell, snap
from tests.test_updates import A_LOCK, B_LOCK, BODY, fleet_issue, site

CLI = {"package": "@angular/compiler-cli", "version": "22.3.0", "range": ">=6.0 <6.1"}


def ts(newer: str | None = None, **blocker: Any) -> dict:
    return {
        "name": "typescript",
        "current": "6.0.3",
        "latest": "7.0.2",
        "kind": "major",
        "blocked_by": [{**CLI, "newer": newer, **blocker}],
    }


def plain(name: str = "rxjs") -> dict:
    return {"name": name, "current": "7.8.1", "latest": "7.8.2", "kind": "patch"}


def npm(*items: dict) -> dict:
    return {"ecosystem": "npm", "checked": 10, "outdated": list(items)}


BLOCKED_NOTE = {
    "code": "blocked_updates",
    "params": {
        "n": 1,
        "package": "typescript",
        "latest": "7.0.2",
        "by": "@angular/compiler-cli",
        "version": "22.3.0",
    },
}


@pytest.mark.django_db
def test_only_blocked_items_is_green_with_a_reason() -> None:
    snap("deps", "l", npm(ts()))
    c = cell("deps", ["l"])
    assert c["state"] == "green"
    assert c["count"] == 0
    assert c["reason"] == BLOCKED_NOTE
    assert c["notes"] == [BLOCKED_NOTE]


@pytest.mark.django_db
def test_mixed_items_stay_orange_counting_unblocked_only() -> None:
    snap("deps", "l", npm(ts(), plain(), plain("zone.js")))
    c = cell("deps", ["l"])
    assert c["state"] == "orange"
    assert c["count"] == 2
    assert c["reason"] == {"code": "outdated", "params": {"n": 2}}
    assert c["notes"] == [BLOCKED_NOTE]


@pytest.mark.django_db
def test_notes_sum_over_subjects_and_plain_cells_have_none() -> None:
    snap("deps", "l1", npm(ts()))
    snap("deps", "l2", npm(ts(), {**ts(), "name": "other"}))
    c = cell("deps", ["l1", "l2"])
    assert c["state"] == "green"
    assert c["notes"][0]["params"]["n"] == 3
    assert c["notes"][0]["params"]["package"] == "typescript"
    snap("deps", "fresh", npm())
    assert cell("deps", ["fresh"])["notes"] == []
    assert cell("deps", ["fresh"])["reason"] is None
    assert cell("deps", [])["notes"] == []


@pytest.mark.django_db
def test_overview_cells_carry_notes(auth_client: APIClient, inventory: None) -> None:
    body = auth_client.get("/api/v1/overview/").json()
    cells = [c for s in body["sites"] for c in s["cells"].values()]
    cells.append(body["box"]["box"])
    assert cells and all(c["notes"] == [] for c in cells)


# ---------------------------------------------------------------- shared updates

NPM_A = "Foxugly/a:package-lock.json"
NPM_B = "Foxugly/b:package-lock.json"


def npm_sites():
    return [site("a", (NPM_A,)), site("b", (NPM_B,))]


def test_auto_blocked_group_is_waiting() -> None:
    deps = {NPM_A: npm(ts()), NPM_B: npm(ts())}
    g = group_updates(npm_sites(), deps, {})[0]
    assert g["package"] == "typescript"
    assert g["progress"] == "waiting"
    assert g["blocked"] == {"issues": [], "by": [{**CLI, "newer": None}]}
    assert "_auto_by" not in g


def test_auto_blocked_group_retry_when_a_blocker_unblocks() -> None:
    deps = {NPM_A: npm(ts()), NPM_B: npm(ts(newer="23.0.0"))}
    g = group_updates(npm_sites(), deps, {})[0]
    assert g["progress"] == "retry"
    assert g["blocked"]["by"] == [{**CLI, "newer": "23.0.0"}]


def test_auto_blocked_merges_distinct_blockers() -> None:
    other = {"package": "@angular/build", "version": "22.3.0", "range": ">=6.0 <6.1"}
    item_b = ts()
    item_b["blocked_by"].append({**other, "newer": None})
    deps = {NPM_A: npm(ts()), NPM_B: npm(item_b)}
    g = group_updates(npm_sites(), deps, {})[0]
    assert [b["package"] for b in g["blocked"]["by"]] == [
        "@angular/compiler-cli",
        "@angular/build",
    ]


def test_unblocked_group_has_no_blocked() -> None:
    deps = {NPM_A: npm(plain()), NPM_B: npm(plain())}
    g = group_updates(npm_sites(), deps, {})[0]
    assert g["blocked"] is None and g["progress"] == "none"


def test_open_pr_wins_over_auto_block() -> None:
    deps = {NPM_A: npm(ts()), NPM_B: npm(ts())}
    prs = {
        "Foxugly/a": [
            {
                "number": 3,
                "url": "https://x/pull/3",
                "branch": "deps/typescript-7.0.2",
                "checks": "pending",
            }
        ]
    }
    g = group_updates(npm_sites(), deps, {}, prs)[0]
    assert g["progress"] == "in_progress"
    assert g["blocked"]["by"][0]["package"] == "@angular/compiler-cli"


def test_issue_block_wins_over_auto_block() -> None:
    sites = [site("a", (A_LOCK,)), site("b", (B_LOCK,))]
    item = {
        "name": "django",
        "current": "6.0",
        "latest": "6.1.2",
        "kind": "minor",
        "blocked_by": [
            {"package": "celery", "version": "5.6.3", "range": "<6.1", "newer": None}
        ],
    }
    deps = {
        A_LOCK: {"ecosystem": "python", "outdated": [item]},
        B_LOCK: {"ecosystem": "python", "outdated": [item]},
    }
    issues = {"Foxugly/a": [fleet_issue(8, BODY)]}
    g = group_updates(sites, deps, {}, {}, issues)[0]
    assert g["progress"] == "waiting"
    assert [i["number"] for i in g["blocked"]["issues"]] == [8]
    assert [b["package"] for b in g["blocked"]["by"]] == ["djangorestframework"]


TWO_NPM_SITES = """
sites:
""" + "".join(
    f"""  - slug: {s}
    repos: [Foxugly/{s}]
    npm_locks: [Foxugly/{s}:package-lock.json]
    deployed: true
    actions_allowed: false
"""
    for s in ("a", "b")
)


@pytest.mark.django_db
def test_updates_endpoint_serialises_auto_block(auth_client: APIClient) -> None:
    make_apps(TWO_NPM_SITES)
    snap("deps", NPM_A, npm(ts()))
    snap("deps", NPM_B, npm(ts()))
    body = auth_client.get("/api/v1/updates/").json()
    g = next(g for g in body["groups"] if g["package"] == "typescript")
    assert g["progress"] == "waiting"
    assert g["blocked"] == {"issues": [], "by": [{**CLI, "newer": None}]}
