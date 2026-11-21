from pathlib import Path

import pytest

from apps.inventory.loader import get_site, load_inventory

REPO_RE = r"^Foxugly/[A-Za-z0-9_.-]+$"


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "fleet.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def _site(slug="a", repo="Foxugly/A", vhost="a.foxugly.com"):
    return (
        f"  - slug: {slug}\n    repos: [{repo}]\n    vhosts: [{vhost}]\n"
        "    health_path: /health/\n    sentry_projects: []\n"
        "    python_locks: []\n    npm_locks: []\n"
        "    deployed: true\n    actions_allowed: true\n"
    )


def test_loads_real_inventory():
    import re

    sites = load_inventory()
    assert len(sites) >= 13
    slugs = [s.slug for s in sites]
    assert len(slugs) == len(set(slugs))
    for s in sites:
        for repo in s.repos:
            assert re.match(REPO_RE, repo), repo


def test_rejects_duplicate_slug(tmp_path):
    p = _write(tmp_path, "sites:\n" + _site() + _site(repo="Foxugly/B"))
    with pytest.raises(ValueError, match="a"):
        load_inventory(p)


def test_rejects_bad_repo(tmp_path):
    p = _write(tmp_path, "sites:\n" + _site(repo="Other/A"))
    with pytest.raises(ValueError, match="a"):
        load_inventory(p)


def test_rejects_vhost_with_scheme(tmp_path):
    p = _write(tmp_path, "sites:\n" + _site(vhost="https://a.foxugly.com"))
    with pytest.raises(ValueError, match="a"):
        load_inventory(p)


def test_fleet_itself_has_no_actions():
    assert get_site("fleet").actions_allowed is False


def test_foxugly_ops_has_no_actions():
    assert get_site("foxugly-ops").actions_allowed is False


def test_get_site_unknown():
    with pytest.raises(KeyError):
        get_site("nope")
