from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from django.db import IntegrityError
from django.utils import timezone
from rest_framework.exceptions import ValidationError
from rest_framework.test import APIClient

from apps.inventory.loader import Site, get_site, load_inventory
from apps.inventory.models import App
from apps.inventory.validation import clean_app
from tests.helpers import make_apps

SNAPSHOT = Path(__file__).parent / "data" / "fleet_import_snapshot.yaml"


def _site(slug="a", repo="Foxugly/A", vhost="a.foxugly.com"):
    return (
        f"  - slug: {slug}\n    repos: [{repo}]\n    vhosts: [{vhost}]\n"
        "    health_path: /health/\n    sentry_projects: []\n"
        "    python_locks: []\n    npm_locks: []\n"
        "    deployed: true\n    actions_allowed: true\n"
    )


def _expected_from_yaml() -> list[Site]:
    raw = yaml.safe_load(SNAPSHOT.read_text(encoding="utf-8"))["sites"]
    sites = [
        Site(
            slug=r["slug"],
            repos=tuple(r["repos"]),
            vhosts=tuple(r["vhosts"]),
            health_path=r["health_path"],
            sentry_projects=tuple(r["sentry_projects"]),
            python_locks=tuple(r["python_locks"]),
            npm_locks=tuple(r["npm_locks"]),
            deployed=r["deployed"],
            actions_allowed=r["actions_allowed"],
        )
        for r in raw
    ]
    return sorted(sites, key=lambda s: s.slug)


@pytest.mark.django_db
def test_migration_imports_yaml_exactly():
    # Review Focus 1: the rows created by migration 0002 equal the old YAML, field
    # for field (the snapshot is the YAML text as it was when imported).
    expected = _expected_from_yaml()
    assert len(expected) == 17
    assert load_inventory() == expected
    assert get_site("fleet").actions_allowed is False
    assert get_site("foxugly-ops").actions_allowed is False
    quiz = get_site("quizonline")
    assert quiz.sentry_projects == ("quizonline-backend", "quizonline-frontend")
    assert quiz.npm_locks == (
        "Foxugly/QuizOnline:quizonline-frontend/package-lock.json",
    )


@pytest.mark.django_db
def test_make_apps_replaces_imported_rows():
    make_apps("sites:\n" + _site())
    assert [s.slug for s in load_inventory()] == ["a"]


@pytest.mark.django_db
def test_load_inventory_ordered_by_slug():
    make_apps("sites:\n" + _site("b", "Foxugly/B") + _site("a", "Foxugly/A"))
    assert [s.slug for s in load_inventory()] == ["a", "b"]


@pytest.mark.django_db
def test_duplicate_slug_is_refused_by_the_database():
    with pytest.raises(IntegrityError):
        make_apps("sites:\n" + _site() + _site(repo="Foxugly/B"))


@pytest.mark.django_db
def test_get_site_unknown_is_none():
    assert get_site("nope") is None


@pytest.mark.django_db
def test_app_as_dict_and_to_site_round_trip():
    app = make_apps("sites:\n" + _site())[0]
    assert app.as_dict() == {
        "slug": "a",
        "repos": ["Foxugly/A"],
        "vhosts": ["a.foxugly.com"],
        "health_path": "/health/",
        "sentry_projects": [],
        "python_locks": [],
        "npm_locks": [],
        "deployed": True,
        "actions_allowed": True,
    }
    assert app.to_site().repos == ("Foxugly/A",)


@pytest.mark.django_db
def test_archived_app_is_excluded(auth_client: APIClient, inventory: None) -> None:
    # Review Focus 2: archived apps leave the loader and the overview, but the
    # site page still answers, marked archived.
    App.objects.filter(slug="beta").update(archived_at=timezone.now())
    assert [s.slug for s in load_inventory()] == ["alpha"]
    assert get_site("beta") is None
    slugs = [s["slug"] for s in auth_client.get("/api/v1/overview/").json()["sites"]]
    assert slugs == ["alpha"]
    assert auth_client.get("/api/v1/updates/").status_code == 200
    resp = auth_client.get("/api/v1/sites/beta/")
    assert resp.status_code == 200
    assert resp.json()["archived"] is True
    assert auth_client.get("/api/v1/sites/alpha/").json()["archived"] is False
    assert auth_client.get("/api/v1/sites/nope/").status_code == 404


@pytest.mark.django_db
def test_collection_skips_archived_app(settings: Any, monkeypatch) -> None:
    from apps.collect import tasks

    settings.FLEET_GITHUB_READ_TOKEN = "tok"
    make_apps("sites:\n" + _site("a", "Foxugly/A") + _site("b", "Foxugly/B"))
    App.objects.filter(slug="b").update(archived_at=timezone.now() - timedelta(1))
    seen: list[str] = []
    monkeypatch.setattr(
        tasks.github, "collect_repo", lambda repo, *, token: seen.append(repo) or {}
    )
    tasks.collect_github()
    assert seen == ["Foxugly/A"]


def _valid(**over: Any) -> dict[str, Any]:
    base = {
        "slug": "a",
        "repos": ["Foxugly/A"],
        "vhosts": ["a.foxugly.com"],
        "health_path": "/health/",
        "sentry_projects": [],
        "python_locks": [],
        "npm_locks": [],
        "deployed": True,
        "actions_allowed": False,
    }
    return {**base, **over}


def test_clean_app_accepts_and_defaults():
    out = clean_app(_valid())
    assert out == _valid()
    minimal = clean_app({"slug": "x", "deployed": False, "actions_allowed": False})
    assert minimal["health_path"] == "/health/"
    assert minimal["repos"] == [] and minimal["npm_locks"] == []


@pytest.mark.parametrize(
    ("over", "field"),
    [
        ({"slug": "Bad Slug"}, "slug"),
        ({"slug": 3}, "slug"),
        ({"repos": ["Other/A"]}, "repos"),
        ({"vhosts": ["https://a.foxugly.com"]}, "vhosts"),
        ({"python_locks": ["Foxugly/B:requirements.txt"]}, "python_locks"),
        ({"python_locks": ["Foxugly/A:"]}, "python_locks"),
        ({"npm_locks": ["nocolon"]}, "npm_locks"),
        ({"health_path": "health"}, "health_path"),
        ({"deployed": "yes"}, "deployed"),
        ({"actions_allowed": None}, "actions_allowed"),
        ({"repos": "Foxugly/A"}, "repos"),
        ({"sentry_projects": [1]}, "sentry_projects"),
    ],
)
def test_clean_app_rules(over, field):
    with pytest.raises(ValidationError) as exc:
        clean_app(_valid(**over))
    assert field in exc.value.detail
