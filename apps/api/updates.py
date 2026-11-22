"""Updates shared by several sites: pure grouping, nothing collected or written."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from apps.collect.sources.deps import _norm
from apps.inventory.loader import Site

KIND_RANK = {"unknown": 0, "patch": 1, "minor": 2, "major": 3}
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def _severity(raw: Any) -> str:
    value = str(raw or "").lower()
    if value == "moderate":
        value = "medium"
    return value if value in SEVERITY_RANK else "low"


def _security(alerts: list[dict]) -> dict[str, Any] | None:
    if not alerts:
        return None
    worst = max((_severity(a.get("severity")) for a in alerts), key=SEVERITY_RANK.get)
    return {"alerts": len(alerts), "max_severity": worst}


def _key(ecosystem: str, name: str) -> str:
    # PyPI names are normalised (as the collector stores them); npm names are
    # case-insensitive but `.`/`_` are meaningful (chart.js is not chart-js).
    return name.lower() if ecosystem == "npm" else _norm(name)


def group_updates(
    sites: list[Site], deps: dict[str, dict], alerts: dict[str, list[dict]]
) -> list[dict]:
    site_repos = {s.slug: s.repos for s in sites}

    def repo_alerts(repo: str, matches) -> list[dict]:
        return [
            a
            for a in alerts.get(repo) or []
            if a.get("package") and matches(str(a["package"]))
        ]

    # (key, ecosystem) -> (display name, members by (site, lock))
    buckets: dict[tuple[str, str, str], dict[tuple[str, str], dict]] = defaultdict(dict)
    display: dict[tuple[str, str, str], str] = {}
    for site in sites:
        for lock in (*site.python_locks, *site.npm_locks):
            data = deps.get(lock)
            if not data:
                continue
            ecosystem = str(data.get("ecosystem") or "")
            repo = lock.partition(":")[0]
            for item in data.get("outdated") or []:
                name, latest = item.get("name"), item.get("latest")
                if not name or not latest:
                    continue
                key = (ecosystem, _key(ecosystem, str(name)), latest)
                display.setdefault(
                    key, _norm(str(name)) if ecosystem != "npm" else str(name)
                )
                buckets[key][(site.slug, lock)] = {
                    "site": site.slug,
                    "repo": repo,
                    "lock": lock,
                    "current": item.get("current"),
                    "kind": item.get("kind") or "unknown",
                }

    groups: list[dict] = []
    kept: set[str] = set()  # lowercase + normalised spellings of kept packages
    for key, found in buckets.items():
        ecosystem, name_key, latest = key
        members = list(found.values())
        member_sites = {m["site"] for m in members}
        if len(member_sites) < 2:
            continue
        kind = max((m["kind"] for m in members), key=lambda k: KIND_RANK.get(k, 0))
        # Only alerts from the member sites' repos, each repo counted once.
        repos = dict.fromkeys(r for s in sorted(member_sites) for r in site_repos[s])
        hits = [
            a
            for repo in repos
            for a in repo_alerts(
                repo, lambda p, k=name_key, e=ecosystem: _key(e, p) == k
            )
        ]
        kept.add(name_key)
        groups.append(
            {
                "ecosystem": ecosystem,
                "package": display[key],
                "latest": latest,
                "kind": kind,
                "members": [
                    {k: m[k] for k in ("site", "repo", "lock", "current")}
                    for m in members
                ],
                "security": _security(hits),
            }
        )

    # Alert-only groups: ecosystem is unknown, so match on the normalised name.
    alert_pkgs: dict[str, dict[tuple[str, str], list[dict]]] = defaultdict(dict)
    shown: dict[str, str] = {}
    for site in sites:
        for repo in site.repos:
            for a in repo_alerts(repo, lambda _p: True):
                pkg = str(a["package"])
                shown.setdefault(_norm(pkg), pkg.lower())
                alert_pkgs[_norm(pkg)].setdefault((site.slug, repo), []).append(a)
    for norm, per_repo in alert_pkgs.items():
        if norm in kept or shown[norm] in kept:
            continue
        if len({s for s, _ in per_repo}) < 2:
            continue
        by_repo: dict[str, list[dict]] = {}
        for (_s, repo), items in per_repo.items():
            by_repo[repo] = items  # a repo shared by two sites counts once
        groups.append(
            {
                "ecosystem": None,
                "package": shown[norm],
                "latest": None,
                "kind": None,
                "members": [
                    {"site": s, "repo": r, "lock": None, "current": None}
                    for s, r in per_repo
                ],
                "security": _security([a for v in by_repo.values() for a in v]),
            }
        )

    for g in groups:
        g["members"].sort(key=lambda m: (m["site"], m["lock"] or "", m["repo"]))

    def order(g: dict) -> tuple[int, int, str]:
        sec = g["security"]
        rank = SEVERITY_RANK[sec["max_severity"]] if sec else 0
        return (-rank, -len({m["site"] for m in g["members"]}), g["package"])

    groups.sort(key=order)
    return groups
