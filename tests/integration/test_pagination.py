"""Trade navigation follows real links without losing the filtered result set."""

import re
from contextlib import closing
from html import unescape
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app


@pytest.mark.parametrize(
    "count,requested", [(0, 1), (1, 1), (51, 1), (51, 2), (51, 3), (51, 999)]
)
def test_trade_pagination_preserves_filters_and_lands_on_correct_records(
    tmp_path, count, requested
):
    with TestClient(create_app(tmp_path)) as client:
        with closing(get_connection(tmp_path)) as conn:
            conn.execute(
                "INSERT INTO securities(id,market,code,name,security_type,status) "
                "VALUES('s','TWSE','2330','A&B 台灣測試','stock','active')"
            )
            conn.executemany(
                "INSERT INTO broker_accounts(id,name,status) VALUES(?,?,'active')",
                [("a", "帳戶 A"), ("b", "帳戶 B")],
            )
            conn.executemany(
                "INSERT INTO trades(id,broker_account_id,security_id,event_type,"
                "trade_date,quantity,unit_price) "
                "VALUES(?,'a','s','buy','2026-01-02',1000,'50')",
                [(f"trade-{i:03}",) for i in range(count)],
            )
            # These rows must not affect the page count or appear on any filtered page.
            conn.execute(
                "INSERT INTO trades(id,broker_account_id,security_id,event_type,"
                "trade_date,quantity,unit_price) "
                "VALUES('other','b','s','buy','2026-01-02',1000,'50')"
            )
        state = {
            "q": "A&B 台灣",
            "broker_account_id": "a",
            "date_from": "2026-01-01",
            "date_to": "2026-01-28",
            "event_type": "buy",
            "page_size": 25,
        }
        response = client.get("/transactions", params={**state, "page": requested})
        assert response.status_code == 200
        assert f"符合 {count} 筆" in response.text
        pages = 3 if count == 51 else 1
        current = min(pages, requested)
        nav = re.search(r'<nav class="pagination".*?</nav>', response.text, re.S)[0]
        assert 'aria-label="交易分頁"' in nav
        assert f"第 {current} 頁／共 {pages} 頁" in nav
        disabled = re.findall(r'<span aria-disabled="true">([^<]+)</span>', nav)
        assert disabled == (
            (["第一頁", "上一頁"] if current == 1 else [])
            + (["下一頁", "最後一頁"] if current == pages else [])
        )
        links = dict(
            (label, unescape(href))
            for href, label in re.findall(r'<a href="([^"]+)"[^>]*>([^<]+)</a>', nav)
        )
        expected = {}
        if current > 1:
            expected.update({"第一頁": 1, "上一頁": current - 1})
            assert 'rel="prev"' in nav
        if current < pages:
            expected.update({"下一頁": current + 1, "最後一頁": pages})
            assert 'rel="next"' in nav
        assert links.keys() == expected.keys()
        for label, target in expected.items():
            parsed = urlsplit(links[label])
            assert parsed.path == "/transactions"
            assert parse_qs(parsed.query) == {
                key: [str(value)] for key, value in {**state, "page": target}.items()
            }
            landed = client.get(links[label])
            assert landed.status_code == 200
            assert f"第 {target} 頁／共 {pages} 頁" in landed.text
            table = re.search(
                r'<table class="transactions-table".*?</table>', landed.text, re.S
            )[0]
            ids = re.findall(r'href="/transactions/(trade-\d+)/edit\?', table)
            assert ids == [
                f"trade-{i:03}"
                for i in range(
                    count - 25 * (target - 1) - 1, max(-1, count - 25 * target - 1), -1
                )
            ]
        if not count:
            assert "目前沒有符合條件的交易" in response.text
