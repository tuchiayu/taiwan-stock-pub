"""Tests for `/securities` (search + the fast catalog-list sync button).

The sync button calls the real TWSE/TPEx adapter functions but
through a mocked transport — see test_catalog_sync.py for why (no
live network in CI/tests).

ETF distributions/investment-trust are the slow, per-unit-throttled
syncs (design 005 §4.4 follow-up) — they no longer have a web route at
all, only `taiwan-stock sync ...` (see cli.py's module docstring), so
their tests here drive `cli.main` directly with `httpx2.Client`
monkeypatched onto the same mocked transport, rather than posting to a
route that no longer exists.
"""

import json
import re
from html import unescape
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from fastapi.testclient import TestClient
from sync_helpers import load_job

from taiwan_stock import cli
from taiwan_stock.catalog import query, tpex_adapter, twse_adapter
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.shared.dates import today_taipei
from taiwan_stock.web.deps import get_http_client


@pytest.mark.parametrize(
    ("security_type", "view", "pay_month", "columns"),
    [
        ("stock", "all", "", 8),
        ("stock", "watchlist", "", 8),
        ("etf", "all", "", 12),
        ("etf", "all", "2026-10", 13),
        ("etf", "watchlist", "", 12),
        ("etf", "watchlist", "2026-10", 13),
    ],
)
def test_empty_results_span_the_actual_columns(
    tmp_path, security_type, view, pay_month, columns
):
    with TestClient(create_app(tmp_path)) as client:
        response = client.get(
            "/securities",
            params={
                "security_type": security_type,
                "view": view,
                "pay_month": pay_month,
            },
        )
    assert response.status_code == 200
    table = re.search(
        r'<table\b[^>]*class="securities-table\b[^"]*"[^>]*>(.*?)</table>',
        response.text,
        re.S,
    )[1]
    header = re.search(r"<thead>(.*?)</thead>", table, re.S)[1]
    header_count = len(re.findall(r"<th\b", header))
    empty_span = int(re.search(r'<td colspan="(\d+)" class="empty">', table)[1])
    assert header_count == columns
    assert empty_span == header_count
    navigation = re.search(r'<nav class="pagination".*?</nav>', response.text, re.S)[0]
    assert "<a href=" not in navigation
    assert navigation.count('aria-disabled="true"') == 4


@pytest.mark.parametrize(
    ("previous", "current", "display", "color"),
    [
        ("100", "105", "+5.00%", "gain"),
        ("100", "95", "-5.00%", "loss"),
        ("100", "100", "0.00%", "unchanged"),
        (None, "100", "—", None),
    ],
)
def test_stock_catalog_shows_eight_columns_and_separates_price_metadata(
    client, tmp_path, previous, current, display, color
):
    conn = get_connection(tmp_path)
    conn.execute(
        "INSERT INTO securities (id,market,code,name,security_type,status,industry) "
        "VALUES ('quote','TWSE','1314','中石化','stock','active','03')"
    )
    conn.execute(
        "INSERT INTO daily_prices (security_id,trade_date,close) "
        "VALUES ('quote','2026-10-16',?)",
        (current,),
    )
    if previous:
        conn.execute(
            "INSERT INTO daily_prices (security_id,trade_date,close) "
            "VALUES ('quote','2026-10-15',?)",
            (previous,),
        )
    conn.commit()
    conn.close()
    response = client.get(
        "/securities", params={"security_type": "stock", "view": "all"}
    )
    assert response.status_code == 200
    table = re.search(
        r'<table class="securities-table".*?</table>', response.text, re.S
    )[0]
    assert len(re.findall(r'<th scope="col"', table)) == 8
    assert '<option value="03">塑膠工業（03）</option>' in response.text
    assert 'title="產業代碼 03">塑膠工業</td>' in table
    quote = re.search(r'<td class="col-number col-quote">(.*?)</td>', table, re.S)[1]
    change = re.search(r'<td class="col-number col-change">(.*?)</td>', table, re.S)[1]
    dividend = re.search(r'<td class="col-number col-yield">(.*?)</td>', table, re.S)[1]
    assert current in quote and "2026/10/16" in quote
    assert display in change
    if color:
        assert f'class="{color}"' in change
    assert "收盤 " not in dividend and "2026/10/16" not in dividend
    assert "產業 塑膠工業" in response.text  # mobile
    assert "最新收盤 " in response.text and "漲跌幅 " in response.text
    price_sort = client.get(
        "/securities", params={"security_type": "stock", "sort": "latest_close"}
    )
    assert re.search(
        r'class="col-number col-quote" aria-sort="descending"', price_sort.text
    )
    change_sort = client.get(
        "/securities", params={"security_type": "stock", "sort": "change_pct"}
    )
    assert re.search(
        r'class="col-number col-change" aria-sort="descending"', change_sort.text
    )


@pytest.mark.parametrize(
    ("previous", "current", "display", "color"),
    [
        ("30", "31.5", "+5.00%", "gain"),
        ("30", "28.5", "-5.00%", "loss"),
        (None, "30", "—", None),
    ],
)
def test_etf_catalog_shows_quote_columns_before_yield(
    client, tmp_path, previous, current, display, color
):
    conn = get_connection(tmp_path)
    conn.execute(
        "INSERT INTO securities (id,market,code,name,security_type,status) "
        "VALUES ('etf','TWSE','0056','元大高股息','etf','active')"
    )
    conn.execute("INSERT INTO etf_profiles (security_id) VALUES ('etf')")
    conn.execute(
        "INSERT INTO daily_prices (security_id,trade_date,close) "
        "VALUES ('etf','2026-10-16',?)",
        (current,),
    )
    if previous:
        conn.execute(
            "INSERT INTO daily_prices (security_id,trade_date,close) "
            "VALUES ('etf','2026-10-15',?)",
            (previous,),
        )
    conn.commit()
    conn.close()
    response = client.get("/securities", params={"security_type": "etf"})
    assert response.status_code == 200
    table = re.search(
        r'<table class="securities-table".*?</table>', response.text, re.S
    )[0]
    headers = re.findall(r'<a class="sort-heading"[^>]*>([^<]+)<', table)
    assert headers[-3:] == ["最新收盤", "漲跌幅", "近一年殖利率"]
    quote = re.search(r'<td class="col-number col-quote">(.*?)</td>', table, re.S)[1]
    change = re.search(r'<td class="col-number col-change">(.*?)</td>', table, re.S)[1]
    dividend = re.search(r'<td class="col-number col-yield">(.*?)</td>', table, re.S)[1]
    assert current in quote and "2026/10/16" in quote
    assert display in change
    if color:
        assert f'class="{color}"' in change
    assert "收盤 " not in dividend and "2026/10/16" not in dividend
    mobile = re.search(r'<ul class="mobile-records".*?</ul>', response.text, re.S)[0]
    assert "最新收盤 " in mobile and "漲跌幅 " in mobile
    assert mobile.count("收盤 2026/10/16") == 1
    assert "產業 " not in mobile
    for sort, css in (("latest_close", "col-quote"), ("change_pct", "col-change")):
        sorted_page = client.get(
            "/securities", params={"security_type": "etf", "sort": sort}
        )
        assert re.search(
            rf'class="col-number {css}" aria-sort="descending"', sorted_page.text
        )


