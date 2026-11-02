"""Design 029: received dividend yield on the holdings pages."""

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import distribution_entitlement, trades

TODAY = "2026-10-17"


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(trades, "today_taipei", lambda: TODAY)
    monkeypatch.setattr(distribution_entitlement, "today_taipei", lambda: TODAY)


def seed(conn, *, top_up=True):
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('e1', 'TWSE', '00999', '季配 ETF', 'etf', 'active')"
    )
    conn.execute(
        "INSERT INTO etf_profiles (security_id, distribution_frequency) "
        "VALUES ('e1', 'quarterly')"
    )
    conn.execute(
        "INSERT INTO daily_prices (security_id, trade_date, close) "
        "VALUES ('e1', '2026-10-16', '20')"
    )
    for i, day in enumerate(("2025-10-20", "2026-01-16", "2026-04-16", "2026-07-21")):
        conn.execute(
            "INSERT INTO distributions (id, security_id, ex_dividend_date, "
            "amount_per_share, amount_status) VALUES (?, 'e1', ?, '0.5', 'final')",
            (f"d{i}", day),
        )
    conn.execute(
        "INSERT INTO broker_accounts (id, name, status) "
        "VALUES ('a', '測試證券', 'active')"
    )
    buys = [("t1", "2025-01-02", 10000, "200000")]
    if top_up:
        buys.append(("t2", "2026-07-18", 15000, "300000"))
    for trade_id, day, quantity, amount in buys:
        conn.execute(
            "INSERT INTO trades (id, broker_account_id, security_id, event_type, "
            "trade_date, quantity, executed_amount, commission, created_at) "
            "VALUES (?, 'a', 'e1', 'buy', ?, ?, ?, '0', ?)",
            (trade_id, day, quantity, amount, f"{day}T00:00:00Z"),
        )


@pytest.fixture
def conn(tmp_path):
    conn = get_connection(tmp_path)
    migrate.apply_pending(conn)
    yield conn
    conn.close()


def test_holding_a_full_year_matches_the_cost_yield(conn):
    seed(conn, top_up=False)

    [position] = trades.list_position_overview(conn)

    assert position["cost_yield"] == "10.00"  # 2.0 × 10000 ÷ 200000
    assert position["received_yield"] == "10.00"
    assert position["received_yield_label"] is None


def test_top_up_is_weighted_by_the_days_it_was_invested(conn):
    seed(conn)

    [position] = trades.list_position_overview(conn)

    # Received: 3 × 5000 before the top-up + 25000 × 0.5 on 07-21 = 27500.
    # Average cost: 200000 + 300000 × 92 ÷ 365 ≈ 275616.44.
    assert position["received_yield"] == "9.98"
    assert position["cost_yield"] == "10.00"  # 2.0 × 25000 ÷ 500000


def test_pages_show_the_column_and_sort_by_it(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        seed(conn)
        conn.commit()
        conn.close()

        home = client.get("/").text
        sorted_home = client.get("/", params={"sort": "received_yield"}).text
        detail = client.get("/securities/e1").text

    assert 'data-sort-key="received_yield"' in home
    assert 'data-sort="9.98" class="col-number">9.98%' in home
    assert "<dt>實際領息殖利率</dt><dd>9.98%" in home  # mobile card
    assert "<dt>實際領息殖利率</dt><dd>近 12 個月實際領到的配息" in home  # calc notes
    assert '<option value="received_yield" selected>實際領息殖利率</option>' in (
        sorted_home
    )
    assert ">實際領息殖利率</th>" in detail
    assert '<td class="col-number">9.98%</td>' in detail
