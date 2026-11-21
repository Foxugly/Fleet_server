"""Health + TLS collector: HTTP status/latency and certificate days left.

`probe` never raises; failures land in `error`. The HTTP and TLS parts are
independent so one failing does not blank the other.
"""

import socket
import ssl
import time
from datetime import UTC, datetime

import requests

TIMEOUT = 10
MAX_MSG = 200


def _describe(exc: Exception) -> str:
    msg = " ".join(str(exc).split())[:MAX_MSG]
    return f"{type(exc).__name__}: {msg}"


def _fetch_not_after(vhost: str) -> str:
    """Return the peer certificate's notAfter string (verified handshake)."""
    ctx = ssl.create_default_context()
    with socket.create_connection((vhost, 443), timeout=TIMEOUT) as sock:
        with ctx.wrap_socket(sock, server_hostname=vhost) as tls:
            return tls.getpeercert()["notAfter"]


def _tls_days_left(vhost: str, now: datetime) -> int:
    expires = ssl.cert_time_to_seconds(_fetch_not_after(vhost))
    return int((expires - now.timestamp()) // 86400)


def probe(vhost: str, path: str, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    status = latency_ms = tls_days_left = None
    errors = []

    start = time.monotonic()
    try:
        resp = requests.get(
            f"https://{vhost}{path}", timeout=TIMEOUT, allow_redirects=False
        )
        latency_ms = int((time.monotonic() - start) * 1000)
        status = resp.status_code
    except Exception as exc:
        errors.append(_describe(exc))

    try:
        tls_days_left = _tls_days_left(vhost, now)
    except Exception as exc:
        errors.append(f"TLS {_describe(exc)}")

    return {
        "status": status,
        "latency_ms": latency_ms,
        "error": "; ".join(errors),
        "tls_days_left": tls_days_left,
    }
