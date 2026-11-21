"""Traffic-light aggregation of collected snapshots. Pure logic, no HTTP."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from apps.collect import store
from apps.collect.intervals import INTERVALS
from apps.collect.sources.github import OK_CONCLUSIONS

STALE_FACTOR = 3
TLS_RED_DAYS = 14
DISK_RED_PCT = 90
HIGH_SEVERITIES = {"critical", "high"}

#: Worst first. `none` only ever wins when there is nothing else to compare.
STATE_ORDER = ["error", "red", "stale", "orange", "green", "none"]


@dataclass(frozen=True)
class Verdict:
    state: str  # red | orange | green
    count: int


def _verdict(red: bool, count: int) -> Verdict:
    if red:
        # A red cell always shows at least one thing to look at.
        return Verdict("red", max(count, 1))
    return Verdict("orange" if count else "green", count)


def _health(data: dict[str, Any]) -> Verdict:
    count = 0 if data.get("status") == 200 else 1
    days = data.get("tls_days_left")
    red = (
        data.get("status") != 200
        # Ruling R5: failed TLS verification/handshake is red.
        or "TLS " in (data.get("error") or "")
        or (days is not None and days < TLS_RED_DAYS)
    )
    return _verdict(red, count)


def _github(data: dict[str, Any]) -> Verdict:
    dependabot = data.get("dependabot_alerts") or []
    code_scanning = data.get("code_scanning") or []
    secrets = data.get("secret_scanning") or []
    main_run = data.get("main_run")
    # Deny by default: any conclusion that is not explicitly OK is a failure.
    main_failed = bool(main_run) and main_run.get("conclusion") not in OK_CONCLUSIONS
    count = (
        len(data.get("prs") or [])
        + len(data.get("issues") or [])
        + len(dependabot)
        + len(code_scanning)
        + len(secrets)
        + (1 if main_failed else 0)
    )
    high = any(
        (a.get("severity") or "").lower() in HIGH_SEVERITIES
        for a in (*dependabot, *code_scanning)
    )
    return _verdict(main_failed or bool(secrets) or high, count)


def _sentry(data: dict[str, Any]) -> Verdict:
    return _verdict(False, int(data.get("unresolved") or 0))


def _deps(data: dict[str, Any]) -> Verdict:
    # A major bump is orange, never red.
    return _verdict(False, len(data.get("outdated") or []))


def _box(data: dict[str, Any]) -> Verdict:
    apt = data.get("apt") or []
    failed = data.get("failed_units") or []
    reboot = bool(data.get("reboot_required"))
    used = (data.get("disk") or {}).get("used_pct") or 0
    red = bool(failed) or any(p.get("security") for p in apt) or used >= DISK_RED_PCT
    return _verdict(red, len(apt) + len(failed) + (1 if reboot else 0))


def _worst(*states: str) -> str:
    return min(states, key=STATE_ORDER.index)


_EVALUATORS = {
    "health": _health,
    "github": _github,
    "sentry": _sentry,
    "deps": _deps,
    "box": _box,
}


def cell(source: str, subjects: list[str], now: datetime) -> dict[str, Any]:
    """Aggregate the latest snapshots of `subjects` into one dashboard cell."""
    if not subjects:
        return {"state": "none", "count": 0, "collected_at": None, "error": ""}

    stale_after = STALE_FACTOR * INTERVALS[source]
    evaluate = _EVALUATORS[source]
    states: list[str] = []
    count = 0
    ok_times: list[datetime] = []
    error = ""
    error_collected_at: datetime | None = None

    for subject in subjects:
        latest = store.latest(source, subject)
        good = store.latest_ok(source, subject)
        if good is not None:
            ok_times.append(good.collected_at)
            count += evaluate(good.data).count
        if latest is None:
            states.append("stale")
        elif not latest.ok:
            states.append("error")
            if not error:
                error = latest.error
                error_collected_at = good.collected_at if good else None
        elif (now - latest.collected_at).total_seconds() > stale_after:
            # Old data still counts: red stays red, green/orange become stale.
            states.append(_worst(evaluate(latest.data).state, "stale"))
        else:
            states.append(evaluate(latest.data).state)

    state = _worst(*states)
    if state == "error":
        collected = error_collected_at
    else:
        collected = min(ok_times) if ok_times else None
        error = ""
    return {
        "state": state,
        "count": count,
        "collected_at": collected.isoformat() if collected else None,
        "error": error,
    }


def worst_rank(cells: dict[str, dict[str, Any]]) -> int:
    return min(STATE_ORDER.index(c["state"]) for c in cells.values())
