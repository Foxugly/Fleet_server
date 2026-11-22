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

# The decrypted payload goes from aws to python through a PIPE: never in a shell
# variable, never in argv (/proc/<pid>/cmdline is world-readable). pipefail makes
# an aws failure fail the pipeline.
# Python refuses, before writing anything (the current .env is then kept):
#   - a key that is not an environment name (^[A-Z][A-Z0-9_]*$), or one that
#     steers the shell, the loader or Python (PATH, LD_*, PYTHON*, BASH_ENV, ENV,
#     IFS, HOME, SHELL);
#   - a value holding a line break, a quote or a backslash: systemd's
#     EnvironmentFile would unquote or unescape it, the literal loader of
#     ssm-deploy.sh would not, and the two must read the same value.
if ! aws ssm get-parameters-by-path \
  --region "$REGION" \
  --path "$PREFIX" \
  --recursive \
  --with-decryption \
  --output json | python3 -c '
import json, re, sys
prefix, out_path = sys.argv[1:3]
data = json.load(sys.stdin)
key_re = re.compile(r"[A-Z][A-Z0-9_]*\Z")
denied = {"PATH", "BASH_ENV", "ENV", "IFS", "HOME", "SHELL"}
bad_chars = ("\n", "\r", chr(34), chr(39), chr(92))
lines = []
for p in data.get("Parameters", []):
    name = p["Name"]
    key = name[len(prefix) + 1:] if name.startswith(prefix + "/") else name.rsplit("/", 1)[-1]
    value = p["Value"]
    if not key_re.match(key) or key in denied or key.startswith(("LD_", "PYTHON")):
        sys.exit(f"ERROR: refused key {key!r} under {prefix}")
    if any(c in value for c in bad_chars):
        sys.exit(f"ERROR: the value of {key} holds a line break, a quote or a backslash")
    lines.append(f"{key}={value}\n")
with open(out_path, "a", encoding="utf-8", newline="\n") as f:
    f.writelines(lines)
print(f"[fetch-env] {len(lines)} entries written to the temporary file", file=sys.stderr)
' "$PREFIX" "$TMP_FILE"; then
  echo "ERROR: fetch or validation failed; the current .env is kept" >&2
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
