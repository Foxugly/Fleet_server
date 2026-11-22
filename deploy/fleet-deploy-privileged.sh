#!/usr/bin/env bash
#
# Fleet_server: PRIVILEGED step of the deploy (runs as ROOT).
#
# Model: FoxHouse (foxhouse-deploy-privileged.sh), OPERATIONS.md §3.10 / §3.11.
# Root runs no script from the tree writable by fleet and installs nothing it
# will load (systemd unit, nginx vhost, /usr/local/sbin script) from that tree.
#
# CALLING CONVENTION (SSM command list of .github/workflows/deploy.yml):
#   1. fetch root's BARE MIRROR /var/lib/fleet-deploy/Fleet_server.git
#      (root:root 700) from GitHub at the pushed SHA, then `cat-file -e`;
#   2. install THIS script from that mirror, as root:
#        git --git-dir=$M show $SHA:deploy/fleet-deploy-privileged.sh \
#          > /usr/local/sbin/fleet-app-deploy-privileged.sh   (root:root 0755)
#   3. run it as root, BEFORE the bundle is extracted:
#        /usr/local/sbin/fleet-app-deploy-privileged.sh <mirror> <sha>
#      <mirror> = /var/lib/fleet-deploy/Fleet_server.git, <sha> = full commit SHA.
#   Exit 0 = everything in place; non-zero = stop the deploy (the old code keeps
#   running). It does NOT restart the app units: the pipeline does, after
#   ssm-deploy.sh.
# Everything it installs is read from the mirror (`git show $SHA:...`), never
# from the bundle that fleet extracts.
#
# What it does, idempotently (a second run with no change installs nothing):
#   - /srv/fleet (root:root 755) and /srv/fleet/Fleet_server (fleet:fleet 750),
#     where the pipeline then extracts the bundle (plan 3, D1);
#   - /usr/local/sbin/fleet-app-env-fetch.sh (root:root 0755);
#   - the four fleet-app-* units (0644; daemon-reload only if one changed);
#   - fleet-app-env-fetch started (first deploy: writes /run/fleet/.env), checked;
#   - fleet-app-gunicorn / -celery / -celery-beat enabled at boot;
#   - the nginx vhost, with a .deploybak copy and a rollback if `nginx -t` fails.
set -euo pipefail
umask 022   # system files: 0644 units, 0755 scripts, readable by systemd/nginx

M="${1:?usage: $0 <git-mirror> <sha>}"
SHA="${2:?usage: $0 <git-mirror> <sha>}"
blob() { git --git-dir="$M" show "$SHA:$1"; }

# Install blob $1 at $2 (root:root, mode $3) if it differs; returns 0 if changed.
# Called under `if` / `||`, where `set -e` does not apply: every failure exits
# explicitly, otherwise a missing blob would install an empty file.
install_blob() {
    local source="$1" target="$2" mode="$3" new
    new=$(mktemp) || exit 1
    if ! blob "$source" > "$new" || [ ! -s "$new" ]; then
        echo "ERROR: $source missing or empty at commit $SHA" >&2
        rm -f "$new"
        exit 1
    fi
    if cmp -s "$new" "$target"; then
        rm -f "$new"
        return 1
    fi
    install -o root -g root -m "$mode" "$new" "$target" || exit 1
    rm -f "$new"
    echo "  installed: $target"
}

# --- App directories (plan 3, D1) ----------------------------------------------
# The fleet user is created once by the box bootstrap (deploy/README.md).
id fleet >/dev/null 2>&1 || { echo "ERROR: user fleet missing (deploy/README.md, bootstrap)" >&2; exit 1; }
install -d -o root -g root -m 755 /srv/fleet
install -d -o fleet -g fleet -m 750 /srv/fleet/Fleet_server

# --- Config script (root, run by fleet-app-env-fetch) ---------------------------
install_blob deploy/fetch-env-from-ssm.sh /usr/local/sbin/fleet-app-env-fetch.sh 0755 || true

# --- systemd units -------------------------------------------------------------
units_changed=0
for unit in fleet-app-env-fetch.service fleet-app-gunicorn.service \
            fleet-app-celery.service fleet-app-celery-beat.service; do
    if install_blob "deploy/systemd/$unit" "/etc/systemd/system/$unit" 0644; then
        units_changed=1
    fi
done
if [ "$units_changed" = 1 ]; then
    systemctl daemon-reload
fi
# start (not restart): an .env already in place is not re-read at every deploy;
# a secret rotation is manual (deploy/README.md).
systemctl enable --now fleet-app-env-fetch.service
test -s /run/fleet/.env || { echo "ERROR: /run/fleet/.env empty (SSM /fleet/prod?)" >&2; exit 1; }
systemctl enable fleet-app-gunicorn.service fleet-app-celery.service fleet-app-celery-beat.service

# --- nginx ---------------------------------------------------------------------
# Rate-limit zones live in the vhost itself (http context, `fleet_` prefix,
# FoxHouse style), so one file is installed and rolled back as a whole.
VHOST=/etc/nginx/sites-available/fleet-api.foxugly.com
ENABLED=/etc/nginx/sites-enabled/fleet-api.foxugly.com
new=$(mktemp)
if ! blob deploy/nginx/fleet-api.foxugly.com > "$new" || [ ! -s "$new" ]; then
    echo "ERROR: deploy/nginx/fleet-api.foxugly.com missing or empty at commit $SHA" >&2
    rm -f "$new"
    exit 1
fi
if ! cmp -s "$new" "$VHOST"; then
    rm -f "$VHOST.deploybak"
    if [ -e "$VHOST" ]; then
        cp -a "$VHOST" "$VHOST.deploybak"
    fi
    install -o root -g root -m 0644 "$new" "$VHOST"
    ln -sfn "$VHOST" "$ENABLED"
    if nginx -t; then
        systemctl reload nginx
        echo "  nginx vhost installed and reloaded"
    else
        echo "ERROR: nginx -t rejects the vhost; back to the previous version." >&2
        if [ -e "$VHOST.deploybak" ]; then
            mv -f "$VHOST.deploybak" "$VHOST"
        else
            rm -f "$ENABLED" "$VHOST"
        fi
        nginx -t && systemctl reload nginx
        rm -f "$new"
        exit 1
    fi
fi
rm -f "$new"

echo "[privileged] Fleet_server ${SHA:0:7}: units, config script and vhost up to date."
