"""Shared HTTP helpers for source collectors. Never put tokens in error messages."""

from datetime import UTC, datetime
from urllib.parse import urlsplit

import requests

TIMEOUT = 20


class SourceError(Exception):
    """A data source could not be read (network, HTTP status, rate limit)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _path(url: str) -> str:
    return urlsplit(url).path


def rate_limit_reset_iso(resp) -> str | None:
    reset = resp.headers.get("X-RateLimit-Reset")
    try:
        return datetime.fromtimestamp(int(reset), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None


def get_response(session, url, *, params=None, allow_404=False):
    """GET once; return the Response (or None on an allowed 404). Raises SourceError."""
    try:
        resp = session.get(url, params=params, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise SourceError(f"GET {_path(url)} failed: {type(exc).__name__}") from exc
    if (
        resp.status_code in (403, 429)
        and resp.headers.get("X-RateLimit-Remaining") == "0"
    ):
        reset = rate_limit_reset_iso(resp)
        raise SourceError(
            f"GET {_path(url)} rate limited (HTTP {resp.status_code});"
            f" resets at {reset}"
        )
    if resp.status_code == 404 and allow_404:
        return None
    if not 200 <= resp.status_code < 300:
        raise SourceError(
            f"GET {_path(url)} returned HTTP {resp.status_code}",
            status=resp.status_code,
        )
    return resp


def get_json(session, url, *, params=None, allow_404=False):
    resp = get_response(session, url, params=params, allow_404=allow_404)
    if resp is None:
        return None
    try:
        return resp.json()
    except ValueError as exc:
        raise SourceError(f"GET {_path(url)} returned invalid JSON") from exc


def get_json_paginated(
    session, url, *, params=None, key=None, allow_404=False, max_items=None
):
    """Follow Link rel="next". With `key`, items come from body[key] (wrapped lists).

    Stops on Sentry's `results="false"` next link, and once `max_items` are collected.
    """
    items: list[dict] = []
    next_url, next_params = url, params
    while next_url:
        resp = get_response(session, next_url, params=next_params, allow_404=allow_404)
        if resp is None:
            return []
        try:
            body = resp.json()
        except ValueError as exc:
            raise SourceError(f"GET {_path(next_url)} returned invalid JSON") from exc
        items.extend(body[key] if key else body)
        if max_items is not None and len(items) >= max_items:
            break
        nxt = resp.links.get("next", {})
        next_url = None if nxt.get("results") == "false" else nxt.get("url")
        next_params = None  # the next URL already carries its query string
    return items
