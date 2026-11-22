"""Updates shared by several sites: pure grouping, nothing collected or written."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from apps.collect.sources.deps import _norm
from apps.inventory.loader import Site

KIND_RANK = {"unknown": 0, "patch": 1, "minor": 2, "major": 3}
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def _security(alerts: list[dict]) -> dict[str, Any] | None:
    if not alerts:
        return None
    worst = max(
        (str(a.get("severity", "")).lower() for a in alerts),
        key=lambda s: SEVERITY_RANK.get(s, 0),
    )
    return {"alerts": len(alerts), "max_severity": worst}


def group_updates(
    sites: list[Site], deps: dict[str, dict], alerts: dict[str, list[dict]]
) -> list[dict]:
    # package -> (site, repo) -> alerts of that package in that repo.
    alert_index: dict[str, dict[tuple[str, str], list[dict]]] = defaultdict(dict)
    for site in sites:
        for repo in site.repos:
            by_pkg: dict[str, list[dict]] = defaultdict(list)
            for alert in alerts.get(repo) or []:
                if alert.get("package"):
                    by_pkg[_norm(str(alert["package"]))].append(alert)
            for pkg, items in by_pkg.items():
                alert_index[pkg][(site.slug, repo)] = items

    buckets: dict[tuple[str, str, str], dict[tuple[str, str], dict]] = defaultdict(dict)
    for site in sites:
        for lock in (*site.python_locks, *site.npm_locks):
            data = deps.get(lock)
            if not data:
                continue
            repo = lock.partition(":")[0]
            for item in data.get("outdated") or []:
                name, latest = item.get("name"), item.get("latest")
                if not name or not latest:
                    continue
                key = (str(data.get("ecosystem") or ""), _norm(str(name)), latest)
                buckets[key][(site.slug, lock)] = {
                    "site": site.slug,
                    "repo": repo,
                    "lock": lock,
                    "current": item.get("current"),
                    "kind": item.get("kind") or "unknown",
                }

    groups: list[dict] = []
    for (ecosystem, package, latest), found in buckets.items():
        members = list(found.values())
        if len({m["site"] for m in members}) < 2:
            continue
        kind = max((m["kind"] for m in members), key=lambda k: KIND_RANK.get(k, 0))
        hits = [a for items in alert_index.get(package, {}).values() for a in items]
        groups.append(
            {
                "ecosystem": ecosystem,
                "package": package,
                "latest": latest,
                "kind": kind,
                "members": [
                    {k: m[k] for k in ("site", "repo", "lock", "current")}
                    for m in members
                ],
                "security": _security(hits),
            }
        )

    kept = {g["package"] for g in groups}
    for package, per_repo in alert_index.items():
        if package in kept or len({s for s, _ in per_repo}) < 2:
            continue
        groups.append(
            {
                "ecosystem": None,
                "package": package,
                "latest": None,
                "kind": None,
                "members": [
                    {"site": s, "repo": r, "lock": None, "current": None}
                    for s, r in per_repo
                ],
                "security": _security([a for v in per_repo.values() for a in v]),
            }
        )

    for g in groups:
        g["members"].sort(key=lambda m: (m["site"], m["lock"] or "", m["repo"]))

    def order(g: dict) -> tuple[int, int, str]:
        sec = g["security"]
        rank = SEVERITY_RANK.get(sec["max_severity"], 0) if sec else -1
        return (-rank, -len({m["site"] for m in g["members"]}), g["package"])

    groups.sort(key=order)
    return groups
