"""A small npm semver range matcher (node-semver semantics, non-loose mode).

Only what reading `peerDependencies` needs: `satisfies(version, range)`. It handles
`||`, space-ANDed comparators (`>= <= > < =`), `^`, `~`, x-ranges (`22.x`, `*`, ``),
hyphen ranges (`1.2 - 2.3`) and partial versions. Anything else (`npm:` aliases,
URLs, tags, `workspace:`) is not a range: `satisfies` then returns None, and the
caller must treat "unknown" as "not blocking".
"""

from __future__ import annotations

import re

_NUM = r"0|[1-9]\d*"
_ID = r"[0-9A-Za-z-]+"
_PRE = rf"(?:-({_ID}(?:\.{_ID})*))?"
_BUILD = rf"(?:\+{_ID}(?:\.{_ID})*)?"
_VERSION = re.compile(rf"^v?=?\s*({_NUM})\.({_NUM})\.({_NUM}){_PRE}{_BUILD}$")
_XR = r"x|X|\*|" + _NUM
_PARTIAL = re.compile(rf"^v?({_XR})(?:\.({_XR})(?:\.({_XR}){_PRE}{_BUILD})?)?$")
_COMPARATOR = re.compile(r"^(<=|>=|<|>|=|\^|~>|~)?\s*(.*)$")

Version = tuple[int, int, int, tuple[int | str, ...]]
Comparator = tuple[str, Version]  # op in < <= > >= = ; a "" op means "any"

ANY: Comparator = ("", (0, 0, 0, ()))


class RangeError(ValueError):
    """The range is not something this matcher understands."""


def _pre(raw: str | None) -> tuple[int | str, ...]:
    if not raw:
        return ()
    return tuple(int(p) if p.isdigit() else p for p in raw.split("."))


def parse_version(text: str) -> Version | None:
    m = _VERSION.match(text.strip())
    if not m:
        return None
    return (int(m[1]), int(m[2]), int(m[3]), _pre(m[4]))


def _cmp_pre(a: tuple, b: tuple) -> int:
    if a == b:
        return 0
    if not a:
        return 1  # a release is greater than any of its prereleases
    if not b:
        return -1
    for x, y in zip(a, b, strict=False):
        if x == y:
            continue
        if isinstance(x, int) and isinstance(y, int):
            return -1 if x < y else 1
        if isinstance(x, int):
            return -1  # numeric identifiers sort before alphanumeric ones
        if isinstance(y, int):
            return 1
        return -1 if x < y else 1
    return -1 if len(a) < len(b) else 1


def compare(a: Version, b: Version) -> int:
    if a[:3] != b[:3]:
        return -1 if a[:3] < b[:3] else 1
    return _cmp_pre(a[3], b[3])


def _is_x(part: str | None) -> bool:
    return part is None or part in ("x", "X", "*")


def _partial(text: str) -> tuple[str | None, str | None, str | None, tuple]:
    m = _PARTIAL.match(text)
    if not m:
        raise RangeError(text)
    major, minor, patch = m[1], m[2], m[3]
    return (
        None if _is_x(major) else major,
        None if _is_x(minor) else minor,
        None if _is_x(patch) else patch,
        _pre(m[4]),
    )


def _v(major: int, minor: int = 0, patch: int = 0, pre: tuple = ()) -> Version:
    return (major, minor, patch, pre)


def _lower_bound_zero() -> Version:
    return _v(0, 0, 0, (0,))  # "-0": the lowest possible prerelease


def _upper(major: int, minor: int = 0, patch: int = 0) -> Version:
    return _v(major, minor, patch, (0,))  # "<X.Y.Z-0" excludes X.Y.Z prereleases


def _tilde(text: str) -> list[Comparator]:
    major, minor, patch, pre = _partial(text)
    if major is None:
        return [ANY]
    ma = int(major)
    if minor is None:
        return [(">=", _v(ma)), ("<", _upper(ma + 1))]
    mi = int(minor)
    if patch is None:
        return [(">=", _v(ma, mi)), ("<", _upper(ma, mi + 1))]
    return [(">=", _v(ma, mi, int(patch), pre)), ("<", _upper(ma, mi + 1))]


