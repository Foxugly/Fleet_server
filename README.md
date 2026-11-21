# Fleet_server

Django/DRF backend that monitors the Foxugly fleet. Celery beat collects, on fixed
intervals, GitHub (CI, Dependabot, PRs), Sentry (unresolved issues), per-vhost health
and TLS expiry, the box itself (disk, apt, services) and dependency locks. A read API
serves the result to a future SPA, behind password + mandatory TOTP.

## Layout

- `config/`: settings, urls, `/health/`, Sentry init, Celery app.
- `apps/api_auth`: JWT login, TOTP enrolment/verify, refresh cookie, throttles;
  commands `create_operator`, `reset_mfa`.
- `apps/inventory`: loader for `inventory/fleet.yaml` (the list of sites).
- `apps/collect`: `sources/` (pure collectors: github, sentry, health, box, deps),
  `store.py` (snapshots, 30-day purge), `tasks.py` (Celery tasks), `intervals.py`.
- `apps/api`: read endpoints under `/api/v1/` (`overview/`, `sites/<slug>/`, `box/`,
  `refresh/<source>/`, `schema/`) and the colour rules in `status.py`.

Auth endpoints live under `/api/v1/auth/` (login, mfa/setup, mfa/verify, refresh, logout, me).

The OpenAPI schema is committed as `openapi-schema.yaml` (the SPA generates its client from it).
Changing an endpoint = regenerate it: `python manage.py spectacular --file openapi-schema.yaml --validate`
(CI fails when it is stale).

## Local setup (Windows)

    py -3.12 -m venv .venv
    .venv/Scripts/python.exe -m pip install --require-hashes --no-deps -r requirements.txt
    .venv/Scripts/python.exe -m pip install -r requirements-dev.txt
    copy .env.example .env
    .venv/Scripts/python.exe manage.py migrate
    .venv/Scripts/python.exe manage.py create_operator <username>
    .venv/Scripts/python.exe manage.py runserver

If `py -3.12` fails (broken Microsoft Store Python), use `py -3.14 -m venv .venv`: the
lock is universal and CI on 3.12 is the authority.

Nothing loads `.env` automatically: settings read only the process environment, so
without this step `manage.py` refuses to start (`STATE` is required). Load the file into the
current shell before `migrate`, `runserver`, `makemigrations` or `celery`.

PowerShell:

    Get-Content .env | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object { $k, $v = $_ -split '=', 2; Set-Item "env:$($k.Trim())" $v.Trim() }

Git Bash:

    set -a; . ./.env; set +a

SQLite is used when `DB_ENGINE` is empty.

The operator enrols TOTP through the API at first sign-in (`POST /api/v1/auth/login/`,
then `mfa/setup/` and `mfa/verify/`). `manage.py reset_mfa <username>` lifts a lockout
and forces re-enrolment.

Collectors and beat locally (needs Redis; Windows requires the solo pool):

    .venv/Scripts/python.exe -m celery -A config worker -B --pool=solo -l info

## Tests

    .venv/Scripts/python.exe -m pytest -q --basetemp=.pytest_tmp
    .venv/Scripts/python.exe -m ruff check .
    .venv/Scripts/python.exe manage.py makemigrations --check --dry-run

pytest sets `STATE=DEV` itself (`config.settings_test`); `manage.py` needs `.env` loaded
(or `STATE=DEV` in the shell).

## Environment variables

Defaults in parentheses; all are listed in `.env.example`.

General
- `STATE` (required, no default): `DEV` or `PROD`; missing or anything else refuses to start
  (`.env.example` sets DEV; pytest uses `config.settings_test`, which sets DEV). `PROD` enables
  Sentry reporting (with `SENTRY_DSN` set) and requires `SECRET_KEY`, `ALLOWED_HOSTS`, `REDIS_URL`.
- `DEBUG` (true in DEV): Django debug, only possible in DEV (ignored in PROD); when off,
  HTTPS/HSTS/secure cookies apply.
- `SECRET_KEY`: required in PROD; in DEV, unset means a random key per process (set it in a
  dev `.env` to keep tokens and enrolled TOTP across restarts).
