from dataclasses import dataclass


@dataclass(frozen=True)
class Site:
    slug: str
    repos: tuple[str, ...]
    vhosts: tuple[str, ...]
    health_path: str
    sentry_projects: tuple[str, ...]
    python_locks: tuple[str, ...]
    npm_locks: tuple[str, ...]
    deployed: bool
    actions_allowed: bool


def load_inventory() -> list[Site]:
    """Active (not archived) apps, ordered by slug."""
    from apps.inventory.models import App

    return [a.to_site() for a in App.objects.filter(archived_at__isnull=True)]


def get_site(slug: str) -> Site | None:
    """An active app by slug; None if unknown or archived."""
    from apps.inventory.models import App

    app = App.objects.filter(slug=slug, archived_at__isnull=True).first()
    return app.to_site() if app else None
