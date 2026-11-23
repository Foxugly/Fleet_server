"""Holds a project declares in its requirements.in (upper bounds + comment)."""

import pytest
import responses
from packaging.specifiers import SpecifierSet

from apps.collect.sources import deps
from apps.collect.sources.deps import collect_lock
from apps.collect.sources.http import SourceError

GH = "https://api.github.com/repos/Foxugly/x/contents/"
BS = chr(92)  # backslash, kept out of the source literals

REQ_IN = "\n".join(
    [
        "-r base.in",
        "--index-url https://example.invalid/simple",
        "Django>=6.1.3,<6.2",
        "# hold: sentry-sdk 3.0 day-0 major, revisit 2026-12-07",
        "sentry-sdk[django]>=2.76,<3",
        "celery>=5.5,<6  # hold: kombu not ready",
        "# a section title",
        "",
        "requests>=2.32",
        "pkg-a==1.2.3  # ordinary pin",
        "Pkg_B ~=1.4",
        "pkg-c==2.*",
        "pkg-d!=3.0.0,>=2",
        "pkg-e<=1.9; python_version >= '3.10'",
        "pkg-f @ https://example.invalid/f.tar.gz",
        "not a requirement ((",
        "pkg-g>=1,#not-a-comment",
        f"pkg-h>=1,{BS}",
        "    <2  # continued",
        "pkg-i<5",
        "pkg-i<4  # listed twice",
    ]
)


def test_parse_requirements_in_specifiers_and_comments():
    got = {k: (str(v[0]), v[1]) for k, v in deps.parse_requirements_in(REQ_IN).items()}
    assert got["django"] == ("<6.2,>=6.1.3", "")
    assert got["sentry-sdk"] == (
        "<3,>=2.76",
        "hold: sentry-sdk 3.0 day-0 major, revisit 2026-12-07",
    )
    assert got["celery"] == ("<6,>=5.5", "hold: kombu not ready")
    # A comment above a blank line does not reach the next requirement.
    assert got["requests"] == (">=2.32", "")
    assert got["pkg-a"] == ("==1.2.3", "ordinary pin")
    assert got["pkg-b"] == ("~=1.4", "")
    assert got["pkg-c"] == ("==2.*", "")
    assert got["pkg-e"][0] == "<=1.9"
    assert got["pkg-h"] == ("<2,>=1", "continued")
    assert got["pkg-i"] == ("<4,<5", "listed twice")
    assert "pkg-f" not in got  # URL requirement
    assert "pkg-g" not in got  # `#` glued to the specifier is not a comment
    assert "base-in" not in got and "index-url" not in got


def test_comment_above_only_from_the_line_right_above():
    text = "# hold: first\nrequests>=2\nflask<3\n"
    got = deps.parse_requirements_in(text)
    assert got["requests"][1] == "hold: first"
    assert got["flask"][1] == ""


def test_note_is_one_plain_capped_line():
    raw = "hold:\x1b[31m red‮ evil\t tab " + "x" * 400
    note = deps.parse_requirements_in(f"a<2  # {raw}\n")["a"][1]
    assert "\x1b" not in note and "‮" not in note and "\t" not in note
    assert note.startswith("hold: [31m red evil tab x")
    assert len(note) == deps.NOTE_MAX and note.endswith("…")


@pytest.mark.parametrize(
    "spec,latest,expected",
    [
        (">=2.76,<3", "3.0.0", "<3"),
        (">=2.76,<3", "2.77.0", None),
        ("<=1.9", "2.0", "<=1.9"),
        ("~=1.4", "2.0.0", "~=1.4"),
        ("~=1.4", "1.9.0", None),
        ("==2.*", "3.0.0", "==2.*"),
        ("!=3.0.0,>=2", "3.0.0", "!=3.0.0"),
        ("!=3.0.0,>=2", "3.0.1", None),
        ("==1.2.3", "1.3.0", None),  # an exact pin is not a hold
        (">=2", "9.0", None),
        ("", "9.0", None),
        ("<3", "not-a-version!", None),
        ("<3,<=2.9", "3.0", "<3,<=2.9"),
    ],
)
def test_declared_hold(spec, latest, expected):
    assert deps.declared_hold(SpecifierSet(spec), latest) == expected


