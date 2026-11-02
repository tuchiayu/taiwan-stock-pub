"""Design 059: yields compare restated cash with today's shares and price."""

from contextlib import closing
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.catalog import query
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import corporate_actions, distribution_entitlement, trades
from taiwan_stock.shared.dates import _business_date

AS_OF = "2026-10-31"


@pytest.fixture(autouse=True)
def fixed_day():
    token = _business_date.set(AS_OF)
    yield
    _business_date.reset(token)


@pytest.fixture
def conn(tmp_path):
    """0050 bought at 160, paid 4 on 01-20, split 1→4 on 06-01, closes at 40."""
    with closing(get_connection(tmp_path)) as conn:
        migrate.apply_pending(conn)
        conn.execute(
            "INSERT INTO broker_accounts(id,name,status) VALUES('a','主帳戶','active')"
        )
        conn.execute(
            "INSERT INTO securities(id,code,name,market,security_type,status,"
            "listed_date) VALUES('s','0050','元大台灣50','TWSE','etf','active',"
            "'2003-06-30')"
        )
        trades.create_trade(
            conn,
            {
                "broker_account_id": "a",
                "security_id": "s",
                "event_type": "buy",
                "trade_date": "2026-01-05",
                "quantity": 1000,
                "unit_price": "160.00",
                "commission": "0",
            },
        )
        conn.execute(
            "INSERT INTO distributions(id,security_id,ex_dividend_date,pay_date,"
            "amount_per_share,amount_status,fetched_at) VALUES('d','s','2026-01-20',"
            "'2026-02-20','4.00','final','2026-10-31T00:00:00Z')"
        )
        corporate_actions.create_corporate_action(
            conn,
            {
                "security_id": "s",
                "action_type": "split",
                "ex_date": "2026-06-01",
                "ratio_numerator": 4,
                "ratio_denominator": 1,
            },
        )
        conn.execute(
            "INSERT INTO daily_prices(security_id,trade_date,close) "
            "VALUES('s','2026-10-30','40.00')"
        )
        yield conn


def test_a_split_no_longer_inflates_holding_yields(conn):
    holding = trades.list_position_overview(conn)[0]
    assert holding["position"].remaining_quantity == 4000
    assert holding["dividend_yield"] == "2.50"
    assert holding["cost_yield"] == "2.50"


def test_cost_yield_matches_received_yield_after_a_split(tmp_path):
    """The 2026-10-31 code review case: 100 shares, 4 paid, then 1→4."""
    with closing(get_connection(tmp_path)) as conn:
        migrate.apply_pending(conn)
        conn.execute(
            "INSERT INTO broker_accounts(id,name,status) VALUES('a','A','active')"
        )
        conn.execute(
            "INSERT INTO securities(id,market,code,name,security_type,status) "
            "VALUES('s','TWSE','0050','Synthetic','etf','active')"
        )
        conn.execute(
            "INSERT INTO etf_profiles(security_id,distribution_frequency) "
            "VALUES('s','annual')"
        )
        trades.create_trade(
            conn,
            {
                "broker_account_id": "a",
                "security_id": "s",
                "event_type": "opening",
                "trade_date": "2025-01-01",
                "quantity": 100,
                "executed_amount": "10000",
            },
        )
        conn.execute(
            "INSERT INTO distributions(id,security_id,ex_dividend_date,pay_date,"
            "amount_per_share,amount_status) VALUES('d','s','2026-09-02',"
            "'2026-10-28','4','final')"
        )
        corporate_actions.create_corporate_action(
            conn,
            {
                "security_id": "s",
                "action_type": "split",
                "ex_date": "2026-10-01",
                "ratio_numerator": 4,
                "ratio_denominator": 1,
            },
        )
        conn.execute(
            "INSERT INTO daily_prices(security_id,trade_date,close,volume) "
            "VALUES('s','2026-10-30','25',1000)"
        )
        row = trades.list_position_overview(conn)[0]
    assert row["cost_yield"] == row["dividend_yield"] == row["received_yield"] == "4.00"


def test_every_page_shows_the_same_restated_yield(conn, tmp_path):
    listed = query.browse(conn, mode="etf")["results"][0]["yield_info"]
    held = trades.list_position_overview(conn)[0]["yield_info"]
    assert listed["known_yield"] == held["known_yield"] == Decimal("2.5")
    assert "已依股數異動換算" in listed["notes"]

    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        detail = client.get("/securities/s").text
    assert '<span class="cell-note">原 4.00</span>' in detail
    assert "2.50%" in detail


def test_entitlements_keep_the_announced_amount(conn):
    entry = distribution_entitlement.list_entitlements(conn)[0]
    assert entry.amount_per_share == "4.00"
    assert entry.expected_gross == Decimal("4000")
    assert distribution_entitlement.confirmed_income_by_pair(conn)[("a", "s")] == 4000


def test_a_capital_reduction_hides_the_yield(conn, tmp_path):
    corporate_actions.create_corporate_action(
        conn,
        {
            "security_id": "s",
            "action_type": "cash_capital_reduction",
            "ex_date": "2026-08-01",
        },
    )
    holding = trades.list_position_overview(conn)[0]
    assert holding["dividend_yield"] is None and holding["cost_yield"] is None

    with TestClient(create_app(tmp_path), base_url="http://127.0.0.1") as client:
        detail = client.get("/securities/s").text
    assert "股數異動無法換算" in detail