def test_etf_quote_sort_runs_before_pagination_and_keeps_unknown_last(tmp_path):
    conn = get_connection(tmp_path)
    migrate.apply_pending(conn)
    for i in range(53):
        sid = f"etf-{i}"
        conn.execute(
            "INSERT INTO securities (id,market,code,name,security_type,status) "
            "VALUES (?,'TWSE',?,'測試 ETF','etf','active')",
            (sid, f"00{900 + i}"),
        )
        if i < 52:
            conn.execute(
                "INSERT INTO daily_prices (security_id,trade_date,close) "
                "VALUES (?,'2026-10-16',?)",
                (sid, str(10 + i)),
            )
    first = query.browse(conn, mode="etf", sort="latest_close", direction="asc")
    last = query.browse(conn, mode="etf", sort="latest_close", direction="asc", page=2)
    conn.close()
    assert first["results"][0]["id"] == "etf-0"
    assert last["results"][-1]["id"] == "etf-52"


@pytest.mark.parametrize(
    ("kind", "view"),
    [("stock", "all"), ("stock", "watchlist"), ("etf", "all"), ("etf", "watchlist")],
)
@pytest.mark.parametrize("page", [1, 2, 3])
def test_pagination_boundaries_and_links_preserve_query(
    client, tmp_path, kind, view, page
):
    conn = get_connection(tmp_path)
    for code in range(1000, 1101):
        sid = f"pagination-{code}"
        conn.execute(
            "INSERT INTO securities "
            "(id, market, code, name, security_type, status, industry) "
            "VALUES (?, 'TWSE', ?, 'A&B 台灣測試', ?, 'active', '03')",
            (sid, str(code), kind),
        )
        if kind == "etf":
            conn.execute(
                "INSERT INTO etf_profiles "
                "(security_id, custodian_bank, distribution_frequency) "
                "VALUES (?, '玉山銀行', 'quarterly')",
                (sid,),
            )
        if view == "watchlist":
            conn.execute("INSERT INTO watchlist (security_id) VALUES (?)", (sid,))
    conn.commit()
    conn.close()
    state = {
        "security_type": kind,
        "view": view,
        "q": "A&B 台灣",
        "market": "TWSE",
        "sort": "code",
        "direction": "desc",
    }
    if kind == "stock":
        state["industry"] = "03"
    else:
        state.update(custodian_bank="玉山銀行", distribution_frequency="quarterly")
    response = client.get("/securities", params={**state, "page": page})
    assert response.status_code == 200
    assert f"第 {page} 頁／共 3 頁" in response.text
    navigation = re.search(r'<nav class="pagination".*?</nav>', response.text, re.S)[0]
    links = {
        label: unescape(href)
        for href, label in re.findall(r'<a href="([^"]+)"[^>]*>([^<]+)</a>', navigation)
    }
    disabled = re.findall(r'<span aria-disabled="true">([^<]+)</span>', navigation)
    expected = {}
    if page > 1:
        expected.update({"第一頁": 1, "上一頁": page - 1})
    else:
        assert disabled == ["第一頁", "上一頁"]
    if page < 3:
        expected.update({"下一頁": page + 1, "最後一頁": 3})
    else:
        assert disabled == ["下一頁", "最後一頁"]
    assert links.keys() == expected.keys()
    for label, target in expected.items():
        parsed = urlsplit(links[label])
        assert parsed.path == "/securities"
        assert parse_qs(parsed.query) == {
            key: [str(value)] for key, value in {**state, "page": target}.items()
        }
        landed = client.get(links[label])
        assert landed.status_code == 200
        assert f"第 {target} 頁／共 3 頁" in landed.text
        codes = re.findall(
            r'href="/securities/pagination-(\d+)">(\d+)</a>', landed.text
        )
        assert codes[0] == (str(1100 - 50 * (target - 1)),) * 2


@pytest.mark.parametrize(
    ("tier", "label"),
    [("official", "官方"), ("inferred", "歷史"), ("user_override", "自訂")],
)
def test_etf_values_keep_source_labels_separate_on_desktop_and_mobile(
    client, tmp_path, tier, label
):
    conn = get_connection(tmp_path)
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('source-etf', 'TWSE', '0050', '來源測試', 'etf', 'active')"
    )
    conn.execute(
        "INSERT INTO etf_profiles "
        "(security_id, custodian_bank, custodian_bank_user_override, "
        "distribution_frequency, distribution_frequency_inferred, "
        "distribution_frequency_user_override) VALUES (?, ?, ?, ?, ?, ?)",
        (
            "source-etf",
            "玉山銀行",
            "中國信託商業銀行" if tier == "user_override" else None,
            "annual" if tier == "official" else None,
            json.dumps({"value": "annual"}) if tier == "inferred" else None,
            "annual" if tier == "user_override" else None,
        ),
    )
    conn.execute("INSERT INTO watchlist (security_id) VALUES ('source-etf')")
    conn.commit()
    conn.close()

    bank_tier = "user_override" if tier == "user_override" else "official"
    bank_label = "自訂" if tier == "user_override" else "官方"
    bank = "中國信託商業銀行" if tier == "user_override" else "玉山銀行"
    for view in ("all", "watchlist", "detail"):
        response = (
            client.get("/securities/source-etf")
            if view == "detail"
            else client.get(
                "/securities", params={"security_type": "etf", "view": view}
            )
        )
        assert response.status_code == 200
        page = response.text
        for value, source_tier, source_label in (
            (bank, bank_tier, bank_label),
            ("年配", tier, label),
        ):
            # Plain values and source badges agree across all three views;
            # both list views show desktop + mobile (design 042).
            matches = re.findall(
                rf'<span class="profile-value">{value}</span>'
                rf'\s*(?:</span>\s*<span class="profile-line">\s*)?'
                rf'<span class="profile-source tier-{source_tier}"[^>]*>'
                rf"{source_label}</span>",
                page,
            )
            # Design 048 §4.2.5: the lists label only what is not official.
            if view == "detail":
                assert len(matches) == 1
            elif source_tier == "official":
                assert not matches
                assert page.count(f'<span class="profile-value">{value}</span>') == 2
            else:
                assert len(matches) == 2
        assert 'class="badge tier-' not in page
        if view != "detail":
            assert "依歷史配息紀錄判斷，非官方確認" in page
            assert " · 歷史 " in page


