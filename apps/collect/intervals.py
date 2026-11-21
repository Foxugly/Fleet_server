"""Collection intervals in seconds. No Django imports: settings.py imports this."""

INTERVALS: dict[str, int] = {
    "health": 300,
    "github": 900,
    "sentry": 900,
    "box": 3600,
    "deps": 21600,
}
PURGE_INTERVAL = 86400
