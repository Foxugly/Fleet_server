from __future__ import annotations

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in _env(name, default).split(",") if item.strip()]


#: Fleet env selector (OPERATIONS.md §3.14): `PROD` or `DEV`.
STATE = _env("STATE", "DEV").upper()
DEBUG = _env_bool("DEBUG", STATE != "PROD")

SECRET_KEY = _env("SECRET_KEY")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("SECRET_KEY must be set when DEBUG is off")
    SECRET_KEY = "dev-secret-key"

ALLOWED_HOSTS = _env_list("ALLOWED_HOSTS", "127.0.0.1,localhost,testserver")
CSRF_TRUSTED_ORIGINS = _env_list("CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

# Database: fleet DB_* convention (OPERATIONS.md §3.13). No DB_ENGINE means
# SQLite, the local development path.
_db_engine = _env("DB_ENGINE").lower()
if _db_engine in {"postgresql", "postgres"}:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": _env("DB_NAME", "fleet"),
            "USER": _env("DB_USER", "fleet"),
            "PASSWORD": os.environ.get("DB_PASSWORD", ""),
            "HOST": _env("DB_HOST", "127.0.0.1"),
            "PORT": _env("DB_PORT", "5432"),
            "CONN_MAX_AGE": int(_env("DB_CONN_MAX_AGE", "60")),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": str(BASE_DIR / "db.sqlite3"),
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation."
        "UserAttributeSimilarityValidator"
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

STATIC_URL = "/static/"
STATIC_ROOT = str(BASE_DIR / "staticfiles")

# Redis db 12 is Fleet's (OPERATIONS.md §3.4): Celery broker and results.
REDIS_URL = _env("REDIS_URL", "redis://127.0.0.1:6379/12")
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TIMEZONE = TIME_ZONE

# Fleet-specific settings (used by the collectors added in later tasks).
FLEET_GITHUB_READ_TOKEN = _env("FLEET_GITHUB_READ_TOKEN")
FLEET_SENTRY_TOKEN = _env("FLEET_SENTRY_TOKEN")
FLEET_SENTRY_ORG = _env("FLEET_SENTRY_ORG", "foxugly-srl")
FLEET_SENTRY_API = _env("FLEET_SENTRY_API", "https://de.sentry.io/api/0/")
FLEET_INVENTORY_PATH = _env(
    "FLEET_INVENTORY_PATH", str(BASE_DIR / "inventory" / "fleet.yaml")
)

if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_HSTS_SECONDS = 31_536_000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
    X_FRAME_OPTIONS = "DENY"

LOG_LEVEL = _env("LOG_LEVEL", "INFO").upper()
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "fleet": {"format": "%(asctime)s %(levelname)-8s %(name)s: %(message)s"},
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "fleet"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "apps": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}

# Sentry (OPERATIONS.md §3.8): reports only in PROD, or with SENTRY_ENABLE for a
# deliberate local test, so a DSN in a dev `.env` cannot pollute production.
SENTRY_DSN = _env("SENTRY_DSN")
if SENTRY_DSN and (STATE == "PROD" or _env_bool("SENTRY_ENABLE", False)):
    from config.sentry import init_sentry

    init_sentry(
        dsn=SENTRY_DSN,
        environment=_env("SENTRY_ENVIRONMENT", STATE),
        release=_env("SENTRY_RELEASE") or None,
        traces_sample_rate=float(_env("SENTRY_TRACES_SAMPLE_RATE", "0")),
    )
