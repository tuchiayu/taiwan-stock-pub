"""Integration tests for syncing ex-dividend notices (no live network)."""

import httpx2
import pytest

from taiwan_stock.catalog import sync_stock_dividends, twse_adapter
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection

NOTICE_ROWS = [
    {
        "Date": "1150916",
        "Code": "2330",
        "Name": "台積電",
        "StockDividendRatio": "",
        "CashDividend": "7.000001",
    },
    {
        "Date": "1150908",
        "Code": "8473",
        "Name": "山林水",
        "StockDividendRatio": "0.10592943",
        "CashDividend": "0.782345",
    },
    {
        # Not in the seeded securities catalog below.
        "Date": "1150910",
        "Code": "9999",
        "Name": "未知代碼",
        "StockDividendRatio": "",
        "CashDividend": "1.000000",
    },
]


def _mock_client() -> httpx2.Client:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith(twse_adapter.EX_DIVIDEND_NOTICE_PATH):
            return httpx2.Response(200, json=NOTICE_ROWS)
        return httpx2.Response(404, json={"error": "unexpected path"})

    return httpx2.Client(transport=httpx2.MockTransport(handler))


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    connection.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('sec-2330', 'TWSE', '2330', '台積電', 'stock', 'active')"
    )
    connection.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('sec-8473', 'TWSE', '8473', '山林水', 'stock', 'active')"
    )
    yield connection
    connection.close()


def test_sync_writes_distributions_and_corporate_actions(conn):
    result = sync_stock_dividends.sync_ex_dividend_notices(conn, _mock_client())

    assert result == {
        "distributions": 2,
        "corporate_actions": 1,
        "skipped_unknown_security": 1,
        "skipped_invalid_ratio": 0,
        "validation_errors": [],
        "reconciliation_warnings": [],
    }

    tsmc_distribution = conn.execute(
        "SELECT amount_per_share, amount_status FROM distributions "
        "WHERE security_id = 'sec-2330'"
    ).fetchone()
    assert tsmc_distribution["amount_per_share"] == "7.0000"
    assert tsmc_distribution["amount_status"] == "final"

    mixed_action = conn.execute(
        "SELECT action_type, ratio_numerator, ratio_denominator FROM corporate_actions "
        "WHERE security_id = 'sec-8473'"
    ).fetchone()
    assert mixed_action["action_type"] == "stock_dividend"
    assert mixed_action["ratio_numerator"] == 110592943
    assert mixed_action["ratio_denominator"] == 100000000


def test_resyncing_updates_rather_than_duplicates(conn):
    sync_stock_dividends.sync_ex_dividend_notices(conn, _mock_client())
    sync_stock_dividends.sync_ex_dividend_notices(conn, _mock_client())

    count = conn.execute("SELECT COUNT(*) AS n FROM distributions").fetchone()["n"]
    assert count == 2


def test_synced_stock_dividend_immediately_affects_position(conn):
    from taiwan_stock.portfolio import trades

    account_id = "acct-1"
    conn.execute(
        "INSERT INTO broker_accounts (id, name, status) VALUES (?, 'Test', 'active')",
        (account_id,),
    )
    trades.create_trade(
        conn,
        {
            "broker_account_id": account_id,
            "security_id": "sec-8473",
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "10",
        },
    )

    sync_stock_dividends.sync_ex_dividend_notices(conn, _mock_client())

    position = trades.compute_position(
        conn, account_id, "sec-8473", as_of_date="2026-09-30"
    )
    # 1,000 shares x 1.10592943 ratio (already "new total per old share")
    # = 1,105.92943; the fraction is paid in cash, so 1,105 shares remain
    # (design 030).
    assert str(position.remaining_quantity) == "1105"
