#!/usr/bin/env bash
#
# Fleet: fetch the configuration from SSM Parameter Store into /run.
#
# Copied from FoxHouse (deploy/fetch-env-from-ssm.sh), same contract. Run at boot
# by fleet-app-env-fetch.service (root, oneshot), it writes /run/fleet/.env
# (tmpfs: nothing on disk, nothing in an EBS snapshot) atomically (.tmp then mv)
# and refuses to replace a valid .env with an empty one.
#
# CALLING CONVENTION: no argument, run as root by fleet-app-env-fetch.service only.
# Installed by the deploy at /usr/local/sbin/fleet-app-env-fetch.sh (root:root
# 0755), from root's bare mirror at the pushed SHA, never run from the tree
# writable by fleet (OPERATIONS.md §3.10).
#
# Permissions (plan 3, Review Focus 3): /run/fleet is 750 root:fleet and the
# .env is 640 fleet:fleet. Only fleet (and root) can read it; the group that
# nginx and the django sites share is kept out on purpose.
set -euo pipefail
umask 027

PREFIX="/fleet/prod"
REGION="eu-west-1"
OUT_DIR="/run/fleet"
OUT_FILE="$OUT_DIR/.env"
OWNER="fleet:fleet"

# The instance role, never the static keys in /root/.aws (certbot-route53, no SSM
# right: OPERATIONS.md §3.19, gotcha 2).
export AWS_SHARED_CREDENTIALS_FILE=/dev/null AWS_CONFIG_FILE=/dev/null

echo "[fetch-env] prefix=$PREFIX region=$REGION out=$OUT_FILE"

mkdir -p "$OUT_DIR"
chown root:fleet "$OUT_DIR"
chmod 750 "$OUT_DIR"

TMP_FILE="$OUT_FILE.tmp"
: > "$TMP_FILE"
chmod 640 "$TMP_FILE"

OUT=$(aws ssm get-parameters-by-path \
  --region "$REGION" \
  --path "$PREFIX" \
  --recursive \
  --with-decryption \
  --output json)

if ! python3 -c '
import json, sys
prefix, out_path, payload = sys.argv[1:4]
data = json.loads(payload)
n = 0
with open(out_path, "a", encoding="utf-8") as f:
    for p in data.get("Parameters", []):
        name = p["Name"]
        key = name[len(prefix) + 1:] if name.startswith(prefix + "/") else name.rsplit("/", 1)[-1]
        value = p["Value"]
        if "\n" in value or "\r" in value:
            print(f"ERROR: the value of {key} contains a line break", file=sys.stderr)
            sys.exit(2)
        f.write(f"{key}={value}\n")
        n += 1
print(f"[fetch-env] {n} entries written to the temporary file", file=sys.stderr)
' "$PREFIX" "$TMP_FILE" "$OUT"; then
  rm -f "$TMP_FILE"
  exit 2
fi

COUNT=$(grep -c '=' "$TMP_FILE" || true)
if [ "$COUNT" -eq 0 ]; then
  echo "ERROR: no parameter under $PREFIX; the current .env is kept" >&2
  rm -f "$TMP_FILE"
  exit 3
fi

chown "$OWNER" "$TMP_FILE"
chmod 640 "$TMP_FILE"
mv -f "$TMP_FILE" "$OUT_FILE"
echo "[fetch-env] $OUT_FILE ready ($COUNT entries, owner $OWNER, mode 0640)"
