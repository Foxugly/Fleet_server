import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from apps.collect.sources import box
from apps.collect.sources.http import SourceError

FIX = Path(__file__).parent / "fixtures" / "box"


def fx(name):
    return (FIX / name).read_text()


def test_parse_apt_upgradable():
    items = box.parse_apt_upgradable(fx("apt_upgradable.txt"))
    assert len(items) == 11
    assert items[0] == {
        "package": "libnss-systemd",
        "version": "255.4-1ubuntu8.19",
        "origin": "noble-updates",
        "security": False,
        "from_version": "255.4-1ubuntu8.18",
    }
    ssl = items[-1]
    assert ssl["package"] == "openssl"
    assert ssl["origin"] == "noble-updates,noble-security"
    assert ssl["security"] is True
    assert sum(i["security"] for i in items) == 1


def test_parse_apt_empty():
    assert box.parse_apt_upgradable("Listing...\n") == []


def test_parse_failed_units():
    assert box.parse_failed_units(fx("failed_units.txt")) == [
        "fabric-asgi.service",
        "nginx.service",
    ]
    assert box.parse_failed_units(fx("failed_units_empty.txt")) == []


def test_parse_df():
    assert box.parse_df(fx("df.txt")) == {"mount": "/", "used_pct": 44}


def test_parse_df_no_root_row():
    with pytest.raises(SourceError):
        box.parse_df("Filesystem 1024-blocks Used Available Capacity Mounted on\n")


def test_parse_meminfo():
    assert box.parse_meminfo(fx("meminfo.txt")) == {
        "total_mb": 7816,
        "available_mb": 3882,
    }


def test_collect_box_non_linux_raises(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(SourceError, match="only runs on Linux"):
        box.collect_box()


def _fake_run(outputs, calls):
    def run(cmd, **kw):
        calls.append((cmd, kw))
        code, out, err = outputs[cmd[0]]
        return SimpleNamespace(returncode=code, stdout=out, stderr=err)

    return run


def _patch(monkeypatch, tmp_path, outputs, calls):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "run", _fake_run(outputs, calls))
    mem = tmp_path / "meminfo"
    mem.write_text(fx("meminfo.txt"))
    monkeypatch.setattr(box, "MEMINFO", mem)
    monkeypatch.setattr(box, "REBOOT_REQUIRED", tmp_path / "reboot-required")
    monkeypatch.setattr(box, "REBOOT_PKGS", tmp_path / "reboot-required.pkgs")


def _ok():
    return {
        "apt": (0, fx("apt_upgradable.txt"), ""),
        "systemctl": (0, fx("failed_units.txt"), ""),
        "df": (0, fx("df.txt"), ""),
    }


def test_collect_box(monkeypatch, tmp_path):
    calls = []
    _patch(monkeypatch, tmp_path, _ok(), calls)
    (tmp_path / "reboot-required").write_text("*** System restart required ***\n")
    (tmp_path / "reboot-required.pkgs").write_text("linux-image-6.8\nlibc6\n")
    data = box.collect_box()
    assert len(data["apt"]) == 11
    assert data["reboot_required"] is True
    assert data["reboot_pkgs"] == ["linux-image-6.8", "libc6"]
    assert data["failed_units"] == ["fabric-asgi.service", "nginx.service"]
    assert data["disk"]["used_pct"] == 44
    assert data["memory"]["total_mb"] == 7816
    assert len(calls) == 3
    for cmd, kw in calls:
        assert isinstance(cmd, list)
        assert kw["env"]["LC_ALL"] == "C"
        assert kw["timeout"] == 60
        assert not kw.get("shell")


def test_collect_box_no_reboot(monkeypatch, tmp_path):
    _patch(monkeypatch, tmp_path, _ok(), [])
    data = box.collect_box()
    assert data["reboot_required"] is False
    assert data["reboot_pkgs"] == []


def test_collect_box_command_failure(monkeypatch, tmp_path):
    outputs = _ok()
    outputs["apt"] = (100, "", "E: " + "x" * 500)
    _patch(monkeypatch, tmp_path, outputs, [])
    with pytest.raises(SourceError, match="apt exited 100") as ei:
        box.collect_box()
    assert len(str(ei.value)) < 260


def test_child_env_is_minimal(monkeypatch, tmp_path):
    monkeypatch.setenv("FLEET_GITHUB_READ_TOKEN", "ghp_secret")
    monkeypatch.setenv("DB_PASSWORD", "dbsecret")
    calls = []
    _patch(monkeypatch, tmp_path, _ok(), calls)
    box.collect_box()
    for _cmd, kw in calls:
        assert set(kw["env"]) == {"PATH", "LC_ALL"}


def test_stderr_is_logged_not_stored(monkeypatch, tmp_path, caplog):
    outputs = _ok()
    outputs["apt"] = (100, "", "E: secret-detail")
    _patch(monkeypatch, tmp_path, outputs, [])
    with caplog.at_level("WARNING", logger="apps.collect.sources.box"):
        with pytest.raises(SourceError) as ei:
            box.collect_box()
    assert str(ei.value) == "apt exited 100"
    assert "secret-detail" in caplog.text
