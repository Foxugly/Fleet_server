"""Behaviour of deploy/ssm-deploy.sh and deploy/fetch-env-from-ssm.sh.

The scripts run in bash against a temp tree, with their hard-coded paths
rewritten and the outside world (python3.12, aws, chown) stubbed on PATH.
Skipped when bash is absent. The env-fetch Python part also runs on its own.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")
# Git Bash on Windows: keep arguments such as /fleet/prod unconverted when bash
# calls the native Python. No effect elsewhere.
ENV = {**os.environ, "MSYS2_ARG_CONV_EXCL": "*"}


def _path(p: Path, flag: str) -> str:
    """Path as bash sees it: -m = C:/x (Windows) for files, -u = /c/x for PATH."""
    res = subprocess.run(
        [
            BASH,
            "-c",
            'if command -v cygpath >/dev/null 2>&1; then cygpath "$1" "$2";'
            ' else printf "%s" "$2"; fi',
            "_",
            flag,
            str(p),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def _write(path: Path, text: str, mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(mode)
    return path


def _stub(stubs: Path, name: str, body: str) -> None:
    _write(stubs / name, "#!/usr/bin/env bash\n" + body + "\n", 0o755)


def _run(stubs: Path, script: Path, *args: str, extra_env=None):
    return subprocess.run(
        [
            BASH,
            "-c",
            'PATH="$1:$PATH"; shift; exec bash "$@"',
            "_",
            _path(stubs, "-u"),
            _path(script, "-m"),
            *args,
        ],
        capture_output=True,
        text=True,
        env={**ENV, **(extra_env or {})},
    )


# --------------------------------------------------------------------------
# ssm-deploy.sh: the literal .env loader
# --------------------------------------------------------------------------


def _load(tmp_path: Path, env_text: str, names: str = "A B C"):
    env_file = _write(tmp_path / "env", env_text)
    return subprocess.run(
        [
            BASH,
            "-c",
            'source "$1"; load_env "$2";'
            ' for n in $3; do printf "%s=[%s]\\n" "$n" "${!n}"; done',
            "_",
            _path(DEPLOY / "ssm-deploy.sh", "-m"),
            _path(env_file, "-m"),
            names,
        ],
        capture_output=True,
        text=True,
        env=ENV,
    )


@needs_bash
def test_loader_keeps_everything_after_the_first_equal_sign(tmp_path):
    res = _load(tmp_path, "A=b=\n\nB=x==\nC=p=q")  # no final newline on purpose
    assert res.returncode == 0, res.stderr
    assert res.stdout.splitlines() == ["A=[b=]", "B=[x==]", "C=[p=q]"]


@needs_bash
@pytest.mark.parametrize(
    "line",
    [
        "lower=1",
        "1A=x",
        "A-B=x",
        "PATH=/tmp",
        "LD_PRELOAD=/tmp/x.so",
        "PYTHONPATH=/tmp",
        "BASH_ENV=/tmp/x",
        "ENV=/tmp/x",
        "IFS=x",
        "HOME=/tmp",
        "SHELL=/bin/sh",
        "A=it's",
        'A=say "hi"',
        "A=back\\slash",
        "NOEQUALSIGN",
    ],
)
def test_loader_refuses_bad_keys_and_values(tmp_path, line):
    res = _load(tmp_path, f"OK=1\n{line}\n", names="OK")
    assert res.returncode != 0
    assert "ERROR" in res.stderr


# --------------------------------------------------------------------------
# ssm-deploy.sh: fresh venv, check before migrate, swap only on success
# --------------------------------------------------------------------------

VENV_PYTHON = """\
echo "$0 $*|A=${A-}" >> "@LOG@"
if [ -n "${FAIL_ON-}" ] && [[ "$*" == *"$FAIL_ON"* ]]; then
  echo "stub failure on $*" >&2
  exit 1
fi
exit 0"""

PYTHON312 = """\
[ "$1" = -m ] && [ "$2" = venv ] || exit 2
mkdir -p "$3/bin"
echo new > "$3/marker"
cp "@STUBS@/venv-python" "$3/bin/python"
chmod +x "$3/bin/python\""""


