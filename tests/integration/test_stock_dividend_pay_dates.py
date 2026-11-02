"""Tests for filling stock dividend pay dates from MOPS (design 015)."""

from decimal import Decimal
from urllib.parse import parse_qs

import httpx2
import pytest
from fastapi.testclient import TestClient
from sync_helpers import load_job

from taiwan_stock.catalog import _parsing, mops_adapter, sync, sync_stock_dividends
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.web.deps import get_http_client

TODAY = "2026-09-29"

# Two-level header exactly as served live (2026-09-29), trimmed of markup
# the parser ignores.
_HEADER = (
    "<tr class='tblHead'><th rowspan='2'>公司代號</th><th rowspan='2'>公司名稱</th>"
    "<th rowspan='2'>股利所屬期間</th><th rowspan='2'>權利分派基準日</th>"
    "<th colspan='3'>股票股利</th><th colspan='5'>現金股利</th>"
    "<th rowspan='2'>現金增資總股數(股)</th><th rowspan='2'>現金增資認股比率(%)</th>"
    "<th rowspan='2'>現金增資認購價(元/股)</th><th rowspan='2'>參加分派總股數</th>"
    "<th rowspan='2'>公告日期</th><th rowspan='2'>公告時間</th>"
    "<th rowspan='2'>普通股每股面額</th></tr><tr class='tblHead'>"
    "<th>盈餘轉增資配股(元/股)</th><th>法定盈餘公積、資本公積轉增資配股(元/股)</th>"
    "<th>除權交易日</th><th>盈餘分配之股東現金股利(元/股)</th>"
    "<th>法定盈餘公積、資本公積發放之現金(元/股)</th><th>特別股配發現金股利(元/股)</th>"
    "<th>除息交易日</th><th>現金股利發放日</th></tr>"
)


def _row(
    code,
    ex,
    pay,
    earnings="",
    reserves="",
    preferred="",
    record="",
    announced="115/06/08",
    at="17:39:41",
):
    cells = [code, "名稱", "114年", record, "&nbsp;", "&nbsp;", "&nbsp;"]
    cells += [earnings, reserves, preferred, ex, pay, "&nbsp;", "&nbsp;", "&nbsp;"]
    cells += ["        825,709,978", announced, at, "新台幣10.0000元"]
    return "<tr class='even'>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def _page(rows_with_closure, rows_without=()):
    """Both tables the live report serves (book-closure / no closure)."""
    return (
        "<span class='reportName'>公司股利分派公告資料彙總表</span>"
        "<適用停止過戶期間規定之公司><table class='hasBorder'>"
        + _HEADER
        + "".join(rows_with_closure)
        + "</table><不適用停止過戶期間規定之公司><table class='hasBorder'>"
        + (_HEADER if rows_without else "")
        + "".join(rows_without)
        + "</table>"
    )


_NO_DATA = (
    "<span class='reportName'>公司股利分派公告資料彙總表</span>"
    "<h4>查無符合條件之資料</h4>"
)


class FakeMops:
    def __init__(self, pages):
        self.pages = pages  # (TYPEK, roc year) -> HTML or int status
        self.requests: list[tuple[str, str]] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        form = parse_qs(request.content.decode())
        key = (form["TYPEK"][0], form["year"][0])
        self.requests.append(key)
        assert form["month"] == ["all"]
        page = self.pages.get(key, _NO_DATA)
        if isinstance(page, int):
            return httpx2.Response(page)
        return httpx2.Response(200, text=page)


def test_parser_reads_both_tables_and_sums_ordinary_cash():
    page = _page(
        [_row("2103", "115/06/24", "115/07/13", earnings="0.27", record="115/06/30")],
        [
            _row(
                "3481",
                "115/06/11",
                "115/07/10",
                earnings="0.5",
                reserves="0.5",
                preferred="9",
            )
        ],  # fmt: skip
    )

    items = {i["code"]: i for i in mops_adapter.parse_ex_rights_announcements(page)}

    assert items["2103"] == {
        "code": "2103",
        "ex_dividend_date": "2026-06-24",
        "pay_date": "2026-07-13",
        "record_date": "2026-06-30",
        "cash": Decimal("0.2700"),
        "announced_at": "2026-06-08 17:39:41",
    }
    # Earnings + reserves; preferred-share cash is not an ordinary holder's.
    assert str(items["3481"]["cash"]) == "1.0000"


