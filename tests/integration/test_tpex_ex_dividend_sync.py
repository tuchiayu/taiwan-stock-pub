"""Integration tests for syncing TPEx ex-dividend notices (no live network)."""

import httpx2
import pytest

from taiwan_stock.catalog import sync_stock_dividends, tpex_adapter
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection

NOTICE_ROWS = [
    {
        "ExRrightsExDividendDate": "1150901",
        "SecuritiesCompanyCode": "3078",
        "CompanyName": "僑威",
        "StockDividendRatio": "0.00000000",
        "CashDividend": "4.00000000",
    },
    {
        "ExRrightsExDividendDate": "1150901",
        "SecuritiesCompanyCode": "4760",
        "CompanyName": "勤凱",
        "StockDividendRatio": "0.01993215",
        "CashDividend": "0.00000000",
    },
    {
        # Not in the seeded securities catalog below.
        "ExRrightsExDividendDate": "1150901",
        "SecuritiesCompanyCode": "9999",
        "CompanyName": "未知代碼",
        "StockDividendRatio": "0.00000000",
        "CashDividend": "1.00000000",
    },
]


def _mock_client() -> httpx2.Client:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith(tpex_adapter.EX_DIVIDEND_NOTICE_PATH):
            return httpx2.Response(200, json=NOTICE_ROWS)
        return httpx2.Response(404, json={"error": "unexpected path"})

    return httpx2.Client(transport=httpx2.MockTransport(handler))


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    connection.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('sec-3078', 'TPEx', '3078', '僑威', 'stock', 'active')"
    )
    connection.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('sec-4760', 'TPEx', '4760', '勤凱', 'stock', 'active')"
    )
    yield connection
    connection.close()


def test_sync_writes_distributions_and_corporate_actions(conn):
    result = sync_stock_dividends.sync_ex_dividend_notices(
        conn, _mock_client(), adapter=tpex_adapter
    )

    assert result == {
        "distributions": 1,
        "corporate_actions": 1,
        "skipped_unknown_security": 1,
        "skipped_invalid_ratio": 0,
        "validation_errors": [],
        "reconciliation_warnings": [],
    }

    cash = conn.execute(
        "SELECT amount_per_share FROM distributions WHERE security_id = 'sec-3078'"
    ).fetchone()
    assert cash["amount_per_share"] == "4.0000"

    stock_dividend = conn.execute(
        "SELECT ratio_numerator, ratio_denominator FROM corporate_actions "
        "WHERE security_id = 'sec-4760'"
    ).fetchone()
    assert stock_dividend["ratio_numerator"] == 101993215
    assert stock_dividend["ratio_denominator"] == 100000000


def test_only_resolves_tpex_codes_not_twse_codes_with_the_same_number(conn):
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('sec-3078-twse', 'TWSE', '3078', '台灣同代號', 'stock', 'active')"
    )

    sync_stock_dividends.sync_ex_dividend_notices(
        conn, _mock_client(), adapter=tpex_adapter
    )

    twse_distribution = conn.execute(
        "SELECT id FROM distributions WHERE security_id = 'sec-3078-twse'"
    ).fetchone()
    assert twse_distribution is None