def _caret(text: str) -> list[Comparator]:
    major, minor, patch, pre = _partial(text)
    if major is None:
        return [ANY]
    ma = int(major)
    if minor is None:
        return [(">=", _v(ma)), ("<", _upper(ma + 1))]
    mi = int(minor)
    if patch is None:
        if ma == 0:
            return [(">=", _v(0, mi)), ("<", _upper(0, mi + 1))]
        return [(">=", _v(ma, mi)), ("<", _upper(ma + 1))]
    pa = int(patch)
    low = (">=", _v(ma, mi, pa, pre))
    if ma != 0:
        return [low, ("<", _upper(ma + 1))]
    if mi != 0:
        return [low, ("<", _upper(0, mi + 1))]
    return [low, ("<", _upper(0, 0, pa + 1))]


def _primitive(op: str, text: str) -> list[Comparator]:
    """`op` in "", =, <, <=, >, >= applied to a possibly partial version."""
    major, minor, patch, pre = _partial(text)
    if major is None:
        # `<*` and `>*` match nothing; every other operator on `*` matches anything.
        return [("<", _lower_bound_zero())] if op in ("<", ">") else [ANY]
    ma = int(major)
    if minor is not None and patch is not None:
        return [(op or "=", _v(ma, int(minor), int(patch), pre))]
    mi = None if minor is None else int(minor)
    # An x-range: [floor, ceiling) of the partial version.
    floor = _v(ma, mi or 0)
    ceiling = _upper(ma + 1) if mi is None else _upper(ma, mi + 1)
    if op in ("", "="):
        return [(">=", floor), ("<", ceiling)]
    if op == ">":
        return [(">=", ceiling[:3] + ((),))]
    if op == ">=":
        return [(">=", floor)]
    if op == "<":
        return [("<", _v(*floor[:3], (0,)))]
    return [("<", ceiling)]  # <=


def _hyphen(low: str, high: str) -> list[Comparator]:
    out: list[Comparator] = []
    lmaj, lmin, lpat, lpre = _partial(low)
    if lmaj is not None:
        out.append((">=", _v(int(lmaj), int(lmin or 0), int(lpat or 0), lpre)))
    hmaj, hmin, hpat, hpre = _partial(high)
    if hmaj is not None:
        if hmin is None:
            out.append(("<", _upper(int(hmaj) + 1)))
        elif hpat is None:
            out.append(("<", _upper(int(hmaj), int(hmin) + 1)))
        else:
            out.append(("<=", _v(int(hmaj), int(hmin), int(hpat), hpre)))
    return out or [ANY]


def _comparator_set(text: str) -> list[Comparator]:
    text = text.strip()
    hyphen = re.match(r"^(\S+)\s+-\s+(\S+)$", text)
    if hyphen:
        return _hyphen(hyphen[1], hyphen[2])
    # "> = 1.2"-style spacing between an operator and its version is allowed.
    text = re.sub(r"(<=|>=|<|>|=|\^|~>|~)\s+", r"\1", text)
    out: list[Comparator] = []
    for token in text.split():
        m = _COMPARATOR.match(token)
        op, rest = m[1] or "", m[2]
        if not rest:
            raise RangeError(token)
        if op == "^":
            out += _caret(rest)
        elif op in ("~", "~>"):
            out += _tilde(rest)
        else:
            out += _primitive(op, rest)
    return out or [ANY]


def parse_range(text: str) -> list[list[Comparator]]:
    """Raises RangeError when `text` is not an npm range."""
    if not isinstance(text, str) or len(text) > 256:
        raise RangeError(repr(text)[:40])
    return [_comparator_set(part) for part in text.split("||")]


def _test(comparator: Comparator, version: Version) -> bool:
    op, bound = comparator
    if not op:
        return True
    c = compare(version, bound)
    return {
        "=": c == 0,
        "<": c < 0,
        "<=": c <= 0,
        ">": c > 0,
        ">=": c >= 0,
    }[op]


def _test_set(comparators: list[Comparator], version: Version) -> bool:
    if not all(_test(c, version) for c in comparators):
        return False
    if version[3]:
        # A prerelease only matches a comparator set that names a prerelease of
        # the same major.minor.patch (node-semver's includePrerelease=false).
        return any(c[0] and c[1][3] and c[1][:3] == version[:3] for c in comparators)
    return True


def satisfies(version: str, range_: str) -> bool | None:
    """True/False, or None when either side cannot be parsed."""
    parsed = parse_version(version)
    if parsed is None:
        return None
    try:
        sets = parse_range(range_)
    except RangeError:
        return None
    return any(_test_set(s, parsed) for s in sets)
