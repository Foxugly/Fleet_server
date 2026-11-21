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


@responses.activate
def test_main_run_skips_codeql():
    mock_all()
    run = collect_repo("Foxugly/x", token="tok")["main_run"]
    assert run["workflow"] == "CI" and run["conclusion"] == "failure"


@responses.activate
def test_main_run_none_when_only_codeql():
    runs = {"workflow_runs": [fx("runs")["workflow_runs"][0]]}
    mock_all(**{"/actions/runs": runs})
    assert collect_repo("Foxugly/x", token="tok")["main_run"] is None


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
