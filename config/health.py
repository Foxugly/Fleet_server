"""Liveness endpoint for nginx, UptimeRobot and the deploy pipeline.

Outside `/api/` and unauthenticated; it never reveals more than a status.
"""

from __future__ import annotations

import logging

from django.db import connection
from django.http import HttpRequest, JsonResponse

LOGGER = logging.getLogger("apps.health")


def health(request: HttpRequest) -> JsonResponse:
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 - logged, never exposed to the probe
        LOGGER.exception("health: database check failed")
        return JsonResponse({"status": "degraded"}, status=503)
    return JsonResponse({"status": "ok"})
