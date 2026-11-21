"""Dependency freshness: read lock files from GitHub main, compare to PyPI / npm."""

import json
import logging
import re

import requests
from packaging.version import InvalidVersion, Version

from .github import API, _session
from .http import SourceError, get_json, get_response

log = logging.getLogger(__name__)

MAX_PACKAGES = 300
PYPI = "https://pypi.org/pypi/{name}/json"
NPM = "https://registry.npmjs.org/{name}/latest"
_REQ = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;\x5c]+)")


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


def _latest(session, ecosystem: str, name: str) -> str | None:
    try:
        if ecosystem == "npm":
            body = get_json(session, NPM.format(name=name.replace("/", "%2F")))
            return str(body["version"])
        return str(get_json(session, PYPI.format(name=name))["info"]["version"])
    except (SourceError, KeyError, TypeError):
        return None


def collect_lock(ref: str, *, token: str) -> dict:
    repo, _, path = ref.partition(":")
    gh = _session(token)
    gh.headers["Accept"] = "application/vnd.github.raw"
    try:
        resp = get_response(
            gh,
            f"{API}/repos/{repo}/contents/{path}",
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
    items = list(pins.items())
    if len(items) > MAX_PACKAGES:
        log.warning(
            "%s: %d packages, checking only the first %d", ref, len(items), MAX_PACKAGES
        )
        items = items[:MAX_PACKAGES]

    ecosystem = "npm" if npm else "python"
    outdated = []
    with requests.Session() as reg:  # no GitHub token here
        for name, current in items:
            latest = _latest(reg, ecosystem, name)
            kind = "unknown" if latest is None else bump_kind(current, latest)
            if kind != "none":
                outdated.append(
                    {"name": name, "current": current, "latest": latest, "kind": kind}
                )
    return {"ecosystem": ecosystem, "outdated": outdated, "checked": len(items)}
