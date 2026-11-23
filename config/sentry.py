"""Sentry initialisation (OPERATIONS.md §3.8): a no-op when no DSN is set.

`DATA_COLLECTION` (sentry-sdk 2.76) replaces the deprecated
`send_default_pii=False` and collects no more than before: no user, cookies,
request bodies, local variables or SQL parameters. Passing it switches off the
SDK's built-in EventScrubber, so `scrub_event` re-applies it.
"""

from __future__ import annotations

from typing import Any, cast

import sentry_sdk
from sentry_sdk.scrubber import EventScrubber

# Proxy/IP header terms the SDK itself uses when it maps send_default_pii=False.
_PII_TERMS = ["forwarded", "-ip", "remote-", "via", "-user"]

DATA_COLLECTION: dict[str, Any] = {
    "user_info": False,
    "cookies": {"mode": "off"},
    "http_headers": {"request": {"mode": "denylist", "terms": _PII_TERMS}},
    "http_bodies": [],
    "url_query_params": {"mode": "denylist", "terms": _PII_TERMS},
    "graphql": {"document": False, "variables": False},
    "gen_ai": {"inputs": False, "outputs": False},
    "database_query_data": False,
    "queues": False,
    "stack_frame_variables": False,
    "frame_context_lines": 5,
}

_SCRUBBER = EventScrubber(send_default_pii=False)


def scrub_event(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any]:
    """`before_send` / `before_send_transaction`: the SDK's denylist scrubbing."""
    _SCRUBBER.scrub_event(cast(Any, event))  # an Event TypedDict to the SDK
    return event


def init_options(
    *, dsn: str, environment: str, release: str | None, traces_sample_rate: float
) -> dict[str, Any]:
    """Keyword arguments for `sentry_sdk.init` (kept here to be testable)."""
    return {
        "dsn": dsn,
        "environment": environment,
        "release": release,
        "traces_sample_rate": traces_sample_rate,
        "data_collection": DATA_COLLECTION,
        "max_request_body_size": "never",
        "before_send": scrub_event,
        "before_send_transaction": scrub_event,
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
