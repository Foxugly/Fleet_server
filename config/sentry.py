"""Sentry initialisation (OPERATIONS.md §3.8): a no-op when no DSN is set.

`DATA_COLLECTION` (sentry-sdk 3.x) collects no more than 2.x did with
`send_default_pii=False`: no user, cookies, request/response bodies, Celery
task arguments, SQL parameters or GenAI/MCP content. Headers, query strings
and local variables go through the SDK's sensitive-key denylist, plus the
proxy/IP header terms the SDK itself used for `send_default_pii=False`.

sentry-sdk 3.0 removed the built-in `EventScrubber`: `config.sentry_scrub`
re-applies its denylist from `before_send` (events) and `before_send_span`
(span names and attributes).
"""

from __future__ import annotations

import logging
from typing import Any

import sentry_sdk

from config.sentry_scrub import scrub_event, scrub_span

# Proxy/IP header terms the SDK itself used when it mapped send_default_pii=False.
_PII_TERMS = ["forwarded", "-ip", "remote-", "via", "-user"]

DATA_COLLECTION: dict[str, Any] = {
    "user_info": False,
    "cookies": {"mode": "off"},
    # `response` is not read by sentry-sdk 3.0.0 (it only collects request
    # headers); pinned off so a later release cannot start collecting them.
    "http_headers": {
        "request": {"mode": "denylist", "terms": _PII_TERMS},
        "response": {"mode": "off"},
    },
    "http_bodies": [],
    "url_query_params": {"mode": "denylist", "terms": _PII_TERMS},
    "graphql": {"document": False, "variables": False},
    "gen_ai": {"inputs": False, "outputs": False},
    "mcp": {"inputs": False, "outputs": False},
    "database_query_data": False,
    "queues": False,
    # 2.x sent no local variables at all here: keep them off.
    "stack_frame_variables": False,
    "frame_context_lines": 5,
}


def init_options(
    *, dsn: str, environment: str, release: str | None, traces_sample_rate: float
) -> dict[str, Any]:
    """Keyword arguments for `sentry_sdk.init` (kept here to be testable)."""
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.logging import LoggingIntegration

    return {
        "dsn": dsn,
        "environment": environment,
        "release": release,
        "traces_sample_rate": traces_sample_rate,
        # Celery stays auto-enabled, as in 2.x.
        "integrations": [
            # 3.0 turned cache spans on by default; keep 2.x's span volume.
            DjangoIntegration(cache_spans=False),
            # 3.0 no longer auto-enables logging and defaults `event_level`
            # to None. Same as the 2.x auto-enabled integration: INFO+ logs
            # become breadcrumbs, ERROR+ become events, and no Sentry Logs
            # (`level=None`; its INFO default would ship every log record).
            LoggingIntegration(
                level=None, breadcrumb_level=logging.INFO, event_level=logging.ERROR
            ),
        ],
        "data_collection": DATA_COLLECTION,
        "max_request_body_size": "never",
        "before_send": scrub_event,
        "before_send_span": scrub_span,
        # 3.0 default is True; 2.x did not attach stack traces to messages.
        "attach_stacktrace": False,
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
