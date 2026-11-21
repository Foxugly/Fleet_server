import ssl
from datetime import UTC, datetime, timedelta

import pytest
import requests
from celery.exceptions import SoftTimeLimitExceeded

from apps.collect.sources import health

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def _not_after(dt: datetime) -> str:
    return dt.strftime("%b %d %H:%M:%S %Y GMT")


class FakeResp:
    status_code = 200
    closed = False

    def close(self):
        self.closed = True


def _patch(monkeypatch, *, get=None, cert=None):
    monkeypatch.setattr(requests, "get", get or (lambda *a, **k: FakeResp()))
    monkeypatch.setattr(
        health,
        "_fetch_not_after",
        cert or (lambda host: _not_after(NOW + timedelta(days=10, hours=3))),
    )


def test_probe_ok(monkeypatch):
    seen = {}

    def fake_get(url, **kw):
        seen["url"], seen["kw"] = url, kw
        return FakeResp()

    _patch(monkeypatch, get=fake_get)
    out = health.probe("a.example.com", "/health/", now=NOW)
    assert out["status"] == 200
    assert isinstance(out["latency_ms"], int)
    assert out["error"] == ""
    assert out["tls_days_left"] == 10
    assert seen["url"] == "https://a.example.com/health/"
    assert seen["kw"]["allow_redirects"] is False


def test_probe_redirect_reported(monkeypatch):
    class R(FakeResp):
        status_code = 301

    _patch(monkeypatch, get=lambda *a, **k: R())
    assert health.probe("a.example.com", "/", now=NOW)["status"] == 301


def test_probe_timeout_reports_error(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.Timeout("timed out")

    _patch(monkeypatch, get=boom)
    out = health.probe("a.example.com", "/", now=NOW)
    assert out["status"] is None and out["latency_ms"] is None
    assert out["error"].startswith("Timeout: ")
    assert out["tls_days_left"] == 10


def test_tls_days_left_computed(monkeypatch):
    _patch(monkeypatch, cert=lambda host: _not_after(NOW + timedelta(days=10)))
    assert health.probe("a.example.com", "/", now=NOW)["tls_days_left"] == 10


def test_tls_failure_is_independent(monkeypatch):
    def bad(host):
        raise ssl.SSLCertVerificationError("certificate verify failed")

    _patch(monkeypatch, cert=bad)
    out = health.probe("a.example.com", "/", now=NOW)
    assert out["status"] == 200
    assert out["tls_days_left"] is None
    assert "TLS" in out["error"] and "SSLCertVerificationError" in out["error"]


def test_expired_cert_negative_days(monkeypatch):
    _patch(monkeypatch, cert=lambda host: _not_after(NOW - timedelta(days=2)))
    assert health.probe("a.example.com", "/", now=NOW)["tls_days_left"] == -2


def test_both_fail_errors_combined(monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.ConnectionError("refused")

    def bad(host):
        raise OSError("no route")

    _patch(monkeypatch, get=boom, cert=bad)
    out = health.probe("a.example.com", "/", now=NOW)
    assert "ConnectionError: refused" in out["error"]
    assert "TLS OSError: no route" in out["error"]


def test_probe_streams_and_closes_without_reading_body(monkeypatch):
    seen = {}

    class Streamed(FakeResp):
        @property
        def content(self):
            raise AssertionError("body must not be read")

    def fake_get(url, **kw):
        seen["kw"] = kw
        seen["resp"] = Streamed()
        return seen["resp"]

    _patch(monkeypatch, get=fake_get)
    assert health.probe("a.example.com", "/", now=NOW)["status"] == 200
    assert seen["kw"]["stream"] is True
    assert seen["resp"].closed is True


def _soft_limit(*a, **k):
    raise SoftTimeLimitExceeded()


def test_soft_time_limit_on_http_propagates(monkeypatch):
    _patch(monkeypatch, get=_soft_limit)
    with pytest.raises(SoftTimeLimitExceeded):
        health.probe("a.example.com", "/", now=NOW)


def test_soft_time_limit_on_tls_propagates(monkeypatch):
    _patch(monkeypatch, cert=_soft_limit)
    with pytest.raises(SoftTimeLimitExceeded):
        health.probe("a.example.com", "/", now=NOW)
