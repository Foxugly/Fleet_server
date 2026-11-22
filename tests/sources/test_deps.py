import re
from pathlib import Path

import pytest
import requests
import responses

from apps.collect.sources import deps
from apps.collect.sources.deps import (
    bump_kind,
    collect_lock,
    parse_npm_lock,
    parse_python_lock,
)
from apps.collect.sources.http import SourceError

FIX = Path(__file__).parent / "fixtures" / "deps"
LOCK = chr(10).join(
    [
        "django==6.1.2 " + chr(92),
        "    --hash=sha256:aa",
        "broken==1.0",
        "netdown==1.0",
        "ok==1.0",
    ]
)
GH = "https://api.github.com/repos/Foxugly/x/contents/"


def test_parse_uv_lock():
    fabric = parse_python_lock((FIX / "fabric-requirements.txt").read_text())
    assert fabric == {
        "asgiref": "3.13.0",
        "attrs": "26.1.0",
        "autobahn": "26.11.1",
    }
    quiz = parse_python_lock((FIX / "quiz-requirements.txt").read_text())
    # marker stripped, first occurrence kept, _ normalised, -e/-r ignored
    assert quiz == {"amqp": "5.4.2", "cffi": "2.1.1", "zope-interface": "7.0"}


def test_parse_pep503_names():
    assert parse_python_lock("Foo.Bar_baz==1.0\n") == {"foo-bar-baz": "1.0"}


def test_parse_npm_lock_direct_only():
    out = parse_npm_lock((FIX / "package-lock.json").read_text())
    assert set(out) == {"@angular/core", "rxjs", "typescript"}  # no tslib, no ghost
    assert out["rxjs"] == "7.8.2"


@pytest.mark.parametrize(
    "cur,new,kind",
    [
        ("6.1.2", "6.1.3", "patch"),
        ("49.0.0", "50.0.2", "major"),
        ("2026.6.3", "2026.9.1", "minor"),
        ("1.0", "1.0.0", "none"),
        ("2.0", "1.9", "none"),
        ("1.0", "2.0rc1", "none"),
        ("1.0", "garbage", "unknown"),
    ],
)
def test_bump_kind(cur, new, kind):
    assert bump_kind(cur, new) == kind


@responses.activate
def test_registry_failure_marks_unknown():
    responses.get(
        GH + "requirements.txt",
        body=LOCK,
    )
    responses.get(
        "https://pypi.org/pypi/django/json", json={"info": {"version": "6.1.3"}}
    )
    responses.get("https://pypi.org/pypi/broken/json", status=500)
    responses.get("https://pypi.org/pypi/netdown/json", body=requests.ConnectionError())
    responses.get("https://pypi.org/pypi/ok/json", json={"info": {"version": "1.0"}})
    out = collect_lock("Foxugly/x:requirements.txt", token="tok")
    assert out["ecosystem"] == "python"
    assert out["checked"] == 4
    assert out["outdated"] == [
        {"name": "django", "current": "6.1.2", "latest": "6.1.3", "kind": "patch"},
        {"name": "broken", "current": "1.0", "latest": None, "kind": "unknown"},
        {"name": "netdown", "current": "1.0", "latest": None, "kind": "unknown"},
    ]
    gh = responses.calls[0].request
    assert gh.headers["Accept"] == "application/vnd.github.raw"
    assert gh.headers["Authorization"] == "Bearer tok"
    assert gh.params["ref"] == "main"
    for call in responses.calls[1:]:
        assert "Authorization" not in call.request.headers


@responses.activate
def test_collect_npm_scoped_and_path():
    responses.get(
        GH + "web/package-lock.json",
        body=(FIX / "package-lock.json").read_text(),
    )
    responses.get(
        "https://registry.npmjs.org/@angular%2Fcore/latest", json={"version": "22.0.0"}
    )
    responses.get("https://registry.npmjs.org/rxjs/latest", json={"version": "7.8.2"})
    responses.get(
        "https://registry.npmjs.org/typescript/latest", json={"version": "5.9.4"}
    )
    out = collect_lock("Foxugly/x:web/package-lock.json", token="t")
    assert out["ecosystem"] == "npm"
    assert out["checked"] == 3
    assert {(o["name"], o["kind"]) for o in out["outdated"]} == {
        ("@angular/core", "major"),
        ("typescript", "patch"),
    }


@responses.activate
def test_lock_not_found():
    responses.get(GH + "requirements.txt", status=404)
    with pytest.raises(SourceError, match="lock not found: Foxugly/x:requirements.txt"):
        collect_lock("Foxugly/x:requirements.txt", token="t")


@responses.activate
def test_caps_at_300(monkeypatch, caplog):
    body = "".join(f"p{i}==1.0\n" for i in range(305))
    responses.get(GH + "requirements.txt", body=body)
    responses.get(
        re.compile(r"https://pypi\.org/pypi/.*/json"),
        json={"info": {"version": "1.0"}},
    )
    out = collect_lock("Foxugly/x:requirements.txt", token="t")
    assert out["checked"] == deps.MAX_PACKAGES == 300
    assert "300" in caplog.text


def test_parse_extras():
    lock = chr(10).join(
        [
            "celery[redis]==5.6.3 " + chr(92),
            "    --hash=sha256:aa",
            "uvicorn[standard,x] == 0.40.0",
        ]
    )
    assert parse_python_lock(lock) == {"celery": "5.6.3", "uvicorn": "0.40.0"}


@responses.activate
def test_lock_with_no_pins_is_an_error():
    responses.get(GH + "requirements.txt", body="# only comments" + chr(10))
    with pytest.raises(SourceError, match="no pins parsed: Foxugly/x:requirements.txt"):
        collect_lock("Foxugly/x:requirements.txt", token="t")


@responses.activate
def test_lock_path_is_url_quoted():
    responses.get(GH + "my%20dir/requirements.txt", status=404)
    with pytest.raises(SourceError):
        collect_lock("Foxugly/x:my dir/requirements.txt", token="t")
    assert responses.calls[0].request.url.startswith(GH + "my%20dir/requirements.txt")
