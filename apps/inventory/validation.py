"""Validation of an app's editable fields (the rules of the former YAML loader)."""

from __future__ import annotations

import re
from typing import Any

from rest_framework.exceptions import ValidationError

REPO_RE = re.compile(r"^Foxugly/[A-Za-z0-9_.-]+\Z")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\Z")
VHOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?\Z")
LOCK_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*\Z")
HEALTH_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]*\Z")
SENTRY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}\Z")


def _bad_lock_path(path: str) -> bool:
    # These values end up in outgoing GitHub URLs: no traversal, no empty segment.
    return not LOCK_PATH_RE.match(path) or any(
        part in ("", ".", "..") for part in path.split("/")
    )


# Lengths match the model columns; the caps keep a runaway payload a 400, never a 500.
SLUG_MAX = 64
HEALTH_PATH_MAX = 200
STRING_MAX = 200
LIST_MAX = 50

LIST_FIELDS = ("repos", "vhosts", "sentry_projects", "python_locks", "npm_locks")
BOOL_FIELDS = ("deployed", "actions_allowed")
EDITABLE_FIELDS = (
    "slug",
    "repos",
    "vhosts",
    "health_path",
    "sentry_projects",
    "python_locks",
    "npm_locks",
    "deployed",
    "actions_allowed",
)


def _strs(data: dict, key: str, errors: dict[str, list[str]]) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        errors.setdefault(key, []).append("Must be a list of strings.")
        return []
    if len(value) > LIST_MAX:
        errors.setdefault(key, []).append(f"At most {LIST_MAX} entries.")
        return []
    if any(len(v) > STRING_MAX for v in value):
        errors.setdefault(key, []).append(
            f"Each entry at most {STRING_MAX} characters."
        )
        return []
    return list(value)


def clean_app(data: dict[str, Any]) -> dict[str, Any]:
    """Normalised editable fields, or raise ValidationError({field: [msg]})."""
    errors: dict[str, list[str]] = {}
    slug = data.get("slug")
    if not isinstance(slug, str) or not SLUG_RE.match(slug) or len(slug) > SLUG_MAX:
        errors["slug"] = [f"Invalid slug (a-z, 0-9, -; at most {SLUG_MAX} characters)."]
    lists = {key: _strs(data, key, errors) for key in LIST_FIELDS}
    for repo in lists["repos"]:
        if not REPO_RE.match(repo):
            errors.setdefault("repos", []).append(f"Invalid repo: {repo!r}.")
    for vhost in lists["vhosts"]:
        if not VHOST_RE.match(vhost):
            errors.setdefault("vhosts", []).append(f"Invalid vhost: {vhost!r}.")
    for project in lists["sentry_projects"]:
        if not SENTRY_RE.match(project):
            errors.setdefault("sentry_projects", []).append(
                f"Invalid Sentry project: {project!r}."
            )
    for key in ("python_locks", "npm_locks"):
        for lock in lists[key]:
            repo, sep, path = lock.partition(":")
            if not sep or repo not in lists["repos"] or _bad_lock_path(path):
                errors.setdefault(key, []).append(f"Invalid lock: {lock!r}.")
    health_path = data.get("health_path", "/health/")
    if (
        not isinstance(health_path, str)
        or not HEALTH_PATH_RE.match(health_path)
        or any(part in (".", "..") for part in health_path.split("/"))
        or len(health_path) > HEALTH_PATH_MAX
    ):
        errors["health_path"] = [
            "Must start with /, use only letters, digits and . _ ~ / - "
            f"(no . or .. segment), at most {HEALTH_PATH_MAX} characters."
        ]
    flags: dict[str, bool] = {}
    for key in BOOL_FIELDS:
        value = data.get(key)
        if not isinstance(value, bool):
            errors[key] = ["Must be a boolean."]
        else:
            flags[key] = value
    if errors:
        raise ValidationError(errors)
    return {"slug": slug, "health_path": health_path, **lists, **flags}
