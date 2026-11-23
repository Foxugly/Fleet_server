"""Dependency freshness: read lock files from GitHub main, compare to PyPI / npm.

An outdated item another installed package holds back carries `blocked_by`:
npm reads the lock's `peerDependencies`, Python the `requires_dist` of the installed
releases (PyPI, cached forever: a release's metadata never changes).

A Python item whose latest release an upper bound of the project's own
`requirements.in` (or, without one, the `[project]` dependencies of the sibling
`pyproject.toml`) excludes is *held*: its first blocker is
`{"kind": "hold", "package": "requirements.in", "version": "", "range": "<3",
"note": <the requirement's comment>}`. Package blockers carry `"kind": "package"`.
"""

import json
import logging
import re
import tomllib
from urllib.parse import quote

import requests
from packaging.markers import InvalidMarker, default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import Specifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from . import npm_semver
from .github import API, _session
from .http import SourceError, get_json, get_response

log = logging.getLogger(__name__)

MAX_PACKAGES = 300
PYPI = "https://pypi.org/pypi/{name}/json"
PYPI_RELEASE = "https://pypi.org/pypi/{name}/{version}/json"
NPM = "https://registry.npmjs.org/{name}/latest"
#: Blockers kept per outdated item (npm: direct dependencies first).
MAX_BLOCKERS = 3
#: A hold's note is untrusted repo text: plain, one line, capped.
NOTE_MAX = 160
#: Specifier operators that cap a version (a plain `==X.Y.Z` pin is not a hold).
_UPPER_OPS = {"<", "<=", "~=", "!="}
#: Requirement markers are evaluated for the box: CPython 3.14 on Linux x86_64.
#: `extra` is empty, so extras-only requirements never apply.
MARKER_ENV = {
    **default_environment(),
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
    "python_version": "3.14",
    "python_full_version": "3.14.0",
    "sys_platform": "linux",
    "platform_system": "Linux",
    "os_name": "posix",
    "platform_machine": "x86_64",
    "extra": "",
}
# name, optional [extras] (dropped), == pin; markers and line continuations cut.
_REQ = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*([^\s;\x5c]+)"
)


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_python_lock(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue  # comments, --hash= continuations, -e / -r
        m = _REQ.match(line)
        if m:
            out.setdefault(_norm(m.group(1)), m.group(2))
    return out


def parse_npm_lock(text: str) -> dict[str, str]:
    packages = json.loads(text).get("packages", {})
    root = packages.get("", {})
    names = [*root.get("dependencies", {}), *root.get("devDependencies", {})]
    out: dict[str, str] = {}
    for name in names:
        version = packages.get(f"node_modules/{name}", {}).get("version")
        if version:
            out[name] = version
    return out


def bump_kind(current: str, latest: str) -> str:
    try:
        cur, new = Version(current), Version(latest)
    except InvalidVersion:
        return "unknown"
    if new.is_prerelease or new <= cur:
        return "none"
    c = (cur.release + (0, 0, 0))[:3]
    n = (new.release + (0, 0, 0))[:3]
    if n[0] != c[0]:
        return "major"
    if n[1] != c[1]:
        return "minor"
    return "patch"


def _requires_list(info: dict) -> list[str]:
    raw = info.get("requires_dist") or []
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, str)]


def _latest(session, ecosystem: str, name: str) -> tuple[str | None, dict]:
    """The latest version, plus what the blocker check reads from that release:
    npm `peers` (its peerDependencies), PyPI `requires` (its requires_dist)."""
    try:
        if ecosystem == "npm":
            body = get_json(session, NPM.format(name=name.replace("/", "%2F")))
            peers = body.get("peerDependencies")
            return str(body["version"]), {
                "peers": peers if isinstance(peers, dict) else {}
            }
        info = get_json(session, PYPI.format(name=name))["info"]
        return str(info["version"]), {"requires": _requires_list(info)}
    except SourceError, KeyError, TypeError, AttributeError:
        return None, {}


# ------------------------------------------------------------------ npm blockers


