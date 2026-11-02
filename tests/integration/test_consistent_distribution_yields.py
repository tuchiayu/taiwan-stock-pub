"""Design 058: every page counts the same distributions the same way."""

import json
from contextlib import closing
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.catalog import query, search, sync_history
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import income_calendar, trades
from taiwan_stock.shared.dates import _business_date

AS_OF = "2026-10-31"


@pytest.fixture(autouse=True)
def fixed_day():
    token = _business_date.set(AS_OF)
    yield
    _business_date.reset(token)


@pytest.fixture
def conn(tmp_path):
    with closing(get_connection(tmp_path)) as conn:
        migrate.apply_pending(conn)
        conn.execute(
            "INSERT INTO broker_accounts(id,name,status) VALUES('a','主帳戶','active')"
        )
        yield conn


def etf(conn, sid, code, close, listed="2020-01-02"):
    conn.execute(
        "INSERT INTO securities(id,code,name,market,security_type,status,listed_date) "
        "VALUES(?,?,?,'TWSE','etf','active',?)",
        (sid, code, f"測試{code}", listed),
    )
    conn.execute(
        "INSERT INTO daily_prices(security_id,trade_date,close) VALUES(?,?,?)",
        (sid, "2026-10-30", close),
    )


def buy(conn, sid, day, price):
    trades.create_trade(
        conn,
        {
            "broker_account_id": "a",
            "security_id": sid,
            "event_type": "buy",
            "trade_date": day,
            "quantity": 1000,
            "unit_price": price,
            "commission": "0",
        },
    )


def distributions(conn, sid, rows):
    conn.executemany(
        "INSERT INTO distributions(id,security_id,ex_dividend_date,pay_date,"
        "amount_per_share,amount_status,reconciliation_status,reconciliation_note,"
        "fetched_at) VALUES(?,?,?,?,?,?,?,?,'2026-10-25T00:00:00Z')",
        [
            (
                f"{sid}-{ex}",
                sid,
                ex,
                pay,
                amount,
                status,
                review,
                "來源不再列出此除息日" if review else None,
            )
            for ex, pay, amount, status, review in rows
        ],
    )


def synced(conn, sid, years=("2024", "2025", "2026")):
    with sync_history.run(conn, "twse_etf_distribution_list") as run:
        for year in years:
            key = f"{sid}:{year}"
            run.plan(key, security_id=sid, period=year)
            run.record(key, "success", count=1)


@pytest.fixture
def moved(conn):
    """00878's 10/05 moved to 10/08, and 11/05 moved to 11/12."""
    etf(conn, "TWSE:00878", "00878", "20.00", "2020-07-20")
    buy(conn, "TWSE:00878", "2026-01-05", "20.00")
    distributions(
        conn,
        "TWSE:00878",
        [
            ("2026-10-05", "2026-10-28", "1.00", "final", "needs_review"),
            ("2026-10-08", "2026-10-28", "1.00", "final", None),
            ("2026-11-05", "2026-11-29", "0.50", "estimated", "needs_review"),
            ("2026-11-12", "2026-12-05", "0.50", "estimated", None),
        ],
    )
    return conn


def test_a_moved_ex_date_is_not_paid_twice_anywhere(moved, tmp_path):
    rows = moved.execute("SELECT * FROM distributions").fetchall()
    summary = query.yield_summary(rows, "20.00", as_of=AS_OF, complete=True)
    assert summary["cash_per_share"] == Decimal("1.00")
    assert "1 筆來源待覆核，未計入" in summary["notes"]

    month = income_calendar.calendar_result(moved, month="2026-10")
    overview = income_calendar.monthly_overview(month["items"], "2026-10")
    assert overview["gross"] == Decimal("1000")
    assert overview["reconciliation_count"] == 1
    assert overview["count"] == 2

    listed = query.browse(moved, mode="etf")["results"][0]
    assert listed["next_distribution"]["ex_dividend_date"] == "2026-11-12"
    assert search.candidate_securities(moved, pay_month="2026-11") == []
    assert [
        r["id"] for r in search.candidate_securities(moved, pay_month="2026-12")
    ] == ["TWSE:00878"]

    holding = trades.list_position_overview(moved)[0]
    assert holding["yield_info"]["cash_per_share"] == Decimal("1.00")

    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        detail = client.get("/securities/TWSE:00878").text
        calendar_page = client.get("/income-calendar", params={"month": "2026-10"}).text
    assert detail.count("來源待覆核</span>") == 2
    assert "另有 1 筆來源資料待覆核，未計入合計。" in calendar_page


@pytest.fixture
def uneven(conn):
    """An official 1、11 月 semiannual ETF held since before both payments."""
    etf(conn, "TWSE:00999", "00999", "50.00")
    conn.execute(
        "INSERT INTO etf_profiles(security_id,distribution_policy,"
        "distribution_frequency,distribution_ex_dividend_schedule) "
        "VALUES(?,'distributing','semiannual',?)",
        (
            "TWSE:00999",
            json.dumps(
                {
                    "frequency": "semiannual",
                    "months": [1, 11],
                    "origin": "investment_trust",
                }
            ),
        ),
    )
    distributions(
        conn,
        "TWSE:00999",
        [
            ("2025-11-18", "2025-12-15", "1.50", "final", None),
            ("2026-01-19", "2026-02-15", "1.50", "final", None),
        ],
    )
    synced(conn, "TWSE:00999")
    buy(conn, "TWSE:00999", "2025-01-02", "50.00")
    return conn


def test_list_detail_and_holdings_agree_on_an_uneven_schedule(uneven, tmp_path):
    listed = query.browse(uneven, mode="etf")["results"][0]["yield_info"]
    held = trades.list_position_overview(uneven)[0]["yield_info"]
    keys = ("known_yield", "estimated_yield", "overdue", "extra_distribution", "notes")
    assert {k: held[k] for k in keys} == {k: listed[k] for k in keys}
    assert listed["overdue"] is False
    assert listed["estimated_annual_cash"] == Decimal("3.00")

    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        detail = client.get("/securities/TWSE:00999")
    assert detail.status_code == 200
    assert "已超過一個配息週期未查得新配息" not in detail.text