def _mock_client(monkeypatch, handler):
    """Point `cli.py`'s own `httpx2.Client(...)` at a mocked transport —
    it isn't built via FastAPI's `get_http_client` dependency, since the
    CLI has no FastAPI app to override."""
    real_client_cls = httpx2.Client
    monkeypatch.setattr(
        httpx2,
        "Client",
        lambda *a, **k: real_client_cls(transport=httpx2.MockTransport(handler)),
    )


STOCK_ROWS = [
    {
        "公司代號": "2330",
        "公司名稱": "台灣積體電路製造股份有限公司",
        "公司簡稱": "台積電",
        "產業別": "24",
        "股票過戶機構": "中國信託商業銀行 代理部",
        "過戶電話": "02-6636-5566",
    }
]
ETF_ROWS: list[dict] = []

TPEX_STOCK_ROWS = [
    {
        "Date": "1150910",
        "SecuritiesCompanyCode": "1240",
        "CompanyName": "茂生農經股份有限公司",
        "CompanyAbbreviation": "茂生農經",
        "SecuritiesIndustryCode": "33",
        "StockTransferAgent": "元大證券股份有限公司股務代理部",
        "StockTransferAgentTelephone": "02-2586-5859",
    }
]


ETF_DISTRIBUTION_FIELDS = [
    "證券代號",
    "證券簡稱",
    "除息交易日",
    "收益分配基準日",
    "收益分配發放日",
    "收益分配金額 (每1受益權益單位)",
    "收益分配標準 (102年度起啟用)",
    "公告年度",
]


TERMINATION_NOTICE_QUERY = "company=A00000&date=20200101&seq=1&fund=9999&type=listing"


def _mock_handler(request: httpx2.Request) -> httpx2.Response:
    if request.url.path.endswith(twse_adapter.STOCK_BASIC_INFO_PATH):
        return httpx2.Response(200, json=STOCK_ROWS)
    if request.url.path.endswith(twse_adapter.ETF_BASIC_INFO_PATH):
        return httpx2.Response(200, json=ETF_ROWS)
    if request.url.path.endswith(tpex_adapter.STOCK_BASIC_INFO_PATH):
        return httpx2.Response(200, json=TPEX_STOCK_ROWS)
    if request.url.path.endswith("/v1/company/suspendListingCsvAndHtml"):
        # A code that isn't in this fixture's catalog — exercises the
        # delisting sync without touching the securities under test.
        return httpx2.Response(
            200, json=[{"Code": "9999", "DelistingDate": "111/05/27"}]
        )
    if request.url.path.endswith("/www/zh-tw/company/deListed"):
        return httpx2.Response(
            200,
            json={
                "stat": "ok",
                "tables": [
                    {
                        "fields": ["股票代號", "終止上櫃日期", "終止上櫃原因"],
                        "totalCount": 1,
                        "data": [["9999", "111-05-27", "test"]],
                    }
                ],
            },
        )
    if request.url.path.endswith("/zh/ETFortune/announcementList"):
        return httpx2.Response(
            200,
            text=f'<a href="/zh/ETFortune/announcement?{TERMINATION_NOTICE_QUERY}">'
            "unrelated notice</a>",
        )
    if request.url.path.endswith("/api/etfMaInfo"):
        return httpx2.Response(
            200,
            json=[{"params": TERMINATION_NOTICE_QUERY, "subject": "unrelated notice"}],
        )
    if request.url.path.endswith("/rwd/zh/ETF/etfDiv"):
        # Year-aware like the real source: this 115 (2026) event only comes
        # back for 2026, and design 062 rejects one outside its year.
        if request.url.params.get("stkNo") == "0050" and request.url.params.get(
            "startDate", ""
        ).startswith("2026"):
            return httpx2.Response(
                200,
                json={
                    "status": "ok",
                    "fields": ETF_DISTRIBUTION_FIELDS,
                    "data": [
                        [
                            "0050",
                            "元大台灣50",
                            "115年08月18日",
                            "115年08月24日",
                            "115年09月11日",
                            "1.01",
                            "…",
                            115,
                        ]
                    ],
                },
            )
        return httpx2.Response(
            200, json={"status": "ok", "fields": ETF_DISTRIBUTION_FIELDS, "data": []}
        )
    if request.url.path.endswith("/api/etf"):
        # No TPEx ETFs in this shared fixture — most tests here don't
        # care about them, so the catalog stays as the TWSE/stock-only
        # counts the other assertions expect.
        return httpx2.Response(200, json={"query": "", "suggestions": []})
    if request.url.path.endswith("/api/etfExDiv"):
        return httpx2.Response(200, json=[])
    return httpx2.Response(404, json={"error": "unexpected path"})


def _failing_handler(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(500, json={"error": "boom"})


def _mock_http_client_override():
    with httpx2.Client(transport=httpx2.MockTransport(_mock_handler)) as client:
        yield client


def _failing_http_client_override():
    with httpx2.Client(transport=httpx2.MockTransport(_failing_handler)) as client:
        yield client


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path)
    app.dependency_overrides[get_http_client] = _mock_http_client_override
    with TestClient(app) as test_client:
        yield test_client


def test_securities_page_shows_empty_catalog_before_sync(client):
    response = client.get("/securities")

    assert response.status_code == 200
    assert "已同步 0 檔商品" in response.text


def test_sync_button_populates_catalog_and_redirects_with_count(client):
    response = client.post("/securities/sync", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/securities?job_id=")
    assert load_job(client, response)["result"]["stock_count"] == 1

    page = client.get(response.headers["location"])
    assert "已同步 2 檔商品" in page.text
    assert "同步完成" in page.text


def test_cli_sync_etf_distributions_reports_count_for_both_markets(
    tmp_path, monkeypatch, capsys
):
    conn = get_connection(tmp_path)
    migrate.apply_pending(conn)
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('e1', 'TWSE', '0050', '元大台灣50', 'etf', 'active')"
    )
    conn.commit()
    conn.close()

    _mock_client(monkeypatch, _mock_handler)
    exit_code = cli.main(["sync", "etf-distributions", "--data-dir", str(tmp_path)])
    assert exit_code == 0

    # The mock is year-aware (design 062): the single 2026 event comes
    # back once across the 3-year sync window, the other years are empty.
    # Also confirms TWSE/TPEx results stay separate (this used to be
    # `/api/v1/etf-distributions-sync`'s own coverage — that route no
    # longer exists).
    out = capsys.readouterr().out
    assert '"synced": 1' in out
    assert '"failed_units": []' in out
    assert '"tpex"' not in out  # printed separately, not nested
    assert "TPEx ETF 配息" in out

    security_id = "e1"
    conn = get_connection(tmp_path)
    row_count = conn.execute(
        "SELECT COUNT(*) AS n FROM distributions WHERE security_id = ?",
        (security_id,),
    ).fetchone()["n"]
    conn.close()
    assert row_count == 1