def _lock_name(key: str) -> str:
    return key.rsplit("node_modules/", 1)[-1]


def npm_blockers(
    packages: dict, direct: set[str], name: str, current: str, latest: str
) -> list[dict]:
    """Installed packages whose `peerDependencies[name]` accepts `current` but
    not `latest`. A range excluding `current` too is a conflict the project already
    lives with, not a new block; a range not understood is never blocking."""
    installed = {_lock_name(k) for k in packages if "node_modules/" in k}
    found: dict[tuple[str, str, str], None] = {}
    for key, pkg in packages.items():
        if "node_modules/" not in key or not isinstance(pkg, dict):
            continue
        peers = pkg.get("peerDependencies")
        if not isinstance(peers, dict) or not isinstance(peers.get(name), str):
            continue
        rng = peers[name]
        meta = pkg.get("peerDependenciesMeta")
        meta = meta.get(name) if isinstance(meta, dict) else None
        if isinstance(meta, dict) and meta.get("optional") and name not in installed:
            continue
        accepts_latest = npm_semver.satisfies(latest, rng)
        accepts_current = npm_semver.satisfies(current, rng)
        if accepts_latest is None or accepts_current is None:
            log.info("peer range not understood: %s -> %s %r", key, name, rng)
            continue
        if accepts_current and not accepts_latest:
            found[(_lock_name(key), str(pkg.get("version") or ""), rng)] = None
    ordered = sorted(found, key=lambda b: (b[0] not in direct, b[0], b[1]))
    return [
        {"kind": "package", "package": p, "version": v, "range": r}
        for p, v, r in ordered[:MAX_BLOCKERS]
    ]


def _npm_still_blocks(peers: dict, name: str, latest: str) -> bool:
    rng = peers.get(name)
    if not isinstance(rng, str):
        return False  # the newer release no longer declares the peer
    return npm_semver.satisfies(latest, rng) is not True


# --------------------------------------------------------------- Python blockers


def python_constraint(requires: list[str], name: str) -> SpecifierSet | None:
    """The specifier `requires` puts on `name` on the box, None when none applies
    (no requirement, no version constraint, extras-only or marker-excluded)."""
    for raw in requires:
        try:
            req = Requirement(raw)
        except InvalidRequirement:
            continue
        if _norm(req.name) != name or not str(req.specifier):
            continue
        try:
            if req.marker is not None and not req.marker.evaluate(MARKER_ENV):
                continue
        except InvalidMarker, KeyError, ValueError:
            continue
        return req.specifier
    return None


def _excludes(spec: SpecifierSet, version: str) -> bool | None:
    try:
        return not spec.contains(Version(version), prereleases=True)
    except InvalidVersion:
        return None


class RequiresFetcher:
    """`requires_dist` of installed releases, one PyPI call per (name, version)
    the cache does not hold yet. Failures are not cached (retried next time)."""

    def __init__(self, session, cache=None) -> None:
        self.session = session
        self.cache = cache
        self.fetched = 0
        self._memo: dict[tuple[str, str], list[str] | None] = {}

    def get(self, name: str, version: str) -> list[str] | None:
        key = (name, version)
        if key in self._memo:
            return self._memo[key]
        cache_key = f"deps:pypi-requires:{name}:{version}"
        value = self.cache.get(cache_key) if self.cache is not None else None
        if value is None:
            self.fetched += 1
            try:
                body = get_json(
                    self.session,
                    PYPI_RELEASE.format(name=quote(name), version=quote(version)),
                )
                value = _requires_list(body["info"])
            except SourceError, KeyError, TypeError, AttributeError:
                value = None
            else:
                if self.cache is not None:
                    self.cache.set(cache_key, value, timeout=None)
        self._memo[key] = value
        return value


