import pytest

from apps.collect.sources.npm_semver import (
    RangeError,
    compare,
    parse_range,
    parse_version,
    satisfies,
)


@pytest.mark.parametrize(
    "version,rng,expected",
    [
        # Real-world peer ranges.
        ("6.0.3", ">=6.0 <6.1", True),  # @angular/compiler-cli 22 -> typescript
        ("7.0.2", ">=6.0 <6.1", False),
        ("6.1.0", ">=6.0 <6.1", False),
        ("6.1.0-beta", ">=6.0 <6.1", False),
        ("22.3.0", "^22.3.0", True),
        ("22.9.9", "^22.3.0", True),
        ("23.0.0", "^22.3.0", False),
        ("22.2.9", "^22.3.0", False),
        ("6.0.9", "~6.0.0", True),
        ("6.1.0", "~6.0.0", False),
        ("18.19.1", ">=18.19.1 || >=20.11.1", True),
        ("18.19.0", ">=18.19.1 || >=20.11.1", False),
        ("24.0.0", ">=18.19.1 || >=20.11.1", True),
        ("7.0.2", "*", True),
        ("7.0.2", "", True),
        ("7.8.2", "^6.5.3 || ^7.4.0", True),
        ("8.0.0", "^6.5.3 || ^7.4.0", False),
        ("19.0.0", "^18.0.0 || ^19.0.0", True),
        # x-ranges and partial versions.
        ("22.0.5", "22.x", True),
        ("23.0.0", "22.x", False),
        ("22.4.1", "22", True),
        ("22.4.1", "22.4", True),
        ("22.5.0", "22.4.x", False),
        ("1.2.3", "X", True),
        ("2.0.0", ">1", True),
        ("1.9.9", ">1", False),
        ("1.3.0", ">1.2", True),
        ("1.2.9", ">1.2", False),
        ("1.0.0", ">=1", True),
        ("1.1.9", "<1.2", True),
        ("1.2.0", "<1.2", False),
        ("1.2.9", "<=1.2", True),
        ("1.3.0", "<=1.2", False),
        ("1.9.0", "<=1", True),
        ("0.0.1", "<*", False),
        ("0.0.1", ">*", False),
        ("1.2.3", "=1.2.3", True),
        ("1.2.3", "v1.2.3", True),
        ("1.2.4", "1.2.3", False),
        ("1.2.0", "=1.2", True),
        ("1.2.5", ">= 1.2.3 < 1.3", True),  # spaces after operators
        # Tilde.
        ("1.2.9", "~1.2", True),
        ("1.3.0", "~1.2", False),
        ("1.9.0", "~1", True),
        ("2.0.0", "~1", False),
        ("1.2.4", "~>1.2.3", True),
        ("1.2.3", "~1.2.3-beta.2", True),
        ("1.2.3-beta.4", "~1.2.3-beta.2", True),
        ("1.2.4-beta.4", "~1.2.3-beta.2", False),
        ("5.0.0", "~*", True),
        # Caret.
        ("0.2.9", "^0.2.3", True),
        ("0.3.0", "^0.2.3", False),
        ("0.0.3", "^0.0.3", True),
        ("0.0.4", "^0.0.3", False),
        ("1.9.0", "^1.2.x", True),
        ("0.0.9", "^0.0.x", True),
        ("0.1.0", "^0.0", False),
        ("0.9.0", "^0.x", True),
        ("1.0.0", "^0.x", False),
        ("1.5.0", "^1", True),
        ("0.5.0", "^0.5", True),
        ("0.6.0", "^0.5", False),
        ("3.0.0", "^*", True),
        # Hyphen ranges.
        ("2.3.9", "1.2 - 2.3", True),
        ("2.4.0", "1.2 - 2.3", False),
        ("1.1.9", "1.2 - 2.3", False),
        ("2.3.4", "1.2.3 - 2.3.4", True),
        ("2.3.5", "1.2.3 - 2.3.4", False),
        ("2.9.0", "1.2.3 - 2", True),
        ("3.0.0", "1.2.3 - 2", False),
        ("0.0.0", "* - 2", True),
        ("9.0.0", "1 - *", True),
        # Prereleases.
        ("1.2.3-alpha.7", ">1.2.3-alpha.3", True),
        ("3.4.5-alpha.9", ">1.2.3-alpha.3", False),
        ("2.0.0-rc.1", "^1.0.0", False),
        ("1.0.0-rc.1", ">=1.0.0", False),
        ("1.2.3-beta", "1.2.3-beta", True),
        ("1.2.3-1", "<1.2.3-beta", True),  # numeric identifiers sort first
        ("1.2.3-beta.2", ">=1.2.3-beta.1 <1.2.4", True),
        ("1.2.3", "1.2.3+build.5", True),
    ],
)
def test_satisfies(version, rng, expected):
    assert satisfies(version, rng) is expected


@pytest.mark.parametrize(
    "rng",
    [
        "latest",
        "npm:typescript@6",
        "workspace:*",
        "https://example.com/x.tgz",
        "file:../x",
        "1.2.3.4",
        ">=",
        "x" * 300,
    ],
)
def test_unknown_ranges_are_none(rng):
    assert satisfies("1.2.3", rng) is None
    with pytest.raises(RangeError):
        parse_range(rng)


def test_bad_version_is_none():
    assert satisfies("not-a-version", "*") is None
    assert parse_version("1.2") is None
    assert parse_version(" =v1.2.3 ") is None  # "=v" is loose-mode only


def test_prerelease_ordering():
    def v(text):
        return parse_version(text)

    ordered = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
    ]
    for low, high in zip(ordered, ordered[1:], strict=False):
        assert compare(v(low), v(high)) == -1
        assert compare(v(high), v(low)) == 1
    assert compare(v("1.0.0"), v("1.0.0+meta")) == 0
