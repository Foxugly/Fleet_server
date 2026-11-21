#!/usr/bin/env bash
# Fleet SSM parameters (/fleet/prod/* and /fleet-frontend/prod/*), to paste into AWS
# CloudShell (eu-west-1). Copied from FoxHouse (deploy/ssm-cloudshell.sh).
#
# CALLING CONVENTION:
#   bash ssm-cloudshell.sh <db-password-file>   # the file copied from the box
#   bash ssm-cloudshell.sh -                    # DB password asked with no echo
# then answers the prompts (no echo for the tokens; Enter = keep / skip).
#
# - DB_PASSWORD: the one of the database created on the box
#   (`sudo cat /root/fleet-db-password`). A file or a no-echo prompt keeps it out
#   of the shell history. Delete the file afterwards (box and CloudShell).
# - SECRET_KEY and FLEET_MFA_ENCRYPTION_KEY are generated HERE (inside AWS) and are
#   NEVER replaced when they exist: a new MFA key makes every enrolled TOTP secret
#   unreadable (`manage.py reset_mfa` for everyone).
# - FLEET_GITHUB_READ_TOKEN (fine-grained, read-only, fleet repos) and
#   FLEET_SENTRY_TOKEN (org:read, project:read, event:read): no-echo prompts; an
#   empty answer keeps the current value (or leaves it unset on a first run).
# - SENTRY_DSN (fleet-backend, then fleet-frontend): String (a DSN is public);
#   empty = skipped.
# - Do NOT set SENTRY_RELEASE: the version comes from the RELEASE file written by
#   CI, and the deploy's /health/ check compares it to the SHA.
# - No secret value is printed; the end lists names and types only.
#
# Re-runnable: the non-secret parameters are rewritten identically.
set -euo pipefail
umask 027

REGION=eu-west-1
P=/fleet/prod
F=/fleet-frontend/prod

PASSWORD_SOURCE="${1:?usage: $0 <db-password-file> | -}"
if [ "$PASSWORD_SOURCE" = "-" ]; then
  read -r -s -p "DB_PASSWORD (output of sudo cat /root/fleet-db-password): " DB_PASSWORD; echo
else
  DB_PASSWORD=$(tr -d '\r\n' < "$PASSWORD_SOURCE")
fi
[ "${#DB_PASSWORD}" -ge 20 ] || { echo "Password too short: wrong copy-paste?" >&2; exit 1; }

read -r -s -p "FLEET_GITHUB_READ_TOKEN (Enter = keep current): " GITHUB_TOKEN; echo
read -r -s -p "FLEET_SENTRY_TOKEN (Enter = keep current): " SENTRY_TOKEN; echo
read -r -p "SENTRY_DSN of fleet-backend (Enter = skip): " BACKEND_DSN
read -r -p "SENTRY_DSN of fleet-frontend (Enter = skip): " FRONTEND_DSN

put() {  # put <full name> <type> <value>
  aws ssm put-parameter --region "$REGION" --name "$1" --type "$2" --value "$3" \
    --overwrite --no-cli-pager >/dev/null
  echo "  $1 ($2)"
}

exists() {
  aws ssm get-parameter --region "$REGION" --name "$1" >/dev/null 2>&1
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
put "$P/DB_PASSWORD" SecureString "$DB_PASSWORD"; unset DB_PASSWORD
if [ -n "$GITHUB_TOKEN" ]; then
  put "$P/FLEET_GITHUB_READ_TOKEN" SecureString "$GITHUB_TOKEN"
else
  echo "  $P/FLEET_GITHUB_READ_TOKEN: not given, unchanged"
fi
unset GITHUB_TOKEN
if [ -n "$SENTRY_TOKEN" ]; then
  put "$P/FLEET_SENTRY_TOKEN" SecureString "$SENTRY_TOKEN"
else
  echo "  $P/FLEET_SENTRY_TOKEN: not given, unchanged"
fi
unset SENTRY_TOKEN
put "$P/STATE"                String "PROD"
put "$P/ALLOWED_HOSTS"        String "fleet-api.foxugly.com"
put "$P/CSRF_TRUSTED_ORIGINS" String "https://fleet.foxugly.com"
put "$P/CORS_ALLOWED_ORIGINS" String "https://fleet.foxugly.com"
put "$P/DB_ENGINE"            String "postgresql"
put "$P/DB_HOST"              String "127.0.0.1"
put "$P/DB_PORT"              String "5432"
put "$P/DB_NAME"              String "fleet"
put "$P/DB_USER"              String "fleet"
put "$P/REDIS_URL"            String "redis://127.0.0.1:6379/12"
put "$P/SENTRY_ENVIRONMENT"   String "production"
put "$P/FLEET_SENTRY_ORG"     String "foxugly-srl"
if [ -n "$BACKEND_DSN" ]; then
  put "$P/SENTRY_DSN" String "$BACKEND_DSN"
else
  echo "  (SENTRY_DSN not given: backend Sentry stays off; re-run with the DSN later)"
fi

# The SPA's runtime configuration: all String (public, served in index.html).
echo "Writing $F:"
put "$F/API_BASE_URL" String "https://fleet-api.foxugly.com"
put "$F/SENTRY_ENV"   String "production"
if [ -n "$FRONTEND_DSN" ]; then
  put "$F/SENTRY_DSN" String "$FRONTEND_DSN"
else
  echo "  (SENTRY_DSN not given: SPA Sentry stays off)"
fi

for prefix in "$P" "$F"; do
  echo
  echo "Content of $prefix (names and types only):"
  aws ssm get-parameters-by-path --region "$REGION" --path "$prefix" --recursive \
    --query 'Parameters[].[Name,Type]' --output table --no-cli-pager
done