def python_blockers(
    pins: dict[str, str],
    requires: RequiresFetcher,
    name: str,
    current: str,
    latest: str,
) -> list[dict]:
    """Installed releases whose requirement on `name` accepts `current` but not
    `latest` (same rule as npm)."""
    found: list[dict] = []
    for pkg, version in sorted(pins.items()):
        if pkg == name:
            continue
        spec = python_constraint(requires.get(pkg, version) or [], name)
        if spec is None:
            continue
        if _excludes(spec, current) is False and _excludes(spec, latest) is True:
            found.append(
                {
                    "kind": "package",
                    "package": pkg,
                    "version": version,
                    "range": str(spec),
                }
            )
            if len(found) >= MAX_BLOCKERS:
                break
    return found


def _python_still_blocks(requires: list[str], name: str, latest: str) -> bool:
    spec = python_constraint(requires, name)
    return spec is not None and _excludes(spec, latest) is not False


# ------------------------------------------------------------ declared holds

# pip's comment rule: `#` at the start of the line or after whitespace.
_COMMENT = re.compile(r"(?:^|\s)#(.*)$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩]")
_CONTINUATION = re.compile(r"\\\r?\n")


def clean_note(text: str) -> str:
    """One plain line of at most NOTE_MAX chars (control and bidi chars dropped)."""
    text = " ".join(_CONTROL.sub(" ", text).split())
    if len(text) > NOTE_MAX:
        text = text[: NOTE_MAX - 1].rstrip() + "…"
    return text


def _pick_comment(same_line: str, above: str) -> str:
    above = above.strip()
    for text in (same_line, above):
        if text.lower().startswith("hold"):
            return text
    return same_line or above


def parse_requirements_in(text: str) -> dict[str, tuple[SpecifierSet, str]]:
    """`requirements.in` -> {normalised name: (specifier, comment)}.

    The comment is the one on the requirement's line or on a comment-only line
    right above it, a `hold...` one first (`# hold: <why>, revisit <date>` above a
    line that already has its own comment), else the same-line one. Options
    (`-r`, `-c`, `--index-url`...), URLs and lines that do not parse are
    skipped; a package listed twice gets both specifiers.
    """
    out: dict[str, tuple[SpecifierSet, str]] = {}
    above = ""
    for raw in _CONTINUATION.sub(" ", text).splitlines():
        line = raw.strip()
        if not line:
            above = ""
            continue
        if line.startswith("#"):
            above = line[1:]
            continue
        m = _COMMENT.search(line)
        body = line[: m.start()].strip() if m else line
        previous, above = above, ""
        if not body or body.startswith("-"):
            continue
        note = clean_note(_pick_comment((m.group(1) if m else "").strip(), previous))
        _add_requirement(out, body, note)
    return out


#: A requirement string alone on its line of a TOML array, then an optional comment.
_TOML_ITEM = re.compile(r"""^\s*(["'])(.+?)\1\s*,?\s*(?:#(.*))?$""")


def _pyproject_notes(text: str) -> dict[str, str]:
    """{requirement string: comment} for the one-per-line array items of a
    pyproject: same-line comment or the comment block right above, read from the
    first `hold...` line of that block when there is one."""
    notes: dict[str, str] = {}
    block: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            block.append(line[1:].strip())
            continue
        m = _TOML_ITEM.match(line)
        if m:
            start = next(
                (i for i, c in enumerate(block) if c.lower().startswith("hold")), 0
            )
            above = " ".join(block[start:])
            notes.setdefault(
                m.group(2), _pick_comment((m.group(3) or "").strip(), above)
            )
        block = []
    return notes


def parse_pyproject(text: str) -> dict[str, tuple[SpecifierSet, str]]:
    """`pyproject.toml` -> {normalised name: (specifier, comment)}, from
    `[project] dependencies` and `optional-dependencies` (what `uv pip compile
    pyproject.toml` locks). Unparsable TOML or entries give no holds."""
    try:
        project = tomllib.loads(text).get("project") or {}
    except tomllib.TOMLDecodeError:
        log.warning("pyproject.toml unreadable: no holds")
        return {}
    if not isinstance(project, dict):
        return {}
    lists = [project.get("dependencies") or []]
    optional = project.get("optional-dependencies") or {}
    if isinstance(optional, dict):
        lists.extend(optional.values())
    notes = _pyproject_notes(text)
    out: dict[str, tuple[SpecifierSet, str]] = {}
    for entries in lists:
        if not isinstance(entries, list):
            continue
        for body in entries:
            if isinstance(body, str):
                _add_requirement(out, body, clean_note(notes.get(body, "")))
    return out


