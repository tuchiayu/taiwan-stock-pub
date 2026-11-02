"""Tests for the `/corporate-actions` page's ex-dividend sync button."""

import httpx2
import pytest
from fastapi.testclient import TestClient
from sync_helpers import load_job

from taiwan_stock.catalog import tpex_adapter, twse_adapter
from taiwan_stock.main import create_app
from taiwan_stock.web.deps import get_http_client

NOTICE_ROWS = [
    {
        "Date": "1150916",
        "Code": "2330",
        "Name": "台積電",
        "StockDividendRatio": "",
        "CashDividend": "7.000001",
    }
]


def _mock_handler(request: httpx2.Request) -> httpx2.Response:
    if request.url.path.endswith(twse_adapter.EX_DIVIDEND_NOTICE_PATH):
        return httpx2.Response(200, json=NOTICE_ROWS)
    if request.url.path.endswith(tpex_adapter.EX_DIVIDEND_NOTICE_PATH):
        return httpx2.Response(200, json=[])
    return httpx2.Response(404, json={"error": "unexpected path"})


def _failing_handler(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(500, json={"error": "boom"})


def _mock_override():
    with httpx2.Client(transport=httpx2.MockTransport(_mock_handler)) as client:
        yield client


def _failing_override():
    with httpx2.Client(transport=httpx2.MockTransport(_failing_handler)) as client:
        yield client


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path)
    app.dependency_overrides[get_http_client] = _mock_override
    with TestClient(app) as test_client:
        yield test_client


def test_sync_button_populates_distributions_and_redirects(client, tmp_path):
    from taiwan_stock.infrastructure.db import get_connection

    conn = get_connection(tmp_path)
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('sec-2330', 'TWSE', '2330', '台積電', 'stock', 'active')"
    )
    conn.close()

    response = client.post("/corporate-actions/sync", follow_redirects=False)
    assert response.status_code == 303

    page = client.get(response.headers["location"])
    assert "部分成功" in page.text  # the fixture does not serve MOPS pay dates
    assert load_job(client, response)["result"]["distributions"] == 1
    assert 'href="/corporate-actions?view=dividends"' in page.text
    page = client.get("/corporate-actions?view=dividends")
    assert "7.0000" in page.text
    assert "台積電" in page.text


def test_sync_failure_shows_error(tmp_path):
    app = create_app(tmp_path)
    app.dependency_overrides[get_http_client] = _failing_override
    with TestClient(app) as test_client:
        response = test_client.post("/corporate-actions/sync", follow_redirects=False)
        assert response.status_code == 303
        assert load_job(test_client, response)["status"] == "failed"

        page = test_client.get(response.headers["location"])
        assert "同步失敗" in page.text


def test_json_api_ex_dividend_sync(client, tmp_path):
    from taiwan_stock.infrastructure.db import get_connection

    conn = get_connection(tmp_path)
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('sec-2330', 'TWSE', '2330', '台積電', 'stock', 'active')"
    )
    conn.close()

    response = client.post("/api/v1/ex-dividend-sync")

    assert response.status_code == 200
    data = response.json()
    assert {k: data[k] for k in data if k not in ("history", "pay_dates")} == {
        "distributions": 1,
        "corporate_actions": 0,
        "skipped_unknown_security": 0,
        "skipped_invalid_ratio": 0,
        "validation_errors": [],
        "reconciliation_warnings": [],
    }
    # Design 014: no traded/watched stocks here, so the backfill makes no
    # requests and reports empty per-market results.
    assert data["history"]["errors"] == {}
    assert data["history"]["twse"]["years"] == []


def test_json_api_ex_dividend_sync_failure_returns_502(tmp_path):
    app = create_app(tmp_path)
    app.dependency_overrides[get_http_client] = _failing_override
    with TestClient(app) as test_client:
        response = test_client.post("/api/v1/ex-dividend-sync")

    assert response.status_code == 502
