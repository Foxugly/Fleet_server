"""Updates shared by several sites: pure grouping, nothing collected or written."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from packaging.version import InvalidVersion, Version

from apps.collect.sources.deps import _norm
from apps.collect.sources.github import TRUSTED_ASSOCIATIONS
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


_UPDATE = re.compile(
    r"^[ \t]*fleet-update:[ \t]*(\S+)[ \t]+(\S+)[ \t]*\r?$", re.I | re.M
)
_BLOCKER = re.compile(
    r"^[ \t]*fleet-blocked-by:[ \t]*(\S+)[ \t]+(\S+)[ \t]*\r?$", re.I | re.M
)


_PKG_OK = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9._@/+-]{0,213}$")
_VER_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+!-]{0,63}$")


def _valid(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Issue bodies are untrusted text: keep only well-formed tokens."""
    return [(p, v) for p, v in pairs if _PKG_OK.match(p) and _VER_OK.match(v)]


def _newer(version: str, candidate: str | None) -> bool:
    try:
        return candidate is not None and Version(candidate) > Version(version)
    except InvalidVersion:
        return False


def _latest_seen(
    deps: dict[str, dict], package: str, ecosystem: str | None
) -> str | None:
    """Highest stable `latest` outdated for `package`, in locks of `ecosystem`
    (every lock when the ecosystem is unknown)."""
    best: tuple[Version, str] | None = None
    for data in deps.values():
        eco = str(data.get("ecosystem") or "")
        if ecosystem and eco != ecosystem:
            continue
        for item in data.get("outdated") or []:
            latest = item.get("latest")
            if not item.get("name") or not latest:
                continue
            if _key(eco, str(item["name"])) != _key(eco, package):
                continue
            try:
                parsed = Version(str(latest))
            except InvalidVersion:
                continue
            if parsed.is_prerelease:
                continue
            if best is None or parsed > best[0]:
                best = (parsed, str(latest))
    return best[1] if best else None


def _blocked(
    group: dict, issues: dict[str, list[dict]], deps: dict[str, dict]
) -> dict | None:
    eco = group["ecosystem"] or ""
    want = group["latest"] or "security"
    pkg = _key(eco, group["package"])
    repos = dict.fromkeys(m["repo"] for m in group["members"])
    found: list[dict] = []
    by: dict[tuple[str, str], None] = {}
    for repo in repos:
        for issue in issues.get(repo) or []:
            body = issue.get("body")
            if issue.get("author_association") not in TRUSTED_ASSOCIATIONS:
                continue
            if not str(issue.get("title", "")).startswith("[fleet]") or not body:
                continue
            if not any(
                _key(eco, p) == pkg and v == want
                for p, v in _valid(_UPDATE.findall(body))
            ):
                continue
            blockers = _valid(_BLOCKER.findall(body))
            if not blockers:  # nothing to wait for: not a blocking issue
                continue
            found.append({"repo": repo, "number": issue["number"], "url": issue["url"]})
            by.update(dict.fromkeys(blockers))
    if not found:
        return None
    return {
        "issues": found,
        "by": [
            {
                "kind": "package",
                "note": "",
                "package": p,
                "version": v,
                "newer": (
                    n
                    if _newer(v, n := _latest_seen(deps, p, group["ecosystem"]))
                    else None
                ),
                "range": None,
            }
            for p, v in by
        ],
    }


def _auto_blocked(auto: list[dict]) -> dict | None:
    """Blockers the collector found in the members' locks (peer dependencies,
    requires_dist, declared holds), merged by (kind, package, version): newer
    when any lock saw one, the first non-empty note."""
    by: dict[tuple[str, str, str], dict] = {}
    for b in auto:
        package, version = str(b.get("package") or ""), str(b.get("version") or "")
        kind = "hold" if b.get("kind") == "hold" else "package"
        if not package:
            continue
        seen = by.setdefault(
            (kind, package, version),
            {
                "kind": kind,
                "package": package,
                "version": version,
                "newer": None,
                "range": b.get("range"),
                "note": "",
            },
        )
        if not seen["newer"] and b.get("newer"):
            seen["newer"] = str(b["newer"])
        if not seen["note"] and b.get("note"):
            seen["note"] = str(b["note"])
    if not by:
        return None
    return {"issues": [], "by": list(by.values())}


def _is_hold(blocker: Any) -> bool:
    return isinstance(blocker, dict) and blocker.get("kind") == "hold"


def _members_blockers(members: list[dict]) -> list[dict]:
    """The members' blockers. A declared hold counts only when every member
    holds the update: one site that has not capped it yet can still do it."""
    all_held = all(any(_is_hold(b) for b in m["blocked_by"]) for m in members)
    return [
        b
        for m in members
        for b in m["blocked_by"]
        if isinstance(b, dict) and (all_held or not _is_hold(b))
    ]


def _track(
    group: dict,
    prs: dict[str, list[dict]],
    issues: dict[str, list[dict]],
    deps: dict[str, dict],
) -> None:
    """Add `branch`, `prs` and `progress`: the PRs opened on the group's branch."""
    suffix = group["latest"] or "security"
    branch = f"deps/{group['package']}-{suffix}"
    repos = list(dict.fromkeys(m["repo"] for m in group["members"]))
    found = [
        {
            "repo": repo,
            "number": pr["number"],
            "url": pr["url"],
            "checks": pr.get("checks") or "none",
        }
        for repo in repos
        for pr in prs.get(repo) or []
        if pr.get("branch") == branch
    ]
    if not found:
        progress = "none"
    elif any(p["checks"] == "failure" for p in found):
        progress = "blocked"
    elif {p["repo"] for p in found} != set(repos) or any(
        p["checks"] != "success" for p in found
    ):
        progress = "in_progress"
    else:
        progress = "possible"
    auto = group.pop("_auto_by", [])
    # A [fleet] issue wins; otherwise what the locks say.
    blocked = _blocked(group, issues, deps) or _auto_blocked(auto)
    if progress == "none" and blocked:
        retry = any(b["newer"] for b in blocked["by"])
        progress = "retry" if retry else "waiting"
    group.update(branch=branch, prs=found, progress=progress, blocked=blocked)


def group_updates(
    sites: list[Site],
    deps: dict[str, dict],
    alerts: dict[str, list[dict]],
    prs: dict[str, list[dict]] | None = None,
    issues: dict[str, list[dict]] | None = None,
    min_sites: int = 2,
) -> list[dict]:
    """Updates of at least `min_sites` sites: 2 for the shared panel, 1 for every update
    (the « all apps » prompt)."""
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
                    "blocked_by": item.get("blocked_by") or [],
                }

    groups: list[dict] = []
    kept: set[str] = set()  # lowercase + normalised spellings of kept packages
    for key, found in buckets.items():
        ecosystem, name_key, latest = key
        members = list(found.values())
        member_sites = {m["site"] for m in members}
        if len(member_sites) < min_sites:
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
                "_auto_by": _members_blockers(members),
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
        if len({s for s, _ in per_repo}) < min_sites:
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

    for g in groups:
        _track(g, prs or {}, issues or {}, deps)

    groups.sort(key=order)
    return groups
