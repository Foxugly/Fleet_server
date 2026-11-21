import pytest


@pytest.mark.django_db
def test_health_ok(client):
    response = client.get("/health/")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert isinstance(body["version"], str)


@pytest.mark.django_db
def test_admin_route_is_gone(client):
    assert client.get("/admin/").status_code == 404
    assert client.get("/admin/login/").status_code == 404


def test_read_release_falls_back_to_dev(tmp_path):
    from config.settings import _read_release

    assert _read_release(tmp_path) == "dev"


def test_read_release_returns_stripped_file_content(tmp_path):
    from config.settings import _read_release

    (tmp_path / "RELEASE").write_text("abc123" + chr(10), encoding="utf-8")
    assert _read_release(tmp_path) == "abc123"


def test_read_release_ignores_an_empty_file(tmp_path):
    from config.settings import _read_release

    (tmp_path / "RELEASE").write_text(chr(10), encoding="utf-8")
    assert _read_release(tmp_path) == "dev"


@pytest.mark.django_db
def test_health_reports_the_configured_version(client, settings):
    settings.RELEASE = "deadbeef"
    assert client.get("/health/").json() == {"status": "ok", "version": "deadbeef"}
