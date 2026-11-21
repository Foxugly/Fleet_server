# Fleet_server on the box (fleet-api.foxugly.com)

Backend of the Fleet dashboard: gunicorn on `127.0.0.1:8014`, a Celery worker and Celery beat,
running as the dedicated Unix user **`fleet`** (not `django`, not in `www-data`). The code
lives in **`/srv/fleet/Fleet_server`** (`fleet:fleet 750`), outside `/var/www/django_websites`,
so no `django` site can read it or replace it (plan 3, D1).

| What | Where on the box | Source in this repo |
|---|---|---|
| App tree (bundle, `.venv`, `.venv.prev`) | `/srv/fleet/Fleet_server` | CI bundle: `git archive` + `RELEASE` |
| Units | `/etc/systemd/system/fleet-app-{env-fetch,gunicorn,celery,celery-beat}.service` | `deploy/systemd/` |
| Config script (root) | `/usr/local/sbin/fleet-app-env-fetch.sh` | `deploy/fetch-env-from-ssm.sh` |
| Privileged deploy step (root) | `/usr/local/sbin/fleet-app-deploy-privileged.sh` | `deploy/fleet-deploy-privileged.sh` |
| App deploy step (fleet) | root-owned temp copy, per deploy | `deploy/ssm-deploy.sh` |
| vhost (+ rate-limit zone `fleet_auth`) | `/etc/nginx/sites-available/fleet-api.foxugly.com` | `deploy/nginx/fleet-api.foxugly.com` |
| Configuration | SSM `/fleet/prod/*` → `/run/fleet/.env` (`640 fleet:fleet`, dir `750 root:fleet`) | `deploy/ssm-cloudshell.sh` |
| Root's mirror | `/var/lib/fleet-deploy/Fleet_server.git` (`root:root 700`) | fetched by the pipeline |

Units are prefixed `fleet-app-` because the `fleet-` prefix belongs to box-wide units
(`fleet-db-backup.*`, `/var/lib/fleet-deploy`; plan 3, D2). Everything else stays `fleet`:
user, PostgreSQL role and database, SSM `/fleet/prod`, Redis db 12, IAM `fleet-deploy`.

## How a deploy runs

A push to `main` runs CI, then `.github/workflows/deploy.yml` (OIDC role `fleet-deploy`):

1. CI builds the bundle (`git archive` + `RELEASE` = SHA) and uploads it to
   `s3://foxugly-deploy/builds/fleet-server/<sha>.tar.gz`.
2. SSM, as root (`set -eu` first): fetch the mirror at the SHA, install
   `fleet-deploy-privileged.sh` from it and run it with `<mirror> <sha>`. It creates
   `/srv/fleet/Fleet_server`, installs the config script, the units (daemon-reload only on a
   change) and the vhost (`nginx -t`, rollback to `.deploybak` on failure), starts
   `fleet-app-env-fetch` and checks `/run/fleet/.env`.
3. Root downloads the bundle into a `mktemp -d`; `fleet` extracts it into
   `/srv/fleet/Fleet_server/.bundle-new`.
4. Root copies `ssm-deploy.sh` from the mirror into a root-owned temp file and runs
   `sudo -u fleet bash <tmp> /srv/fleet/Fleet_server/.bundle-new <sha>`: a fresh `.venv.new`
   (`python3.12 -m venv`, hashed lock, `pip check`), then with that venv and the new code
   `check --deploy` and only then `migrate`, then the swap of code and venv together
   (`.venv` → `.venv.prev`, `.venv.new` → `.venv`). A failure stops before the swap: the
   old code and the old `.venv` keep serving, `.venv.new` is removed, the workflow goes red.
   Units run `.venv/bin/python -m gunicorn` / `-m celery`: a renamed venv keeps stale
   console-script shebangs, so nothing calls `.venv/bin/<script>`.
5. Root restarts `fleet-app-gunicorn fleet-app-celery fleet-app-celery-beat`, checks
   `/health/` locally, then CI checks `https://fleet-api.foxugly.com/health/` returns the SHA.

Root never runs anything from `/srv/fleet/Fleet_server` and installs nothing from it
(OPERATIONS.md §3.10/§3.11). `fleet` has no sudo grant: root does every restart.

## One-time bootstrap (Task 6)

Every step touches production: do it with Renaud's go, in this order.

### 0. Preflight (read-only, on the box)

