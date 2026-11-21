from __future__ import annotations

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.api import status
from apps.api.views import SITE_SOURCES, subjects_for
from apps.collect import store
from apps.collect.models import Snapshot
from apps.inventory.loader import load_inventory


@pytest.mark.django_db
def test_refuses_outside_dev(settings):
    settings.STATE = "PROD"
    with pytest.raises(CommandError):
        call_command("seed_demo")
    assert Snapshot.objects.count() == 0


@pytest.mark.django_db
def test_seeds_every_source():
    call_command("seed_demo")
    now = timezone.now()
    states = {}
    for site in load_inventory():
        for source in SITE_SOURCES:
            subjects = subjects_for(site, source)
            for subject in subjects:
                assert store.latest(source, subject) is not None, (source, subject)
            states[(site.slug, source)] = status.cell(source, subjects, now)["state"]
    assert store.latest("box", "box") is not None
    stale = [k for k, v in states.items() if v == "stale"]
    assert len(stale) == 1
    assert "red" in states.values()
    assert "error" in states.values()
    assert "green" in states.values()


@pytest.mark.django_db
def test_failed_subject_keeps_previous_good_snapshot():
    call_command("seed_demo")
    failed = Snapshot.objects.filter(ok=False)
    assert failed.count() == 1
    bad = failed.get()
    assert bad.error
    assert store.latest(bad.source, bad.subject).ok is False
    assert store.latest_ok(bad.source, bad.subject) is not None


@pytest.mark.django_db
def test_box_is_red_with_reboot_and_security_package():
    call_command("seed_demo")
    box = store.latest("box", "box").data
    assert box["reboot_required"] and box["reboot_pkgs"]
    assert any(p["security"] for p in box["apt"])
    assert box["failed_units"]
    assert box["disk"]["used_pct"] == 72


@pytest.mark.django_db
def test_idempotent():
    call_command("seed_demo")
    first = Snapshot.objects.count()
    call_command("seed_demo")
    assert Snapshot.objects.count() == first
