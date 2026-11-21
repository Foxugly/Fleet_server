"""Sentry initialisation (OPERATIONS.md §3.8): a no-op when no DSN is set."""

from __future__ import annotations

from typing import Any

import sentry_sdk


def init_options(
    *, dsn: str, environment: str, release: str | None, traces_sample_rate: float
) -> dict[str, Any]:
    """Keyword arguments for `sentry_sdk.init` (kept here to be testable)."""
    return {
        "dsn": dsn,
        "environment": environment,
        "release": release,
        "traces_sample_rate": traces_sample_rate,
        "send_default_pii": False,
        "include_local_variables": False,
        "max_request_body_size": "never",
    }


def init_sentry(
    *, dsn: str, environment: str, release: str | None, traces_sample_rate: float
) -> bool:
    """Start Sentry when a DSN is given; return whether it was started."""
    if not dsn:
        return False
    sentry_sdk.init(
        **init_options(
            dsn=dsn,
            environment=environment,
            release=release,
            traces_sample_rate=traces_sample_rate,
        )
    )
    return True
