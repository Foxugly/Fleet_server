from django.db import models
from django.utils import timezone


class Snapshot(models.Model):
    """One timestamped collector result for a (source, subject) pair."""

    source = models.CharField(max_length=16)
    subject = models.CharField(max_length=120)
    collected_at = models.DateTimeField(db_index=True, default=timezone.now)
    ok = models.BooleanField()
    data = models.JSONField(default=dict)
    error = models.TextField(blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["source", "subject", "-collected_at"],
                name="collect_snap_lookup_idx",
            )
        ]

    def __str__(self) -> str:
        state = "ok" if self.ok else "error"
        return (
            f"{self.source}/{self.subject} {state} @ {self.collected_at:%Y-%m-%d %H:%M}"
        )
