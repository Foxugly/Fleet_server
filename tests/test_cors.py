from __future__ import annotations

from django.test import Client

SPA = "http://localhost:4200"
URL = "/api/v1/auth/refresh/"


def _preflight(origin: str):
    return Client().options(
        URL,
        HTTP_ORIGIN=origin,
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="POST",
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS="x-auth-mode,content-type",
    )


def test_preflight_allows_spa_origin_with_credentials() -> None:
    resp = _preflight(SPA)
    assert resp.status_code == 200
    assert resp["Access-Control-Allow-Origin"] == SPA
    assert resp["Access-Control-Allow-Credentials"] == "true"
    assert "x-auth-mode" in resp["Access-Control-Allow-Headers"].lower()


def test_preflight_rejects_unknown_origin() -> None:
    resp = _preflight("https://evil.example")
    assert "Access-Control-Allow-Origin" not in resp