```bash
ss -lntp | grep -c ':8014 '                 # 0
sudo nginx -T 2>/dev/null | grep -c fleet-api  # 0
python3.12 --version                        # 3.12.x
redis-cli -n 12 dbsize                      # 0
id fleet                                    # "no such user" before step 3
```

### 1. DNS (admin, CloudShell)

```bash
ZONE=$(aws route53 list-hosted-zones-by-name --dns-name foxugly.com --query 'HostedZones[?Name==`foxugly.com.`].Id' --output text)
for h in fleet-api fleet; do
  aws route53 change-resource-record-sets --hosted-zone-id "$ZONE" --change-batch \
    '{"Comment":"Fleet","Changes":[{"Action":"CREATE","ResourceRecordSet":{"Name":"'"$h"'.foxugly.com","Type":"A","TTL":300,"ResourceRecords":[{"Value":"54.229.220.110"}]}}]}'
done
```

TLS: the wildcard `foxugly.com` lineage already covers both names.

### 2. IAM (admin, CloudShell, never from the box)

```bash
bash deploy/iam-cloudshell.sh
```

Creates or updates `fleet-deploy` and `fleet-frontend-deploy` (both `sub` forms, environment
`production`) and extends `SsmReadFleetAppConfig` in `foxugly-fleet-app-config` with
`/fleet/prod` and `/fleet-frontend/prod`. The previous policy is saved to
`~/foxugly-fleet-app-config.<timestamp>.json`. Copy the two printed ARNs for step 6.

### 3. Unix user and PostgreSQL (root, on the box)

```bash
# System user, no login shell, no home; its own group, NOT www-data.
adduser --system --group --no-create-home --home /nonexistent --shell /usr/sbin/nologin fleet
install -d -o root -g root -m 755 /srv/fleet
install -d -o fleet -g fleet -m 750 /srv/fleet/Fleet_server   # the deploy does it too

openssl rand -base64 33 | tr -d '\n' > /root/fleet-db-password && chmod 600 /root/fleet-db-password
sudo -u postgres psql <<SQL
CREATE ROLE fleet LOGIN PASSWORD '$(cat /root/fleet-db-password)';
CREATE DATABASE fleet OWNER fleet;
\c fleet
ALTER SCHEMA public OWNER TO fleet;
SQL
```

The weekly `fleet-db-backup.timer` (§3.23) picks the new database up by itself. Redis db 12
needs nothing.

### 4. SSM `/fleet/prod/*` and `/fleet-frontend/prod/*` (admin, CloudShell)

Copy `/root/fleet-db-password` into a CloudShell file, then:

```bash
bash deploy/ssm-cloudshell.sh ~/fleet-db-password && rm ~/fleet-db-password
sudo rm /root/fleet-db-password    # on the box, once the deploy works
```

The script generates `SECRET_KEY` and `FLEET_MFA_ENCRYPTION_KEY` inside AWS (never replaced),
writes `DB_PASSWORD`, asks (no echo) for `FLEET_GITHUB_READ_TOKEN` and `FLEET_SENTRY_TOKEN`,
and for the two Sentry DSNs (skipped when empty). It prints names and types only.

| `/fleet/prod/` | Type | Value |
|---|---|---|
| `STATE` | String | `PROD` |
| `SECRET_KEY`, `FLEET_MFA_ENCRYPTION_KEY` | SecureString | generated, never replaced |
| `DB_PASSWORD` | SecureString | step 3 |
| `FLEET_GITHUB_READ_TOKEN`, `FLEET_SENTRY_TOKEN` | SecureString | prompts |
| `ALLOWED_HOSTS` | String | `fleet-api.foxugly.com` |
| `CSRF_TRUSTED_ORIGINS`, `CORS_ALLOWED_ORIGINS` | String | `https://fleet.foxugly.com` |
| `DB_ENGINE` / `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` | String | `postgresql` / `127.0.0.1` / `5432` / `fleet` / `fleet` |
| `REDIS_URL` | String | `redis://127.0.0.1:6379/12` |
| `SENTRY_ENVIRONMENT` / `FLEET_SENTRY_ORG` | String | `production` / `foxugly-srl` |
| `SENTRY_DSN` | String | optional, project `fleet-backend` |

