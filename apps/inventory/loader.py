import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from django.conf import settings

_REPO_RE = re.compile(r"^Foxugly/[A-Za-z0-9_.-]+$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_VHOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")


@dataclass(frozen=True)
class Site:
    slug: str
    repos: tuple[str, ...]
    vhosts: tuple[str, ...]
    health_path: str
    sentry_projects: tuple[str, ...]
    python_locks: tuple[str, ...]
    npm_locks: tuple[str, ...]
    deployed: bool
    actions_allowed: bool


def _strs(raw: dict, key: str, slug: str) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"site {slug!r}: {key} must be a list of strings")
    return tuple(value)


def _parse(raw: dict) -> Site:
    slug = raw.get("slug")
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    repos = _strs(raw, "repos", slug)
    for repo in repos:
        if not _REPO_RE.match(repo):
            raise ValueError(f"site {slug!r}: invalid repo {repo!r}")
    vhosts = _strs(raw, "vhosts", slug)
    for vhost in vhosts:
        if not _VHOST_RE.match(vhost):
            raise ValueError(f"site {slug!r}: invalid vhost {vhost!r}")
    locks = _strs(raw, "python_locks", slug) + _strs(raw, "npm_locks", slug)
    for lock in locks:
        repo, sep, path = lock.partition(":")
        if not sep or repo not in repos or not path:
            raise ValueError(f"site {slug!r}: invalid lock {lock!r}")
    health_path = raw.get("health_path", "/health/")
    if not isinstance(health_path, str) or not health_path.startswith("/"):
        raise ValueError(f"site {slug!r}: invalid health_path {health_path!r}")
    flags = {}
    for key in ("deployed", "actions_allowed"):
        flags[key] = raw.get(key)
        if not isinstance(flags[key], bool):
            raise ValueError(f"site {slug!r}: {key} must be a boolean")
    return Site(
        slug=slug,
        repos=repos,
        vhosts=vhosts,
        health_path=health_path,
        sentry_projects=_strs(raw, "sentry_projects", slug),
        python_locks=_strs(raw, "python_locks", slug),
        npm_locks=_strs(raw, "npm_locks", slug),
        **flags,
    )


def load_inventory(path: Path | None = None) -> list[Site]:
    path = Path(path or settings.FLEET_INVENTORY_PATH)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_sites = data.get("sites")
    if not isinstance(raw_sites, list):
        raise ValueError("inventory must contain a 'sites' list")
    sites: list[Site] = []
    seen: set[str] = set()
    for raw in raw_sites:
        site = _parse(raw)
        if site.slug in seen:
            raise ValueError(f"duplicate slug: {site.slug!r}")
        seen.add(site.slug)
        sites.append(site)
    return sites


def get_site(slug: str) -> Site:
    for site in load_inventory():
        if site.slug == slug:
            return site
    raise KeyError(slug)
