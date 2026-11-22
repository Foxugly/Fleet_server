"""Sentry collector: pure function, token passed in, returns a plain dict."""

from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import requests

from .http import SourceError, get_json_paginated

MAX_ISSUES = 1000
TOP = 25


def _session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({"Authorization": f"Bearer {token}"})
    return s


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _to_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def collect_project(
    project: str, *, token: str, org: str, api: str, now: datetime | None = None
) -> dict:
    now = now or datetime.now(UTC)
    url = f"{api.rstrip('/')}/projects/{org}/{quote(project, safe='')}/issues/"
    try:
        with _session(token) as session:
            raw = get_json_paginated(
                session,
                url,
                params={"query": "is:unresolved", "statsPeriod": "24h"},
                max_items=MAX_ISSUES,
            )
    except SourceError as exc:
        if exc.status == 404:
            raise SourceError(f"sentry project {project} not found") from exc
        if exc.status in (401, 403):
            raise SourceError(
                f"sentry authentication failed (HTTP {exc.status})"
            ) from exc
        raise
    raw = raw[:MAX_ISSUES]
    cutoff = now - timedelta(hours=24)
    new_24h = sum(1 for i in raw if _parse(i["firstSeen"]) >= cutoff)
    top = sorted(raw, key=lambda i: _parse(i["lastSeen"]), reverse=True)[:TOP]
    return {
        "unresolved": len(raw),
        "new_24h": new_24h,
        "issues": [
            {
                "title": i.get("title"),
                "culprit": i.get("culprit"),
                "count": _to_int(i.get("count")),
                "last_seen": i.get("lastSeen"),
                "first_seen": i.get("firstSeen"),
                "url": i.get("permalink"),
                "level": i.get("level"),
            }
            for i in top
        ],
    }
