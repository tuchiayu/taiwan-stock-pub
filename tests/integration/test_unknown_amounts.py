"""Design 061: unknown cash never reads as zero in daily or total P&L."""

from contextlib import closing
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import trades
from taiwan_stock.shared.dates import _business_date


@pytest.fixture(autouse=True)
def fixed_day():
    token = _business_date.set("2026-10-31")
    yield
    _business_date.reset(token)


@pytest.fixture
def conn(tmp_path):
    with closing(get_connection(tmp_path)) as conn:
        migrate.apply_pending(conn)
        conn.execute(
            "INSERT INTO broker_accounts(id,name,status) VALUES('a','主帳戶','active')"
        )
        conn.executemany(
            "INSERT INTO securities(id,market,code,name,security_type,status) "
            "VALUES(?,'TWSE',?,?,'stock','active')",
            [("s", "2330", "台積電"), ("t", "2317", "鴻海")],
        )
        yield conn


def flat(conn, security_id, close="1000"):
    conn.executemany(
        "INSERT INTO daily_prices(security_id,trade_date,close) VALUES(?,?,?)",
        [(security_id, "2026-10-29", close), (security_id, "2026-10-30", close)],
    )


def add(conn, security_id="s", **fields):
    trades.create_trade(
        conn,
        {
            "broker_account_id": "a",
            "security_id": security_id,
            "event_type": "buy",
            "trade_date": "2026-10-01",
            "quantity": 1000,
            "unit_price": "1000",
            **fields,
        },
    )


def row(conn, security_id="s"):
    return next(
        r
        for r in trades.list_position_overview(conn)
        if r["security_id"] == security_id
    )


@pytest.mark.parametrize("event_type", ["buy", "sell"])
def test_a_same_day_trade_without_any_amount_makes_daily_pnl_unknown(conn, event_type):
    """The 2026-10-31 review cases (code-review B4, audit test 5)."""
    flat(conn, "s")
    add(conn)
    add(
        conn,
        event_type=event_type,
        trade_date="2026-10-30",
        quantity=10,
        unit_price=None,
    )
    holding = row(conn)
    assert holding["daily_pnl"] is None
    assert holding["daily_pnl_unknown"] is True


@pytest.mark.parametrize(
    "fields,expected",
    [
        ({"unit_price": None, "executed_amount": "1000000", "commission": "0"}, "0"),
        ({"unit_price": "1000", "commission": "0"}, "0"),
    ],
    ids=["amount-only", "price-only"],
)
def test_a_known_amount_still_counts(conn, fields, expected):
    flat(conn, "s")
    add(conn)
    add(conn, trade_date="2026-10-30", **fields)
    holding = row(conn)
    assert holding["daily_pnl"] == expected
    assert holding["daily_pnl_unknown"] is False


def test_unknown_unrealized_pnl_everywhere_is_not_a_zero_total(conn):
    """audit test 4: one holding without cost, the other without a close."""
    conn.execute(
        "INSERT INTO daily_prices(security_id,trade_date,close) "
        "VALUES('s','2026-10-30','1000')"
    )
    add(conn, event_type="opening", unit_price=None)
    add(conn, security_id="t", unit_price="200")
    overview = trades.list_position_overview(conn)
    assert all(r["unrealized_pnl"] is None for r in overview)
    summary = trades.compute_portfolio_summary(overview)
    assert summary["total_unrealized_pnl"] is None
    assert summary["total_unrealized_pnl_pct"] is None
    assert summary["unrealized_missing_count"] == 2


def test_partial_totals_leave_out_unknown_rows_and_say_so(conn, tmp_path):
    flat(conn, "s", close="1100")
    flat(conn, "t", close="200")
    add(conn)
    add(conn, security_id="t", event_type="opening", unit_price=None)
    add(conn, security_id="t", trade_date="2026-10-30", unit_price=None)

    summary = trades.compute_portfolio_summary(trades.list_position_overview(conn))
    assert summary["total_unrealized_pnl"] == str(row(conn)["unrealized_pnl"])
    assert Decimal(summary["total_unrealized_pnl_pct"]) == Decimal(
        row(conn)["unrealized_pnl_pct"]
    )
    assert summary["unrealized_missing_count"] == 1
    assert summary["total_daily_pnl"] == row(conn)["daily_pnl"]
    assert summary["daily_pnl_unknown_count"] == 1

    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        page = client.get("/").text
    assert "1 檔未計入" in page
    assert "1 檔交易金額未知" in page


def test_complete_data_adds_no_note(conn, tmp_path):
    flat(conn, "s")
    add(conn)
    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        page = client.get("/").text
    assert "檔未計入" not in page and "交易金額未知" not in page
