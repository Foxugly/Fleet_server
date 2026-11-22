from datetime import UTC, datetime, timedelta

import pytest
import responses

from apps.collect.sources import sentry
from apps.collect.sources.http import SourceError

API = "https://de.sentry.io/api/0/"
URL = f"{API}projects/org/proj/issues/"
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _issue(i, first_hours_ago=100, last_minutes_ago=None):
    return {
        "title": f"Err {i}",
        "culprit": f"mod.f{i}",
        "count": str(i + 1),
        "lastSeen": _iso(NOW - timedelta(minutes=last_minutes_ago or i)),
        "firstSeen": _iso(NOW - timedelta(hours=first_hours_ago)),
        "permalink": f"https://sentry.io/i/{i}/",
        "level": "error",
    }


def _link(next_url, results):
    return (
        "<https://de.sentry.io/api/0/projects/org/proj/issues/?cursor=0:0:1>; "
        'rel="previous"; results="false"; cursor="0:0:1", '
        f'<{next_url}>; rel="next"; results="{results}"; cursor="0:100:0"'
    )


@responses.activate
def test_counts_unresolved_and_new():
    issues = [_issue(0, first_hours_ago=2), _issue(1), _issue(2, first_hours_ago=23)]
    responses.get(URL, json=issues)
    out = sentry.collect_project("proj", token="t", org="org", api=API, now=NOW)
    assert out["unresolved"] == 3
    assert out["new_24h"] == 2
    first = out["issues"][0]
    assert first == {
        "title": "Err 0",
        "culprit": "mod.f0",
        "count": 1,
        "last_seen": _iso(NOW),
        "first_seen": _iso(NOW - timedelta(hours=2)),
        "url": "https://sentry.io/i/0/",
        "level": "error",
    }
    assert "is%3Aunresolved" in responses.calls[0].request.url
    assert responses.calls[0].request.headers["Authorization"] == "Bearer t"


@responses.activate
def test_api_without_trailing_slash():
    responses.get(URL, json=[])
    out = sentry.collect_project(
        "proj", token="t", org="org", api=API.rstrip("/"), now=NOW
    )
    assert out == {"unresolved": 0, "new_24h": 0, "issues": []}


@responses.activate
def test_paginates_until_results_false_and_keeps_top_25():
    page2 = f"{URL}?cursor=0:100:0"
    responses.get(
        URL,
        json=[_issue(i) for i in range(30)],
        headers={"Link": _link(page2, "true")},
    )
    responses.get(
        page2,
        json=[_issue(100 + i, last_minutes_ago=500 + i) for i in range(5)],
        headers={"Link": _link(f"{URL}?cursor=0:200:0", "false")},
    )
    out = sentry.collect_project("proj", token="t", org="org", api=API, now=NOW)
    assert out["unresolved"] == 35
    assert len(out["issues"]) == 25
    seen = [i["last_seen"] for i in out["issues"]]
    assert seen == sorted(seen, reverse=True)
    assert len(responses.calls) == 2


@responses.activate
def test_unknown_project_raises():
    responses.get(URL, status=404)
    with pytest.raises(SourceError, match="sentry project proj not found"):
        sentry.collect_project("proj", token="t", org="org", api=API)


@responses.activate
@pytest.mark.parametrize("status", [401, 403])
def test_auth_error_raises(status):
    responses.get(URL, status=status)
    with pytest.raises(SourceError, match="authentication") as exc:
        sentry.collect_project("proj", token="secret-tok", org="org", api=API)
    assert "secret-tok" not in str(exc.value)


@responses.activate
def test_session_is_closed(monkeypatch):
    closed = []
    real = sentry._session

    def tracking(token):
        session = real(token)
        orig = session.close
        session.close = lambda: (closed.append(True), orig())
        return session

    monkeypatch.setattr(sentry, "_session", tracking)
    responses.get(URL, json=[_issue(0)])
    sentry.collect_project("proj", token="t", org="org", api=API, now=NOW)
    assert closed == [True]


@responses.activate
def test_project_segment_is_url_quoted():
    responses.get(f"{API}projects/org/a%2Fb%3Fc/issues/", json=[])
    sentry.collect_project("a/b?c", token="t", org="org", api=API, now=NOW)
    assert responses.calls[0].request.url.startswith(
        f"{API}projects/org/a%2Fb%3Fc/issues/"
    )
