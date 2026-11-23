"""Updates the project holds on purpose (requirements.in upper bound)."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from apps.api.updates import group_updates
from tests.helpers import make_apps
from tests.test_api import cell, snap
from tests.test_updates import A_LOCK, B_LOCK, site

NOTE = "hold: sentry-sdk 3.0 day-0 major, revisit 2026-12-07"
HOLD = {
    "kind": "hold",
    "package": "requirements.in",
    "version": "",
    "range": "<3",
    "note": NOTE,
    "newer": None,
}
CELERY = {
    "kind": "package",
    "package": "celery",
    "version": "5.6.3",
    "range": "<6.1",
    "newer": None,
}


def sentry(*blockers: dict) -> dict:
    item = {
        "name": "sentry-sdk",
        "current": "2.76.0",
        "latest": "3.0.0",
        "kind": "major",
    }
    if blockers:
        item["blocked_by"] = list(blockers)
    return item


def django(*blockers: dict) -> dict:
    item = {"name": "django", "current": "6.0", "latest": "6.1.2", "kind": "minor"}
    if blockers:
        item["blocked_by"] = list(blockers)
    return item


def py(*items: dict) -> dict:
    return {"ecosystem": "python", "checked": 10, "outdated": list(items)}


HELD_NOTE = {
    "code": "held_updates",
    "params": {
        "n": 1,
        "package": "sentry-sdk",
        "latest": "3.0.0",
        "file": "requirements.in",
        "range": "<3",
        "note": NOTE,
    },
}


@pytest.mark.django_db
def test_only_held_items_is_green_with_a_held_reason() -> None:
    snap("deps", "l", py(sentry(HOLD)))
    c = cell("deps", ["l"])
    assert c["state"] == "green" and c["count"] == 0
    assert c["reason"] == HELD_NOTE
    assert c["notes"] == [HELD_NOTE]


@pytest.mark.django_db
def test_held_and_blocked_are_two_notes() -> None:
    # A hold wins over package blockers of the same item.
    snap("deps", "l", py(sentry(HOLD, CELERY), django(CELERY)))
    c = cell("deps", ["l"])
    assert c["state"] == "green"
    assert [n["code"] for n in c["notes"]] == ["blocked_updates", "held_updates"]
    assert c["notes"][0]["params"]["n"] == 1
    assert c["notes"][0]["params"]["package"] == "django"
    assert c["notes"][1] == HELD_NOTE


@pytest.mark.django_db
def test_held_plus_plain_outdated_is_orange() -> None:
    plain = {"name": "rich", "current": "1.0", "latest": "1.1", "kind": "minor"}
    snap("deps", "l", py(sentry(HOLD), plain))
    c = cell("deps", ["l"])
    assert c["state"] == "orange" and c["count"] == 1
    assert c["reason"] == {"code": "outdated", "params": {"n": 1}}
    assert c["notes"] == [HELD_NOTE]


@pytest.mark.django_db
def test_held_notes_sum_over_subjects() -> None:
    snap("deps", "l1", py(sentry(HOLD)))
    snap("deps", "l2", py(sentry(HOLD)))
    c = cell("deps", ["l1", "l2"])
    assert c["notes"][0]["code"] == "held_updates"
    assert c["notes"][0]["params"]["n"] == 2


# ---------------------------------------------------------------- shared updates


def two_sites():
    return [site("a", (A_LOCK,)), site("b", (B_LOCK,))]


def test_group_held_everywhere_is_waiting_on_the_hold() -> None:
    other = {**HOLD, "note": ""}
    deps = {A_LOCK: py(sentry(other)), B_LOCK: py(sentry(HOLD))}
    g = group_updates(two_sites(), deps, {})[0]
    assert g["progress"] == "waiting"
    assert g["blocked"] == {
        "issues": [],
        "by": [
            {
                "kind": "hold",
                "package": "requirements.in",
                "version": "",
                "range": "<3",
                "note": NOTE,  # first non-empty note of the members
                "newer": None,
            }
        ],
    }


def test_group_held_on_one_site_only_is_not_blocked() -> None:
    deps = {A_LOCK: py(sentry(HOLD)), B_LOCK: py(sentry())}
    g = group_updates(two_sites(), deps, {})[0]
    assert g["progress"] == "none" and g["blocked"] is None


def test_group_partly_held_keeps_package_blockers() -> None:
    deps = {A_LOCK: py(sentry(HOLD)), B_LOCK: py(sentry(CELERY))}
    g = group_updates(two_sites(), deps, {})[0]
    assert g["progress"] == "waiting"
    assert [b["kind"] for b in g["blocked"]["by"]] == ["package"]


TWO_PY_SITES = """
sites:
""" + "".join(
    f"""  - slug: {s}
    repos: [Foxugly/{s}]
    python_locks: [Foxugly/{s}:requirements.txt]
    deployed: true
    actions_allowed: false
"""
    for s in ("a", "b")
)


@pytest.mark.django_db
def test_updates_endpoint_serialises_hold(auth_client: APIClient) -> None:
    make_apps(TWO_PY_SITES)
    snap("deps", A_LOCK, py(sentry(HOLD)))
    snap("deps", B_LOCK, py(sentry(HOLD)))
    body = auth_client.get("/api/v1/updates/").json()
    g = next(g for g in body["groups"] if g["package"] == "sentry-sdk")
    assert g["progress"] == "waiting"
    assert g["blocked"]["by"] == [HOLD]