def _add_requirement(
    out: dict[str, tuple[SpecifierSet, str]], body: str, note: str
) -> None:
    """Merge one requirement into `out`; a package listed twice gets both
    specifiers. URLs and lines that do not parse are skipped."""
    try:
        req = Requirement(body)
    except InvalidRequirement:
        return
    if req.url:
        return
    name = _norm(req.name)
    if name in out:
        spec, old = out[name]
        out[name] = (spec & req.specifier, old or note)
    else:
        out[name] = (req.specifier, note)


def _caps(spec: Specifier) -> bool:
    return spec.operator in _UPPER_OPS or (
        spec.operator == "==" and spec.version.endswith(".*")
    )


def declared_hold(spec: SpecifierSet, latest: str) -> str | None:
    """The upper-bound specifiers of `spec` that exclude `latest` (e.g. "<3"),
    None when none does. `>`/`>=` and exact `==X.Y.Z` pins never hold."""
    try:
        version = Version(latest)
    except InvalidVersion:
        return None
    excluding = [
        str(s)
        for s in sorted(spec, key=str)
        if _caps(s) and not s.contains(version, prereleases=True)
    ]
    return ",".join(excluding) or None


def requirements_in_paths(lock_path: str) -> list[str]:
    """Where a lock's sources may live: the sibling `requirements.in`, plus
    `requirements/base.in` for the `requirements/` directory layout."""
    folder = lock_path.rpartition("/")[0]
    prefix = f"{folder}/" if folder else ""
    paths = [f"{prefix}requirements.in"]
    if folder.rsplit("/", 1)[-1] == "requirements":
        paths.append(f"{prefix}base.in")
    return paths


def hold_source_paths(lock_path: str) -> list[str]:
    """`requirements_in_paths`, then the sibling `pyproject.toml` (projects that
    lock with `uv pip compile pyproject.toml`)."""
    folder = lock_path.rpartition("/")[0]
    prefix = f"{folder}/" if folder else ""
    return [*requirements_in_paths(lock_path), f"{prefix}pyproject.toml"]


def parse_hold_source(path: str, text: str) -> dict[str, tuple[SpecifierSet, str]]:
    if path.rsplit("/", 1)[-1] == "pyproject.toml":
        return parse_pyproject(text)
    return parse_requirements_in(text)


def _fetch_requirements_in(gh, repo: str, lock_path: str) -> tuple[str, str] | None:
    """(path, text) of the first hold source found on main, None when there is none."""
    for path in hold_source_paths(lock_path):
        resp = get_response(
            gh,
            f"{API}/repos/{repo}/contents/{quote(path, safe='/')}",
            params={"ref": "main"},
            allow_404=True,
        )
        if resp is not None:
            return path, resp.text
    return None


def _hold(
    holds: dict[str, tuple[SpecifierSet, str]], source: str, name: str, latest: str
) -> dict | None:
    entry = holds.get(name)
    rng = declared_hold(entry[0], latest) if entry else None
    if entry is None or rng is None:
        return None
    return {
        "kind": "hold",
        "package": source.rsplit("/", 1)[-1],
        "version": "",
        "range": rng,
        "note": entry[1],
        "newer": None,
    }


