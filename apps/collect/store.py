from __future__ import annotations

from datetime import timedelta

from django.db.models import OuterRef, Subquery
from django.utils import timezone

from .models import Snapshot


def record(
    source: str, subject: str, *, data: dict | None = None, error: str = ""
) -> Snapshot:
    """Store a success (data) or a failure (error); exactly one must be given."""
    # `data={}` is a valid (empty) success: test for None, not falsiness.
    if data is None and not error:
        raise ValueError("record() needs data or error")
    if data is not None and error:
        raise ValueError("record() takes data or error, not both")
    return Snapshot.objects.create(
        source=source,
        subject=subject,
        ok=not error,
        data=data if data is not None else {},
        error=error,
    )


def latest(source: str, subject: str) -> Snapshot | None:
    return (
        Snapshot.objects.filter(source=source, subject=subject)
        .order_by("-collected_at", "-id")
        .first()
    )


def latest_ok(source: str, subject: str) -> Snapshot | None:
    return (
        Snapshot.objects.filter(source=source, subject=subject, ok=True)
        .order_by("-collected_at", "-id")
        .first()
    )


def latest_ok_many(source: str, subjects: list[str]) -> dict[str, Snapshot]:
    """Latest ok snapshot per subject, in one query; absent subjects are omitted."""
    newest = (
        Snapshot.objects.filter(source=source, subject=OuterRef("subject"), ok=True)
        .order_by("-collected_at", "-id")
        .values("id")[:1]
    )
    rows = Snapshot.objects.filter(
        source=source, subject__in=subjects, ok=True, id=Subquery(newest)
    )
    return {row.subject: row for row in rows}


def purge_older_than(days: int = 30) -> int:
    """Delete rows older than `days`, keeping each pair's most recent ok row."""
    cutoff = timezone.now() - timedelta(days=days)
    keep_ids = []
    pairs = Snapshot.objects.filter(ok=True).values("source", "subject").distinct()
    for pair in pairs:
        row = (
            Snapshot.objects.filter(
                ok=True, source=pair["source"], subject=pair["subject"]
            )
            .order_by("-collected_at", "-id")
            .values_list("id", flat=True)
            .first()
        )
        keep_ids.append(row)
    deleted, _ = (
        Snapshot.objects.filter(collected_at__lt=cutoff)
        .exclude(id__in=keep_ids)
        .delete()
    )
    return deleted
