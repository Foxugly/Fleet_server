#!/usr/bin/env bash
#
# Fleet_server: APP step of the deploy, run as user fleet after the privileged
# step of root (fleet-deploy-privileged.sh). Model: FoxHouse (deploy/ssm-deploy.sh).
#
# CALLING CONVENTION (SSM command list of .github/workflows/deploy.yml):
#   - root has downloaded s3://foxugly-deploy/builds/fleet-server/<sha>.tar.gz into
#     a `mktemp -d`, and fleet has extracted it (umask 027, from stdin) into
#     /srv/fleet/Fleet_server/.bundle-new. The bundle is a `git archive` of the
#     repo plus a RELEASE file holding the SHA; there is no .git in it.
#   - root copies THIS script from its mirror into a root-owned temp file (never
#     from the app tree) and runs:
#        sudo -u fleet bash <tmpfile> /srv/fleet/Fleet_server/.bundle-new [<sha>]
#     <bundle-dir> must be exactly /srv/fleet/Fleet_server/.bundle-new.
#     <sha> is optional: when given, the bundle's RELEASE must equal it.
#   - exit 0 = new code in place; root then restarts fleet-app-gunicorn,
#     fleet-app-celery and fleet-app-celery-beat. Non-zero = nothing swapped, the
#     old code keeps serving (a failed migrate or check stops BEFORE the swap).
#   - fleet needs no sudo: root does every restart.
#
# What it does:
#   1. installs the locked dependencies in /srv/fleet/Fleet_server/.venv (outside
#      the bundle: it survives deploys; created with python3.12 on the first run);
#   2. in the NEW tree: migrate, check --deploy, with the production
#      configuration (/run/fleet/.env). No collectstatic: no admin, no static files;
#   3. swap: the top-level entries of the previous bundle (listed in
#      .bundle-manifest) and those the new bundle brings are moved aside, the new
#      ones moved in, the old ones deleted. A file removed from the repo
#      (module, migration) disappears from the box too; .venv and anything that
#      never came from a bundle are kept.
set -euo pipefail
umask 027
shopt -s dotglob nullglob

APP=/srv/fleet/Fleet_server
NEW="${1:?usage: $0 <bundle-dir> [<sha>]}"
EXPECTED_SHA="${2-}"
OLD="$APP/.bundle-old"
MANIFEST="$APP/.bundle-manifest"
ENV_FILE=/run/fleet/.env

die() { echo "ERROR: $*" >&2; exit 1; }

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
    .venv|.bundle-*) die "the bundle must not contain ${entry##*/}" ;;
  esac
done
chmod -R g-w,o-rwx "$NEW"

cd "$APP"

# --- 1. Dependencies ---------------------------------------------------------
if [ ! -x .venv/bin/python ]; then
  # First deploy: Python 3.12 of Ubuntu 24.04, like CI (requirements.txt is
  # locked for 3.12).
  python3.12 -m venv .venv
fi
# requirements.txt is a hashed lock (uv, from requirements.in): production
# installs exactly what CI tested. --no-deps: everything is in the lock; pip
# check verifies it is consistent.
.venv/bin/pip install --quiet --disable-pip-version-check --require-hashes --no-deps -r "$NEW/requirements.txt"
.venv/bin/pip check

# --- 2. Production configuration, then migrate and check in the NEW tree ------
[ -s "$ENV_FILE" ] || die "$ENV_FILE missing or empty: did fleet-app-env-fetch run? (systemctl status fleet-app-env-fetch)"
# Literal parsing (key=value), never `source`: a value holding $( ) or quotes
# must not be interpreted by the shell (OPERATIONS.md §3.11).
while IFS='=' read -r key value || [ -n "$key" ]; do
  case "$key" in ''|\#*) continue ;; esac
  export "$key=$value"
done < "$ENV_FILE"

cd "$NEW"
"$APP/.venv/bin/python" manage.py migrate --noinput
"$APP/.venv/bin/python" manage.py check --deploy --fail-level ERROR
cd "$APP"

# --- 3. Swap -----------------------------------------------------------------
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
  case "$name" in ''|.|..|*/*|.venv|.bundle-*) continue ;; esac
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
rmdir "$NEW"
rm -rf "$OLD"

echo "[ssm-deploy] Fleet_server ${RELEASE:0:7} in place (dependencies, migrate, check)."
