"""Unit tests for the shared `httpx2.Client` factory (design 005 §4.4
follow-up: a browser-like User-Agent is required to get past some
sources' bot-detection WAFs — see `shared/http.py`'s module docstring).
"""

from taiwan_stock.shared.http import new_client


def test_new_client_sends_a_browser_like_user_agent():
    client = new_client()
    try:
        assert "Mozilla" in client.headers["User-Agent"]
        assert "Chrome" in client.headers["User-Agent"]
    finally:
        client.close()


def test_new_client_passes_through_extra_kwargs():
    client = new_client(timeout=5, follow_redirects=True)
    try:
        assert client.timeout.connect == 5
        assert client.follow_redirects is True
    finally:
        client.close()