`/fleet-frontend/prod/`: `API_BASE_URL=https://fleet-api.foxugly.com`, `SENTRY_ENV=production`,
`SENTRY_DSN` (optional, project `fleet-frontend`), all String.
**Do not set** `SENTRY_RELEASE` (the `RELEASE` file carries it) nor `DB_CONN_MAX_AGE` (60 is in
the gunicorn unit only).

### 5. Sentry (Renaud, org owner)

Create `fleet-backend` and `fleet-frontend` in `foxugly-srl` (members cannot), then re-run
step 4 with the DSNs if they were skipped.

### 6. GitHub (`gh`)

Environment `production` (protected branches only) on `Foxugly/Fleet_server`, with secrets
`AWS_DEPLOY_ROLE_ARN` (the `fleet-deploy` ARN of step 2) and `EC2_INSTANCE_ID=i-0fe664678563bae5f`.
Required checks on `main`, Dependabot alerts and security updates, `DEPENDABOT_AUTOMERGE_TOKEN`.

### 7. First deploy

Merge to `main`. The pipeline creates the mirror, `/usr/local/sbin` scripts, units, vhost and
`.venv`; there is no separate bootstrap. If it fails on a missing prerequisite, fix it and
re-run the failed jobs. Deploy the API before the SPA (CORS, cookies).

### 8. After going live

```bash
curl -sS https://fleet-api.foxugly.com/health/        # 200, "version" = deployed SHA
systemctl show fleet-app-gunicorn fleet-app-celery fleet-app-celery-beat \
  -p NoNewPrivileges -p ProtectSystem -p PrivateTmp   # yes / full / yes
sudo stat -c '%a %U:%G %n' /run/fleet /run/fleet/.env # 750 root:fleet, 640 fleet:fleet
sudo -u www-data cat /run/fleet/.env                  # must fail: Permission denied
sudo find /srv/fleet/Fleet_server ! -type l \( -perm /020 -o -perm /004 \)   # empty
sudo -l -U fleet                                       # no grant
```

Then: create the operator (below), an UptimeRobot monitor on
`https://fleet-api.foxugly.com/health/` (keyword `"status": "ok"`), and the docs (OPERATIONS.md
§2, §3.4 port 8014 / Redis 12 taken, §3.11 repo table, §3.14).

## Operations

Commands that need the production configuration run as `fleet` through `systemd-run`, which
reads `/run/fleet/.env` the way the units do (never `source` it in a shell). Interactive ones
need a terminal: `ssh -t …`.

```bash
fleet_manage() {
  sudo systemd-run --quiet --pty --wait --collect --uid=fleet --gid=fleet \
    -p EnvironmentFile=/run/fleet/.env -p WorkingDirectory=/srv/fleet/Fleet_server \
    /srv/fleet/Fleet_server/.venv/bin/python manage.py "$@"
}
fleet_manage create_operator <username>   # once; prompts for the password (needs ssh -t)
fleet_manage reset_mfa <username>         # lost phone or lockout: re-enrol at next sign-in
```

**Secret rotation.** Change the value in SSM (`/fleet/prod/<NAME>`), then:

```bash
sudo systemctl restart fleet-app-env-fetch
sudo systemctl restart fleet-app-gunicorn fleet-app-celery fleet-app-celery-beat
```

A deploy does not re-read SSM (`enable --now` starts the oneshot only once).
`fleet-app-env-fetch` refuses (and keeps the current `.env`) a parameter whose name is not
`^[A-Z][A-Z0-9_]*$` or is `PATH`, `LD_*`, `PYTHON*`, `BASH_ENV`, `ENV`, `IFS`, `HOME`, `SHELL`,
and a value holding a line break, a quote or a backslash.
`SECRET_KEY` rotation signs every operator out. **Never rotate `FLEET_MFA_ENCRYPTION_KEY`**
without running `reset_mfa` for every user afterwards: the enrolled TOTP secrets become
unreadable.

**Logs.**

```bash
journalctl -u fleet-app-gunicorn -u fleet-app-celery -u fleet-app-celery-beat --since -1h
journalctl -u fleet-app-env-fetch -b
sudo tail -f /var/log/nginx/fleet-api-access.log /var/log/nginx/fleet-api-error.log
```

**Rollback.** Revert the commit on `main` and let the pipeline deploy it (migrations are not
reversed automatically). A broken vhost never stays installed: the privileged step restores
`/etc/nginx/sites-available/fleet-api.foxugly.com.deploybak` when `nginx -t` fails.
