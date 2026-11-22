#!/usr/bin/env bash
# Fleet SSM parameters (/fleet/prod/* and /fleet-frontend/prod/*), to paste into AWS
# CloudShell (eu-west-1). Copied from FoxHouse (deploy/ssm-cloudshell.sh).
#
# CALLING CONVENTION:
#   bash ssm-cloudshell.sh <db-password-file>   # the file copied from the box
#   bash ssm-cloudshell.sh [-]                  # DB password asked with no echo
# then answers the prompts (no echo for the tokens; Enter = keep / skip).
#
# - DB_PASSWORD: the one of the database created on the box
#   (`sudo cat /root/fleet-db-password`). A file or a no-echo prompt keeps it out
#   of the shell history. Delete the file afterwards (box and CloudShell).
#   At the prompt, Enter keeps /fleet/prod/DB_PASSWORD when it exists (a re-run to
#   add the DSNs needs no password); on a first run it is required.
# - SECRET_KEY and FLEET_MFA_ENCRYPTION_KEY are generated HERE (inside AWS) and are
#   NEVER replaced when they exist: a new MFA key makes every enrolled TOTP secret
#   unreadable (`manage.py reset_mfa` for everyone).
# - FLEET_GITHUB_READ_TOKEN (fine-grained, read-only, fleet repos) and
#   FLEET_SENTRY_TOKEN (org:read, project:read, event:read): no-echo prompts; an
#   empty answer keeps the current value (or leaves it unset on a first run).
# - SENTRY_DSN (fleet-backend, then fleet-frontend): String (a DSN is public);
#   empty = unchanged (unset on a first run).
# - Do NOT set SENTRY_RELEASE: the version comes from the RELEASE file written by
#   CI, and the deploy's /health/ check compares it to the SHA.
# - No secret value is printed; the end lists names and types only.
#
# Re-runnable: a re-run only writes what is missing or different (the String
# values are compared first), e.g. the DSNs once the Sentry projects exist.
set -euo pipefail
umask 027

REGION=eu-west-1
P=/fleet/prod
F=/fleet-frontend/prod

exists() {
  aws ssm get-parameter --region "$REGION" --name "$1" >/dev/null 2>&1
}

PASSWORD_SOURCE="${1:--}"
if [ "$PASSWORD_SOURCE" = "-" ]; then
  read -r -s -p "DB_PASSWORD (sudo cat /root/fleet-db-password; Enter = keep current): " DB_PASSWORD; echo
else
  DB_PASSWORD=$(tr -d '\r\n' < "$PASSWORD_SOURCE")
fi
if [ -z "$DB_PASSWORD" ]; then
  exists "$P/DB_PASSWORD" \
    || { echo "$P/DB_PASSWORD does not exist yet: the password is required." >&2; exit 1; }
elif [ "${#DB_PASSWORD}" -lt 20 ]; then
  echo "Password too short: wrong copy-paste?" >&2; exit 1
fi

read -r -s -p "FLEET_GITHUB_READ_TOKEN (Enter = keep current): " GITHUB_TOKEN; echo
read -r -s -p "FLEET_SENTRY_TOKEN (Enter = keep current): " SENTRY_TOKEN; echo
read -r -p "SENTRY_DSN of fleet-backend (Enter = keep / skip): " BACKEND_DSN
read -r -p "SENTRY_DSN of fleet-frontend (Enter = keep / skip): " FRONTEND_DSN

put() {  # put <full name> <type> <value>
  aws ssm put-parameter --region "$REGION" --name "$1" --type "$2" --value "$3" \
    --overwrite --no-cli-pager >/dev/null
  echo "  $1 ($2)"
}

# put_string <full name> <value>: a String, written only when missing or different.
put_string() {
  local current
  current=$(aws ssm get-parameter --region "$REGION" --name "$1" \
    --query Parameter.Value --output text 2>/dev/null) || current=""
  if [ -n "$current" ] && [ "$current" = "$2" ]; then
    echo "  $1 unchanged"
  else
    put "$1" String "$2"
  fi
}

# put_secret <name> <value>: a SecureString; an empty value keeps the current one.
put_secret() {
  if [ -n "$2" ]; then
    put "$P/$1" SecureString "$2"
  elif exists "$P/$1"; then
    echo "  $P/$1 already exists: kept"
  else
    echo "  $P/$1: not given, still unset"
  fi
}

# generate_once <name>: a random SecureString, never replaced when present.
generate_once() {
  local value
  if exists "$P/$1"; then
    echo "  $P/$1 already exists: kept"
  else
    value=$(python3 -c "import secrets; print(secrets.token_urlsafe(64))")
    aws ssm put-parameter --region "$REGION" --name "$P/$1" --type SecureString \
      --value "$value" --no-cli-pager >/dev/null
    echo "  $P/$1 (SecureString, generated)"
  fi
}

echo "Writing $P:"
generate_once SECRET_KEY
generate_once FLEET_MFA_ENCRYPTION_KEY
put_secret DB_PASSWORD "$DB_PASSWORD"; unset DB_PASSWORD
put_secret FLEET_GITHUB_READ_TOKEN "$GITHUB_TOKEN"; unset GITHUB_TOKEN
put_secret FLEET_SENTRY_TOKEN "$SENTRY_TOKEN"; unset SENTRY_TOKEN
put_string "$P/STATE"                "PROD"
put_string "$P/ALLOWED_HOSTS"        "fleet-api.foxugly.com"
put_string "$P/CSRF_TRUSTED_ORIGINS" "https://fleet.foxugly.com"
put_string "$P/CORS_ALLOWED_ORIGINS" "https://fleet.foxugly.com"
put_string "$P/DB_ENGINE"            "postgresql"
put_string "$P/DB_HOST"              "127.0.0.1"
put_string "$P/DB_PORT"              "5432"
put_string "$P/DB_NAME"              "fleet"
put_string "$P/DB_USER"              "fleet"
put_string "$P/REDIS_URL"            "redis://127.0.0.1:6379/12"
put_string "$P/SENTRY_ENVIRONMENT"   "production"
put_string "$P/FLEET_SENTRY_ORG"     "foxugly-srl"
if [ -n "$BACKEND_DSN" ]; then
  put_string "$P/SENTRY_DSN" "$BACKEND_DSN"
elif exists "$P/SENTRY_DSN"; then
  echo "  $P/SENTRY_DSN already exists: kept"
else
  echo "  (SENTRY_DSN not given: backend Sentry stays off; re-run with the DSN later)"
fi

# The SPA's runtime configuration: all String (public, served in index.html).
echo "Writing $F:"
put_string "$F/API_BASE_URL" "https://fleet-api.foxugly.com"
put_string "$F/SENTRY_ENV"   "production"
if [ -n "$FRONTEND_DSN" ]; then
  put_string "$F/SENTRY_DSN" "$FRONTEND_DSN"
elif exists "$F/SENTRY_DSN"; then
  echo "  $F/SENTRY_DSN already exists: kept"
else
  echo "  (SENTRY_DSN not given: SPA Sentry stays off)"
fi

for prefix in "$P" "$F"; do
  echo
  echo "Content of $prefix (names and types only):"
  aws ssm get-parameters-by-path --region "$REGION" --path "$prefix" --recursive \
    --query 'Parameters[].[Name,Type]' --output table --no-cli-pager
done
