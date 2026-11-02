"""Integration tests for TPEx catalog sync against a mocked transport.

No real network access — DEVELOPMENT.md §2.4.2 test isolation and
design 002 §5.1 both expect CI to run on canned/replayable data, not
live calls.
"""

import httpx2
import pytest

from taiwan_stock.catalog import sync, tpex_adapter
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection

STOCK_ROWS = [
    {
        "Date": "1150910",
        "SecuritiesCompanyCode": "1240",
        "CompanyName": "茂生農經股份有限公司",
        "CompanyAbbreviation": "茂生農經",
        "SecuritiesIndustryCode": "33",
        "StockTransferAgent": "元大證券股份有限公司股務代理部",
        "StockTransferAgentTelephone": "02-2586-5859",
    },
    {
        "Date": "1150910",
        "SecuritiesCompanyCode": "1259",
        "CompanyName": "安心食品服務股份有限公司",
        "CompanyAbbreviation": "安心",
        "SecuritiesIndustryCode": "16",
        "StockTransferAgent": "",
        "StockTransferAgentTelephone": "",
    },
]


def _mock_client() -> httpx2.Client:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith(tpex_adapter.STOCK_BASIC_INFO_PATH):
            return httpx2.Response(200, json=STOCK_ROWS)
        return httpx2.Response(404, json={"error": "unexpected path"})

    return httpx2.Client(transport=httpx2.MockTransport(handler))


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    yield connection
    connection.close()


def test_sync_tpex_stock_catalog_inserts_rows(conn):
    count = sync.sync_stock_catalog(conn, _mock_client(), adapter=tpex_adapter)

    assert count == 2
    stored = conn.execute(
        "SELECT market, code, name, transfer_agent_name FROM securities ORDER BY code"
    ).fetchall()
    assert [dict(row) for row in stored] == [
        {
            "market": "TPEx",
            "code": "1240",
            "name": "茂生農經",
            "transfer_agent_name": "元大證券股份有限公司股務代理部",
        },
        {"market": "TPEx", "code": "1259", "name": "安心", "transfer_agent_name": None},
    ]


def test_tpex_and_twse_securities_with_the_same_code_coexist(conn):
    from taiwan_stock.catalog import twse_adapter

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith(twse_adapter.STOCK_BASIC_INFO_PATH):
            return httpx2.Response(
                200,
                json=[
                    {
                        "公司代號": "1240",
                        "公司名稱": "台灣同代號公司",
                        "公司簡稱": "同代號",
                        "產業別": "01",
                        "股票過戶機構": "",
                        "過戶電話": "",
                    }
                ],
            )
        if request.url.path.endswith(tpex_adapter.STOCK_BASIC_INFO_PATH):
            return httpx2.Response(200, json=STOCK_ROWS)
        return httpx2.Response(404, json={"error": "unexpected path"})

    client = httpx2.Client(transport=httpx2.MockTransport(handler))

    sync.sync_stock_catalog(conn, client)
    sync.sync_stock_catalog(conn, client, adapter=tpex_adapter)

    rows = conn.execute(
        "SELECT market, code FROM securities WHERE code = '1240' ORDER BY market"
    ).fetchall()
    assert [dict(row) for row in rows] == [
        {"market": "TPEx", "code": "1240"},
        {"market": "TWSE", "code": "1240"},
    ]
