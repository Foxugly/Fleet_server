import pytest


@pytest.mark.django_db
def test_health_ok(client):
    response = client.get("/health/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_admin_route_is_gone(client):
    assert client.get("/admin/").status_code == 404
    assert client.get("/admin/login/").status_code == 404
