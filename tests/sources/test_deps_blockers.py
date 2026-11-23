import json

import pytest
import responses

from apps.collect.sources import deps
from apps.collect.sources.deps import collect_lock

GH = "https://api.github.com/repos/Foxugly/x/contents/"

# ---------------------------------------------------------------------------- npm

NG_LOCK = {
    "lockfileVersion": 3,
    "packages": {
        "": {
            "dependencies": {"@angular/core": "^22.3.0"},
            "devDependencies": {
                "@angular/compiler-cli": "^22.3.0",
                "typescript": "~6.0.3",
            },
        },
        "node_modules/@angular/core": {"version": "22.3.0"},
        "node_modules/@angular/compiler-cli": {
            "version": "22.3.0",
            "peerDependencies": {"typescript": ">=6.0 <6.1"},
        },
        "node_modules/typescript": {"version": "6.0.3"},
        # transitive: sorted after the direct blocker
        "node_modules/@angular/build": {
            "version": "22.3.0",
            "peerDependencies": {"typescript": ">=6.0 <6.1", "less": "^4"},
            "peerDependenciesMeta": {"less": {"optional": True}},
        },
        # the same blocker nested twice is listed once
        "node_modules/x/node_modules/@angular/build": {
            "version": "22.3.0",
            "peerDependencies": {"typescript": ">=6.0 <6.1"},
        },
        # accepts the latest: not a blocker
        "node_modules/ok": {
            "version": "1.0.0",
            "peerDependencies": {"typescript": "*"},
        },
        # range not understood: never blocking
        "node_modules/odd": {
            "version": "1.0.0",
            "peerDependencies": {"typescript": "npm:typescript@6"},
        },
        # already excludes the current version: not a new block
        "node_modules/old": {
            "version": "1.0.0",
            "peerDependencies": {"typescript": "^4"},
        },
    },
}
DIRECT = {"@angular/core", "@angular/compiler-cli", "typescript"}


def test_npm_blockers_direct_first_deduped_and_capped(monkeypatch):
    packages = NG_LOCK["packages"]
    got = deps.npm_blockers(packages, DIRECT, "typescript", "6.0.3", "7.0.2")
    assert got == [
        {
            "kind": "package",
            "package": "@angular/compiler-cli",
            "version": "22.3.0",
            "range": ">=6.0 <6.1",
        },
        {
            "kind": "package",
            "package": "@angular/build",
            "version": "22.3.0",
            "range": ">=6.0 <6.1",
        },
    ]
    assert deps.npm_blockers(packages, DIRECT, "typescript", "6.0.3", "6.0.9") == []
    monkeypatch.setattr(deps, "MAX_BLOCKERS", 1)
    assert len(deps.npm_blockers(packages, DIRECT, "typescript", "6.0.3", "7.0.2")) == 1


def test_npm_optional_peer_only_when_installed():
    packages = {
        "node_modules/a": {
            "version": "1.0.0",
            "peerDependencies": {"less": "^4"},
            "peerDependenciesMeta": {"less": {"optional": True}},
        },
    }
    assert deps.npm_blockers(packages, set(), "less", "4.1.0", "5.0.0") == []
    packages["node_modules/less"] = {"version": "4.1.0"}
    assert deps.npm_blockers(packages, set(), "less", "4.1.0", "5.0.0") == [
        {"kind": "package", "package": "a", "version": "1.0.0", "range": "^4"}
    ]


def test_npm_unparseable_peer_is_logged(caplog):
    caplog.set_level("INFO")
    packages = {"node_modules/a": {"version": "1", "peerDependencies": {"b": "next"}}}
    assert deps.npm_blockers(packages, set(), "b", "1.0.0", "2.0.0") == []
    assert "peer range not understood" in caplog.text


def _npm_registry(cli_latest: dict) -> None:
    responses.get(GH + "package-lock.json", body=json.dumps(NG_LOCK))
    responses.get(
        "https://registry.npmjs.org/@angular%2Fcore/latest", json={"version": "22.3.0"}
    )
    responses.get(
        "https://registry.npmjs.org/@angular%2Fcompiler-cli/latest", json=cli_latest
    )
    responses.get(
        "https://registry.npmjs.org/typescript/latest", json={"version": "7.0.2"}
    )


@responses.activate
def test_collect_npm_marks_typescript_blocked_with_newer():
    # compiler-cli 23 accepts typescript 7: the update can be retried with it.
    _npm_registry(
        {"version": "23.0.0", "peerDependencies": {"typescript": ">=7.0 <7.1"}}
    )
    out = collect_lock("Foxugly/x:package-lock.json", token="t")
    ts = next(o for o in out["outdated"] if o["name"] == "typescript")
    assert ts["blocked_by"][0] == {
        "kind": "package",
        "package": "@angular/compiler-cli",
        "version": "22.3.0",
        "range": ">=6.0 <6.1",
        "newer": "23.0.0",
    }
    # @angular/build is not a direct dependency, so not checked: newer unknown.
    assert ts["blocked_by"][1]["newer"] is None
    cli = next(o for o in out["outdated"] if o["name"] == "@angular/compiler-cli")
    assert "blocked_by" not in cli


@responses.activate
def test_collect_npm_newer_blocker_still_blocking():
    _npm_registry(
        {"version": "22.4.0", "peerDependencies": {"typescript": ">=6.0 <6.2"}}
    )
    out = collect_lock("Foxugly/x:package-lock.json", token="t")
    ts = next(o for o in out["outdated"] if o["name"] == "typescript")
    assert ts["blocked_by"][0]["newer"] is None


