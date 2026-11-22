#!/usr/bin/env bash
#
# Fleet_server: APP step of the deploy, run as user fleet after the privileged
# step of root (fleet-deploy-privileged.sh). Model: FoxHouse (deploy/ssm-deploy.sh).
#
# CALLING CONVENTION (SSM command list of .github/workflows/deploy.yml):
#   - root has downloaded s3://foxugly-deploy/builds/fleet-server/<sha>.tar.gz into
#     a `mktemp -d`, and fleet has extracted it (umask 027, from stdin) into
#     /srv/fleet/Fleet_server/.bundle-new. The bundle is a root-level `git archive`
#     of the repo plus a RELEASE file holding the SHA; there is no .git in it.
#   - root copies THIS script from its mirror into a root-owned temp file that
#     fleet can read (0644; never from the app tree) and runs:
#        sudo -u fleet bash <tmpfile> /srv/fleet/Fleet_server/.bundle-new [<sha>]
#     <bundle-dir> must be exactly /srv/fleet/Fleet_server/.bundle-new.
#     <sha> is optional: when given, the bundle's RELEASE must equal it.
#   - exit 0 = new code AND new .venv in place; root then restarts
#     fleet-app-gunicorn, fleet-app-celery and fleet-app-celery-beat.
#     Non-zero = nothing swapped: the old code and the old .venv keep serving,
#     .venv.new is removed.
#   - fleet needs no sudo: root does every restart.
#
# What it does, in this order (each step stops the deploy on failure):
#   1. builds a FRESH /srv/fleet/Fleet_server/.venv.new (/usr/bin/python3.14 -m venv), installs
#      the locked dependencies of the NEW bundle into it, pip check;
#   2. loads the production configuration (/run/fleet/.env) by literal parsing;
#   3. with .venv.new and the NEW code: check --deploy, THEN migrate.
#      No collectstatic: no admin, no static files;
#   4. swap, code and venv together:
#      - code: the top-level entries of the previous bundle (listed in
#        .bundle-manifest) and those the new bundle brings are moved aside, the
#        new ones moved in, the old ones deleted. A file removed from the repo
#        (module, migration) disappears from the box too; anything that never
#        came from a bundle is kept;
#      - venv: .venv -> .venv.prev, .venv.new -> .venv.
#   A moved venv keeps its console-script shebangs pointing at .venv.new, so
#   nothing here or in the units runs .venv/bin/<script>: always
#   .venv/bin/python -m <module> (units: -m gunicorn, -m celery).
set -euo pipefail
umask 027
shopt -s dotglob nullglob

die() { echo "ERROR: $*" >&2; exit 1; }

# load_env <file>: export KEY=value lines literally, never `source` (a value
# holding $( ) or quotes must not be interpreted, OPERATIONS.md §3.11). The value
# is everything after the FIRST `=`, so `A=b=` gives `b=`. Keys must look like
# environment names, and the ones that steer the shell, the loader or Python are
# refused (same rules as fetch-env-from-ssm.sh).
load_env() {
  local line key value
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|\#*) continue ;; esac
    [[ "$line" == *=* ]] || die "malformed line in $1 (no '=')"
    key=${line%%=*}
    value=${line#*=}
    [[ "$key" =~ ^[A-Z][A-Z0-9_]*$ ]] || die "invalid key in $1: $key"
    case "$key" in
      PATH|LD_*|PYTHON*|BASH_ENV|ENV|IFS|HOME|SHELL) die "refused key in $1: $key" ;;
    esac
    case "$value" in
      *\"*|*\'*|*\\*) die "refused value for $key in $1 (quote or backslash)" ;;
    esac
    export "$key=$value"
  done < "$1"
}

# Sourced (tests): functions only.
if [ "${BASH_SOURCE[0]}" != "$0" ]; then
  return 0
fi

APP=/srv/fleet/Fleet_server
NEW="${1:?usage: $0 <bundle-dir> [<sha>]}"
EXPECTED_SHA="${2-}"
OLD="$APP/.bundle-old"
MANIFEST="$APP/.bundle-manifest"
VENV_NEW="$APP/.venv.new"
ENV_FILE=/run/fleet/.env
# deadsnakes 3.14, root-owned; the system python3 of Ubuntu 24.04 stays 3.12.
PYTHON=/usr/bin/python3.14

