# CLAUDE.md

Fleet_server: Django/DRF backend monitoring the Foxugly fleet (GitHub, Sentry, health/TLS,
box, dependency locks), Celery beat, read API behind password + TOTP. Fleet-wide ops rules:
`~/.claude/CLAUDE.md` and `OPERATIONS.md`. Full docs: `README.md`.

## Layout

- `config/`: settings, urls, health, sentry, celery.
- `apps/api_auth` (JWT + TOTP, `create_operator`, `reset_mfa`), `apps/inventory` (loads
  `inventory/fleet.yaml`), `apps/collect` (`sources/` pure collectors, `store.py`, `tasks.py`,
  `intervals.py`), `apps/api` (read endpoints, colour rules in `status.py`).

## Commands (Windows)

- Tests: `.venv/Scripts/python.exe -m pytest -q --basetemp=.pytest_tmp`
- Lint: `.venv/Scripts/python.exe -m ruff check .`; migrations: `manage.py makemigrations --check --dry-run`
  (needs `STATE=DEV` in the shell, see `.env` below)
- Install: `pip install --require-hashes --no-deps -r requirements.txt` then `pip install -r requirements-dev.txt`
- Lock: `uv pip compile requirements.in --generate-hashes --universal --python-version 3.12 -o requirements.txt`
  (3.14 is fine locally; CI on 3.12 is the authority)
- Worker + beat locally: `python -m celery -A config worker -B --pool=solo -l info` (solo needed on Windows)
- Nothing loads `.env`: load it into the shell first (PowerShell snippet in README, Git Bash `set -a; . ./.env; set +a`).
- First operator: `manage.py create_operator <username>`, TOTP enrolment via the API.

## Conventions

- Settings read env through `_env()` in `config/settings.py`; every variable is
  documented in `.env.example`; no secret is committed.
- `STATE` (DEV|PROD) is required, no default; pytest runs on `config.settings_test` (sets
  DEV). PROD also requires `SECRET_KEY`, `ALLOWED_HOSTS` and `REDIS_URL` (shared throttle cache).
- `DB_ENGINE=postgresql` selects PostgreSQL, otherwise SQLite. Redis db 12.
- CI uses a cached PostgreSQL 16 image as steps, never a `services:` container.
- Adding a site: edit `inventory/fleet.yaml`; the GitHub read token must cover the new repo.
- Collectors are pure functions (token in, dict out); tokens are read-only; never read other
  sites' venvs or `/etc/letsencrypt`. Prod runs as unprivileged user `fleet` (plan 3).
