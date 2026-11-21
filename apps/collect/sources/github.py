"""GitHub collector: pure function, token passed in, returns a plain dict."""

import requests

from .http import get_json, get_json_paginated

API = "https://api.github.com"
PER_PAGE = {"per_page": 100}
BAD_CONCLUSIONS = {"failure", "cancelled", "timed_out", "action_required"}
BAD_STATES = {"failure", "error"}
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


def _checks(session, base: str, sha: str) -> str:
    runs = get_json_paginated(
        session, f"{base}/commits/{sha}/check-runs", params=PER_PAGE, key="check_runs"
    )
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
                "checks": _checks(session, base, pr["head"]["sha"]),
                "merge_state": detail.get("mergeable_state", "unknown"),
                "created_at": pr["created_at"],
            }
        )
    return out


def _main_run(session, base: str) -> dict | None:
    body = get_json(
        session,
        f"{base}/actions/runs",
        params={"branch": "main", "status": "completed", **PER_PAGE},
    )
    for run in body.get("workflow_runs", []):
        if run.get("name") != "CodeQL":
            return {
                "workflow": run["name"],
                "conclusion": run.get("conclusion"),
                "url": run["html_url"],
                "created_at": run["created_at"],
            }
    return None


def collect_repo(repo: str, *, token: str) -> dict:
    session = _session(token)
    base = f"{API}/repos/{repo}"
    open_params = {"state": "open", **PER_PAGE}

    prs = _prs(session, base)
    issues = [
        {
            "number": i["number"],
            "title": i["title"],
            "url": i["html_url"],
            "created_at": i["created_at"],
        }
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
        for a in get_json_paginated(
            session, f"{base}/code-scanning/alerts", params=open_params, allow_404=True
        )
    ]
    secret_scanning = [
        {"type": a["secret_type"], "url": a["html_url"]}
        for a in get_json_paginated(
            session,
            f"{base}/secret-scanning/alerts",
            params=open_params,
            allow_404=True,
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
