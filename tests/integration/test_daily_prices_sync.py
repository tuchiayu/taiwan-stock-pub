"""Integration tests for daily prices sync (TWSE & TPEx)."""

import httpx2
import pytest
from fastapi.testclient import TestClient
from sync_helpers import load_job

from taiwan_stock.catalog import sync, sync_daily_prices, tpex_adapter, twse_adapter
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.web.deps import get_http_client

TWSE_PRICE_ROWS = [
    {
        "Date": "1150910",
        "Code": "2330",
        "Name": "台積電",
        "TradeVolume": "25000000",
        "ClosingPrice": "1050.00",
    },
    {
        "Date": "1150910",
        "Code": "9999",  # Not in database, will be skipped
        "Name": "未知股票",
        "TradeVolume": "1000",
        "ClosingPrice": "10.00",
    },
]

TPEX_PRICE_ROWS = [
    {
        "Date": "1150910",
        "SecuritiesCompanyCode": "1240",
        "CompanyName": "茂生農經",
        "TradingShares": "50000",
        "Close": "35.50",
    }
]


def _mock_handler(request: httpx2.Request) -> httpx2.Response:
    if request.url.path.endswith(twse_adapter.DAILY_PRICES_PATH):
        return httpx2.Response(200, json=TWSE_PRICE_ROWS)
    if request.url.path.endswith(tpex_adapter.DAILY_PRICES_PATH):
        return httpx2.Response(200, json=TPEX_PRICE_ROWS)
    return httpx2.Response(404, json={"error": "unexpected path"})


def _mock_handler_twse_down(request: httpx2.Request) -> httpx2.Response:
    # issue #17: TWSE returning a blocking-style error must not prevent
    # TPEx (a completely unrelated host) from being attempted at all.
    if request.url.path.endswith(twse_adapter.DAILY_PRICES_PATH):
        return httpx2.Response(428)
    if request.url.path.endswith(tpex_adapter.DAILY_PRICES_PATH):
        return httpx2.Response(200, json=TPEX_PRICE_ROWS)
    return httpx2.Response(404, json={"error": "unexpected path"})


def _mock_handler_both_down(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(503)


def _mock_http_client_override():
    with httpx2.Client(transport=httpx2.MockTransport(_mock_handler)) as client:
        yield client


def _mock_http_client_override_twse_down():
    with httpx2.Client(
        transport=httpx2.MockTransport(_mock_handler_twse_down)
    ) as client:
        yield client


def _mock_http_client_override_both_down():
    with httpx2.Client(
        transport=httpx2.MockTransport(_mock_handler_both_down)
    ) as client:
        yield client


def _seed_securities(tmp_path):
    conn = get_connection(tmp_path)
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('sec-2330', 'TWSE', '2330', '台積電', 'stock', 'active')"
    )
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('sec-1240', 'TPEx', '1240', '茂生農經', 'stock', 'active')"
    )
    conn.commit()
    conn.close()


@pytest.fixture
def app_client(tmp_path):
    app = create_app(tmp_path)
    app.dependency_overrides[get_http_client] = _mock_http_client_override
    with TestClient(app) as test_client:
        _seed_securities(tmp_path)
        yield test_client, tmp_path


@pytest.fixture
def app_client_twse_down(tmp_path):
    app = create_app(tmp_path)
    app.dependency_overrides[get_http_client] = _mock_http_client_override_twse_down
    with TestClient(app) as test_client:
        _seed_securities(tmp_path)
        yield test_client, tmp_path


@pytest.fixture
def app_client_both_down(tmp_path):
    app = create_app(tmp_path)
    app.dependency_overrides[get_http_client] = _mock_http_client_override_both_down
    with TestClient(app) as test_client:
        _seed_securities(tmp_path)
        yield test_client, tmp_path


def test_sync_daily_prices_inserts_records_for_known_securities(app_client):
    _, tmp_path = app_client
    conn = get_connection(tmp_path)
    with httpx2.Client(transport=httpx2.MockTransport(_mock_handler)) as http_client:
        twse_result = sync_daily_prices.sync_daily_prices(
            conn, http_client, adapter=twse_adapter
        )
        tpex_result = sync_daily_prices.sync_daily_prices(
            conn, http_client, adapter=tpex_adapter
        )

    assert twse_result["synced"] == 1
    assert twse_result["skipped_unknown_security"] == 1
    assert twse_result["latest_trade_date"] == "2026-09-10"

    assert tpex_result["synced"] == 1
    assert tpex_result["skipped_unknown_security"] == 0

    rows = conn.execute("SELECT * FROM daily_prices ORDER BY security_id").fetchall()
    assert len(rows) == 2
    assert rows[0]["security_id"] == "sec-1240"
    assert rows[0]["trade_date"] == "2026-09-10"
    assert rows[0]["close"] == "35.50"
    assert rows[0]["volume"] == 50000

    assert rows[1]["security_id"] == "sec-2330"
    assert rows[1]["trade_date"] == "2026-09-10"
    assert rows[1]["close"] == "1050.00"
    assert rows[1]["volume"] == 25000000
    conn.close()


