import pytest

from apps.collect import store, tasks
from apps.collect.sources.http import SourceError

INV = """sites:
  - slug: a
    repos: [Foxugly/A, Foxugly/B]
    vhosts: [a.foxugly.com, api.foxugly.com]
    health_path: /health/
    sentry_projects: [a-backend]
    python_locks: ["Foxugly/A:requirements.txt"]
    npm_locks: []
    deployed: true
    actions_allowed: true
  - slug: b
    repos: [Foxugly/B]
    vhosts: [a.foxugly.com]
    health_path: /h/
    sentry_projects: []
    python_locks: []
    npm_locks: []
    deployed: false
    actions_allowed: false
"""


@pytest.fixture(autouse=True)
def inventory(settings, tmp_path):
    p = tmp_path / "fleet.yaml"
    p.write_text(INV, encoding="utf-8")
    settings.FLEET_INVENTORY_PATH = str(p)
    settings.FLEET_GITHUB_READ_TOKEN = "tok"
    settings.FLEET_SENTRY_TOKEN = "tok"


@pytest.mark.django_db
def test_one_failing_repo_does_not_stop_others(monkeypatch):
    def fake(repo, *, token):
        if repo == "Foxugly/A":
            raise SourceError("boom")
        return {"x": 1}

    monkeypatch.setattr(tasks.github, "collect_repo", fake)
    assert tasks.collect_github() is None
    assert store.latest("github", "Foxugly/A").ok is False
    assert store.latest("github", "Foxugly/B").ok is True


@pytest.mark.django_db
def test_unexpected_exception_is_recorded(monkeypatch):
    def fake(repo, *, token):
        raise KeyError("k")

    monkeypatch.setattr(tasks.github, "collect_repo", fake)
    tasks.collect_github()
    snap = store.latest("github", "Foxugly/B")
    assert not snap.ok and "KeyError" in snap.error


@pytest.mark.django_db
def test_missing_token_records_error(settings, monkeypatch):
    settings.FLEET_GITHUB_READ_TOKEN = ""
    monkeypatch.setattr(
        tasks.github, "collect_repo", lambda *a, **k: pytest.fail("called")
    )
    tasks.collect_github()
    for repo in ("Foxugly/A", "Foxugly/B"):
        snap = store.latest("github", repo)
        assert not snap.ok and "FLEET_GITHUB_READ_TOKEN" in snap.error
    tasks.collect_deps()
    snap = store.latest("deps", "Foxugly/A:requirements.txt")
    assert "FLEET_GITHUB_READ_TOKEN" in snap.error


@pytest.mark.django_db
def test_health_paths_and_dedupe(monkeypatch):
    calls = []
    monkeypatch.setattr(
        tasks.health, "probe", lambda v, p: calls.append((v, p)) or {"status": 500}
    )
    tasks.collect_health()
    assert calls == [("a.foxugly.com", "/health/"), ("api.foxugly.com", "/")]
    assert store.latest("health", "a.foxugly.com").ok is True


@pytest.mark.django_db
def test_sentry_box_deps_and_purge(monkeypatch):
    monkeypatch.setattr(tasks.sentry, "collect_project", lambda p, **k: {"p": p})
    monkeypatch.setattr(tasks.box, "collect_box", lambda: {"up": 1})
    monkeypatch.setattr(tasks.deps, "collect_lock", lambda r, **k: {"r": r})
    tasks.collect_sentry()
    tasks.collect_box_task()
    tasks.collect_deps()
    tasks.purge_snapshots()
    assert store.latest("sentry", "a-backend").ok
    assert store.latest("box", "box").ok
    assert store.latest("deps", "Foxugly/A:requirements.txt").ok


def test_beat_schedule_intervals(settings):
    got = {v["task"]: v["schedule"] for v in settings.CELERY_BEAT_SCHEDULE.values()}
    assert got["collect_health"] == 300
    assert got["collect_github"] == 900
    assert got["collect_sentry"] == 900
    assert got["collect_box_task"] == 3600
    assert got["collect_deps"] == 21600
    assert got["purge_snapshots"] == 86400
    assert tasks.INTERVALS["deps"] == 21600


@pytest.mark.django_db
def test_soft_time_limit_propagates(monkeypatch):
    from celery.exceptions import SoftTimeLimitExceeded

    def fake(repo, *, token):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(tasks.github, "collect_repo", fake)
    with pytest.raises(SoftTimeLimitExceeded):
        tasks.collect_github()
    assert store.latest("github", "Foxugly/A") is None


@pytest.mark.django_db
def test_token_never_in_recorded_error(settings, monkeypatch):
    settings.FLEET_GITHUB_READ_TOKEN = "s3cret-token-value"

    def fake(repo, *, token):
        if repo == "Foxugly/A":
            raise SourceError("github unreachable")
        raise RuntimeError("bad response")

    monkeypatch.setattr(tasks.github, "collect_repo", fake)
    tasks.collect_github()
    from apps.collect.models import Snapshot

    errors = list(Snapshot.objects.values_list("error", flat=True))
    assert len(errors) == 2
    assert all("s3cret-token-value" not in e for e in errors)


@pytest.mark.django_db
def test_fallback_error_message_is_truncated_to_200_chars(monkeypatch):
    def fake(repo, *, token):
        raise ValueError("x" * 1000)

    monkeypatch.setattr(tasks.github, "collect_repo", fake)
    tasks.collect_github()
    snap = store.latest("github", "Foxugly/B")
    assert snap.error.startswith("ValueError: xxx")
    assert len(snap.error) == 200
