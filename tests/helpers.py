from __future__ import annotations

import yaml

from apps.inventory.models import App
from apps.inventory.validation import clean_app


def make_apps(yaml_text: str) -> list[App]:
    """Replace every App row by the `sites:` of a YAML text (validated like the API).

    The test DB already holds the apps imported by migration 0002; starting from a
    clean table keeps each test's inventory deterministic.
    """
    App.objects.all().delete()
    raw_sites = (yaml.safe_load(yaml_text) or {}).get("sites") or []
    return [App.objects.create(**clean_app(raw)) for raw in raw_sites]