@pytest.mark.parametrize(
    "lock,expected",
    [
        ("requirements.txt", ["requirements.in"]),
        ("backend/requirements.txt", ["backend/requirements.in"]),
        (
            "requirements/prod.txt",
            ["requirements/requirements.in", "requirements/base.in"],
        ),
        (
            "srv/requirements/prod.txt",
            ["srv/requirements/requirements.in", "srv/requirements/base.in"],
        ),
    ],
)
def test_requirements_in_paths(lock, expected):
    assert deps.requirements_in_paths(lock) == expected


LOCK = "sentry-sdk==2.76.0\nrequests==2.32.0\ncelery==5.6.0\n"


def _registry():
    for name, version in (
        ("sentry-sdk", "3.0.0"),
        ("requests", "2.33.0"),
        ("celery", "5.7.0"),
    ):
        responses.get(
            f"https://pypi.org/pypi/{name}/json",
            json={"info": {"version": version, "requires_dist": []}},
        )
    for name, version in (
        ("sentry-sdk", "2.76.0"),
        ("requests", "2.32.0"),
        ("celery", "5.6.0"),
    ):
        responses.get(
            f"https://pypi.org/pypi/{name}/{version}/json",
            json={"info": {"requires_dist": []}},
        )


@responses.activate
def test_collect_marks_held_items_first():
    responses.get(GH + "backend/requirements.txt", body=LOCK)
    responses.get(
        GH + "backend/requirements.in",
        body="# hold: day-0 major\nsentry-sdk[django]>=2.76,<3\nrequests>=2.32,<3\n"
        "celery>=5.5,<6\n",
    )
    _registry()
    out = collect_lock("Foxugly/x:backend/requirements.txt", token="t")
    by_name = {o["name"]: o for o in out["outdated"]}
    assert by_name["sentry-sdk"]["blocked_by"] == [
        {
            "kind": "hold",
            "package": "requirements.in",
            "version": "",
            "range": "<3",
            "note": "hold: day-0 major",
            "newer": None,
        }
    ]
    # Bounded but not excluding the latest: not held.
    assert "blocked_by" not in by_name["requests"]
    assert "blocked_by" not in by_name["celery"]
    gh_in = responses.calls[1].request
    assert gh_in.url.startswith(GH + "backend/requirements.in")
    assert gh_in.headers["Authorization"] == "Bearer t"
    assert gh_in.params["ref"] == "main"


@responses.activate
def test_collect_without_requirements_in_has_no_holds():
    responses.get(GH + "requirements.txt", body=LOCK)
    responses.get(GH + "requirements.in", status=404)
    _registry()
    out = collect_lock("Foxugly/x:requirements.txt", token="t")
    assert all("blocked_by" not in o for o in out["outdated"])


@responses.activate
def test_collect_reads_base_in_for_the_requirements_layout():
    responses.get(GH + "requirements/prod.txt", body=LOCK)
    responses.get(GH + "requirements/requirements.in", status=404)
    responses.get(GH + "requirements/base.in", body="sentry-sdk<3  # hold: wait\n")
    _registry()
    out = collect_lock("Foxugly/x:requirements/prod.txt", token="t")
    held = next(o for o in out["outdated"] if o["name"] == "sentry-sdk")
    assert held["blocked_by"][0]["package"] == "base.in"
    assert held["blocked_by"][0]["note"] == "hold: wait"


@responses.activate
def test_collect_fails_when_requirements_in_is_unreadable():
    responses.get(GH + "requirements.txt", body=LOCK)
    responses.get(GH + "requirements.in", status=500)
    with pytest.raises(SourceError):
        collect_lock("Foxugly/x:requirements.txt", token="t")


@responses.activate
def test_npm_lock_reads_no_requirements_in():
    responses.get(
        GH + "package-lock.json",
        body='{"packages": {"": {"dependencies": {"rxjs": "^7"}},'
        ' "node_modules/rxjs": {"version": "7.8.1"}}}',
    )
    responses.get("https://registry.npmjs.org/rxjs/latest", json={"version": "7.8.2"})
    collect_lock("Foxugly/x:package-lock.json", token="t")
    assert [c.request.url for c in responses.calls][1:] == [
        "https://registry.npmjs.org/rxjs/latest"
    ]
