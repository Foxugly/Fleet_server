"""Print, one per line, the arguments a systemd unit passes to `python -m <module>`.

    python deploy/unit_exec_args.py deploy/systemd/fleet-app-gunicorn.service gunicorn

CI feeds them to `python -m gunicorn --check-config`, and a test feeds them to
gunicorn's argparser: both read the unit itself, so the check cannot drift from
what systemd runs. Not installed on the box; CI and tests only.
"""

import shlex
import sys
from pathlib import Path


def exec_start(unit_text: str) -> str:
    """The ExecStart= value, continuation lines joined, comments ignored."""
    joined = []
    for line in unit_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("#", ";")):
            continue
        joined.append(line)
    text = "\n".join(joined).replace("\\\n", " ")
    values = [
        ln.split("=", 1)[1].strip()
        for ln in text.splitlines()
        if ln.strip().startswith("ExecStart=")
    ]
    if len(values) != 1:
        raise SystemExit(f"expected one ExecStart=, found {len(values)}")
    return values[0]


def module_args(unit_text: str, module: str) -> list[str]:
    words = shlex.split(exec_start(unit_text))
    for i in range(len(words) - 1):
        if words[i] == "-m" and words[i + 1] == module:
            return words[i + 2 :]
    raise SystemExit(f"ExecStart does not run `-m {module}`")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        raise SystemExit("usage: unit_exec_args.py <unit file> <module>")
    unit, module = argv[1], argv[2]
    print("\n".join(module_args(Path(unit).read_text(encoding="utf-8"), module)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