def test_cli_sync_etf_distributions_also_recomputes_inferred_frequency(
    tmp_path, monkeypatch
):
    # Design 004 §4.4.1 Phase F: chained onto the same sync so the
    # inference reflects newly-synced events without a separate trigger.
    def year_aware_handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith(twse_adapter.STOCK_BASIC_INFO_PATH):
            return httpx2.Response(200, json=[])
        if request.url.path.endswith(twse_adapter.ETF_BASIC_INFO_PATH):
            return httpx2.Response(200, json=[])
        if request.url.path.endswith(tpex_adapter.STOCK_BASIC_INFO_PATH):
            return httpx2.Response(200, json=[])
        if request.url.path.endswith("/rwd/zh/ETF/etfDiv"):
            start_date = request.url.params.get("startDate")
            rows = (
                [
                    ["0050", "元大台灣50", "115年04月15日", "", "", "1.0", "", 115],
                    ["0050", "元大台灣50", "115年07月15日", "", "", "1.0", "", 115],
                ]
                if start_date == "2026"
                else []
            )
            return httpx2.Response(
                200,
                json={"status": "ok", "fields": ETF_DISTRIBUTION_FIELDS, "data": rows},
            )
        if request.url.path.endswith("/api/etf"):
            return httpx2.Response(200, json={"query": "", "suggestions": []})
        if request.url.path.endswith("/api/etfExDiv"):
            return httpx2.Response(200, json=[])
        return httpx2.Response(404, json={"error": "unexpected path"})

    conn = get_connection(tmp_path)
    migrate.apply_pending(conn)
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('e1', 'TWSE', '0050', '元大台灣50', 'etf', 'active')"
    )
    conn.execute("INSERT INTO etf_profiles (security_id) VALUES ('e1')")
    conn.commit()
    conn.close()

    _mock_client(monkeypatch, year_aware_handler)
    assert cli.main(["sync", "etf-distributions", "--data-dir", str(tmp_path)]) == 0

    with TestClient(create_app(tmp_path)) as client:
        detail = client.get("/securities/e1")
    assert "季配" in detail.text
    assert "tier-inferred" in detail.text


def test_search_after_sync_finds_the_security_and_its_industry(client):
    # Design 004 §4.7.2: transfer agent only shows on the detail page
    # now, not in the search results table (see the detail-page test
    # below for that).
    client.post("/securities/sync", follow_redirects=False)

    response = client.get("/securities", params={"security_type": "stock", "q": "2330"})

    assert response.status_code == 200
    assert "台積電" in response.text
    assert "24" in response.text


def test_detail_page_still_shows_the_transfer_agent(client):
    client.post("/securities/sync", follow_redirects=False)

    listing = client.get("/securities", params={"security_type": "stock", "q": "2330"})
    detail_url = listing.text.split('href="/securities/')[1].split('"')[0]

    response = client.get(f"/securities/{detail_url}")

    assert response.status_code == 200
    assert "中國信託商業銀行 代理部" in response.text


def test_detail_page_labels_a_newly_listed_etf_distinctly_from_a_non_payer(
    client, tmp_path
):
    # design 005 §4.5 follow-up: zero distribution events reads
    # differently depending on whether the security had a full year to
    # pay and confirmed it didn't, or it simply listed too recently to
    # have had that chance yet (real case: 00400A listed 2026-04-09).
    # `listed_date` set to today guarantees "newly listed" regardless of
    # when this test actually runs.
    conn = get_connection(tmp_path)
    conn.execute(
        "INSERT INTO securities "
        "(id, market, code, name, security_type, status, listed_date) "
        "VALUES ('s00400a', 'TWSE', '00400A', '新上市ETF', 'etf', 'active', ?)",
        (today_taipei(),),
    )
    conn.commit()
    conn.close()

    response = client.get("/securities/s00400a")

    assert response.status_code == 200
    assert "新上市，尚無配息紀錄" in response.text


def test_industry_filter_narrows_results(client):
    client.post("/securities/sync", follow_redirects=False)

    response = client.get(
        "/securities", params={"security_type": "stock", "industry": "24"}
    )

    assert response.status_code == 200
    assert "台積電" in response.text


def test_industry_filter_excludes_non_matching_securities(client):
    client.post("/securities/sync", follow_redirects=False)

    response = client.get(
        "/securities", params={"security_type": "stock", "industry": "no-such-industry"}
    )

    assert response.status_code == 200
    assert "台積電" not in response.text
    assert "查無符合條件的商品" in response.text


def test_search_results_link_to_the_detail_page(client):
    client.post("/securities/sync", follow_redirects=False)

    response = client.get("/securities", params={"security_type": "stock", "q": "2330"})

    assert response.status_code == 200
    assert '<a href="/securities/' in response.text


def test_blank_query_lists_the_whole_catalog(client):
    # Design 004 §4.7.1: 002 §4.3.2 deliberately showed nothing on a
    # blank query to stay a "single lookup" tool rather than a market
    # browser; this is reversed so `view=all` with no filters lists
    # everything (paginated) instead.
    client.post("/securities/sync", follow_redirects=False)

    response = client.get(
        "/securities", params={"security_type": "stock", "view": "all"}
    )

    assert response.status_code == 200
    assert "台積電" in response.text
    assert "查無符合條件的商品" not in response.text


def test_second_page_shows_further_results(client, tmp_path):
    conn = get_connection(tmp_path)
    for n in range(60):
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES (?, 'TWSE', ?, ?, 'stock', 'active')",
            (f"s{n}", f"9{n:03d}", f"測試股{n}"),
        )
    conn.commit()
    conn.close()

    page_1 = client.get("/securities", params={"security_type": "stock", "view": "all"})
    page_2 = client.get(
        "/securities", params={"security_type": "stock", "view": "all", "page": 2}
    )

    assert "測試股0" in page_1.text
    assert "測試股0" not in page_2.text
    assert "測試股59" in page_2.text


def _insert_stock_and_etf(conn):
    conn.execute(
        "INSERT INTO securities "
        "(id, market, code, name, security_type, industry, status) "
        "VALUES ('s1', 'TWSE', '2330', '台積電', 'stock', '半導體業', 'active')"
    )
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('e1', 'TWSE', '0050', '元大台灣50', 'etf', 'active')"
    )
    conn.execute(
        "INSERT INTO etf_profiles (security_id, benchmark) VALUES ('e1', '臺灣50指數')"
    )
    conn.commit()
    conn.close()


def test_stock_mode_excludes_etfs(client, tmp_path):
    # Design 004 §4.7.1: `security_type` is a mode, not an optional
    # "show both" filter — never mix the two types in one result table.
    _insert_stock_and_etf(get_connection(tmp_path))

    response = client.get(
        "/securities", params={"security_type": "stock", "view": "all"}
    )

    assert response.status_code == 200
    assert "台積電" in response.text
    assert "元大台灣50" not in response.text