def test_parser_rounds_like_stored_amounts_and_keeps_latest_announcement():
    page = _page(
        [
            _row("2363", "115/08/19", "115/09/10", earnings="0.6",
                 announced="115/07/01", at="10:00:00"),
            _row("2363", "115/08/19", "115/09/18", earnings="0.60588248",
                 announced="115/08/04", at="16:50:33"),
            _row("9999", "", "", earnings=""),  # stock dividend only
        ]
    )  # fmt: skip

    [item] = mops_adapter.parse_ex_rights_announcements(page)

    assert item["pay_date"] == "2026-09-18"  # the correction wins
    assert str(item["cash"]) == "0.6059"


@pytest.mark.parametrize(
    ("page", "message"),
    [
        ("<body>因為安全性考量，您所執行的頁面無法呈現</body>", "拒絕查詢"),
        ("<body>查詢過於頻繁，請稍後再查詢</body>", "拒絕查詢"),
        ("<html><body>維護中</body></html>", "沒有資料列"),
        (
            "<table><tr><th>公司代號</th></tr>"
            + _row("2103", "115/06/24", "115/07/13")
            + "</table>",
            "表頭缺少",
        ),
        (
            _page(
                [_row("2103", "115/06/24", "115/07/13").replace("<td>名稱</td>", "")]
            ),
            "19 格",
        ),
    ],
)
def test_parser_rejects_blocked_or_changed_pages(page, message):
    with pytest.raises(_parsing.QuoteFormatError, match=message):
        mops_adapter.parse_ex_rights_announcements(page)


def test_parser_treats_explicit_no_data_as_empty():
    assert mops_adapter.parse_ex_rights_announcements(_NO_DATA) == []


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    connection.executemany(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES (?, ?, ?, ?, ?, 'active')",
        [
            ("s-2103", "TWSE", "2103", "台橡", "stock"),
            ("s-3481", "TWSE", "3481", "群創", "stock"),
            ("s-0056", "TWSE", "0056", "元大高股息", "etf"),
            ("s-1240", "TPEx", "1240", "茂生農經", "stock"),
        ],
    )
    connection.executemany(
        "INSERT INTO distributions (id, security_id, ex_dividend_date, "
        "amount_per_share, amount_status, pay_date, record_date) "
        "VALUES (?, ?, ?, ?, 'final', ?, ?)",
        [
            ("d-2103-22", "s-2103", "2022-07-19", "2.4000", None, None),
            ("d-2103-26", "s-2103", "2026-06-24", "0.2700", None, "2026-06-20"),
            ("d-3481-26", "s-3481", "2026-06-11", "0.9000", None, None),
            ("d-3481-25", "s-3481", "2025-05-22", "1.0000", "2025-06-20", None),
            ("d-0056", "s-0056", "2026-07-17", "1.0000", None, None),
            ("d-1240", "s-1240", "2026-07-15", "3.0000", None, None),
        ],
    )
    connection.commit()
    yield connection
    connection.close()


def _pages():
    return {
        ("sii", "111"): _page([_row("2103", "111/07/19", "111/08/05", earnings="2.4",
                                    record="111/07/25")]),
        ("sii", "115"): _page(
            [
                _row("2103", "115/06/24", "115/07/13", earnings="0.27",
                     record="115/06/30"),
                _row("3481", "115/06/11", "115/07/10", earnings="0.5",
                     reserves="0.5"),
                _row("0056", "115/07/17", "115/08/12", earnings="1"),
            ]
        ),
        ("otc", "115"): _page([_row("1240", "115/07/15", "115/08/12", earnings="3")]),
    }  # fmt: skip


def _run(conn, mops):
    sleeps = []
    with httpx2.Client(transport=httpx2.MockTransport(mops.handler)) as client:
        outcome = sync_stock_dividends.sync_stock_dividend_pay_dates(
            conn, client, today=TODAY, sleep=sleeps.append
        )
    return outcome, sleeps


def _row_of(conn, dist_id):
    return conn.execute(
        "SELECT pay_date, record_date, amount_per_share FROM distributions "
        "WHERE id = ?",
        (dist_id,),
    ).fetchone()