@pytest.fixture
def deploy_tree(tmp_path):
    app = tmp_path / "app"
    stubs = tmp_path / "stubs"
    log = tmp_path / "log"
    log.write_text("", encoding="utf-8")
    # Running state: old code, old venv, manifest of the previous bundle.
    _write(app / ".venv" / "marker", "old\n")
    for name, text in {
        "RELEASE": "old\n",
        "manage.py": "",
        "requirements.txt": "",
        "oldmod.py": "",
    }.items():
        _write(app / name, text)
    _write(
        app / ".bundle-manifest", "RELEASE\nmanage.py\nrequirements.txt\noldmod.py\n"
    )
    _write(app / "keepme", "not from a bundle\n")
    # New bundle.
    new = app / ".bundle-new"
    for name, text in {
        "RELEASE": "sha2\n",
        "manage.py": "",
        "requirements.txt": "",
        "newmod.py": "",
    }.items():
        _write(new / name, text)
    env_file = _write(tmp_path / "fleet.env", "A=b=\nSTATE=PROD\n")
    _stub(stubs, "venv-python", VENV_PYTHON.replace("@LOG@", _path(log, "-m")))
    _stub(stubs, "python3.12", PYTHON312.replace("@STUBS@", _path(stubs, "-m")))
    text = (DEPLOY / "ssm-deploy.sh").read_text(encoding="utf-8")
    text = text.replace(
        "\nAPP=/srv/fleet/Fleet_server\n", f'\nAPP="{_path(app, "-m")}"\n'
    )
    text = text.replace(
        "\nENV_FILE=/run/fleet/.env\n", f'\nENV_FILE="{_path(env_file, "-m")}"\n'
    )
    script = _write(tmp_path / "ssm-deploy.sh", text)
    bundle_arg = _path(app, "-m") + "/.bundle-new"

    def run(*extra_args, fail_on=None):
        env = {"FAIL_ON": fail_on} if fail_on else {}
        return _run(stubs, script, bundle_arg, *extra_args, extra_env=env)

    def calls():
        return [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln]

    return app, run, calls, env_file


def _untouched(app: Path) -> None:
    assert (app / ".venv" / "marker").read_text().strip() == "old"
    assert not (app / ".venv.new").exists()
    assert not (app / ".venv.prev").exists()
    assert (app / "RELEASE").read_text().strip() == "old"
    assert (app / "oldmod.py").exists()
    assert not (app / "newmod.py").exists()


@needs_bash
def test_deploy_checks_before_migrating_then_swaps_code_and_venv(deploy_tree):
    app, run, calls, _ = deploy_tree
    res = run("sha2")
    assert res.returncode == 0, res.stderr
    steps = calls()
    order = [
        "-m pip install",
        "-m pip check",
        "manage.py check --deploy",
        "manage.py migrate",
    ]
    positions = [next(i for i, c in enumerate(steps) if s in c) for s in order]
    assert positions == sorted(positions), steps
    assert len(steps) == 4, steps
    # Every step uses the fresh venv; the manage.py steps see the loaded .env.
    assert all("/.venv.new/bin/python " in c for c in steps), steps
    assert all(c.endswith("|A=b=") for c in steps if "manage.py" in c), steps
    # Swapped together.
    assert (app / ".venv" / "marker").read_text().strip() == "new"
    assert (app / ".venv.prev" / "marker").read_text().strip() == "old"
    assert not (app / ".venv.new").exists()
    assert (app / "RELEASE").read_text().strip() == "sha2"
    assert (app / "newmod.py").exists()
    assert not (app / "oldmod.py").exists()
    assert (app / "keepme").exists()
    assert not (app / ".bundle-new").exists()


@needs_bash
@pytest.mark.parametrize(
    "fail_on",
    ["-m pip install", "-m pip check", "manage.py check", "manage.py migrate"],
)
def test_a_failed_step_leaves_code_and_venv_untouched(deploy_tree, fail_on):
    app, run, calls, _ = deploy_tree
    res = run("sha2", fail_on=fail_on)
    assert res.returncode != 0
    _untouched(app)
    if fail_on != "manage.py migrate":
        assert not any("manage.py migrate" in c for c in calls())


@needs_bash
def test_a_refused_env_key_stops_before_check_and_migrate(deploy_tree):
    app, run, calls, env_file = deploy_tree
    env_file.write_text("A=1\nLD_PRELOAD=/tmp/x.so\n", encoding="utf-8", newline="\n")
    res = run("sha2")
    assert res.returncode != 0
    assert "LD_PRELOAD" in res.stderr
    assert not any("manage.py" in c for c in calls())
    _untouched(app)


@needs_bash
def test_a_bundle_of_another_sha_is_refused_before_any_work(deploy_tree):
    app, run, calls, _ = deploy_tree
    res = run("othersha")
    assert res.returncode != 0
    assert calls() == []
    _untouched(app)


# --------------------------------------------------------------------------
# fetch-env-from-ssm.sh
# --------------------------------------------------------------------------

SECRET = "s3cr3t-Value-0123456789"


def _payload(params: dict[str, str]) -> str:
    return json.dumps(
        {
            "Parameters": [
                {"Name": f"/fleet/prod/{k}", "Value": v} for k, v in params.items()
            ]
        }
    )


