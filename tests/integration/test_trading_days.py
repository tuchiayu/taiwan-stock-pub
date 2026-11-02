"""Issue #89: one "previous trading day" rule for every price change."""

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.catalog import query
from taiwan_stock.catalog.trading_days import TradingCalendar
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import trades


@pytest.fixture
def conn(tmp_path):
    conn = get_connection(tmp_path)
    migrate.apply_pending(conn)
    yield conn
    conn.close()


def event(conn, day, status="closed", source="annual"):
    conn.execute(
        "INSERT INTO market_calendar_events "
        "(source, year, event_key, date, status, name, title, source_url, "
        " fingerprint, updated_at) "
        "VALUES (?, ?, ?, ?, ?, '國慶日', '國慶日', 'https://example.test', "
        "'f', 'now')",
        (source, int(day[:4]), f"{source}-{day}", day, status),
    )


@pytest.mark.parametrize(
    ("earlier", "later", "expected"),
    [
        ("2026-10-15", "2026-10-16", True),
        ("2026-10-16", "2026-10-19", True),  # Friday to Monday
        ("2026-10-14", "2026-10-16", False),  # a weekday missing
        ("2026-10-16", "2026-10-16", False),
        ("2026-10-16", "2026-10-15", False),
        (None, "2026-10-16", False),
    ],
)
def test_weekdays_without_calendar_data(conn, earlier, later, expected):
    assert TradingCalendar(conn).adjacent(earlier, later) is expected


def test_known_holiday_makes_the_gap_adjacent(conn):
    assert TradingCalendar(conn).adjacent("2026-10-08", "2026-10-12") is False
    event(conn, "2026-10-09")

    assert TradingCalendar(conn).adjacent("2026-10-08", "2026-10-12") is True


def test_closure_notice_counts_as_a_closed_day(conn):
    event(conn, "2026-10-14", source="notices")  # e.g. a typhoon day

    assert TradingCalendar(conn).adjacent("2026-10-13", "2026-10-15") is True


def test_unconfirmed_day_between_is_not_adjacent(conn):
    event(conn, "2026-10-14", status="needs_review", source="notices")

    assert TradingCalendar(conn).adjacent("2026-10-13", "2026-10-15") is False


def test_weekend_marked_open_is_a_trading_day(conn):
    event(conn, "2026-10-17", status="open")  # a make-up trading Saturday

    assert TradingCalendar(conn).adjacent("2026-10-16", "2026-10-19") is False


def test_confirmed_notice_beats_the_annual_table(conn):
    event(conn, "2026-10-14", status="open")
    event(conn, "2026-10-14", source="notices")

    assert TradingCalendar(conn).adjacent("2026-10-13", "2026-10-15") is True


def seed_prices(conn, *closes, security_type="stock"):
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('s1', 'TWSE', '1314', '中石化', ?, 'active')",
        (security_type,),
    )
    for day, close in closes:
        conn.execute(
            "INSERT INTO daily_prices (security_id, trade_date, close) "
            "VALUES ('s1', ?, ?)",
            (day, close),
        )


def test_list_shows_the_change_after_a_known_holiday(conn):
    seed_prices(conn, ("2026-10-08", "100"), ("2026-10-12", "105"))
    event(conn, "2026-10-09")

    [row] = query.browse(conn, mode="stock")["results"]

    assert row["change_pct"] == 5


def test_detail_page_does_not_show_a_multi_day_change(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        seed_prices(conn, ("2026-09-30", "100"), ("2026-10-16", "120"))
        conn.commit()
        conn.close()

        page = client.get("/securities/s1").text

    assert "2026-10-16 收盤 120" in page
    assert "較前一有效交易日" not in page
    assert "（漲跌 —，前一交易日收盤未同步）" in page


def test_detail_page_still_shows_an_adjacent_change(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        seed_prices(conn, ("2026-10-15", "100"), ("2026-10-16", "120"))
        conn.commit()
        conn.close()

        page = client.get("/securities/s1").text

    assert "較前一有效交易日 +20" in page
    assert "前一交易日收盤未同步" not in page


def test_daily_pnl_is_unknown_across_a_price_gap(conn):
    # Before #89 the 1000 shares bought inside the gap counted as profit.
    seed_prices(conn, ("2026-09-30", "100"), ("2026-10-16", "101"))
    conn.execute(
        "INSERT INTO broker_accounts (id, name, status) VALUES ('a', '測試', 'active')"
    )
    for trade_id, day in (("t1", "2026-09-01"), ("t2", "2026-10-08")):
        conn.execute(
            "INSERT INTO trades (id, broker_account_id, security_id, event_type, "
            "trade_date, quantity, unit_price, executed_amount, commission, "
            "created_at) VALUES (?, 'a', 's1', 'buy', ?, 1000, '100', '100000', "
            "'0', ?)",
            (trade_id, day, f"{day}T00:00:00Z"),
        )

    [position] = trades.list_position_overview(conn)

    assert position["market_value"] == "202000"
    assert position["daily_pnl"] is None
