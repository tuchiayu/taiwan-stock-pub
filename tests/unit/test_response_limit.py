"""Design 068: external responses have a size cap."""

import httpx2
import pytest

from taiwan_stock.catalog.sync_history import error_info
from taiwan_stock.shared import http


@pytest.fixture(autouse=True)
def small_limit(monkeypatch):
    monkeypatch.setattr(http, "MAX_RESPONSE_BYTES", 1024 * 1024)


def fetch(response):
    with http.new_client(transport=httpx2.MockTransport(lambda _: response)) as c:
        return c.get("https://example.invalid/")


def chunks(count):
    for _ in range(count):
        yield b"x" * 1024


def test_a_declared_oversized_response_is_refused_before_reading():
    response = httpx2.Response(
        200, headers={"Content-Length": str(2 * 1024 * 1024)}, content=chunks(1)
    )
    with pytest.raises(http.ResponseTooLarge, match="1 MB"):
        fetch(response)


def test_an_undeclared_oversized_response_stops_while_reading():
    with pytest.raises(http.ResponseTooLarge):
        fetch(httpx2.Response(200, content=chunks(1025)))


def test_a_response_at_the_limit_is_read():
    assert len(fetch(httpx2.Response(200, content=chunks(1024))).content) == 1024**2


def test_it_is_recorded_as_a_source_change_and_not_retried():
    error = http.ResponseTooLarge(http.MAX_RESPONSE_BYTES)
    assert isinstance(error, ValueError)
    assert not isinstance(error, httpx2.HTTPError)
    assert error_info(error) == (
        "parse_failed",
        "ResponseTooLarge",
        "外部回應超過 1 MB 上限",
    )


def test_caller_hooks_still_run_after_the_limit():
    seen = []
    with http.new_client(
        transport=httpx2.MockTransport(lambda _: httpx2.Response(200, text="ok")),
        event_hooks={"response": [lambda r: seen.append(r.status_code)]},
    ) as client:
        assert client.get("https://example.invalid/").text == "ok"
    assert seen == [200]
