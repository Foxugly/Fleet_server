"""DEV only: seed fake snapshots shaped like the collectors output."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.api.views import SITE_SOURCES, subjects_for
from apps.collect import store
from apps.collect.intervals import INTERVALS
from apps.collect.models import Snapshot
from apps.inventory.loader import Site, load_inventory

# Deliberate cases, by inventory slug.
RED_SITE = "quizonline"  # github: high Dependabot alert + failed main run
STALE_SITE = "ical"  # health: collected > 3x its interval ago
ERROR_SITE = "pushit"  # sentry: first project fails after a good snapshot


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _health(index: int) -> dict[str, Any]:
    return {
        "status": 200,
        "latency_ms": 40 + 17 * (index % 5),
        "error": "",
        "tls_days_left": 30 + 9 * (index % 6),
    }


def _github(site: Site, repo: str, now: datetime, index: int, first: bool) -> dict:
    url = f"https://github.com/{repo}"

    def ago(days: int) -> str:
        return _iso(now - timedelta(days=days))

    data: dict[str, Any] = {
        "prs": [],
        "issues": [],
        "dependabot_alerts": [],
        "code_scanning": [],
        "secret_scanning": [],
        "main_run": {
            "sha": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
            "conclusion": "success",
            "url": f"{url}/actions/runs/1001",
            "workflow": "CI",
            "created_at": ago(1),
        },
    }
    if index % 3 == 1:  # a quiet repo stays green
        return data
    data["prs"].append(
        {
            "number": 12,
            "title": "Bump requests from 2.32.3 to 2.32.4",
            "url": f"{url}/pull/12",
            "author": "dependabot[bot]",
            "is_dependabot": True,
            "draft": False,
            "checks": "success",
            "merge_state": "clean",
            "created_at": ago(2),
        }
    )
    if index % 3 == 2:
        data["issues"].append(
            {
                "number": 7,
                "title": "Example: slow dashboard on mobile",
                "url": f"{url}/issues/7",
                "created_at": ago(9),
            }
        )
    if first and site.slug == "pushit":
        data["prs"].append(
            {
                "number": 3,
                "title": "WIP: rework notification batching",
                "url": f"{url}/pull/3",
                "author": "foxugly",
                "is_dependabot": False,
                "draft": True,
                "checks": "pending",
                "merge_state": "unstable",
                "created_at": ago(4),
            }
        )
    if first and site.slug == RED_SITE:
        data["dependabot_alerts"].append(
            {
                "package": "django",
                "severity": "high",
                "url": f"{url}/security/dependabot/1",
                "created_at": ago(3),
            }
        )
        data["code_scanning"].append(
            {
                "rule": "py/path-injection",
                "severity": "medium",
                "path": "app/views/export.py",
                "url": f"{url}/security/code-scanning/1",
            }
        )
        data["main_run"].update(
            conclusion="failure", url=f"{url}/actions/runs/1002", created_at=ago(0)
        )
    return data


def _sentry(now: datetime, index: int) -> dict[str, Any]:
    issues = [
        {
            "title": "ValueError: invalid literal for int() with base 10",
            "culprit": "app.views.detail",
            "count": 14 + index,
            "last_seen": _iso(now - timedelta(minutes=12)),
            "first_seen": _iso(now - timedelta(hours=3)),
            "url": "https://foxugly-srl.sentry.io/issues/1001/",
            "level": "error",
        },
        {
            "title": "TimeoutError: upstream did not answer",
            "culprit": "app.clients.fetch",
            "count": 3,
            "last_seen": _iso(now - timedelta(hours=5)),
            "first_seen": _iso(now - timedelta(days=6)),
            "url": "https://foxugly-srl.sentry.io/issues/1002/",
            "level": "warning",
        },
    ]
    if index % 2:
        issues = issues[1:]
    return {
        "unresolved": len(issues),
        "new_24h": sum(1 for i in issues if i["level"] == "error"),
        "issues": issues,
    }


def _deps(subject: str, index: int) -> dict[str, Any]:
    npm = subject.endswith("package-lock.json")
    outdated = []
    if index % 3 != 1:
        outdated = [
            {
                "name": "typescript" if npm else "django",
                "current": "5.8.3" if npm else "5.2.1",
                "latest": "6.0.1" if npm else "6.0.2",
                "kind": "major",
            },
            {
                "name": "rxjs" if npm else "requests",
                "current": "7.8.1" if npm else "2.32.3",
                "latest": "7.8.2" if npm else "2.32.4",
                "kind": "patch",
            },
        ]
    return {
        "ecosystem": "npm" if npm else "python",
        "outdated": outdated,
        "checked": 48 + index,
    }


def _box() -> dict[str, Any]:
    return {
        "apt": [
            {
                "package": "openssl",
                "version": "3.0.13-0ubuntu3.7",
                "origin": "noble-updates,noble-security",
                "security": True,
                "from_version": "3.0.13-0ubuntu3.6",
            },
            {
                "package": "curl",
                "version": "8.5.0-2ubuntu10.7",
                "origin": "noble-updates",
                "security": False,
                "from_version": "8.5.0-2ubuntu10.6",
            },
        ],
        "reboot_required": True,
        "reboot_pkgs": ["linux-image-6.8.0-1040-aws", "libssl3"],
        "failed_units": ["fleet-db-backup.service"],
        "disk": {"mount": "/", "used_pct": 72},
        "memory": {"total_mb": 7938, "available_mb": 4210},
    }


class Command(BaseCommand):
    help = "DEV only: seed fake snapshots for every inventory subject."

    def handle(self, *args: Any, **options: Any) -> None:
        if settings.STATE != "DEV":
            raise CommandError("seed_demo only runs with STATE=DEV")
        now = timezone.now()
        written = 0

        def put(source: str, subject: str, data: dict, age: timedelta) -> None:
            nonlocal written
            snap = store.record(source, subject, data=data)
            Snapshot.objects.filter(pk=snap.pk).update(collected_at=now - age)
            written += 1

        fresh = timedelta(minutes=1)
        sites = load_inventory()
        pairs = [("box", "box")]
        for site in sites:
            for source in SITE_SOURCES:
                pairs += [(source, s) for s in subjects_for(site, source)]
        for source, subject in pairs:
            Snapshot.objects.filter(source=source, subject=subject).delete()

        put("box", "box", _box(), fresh)
        for i, site in enumerate(sites):
            for j, vhost in enumerate(subjects_for(site, "health")):
                age = (
                    timedelta(seconds=4 * INTERVALS["health"])
                    if site.slug == STALE_SITE
                    else fresh
                )
                put("health", vhost, _health(i + j), age)
            for j, repo in enumerate(subjects_for(site, "github")):
                put("github", repo, _github(site, repo, now, i + j, j == 0), fresh)
            for j, project in enumerate(subjects_for(site, "sentry")):
                if site.slug == ERROR_SITE and j == 0:
                    # Good snapshot first, then a later failure: the good one
                    # stays what the API serves as data.
                    put("sentry", project, _sentry(now, i + j), timedelta(minutes=30))
                    store.record(
                        "sentry",
                        project,
                        error="sentry authentication failed (HTTP 403)",
                    )
                    written += 1
                else:
                    put("sentry", project, _sentry(now, i + j), fresh)
            for j, lock in enumerate(subjects_for(site, "deps")):
                put("deps", lock, _deps(lock, i + j), fresh)
        self.stdout.write(f"Seeded {written} snapshots for {len(sites)} sites.")
