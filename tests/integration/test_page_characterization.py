"""Design 070: branches of the page routes no other test reached, pinned
before the routes moved out of `web/pages.py`."""

from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import csv_trades, trade_imports, trades


@pytest.fixture
def data_dir(tmp_path):
    with closing(get_connection(tmp_path)) as conn:
        migrate.apply_pending(conn)
        conn.execute(
            "INSERT INTO broker_accounts(id,name,status) VALUES('a','券商','active')"
        )
        conn.execute(
            "INSERT INTO securities(id,code,name,market,security_type,status) "
            "VALUES('TWSE:2330','2330','台積電','TWSE','stock','active')"
        )
        conn.execute(
            "INSERT INTO securities(id,code,name,market,security_type,status) "
            "VALUES('TWSE:0050','0050','元大台灣50','TWSE','etf','active')"
        )
        trades.create_trade(
            conn,
            {
                "broker_account_id": "a",
                "security_id": "TWSE:2330",
                "event_type": "buy",
                "trade_date": "2026-01-05",
                "quantity": 1000,
                "unit_price": "500",
            },
        )
    return tmp_path


@pytest.fixture
def client(data_dir):
    with TestClient(create_app(data_dir), raise_server_exceptions=False) as client:
        yield client


def test_transactions_page_notices(client, data_dir):
    with closing(get_connection(data_dir)) as conn:
        trade_id = conn.execute("SELECT id FROM trades").fetchone()[0]
    saved = client.get("/transactions", params={"saved": trade_id}).text
    assert "已儲存交易：2330 台積電。" in saved
    created = client.get("/transactions", params={"account_created": "1"}).text
    assert "已建立券商帳戶，可新增交易。" in created
    stale = client.get("/transactions", params={"new_account_id": "gone"}).text
    assert "帳戶預填條件已失效，請重新選擇。" in stale


def test_an_unknown_sync_report_is_404(client):
    response = client.get("/catalog-sync-runs/missing")
    assert response.status_code == 404
    assert response.json()["detail"] == "找不到同步紀錄"


def test_an_unknown_frequency_override_is_400(client):
    response = client.post(
        "/securities/TWSE:0050/distribution-frequency-override",
        data={"distribution_frequency_user_override": "hourly"},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "不是有效的配息頻率選項"


def test_an_invalid_return_month_is_reported(client):
    page = client.get("/income-calendar", params={"return_month": "2026-1"}).text
    assert "返回月份格式必須是 YYYY-MM" in page


def test_a_stock_dividend_is_entered_per_thousand_shares(client, data_dir):
    response = client.post(
        "/corporate-actions",
        data={
            "security_code": "2330",
            "action_type": "stock_dividend",
            "ex_date": "2026-07-01",
            "per_thousand_shares": "100",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with closing(get_connection(data_dir)) as conn:
        row = conn.execute(
            "SELECT ratio_numerator, ratio_denominator FROM corporate_actions"
        ).fetchone()
    assert (row[0], row[1]) == (1100, 1000)


def test_a_stale_preview_that_cannot_refresh_fails_cleanly(client, monkeypatch):
    def stale(*_args):
        raise csv_trades.StalePreviewError()

    def gone(*_args):
        raise trade_imports.PreviewNotFoundError()

    monkeypatch.setattr(trade_imports, "confirm_preview", stale)
    monkeypatch.setattr(trade_imports, "refresh_preview", gone)
    response = client.post(
        "/transactions/import/confirm",
        data={"preview_id": "p", "plan_digest": "d"},
    )
    assert response.status_code == 422
    assert "匯入失敗：找不到這次匯入的預覽" in response.text
