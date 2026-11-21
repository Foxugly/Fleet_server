from datetime import timedelta

import pytest
from django.utils import timezone

from apps.collect.models import Snapshot
from apps.collect.store import latest, latest_ok, purge_older_than, record

pytestmark = pytest.mark.django_db


def test_record_ok_then_error_keeps_last_good():
    record("github", "foxugly", data={"open_prs": 3})
    record("github", "foxugly", error="timeout")
    assert latest("github", "foxugly").ok is False
    assert latest_ok("github", "foxugly").data == {"open_prs": 3}
    assert latest("github", "other") is None


def test_record_requires_data_or_error():
    with pytest.raises(ValueError):
        record("github", "foxugly")


def test_record_rejects_both():
    with pytest.raises(ValueError):
        record("github", "foxugly", data={"a": 1}, error="boom")


def test_purge_keeps_latest_ok():
    old = timezone.now() - timedelta(days=40)
    older = old - timedelta(days=1)
    Snapshot.objects.create(
        source="box", subject="box", ok=True, data={"v": 1}, collected_at=older
    )
    keep = Snapshot.objects.create(
        source="box", subject="box", ok=True, data={"v": 2}, collected_at=old
    )
    Snapshot.objects.create(
        source="box", subject="box", ok=False, error="x", collected_at=old
    )
    Snapshot.objects.create(
        source="health", subject="a", ok=False, error="x", collected_at=old
    )
    fresh = record("box", "other", data={"v": 3})
    assert purge_older_than(30) == 3
    assert set(Snapshot.objects.values_list("id", flat=True)) == {keep.id, fresh.id}


def test_record_empty_dict_is_a_success():
    snap = record("box", "box", data={})
    assert snap.ok is True and snap.data == {}
    assert latest_ok("box", "box") == snap


def test_record_empty_dict_with_error_is_rejected():
    with pytest.raises(ValueError):
        record("box", "box", data={}, error="boom")