def test_bare_entry_opens_all_etfs_even_with_a_watchlist(client, tmp_path):
    # Design 047: no parameters means ETF / 全部商品, not stock or 追蹤.
    _insert_stock_and_etf(get_connection(tmp_path))
    client.post("/watchlist/add", data={"security_id": "e1"})
    conn = get_connection(tmp_path)
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('e2', 'TWSE', '0056', '元大高股息', 'etf', 'active')"
    )
    conn.commit()
    conn.close()

    bare = client.get("/securities").text
    watchlist = client.get("/securities", params={"view": "watchlist"}).text
    stock = client.get("/securities", params={"security_type": "stock"}).text

    assert "元大台灣50" in bare and "元大高股息" in bare
    assert "台積電" not in bare
    assert "元大台灣50" in watchlist and "元大高股息" not in watchlist
    assert "台積電" in stock


def test_detail_back_link_keeps_the_security_type(client, tmp_path):
    _insert_stock_and_etf(get_connection(tmp_path))

    stock = client.get("/securities/s1").text
    etf = client.get("/securities/e1").text

    assert 'href="/securities?security_type=stock">返回商品查詢' in stock
    assert 'href="/securities?security_type=etf">返回商品查詢' in etf


def test_etf_mode_excludes_stocks_and_leaves_benchmark_to_detail(client, tmp_path):
    # Design 042 §3: the list gives 追蹤指數's width to 下次配息; the
    # detail page still shows the benchmark.
    _insert_stock_and_etf(get_connection(tmp_path))

    response = client.get("/securities", params={"view": "all", "security_type": "etf"})
    detail = client.get("/securities/e1")

    assert response.status_code == 200
    assert "元大台灣50" in response.text
    assert "台積電" not in response.text
    assert "臺灣50指數" not in response.text
    assert "追蹤指數" not in response.text
    assert "臺灣50指數" in detail.text


def _table_headers(html):
    table = re.search(r'<table class="securities-table.*?</table>', html, re.S)[0]
    return re.findall(r"<th scope=\"col\"[^>]*>(?:\s*<a[^>]*>)?([^<]+)<", table)


@pytest.mark.parametrize(
    ("security_type", "pay_month", "expected"),
    [
        (
            "etf",
            "",
            [
                "代碼", "名稱", "市場", "規模（億元）", "受益人數", "保管銀行",
                "配息頻率", "下次配息", "最新收盤", "漲跌幅", "近一年殖利率",
                "追蹤",
            ],
        ),
        (
            "etf",
            "2026-10",
            [
                "代碼", "名稱", "市場", "規模（億元）", "受益人數", "保管銀行",
                "配息頻率", "下次配息", "2026-10 發放事件", "最新收盤", "漲跌幅",
                "近一年殖利率", "追蹤",
            ],
        ),
        (
            "stock",
            "",
            [
                "代碼", "名稱", "市場", "產業", "最新收盤", "漲跌幅",
                "近一年殖利率", "追蹤",
            ],
        ),
    ],
)  # fmt: skip
def test_all_and_watchlist_views_share_the_same_columns(
    client, tmp_path, security_type, pay_month, expected
):
    conn = get_connection(tmp_path)
    for sid, code, kind in (
        ("s1", "2330", "stock"),
        ("s2", "2317", "stock"),
        ("e1", "0050", "etf"),
        ("e2", "0056", "etf"),
    ):
        conn.execute(
            "INSERT INTO securities (id,market,code,name,security_type,status) "
            "VALUES (?,'TWSE',?,?,?,'active')",
            (sid, code, f"名稱{code}", kind),
        )
    conn.execute(
        "INSERT INTO distributions (id,security_id,ex_dividend_date,pay_date,"
        "amount_per_share,amount_status) VALUES "
        "('d1','e1','2026-10-16','2026-10-30','1','final'),"
        "('d2','e2','2026-10-16','2026-10-30','1','final')"
    )
    conn.executemany(
        "INSERT INTO watchlist (security_id) VALUES (?)", [("s1",), ("e1",)]
    )
    conn.commit()
    conn.close()
    pages = {
        view: client.get(
            "/securities",
            params={
                "security_type": security_type,
                "view": view,
                "pay_month": pay_month,
            },
        ).text
        for view in ("all", "watchlist")
    }
    assert _table_headers(pages["all"]) == expected
    assert _table_headers(pages["watchlist"]) == expected
    tracked, untracked = (
        ("名稱0050", "名稱0056") if security_type == "etf" else ("名稱2330", "名稱2317")
    )
    assert tracked in pages["all"] and untracked in pages["all"]
    assert tracked in pages["watchlist"] and untracked not in pages["watchlist"]
    for page in pages.values():
        assert "目標價" not in page and "過戶機構" not in page


@pytest.mark.parametrize(
    "sort", ["benchmark", "target_price", "note", "transfer_agent_name"]
)
@pytest.mark.parametrize("view", ["all", "watchlist"])
def test_removed_sort_keys_fall_back_to_the_default(client, tmp_path, sort, view):
    _insert_stock_and_etf(get_connection(tmp_path))
    etf = client.get(
        "/securities",
        params={"security_type": "etf", "view": view, "sort": sort},
    )
    stock = client.get(
        "/securities",
        params={"security_type": "stock", "view": view, "sort": sort},
    )
    assert etf.status_code == stock.status_code == 200
    # Design 048 §4.2.3: ETFs by size, largest first; stocks by code.
    assert re.search(r'class="col-number col-size" aria-sort="descending"', etf.text)
    assert re.search(r'class="col-code" aria-sort="ascending"', stock.text)


def _next_event_row(conn, sid, ex, pay, amount, status="final"):
    conn.execute(
        "INSERT INTO distributions (id,security_id,ex_dividend_date,pay_date,"
        "amount_per_share,amount_status) VALUES (?,?,?,?,?,?)",
        (f"{sid}-{ex}-{pay}", sid, ex, pay, amount, status),
    )


def test_next_distribution_picks_the_nearest_unfinished_event(tmp_path, monkeypatch):
    monkeypatch.setattr(query, "today_taipei", lambda: "2026-10-20")
    conn = get_connection(tmp_path)
    migrate.apply_pending(conn)
    for sid, code in (
        ("future", "00901"),
        ("unpaid", "00902"),
        ("paid", "00903"),
        ("none", "00904"),
        ("open", "00905"),
    ):
        conn.execute(
            "INSERT INTO securities (id,market,code,name,security_type,status) "
            "VALUES (?,'TWSE',?,'測試','etf','active')",
            (sid, code),
        )
    _next_event_row(conn, "future", "2026-12-16", "2027-01-10", "0.5")
    _next_event_row(conn, "future", "2026-11-17", "2026-12-12", "0.4", "estimated")
    _next_event_row(conn, "unpaid", "2026-10-16", "2026-11-10", "1.2")
    _next_event_row(conn, "paid", "2026-09-16", "2026-10-10", "0.3")
    _next_event_row(conn, "open", "2026-11-01", None, None, "unknown")

    def ids(**kwargs):
        return [r["id"] for r in query.browse(conn, mode="etf", **kwargs)["results"]]

    result = {
        r["id"]: r["next_distribution"]
        for r in query.browse(conn, mode="etf")["results"]
    }
    ordered = ids(sort="next_distribution", direction="asc")
    desc = ids(sort="next_distribution", direction="desc")
    conn.close()
    assert result["future"]["ex_dividend_date"] == "2026-11-17"
    assert result["unpaid"]["ex_dividend_date"] == "2026-10-16"
    assert result["open"]["pay_date"] is None
    assert result["paid"] is None and result["none"] is None
    # No announced event sorts last in both directions, in code order.
    assert ordered == ["unpaid", "open", "future", "paid", "none"]
    assert desc == ["future", "open", "unpaid", "paid", "none"]