def _add_blockers(
    outdated: list[dict],
    text: str,
    npm: bool,
    pins: dict[str, str],
    manifests: dict[str, tuple[str, dict]],
    session,
    cache,
    ref: str,
    holds: dict[str, tuple[SpecifierSet, str]] | None = None,
    holds_source: str = "requirements.in",
) -> None:
    """Set `blocked_by` on the outdated items an installed package holds back,
    a declared hold (`holds`, Python only) first.

    Each blocker's `newer` is its latest version when that release no longer
    blocks (known only for packages the collection checked), else None.
    """
    candidates = [o for o in outdated if o["latest"] and o["kind"] != "unknown"]
    if not candidates:
        return
    packages: dict = {}
    direct: set[str] = set()
    requires = RequiresFetcher(session, cache)
    if npm:
        packages = json.loads(text).get("packages") or {}
        root = packages.get("", {})
        direct = {*root.get("dependencies", {}), *root.get("devDependencies", {})}
    for item in candidates:
        name, current, latest = item["name"], item["current"], item["latest"]
        if npm:
            found = npm_blockers(packages, direct, name, current, latest)
        else:
            found = python_blockers(pins, requires, name, current, latest)
        for blocker in found:
            newer = manifests.get(blocker["package"])
            unblocks = False
            if newer and bump_kind(blocker["version"], newer[0]) != "none":
                if npm:
                    still = _npm_still_blocks(newer[1].get("peers") or {}, name, latest)
                else:
                    still = _python_still_blocks(
                        newer[1].get("requires") or [], name, latest
                    )
                unblocks = not still
            blocker["newer"] = newer[0] if unblocks else None
        hold = None if npm else _hold(holds or {}, holds_source, name, latest)
        if hold is not None:
            found.insert(0, hold)
        if found:
            item["blocked_by"] = found
    if requires.fetched:
        log.info("%s: %d PyPI release metadata fetched", ref, requires.fetched)


def collect_lock(ref: str, *, token: str, requires_cache=None) -> dict:
    """`requires_cache` (get/set with `timeout=None`, e.g. Django's cache) keeps
    PyPI release metadata across collections; None fetches it every time."""
    repo, _, path = ref.partition(":")
    gh = _session(token)
    gh.headers["Accept"] = "application/vnd.github.raw"
    try:
        resp = get_response(
            gh,
            f"{API}/repos/{repo}/contents/{quote(path, safe='/')}",
            params={"ref": "main"},
            allow_404=True,
        )
    finally:
        gh.close()
    if resp is None:
        raise SourceError(f"lock not found: {ref}")

    npm = path.rsplit("/", 1)[-1] == "package-lock.json"
    try:
        pins = parse_npm_lock(resp.text) if npm else parse_python_lock(resp.text)
    except ValueError as exc:
        raise SourceError(f"lock unreadable: {ref}") from exc
    if not pins:
        # An empty result means the format was not understood, not "all fresh".
        raise SourceError(f"no pins parsed: {ref}")
    holds: dict[str, tuple[SpecifierSet, str]] = {}
    holds_source = "requirements.in"
    if not npm:
        # One more GitHub call: the holds the project declares. No file, no holds;
        # a failed read fails the lock rather than turn held items orange.
        gh = _session(token)
        gh.headers["Accept"] = "application/vnd.github.raw"
        try:
            found_in = _fetch_requirements_in(gh, repo, path)
        finally:
            gh.close()
        if found_in is not None:
            holds_source, text = found_in
            holds = parse_hold_source(holds_source, text)
    items = list(pins.items())
    if len(items) > MAX_PACKAGES:
        log.warning(
            "%s: %d packages, checking only the first %d", ref, len(items), MAX_PACKAGES
        )
        items = items[:MAX_PACKAGES]

    ecosystem = "npm" if npm else "python"
    outdated = []
    manifests: dict[str, tuple[str, dict]] = {}
    with requests.Session() as reg:  # no GitHub token here
        for name, current in items:
            latest, manifest = _latest(reg, ecosystem, name)
            if latest is not None:
                manifests[name] = (latest, manifest)
            kind = "unknown" if latest is None else bump_kind(current, latest)
            if kind != "none":
                outdated.append(
                    {"name": name, "current": current, "latest": latest, "kind": kind}
                )
        _add_blockers(
            outdated,
            resp.text,
            npm,
            pins,
            manifests,
            reg,
            requires_cache,
            ref,
            holds,
            holds_source,
        )
    return {"ecosystem": ecosystem, "outdated": outdated, "checked": len(items)}
