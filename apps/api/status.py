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


Reason = dict[str, Any]  # {"code": str, "params": {name: int | str}}

#: Reasons whose `n` is the cell's aggregated `count`, not a per-subject number.
COUNT_CODES = {"open_items", "unresolved", "outdated"}


def _reason(code: str, **params: int | str) -> Reason:
    return {"code": code, "params": params}


@dataclass(frozen=True)
class Verdict:
    state: str  # red | orange | green
    count: int
    reasons: tuple[Reason, ...] = ()  # most severe first, empty when green


def _verdict(red: bool, count: int, reasons: list[Reason] | None = None) -> Verdict:
    found = tuple(reasons or ())
    if red:
        # A red cell always shows at least one thing to look at.
        return Verdict("red", max(count, 1), found)
    return Verdict("orange" if count else "green", count, found)


def _health(data: dict[str, Any]) -> Verdict:
    status = data.get("status")
    count = 0 if status == 200 else 1
    days = data.get("tls_days_left")
    # Ruling R5: failed TLS verification/handshake is red.
    tls_error = "TLS " in (data.get("error") or "")
    expiring = days is not None and days < TLS_RED_DAYS
    reasons: list[Reason] = []
    if status is not None and status != 200:
        reasons.append(_reason("http_status", status=status))
    if status is None:
        reasons.append(_reason("unreachable"))
    if tls_error:
        reasons.append(_reason("tls_error"))
    if expiring:
        reasons.append(_reason("tls_expiring", days=days))
    return _verdict(status != 200 or tls_error or expiring, count, reasons)


def _github(data: dict[str, Any]) -> Verdict:
    dependabot = data.get("dependabot_alerts") or []
    code_scanning = data.get("code_scanning") or []
    secrets = data.get("secret_scanning") or []
    main_run = data.get("main_run")
    # Deny by default: any conclusion that is not explicitly OK is a failure.
    main_failed = bool(main_run) and main_run.get("conclusion") not in OK_CONCLUSIONS
    opened = len(data.get("prs") or []) + len(data.get("issues") or [])
    count = (
        opened
        + len(dependabot)
        + len(code_scanning)
        + len(secrets)
        + (1 if main_failed else 0)
    )
    high_alerts = [
        a
        for a in (*dependabot, *code_scanning)
        if (a.get("severity") or "").lower() in HIGH_SEVERITIES
    ]
    reasons: list[Reason] = []
    if main_failed:
        workflow = main_run.get("workflow") or ""
        reasons.append(_reason("main_ci_failed", workflow=workflow))
    if secrets:
        reasons.append(_reason("secret_alert", n=len(secrets)))
    if high_alerts:
        reasons.append(_reason("high_alert", n=len(high_alerts)))
    if count:
        reasons.append(_reason("open_items", n=count))
    return _verdict(main_failed or bool(secrets) or bool(high_alerts), count, reasons)


def _sentry(data: dict[str, Any]) -> Verdict:
    count = int(data.get("unresolved") or 0)
    return _verdict(False, count, [_reason("unresolved", n=count)] if count else None)


def _deps(data: dict[str, Any]) -> Verdict:
    # A major bump is orange, never red.
    count = len(data.get("outdated") or [])
    return _verdict(False, count, [_reason("outdated", n=count)] if count else None)


def _box(data: dict[str, Any]) -> Verdict:
    apt = data.get("apt") or []
    failed = data.get("failed_units") or []
    reboot = bool(data.get("reboot_required"))
    used = (data.get("disk") or {}).get("used_pct") or 0
    security = [p for p in apt if p.get("security")]
    red = bool(failed) or bool(security) or used >= DISK_RED_PCT
    count = len(apt) + len(failed) + (1 if reboot else 0)
    reasons: list[Reason] = []
    if failed:
        reasons.append(_reason("failed_units", n=len(failed)))
    if security:
        reasons.append(_reason("security_updates", n=len(security)))
    if used >= DISK_RED_PCT:
        reasons.append(_reason("disk_full", pct=used))
    if len(apt) > len(security):
        reasons.append(_reason("updates", n=len(apt) - len(security)))
    if reboot:
        reasons.append(_reason("reboot_required"))
    return _verdict(red, count, reasons)


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
        return {
            "state": "none",
            "count": 0,
            "collected_at": None,
            "error": "",
            "reason": None,
        }

    stale_after = STALE_FACTOR * INTERVALS[source]
    evaluate = _EVALUATORS[source]
    verdicts: list[tuple[str, tuple[Reason, ...]]] = []
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
            verdicts.append(("stale", (_reason("never_collected"),)))
        elif not latest.ok:
            verdicts.append(("error", (_reason("collect_error"),)))
            if not error:
                error = latest.error
                error_collected_at = good.collected_at if good else None
        else:
            verdict = evaluate(latest.data)
            if (now - latest.collected_at).total_seconds() > stale_after:
                # Old data still counts: red stays red, green/orange become stale.
                if _worst(verdict.state, "stale") == "stale":
                    stamp = latest.collected_at.isoformat()
                    verdicts.append(("stale", (_reason("stale", collected_at=stamp),)))
                    continue
            verdicts.append((verdict.state, verdict.reasons))

    # min() keeps the first of equally bad subjects.
    state, reasons = min(verdicts, key=lambda v: STATE_ORDER.index(v[0]))
    if state == "error":
        collected = error_collected_at
    else:
        collected = min(ok_times) if ok_times else None
        error = ""
    top = reasons[0] if reasons and state not in ("green", "none") else None
    if top is not None and top["code"] in COUNT_CODES:
        top = _reason(top["code"], n=count)
    return {
        "state": state,
        "count": count,
        "collected_at": collected.isoformat() if collected else None,
        "error": error,
        "reason": top,
    }


def worst_rank(cells: dict[str, dict[str, Any]]) -> int:
    return min(STATE_ORDER.index(c["state"]) for c in cells.values())
