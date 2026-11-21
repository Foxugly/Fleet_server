"""Box collector: reads the host's own state as an unprivileged user (Linux only)."""

import os
import subprocess
import sys
from pathlib import Path

from apps.collect.sources.http import SourceError

REBOOT_REQUIRED = Path("/var/run/reboot-required")
REBOOT_PKGS = Path("/var/run/reboot-required.pkgs")
MEMINFO = Path("/proc/meminfo")
TIMEOUT = 60


def parse_apt_upgradable(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("Listing") or "/" not in line:
            continue
        head, _, rest = line.partition(" ")
        package, _, origin = head.partition("/")
        fields = rest.split()
        if not package or not fields:
            continue
        item = {
            "package": package,
            "version": fields[0],
            "origin": origin,
            "security": "-security" in origin,
        }
        if "from:" in rest:
            item["from_version"] = rest.split("from:", 1)[1].strip(" ]")
        out.append(item)
    return out


def parse_failed_units(text: str) -> list[str]:
    return [ln.split()[0] for ln in text.splitlines() if ln.strip()]


def parse_df(text: str) -> dict:
    lines = [ln.split() for ln in text.splitlines() if ln.strip()]
    for cols in lines[1:]:
        if len(cols) >= 6 and cols[-1] == "/":
            return {"mount": "/", "used_pct": int(cols[-2].rstrip("%"))}
    raise SourceError("df output has no / row")


def parse_meminfo(text: str) -> dict:
    kb = {}
    for line in text.splitlines():
        key, _, val = line.partition(":")
        if key in ("MemTotal", "MemAvailable"):
            kb[key] = int(val.split()[0])
    if len(kb) != 2:
        raise SourceError("meminfo missing MemTotal/MemAvailable")
    return {
        "total_mb": kb["MemTotal"] // 1024,
        "available_mb": kb["MemAvailable"] // 1024,
    }


def _run(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(
            cmd,
            timeout=TIMEOUT,
            check=False,
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SourceError(f"{cmd[0]} failed: {type(exc).__name__}") from exc
    if proc.returncode != 0:
        raise SourceError(
            f"{cmd[0]} exited {proc.returncode}: {(proc.stderr or '')[:200]}"
        )
    return proc.stdout


def collect_box() -> dict:
    if not sys.platform.startswith("linux"):
        raise SourceError("box collection only runs on Linux")
    reboot_pkgs = []
    if REBOOT_PKGS.exists():
        reboot_pkgs = [
            ln.strip() for ln in REBOOT_PKGS.read_text().splitlines() if ln.strip()
        ]
    return {
        "apt": parse_apt_upgradable(_run(["apt", "list", "--upgradable"])),
        "reboot_required": REBOOT_REQUIRED.exists(),
        "reboot_pkgs": reboot_pkgs,
        "failed_units": parse_failed_units(
            _run(["systemctl", "--failed", "--no-legend", "--plain"])
        ),
        "disk": parse_df(_run(["df", "-P", "/"])),
        "memory": parse_meminfo(MEMINFO.read_text()),
    }
