"""Design 063: share classes retry with their TWD class; no TWD bookkeeping."""

import io
from contextlib import closing
from unittest.mock import patch

import httpx2
import pytest
from fastapi.testclient import TestClient

from taiwan_stock.catalog import sync, sync_history, sync_investment_trust
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import trades

FOREIGN = "加掛外幣之跨國投資指數股票型"


@pytest.fixture
def data_dir(tmp_path):
    with closing(get_connection(tmp_path)) as conn:
        migrate.apply_pending(conn)
        conn.execute(
            "INSERT INTO broker_accounts(id,name,status) VALUES('a','券商','active')"
        )
        for sid, code, name, fund_type in (
            ("TWSE:00636", "00636", "元大中國A50", "跨國投資指數股票型"),
            ("TWSE:00636K", "00636K", "元大中國A50+U", FOREIGN),
            ("TWSE:0050", "0050", "元大台灣50", "國內成分證券指數股票型基金"),
        ):
            conn.execute(
                "INSERT INTO securities(id,code,name,market,security_type,status) "
                "VALUES(?,?,?,'TWSE','etf','active')",
                (sid, code, name),
            )
            conn.execute(
                "INSERT INTO etf_profiles(security_id,issuer_name,benchmark,"
                "fund_tax_id,fund_type) VALUES(?,'元大投信',?,?,?)",
                (
                    sid,
                    "富時中國A50指數" if "00636" in code else "臺灣50指數",
                    "12345678" if "00636" in code else "87654321",
                    fund_type,
                ),
            )
    return tmp_path


def unit_status(conn, run_id, code):
    report = sync_history.get_report(conn, run_id)
    return next(u for u in report["units"] if u["unit_key"].endswith(":" + code))


def test_a_share_class_waits_for_its_twd_class_and_is_retried(data_dir):
    """The 2026-10-31 audit case 9 (first part), with the expected results."""
    with closing(get_connection(data_dir)) as conn:
        outage = httpx2.HTTPStatusError(
            "503",
            request=httpx2.Request("GET", "https://www.yuantaetfs.com"),
            response=httpx2.Response(503),
        )
        with patch.object(sync, "_fetch_with_retry", side_effect=outage):
            first = sync_investment_trust.sync_investment_trust_data(
                conn, httpx2.Client(), issuer="元大"
            )
        assert unit_status(conn, first["run_id"], "00636")["status"] == "http_failed"
        child = unit_status(conn, first["run_id"], "00636K")
        assert child["status"] == "base_pending"
        assert child["reason"] == "主級別 00636 尚未取得資料"

        detail = {
            "custodian_bank": "中國信託商業銀行",
            "distribution_frequency": None,
            "distribution_policy": "non_distributing",
            "source_url": "https://www.yuantaetfs.com/product/detail/00636",
        }
        with patch.object(sync, "_fetch_with_retry", return_value=detail):
            retry = sync_investment_trust.sync_investment_trust_data(
                conn, httpx2.Client(), issuer="元大", retry_run_id=first["run_id"]
            )
        assert unit_status(conn, retry["run_id"], "00636K")["status"] == "success"
        bank = conn.execute(
            "SELECT custodian_bank FROM etf_profiles WHERE security_id='TWSE:00636K'"
        ).fetchone()[0]
        assert bank == "中國信託商業銀行"


def trade(**fields):
    return {
        "broker_account_id": "a",
        "security_id": "TWSE:00636K",
        "event_type": "buy",
        "trade_date": "2026-10-30",
        "quantity": 100,
        "unit_price": "7.54",
        **fields,
    }


def test_foreign_currency_trades_are_rejected_everywhere(data_dir):
    with TestClient(
        create_app(data_dir), base_url="http://127.0.0.1", raise_server_exceptions=False
    ) as client:
        api = client.post("/api/v1/trades", json=trade())
        assert api.status_code == 422
        assert "外幣計價" in api.json()["detail"]

        quote = client.post(
            "/api/v2/trade-fee-quotes",
            json={
                "brokerAccountId": "a",
                "securityId": "TWSE:00636K",
                "eventType": "buy",
                "tradeDate": "2026-10-30",
                "quantity": 100,
                "unitPrice": "7.54",
            },
        )
        assert quote.status_code == 422

        content = (
            "broker_account_name,security_code,market,event_type,trade_date,quantity,unit_price\n"
            "券商,00636K,TWSE,buy,2026-10-30,100,7.54\n"
        )
        preview = client.post(
            "/api/v1/trade-imports/previews",
            files={"file": ("t.csv", io.BytesIO(content.encode()), "text/csv")},
        )
        assert preview.status_code == 422
        assert "外幣計價" in preview.text

        form = client.post(
            "/transactions",
            data={
                "broker_account_id": "a",
                "security_code": "TWSE:00636K",
                "event_type": "buy",
                "trade_date": "2026-10-30",
                "quantity": "100",
                "unit_price": "7.54",
            },
        )
        assert form.status_code == 422
        assert "外幣計價" in form.text

    with closing(get_connection(data_dir)) as conn:
        assert conn.execute("SELECT count(*) FROM trades").fetchone()[0] == 0


def test_a_stored_foreign_currency_trade_is_left_out_of_twd_totals(data_dir):
    with closing(get_connection(data_dir)) as conn:
        trades.create_trade(conn, trade(security_id="TWSE:0050", unit_price="100"))
        conn.execute(
            "INSERT INTO trades(id,broker_account_id,security_id,event_type,"
            "trade_date,quantity,unit_price,created_at,trade_order) "
            "VALUES('fx','a','TWSE:00636K','buy','2026-10-30',100,'7.54',"
            "'2026-10-30T00:00:00.000Z',99)"
        )
        overview = trades.list_position_overview(conn)
        assert [row["security_id"] for row in overview] == ["TWSE:0050"]

    with TestClient(create_app(data_dir), base_url="http://127.0.0.1") as client:
        page = client.get("/").text
    assert "外幣計價，未列入新台幣合計" in page
    assert "/transactions/fx/edit" in page