def test_next_distribution_cell_shows_dates_amounts_and_gaps(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr(query, "today_taipei", lambda: "2026-10-20")
    monkeypatch.setattr(
        "taiwan_stock.web.pages_securities.today_taipei", lambda: "2026-10-20"
    )
    conn = get_connection(tmp_path)
    for sid, code in (("a", "00901"), ("b", "00902"), ("c", "00903"), ("d", "00904")):
        conn.execute(
            "INSERT INTO securities (id,market,code,name,security_type,status) "
            "VALUES (?,'TWSE',?,'測試','etf','active')",
            (sid, code),
        )
    _next_event_row(conn, "a", "2026-11-17", "2026-12-12", "0.4", "estimated")
    _next_event_row(conn, "b", "2026-10-16", "2026-11-10", "1.2")
    _next_event_row(conn, "c", "2026-11-01", None, None, "unknown")
    conn.commit()
    conn.close()
    response = client.get(
        "/securities",
        params={"security_type": "etf", "view": "all", "sort": "code"},
    )
    cells = [
        " ".join(unescape(re.sub(r"<[^>]+>", " ", c)).split())
        for c in re.findall(
            r'<td class="col-next-event">(.*?)</td>', response.text, re.S
        )
    ]
    assert cells == [
        "除息 2026/11/17 發放 2026/12/12 · 0.4 元（預估）",
        "已除息 發放 2026/11/10 · 1.2 元",
        "除息 2026/11/01 發放日待確認 · 金額待確認",
        "—",
    ]
    mobile = re.search(r'<ul class="mobile-records".*?</ul>', response.text, re.S)[0]
    assert mobile.count("下次配息") == 4
    assert "下次配息只列出已公告" in response.text


def test_pay_month_filter_shows_the_matching_announced_event(client, tmp_path):
    conn = get_connection(tmp_path)
    _insert_stock_and_etf(conn)
    conn2 = get_connection(tmp_path)
    conn2.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, pay_date, amount_per_share, "
        "amount_status) "
        "VALUES ('d1', 'e1', '2026-08-18', '2026-09-11', '1.01', 'final')"
    )
    conn2.commit()
    conn2.close()

    response = client.get(
        "/securities",
        params={"view": "all", "security_type": "etf", "pay_month": "2026-09"},
    )

    assert response.status_code == 200
    assert "元大台灣50" in response.text
    assert "2026-09-11" in response.text
    assert "1.01" in response.text


def test_pay_month_filter_excludes_securities_without_that_months_event(
    client, tmp_path
):
    conn = get_connection(tmp_path)
    _insert_stock_and_etf(conn)
    conn2 = get_connection(tmp_path)
    conn2.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, pay_date, amount_per_share, "
        "amount_status) "
        "VALUES ('d1', 'e1', '2026-08-18', '2026-09-11', '1.01', 'final')"
    )
    conn2.commit()
    conn2.close()

    response = client.get(
        "/securities",
        params={"view": "all", "security_type": "etf", "pay_month": "2026-10"},
    )

    assert response.status_code == 200
    assert "查無符合條件的商品" in response.text


def test_malformed_pay_month_is_ignored_server_side(client, tmp_path):
    _insert_stock_and_etf(get_connection(tmp_path))

    response = client.get(
        "/securities",
        params={"view": "all", "security_type": "etf", "pay_month": "not-a-month"},
    )

    assert response.status_code == 200
    assert "元大台灣50" in response.text


def test_industry_filter_is_ignored_server_side_in_etf_mode(client, tmp_path):
    # Design 004 §4.7.1: "後端依模式驗證條件，不能只在畫面隱藏不適用欄位"
    # — even if an industry value is submitted while in ETF mode
    # (e.g. a stale form or a hand-built URL), it must not filter out
    # ETFs, since industry is a stock-only field that's always NULL on
    # etf_profiles' underlying securities row.
    _insert_stock_and_etf(get_connection(tmp_path))

    response = client.get(
        "/securities",
        params={"view": "all", "security_type": "etf", "industry": "半導體業"},
    )

    assert response.status_code == 200
    assert "元大台灣50" in response.text


def test_watchlist_tab_count_is_scoped_to_current_mode(client, tmp_path):
    conn = get_connection(tmp_path)
    _insert_stock_and_etf(conn)
    client.post(
        "/watchlist/add",
        data={"security_id": "s1", "return_to": "/securities?view=all"},
    )
    client.post(
        "/watchlist/add",
        data={
            "security_id": "e1",
            "return_to": "/securities?view=all&security_type=etf",
        },
    )

    stock_page = client.get(
        "/securities", params={"security_type": "stock", "view": "all"}
    )
    etf_page = client.get("/securities", params={"view": "all", "security_type": "etf"})

    assert "我的追蹤清單（1）" in stock_page.text
    assert "我的追蹤清單（1）" in etf_page.text


def test_custodian_bank_and_frequency_filters_apply_to_etf_results(client, tmp_path):
    # Design 004 §4.3.1/§4.8 Phase E: once the per-投信 adapters
    # populate `custodian_bank`/`distribution_frequency`, these filters
    # are real again — an unrecognized value is ignored server-side
    # (design 004 §4.7.1) rather than erroring or silently filtering by
    # a raw, unvalidated string.
    conn = get_connection(tmp_path)
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('e1', 'TWSE', '0056', '元大高股息', 'etf', 'active')"
    )
    conn.execute(
        "INSERT INTO etf_profiles "
        "(security_id, custodian_bank, distribution_frequency) "
        "VALUES ('e1', '玉山銀行', 'quarterly')"
    )
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('e2', 'TWSE', '00878', '國泰永續高股息', 'etf', 'active')"
    )
    conn.execute(
        "INSERT INTO etf_profiles "
        "(security_id, custodian_bank, distribution_frequency) "
        "VALUES ('e2', '中國信託商業銀行', 'monthly')"
    )
    conn.commit()
    conn.close()

    response = client.get(
        "/securities",
        params={
            "security_type": "etf",
            "custodian_bank": "玉山銀行",
            "distribution_frequency": "quarterly",
        },
    )

    assert response.status_code == 200
    assert "元大高股息" in response.text
    assert "國泰永續高股息" not in response.text
    assert "保管銀行（不限）" in response.text
    assert "配息頻率（不限）" in response.text


