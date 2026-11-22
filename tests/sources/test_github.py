import json
from pathlib import Path

import pytest
import responses

from apps.collect.sources.github import collect_repo
from apps.collect.sources.http import SourceError

FIX = Path(__file__).parent / "fixtures" / "github"
API = "https://api.github.com/repos/Foxugly/x"


def fx(name):
    return json.loads((FIX / f"{name}.json").read_text())


def resp(path, **kw):
    return responses.Response("GET", API + path, **kw)


def mock_all(**overrides):
    routes = {
        "/pulls": fx("pulls"),
        "/pulls/7": fx("pull_detail"),
        "/commits/abc123/check-runs": fx("check_runs"),
        "/commits/abc123/status": fx("status"),
        "/issues": fx("issues"),
        "/dependabot/alerts": fx("dependabot_alerts"),
        "/code-scanning/alerts": fx("code_scanning"),
        "/secret-scanning/alerts": fx("secret_scanning"),
        "/actions/runs": fx("runs"),
    }
    routes.update(overrides)
    for path, body in routes.items():
        if isinstance(body, responses.Response):
            responses.add(body)
        else:
            responses.get(API + path, json=body)


@responses.activate
def test_collect_repo_shapes_output():
    mock_all()
    out = collect_repo("Foxugly/x", token="tok")
    assert out["prs"] == [
        {
            "number": 7,
            "title": "Bump django",
            "url": "https://github.com/Foxugly/x/pull/7",
            "author": "dependabot[bot]",
            "is_dependabot": True,
            "draft": False,
            "checks": "failure",
            "merge_state": "blocked",
            "created_at": "2026-11-01T10:00:00Z",
        }
    ]
    assert [i["number"] for i in out["issues"]] == [3]
    assert out["dependabot_alerts"] == [
        {
            "package": "django",
            "severity": "high",
            "url": "https://github.com/Foxugly/x/security/dependabot/1",
            "created_at": "2026-11-02T10:00:00Z",
        }
    ]
    assert [c["severity"] for c in out["code_scanning"]] == ["critical", "warning"]
    assert out["code_scanning"][0]["path"] == "app/db.py"
    assert out["secret_scanning"][0]["type"] == "aws_access_key_id"
    headers = responses.calls[0].request.headers
    assert headers["Authorization"] == "Bearer tok"
    assert headers["X-GitHub-Api-Version"] == "2022-11-28"


@responses.activate
def test_paginates_alerts():
    alert = fx("dependabot_alerts")[0]
    mock_all(
        **{
            "/dependabot/alerts": resp(
                "/dependabot/alerts",
                json=[alert] * 100,
                headers={"Link": f'<{API}/dependabot/alerts?page=2>; rel="next"'},
            )
        }
    )
    responses.get(API + "/dependabot/alerts?page=2", json=[alert] * 50)
    out = collect_repo("Foxugly/x", token="tok")
    assert len(out["dependabot_alerts"]) == 150


@responses.activate
def test_code_scanning_404_no_analysis_is_empty():
    mock_all(
        **{
            "/code-scanning/alerts": resp(
                "/code-scanning/alerts",
                status=404,
                json={"message": "no analysis found"},
            )
        }
    )
    assert collect_repo("Foxugly/x", token="tok")["code_scanning"] == []


@responses.activate
def test_secret_scanning_disabled_404_is_empty():
    mock_all(
        **{
            "/secret-scanning/alerts": resp(
                "/secret-scanning/alerts", status=404, json={"message": "disabled"}
            )
        }
    )
    assert collect_repo("Foxugly/x", token="tok")["secret_scanning"] == []


@responses.activate
def test_rate_limited_raises_source_error_with_reset():
    responses.get(
        API + "/pulls",
        status=403,
        json={"message": "rate limit"},
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1790000000"},
    )
    with pytest.raises(SourceError) as exc:
        collect_repo("Foxugly/x", token="secret-token")
    assert "2026-09-21T" in str(exc.value)
    assert "secret-token" not in str(exc.value)