def test_fills_pay_dates_without_touching_amounts(conn):
    mops = FakeMops(_pages())

    outcome, sleeps = _run(conn, mops)

    assert tuple(_row_of(conn, "d-2103-22")) == ("2022-08-05", "2022-07-25", "2.4000")
    # An existing record_date is kept.
    assert tuple(_row_of(conn, "d-2103-26")) == ("2026-07-13", "2026-06-20", "0.2700")
    # MOPS says 1.00 but the stored 0.90 is reported, not overwritten.
    assert tuple(_row_of(conn, "d-3481-26")) == ("2026-07-10", None, "0.9000")
    assert outcome["twse"]["amount_mismatches"] == [
        {
            "code": "3481",
            "ex_dividend_date": "2026-06-11",
            "stored": "0.9000",
            "mops": "1.0000",
        }
    ]
    # ETFs keep their own pay-date source.
    assert _row_of(conn, "d-0056")["pay_date"] is None
    assert tuple(_row_of(conn, "d-1240")) == ("2026-08-12", None, "3.0000")
    assert outcome["errors"] == {}
    assert outcome["twse"]["updated"] == 3
    assert outcome["twse"]["unmatched"] == 0


def test_queries_only_years_still_missing_plus_the_year_before(conn):
    mops = FakeMops(_pages())

    outcome, sleeps = _run(conn, mops)

    # 2022 → 110, 111; 2026 → 114, 115. 3481's 2025 row already has one.
    assert outcome["twse"]["years"] == [110, 111, 114, 115]
    assert [y for t, y in mops.requests if t == "sii"] == ["110", "111", "114", "115"]
    assert outcome["tpex"]["years"] == [114, 115]
    assert sleeps == [3.0] * (3 + 1)


def test_nothing_missing_means_no_requests(conn):
    mops = FakeMops(_pages())
    _run(conn, mops)
    mops.requests.clear()

    outcome, sleeps = _run(conn, mops)

    assert mops.requests == []
    assert sleeps == []
    assert outcome["twse"]["years"] == []


def test_rows_mops_does_not_know_stay_pending(conn):
    pages = _pages()
    del pages[("sii", "111")]
    mops = FakeMops(pages)

    outcome, _ = _run(conn, mops)

    assert _row_of(conn, "d-2103-22")["pay_date"] is None
    assert outcome["twse"]["unmatched"] == 1


def test_one_market_failing_leaves_the_other_and_writes_nothing(conn):
    pages = _pages()
    pages[("sii", "115")] = "<body>查詢過於頻繁，請稍後再查詢</body>"
    mops = FakeMops(pages)

    outcome, _ = _run(conn, mops)

    assert "拒絕查詢" in outcome["errors"]["twse"]
    # Nothing from the TWSE run is written, not even the years that parsed.
    assert _row_of(conn, "d-2103-22")["pay_date"] is None
    assert _row_of(conn, "d-1240")["pay_date"] == "2026-08-12"


def test_http_failure_is_retried_then_reported(conn, monkeypatch):
    monkeypatch.setattr(sync, "_RETRY_BACKOFF_SECONDS", (0, 0))
    pages = _pages()
    pages[("otc", "114")] = 503
    mops = FakeMops(pages)

    outcome, _ = _run(conn, mops)

    assert outcome["errors"]["tpex"] == "HTTP 503"
    assert mops.requests.count(("otc", "114")) == 3


def test_sync_button_reports_pay_dates_and_mismatches(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_stock_dividends, "_PAY_DATE_DELAY_SECONDS", 0)
    mops = FakeMops(_pages())

    def handler(request):
        if request.url.path.endswith("/ajax_t108sb27"):
            return mops.handler(request)
        return httpx2.Response(200, json=[])  # notices: none

    def override():
        with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
            yield client

    app = create_app(tmp_path)
    app.dependency_overrides[get_http_client] = override
    with TestClient(app) as client:
        connection = get_connection(tmp_path)
        connection.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('s-3481', 'TWSE', '3481', '群創', 'stock', 'active')"
        )
        connection.execute(
            "INSERT INTO distributions (id, security_id, ex_dividend_date, "
            "amount_per_share, amount_status) "
            "VALUES ('d', 's-3481', '2026-06-11', '0.9000', 'final')"
        )
        connection.commit()
        connection.close()
        response = client.post("/corporate-actions/sync", follow_redirects=False)
        api = client.post("/api/v1/ex-dividend-sync").json()

    message = load_job(client, response)["summary"]
    assert "補上發放日 1 筆" in message
    assert "金額與 MOPS 不一致 1 筆，未修改" in message
    assert api["pay_dates"]["errors"] == {}
