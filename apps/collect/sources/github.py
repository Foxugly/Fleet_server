"""GitHub collector: pure function, token passed in, returns a plain dict."""

import json

import requests

from .http import SourceError, get_json, get_json_paginated

API = "https://api.github.com"
PER_PAGE = {"per_page": 100}
BAD_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required"}
BAD_STATES = {"failure", "error"}
#: Main-branch run conclusions that are not a failure; anything else is.
OK_CONCLUSIONS = {"success", "skipped", "neutral"}
PENDING_STATUSES = {"queued", "in_progress", "pending", "waiting", "requested"}


def _session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    )
    return s


def _check_runs(session, base: str, sha: str) -> list[dict]:
    """Check runs of a commit; [] when the token lacks `checks=read`.

    Reading check runs of a private repo needs `checks=read`, which fine-grained
    tokens cannot hold: the PR's state then falls back to commit statuses. Only
    the 403 naming that permission is tolerated (a secondary rate limit is a 403
    too and must still fail the collection).
    """
    try:
        return get_json_paginated(
            session,
            f"{base}/commits/{sha}/check-runs",
            params=PER_PAGE,
            key="check_runs",
        )
    except SourceError as exc:
        accepted = {k.lower(): v for k, v in exc.headers.items()}.get(
            "x-accepted-github-permissions", ""
        )
        if exc.status == 403 and "checks=read" in accepted:
            return []
        raise


#: Starts of GitHub's 403 `message` meaning "this repo has no scanning", not
#: "you may not read it".
SCANNING_OFF = (
    "code scanning is not enabled",
    "secret scanning is not enabled",
    "secret scanning is disabled",
    "advanced security must be enabled",
)


def _scanning_off(exc: SourceError) -> bool:
    if exc.status != 403:
        return False
    try:
        message = json.loads(exc.body).get("message", "")
    except (ValueError, AttributeError):
        return False
    return isinstance(message, str) and message.lower().startswith(SCANNING_OFF)


def _scanning_alerts(session, url: str, params: dict) -> list[dict]:
    """Code/secret scanning alerts; [] when scanning is off for the repo.

    Scanning off answers 404 on public repos but 403 on private ones; only a 403
    whose body says so is tolerated (a permission or rate-limit 403 still fails).
    """
    try:
        return get_json_paginated(session, url, params=params, allow_404=True)
    except SourceError as exc:
        if _scanning_off(exc):
            return []
        raise


def _checks(session, base: str, sha: str) -> str:
    runs = _check_runs(session, base, sha)
    statuses = get_json(session, f"{base}/commits/{sha}/status", params=PER_PAGE)[
        "statuses"
    ]
    failure = pending = success = False
    for run in runs:
        if run.get("status") != "completed":
            pending = True
        elif run.get("conclusion") in BAD_CONCLUSIONS:
            failure = True
        elif run.get("conclusion") == "success":
            success = True
    for st in statuses:
        state = st.get("state")
        if state in BAD_STATES:
            failure = True
        elif state == "pending":
            pending = True
        elif state == "success":
            success = True
    if failure:
        return "failure"
    if pending:
        return "pending"
    return "success" if success else "none"


def _prs(session, base: str) -> list[dict]:
    out = []
    for pr in get_json_paginated(
        session, f"{base}/pulls", params={"state": "open", **PER_PAGE}
    ):
        detail = get_json(session, f"{base}/pulls/{pr['number']}")
        login = (pr.get("user") or {}).get("login")
        out.append(
            {
                "number": pr["number"],
                "title": pr["title"],
                "url": pr["html_url"],
                "author": login,
                "is_dependabot": login == "dependabot[bot]",
                "draft": bool(pr.get("draft")),
                "branch": pr["head"]["ref"],
                "checks": _checks(session, base, pr["head"]["sha"]),
                "merge_state": detail.get("mergeable_state", "unknown"),
                "created_at": pr["created_at"],
            }
        )
    return out


def _main_run(session, base: str) -> dict | None:
    """Verdict of the newest commit pushed to main, across all its workflows.

    Only `push` runs count: a fork's pull_request from a branch named `main`, or
    Dependabot's own "dynamic" runs, say nothing about main. Every non-CodeQL run
    of that commit is evaluated; any conclusion outside OK_CONCLUSIONS (failure,
    cancelled, startup_failure, timed_out...) fails it, deny by default.
    """
    body = get_json(
        session,
        f"{base}/actions/runs",
        params={"branch": "main", "event": "push", "status": "completed", **PER_PAGE},
    )
    runs = [
        r
        for r in body.get("workflow_runs", [])
        if r.get("name") != "CodeQL" and r.get("event") == "push" and r.get("head_sha")
    ]
    if not runs:
        return None
    newest = max(runs, key=lambda r: r["created_at"])
    sha = newest["head_sha"]
    of_sha = [r for r in runs if r["head_sha"] == sha]
    failing = [r for r in of_sha if r.get("conclusion") not in OK_CONCLUSIONS]
    shown = failing[0] if failing else newest
    return {
        "sha": sha,
        "conclusion": "failure" if failing else "success",
        "url": shown["html_url"],
        "workflow": shown["name"],
        "created_at": shown["created_at"],
    }


#: Who may mark an update blocked: repos are public, so anyone can open an issue.
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


def _issue(i: dict) -> dict:
    out = {
        "number": i["number"],
        "title": i["title"],
        "url": i["html_url"],
        "created_at": i["created_at"],
        "author_association": i.get("author_association"),
    }
    if (
        str(i["title"]).startswith("[fleet]")
        and i.get("author_association") in TRUSTED_ASSOCIATIONS
    ):
        out["body"] = (i.get("body") or "")[:2000]
    return out


def collect_repo(repo: str, *, token: str) -> dict:
    session = _session(token)
    base = f"{API}/repos/{repo}"
    open_params = {"state": "open", **PER_PAGE}

    prs = _prs(session, base)
    issues = [
        _issue(i)
        for i in get_json_paginated(session, f"{base}/issues", params=open_params)
        if "pull_request" not in i
    ]
    dependabot = [
        {
            "package": a["dependency"]["package"]["name"],
            "severity": a["security_advisory"]["severity"],
            "url": a["html_url"],
            "created_at": a["created_at"],
        }
        for a in get_json_paginated(
            session, f"{base}/dependabot/alerts", params=open_params
        )
    ]
    code_scanning = [
        {
            "rule": a["rule"]["id"],
            "severity": a["rule"].get("security_severity_level")
            or a["rule"].get("severity"),
            "path": ((a.get("most_recent_instance") or {}).get("location") or {}).get(
                "path"
            ),
            "url": a["html_url"],
        }
        for a in _scanning_alerts(session, f"{base}/code-scanning/alerts", open_params)
    ]
    secret_scanning = [
        {"type": a["secret_type"], "url": a["html_url"]}
        for a in _scanning_alerts(
            session, f"{base}/secret-scanning/alerts", open_params
        )
    ]
    return {
        "prs": prs,
        "issues": issues,
        "dependabot_alerts": dependabot,
        "code_scanning": code_scanning,
        "secret_scanning": secret_scanning,
        "main_run": _main_run(session, base),
    }
