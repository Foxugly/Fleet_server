from __future__ import annotations

import os
import secrets
from datetime import timedelta
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


#: Fleet env selector (OPERATIONS.md §3.14): `PROD` or `DEV`. Anything else is
#: a misconfiguration, never a silent fallback to DEV.
STATE = (_env("STATE") or "DEV").upper()
if STATE not in {"DEV", "PROD"}:
    raise ImproperlyConfigured("STATE must be DEV or PROD")
# DEBUG can only ever be on in DEV: DEBUG=true next to STATE=PROD is ignored.
DEBUG = STATE == "DEV" and _env_bool("DEBUG", True)

SECRET_KEY = _env("SECRET_KEY")
if not SECRET_KEY:
    if STATE == "PROD":
        raise ImproperlyConfigured("SECRET_KEY must be set when STATE=PROD")
    # DEV only: a random key per process, never a constant anyone could read
    # in the repo and use to forge JWTs. Set SECRET_KEY in a dev `.env` to keep
    # sessions and enrolled TOTP secrets across restarts.
    SECRET_KEY = secrets.token_urlsafe(50)

if STATE == "PROD":
    ALLOWED_HOSTS = _env_list("ALLOWED_HOSTS")
    if not ALLOWED_HOSTS:
        raise ImproperlyConfigured("ALLOWED_HOSTS must be set when STATE=PROD")
else:
    ALLOWED_HOSTS = _env_list("ALLOWED_HOSTS", "127.0.0.1,localhost,testserver")
CSRF_TRUSTED_ORIGINS = _env_list("CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "drf_spectacular",
    "rest_framework_simplejwt.token_blacklist",
    "apps.api_auth",
    "apps.inventory",
    "apps.collect",
    "apps.api",
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

# Cache = DRF throttle counters. LocMem is per process and emptied by every
# restart (FABRIC-3): a deploy or a second worker would reset the login/MFA
# limits. With an explicit REDIS_URL they are shared and survive restarts; the
# prefix keeps the keys apart from Celery's. No REDIS_URL (tests, local dev
# without Redis): LocMem.
if _env("REDIS_URL"):
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "KEY_PREFIX": "fleet-cache",
        }
    }
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TIMEZONE = "UTC"
CELERY_TASK_TIME_LIMIT = 600
CELERY_TASK_SOFT_TIME_LIMIT = 540
CELERY_WORKER_PREFETCH_MULTIPLIER = 1

from apps.collect.intervals import INTERVALS, PURGE_INTERVAL  # noqa: E402

CELERY_BEAT_SCHEDULE = {
    name: {"task": task, "schedule": float(INTERVALS[key])}
    for name, task, key in (
        ("collect-health", "collect_health", "health"),
        ("collect-github", "collect_github", "github"),
        ("collect-sentry", "collect_sentry", "sentry"),
        ("collect-box", "collect_box_task", "box"),
        ("collect-deps", "collect_deps", "deps"),
    )
}
CELERY_BEAT_SCHEDULE["purge-snapshots"] = {
    "task": "purge_snapshots",
    "schedule": float(PURGE_INTERVAL),
}

# Fleet-specific settings (used by the collectors added in later tasks).
FLEET_GITHUB_READ_TOKEN = _env("FLEET_GITHUB_READ_TOKEN")
FLEET_SENTRY_TOKEN = _env("FLEET_SENTRY_TOKEN")
FLEET_SENTRY_ORG = _env("FLEET_SENTRY_ORG", "foxugly-srl")
FLEET_SENTRY_API = _env("FLEET_SENTRY_API", "https://de.sentry.io/api/0/")
FLEET_INVENTORY_PATH = _env(
    "FLEET_INVENTORY_PATH", str(BASE_DIR / "inventory" / "fleet.yaml")
)

REST_FRAMEWORK = {
    # SimpleJWT only: a short-lived access token kept in the SPA's memory, the
    # refresh token in an HttpOnly cookie (apps/api_auth/refresh_cookie.py). No
    # SessionAuthentication, so CSRF never applies to the API.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "anon": _env("THROTTLE_ANON", "60/min"),
        "user": _env("THROTTLE_USER", "300/min"),
        "login": _env("THROTTLE_LOGIN", "10/hour"),
        # Per username, only FAILED password steps count (FLEET-7).
        "login_username": _env("THROTTLE_LOGIN_USERNAME", "20/hour"),
        # TOTP: a 6-digit code is guessable, so verify/confirm are bounded per
        # client IP AND per account (the one the mfa_token names).
        "mfa": _env("THROTTLE_MFA", "5/min"),
        "mfa_user": _env("THROTTLE_MFA_USER", "5/min"),
        # Manual refresh fans out to external APIs.
        "refresh": _env("THROTTLE_REFRESH", "6/min"),
    },
    # nginx is the single reverse proxy: trust only the last X-Forwarded-For
    # entry, never what the client put there itself.
    "NUM_PROXIES": 1,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Fleet API",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    # The schema reveals the API surface: same JWT as everything else.
    "SERVE_PERMISSIONS": ["rest_framework.permissions.IsAuthenticated"],
    "SERVE_AUTHENTICATION": [
        "rest_framework_simplejwt.authentication.JWTAuthentication"
    ],
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=int(_env("JWT_ACCESS_MINUTES", "15"))),
    # Sliding window: every refresh rotates the token and blacklists the old one.
    "REFRESH_TOKEN_LIFETIME": timedelta(days=int(_env("JWT_REFRESH_DAYS", "7"))),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# Origins allowed to use the cookie mode (checked server-side on `Origin`).
CORS_ALLOWED_ORIGINS = _env_list(
    "CORS_ALLOWED_ORIGINS", "http://127.0.0.1:4200,http://localhost:4200"
)
if "*" in CORS_ALLOWED_ORIGINS:
    raise ImproperlyConfigured("CORS_ALLOWED_ORIGINS must list explicit origins")

# HttpOnly refresh cookie (web SPA only, opt-in via `X-Auth-Mode: cookie`).
AUTH_REFRESH_COOKIE_NAME = _env("AUTH_REFRESH_COOKIE_NAME", "fleet_refresh")
AUTH_REFRESH_COOKIE_PATH = "/api/v1/auth/"
AUTH_REFRESH_COOKIE_SECURE = _env_bool("AUTH_REFRESH_COOKIE_SECURE", True)

# Mandatory TOTP second factor. Secrets are encrypted at rest with Fernet; the
# key is derived (HKDF-SHA256) from FLEET_MFA_ENCRYPTION_KEY when set, else from
# SECRET_KEY. Changing the source key makes enrolled secrets unreadable: run
# `reset_mfa` for the user, who then re-enrolls at the next sign-in.
FLEET_MFA_ENCRYPTION_KEY = _env("FLEET_MFA_ENCRYPTION_KEY")
#: Lifetime of the `mfa_token` handed out by the password step.
FLEET_MFA_TOKEN_TTL_SECONDS = 300
#: Wrong codes are counted on `MfaDevice` (reset by a success): every N-th one
#: spends the `mfa_token`, the threshold locks the factor for
#: FLEET_MFA_LOCKOUT_SECONDS (`manage.py reset_mfa <user>` lifts it at once).
FLEET_MFA_MAX_FAILURES_PER_TOKEN = 3
FLEET_MFA_LOCKOUT_THRESHOLD = 10
FLEET_MFA_LOCKOUT_SECONDS = int(_env("FLEET_MFA_LOCKOUT_SECONDS", str(24 * 3600)))
FLEET_MFA_ISSUER = "Fleet"

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