def test_unrecognized_custodian_bank_filter_reports_invalid_condition(client, tmp_path):
    conn = get_connection(tmp_path)
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('e1', 'TWSE', '0056', '元大高股息', 'etf', 'active')"
    )
    conn.commit()
    conn.close()

    response = client.get(
        "/securities", params={"security_type": "etf", "custodian_bank": "不存在的銀行"}
    )

    assert response.status_code == 200
    assert "元大高股息" not in response.text
    assert "銀行條件已失效" in response.text


def test_distribution_frequency_filter_matches_inferred_tier_too(client, tmp_path):
    # Design 004 §4.4.1/§4.5.1: the frequency filter must reach ETFs
    # whose only frequency signal is the local two-event inference
    # (Phase F), not just the 投信-confirmed official column — otherwise
    # "find me quarterly ETFs" silently misses most of the catalog.
    from taiwan_stock.catalog import frequency_inference, sync

    conn = get_connection(tmp_path)
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('e1', 'TWSE', '0050', '元大台灣50', 'etf', 'active')"
    )
    conn.execute("INSERT INTO etf_profiles (security_id) VALUES ('e1')")
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('e2', 'TWSE', '0056', '元大高股息', 'etf', 'active')"
    )
    conn.execute("INSERT INTO etf_profiles (security_id) VALUES ('e2')")
    conn.commit()
    sync.upsert_distribution(
        conn,
        "e1",
        {
            "ex_dividend_date": "2026-04-15",
            "amount_per_share": "1.0",
            "amount_status": "final",
        },
    )
    sync.upsert_distribution(
        conn,
        "e1",
        {
            "ex_dividend_date": "2026-07-15",
            "amount_per_share": "1.0",
            "amount_status": "final",
        },
    )
    conn.commit()
    frequency_inference.recompute_all(conn)
    conn.close()

    response = client.get(
        "/securities",
        params={"security_type": "etf", "distribution_frequency": "quarterly"},
    )

    assert response.status_code == 200
    assert "元大台灣50" in response.text
    assert "元大高股息" not in response.text


def test_frequency_month_pattern_hint_shown_for_quarterly_not_monthly(client, tmp_path):
    # month_pattern disambiguates which quarterly group (1/4/7/10 vs
    # 2/5/8/11 vs 3/6/9/12) a "季配" badge refers to on the list page —
    # monthly is excluded since every month is trivially in its pattern.
    from taiwan_stock.catalog import frequency_inference, sync

    conn = get_connection(tmp_path)
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('q1', 'TWSE', '00920', '季配ETF', 'etf', 'active')"
    )
    conn.execute("INSERT INTO etf_profiles (security_id) VALUES ('q1')")
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('m1', 'TWSE', '00929', '月配ETF', 'etf', 'active')"
    )
    conn.execute("INSERT INTO etf_profiles (security_id) VALUES ('m1')")
    conn.commit()

    sync.upsert_distribution(
        conn,
        "q1",
        {
            "ex_dividend_date": "2026-01-20",
            "amount_per_share": "1.0",
            "amount_status": "final",
        },
    )
    sync.upsert_distribution(
        conn,
        "q1",
        {
            "ex_dividend_date": "2026-04-20",
            "amount_per_share": "1.0",
            "amount_status": "final",
        },
    )
    sync.upsert_distribution(
        conn,
        "m1",
        {
            "ex_dividend_date": "2026-03-10",
            "amount_per_share": "0.1",
            "amount_status": "final",
        },
    )
    sync.upsert_distribution(
        conn,
        "m1",
        {
            "ex_dividend_date": "2026-04-10",
            "amount_per_share": "0.1",
            "amount_status": "final",
        },
    )
    conn.commit()
    frequency_inference.recompute_all(conn)
    conn.close()

    response = client.get("/securities", params={"security_type": "etf"})

    assert response.status_code == 200
    assert "1、4、7、10 月" in response.text
    assert re.search(
        r">月配</span>\s*"
        r'<span class="profile-source tier-inferred"[^>]*>歷史</span>',
        response.text,
    )
    assert "1、2、3、4、5、6、7、8、9、10、11、12 月" not in response.text


def test_official_frequency_labels_historical_ex_dividend_months_separately(
    client, tmp_path
):
    conn = get_connection(tmp_path)
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('q1', 'TWSE', '00927', '群益半導體收益', 'etf', 'active')"
    )
    conn.execute(
        "INSERT INTO etf_profiles "
        "(security_id, distribution_frequency, distribution_month_pattern, "
        "distribution_frequency_inferred) VALUES "
        "('q1', 'quarterly', '[3,6,9,12]', "
        '\'{"value":"quarterly","month_pattern":[1,4,7,10]}\')'
    )
    conn.commit()
    conn.close()

    response = client.get("/securities", params={"security_type": "etf"})

    assert response.status_code == 200
    # Design 048 §4.2.5: the list drops 官方 and the 歷史 prefix; the month
    # source stays in the tooltip.
    assert re.search(
        r">季配</span>\s*</span>"
        r'.*?class="profile-months" title="依近期除息紀錄推定[^"]*"[^>]*>'
        r"1、4、7、10 月</span>",
        response.text,
        re.S,
    )
    assert "歷史：1、4、7、10 月" not in response.text
    # The valuation months never show as ex-dividend months (the filter
    # option 季配・3、6、9、12 月 is a different text).
    assert "：3、6、9、12 月" not in response.text


def test_official_twice_yearly_nonuniform_months_show_on_list_and_detail(
    client, tmp_path, monkeypatch
):
    from taiwan_stock.catalog import sync, sync_history

    monkeypatch.setattr("taiwan_stock.catalog.query.today_taipei", lambda: "2026-10-26")
    for page in ("pages_securities", "pages_security_detail"):
        monkeypatch.setattr(
            f"taiwan_stock.web.{page}.today_taipei", lambda: "2026-10-26"
        )
    conn = get_connection(tmp_path)
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('semi', 'TWSE', '00922', '國泰台灣領袖50', 'etf', 'active')"
    )
    conn.execute(
        "INSERT INTO etf_profiles (security_id,distribution_frequency,"
        "distribution_frequency_source) VALUES ('semi','semiannual',?)",
        (json.dumps({"raw": "一年兩次配息", "origin": "sitca"}),),
    )
    for day in (
        "2024-03-18",
        "2024-10-17",
        "2025-03-18",
        "2025-10-20",
        "2026-03-17",
        "2026-10-19",
    ):
        sync.upsert_distribution(
            conn,
            "semi",
            {
                "ex_dividend_date": day,
                "amount_per_share": "1.0",
                "amount_status": "final",
            },
        )
    with sync_history.run(conn, "twse_etf_distribution_list") as task:
        for year in (2024, 2025):
            key = f"00922:{year}"
            task.plan(key, security_id="semi", period=str(year))
            task.record(key, "success")
    conn.close()
    for path in ("/securities?security_type=etf", "/securities/semi"):
        response = client.get(path)
        assert response.status_code == 200
        # Issue #143: one label for every semiannual ETF, as in the filter.
        assert '<span class="profile-value">半年配</span>' in response.text
        assert "每年 2 次" not in response.text
        assert "3、10 月" in response.text
        # Design 048 §4.2.5: only the detail page names the month source.
        if path == "/securities/semi":
            assert "歷史：3、10 月" in response.text
        else:
            assert "歷史：3、10 月" not in response.text