@responses.activate
def test_other_error_is_source_error_without_token():
    mock_all(**{"/issues": resp("/issues", status=500)})
    with pytest.raises(SourceError) as exc:
        collect_repo("Foxugly/x", token="secret-token")
    assert "500" in str(exc.value) and "/issues" in str(exc.value)
    assert "secret-token" not in str(exc.value)


def run(name, sha, conclusion, created, *, event="push", n=1):
    return {
        "name": name,
        "event": event,
        "head_sha": sha,
        "conclusion": conclusion,
        "html_url": f"https://github.com/Foxugly/x/actions/runs/{n}",
        "created_at": created,
    }


def main_run_of(runs):
    mock_all(**{"/actions/runs": {"workflow_runs": runs}})
    out = collect_repo("Foxugly/x", token="tok")["main_run"]
    call = next(c for c in responses.calls if "/actions/runs" in c.request.url)
    return out, call.request.params


@responses.activate
def test_main_run_skips_codeql():
    mock_all()
    run_ = collect_repo("Foxugly/x", token="tok")["main_run"]
    assert run_["workflow"] == "CI" and run_["conclusion"] == "failure"
    assert run_["sha"] == "s1"


@responses.activate
def test_main_run_none_when_only_codeql():
    runs = {"workflow_runs": [fx("runs")["workflow_runs"][0]]}
    mock_all(**{"/actions/runs": runs})
    assert collect_repo("Foxugly/x", token="tok")["main_run"] is None


@responses.activate
def test_main_run_query_is_push_on_main_completed():
    _, params = main_run_of([])
    assert params["branch"] == "main"
    assert params["event"] == "push"
    assert params["status"] == "completed"


@responses.activate
def test_main_run_newer_dependabot_run_does_not_hide_failed_ci():
    out, _ = main_run_of(
        [
            run(
                "Dependabot Updates",
                "s1",
                "success",
                "2026-11-20T10:00:00Z",
                event="dynamic",
                n=9,
            ),
            run("Deploy", "s1", "success", "2026-11-19T10:05:00Z", n=2),
            run("CI", "s1", "failure", "2026-11-19T10:00:00Z", n=1),
        ]
    )
    assert out == {
        "sha": "s1",
        "conclusion": "failure",
        "url": "https://github.com/Foxugly/x/actions/runs/1",
        "workflow": "CI",
        "created_at": "2026-11-19T10:00:00Z",
    }


@responses.activate
def test_main_run_fork_pull_request_on_branch_main_is_ignored():
    out, params = main_run_of(
        [
            run(
                "CI",
                "fork",
                "success",
                "2026-11-20T10:00:00Z",
                event="pull_request",
                n=5,
            ),
            run("CI", "s1", "failure", "2026-11-19T10:00:00Z", n=1),
        ]
    )
    assert params["event"] == "push"
    assert out["sha"] == "s1" and out["conclusion"] == "failure"


@responses.activate
def test_main_run_only_newest_sha_counts():
    out, _ = main_run_of(
        [
            run("CI", "new", "success", "2026-11-20T10:00:00Z", n=3),
            run("CI", "old", "failure", "2026-11-19T10:00:00Z", n=1),
        ]
    )
    assert out["sha"] == "new" and out["conclusion"] == "success"
    assert out["url"].endswith("/3")


@pytest.mark.parametrize(
    "bad", ["startup_failure", "cancelled", "timed_out", "action_required", None]
)
@responses.activate
def test_main_run_any_non_ok_conclusion_fails(bad):
    out, _ = main_run_of(
        [
            run("CI", "s1", "success", "2026-11-20T10:01:00Z", n=2),
            run("Deploy", "s1", bad, "2026-11-20T10:00:00Z", n=7),
        ]
    )
    assert out["conclusion"] == "failure" and out["workflow"] == "Deploy"


