# CLAUDE.md

Fleet_server: Django/DRF backend monitoring the Foxugly fleet (GitHub, Sentry, health/TLS,
box, dependency locks), Celery beat, read API behind password + TOTP. Fleet-wide ops rules:
`~/.claude/CLAUDE.md` and `OPERATIONS.md`. Full docs: `README.md`.

## Layout

- `config/`: settings, urls, health, sentry, celery.
- `apps/api_auth` (JWT + TOTP, `create_operator`, `reset_mfa`), `apps/inventory` (`App` rows,
  loader, validation), `apps/collect` (`sources/` pure collectors, `store.py`, `tasks.py`,
  `intervals.py`), `apps/api` (read endpoints, colour rules in `status.py`).

## Commands (Windows)

- Tests: `.venv/Scripts/python.exe -m pytest -q --basetemp=.pytest_tmp`
- Lint: `.venv/Scripts/python.exe -m ruff check .`; migrations: `manage.py makemigrations --check --dry-run`
  (needs `STATE=DEV` in the shell, see `.env` below)
- Install: `pip install --require-hashes --no-deps -r requirements.txt` then `pip install -r requirements-dev.txt`
- Lock: `uv pip compile requirements.in --generate-hashes --universal --python-version 3.14 -o requirements.txt`
  (CI and prod run 3.14)
- Worker + beat locally: `python -m celery -A config worker -B --pool=solo -l info` (solo needed on Windows)
- Nothing loads `.env`: load it into the shell first (PowerShell snippet in README, Git Bash `set -a; . ./.env; set +a`).
- First operator: `manage.py create_operator <username>`, TOTP enrolment via the API.

## Conventions

- Settings read env through `_env()` in `config/settings.py`; every variable is
  documented in `.env.example`; no secret is committed.
- `STATE` (DEV|PROD) is required, no default; pytest runs on `config.settings_test` (sets
  DEV). PROD also requires `SECRET_KEY`, `ALLOWED_HOSTS`, `REDIS_URL` and `CORS_ALLOWED_ORIGINS` (shared throttle cache).
- `DB_ENGINE=postgresql` selects PostgreSQL, otherwise SQLite. Redis db 12.
- CI uses a cached PostgreSQL 16 image as steps, never a `services:` container.
- `openapi-schema.yaml` is committed: changing an endpoint = regenerate it with
  `manage.py spectacular --file openapi-schema.yaml --validate` (test + CI check it).
- Adding a site: an `App` row, via the API / Fleet's Apps page (no YAML file any more); the
  GitHub read token must cover the new repo. `get_site` returns active apps only.
- Tests start with the 17 apps imported by migration 0002: use `tests.helpers.make_apps(yaml)`
  to replace them with a deterministic inventory.
- Deps: an item another installed package holds back carries `blocked_by` (npm peers via
  `sources/npm_semver.py`, Python `requires_dist` cached forever); it does not count in the
  cell (green + `blocked_updates` note when all are blocked). Details: README « Colour rules ».
- Holds: an upper bound in the lock's sibling `requirements.in` (`<`, `<=`, `~=`, `==X.*`, `!=`
  naming the latest; never a plain `==X.Y.Z`) excluding the latest makes the item held: first
  blocker `kind: hold` with the line's comment as `note` (untrusted, capped), `held_updates` note.
- Collectors are pure functions (token in, dict out); tokens are read-only; never read other
  sites' venvs or `/etc/letsencrypt`. Prod runs as unprivileged user `fleet` (plan 3).