- `ALLOWED_HOSTS` (DEV only: 127.0.0.1,localhost,testserver), `CSRF_TRUSTED_ORIGINS`: comma lists.
- `LOG_LEVEL` (INFO).

Database
- `DB_ENGINE`: `postgresql` selects PostgreSQL, empty means SQLite.
- `DB_NAME` (fleet), `DB_USER` (fleet), `DB_PASSWORD`, `DB_HOST` (127.0.0.1),
  `DB_PORT` (5432), `DB_CONN_MAX_AGE` (60).

Auth
- `FLEET_MFA_ENCRYPTION_KEY`: derives the key encrypting TOTP secrets (else `SECRET_KEY`);
  changing it requires `reset_mfa` for every user.
- `FLEET_MFA_LOCKOUT_SECONDS` (86400): lockout after too many wrong codes.
- `JWT_ACCESS_MINUTES` (15), `JWT_REFRESH_DAYS` (7).
- `CORS_ALLOWED_ORIGINS` (localhost:4200): explicit origins, `*` is refused; credentials and
  the `X-Auth-Mode` header are allowed (cookie-mode SPA).
- `AUTH_REFRESH_COOKIE_NAME` (fleet_refresh), `AUTH_REFRESH_COOKIE_SECURE` (true).
- Throttles: `THROTTLE_ANON` (60/min), `THROTTLE_USER` (300/min), `THROTTLE_LOGIN` (10/hour),
  `THROTTLE_LOGIN_USERNAME` (20/hour), `THROTTLE_MFA` (5/min), `THROTTLE_MFA_USER` (5/min),
  `THROTTLE_REFRESH` (6/min, manual refresh).

Redis / Celery
- `REDIS_URL` (redis://127.0.0.1:6379/12): broker and results; Redis db 12 is Fleet's. When set
  explicitly it also backs the cache (throttle counters, prefix `fleet-cache`); required in
  PROD, unset in DEV = per-process LocMem.

Collectors
- `FLEET_GITHUB_READ_TOKEN`: read-only token; must cover every repo in the inventory.
- `FLEET_SENTRY_TOKEN`: read-only Sentry token.
- `FLEET_SENTRY_ORG` (foxugly-srl), `FLEET_SENTRY_API` (https://de.sentry.io/api/0/).
- `FLEET_INVENTORY_PATH` (inventory/fleet.yaml).

Sentry (self-reporting)
- `SENTRY_DSN`: reporting needs it AND (`STATE=PROD` or `SENTRY_ENABLE`). `SENTRY_ENABLE` (report outside PROD), `SENTRY_ENVIRONMENT` (STATE),
  `SENTRY_RELEASE`, `SENTRY_TRACES_SAMPLE_RATE` (0).

## Inventory

Edit `inventory/fleet.yaml`: one entry per site with `slug`, `repos`, `vhosts`,
`health_path`, `sentry_projects`, `python_locks` / `npm_locks` (`Owner/Repo:path`),
`deployed`, `actions_allowed`. A new repo must also be covered by the GitHub read token
(add it to the token's repository selection), otherwise its collection errors.

## Collection intervals

`apps/collect/intervals.py`: health 5 min, github 15 min, sentry 15 min, box 1 h,
deps 6 h; snapshots older than 30 days are purged daily. A snapshot older than 3x its
interval is shown as stale.

## Colour rules

Pure logic in `apps/api/status.py`: red for a failing health check, TLS verification
failure or under 14 days left, disk at 90% or more, critical/high findings; orange for
other findings; green when clean; `stale` and `error` when data is old or collection failed.

## Dependencies

Edit `requirements.in`, then regenerate the lock:

    uv pip compile requirements.in --generate-hashes --universal --python-version 3.12 -o requirements.txt

## Security notes

- Tokens are read-only. Fleet never reads other sites' virtualenvs nor `/etc/letsencrypt`
  (TLS expiry is probed over the network, locks are fetched from GitHub).
- In prod it runs as the unprivileged user `fleet` (plan 3); secrets come from SSM
  `/fleet/prod` into `/run/fleet/.env`.
- Every API route except login/MFA/refresh requires a JWT; TOTP is mandatory.
