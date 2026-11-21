# CLAUDE.md

Fleet_server: Django/DRF backend monitoring the Foxugly fleet. Project layout:
`config/` at the repo root (settings, urls, health, sentry, celery), apps under
`apps/` (added by later tasks). Fleet-wide ops rules live in the user-level
`~/.claude/CLAUDE.md` and `OPERATIONS.md`.

## Commands (Windows)

- Tests: `.venv/Scripts/python.exe -m pytest -q`
- Lint: `.venv/Scripts/python.exe -m ruff check .`
- Lock: `uv pip compile requirements.in --generate-hashes --universal --python-version 3.12 -o requirements.txt`
- Install: `pip install --require-hashes -r requirements.txt` then `pip install -r requirements-dev.txt`

## Conventions

- Settings read env through `_env()` in `config/settings.py`; every variable is
  documented in `.env.example`; no secret is committed.
- `DB_ENGINE=postgresql` selects PostgreSQL, otherwise SQLite. Redis db 12.
- CI uses a cached PostgreSQL 16 image as steps, never a `services:` container.
