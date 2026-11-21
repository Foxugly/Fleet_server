import pytest
import requests
import responses

from apps.collect.sources import http
from apps.collect.sources.http import SourceError, get_json_paginated

URL = "https://api.github.com/repos/Foxugly/x/issues"


def page(url, body, nxt=None, **kw):
    headers = {"Link": f'<{nxt}>; rel="next"'} if nxt else {}
    responses.get(url, json=body, headers=headers, **kw)


def fetch(**kw):
    with requests.Session() as s:
        return get_json_paginated(s, URL, **kw)


@responses.activate
def test_allow_404_on_first_page_is_empty():
    responses.get(URL, status=404)
    assert fetch(allow_404=True) == []


@responses.activate
def test_404_on_a_later_page_is_an_error():
    page(URL, [{"n": 1}], f"{URL}?page=2")
    responses.get(f"{URL}?page=2", status=404)
    with pytest.raises(SourceError, match="404"):
        fetch(allow_404=True)


@responses.activate
@pytest.mark.parametrize(
    "nxt",
    [
        "https://evil.example.com/steal?page=2",
        "http://api.github.com/repos/Foxugly/x/issues?page=2",
    ],
)
def test_next_link_must_stay_on_host_over_https(nxt):
    page(URL, [{"n": 1}], nxt)
    with pytest.raises(SourceError, match="leaves api.github.com"):
        fetch()
    assert len(responses.calls) == 1


@responses.activate
def test_repeated_next_link_stops():
    p2 = f"{URL}?page=2"
    page(URL, [{"n": 1}], p2)
    page(p2, [{"n": 2}], p2)
    with pytest.raises(SourceError, match="loops"):
        fetch()


@responses.activate
def test_page_cap(monkeypatch):
    monkeypatch.setattr(http, "MAX_PAGES", 3)
    page(URL, [{"n": 0}], f"{URL}?page=1")
    for i in range(1, 5):
        page(f"{URL}?page={i}", [{"n": i}], f"{URL}?page={i + 1}")
    with pytest.raises(SourceError, match="more than 3 pages"):
        fetch()
    assert len(responses.calls) == 3


@responses.activate
def test_follows_next_on_same_host():
    page(URL, [{"n": 1}], f"{URL}?page=2")
    page(f"{URL}?page=2", [{"n": 2}])
    assert fetch() == [{"n": 1}, {"n": 2}]
