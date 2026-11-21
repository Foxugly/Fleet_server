"""deploy.yml and ci.yml: structure (YAML parsing) and the behaviour of the
deploy job's shell steps, run in bash with the outside world stubbed on PATH
(aws, curl, git ls-remote, sleep). Behaviour tests skip when bash is absent.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
DEPLOY = WORKFLOWS / "deploy.yml"
CI = WORKFLOWS / "ci.yml"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not available")

SHA = "0123456789abcdef0123456789abcdef01234567"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _on(wf: dict) -> dict:
    # PyYAML reads the bare key `on` as the boolean True (YAML 1.1).
    return wf.get("on", wf.get(True))


def _deploy_job() -> dict:
    return _load(DEPLOY)["jobs"]["deploy"]


def _step(step_id: str) -> dict:
    for step in _deploy_job()["steps"]:
        if step.get("id") == step_id:
            return step
    raise AssertionError(f"no step with id {step_id!r}")


# --------------------------------------------------------------------------
# Structure
# --------------------------------------------------------------------------


def test_deploy_triggers_and_top_level_permissions():
    wf = _load(DEPLOY)
    on = _on(wf)
    assert on["push"] == {"branches": ["main"]}
    assert "workflow_dispatch" in on
    assert "pull_request" not in on
    assert wf["permissions"] == {}


def test_deploy_reuses_ci_and_waits_for_it():
    jobs = _load(DEPLOY)["jobs"]
    assert jobs["ci"]["uses"] == "./.github/workflows/ci.yml"
    assert jobs["ci"]["permissions"] == {"contents": "read"}
    assert jobs["deploy"]["needs"] == "ci"


def test_deploy_job_is_production_oidc_and_serialised():
    job = _deploy_job()
    assert job["environment"] == "production"
    assert job["permissions"] == {"id-token": "write", "contents": "read"}
    assert job["concurrency"] == {
        "group": "deploy-fleet-server",
        "cancel-in-progress": False,
    }
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["timeout-minutes"] >= 25


@pytest.mark.parametrize("path", [DEPLOY, CI], ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_full_sha(path):
    text = path.read_text(encoding="utf-8")
    uses = re.findall(r"^\s*(?:-\s*)?uses:\s*(\S+)", text, flags=re.M)
    assert uses, path
    for ref in uses:
        if ref.startswith("./"):
            continue  # local reusable workflow
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref), ref


def test_ci_is_reusable_and_still_runs_on_pull_requests():
    wf = _load(CI)
    on = _on(wf)
    assert "workflow_call" in on
    assert "pull_request" in on
    # A push to main is covered by deploy.yml calling ci.yml: a `push` trigger
    # here would run the suite twice.
    assert "push" not in on
    # Branch protection requires these check names on PRs.
    assert {"test", "pip-audit"} <= set(wf["jobs"])


def test_ci_concurrency_never_cancels_a_main_run():
    conc = _load(CI)["concurrency"]
    assert conc["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert "github.sha" in conc["group"]


def test_every_step_after_the_head_guard_is_conditional_on_it():
    steps = _deploy_job()["steps"]
    ids = [s.get("id") for s in steps]
    assert "head" in ids
    after = steps[ids.index("head") + 1 :]
    assert after
    for step in after:
        assert step.get("if") == "steps.head.outputs.current == 'true'", step.get(
            "name"
        )


def test_the_oidc_step_assumes_the_environment_role():
    step = next(
        s
        for s in _deploy_job()["steps"]
        if "configure-aws-credentials" in s.get("uses", "")
    )
    assert step["with"]["role-to-assume"] == "${{ secrets.AWS_DEPLOY_ROLE_ARN }}"
    assert step["with"]["aws-region"] == "eu-west-1"


@pytest.mark.parametrize(
    "step_id", ["head", "bundle", "upload", "ssm", "wait", "health"]
)
def test_shell_steps_take_github_values_through_env_not_inline(step_id):
    # No `${{ }}` inside a run: script (injection-safe, and runnable as is here).
    assert "${{" not in _step(step_id)["run"]


def test_ssm_steps_read_the_instance_from_the_secret():
    assert _step("ssm")["env"]["INSTANCE_ID"] == "${{ secrets.EC2_INSTANCE_ID }}"
    assert _step("wait")["env"]["INSTANCE_ID"] == "${{ secrets.EC2_INSTANCE_ID }}"
    assert _step("wait")["env"]["CMD_ID"] == "${{ steps.ssm.outputs.cmd_id }}"


def test_wait_loop_outlasts_the_ssm_execution_timeout():
    run = _step("wait")["run"]
    tries = int(re.search(r"seq 1 (\d+)", run).group(1))
    pause = int(re.search(r"sleep (\d+)", run).group(1))
    assert tries * pause >= 1200 + 60


# --------------------------------------------------------------------------
# Behaviour: each step's script run in bash, outside world stubbed
# --------------------------------------------------------------------------


def _posix(p: Path) -> str:
    res = subprocess.run(
        [
            BASH,
            "-c",
            'if command -v cygpath >/dev/null 2>&1; then cygpath -u "$1";'
            ' else printf "%s" "$1"; fi',
            "_",
            str(p),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def _stub(stubs: Path, name: str, body: str) -> None:
    stubs.mkdir(parents=True, exist_ok=True)
    path = stubs / name
    path.write_text(
        "#!/usr/bin/env bash\n" + body + "\n", encoding="utf-8", newline="\n"
    )
    path.chmod(0o755)


def _runner(tmp_path: Path) -> tuple[Path, Path]:
    """A fake runner: temp dir, $GITHUB_OUTPUT/$GITHUB_ENV files, stub dir."""
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir()
    stubs = tmp_path / "stubs"
    _stub(stubs, "python3", f'exec "{_posix(Path(sys.executable))}" "$@"')
    _stub(stubs, "sleep", ":")
    return runner_temp, stubs


def _github_shell(step: dict, job: dict, wf: dict) -> list[str]:
    """bash flags GitHub uses for a `run:` step on ubuntu, from the nearest
    `shell:` (step, then job defaults, then workflow defaults)."""
    shell = (
        step.get("shell")
        or job.get("defaults", {}).get("run", {}).get("shell")
        or wf.get("defaults", {}).get("run", {}).get("shell")
    )
    if shell is None:
        return ["-e"]  # GitHub's `bash -e {0}`: no pipefail
    if shell == "bash":
        return ["--noprofile", "--norc", "-eo", "pipefail"]
    raise AssertionError(f"unexpected shell {shell!r}")


def _run_step(
    step_id: str,
    tmp_path: Path,
    stubs: Path,
    env: dict,
    cwd: Path | None = None,
    *,
    run: str | None = None,
    flags: list[str] | None = None,
):
    """Run a deploy step's script with the bash flags GitHub would use."""
    wf = _load(DEPLOY)
    job = wf["jobs"]["deploy"]
    step = _step(step_id)
    if flags is None:
        flags = _github_shell(step, job, wf)
    script = tmp_path / f"{step_id}.sh"
    script.write_text(run or step["run"], encoding="utf-8", newline="\n")
    out = tmp_path / "github_output"
    genv = tmp_path / "github_env"
    out.touch()
    genv.touch()
    full_env = {
        **os.environ,
        "GITHUB_SHA": SHA,
        "GITHUB_REPOSITORY": "Foxugly/Fleet_server",
        "GITHUB_OUTPUT": _posix(out),
        "GITHUB_ENV": _posix(genv),
        "RUNNER_TEMP": _posix(tmp_path / "runner"),
        **env,
    }
    res = subprocess.run(
        [
            BASH,
            "--noprofile",
            "--norc",
            "-c",
            'PATH="$1:$PATH"; shift; exec bash "$@"',
            "_",
            _posix(stubs),
            *flags,
            _posix(script),
        ],
        capture_output=True,
        text=True,
        env=full_env,
        cwd=cwd or tmp_path,
    )
    outputs = dict(
        line.split("=", 1)
        for line in out.read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    return res, outputs


# -- head-of-main guard ------------------------------------------------------


@needs_bash
@pytest.mark.parametrize(("head", "current"), [(SHA, "true"), ("f" * 40, "false")])
def test_head_guard_deploys_only_the_head_of_main(tmp_path, head, current):
    _, stubs = _runner(tmp_path)
    _stub(
        stubs,
        "git",
        f'[ "$1 $3" = "ls-remote refs/heads/main" ] || exit 9\n'
        f'[ "$2" = "https://github.com/Foxugly/Fleet_server.git" ] || exit 8\n'
        f'printf "%s\\trefs/heads/main\\n" "{head}"',
    )
    res, outputs = _run_step("head", tmp_path, stubs, {})
    assert res.returncode == 0, res.stderr
    assert outputs["current"] == current


def test_deploy_steps_run_under_bash_with_pipefail():
    # Without `shell: bash`, GitHub runs `bash -e {0}`: no pipefail, so a
    # failing `git ls-remote | cut` would look like success.
    assert _load(DEPLOY)["defaults"] == {"run": {"shell": "bash"}}
    wf = _load(DEPLOY)
    job = wf["jobs"]["deploy"]
    for step in job["steps"]:
        if "run" in step:
            assert "pipefail" in _github_shell(step, job, wf), step.get("name")


# The guard as first written (review fix round 1): no explicit empty-head check.
OLD_HEAD_GUARD = """\
head=$(git ls-remote "https://github.com/${GITHUB_REPOSITORY}.git" refs/heads/main \
  | cut -f1)
if [ "$head" = "$GITHUB_SHA" ]; then
  echo "current=true" >> "$GITHUB_OUTPUT"
else
  echo "current=false" >> "$GITHUB_OUTPUT"
fi
"""


@needs_bash
def test_old_head_guard_without_shell_bash_skipped_silently(tmp_path):
    """The regression: GitHub unreadable, `bash -e` (no `shell:`) -> green skip."""
    _, stubs = _runner(tmp_path)
    _stub(stubs, "git", "exit 128")
    res, outputs = _run_step(
        "head", tmp_path, stubs, {}, run=OLD_HEAD_GUARD, flags=["-e"]
    )
    assert res.returncode == 0
    assert outputs["current"] == "false"


@needs_bash
def test_head_guard_fails_when_github_cannot_be_read(tmp_path):
    _, stubs = _runner(tmp_path)
    _stub(stubs, "git", "exit 128")
    res, outputs = _run_step("head", tmp_path, stubs, {})
    assert res.returncode != 0
    assert "current" not in outputs


@needs_bash
@pytest.mark.parametrize("flags", [["-e"], None], ids=["bash-e", "github-shell"])
def test_head_guard_fails_on_an_empty_answer_whatever_the_shell(tmp_path, flags):
    _, stubs = _runner(tmp_path)
    _stub(stubs, "git", "exit 0")  # ls-remote "succeeds" with no ref
    res, outputs = _run_step("head", tmp_path, stubs, {}, flags=flags)
    assert res.returncode != 0
    assert "current" not in outputs


# -- bundle ------------------------------------------------------------------


@needs_bash
def test_bundle_is_the_repo_at_head_plus_release_without_prefix(tmp_path):
    runner_temp, stubs = _runner(tmp_path)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    res, _ = _run_step("bundle", tmp_path, stubs, {"GITHUB_SHA": head}, cwd=ROOT)
    assert res.returncode == 0, res.stderr
    genv = dict(
        line.split("=", 1)
        for line in (tmp_path / "github_env").read_text(encoding="utf-8").splitlines()
    )
    bundle = runner_temp / f"fleet-server-{head}.tar.gz"
    assert _posix(bundle) == genv["BUNDLE"]
    with tarfile.open(bundle, "r:gz") as tar:
        names = tar.getnames()
        release = tar.extractfile("RELEASE").read().decode()
    assert release.strip() == head
    assert {"manage.py", "requirements.txt", "deploy/ssm-deploy.sh"} <= set(names)
    assert names.count("RELEASE") == 1
    assert not any(n.startswith(".git/") for n in names)


@needs_bash
def test_bundle_refuses_a_checkout_that_is_not_the_pushed_sha(tmp_path):
    _, stubs = _runner(tmp_path)
    res, _ = _run_step("bundle", tmp_path, stubs, {"GITHUB_SHA": SHA}, cwd=ROOT)
    assert res.returncode != 0


# -- upload ------------------------------------------------------------------


@needs_bash
def test_upload_goes_to_the_fleet_server_builds_prefix(tmp_path):
    _, stubs = _runner(tmp_path)
    log = tmp_path / "aws.log"
    _stub(stubs, "aws", f'printf "%s\\n" "$@" > "{_posix(log)}"')
    res, _ = _run_step("upload", tmp_path, stubs, {"BUNDLE": "/x/fleet-server.tar.gz"})
    assert res.returncode == 0, res.stderr
    args = log.read_text(encoding="utf-8").split()
    assert args[:2] == ["s3", "cp"]
    assert f"s3://foxugly-deploy/builds/fleet-server/{SHA}.tar.gz" in args
    assert "/x/fleet-server.tar.gz" in args


# -- SSM command list ----------------------------------------------------------


def _ssm_params(tmp_path: Path, sha: str = SHA):
    _, stubs = _runner(tmp_path)
    capture = tmp_path / "params.json"
    _stub(
        stubs,
        "aws",
        'for a in "$@"; do case "$a" in file://*) cp "${a#file://}" '
        f'"{_posix(capture)}";; esac; done\necho cmd-0001',
    )
    # MSYS2_ARG_CONV_EXCL: Git Bash on Windows would otherwise rewrite arguments
    # such as `M=/var/lib/...` into Windows paths when calling the native Python.
    env = {"GITHUB_SHA": sha, "INSTANCE_ID": "i-test", "MSYS2_ARG_CONV_EXCL": "*"}
    res, outputs = _run_step("ssm", tmp_path, stubs, env)
    params = (
        json.loads(capture.read_text(encoding="utf-8")) if capture.exists() else None
    )
    return res, outputs, params


@needs_bash
def test_ssm_command_list_starts_with_set_eu_and_root_home(tmp_path):
    res, outputs, params = _ssm_params(tmp_path)
    assert res.returncode == 0, res.stderr
    assert outputs["cmd_id"] == "cmd-0001"
    cmds = params["commands"]
    assert cmds[0] == "set -eu"
    assert cmds[1] == (
        "export HOME=/root"
        " AWS_SHARED_CREDENTIALS_FILE=/dev/null AWS_CONFIG_FILE=/dev/null"
    )
    assert params["executionTimeout"] == ["1200"]
    assert f"SHA={SHA}" in cmds


@needs_bash
def test_ssm_command_list_runs_the_deploy_in_order(tmp_path):
    _, _, params = _ssm_params(tmp_path)
    script = "\n".join(params["commands"])
    expected_in_order = [
        "M=/var/lib/fleet-deploy/Fleet_server.git",
        "install -d -m 700 -o root -g root /var/lib/fleet-deploy",
        '[ -d "$M" ] || git init --quiet --bare "$M"',
        'git --git-dir="$M" fetch --quiet --depth=1'
        ' https://github.com/Foxugly/Fleet_server.git "$SHA"',
        'git --git-dir="$M" cat-file -e "$SHA^{commit}"',
        'git --git-dir="$M" show "$SHA:deploy/fleet-deploy-privileged.sh" >',
        "/usr/local/sbin/fleet-app-deploy-privileged.sh",
        '/usr/local/sbin/fleet-app-deploy-privileged.sh "$M" "$SHA"',
        'aws s3 cp --only-show-errors "s3://foxugly-deploy/builds/fleet-server/$SHA.tar.gz"',
        "tar --no-same-owner --no-overwrite-dir -xzf -"
        " -C /srv/fleet/Fleet_server/.bundle-new",
        'git --git-dir="$M" show "$SHA:deploy/ssm-deploy.sh" >',
        'sudo -u fleet bash "$S" /srv/fleet/Fleet_server/.bundle-new "$SHA"',
        "systemctl restart fleet-app-gunicorn fleet-app-celery fleet-app-celery-beat",
        "-H 'Host: fleet-api.foxugly.com' -H 'X-Forwarded-Proto: https' http://127.0.0.1:8014/health/",
        'chown -R -h fleet:fleet "$A"',
        'sudo -u fleet chmod -R g-w,o-rwx "$A"',
        'rm -rf "$T" "$S"',
    ]
    pos = -1
    for fragment in expected_in_order:
        nxt = script.find(fragment, pos + 1)
        assert nxt > pos, f"missing or out of order: {fragment}"
        pos = nxt


@needs_bash
def test_ssm_runs_nothing_from_the_app_tree_as_root(tmp_path):
    _, _, params = _ssm_params(tmp_path)
    for cmd in params["commands"]:
        # Root never executes or sources a file fleet can write.
        assert not re.search(r"(^|[;&|]\s*)(bash|sh|\.)\s+\"?(\$A|/srv/fleet)", cmd), (
            cmd
        )
        # The app step script comes from root's mirror, into a root-owned file.
        assert "/srv/fleet/Fleet_server/deploy/" not in cmd, cmd
        assert ".bundle-new/deploy/" not in cmd, cmd


@needs_bash
def test_ssm_script_parses_as_posix_sh(tmp_path):
    _, _, params = _ssm_params(tmp_path)
    script = tmp_path / "box.sh"
    script.write_text(
        "\n".join(params["commands"]) + "\n", encoding="utf-8", newline="\n"
    )
    res = subprocess.run(
        [BASH, "--posix", "-n", _posix(script)], capture_output=True, text=True
    )
    assert res.returncode == 0, res.stderr


@needs_bash
@pytest.mark.parametrize("bad", ["abc", SHA.upper(), SHA + "; reboot", ""])
def test_ssm_step_refuses_a_malformed_sha(tmp_path, bad):
    res, _, params = _ssm_params(tmp_path, sha=bad)
    assert res.returncode != 0
    assert params is None


# -- wait --------------------------------------------------------------------


@needs_bash
@pytest.mark.parametrize(
    ("status", "code"), [("Success", 0), ("Failed", 1), ("TimedOut", 1)]
)
def test_wait_step_follows_the_invocation_status(tmp_path, status, code):
    _, stubs = _runner(tmp_path)
    _stub(
        stubs,
        "aws",
        'case "$*" in *"--query Status"*) echo '
        + status
        + " ;; *) echo output ;; esac",
    )
    res, _ = _run_step(
        "wait", tmp_path, stubs, {"INSTANCE_ID": "i-test", "CMD_ID": "cmd-1"}
    )
    assert res.returncode == code, res.stdout + res.stderr


# -- public health -----------------------------------------------------------


def _curl_stub(stubs: Path, status: str, version: str) -> None:
    body = json.dumps({"status": "ok", "version": version})
    _stub(
        stubs,
        "curl",
        'out=""; prev=""\n'
        'for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done\n'
        'case "$*" in *https://fleet-api.foxugly.com/health/*) ;; *) exit 7 ;; esac\n'
        f"printf '%s' '{body}' > \"$out\"\nprintf '%s' '{status}'",
    )


@needs_bash
@pytest.mark.parametrize(
    ("status", "version", "code"),
    [("200", SHA, 0), ("200", "f" * 40, 1), ("502", SHA, 1)],
)
def test_public_health_requires_200_and_the_pushed_version(
    tmp_path, status, version, code
):
    _, stubs = _runner(tmp_path)
    _curl_stub(stubs, status, version)
    res, _ = _run_step("health", tmp_path, stubs, {})
    assert res.returncode == code, res.stdout + res.stderr


# -- review fix round 1: bounded calls, wait-loop errors, curl exit codes -----


@needs_bash
def test_send_command_has_a_delivery_timeout(tmp_path):
    _, stubs = _runner(tmp_path)
    log = tmp_path / "aws.log"
    _stub(stubs, "aws", f'printf "%s\n" "$@" > "{_posix(log)}"; echo cmd-1')
    env = {"INSTANCE_ID": "i-test", "MSYS2_ARG_CONV_EXCL": "*"}
    res, _ = _run_step("ssm", tmp_path, stubs, env)
    assert res.returncode == 0, res.stderr
    args = log.read_text(encoding="utf-8").splitlines()
    assert args[args.index("--timeout-seconds") + 1] == "120"


@needs_bash
def test_local_health_curl_is_bounded(tmp_path):
    _, _, params = _ssm_params(tmp_path)
    (loop,) = [c for c in params["commands"] if "127.0.0.1:8014/health/" in c]
    assert "curl -fsS --max-time 5 " in loop


def _flaky_aws(stubs: Path, tmp_path: Path, failures: int, status: str) -> Path:
    """aws stub: the first `failures` Status queries fail (AccessDenied)."""
    count = tmp_path / "calls"
    _stub(
        stubs,
        "aws",
        f'c="{_posix(count)}"\n'
        'case "$*" in *"--query Status"*)\n'
        '  n=$(( $(cat "$c" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$c"\n'
        f'  if [ "$n" -le {failures} ]; then\n'
        '    echo "AccessDenied #$n" >&2; exit 254\n'
        "  fi\n"
        f"  echo {status} ;;\n"
        "*) echo output ;; esac",
    )
    return count


@needs_bash
def test_wait_step_fails_after_five_consecutive_api_errors(tmp_path):
    _, stubs = _runner(tmp_path)
    count = _flaky_aws(stubs, tmp_path, failures=99, status="Success")
    res, _ = _run_step(
        "wait", tmp_path, stubs, {"INSTANCE_ID": "i-test", "CMD_ID": "cmd-1"}
    )
    assert res.returncode == 1
    assert count.read_text().strip() == "5"
    assert "AccessDenied #5" in res.stdout + res.stderr


@needs_bash
def test_wait_step_tolerates_four_transient_api_errors(tmp_path):
    _, stubs = _runner(tmp_path)
    _flaky_aws(stubs, tmp_path, failures=4, status="Success")
    res, _ = _run_step(
        "wait", tmp_path, stubs, {"INSTANCE_ID": "i-test", "CMD_ID": "cmd-1"}
    )
    assert res.returncode == 0, res.stdout + res.stderr


@needs_bash
@pytest.mark.parametrize(("fail_first", "code"), [(5, 1), (2, 0)])
def test_public_health_survives_curl_errors_and_reports_000_once(
    tmp_path, fail_first, code
):
    _, stubs = _runner(tmp_path)
    count = tmp_path / "curls"
    body = json.dumps({"status": "ok", "version": SHA})
    _stub(
        stubs,
        "curl",
        f'c="{_posix(count)}"\n'
        'n=$(( $(cat "$c" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$c"\n'
        f'if [ "$n" -le {fail_first} ]; then printf 000; exit 7; fi\n'
        'out=""; prev=""\n'
        'for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done\n'
        f"printf '%s' '{body}' > \"$out\"; printf 200",
    )
    res, _ = _run_step("health", tmp_path, stubs, {})
    assert res.returncode == code, res.stdout + res.stderr
    assert "HTTP 000" in res.stdout
    assert "000000" not in res.stdout
