from __future__ import annotations

from typing import Any

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.inventory.loader import Site
from apps.inventory.validation import EDITABLE_FIELDS


class App(models.Model):
    slug = models.SlugField(max_length=64, unique=True)
    repos = models.JSONField(default=list)
    vhosts = models.JSONField(default=list)
    health_path = models.CharField(max_length=200, default="/health/")
    sentry_projects = models.JSONField(default=list)
    python_locks = models.JSONField(default=list)
    npm_locks = models.JSONField(default=list)
    deployed = models.BooleanField(default=False)
    actions_allowed = models.BooleanField(default=False)
    archived_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["slug"]

    def __str__(self) -> str:
        return self.slug

    @property
    def archived(self) -> bool:
        return self.archived_at is not None

    def to_site(self) -> Site:
        return Site(
            slug=self.slug,
            repos=tuple(self.repos),
            vhosts=tuple(self.vhosts),
            health_path=self.health_path,
            sentry_projects=tuple(self.sentry_projects),
            python_locks=tuple(self.python_locks),
            npm_locks=tuple(self.npm_locks),
            deployed=self.deployed,
            actions_allowed=self.actions_allowed,
        )

    def audit_dict(self) -> dict[str, Any]:
        """Editable fields plus the archive flag: what the audit trail records."""
        return {**self.as_dict(), "archived": self.archived}

    def as_dict(self) -> dict[str, Any]:
        """The editable fields only (audit before/after)."""
        return {f: getattr(self, f) for f in EDITABLE_FIELDS}


class InventoryChange(models.Model):
    """Append-only audit of inventory writes."""

    class Action(models.TextChoices):
        CREATE = "create"
        UPDATE = "update"
        ARCHIVE = "archive"
        RESTORE = "restore"

    at = models.DateTimeField(default=timezone.now, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    app_slug = models.CharField(max_length=64, db_index=True)
    action = models.CharField(max_length=16, choices=Action.choices)
    before = models.JSONField(null=True)
    after = models.JSONField(null=True)

    class Meta:
        ordering = ["-at", "-id"]

    def __str__(self) -> str:
        return f"{self.at:%Y-%m-%d %H:%M} {self.action} {self.app_slug}"