@pytest.mark.parametrize("ok", ["success", "skipped", "neutral"])
@responses.activate
def test_main_run_ok_conclusions_succeed(ok):
    out, _ = main_run_of([run("CI", "s1", ok, "2026-11-20T10:00:00Z")])
    assert out["conclusion"] == "success"


@responses.activate
def test_checks_none():
    mock_all(
        **{
            "/commits/abc123/check-runs": {"check_runs": []},
            "/commits/abc123/status": {"statuses": []},
        }
    )
    assert collect_repo("Foxugly/x", token="t")["prs"][0]["checks"] == "none"


@responses.activate
def test_checks_pending_beats_success_and_skipped_ignored():
    mock_all(
        **{
            "/commits/abc123/check-runs": {
                "check_runs": [
                    {"status": "completed", "conclusion": "success"},
                    {"status": "completed", "conclusion": "skipped"},
                    {"status": "in_progress", "conclusion": None},
                ]
            },
            "/commits/abc123/status": {"statuses": [{"state": "success"}]},
        }
    )
    assert collect_repo("Foxugly/x", token="t")["prs"][0]["checks"] == "pending"


@responses.activate
def test_checks_success():
    mock_all(
        **{
            "/commits/abc123/check-runs": {
                "check_runs": [{"status": "completed", "conclusion": "success"}]
            },
            "/commits/abc123/status": {"statuses": []},
        }
    )
    assert collect_repo("Foxugly/x", token="t")["prs"][0]["checks"] == "success"


@responses.activate
def test_check_runs_403_falls_back_to_statuses():
    """Private repo + fine-grained token: no checks=read, statuses still count."""
    mock_all(
        **{
            "/commits/abc123/check-runs": resp(
                "/commits/abc123/check-runs",
                status=403,
                json={"message": "Resource not accessible by personal access token"},
                headers={"X-Accepted-GitHub-Permissions": "checks=read"},
            ),
            "/commits/abc123/status": {"statuses": [{"state": "failure"}]},
        }
    )
    assert collect_repo("Foxugly/x", token="t")["prs"][0]["checks"] == "failure"


@responses.activate
def test_check_runs_other_403_still_raises():
    """A secondary rate limit is a 403 without the permission header."""
    mock_all(
        **{
            "/commits/abc123/check-runs": resp(
                "/commits/abc123/check-runs",
                status=403,
                json={"message": "You have exceeded a secondary rate limit"},
                headers={"Retry-After": "60"},
            ),
        }
    )
    with pytest.raises(SourceError):
        collect_repo("Foxugly/x", token="t")


@responses.activate
def test_check_runs_other_error_still_raises():
    mock_all(
        **{
            "/commits/abc123/check-runs": resp(
                "/commits/abc123/check-runs", status=500, json={}
            ),
        }
    )
    with pytest.raises(SourceError):
        collect_repo("Foxugly/x", token="t")


@responses.activate
@pytest.mark.parametrize(
    "path, message",
    [
        (
            "/code-scanning/alerts",
            "Code scanning is not enabled for this repository.",
        ),
        (
            "/secret-scanning/alerts",
            "Advanced Security must be enabled for this repository.",
        ),
    ],
)
def test_scanning_off_on_private_repo_403_is_empty(path, message):
    mock_all(**{path: resp(path, status=403, json={"message": message})})
    out = collect_repo("Foxugly/x", token="t")
    key = "code_scanning" if "code" in path else "secret_scanning"
    assert out[key] == []


@responses.activate
def test_scanning_permission_403_still_raises():
    path = "/code-scanning/alerts"
    mock_all(
        **{
            path: resp(
                path,
                status=403,
                json={"message": "Resource not accessible by personal access token"},
            )
        }
    )
    with pytest.raises(SourceError):
        collect_repo("Foxugly/x", token="t")
