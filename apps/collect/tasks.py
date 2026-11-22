"""Scheduled collection tasks: one snapshot per subject, failures isolated."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings

from apps.inventory.loader import load_inventory

from . import store
from .intervals import INTERVALS
from .sources import box, deps, github, health, sentry
from .sources.http import SourceError

__all__ = ["INTERVALS"]

logger = logging.getLogger(__name__)


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _run(source: str, subjects: list[str], fn: Callable[[str], dict]) -> None:
    for subject in subjects:
        try:
            data = fn(subject)
        except SourceError as exc:
            store.record(source, subject, error=str(exc))
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - isolate every subject
            logger.exception("collect %s failed for %s", source, subject)
            store.record(source, subject, error=f"{type(exc).__name__}: {exc}"[:200])
        else:
            store.record(source, subject, data=data)


def _run_with_token(
    source: str,
    subjects: list[str],
    token: str,
    token_name: str,
    fn: Callable[[str], dict],
) -> None:
    if not token:
        for subject in subjects:
            store.record(source, subject, error=f"{token_name} is not set")
        return
    _run(source, subjects, fn)


@shared_task(name="collect_health", ignore_result=True)
def collect_health() -> None:
    probes: dict[str, str] = {}
    for site in load_inventory():
        for i, vhost in enumerate(site.vhosts):
            probes.setdefault(vhost, site.health_path if i == 0 else "/")
    _run("health", list(probes), lambda vhost: health.probe(vhost, probes[vhost]))


@shared_task(name="collect_github", ignore_result=True)
def collect_github() -> None:
    repos = _unique(r for s in load_inventory() for r in s.repos)
    token = settings.FLEET_GITHUB_READ_TOKEN
    _run_with_token(
        "github",
        repos,
        token,
        "FLEET_GITHUB_READ_TOKEN",
        lambda repo: github.collect_repo(repo, token=token),
    )


@shared_task(name="collect_sentry", ignore_result=True)
def collect_sentry() -> None:
    projects = _unique(p for s in load_inventory() for p in s.sentry_projects)
    token = settings.FLEET_SENTRY_TOKEN
    _run_with_token(
        "sentry",
        projects,
        token,
        "FLEET_SENTRY_TOKEN",
        lambda project: sentry.collect_project(
            project,
            token=token,
            org=settings.FLEET_SENTRY_ORG,
            api=settings.FLEET_SENTRY_API,
        ),
    )


@shared_task(name="collect_box_task", ignore_result=True)
def collect_box_task() -> None:
    _run("box", ["box"], lambda _subject: box.collect_box())


@shared_task(name="collect_deps", ignore_result=True)
def collect_deps() -> None:
    refs = _unique(
        ref for s in load_inventory() for ref in (*s.python_locks, *s.npm_locks)
    )
    token = settings.FLEET_GITHUB_READ_TOKEN
    _run_with_token(
        "deps",
        refs,
        token,
        "FLEET_GITHUB_READ_TOKEN",
        lambda ref: deps.collect_lock(ref, token=token),
    )


@shared_task(name="purge_snapshots", ignore_result=True)
def purge_snapshots() -> None:
    store.purge_older_than(30)