[ "$NEW" = "$APP/.bundle-new" ] || die "bundle dir must be $APP/.bundle-new (got $NEW)"
[ -d "$NEW" ] && [ ! -L "$NEW" ] || die "$NEW missing (bundle extraction?)"
[ -f "$NEW/manage.py" ] && [ -f "$NEW/requirements.txt" ] || die "$NEW is not a Fleet_server bundle"
RELEASE=$(tr -d '[:space:]' 2>/dev/null < "$NEW/RELEASE" || true)
[ -n "$RELEASE" ] || die "$NEW/RELEASE missing or empty (written by CI)"
if [ -n "$EXPECTED_SHA" ] && [ "$RELEASE" != "$EXPECTED_SHA" ]; then
  die "the bundle carries $RELEASE, not $EXPECTED_SHA"
fi

# Names the swap must never touch: refuse a bundle that would bring one.
entries=("$NEW"/*)
[ "${#entries[@]}" -gt 0 ] || die "$NEW is empty"
for entry in "${entries[@]}"; do
  case "${entry##*/}" in
    .venv|.venv.*|.bundle-*) die "the bundle must not contain ${entry##*/}" ;;
  esac
done
chmod -R g-w,o-rwx "$NEW"

cd "$APP"

# Whatever happens before the swap, a half-built venv does not survive; after the
# swap .venv.new no longer exists and this is a no-op.
trap 'rm -rf "$VENV_NEW"' EXIT

# --- 1. Fresh venv with the NEW lock -----------------------------------------
rm -rf "$VENV_NEW"
# Python 3.14, like CI (requirements.txt is locked for 3.14).
"$PYTHON" -m venv "$VENV_NEW"
# requirements.txt is a hashed lock (uv, from requirements.in): production
# installs exactly what CI tested. --no-deps: everything is in the lock; pip
# check verifies it is consistent.
"$VENV_NEW/bin/python" -m pip install --quiet --disable-pip-version-check \
  --require-hashes --no-deps -r "$NEW/requirements.txt"
"$VENV_NEW/bin/python" -m pip check

# --- 2. Production configuration ---------------------------------------------
[ -s "$ENV_FILE" ] || die "$ENV_FILE missing or empty: did fleet-app-env-fetch run? (systemctl status fleet-app-env-fetch)"
load_env "$ENV_FILE"

# --- 3. Check, then migrate: new venv, new code ------------------------------
cd "$NEW"
"$VENV_NEW/bin/python" manage.py check --deploy --fail-level ERROR
"$VENV_NEW/bin/python" manage.py migrate --noinput
cd "$APP"

# --- 4. Swap: code, then venv ------------------------------------------------
rm -rf "$OLD"
mkdir "$OLD"
retire=()
if [ -f "$MANIFEST" ]; then
  while IFS= read -r name; do
    retire+=("$name")
  done < "$MANIFEST"
fi
for entry in "${entries[@]}"; do
  retire+=("${entry##*/}")
done
for name in "${retire[@]}"; do
  case "$name" in ''|.|..|*/*|.venv|.venv.*|.bundle-*) continue ;; esac
  if [ -e "$APP/$name" ] || [ -L "$APP/$name" ]; then
    mv "$APP/$name" "$OLD/"
  fi
done

manifest_new="$MANIFEST.tmp"
: > "$manifest_new"
for entry in "${entries[@]}"; do
  name="${entry##*/}"
  mv "$entry" "$APP/$name"
  printf '%s\n' "$name" >> "$manifest_new"
done
mv -f "$manifest_new" "$MANIFEST"

rm -rf "$APP/.venv.prev"
if [ -e "$APP/.venv" ]; then
  mv "$APP/.venv" "$APP/.venv.prev"
fi
mv "$VENV_NEW" "$APP/.venv"

rmdir "$NEW"
rm -rf "$OLD"

echo "[ssm-deploy] Fleet_server ${RELEASE:0:7} in place (fresh venv, check, migrate)."
