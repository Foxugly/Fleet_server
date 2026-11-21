from __future__ import annotations

from typing import Any

from django.http import Http404
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import status as http
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle, UserRateThrottle
from rest_framework.views import APIView

from apps.collect import store, tasks
from apps.inventory.loader import Site, get_site, load_inventory

from . import status
from .serializers import (
    BoxSerializer,
    OverviewSerializer,
    QueuedSerializer,
    SiteDetailSerializer,
)

SITE_SOURCES = ("health", "github", "sentry", "deps")

REFRESH_TASKS = {
    "health": "collect_health",
    "github": "collect_github",
    "sentry": "collect_sentry",
    "box": "collect_box_task",
    "deps": "collect_deps",
}


def subjects_for(site: Site, source: str) -> list[str]:
    if source == "health":
        return list(site.vhosts)
    if source == "github":
        return list(site.repos)
    if source == "sentry":
        return list(site.sentry_projects)
    return [*site.python_locks, *site.npm_locks]


def _iso(snapshot: Any) -> str | None:
    return snapshot.collected_at.isoformat() if snapshot else None


def _subject_view(source: str, subject: str) -> dict[str, Any]:
    latest = store.latest(source, subject)
    good = store.latest_ok(source, subject)
    return {
        "data": good.data if good else None,
        "collected_at": _iso(good),
        "ok": latest.ok if latest else None,
        "error": latest.error if latest else "",
    }


class OverviewView(APIView):
    @extend_schema(responses=OverviewSerializer)
    def get(self, request: Request) -> Response:
        now = timezone.now()
        sites = []
        for site in load_inventory():
            cells = {
                s: status.cell(s, subjects_for(site, s), now) for s in SITE_SOURCES
            }
            sites.append({"slug": site.slug, "deployed": site.deployed, "cells": cells})
        sites.sort(key=lambda s: (status.worst_rank(s["cells"]), s["slug"]))
        return Response(
            {
                "generated_at": now.isoformat(),
                "box": {"box": status.cell("box", ["box"], now)},
                "sites": sites,
            }
        )


class SiteDetailView(APIView):
    @extend_schema(responses=SiteDetailSerializer)
    def get(self, request: Request, slug: str) -> Response:
        try:
            site = get_site(slug)
        except KeyError:
            raise Http404 from None
        now = timezone.now()
        sources: dict[str, Any] = {}
        for source in SITE_SOURCES:
            subjects = subjects_for(site, source)
            sources[source] = {
                "cell": status.cell(source, subjects, now),
                "items": {s: _subject_view(source, s) for s in subjects},
            }
        return Response(
            {
                "slug": site.slug,
                "repos": list(site.repos),
                "vhosts": list(site.vhosts),
                "deployed": site.deployed,
                "actions_allowed": site.actions_allowed,
                "sources": sources,
            }
        )


class BoxView(APIView):
    @extend_schema(responses=BoxSerializer)
    def get(self, request: Request) -> Response:
        return Response(
            {
                "cell": status.cell("box", ["box"], timezone.now()),
                **_subject_view("box", "box"),
            }
        )


class RefreshView(APIView):
    throttle_classes = [UserRateThrottle, ScopedRateThrottle]
    throttle_scope = "refresh"

    @extend_schema(request=None, responses={202: QueuedSerializer})
    def post(self, request: Request, source: str) -> Response:
        task_name = REFRESH_TASKS.get(source)
        if task_name is None:
            raise Http404
        getattr(tasks, task_name).delay()
        return Response({"queued": source}, status=http.HTTP_202_ACCEPTED)
