# Fleet_server

Django/DRF backend that monitors the Foxugly fleet (inventory, GitHub, Sentry,
box health). Auth, collectors, Celery tasks and the API arrive in later tasks.

## Local setup

    py -3.12 -m venv .venv
    .venv/Scripts/python.exe -m pip install --require-hashes -r requirements.txt
    .venv/Scripts/python.exe -m pip install -r requirements-dev.txt
    .venv/Scripts/python.exe manage.py migrate
    .venv/Scripts/python.exe manage.py runserver

Copy `.env.example` to `.env` to document your local settings (SQLite is used
when `DB_ENGINE` is empty). `GET /health/` returns `{"status": "ok"}`.

## Tests

    .venv/Scripts/python.exe -m pytest -q
    .venv/Scripts/python.exe -m ruff check .

## Dependencies

Edit `requirements.in`, then regenerate the lock:

    uv pip compile requirements.in --generate-hashes --universal --python-version 3.12 -o requirements.txt
