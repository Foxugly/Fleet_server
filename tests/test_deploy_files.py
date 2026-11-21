"""Guards on deploy/: units, root scripts, vhost (parsing only, no AWS or systemd)."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
SYSTEMD = DEPLOY / "systemd"
APP_UNITS = ("fleet-app-gunicorn", "fleet-app-celery", "fleet-app-celery-beat")
HARDENING = (
    "NoNewPrivileges=yes",
    "PrivateTmp=yes",
    "ProtectSystem=full",
    "ProtectKernelTunables=yes",
    "ProtectControlGroups=yes",
    "RestrictSUIDSGID=yes",
    "Restart=always",
    "RestartSec=5",
    "UMask=0027",
    "User=fleet",
    "Group=fleet",
    "WorkingDirectory=/srv/fleet/Fleet_server",
    "EnvironmentFile=/run/fleet/.env",
    "Requires=fleet-app-env-fetch.service",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _directives(text: str) -> list[str]:
    """Non-comment lines of a unit, stripped."""
    return [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


@pytest.mark.parametrize("unit", APP_UNITS)
def test_app_units_are_hardened_and_run_as_fleet(unit):
    text = _read(SYSTEMD / f"{unit}.service")
    lines = _directives(text)
    for directive in HARDENING:
        assert directive in lines, f"{unit}: missing {directive}"
    assert "ProtectSystem=strict" not in lines
    directives = "\n".join(lines)
    assert "www-data" not in directives
    assert "django" not in directives
    after = next(ln for ln in lines if ln.startswith("After="))
    assert "postgresql.service" in after and "redis-server.service" in after


def test_only_gunicorn_keeps_db_connections():
    for unit in APP_UNITS:
        text = _read(SYSTEMD / f"{unit}.service")
        has = 'Environment="DB_CONN_MAX_AGE=60"' in _directives(text)
        assert has is (unit == "fleet-app-gunicorn"), unit


def test_gunicorn_binds_8014_on_localhost():
    lines = _directives(_read(SYSTEMD / "fleet-app-gunicorn.service"))
    assert "MemoryMax=512M" in lines
    exec_start = " ".join(
        lines[lines.index(next(ln for ln in lines if ln.startswith("ExecStart="))) :]
    )
    assert "--bind 127.0.0.1:8014" in exec_start
    assert "--no-control-socket" in exec_start
    assert "--workers 2" in exec_start and "--timeout 60" in exec_start


def test_celery_units():
    worker = " ".join(_directives(_read(SYSTEMD / "fleet-app-celery.service")))
    assert "celery -A config worker -l info --pool=prefork --concurrency=2" in worker
    beat = _directives(_read(SYSTEMD / "fleet-app-celery-beat.service"))
    assert "RuntimeDirectory=fleet-app-beat" in beat
    assert "--schedule /run/fleet-app-beat/schedule" in " ".join(beat)


def test_env_fetch_unit_is_a_root_oneshot():
    lines = _directives(_read(SYSTEMD / "fleet-app-env-fetch.service"))
    for directive in (
        "Type=oneshot",
        "User=root",
        "RuntimeDirectory=fleet",
        "RuntimeDirectoryMode=0750",
        "RuntimeDirectoryPreserve=yes",
        "ProtectSystem=strict",
        "ExecStart=/usr/local/sbin/fleet-app-env-fetch.sh",
    ):
        assert directive in lines, directive


def test_env_fetch_script_keeps_the_env_away_from_www_data():
    text = _read(DEPLOY / "fetch-env-from-ssm.sh")
    assert "www-data" not in text
    assert 'chown root:fleet "$OUT_DIR"' in text
    assert 'chmod 750 "$OUT_DIR"' in text
    assert 'OWNER="fleet:fleet"' in text
    assert 'chown "$OWNER" "$TMP_FILE"' in text
    assert 'chmod 640 "$TMP_FILE"' in text
    assert 'PREFIX="/fleet/prod"' in text


PRIVILEGED = DEPLOY / "fleet-deploy-privileged.sh"


def test_privileged_script_reads_only_the_mirror():
    text = _read(PRIVILEGED)
    assert 'git --git-dir="$M" show "$SHA:' in text
    # Every git invocation goes through the mirror, never a working tree.
    for line in _directives(text):
        if re.search(r"\bgit\s", line):
            assert 'git --git-dir="$M"' in line, line
    assert not re.search(r"\bcp\b[^\n]*/srv", text)
    assert not re.search(r"\binstall\b[^\n]*/srv/fleet/Fleet_server/", text)
    assert "set -euo pipefail" in text


def test_privileged_script_installs_units_and_enables_them():
    text = _read(PRIVILEGED)
    for unit in ("fleet-app-env-fetch", *APP_UNITS):
        assert f"{unit}.service" in text
    assert "/usr/local/sbin/fleet-app-env-fetch.sh" in text
    assert "cmp -s" in text
    assert "systemctl enable --now fleet-app-env-fetch.service" in text
    assert "test -s /run/fleet/.env" in text
    enable = "systemctl enable " + " ".join(f"{u}.service" for u in APP_UNITS)
    assert enable in text


def test_privileged_script_restores_the_vhost_when_nginx_t_fails():
    """Review Focus 2: a broken vhost never replaces the working one."""
    text = _read(PRIVILEGED)
    m = re.search(r"if nginx -t; then(.*?)\n    fi\n", text, re.S)
    assert m, "nginx -t branch not found"
    failure = m.group(1).split("else", 1)[1]
    assert 'mv -f "$VHOST.deploybak" "$VHOST"' in failure
    assert "exit 1" in failure
    assert 'cp -a "$VHOST" "$VHOST.deploybak"' in text


def test_ssm_deploy_script():
    text = _read(DEPLOY / "ssm-deploy.sh")
    assert "set -euo pipefail" in text and "umask 027" in text
    assert "python3.12 -m venv" in text
    assert "--require-hashes --no-deps -r" in text
    assert "pip check" in text
    assert "manage.py migrate --noinput" in text
    assert "manage.py check --deploy --fail-level ERROR" in text
    code = "\n".join(_directives(text))
    assert "collectstatic" not in code
    # literal .env parsing, never sourced
    assert not re.search(r"^\s*(source|\.)\s+\S*\.env", code, re.M)
    assert "read -r key value" in text
    assert '[ "$NEW" = "$APP/.bundle-new" ]' in text
    # migrate and check run BEFORE the swap (Review Focus 2: old code keeps serving).
    assert text.index("manage.py migrate") < text.index("# --- 3. Swap")


VHOST = DEPLOY / "nginx" / "fleet-api.foxugly.com"


def test_vhost_proxies_to_8014_with_tls_and_headers():
    text = _read(VHOST)
    assert "listen 443 ssl http2;" in text
    assert "server_name fleet-api.foxugly.com;" in text
    assert "proxy_pass http://127.0.0.1:8014;" in text
    assert "proxy_set_header X-Forwarded-Proto $scheme;" in text
    assert "Strict-Transport-Security" in text
    assert 'X-Content-Type-Options "nosniff"' in text
    assert "/etc/letsencrypt/live/foxugly.com/fullchain.pem" in text
    assert "/var/log/nginx/fleet-api-access.log" in text
    assert "location ^~ /api/v1/auth/" in text
    for zone in re.findall(r"zone=(\w+)", text):
        assert zone.startswith("fleet_"), zone


def test_cloudshell_scripts_target_fleet():
    iam = _read(DEPLOY / "iam-cloudshell.sh")
    for needle in (
        "1466918968",
        "1467008189",
        "362629935151",
        "i-0fe664678563bae5f",
        "fleet-deploy",
        "fleet-frontend-deploy",
        "builds/fleet-server/*",
        "builds/fleet-frontend/*",
        "SsmReadFleetAppConfig",
        "parameter/fleet/prod/*",
        "parameter/fleet-frontend/prod/*",
    ):
        assert needle in iam, needle
    assert "create-policy" not in iam
    ssm = _read(DEPLOY / "ssm-cloudshell.sh")
    for needle in (
        "/fleet/prod",
        "/fleet-frontend/prod",
        "FLEET_MFA_ENCRYPTION_KEY",
        "redis://127.0.0.1:6379/12",
    ):
        assert needle in ssm, needle


BASH = shutil.which("bash")


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize("script", sorted(p.name for p in DEPLOY.glob("*.sh")))
def test_shell_scripts_parse(script):
    res = subprocess.run(
        [BASH, "-n", str(DEPLOY / script)], capture_output=True, text=True
    )
    assert res.returncode == 0, res.stderr


def test_there_are_shell_scripts_to_check():
    names = {p.name for p in DEPLOY.glob("*.sh")}
    assert {
        "fetch-env-from-ssm.sh",
        "fleet-deploy-privileged.sh",
        "ssm-deploy.sh",
        "iam-cloudshell.sh",
        "ssm-cloudshell.sh",
    } <= names
