"""Design 062: bad source data fails its unit instead of landing as success."""

import sqlite3
from contextlib import closing
from unittest.mock import patch

import httpx2
import pytest

from taiwan_stock.catalog import (
    sync_daily_prices,
    sync_etf_distributions,
    sync_history,
    sync_investment_trust,
    twse_adapter,
)
from taiwan_stock.infrastructure import migrate, sync_jobs
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.portfolio import trades


@pytest.fixture
def conn(tmp_path):
    with closing(get_connection(tmp_path)) as conn:
        migrate.apply_pending(conn)
        yield conn


def etf(conn, code="0050", issuer=None):
    conn.execute(
        "INSERT INTO securities(id,market,code,name,security_type,status) "
        "VALUES('s','TWSE',?,'Synthetic','etf','active')",
        (code,),
    )
    conn.execute(
        "INSERT INTO etf_profiles(security_id,distribution_frequency,issuer_name) "
        "VALUES('s','annual',?)",
        (issuer,),
    )


def distributions(conn, *rows):
    payload = {
        "fields": list(twse_adapter._ETF_DISTRIBUTION_FIELD_NAMES.values()),
        "data": [["0050", ex_date, "", "115年03月10日", "4"] for ex_date in rows],
    }
    with (
        httpx2.Client(
            transport=httpx2.MockTransport(lambda _: httpx2.Response(200, json=payload))
        ) as client,
        patch.object(sync_etf_distributions, "_ETF_DISTRIBUTION_DELAY_SECONDS", 0),
    ):
        return sync_etf_distributions.sync_etf_distributions(
            conn, client, years=1, reference_year=2026
        )


def test_an_impossible_ex_date_fails_its_unit_and_keeps_the_page_up(conn):
    """The 2026-10-31 code review case B5, now with the expected results."""
    etf(conn)
    conn.execute("INSERT INTO broker_accounts(id,name,status) VALUES('a','A','active')")
    trades.create_trade(
        conn,
        {
            "broker_account_id": "a",
            "security_id": "s",
            "event_type": "buy",
            "trade_date": "2026-01-02",
            "quantity": 100,
            "unit_price": "100",
        },
    )
    assert distributions(conn, "115年01月20日")["synced"] == 1

    result = distributions(conn, "115年02月30日")
    assert result["synced"] == 0 and result["failed_units"] == ["0050:2026"]
    report = sync_history.get_report(conn, result["run_id"])
    assert report["units"][0]["status"] == "parse_failed"
    rows = conn.execute(
        "SELECT ex_dividend_date, reconciliation_status FROM distributions"
    ).fetchall()
    assert [tuple(r) for r in rows] == [("2026-01-20", None)]
    trades.list_position_overview(conn)


def test_an_ex_date_outside_the_requested_year_fails_its_unit(conn):
    etf(conn)
    result = distributions(conn, "114年12月20日")
    assert result["failed_units"] == ["0050:2026"]
    report = sync_history.get_report(conn, result["run_id"])
    assert report["units"][0]["reason"] == "除息日不在查詢年度"
    assert not conn.execute("SELECT 1 FROM distributions").fetchall()


def test_unreadable_ex_date_text_is_a_format_change_not_the_sentinel(conn):
    etf(conn)
    result = distributions(conn, "下次公告")
    assert result["failed_units"] == ["0050:2026"]
    assert distributions(conn, "")["failed_units"] == []


def test_a_missing_field_is_retried_not_settled_as_not_listed(conn):
    """The 2026-10-31 code review case B6, now with the expected results."""
    etf(conn, code="00935", issuer="野村")
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx2.Response(200, json={})

    with (
        httpx2.Client(transport=httpx2.MockTransport(handle)) as client,
        patch.object(sync_investment_trust, "_REQUEST_DELAY_SECONDS", 0),
    ):
        result = sync_investment_trust.sync_investment_trust_data(conn, client)
        report = sync_history.get_report(conn, result["run_id"])
        assert report["counts"] == {"parse_failed": 1}
        assert report["status"] == "failed"
        assert "缺少欄位" in report["units"][0]["reason"]
        calls.clear()
        sync_investment_trust.sync_investment_trust_data(
            conn, client, retry_run_id=result["run_id"]
        )
    assert calls


def test_a_code_missing_from_a_full_list_is_still_not_listed():
    with pytest.raises(sync_history.ProductNotListed) as caught:
        sync_investment_trust._fetch_trust_data(
            httpx2.Client(),
            "聯博",
            "00999",
            capital_ids={},
            mega_ids={},
            sinopac_overview={},
            ezmoney_codes={},
            ab_catalog={"00980A": {"custodian_bank": "x"}},
        )
    assert sync_history.error_info(caught.value)[0] == "not_listed"


def test_a_twse_maintenance_page_does_not_stop_tpex(conn):
    """The 2026-10-31 audit case 7, now with the expected results."""
    conn.execute(
        "INSERT INTO securities(id,code,name,market,security_type,status) "
        "VALUES('TPEx:6488','6488','環球晶','TPEx','stock','active')"
    )
    conn.execute(
        "INSERT INTO watchlist(security_id,updated_at) "
        "VALUES('TPEx:6488','2026-10-30T00:00:00Z')"
    )

    def handler(request):
        if "twse.com.tw" in request.url.host:
            return httpx2.Response(200, text="<html>TWSE Maintenance</html>")
        return httpx2.Response(
            200,
            json=[
                {
                    "Date": "1151030",
                    "SecuritiesCompanyCode": "6488",
                    "Close": "500.00",
                    "Open": "495.00",
                    "High": "505.00",
                    "Low": "490.00",
                    "TradingShares": "1000000",
                }
            ],
        )

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        outcome = sync_daily_prices.sync_daily_prices_both(
            conn, client, today="2026-10-30", sleep=lambda _: None
        )
    assert "twse" in outcome["errors"]
    assert "tpex" in outcome["results"]
    assert conn.execute("SELECT count(*) FROM daily_prices").fetchone()[0] == 1


def test_an_invalid_listing_date_reads_as_unknown():
    security, _ = twse_adapter.normalize_etf(
        {"基金代號": "0050", "基金簡稱": "元大台灣50", "上市日期": "1150230"}
    )
    assert security["listed_date"] is None


def test_a_failed_worker_leaves_a_traceback(conn, tmp_path, capsys):
    def execute(connection, client, operation, options):
        raise sqlite3.OperationalError("database is locked")

    job = sync_jobs.submit(
        conn,
        tmp_path,
        "catalog",
        launcher=lambda d, j: sync_jobs.run_worker(d, j, executor=execute),
    )
    assert job["status"] == "failed"
    assert job["error"] == "資料庫忙碌或無法寫入"
    assert "Traceback" in capsys.readouterr().err


def test_a_locked_final_write_is_retried(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(sync_jobs, "FINISH_RETRY_SECONDS", 0)
    real = sync_jobs.write_transaction

    def execute(connection, client, operation, options):
        attempts = []

        def flaky(c):
            attempts.append(1)
            if len(attempts) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real(c)

        monkeypatch.setattr(sync_jobs, "write_transaction", flaky)
        return {}

    job = sync_jobs.submit(
        conn,
        tmp_path,
        "catalog",
        launcher=lambda d, j: sync_jobs.run_worker(d, j, executor=execute),
    )
    assert job["status"] == "success"
    assert not conn.execute("SELECT * FROM sync_job_resources").fetchall()