def test_yield_sort_orders_results_by_current_yield_descending(client, tmp_path):
    conn = get_connection(tmp_path)
    for id_, code, name, close in (
        ("e1", "0056", "低殖利率ETF", "100"),
        ("e2", "0057", "高殖利率ETF", "100"),
    ):
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES (?, 'TWSE', ?, ?, 'etf', 'active')",
            (id_, code, name),
        )
        conn.execute(
            "INSERT INTO daily_prices (security_id, trade_date, close) "
            "VALUES (?, '2026-09-01', ?)",
            (id_, close),
        )
    conn.execute(
        "INSERT INTO distributions (id, security_id, ex_dividend_date, "
        "amount_per_share, amount_status) "
        "VALUES ('d1', 'e1', '2026-06-01', '1', 'final')"
    )
    conn.execute(
        "INSERT INTO distributions (id, security_id, ex_dividend_date, "
        "amount_per_share, amount_status) "
        "VALUES ('d2', 'e2', '2026-06-01', '5', 'final')"
    )
    conn.commit()
    conn.close()

    response = client.get(
        "/securities",
        params={"security_type": "etf", "q": "ETF", "sort": "yield_desc"},
    )

    assert response.status_code == 200
    high_pos = response.text.index("高殖利率ETF")
    low_pos = response.text.index("低殖利率ETF")
    assert high_pos < low_pos


def test_json_api_catalog_sync(client):
    response = client.post("/api/v1/catalog-sync")

    assert response.status_code == 200
    body = response.json()
    listing_status = body.pop("listingStatus")
    assert body == {
        "stock_count": 1,
        "etf_count": 0,
        "tpex_stock_count": 1,
        "tpex_etf_count": 0,
    }
    assert listing_status["errors"] == {}


def test_json_api_catalog_sync_failure_returns_502(tmp_path):
    app = create_app(tmp_path)
    app.dependency_overrides[get_http_client] = _failing_http_client_override
    with TestClient(app) as test_client:
        response = test_client.post("/api/v1/catalog-sync")

    assert response.status_code == 502


def test_sync_failure_shows_error_without_crashing(tmp_path):
    app = create_app(tmp_path)
    app.dependency_overrides[get_http_client] = _failing_http_client_override
    with TestClient(app) as test_client:
        response = test_client.post("/securities/sync", follow_redirects=False)
        assert response.status_code == 303
        assert load_job(test_client, response)["status"] == "failed"

        page = test_client.get(response.headers["location"])
        assert "同步失敗" in page.text


def test_every_sortable_header_shows_a_sort_hint(client, tmp_path):
    _insert_stock_and_etf(get_connection(tmp_path))
    for security_type, sortable in (("etf", 11), ("stock", 7)):
        html = client.get(
            "/securities", params={"security_type": security_type, "view": "all"}
        ).text
        head = html.split("<thead>", 1)[1].split("</thead>", 1)[0]
        # Design 048 §4.2.4: every sortable header carries an indicator;
        # exactly one says which way the list is sorted.
        assert head.count('class="sort-indicator"') == sortable
        assert head.count("aria-sort=") == 1


def test_etf_tab_and_header_links_follow_the_new_defaults(client, tmp_path):
    _insert_stock_and_etf(get_connection(tmp_path))
    html = unescape(
        client.get(
            "/securities",
            params={"security_type": "stock", "view": "all", "include_delisted": "1"},
        ).text
    )
    etf_tab = re.search(r'<a href="([^"]+)"[^>]*>ETF</a>', html)[1]
    # Design 048 §4.2.3: the ETF tab leaves the sort to its own default and
    # starts again without delisted rows.
    assert "sort=" not in etf_tab and "include_delisted" not in etf_tab
    etf = unescape(client.get(etf_tab).text)
    links = {
        key: parse_qs(urlsplit(href).query)
        for href, key in re.findall(
            r'<a class="sort-heading" href="([^"]+)">([^<]+)<', etf
        )
    }
    assert links["規模（億元）"]["direction"] == ["asc"]  # already desc
    assert links["受益人數"]["direction"] == ["desc"]
    assert links["代碼"]["direction"] == ["asc"]
    shown = unescape(
        client.get(
            "/securities",
            params={"security_type": "etf", "view": "all", "include_delisted": "1"},
        ).text
    )
    sort_link = re.search(r'<a class="sort-heading" href="([^"]+)">代碼<', shown)[1]
    assert parse_qs(urlsplit(sort_link).query)["include_delisted"] == ["1"]
    assert '<input type="hidden" name="include_delisted" value="1">' in shown


def test_official_annual_months_and_this_years_mismatch_show_in_the_list(
    client, tmp_path, monkeypatch
):
    monkeypatch.setattr("taiwan_stock.catalog.query.today_taipei", lambda: "2026-10-31")
    conn = get_connection(tmp_path)
    for sid, code, dates in (
        ("a1", "00851", ("2025-10-17", "2026-10-16")),
        ("a2", "00971", ("2025-12-16",)),
        ("a3", "00770", ("2025-01-17", "2026-01-20", "2026-09-16")),
    ):
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES (?, 'TWSE', ?, ?, 'etf', 'active')",
            (sid, code, f"年配{code}"),
        )
        conn.execute(
            "INSERT INTO etf_profiles (security_id, distribution_frequency) "
            "VALUES (?, 'annual')",
            (sid,),
        )
        for day in dates:
            conn.execute(
                "INSERT INTO distributions (id, security_id, ex_dividend_date, "
                "amount_per_share, amount_status) VALUES (?, ?, ?, '1', 'final')",
                (f"{sid}-{day}", sid, day),
            )
    conn.commit()
    conn.close()
    html = client.get("/securities", params={"security_type": "etf"}).text

    def row(code):
        return re.search(
            rf'<tr>\s*<th scope="row" class="col-code"><a[^>]*>{code}</a>.*?</tr>',
            html,
            re.S,
        )[0]

    # Design 048 §4.2.7 / §4.2.6.
    assert ">10 月</span>" in row("00851")
    assert ">暫定：12 月</span>" in row("00971")
    assert " 月</span>" not in row("00770")
    assert "官方與紀錄不符（2026 年至今配 2 次）" in row("00770")
