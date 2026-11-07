"""Tests for the Origin-header CSRF defense (design 002 §4.14)."""

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.main import create_app
from taiwan_stock.web.csrf import LOCAL_HOSTS, trusted_hosts


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as test_client:
        yield test_client


def test_form_post_without_origin_header_is_allowed(client):
    response = client.post(
        "/broker-accounts", data={"name": "測試證券"}, follow_redirects=False
    )
    assert response.status_code == 303


def test_form_post_with_matching_origin_is_allowed(client):
    response = client.post(
        "/broker-accounts",
        data={"name": "測試證券"},
        headers={"Origin": str(client.base_url).rstrip("/")},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_form_post_with_foreign_origin_is_rejected(client):
    response = client.post(
        "/broker-accounts",
        data={"name": "測試證券"},
        headers={"Origin": "http://evil.example"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_json_api_write_with_foreign_origin_is_rejected(client):
    response = client.patch(
        "/api/v1/settings",
        json={"min_commission": "5"},
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403


def test_json_api_write_without_origin_header_is_allowed(client):
    response = client.patch("/api/v1/settings", json={"min_commission": "5"})
    assert response.status_code == 200


def test_delete_with_foreign_origin_is_rejected(client):
    response = client.delete(
        "/api/v1/watchlist/does-not-exist",
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403


def test_get_requests_are_never_checked(client):
    response = client.get("/", headers={"Origin": "http://evil.example"})
    assert response.status_code == 200


# Issue #87: DNS rebinding makes the attacker's own name resolve to
# 127.0.0.1, so Host and Origin agree with each other but not with us.
REBOUND = "evil.example:2330"


def test_page_for_an_untrusted_host_is_not_served(client):
    response = client.get("/", headers={"Host": REBOUND})
    assert response.status_code == 400
    assert "我的持股" not in response.text


@pytest.mark.parametrize("host", ["[::1", ""])
def test_malformed_or_missing_host_is_rejected(client, host):
    assert client.get("/", headers={"Host": host}).status_code == 400


def test_rebound_write_with_matching_host_and_origin_is_rejected(client):
    response = client.post(
        "/broker-accounts",
        data={"name": "測試證券"},
        headers={"Host": REBOUND, "Origin": f"http://{REBOUND}"},
        follow_redirects=False,
    )
    assert response.status_code == 400


@pytest.mark.parametrize("host", ["127.0.0.1:2330", "localhost:2330", "[::1]:2330"])
def test_local_names_are_served(client, host):
    assert client.get("/", headers={"Host": host}).status_code == 200


def test_local_write_with_its_own_origin_is_allowed(client):
    response = client.post(
        "/broker-accounts",
        data={"name": "測試證券"},
        headers={"Host": "localhost:2330", "Origin": "http://localhost:2330"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_origin_must_also_match_the_host(client):
    # Both names are trusted, but they still have to be the same origin.
    response = client.post(
        "/broker-accounts",
        data={"name": "測試證券"},
        headers={"Host": "localhost:2330", "Origin": "http://127.0.0.1:2330"},
        follow_redirects=False,
    )
    assert response.status_code == 403


def test_explicit_bind_host_is_trusted(tmp_path):
    app = create_app(tmp_path, bind_host="192.168.1.20")
    with TestClient(app) as client:
        assert client.get("/", headers={"Host": "192.168.1.20:2330"}).status_code == 200
        assert client.get("/", headers={"Host": REBOUND}).status_code == 400


@pytest.mark.parametrize("bind_host", ["0.0.0.0", "::"])
def test_wildcard_bind_still_only_trusts_local_names(tmp_path, bind_host):
    assert trusted_hosts(bind_host) == LOCAL_HOSTS
    with TestClient(create_app(tmp_path, bind_host=bind_host)) as client:
        assert client.get("/", headers={"Host": "192.168.1.20:2330"}).status_code == 400
        assert client.get("/", headers={"Host": "localhost:2330"}).status_code == 200


def test_trusted_hosts_normalizes_an_ipv6_bind_host():
    assert "fe80::1" in trusted_hosts("[FE80::1]")


PROXY_NAME = "macair.tail0000.ts.net"


def test_an_extra_trusted_host_is_accepted_and_others_are_not(tmp_path):
    """Design 082: a private proxy's name joins the local names."""
    app = create_app(tmp_path, extra_trusted_hosts=(PROXY_NAME,))
    with TestClient(app) as client:
        assert client.get("/", headers={"Host": PROXY_NAME}).status_code == 200
        assert client.get("/", headers={"Host": "localhost:2330"}).status_code == 200
        assert client.get("/", headers={"Host": REBOUND}).status_code == 400
        allowed = client.post(
            "/broker-accounts",
            data={"name": "代理"},
            headers={"Host": PROXY_NAME, "Origin": f"http://{PROXY_NAME}"},
            follow_redirects=False,
        )
        assert allowed.status_code == 303
        mismatched = client.post(
            "/broker-accounts",
            data={"name": "代理"},
            headers={"Host": PROXY_NAME, "Origin": "http://evil.example"},
            follow_redirects=False,
        )
        assert mismatched.status_code == 403


def test_without_extra_names_a_proxy_name_is_refused(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert client.get("/", headers={"Host": PROXY_NAME}).status_code == 400


def test_an_https_proxy_origin_passes_once_uvicorn_applies_forwarded_proto(tmp_path):
    """`tailscale serve` sends Host: <name> and X-Forwarded-Proto: https from
    127.0.0.1; uvicorn's default proxy-header handling makes the scheme https,
    so the browser's https Origin matches."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    app = create_app(tmp_path, extra_trusted_hosts=(PROXY_NAME,))
    # TestClient's peer is not 127.0.0.1, so trust it explicitly here.
    proxied = ProxyHeadersMiddleware(app, trusted_hosts="*")
    proxied.dependency_overrides = app.dependency_overrides  # for conftest
    with TestClient(proxied) as client:
        response = client.post(
            "/broker-accounts",
            data={"name": "經由代理"},
            headers={
                "Host": PROXY_NAME,
                "Origin": f"https://{PROXY_NAME}",
                "X-Forwarded-Proto": "https",
                "X-Forwarded-For": "100.64.0.2",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        no_proto = client.post(
            "/broker-accounts",
            data={"name": "經由代理"},
            headers={"Host": PROXY_NAME, "Origin": f"https://{PROXY_NAME}"},
            follow_redirects=False,
        )
        assert no_proto.status_code == 403  # http scheme vs https Origin


def test_extra_trusted_hosts_are_normalized():
    hosts = trusted_hosts("127.0.0.1", ("MacAir.Local", "[FE80::2]"))
    assert {"macair.local", "fe80::2"} <= hosts