def test_sync_daily_prices_is_idempotent_and_updates_price(app_client):
    _, tmp_path = app_client
    conn = get_connection(tmp_path)
    with httpx2.Client(transport=httpx2.MockTransport(_mock_handler)) as http_client:
        sync_daily_prices.sync_daily_prices(conn, http_client, adapter=twse_adapter)
        # Second run with same date
        sync_daily_prices.sync_daily_prices(conn, http_client, adapter=twse_adapter)

    rows = conn.execute(
        "SELECT * FROM daily_prices WHERE security_id = 'sec-2330'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["close"] == "1050.00"
    conn.close()


def test_api_prices_sync_endpoint(app_client):
    client, tmp_path = app_client
    response = client.post("/api/v1/prices-sync")
    assert response.status_code == 200
    data = response.json()
    assert data["total_synced"] == 2
    assert data["twse"]["synced"] == 1
    assert data["tpex"]["synced"] == 1


def test_prices_sync_form_redirects_with_synced_count(app_client):
    client, tmp_path = app_client
    response = client.post("/prices/sync", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/?job_id=")
    job = load_job(client, response)
    assert job["result"]["total_synced"] == 2
    # These mocks only serve the OpenAPI, so both markets report the
    # by-date fallback — with a short reason, not httpx's full message.
    summary = job["summary"]
    assert summary == (
        "TWSE 09/10（依日期來源失敗，改用 OpenAPI：HTTP 404）；"
        "TPEx 09/10（依日期來源失敗，改用 OpenAPI：HTTP 404）（寫入 2 筆）"
    )


def test_api_prices_sync_reports_tpex_even_when_twse_is_down(app_client_twse_down):
    # issue #17: TWSE failing must not hide TPEx's own successful sync.
    client, tmp_path = app_client_twse_down
    response = client.post("/api/v1/prices-sync")
    assert response.status_code == 200
    data = response.json()
    assert "twse" not in data
    assert data["tpex"]["synced"] == 1
    assert data["total_synced"] == 1
    assert "428" in data["errors"]["twse"]


def test_prices_sync_form_still_shows_tpex_count_when_twse_is_down(
    app_client_twse_down,
):
    client, tmp_path = app_client_twse_down
    response = client.post("/prices/sync", follow_redirects=False)
    assert response.status_code == 303
    job = load_job(client, response)
    assert job["status"] == "partial"
    assert job["result"]["total_synced"] == 1
    assert "twse" in job["result"]["errors"]


def test_sync_daily_prices_retries_transient_error_then_succeeds(app_client):
    # issue #23: a single dropped connection (e.g. WinError 10054) must
    # not fail the whole sync outright — `_fetch_with_retry` should give
    # the source a couple more chances first.
    _, tmp_path = app_client
    conn = get_connection(tmp_path)
    attempts = {"count": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith(tpex_adapter.DAILY_PRICES_PATH):
            attempts["count"] += 1
            if attempts["count"] < 2:
                return httpx2.Response(503)
            return httpx2.Response(200, json=TPEX_PRICE_ROWS)
        return httpx2.Response(404, json={"error": "unexpected path"})

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as http_client:
        result = sync_daily_prices.sync_daily_prices(
            conn, http_client, adapter=tpex_adapter
        )

    assert attempts["count"] == 2
    assert result["synced"] == 1
    conn.close()


def test_sync_daily_prices_gives_up_after_exhausting_retries(app_client):
    _, tmp_path = app_client
    conn = get_connection(tmp_path)
    attempts = {"count": 0}

    def handler(request: httpx2.Request) -> httpx2.Response:
        attempts["count"] += 1
        return httpx2.Response(503)

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as http_client:
        with pytest.raises(httpx2.HTTPStatusError):
            sync_daily_prices.sync_daily_prices(conn, http_client, adapter=tpex_adapter)

    assert attempts["count"] == len(sync._RETRY_BACKOFF_SECONDS) + 1
    conn.close()


def test_api_prices_sync_returns_502_only_when_both_markets_fail(app_client_both_down):
    client, tmp_path = app_client_both_down
    response = client.post("/api/v1/prices-sync")
    assert response.status_code == 502


def test_prices_sync_form_shows_only_error_when_both_markets_fail(
    app_client_both_down,
):
    client, tmp_path = app_client_both_down
    response = client.post("/prices/sync", follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert "prices_synced" not in location
    assert load_job(client, response)["result"]["errors"]