def _python_part() -> str:
    text = (DEPLOY / "fetch-env-from-ssm.sh").read_text(encoding="utf-8")
    m = re.search(r"python3 -c '(.*?)' \"\$PREFIX\" \"\$TMP_FILE\"; then", text, re.S)
    assert m, "python part not found"
    return m.group(1)


def test_python_part_reads_the_payload_from_stdin(tmp_path):
    out = tmp_path / "env.tmp"
    out.write_text("", encoding="utf-8")
    res = subprocess.run(
        [sys.executable, "-c", _python_part(), "/fleet/prod", str(out)],
        input=_payload({"A": "b=", "SECRET_KEY": SECRET}),
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    assert out.read_bytes() == f"A=b=\nSECRET_KEY={SECRET}\n".encode()


@pytest.mark.parametrize(
    "params",
    [
        {"LD_PRELOAD": "/tmp/x.so"},
        {"lower": "x"},
        {"PYTHONPATH": "/tmp"},
        {"PATH": "/tmp"},
        {"HOME": "/tmp"},
        {"A": 'say "hi"'},
        {"A": "it's"},
        {"A": "back\\slash"},
        {"A": "two\nlines"},
    ],
)
def test_python_part_refuses_bad_keys_and_values_and_writes_nothing(tmp_path, params):
    out = tmp_path / "env.tmp"
    out.write_text("", encoding="utf-8")
    res = subprocess.run(
        [sys.executable, "-c", _python_part(), "/fleet/prod", str(out)],
        input=_payload({"OK": "1", **params}),
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert out.read_text(encoding="utf-8") == ""


@pytest.fixture
def env_fetch(tmp_path):
    out_dir = tmp_path / "run"
    out_dir.mkdir()
    _write(out_dir / ".env", "OLD=1\n")
    stubs = tmp_path / "stubs"
    argv_log = tmp_path / "python-argv"
    payload = tmp_path / "payload.json"
    _stub(stubs, "chown", "exit 0")
    _stub(
        stubs,
        "python3",
        f'printf "%s\\n" "$@" >> "{_path(argv_log, "-m")}"\n'
        f'exec "{_path(Path(sys.executable), "-m")}" "$@"',
    )
    text = (DEPLOY / "fetch-env-from-ssm.sh").read_text(encoding="utf-8")
    text = text.replace('OUT_DIR="/run/fleet"', f'OUT_DIR="{_path(out_dir, "-m")}"')
    script = _write(tmp_path / "fetch-env.sh", text)

    def run(params: dict[str, str] | None):
        if params is None:  # aws fails
            _stub(stubs, "aws", "echo 'AccessDenied' >&2; exit 254")
        else:
            payload.write_text(_payload(params), encoding="utf-8")
            _stub(stubs, "aws", f'cat "{_path(payload, "-m")}"')
        return _run(stubs, script)

    return out_dir, run, argv_log


def _env_lines(out_dir: Path) -> list[str]:
    return (out_dir / ".env").read_text(encoding="utf-8").splitlines()


@needs_bash
def test_env_fetch_writes_the_env_without_the_payload_in_argv(env_fetch):
    out_dir, run, argv_log = env_fetch
    res = run({"A": "b=", "SECRET_KEY": SECRET})
    assert res.returncode == 0, res.stderr
    assert _env_lines(out_dir) == ["A=b=", f"SECRET_KEY={SECRET}"]
    argv = argv_log.read_text(encoding="utf-8")
    assert SECRET not in argv
    # After the code, python gets exactly two arguments: the prefix and the file.
    tail = argv.splitlines()[-2:]
    assert tail[0] == "/fleet/prod"
    assert tail[1].endswith("/run/.env.tmp")
    assert not (out_dir / ".env.tmp").exists()


@needs_bash
@pytest.mark.parametrize(
    "params",
    [
        None,
        {"LD_PRELOAD": "/tmp/x.so"},
        {"lower": "x"},
        {"A": "it's"},
        {"A": "back\\slash"},
        {},
    ],
    ids=["aws-fails", "ld-preload", "lowercase", "quote", "backslash", "empty"],
)
def test_env_fetch_keeps_the_old_env_on_failure(env_fetch, params):
    out_dir, run, _ = env_fetch
    res = run(params)
    assert res.returncode != 0
    assert _env_lines(out_dir) == ["OLD=1"]
    assert not (out_dir / ".env.tmp").exists()


# --------------------------------------------------------------------------
# ssm-cloudshell.sh: a re-run only adds what is missing (final review, item 7)
# --------------------------------------------------------------------------

DB_PW = "p" * 44
# A fake SSM: one file per parameter under <store>/, name with "/" -> "%".
_FAKE_AWS = r"""
store="__STORE__"; log="__LOG__"
op="$2"; name=""; value=""; query=""
shift 2
while [ $# -gt 0 ]; do
  case "$1" in
    --name) name="$2"; shift 2 ;;
    --value) value="$2"; shift 2 ;;
    --query) query="$2"; shift 2 ;;
    *) shift ;;
  esac
done
f="$store/$(printf '%s' "$name" | tr / %)"
case "$op" in
  get-parameter)
    [ -f "$f" ] || { echo "ParameterNotFound" >&2; exit 254; }
    [ "$query" = "Parameter.Value" ] && cat "$f"; exit 0 ;;
  put-parameter) printf '%s' "$value" > "$f"; echo "PUT $name" >> "$log" ;;
  get-parameters-by-path) : ;;
esac
"""


@pytest.fixture
def cloudshell(tmp_path):
    store = tmp_path / "ssm"
    store.mkdir()
    log = tmp_path / "puts.log"
    log.touch()
    stubs = tmp_path / "stubs"
    _stub(
        stubs,
        "aws",
        _FAKE_AWS.replace("__STORE__", _path(store, "-u")).replace(
            "__LOG__", _path(log, "-u")
        ),
    )
    _stub(stubs, "python3", f'exec "{_path(Path(sys.executable), "-u")}" "$@"')

    def run(*args, answers=("", "", "", "", "")):
        res = subprocess.run(
            [
                BASH,
                "-c",
                'PATH="$1:$PATH"; shift; exec bash "$@"',
                "_",
                _path(stubs, "-u"),
                _path(DEPLOY / "ssm-cloudshell.sh", "-m"),
                *args,
            ],
            # bytes, not text=True: Windows would turn each "\n" into "\r\n" and
            # `read` would keep the "\r".
            input=("\n".join(answers) + "\n").encode(),
            capture_output=True,
            env=ENV,
        )
        res.stdout = res.stdout.decode()
        res.stderr = res.stderr.decode()
        return res

    def value(name):
        f = store / name.replace("/", "%")
        return f.read_text(encoding="utf-8") if f.exists() else None

    def puts():
        return log.read_text(encoding="utf-8").split("PUT ")[1:]

    return run, value, puts, log, tmp_path


@needs_bash
def test_cloudshell_first_run_writes_everything(cloudshell):
    run, value, puts, _, tmp = cloudshell
    pw = _write(tmp / "pw", DB_PW + "\n")
    res = run(_path(pw, "-m"), answers=("ghtok", "sentrytok", "", ""))
    assert res.returncode == 0, res.stderr
    assert value("/fleet/prod/DB_PASSWORD") == DB_PW
    assert value("/fleet/prod/FLEET_GITHUB_READ_TOKEN") == "ghtok"
    assert value("/fleet/prod/SECRET_KEY")
    assert value("/fleet/prod/STATE") == "PROD"
    assert value("/fleet/prod/SENTRY_DSN") is None
    assert DB_PW not in res.stdout + res.stderr


@needs_bash
def test_cloudshell_rerun_with_empty_answers_only_adds_what_is_missing(cloudshell):
    run, value, puts, log, tmp = cloudshell
    pw = _write(tmp / "pw", DB_PW + "\n")
    assert run(_path(pw, "-m"), answers=("ghtok", "sentrytok", "", "")).returncode == 0
    key = value("/fleet/prod/SECRET_KEY")
    log.write_text("", encoding="utf-8")
    # No argument: the DB password is prompted; Enter keeps it, as for the tokens.
    res = run(answers=("", "", "", "https://k@o1.ingest.de.sentry.io/2", ""))
    assert res.returncode == 0, res.stderr
    assert [p.strip() for p in puts()] == ["/fleet/prod/SENTRY_DSN"]
    assert value("/fleet/prod/DB_PASSWORD") == DB_PW
    assert value("/fleet/prod/FLEET_GITHUB_READ_TOKEN") == "ghtok"
    assert value("/fleet/prod/SECRET_KEY") == key
    assert "DB_PASSWORD already exists: kept" in res.stdout


@needs_bash
def test_cloudshell_refuses_an_empty_db_password_on_a_first_run(cloudshell):
    run, value, puts, _, _ = cloudshell
    res = run("-")
    assert res.returncode != 0
    assert puts() == []
    assert value("/fleet/prod/DB_PASSWORD") is None


@needs_bash
def test_cloudshell_still_rejects_a_short_password(cloudshell):
    run, _, puts, _, _ = cloudshell
    res = run("-", answers=("short", "", "", "", ""))
    assert res.returncode != 0
    assert "too short" in res.stderr
    assert puts() == []