@responses.activate
def test_collect_npm_newer_blocker_drops_the_peer():
    _npm_registry({"version": "23.0.0"})
    out = collect_lock("Foxugly/x:package-lock.json", token="t")
    ts = next(o for o in out["outdated"] if o["name"] == "typescript")
    assert ts["blocked_by"][0]["newer"] == "23.0.0"


# ------------------------------------------------------------------------- Python

PY_LOCK = "\n".join(
    ["celery==5.6.3", "python-dateutil==2.9.0.post0", "kombu==5.6.0", "other==1.0"]
)


class FakeCache:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, timeout):
        assert timeout is None  # releases are immutable: cached forever
        self.data[key] = value


def _pypi(name, version, requires=None):
    responses.get(
        f"https://pypi.org/pypi/{name}/json",
        json={"info": {"version": version, "requires_dist": requires}},
    )


def _release(name, version, requires=None, status=200):
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    if status != 200:
        responses.get(url, status=status)
    else:
        responses.get(url, json={"info": {"requires_dist": requires}})


CELERY_REQUIRES = [
    "kombu<5.7,>=5.6.0",
    "python-dateutil==2.9.0.post0",
    "tzdata>=2022.7",
    'redis!=4.5.5,>=4.5.2; extra == "redis"',
]


@responses.activate
def test_collect_python_blocked_by_requires_dist_and_cached():
    responses.get(GH + "requirements.txt", body=PY_LOCK)
    responses.get(GH + "requirements.in", status=404)
    _pypi("celery", "5.6.3")
    _pypi("python-dateutil", "4.0.0")
    _pypi("kombu", "5.6.0")
    _pypi("other", "1.0")
    _release("celery", "5.6.3", CELERY_REQUIRES)
    _release("kombu", "5.6.0", ["python-dateutil>=2.8.2"])
    _release("other", "1.0", status=500)
    cache = FakeCache()
    out = collect_lock("Foxugly/x:requirements.txt", token="t", requires_cache=cache)
    assert out["outdated"] == [
        {
            "name": "python-dateutil",
            "current": "2.9.0.post0",
            "latest": "4.0.0",
            "kind": "major",
            "blocked_by": [
                {
                    "kind": "package",
                    "package": "celery",
                    "version": "5.6.3",
                    "range": "==2.9.0.post0",
                    "newer": None,
                }
            ],
        }
    ]
    # Successes are cached, failures retried next time.
    assert set(cache.data) == {
        "deps:pypi-requires:celery:5.6.3",
        "deps:pypi-requires:kombu:5.6.0",
    }
    before = len(responses.calls)
    collect_lock("Foxugly/x:requirements.txt", token="t", requires_cache=cache)
    release_calls = [
        c.request.url
        for c in responses.calls[before:]
        if c.request.url.startswith("https://pypi.org/")
        and c.request.url.count("/") == 6
    ]
    assert release_calls == ["https://pypi.org/pypi/other/1.0/json"]


@responses.activate
def test_collect_python_newer_blocker_unblocks():
    responses.get(GH + "requirements.txt", body=PY_LOCK)
    responses.get(GH + "requirements.in", status=404)
    _pypi("celery", "5.7.0", ["python-dateutil>=2.9"])
    _pypi("python-dateutil", "4.0.0")
    _pypi("kombu", "5.6.0")
    _pypi("other", "1.0")
    _release("celery", "5.6.3", ["python-dateutil==2.9.0.post0"])
    _release("kombu", "5.6.0", None)
    _release("other", "1.0", [])
    out = collect_lock("Foxugly/x:requirements.txt", token="t")
    dateutil = next(o for o in out["outdated"] if o["name"] == "python-dateutil")
    assert dateutil["blocked_by"][0]["newer"] == "5.7.0"
    celery = next(o for o in out["outdated"] if o["name"] == "celery")
    assert "blocked_by" not in celery


@responses.activate
def test_nothing_outdated_fetches_no_release_metadata():
    responses.get(GH + "requirements.txt", body="celery==5.6.3\n")
    responses.get(GH + "requirements.in", status=404)
    _pypi("celery", "5.6.3")
    collect_lock("Foxugly/x:requirements.txt", token="t")
    assert len(responses.calls) == 3  # lock, requirements.in, celery


@pytest.mark.parametrize(
    "requires,expected",
    [
        (["dateutil==2.9"], "==2.9"),
        (["dateutil"], None),  # no version constraint
        (['dateutil<3; extra == "x"'], None),  # extras-only
        (['dateutil<3; sys_platform == "win32"'], None),  # not on the box
        (['dateutil<3; python_version < "3.10"'], None),
        (['dateutil<3; python_version >= "3.14" and os_name == "posix"'], "<3"),
        (["not a requirement ((", "Dateutil<3"], "<3"),  # bad entries skipped
    ],
)
def test_python_constraint(requires, expected):
    spec = deps.python_constraint(requires, "dateutil")
    assert (None if spec is None else str(spec)) == expected


def test_python_blockers_cap_and_skip_current_conflict(monkeypatch):
    class Fixed:
        def get(self, name, version):
            return {"a": ["n<2"], "b": ["n<2"], "c": ["n>5"], "d": None}[name]

    pins = {"a": "1", "b": "1", "c": "1", "d": "1", "n": "1.5"}
    got = deps.python_blockers(pins, Fixed(), "n", "1.5", "2.0")
    assert [b["package"] for b in got] == ["a", "b"]  # c already excludes 1.5
    assert deps.python_blockers(pins, Fixed(), "n", "1.5", "bad!") == []
    monkeypatch.setattr(deps, "MAX_BLOCKERS", 1)
    assert len(deps.python_blockers(pins, Fixed(), "n", "1.5", "2.0")) == 1
