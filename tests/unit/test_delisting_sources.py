import json
from pathlib import Path

import httpx2
import pytest

from taiwan_stock.catalog import delisting_sources as s

FIXTURES = Path(__file__).parents[1] / "fixtures" / "delistings"
TWSE_PARAMS = dict(
    company="A00005", date="20220425", seq="1", fund="0054", type="listing"
)
TPEX_PARAMS = dict(
    company="A00016", date="20250723", seq="1", fund="00794B", type="listing"
)


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def official_handler(request):
    path = request.url.path
    if path == "/v1/company/suspendListingCsvAndHtml":
        return httpx2.Response(200, text=fixture("twse_stocks.json"))
    if path == "/www/zh-tw/company/deListed":
        assert request.url.params["date"] == "ALL"
        return httpx2.Response(200, text=fixture("tpex_stocks.json"))
    if path == "/zh/ETFortune/announcementList":
        href = (
            "/zh/ETFortune/announcement?company=A00005&amp;date=20220425"
            "&amp;seq=1&amp;fund=0054&amp;type=listing"
        )
        return httpx2.Response(
            200,
            text=f'<a href="{href}">本基金終止上市日及信託契約終止事宜</a>',
        )
    if path == "/zh/ETFortune/announcement":
        return httpx2.Response(200, text=fixture("twse_0054.html"))
    if path == "/api/etfMaInfo":
        return httpx2.Response(
            200,
            json=[
                dict(
                    subject="本基金終止上櫃日及信託契約終止事宜",
                    params="company=A00016&date=20250723&seq=1&fund=00794B&type=listing",
                )
            ],
        )
    if path == "/api/etfMaInfoDetail":
        return httpx2.Response(200, text=fixture("tpex_00794b.json"))
    return httpx2.Response(404)


def test_real_official_notices_select_trading_date_not_contract_or_liquidation():
    with httpx2.Client(transport=httpx2.MockTransport(official_handler)) as client:
        twse = s.fetch_notice(client, "TWSE", TWSE_PARAMS)
        tpex = s.fetch_notice(client, "TPEx", TPEX_PARAMS)
    assert (twse["code"], twse["effective_date"], twse["last_trade_date"]) == (
        "0054",
        "2022-05-27",
        "2022-05-26",
    )
    assert (tpex["code"], tpex["effective_date"], tpex["last_trade_date"]) == (
        "00794B",
        "2025-08-22",
        "2025-08-21",
    )


NOTICE_CASES = json.loads(fixture("notice_cases.json"))


@pytest.mark.parametrize("case", NOTICE_CASES, ids=lambda c: c["params"]["fund"])
def test_six_previously_failed_official_notices(case):
    def handler(request):
        if case["market"] == "TWSE":
            assert dict(request.url.params) == case["params"]
            return httpx2.Response(
                200,
                text=(
                    f"<table><tr><th>主旨</th><td>{case['subject']}</td></tr>"
                    f"<tr><th>說明</th><td>{case['description']}</td></tr></table>"
                ),
            )
        return httpx2.Response(
            200,
            json=[{**case, "stockNo": case["params"]["fund"]}],
        )

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        if case["effective_date"]:
            value = s.fetch_notice(client, case["market"], case["params"])
            assert value["effective_date"] == case["effective_date"]
            assert value["code"] == case["params"]["fund"]
            assert value["market"] == case["market"]
        else:
            with pytest.raises(s.NoticeDatePending) as pending:
                s.fetch_notice(client, case["market"], case["params"])
            assert pending.value.url == case["url"]


@pytest.mark.parametrize(
    "subject,body,market",
    [
        ("0080公告", "申請終止上市。自104年12月9日終止上市。", "TWSE"),
        ("0081公告", "經臺灣證券交易所核准，自104年12月9日終止上市。", "TWSE"),
        ("00800公告", "經臺灣證券交易所核准，自104年12月9日終止上市。", "TWSE"),
        ("0080公告", "經臺灣證券交易所核准，自104年12月9日終止上市。", "TPEx"),
        ("0080公告", "經臺灣證券交易所核准，但不會自104年12月9日終止上市。", "TWSE"),
        ("0080公告", "未經臺灣證券交易所核准，自104年12月9日終止上市。", "TWSE"),
        ("0080公告", "尚未取得臺灣證券交易所同意，自104年12月9日終止上市。", "TWSE"),
        (
            "0080公告",
            "未取得臺灣證券交易所同意，已於104年12月9日終止在臺灣上市。",
            "TWSE",
        ),
        (
            "0080公告",
            "經臺灣證券交易所核准，取消終止上市。終止上市日：104年12月9日",
            "TWSE",
        ),
        (
            "0080公告",
            "經臺灣證券交易所核准，分割換發。終止上市日：104年12月9日",
            "TWSE",
        ),
    ],
)
def test_local_delisting_requires_matching_code_market_and_positive_approval(
    subject, body, market
):
    with pytest.raises(ValueError) as failure:
        s.parse_notice(
            market,
            {**TWSE_PARAMS, "fund": "0080"},
            subject,
            body,
            "https://example.com",
        )
    assert not isinstance(failure.value, s.NoticeDatePending)


@pytest.mark.parametrize(
    "prefix",
    [
        "終止上市日：112年2月30日。",
        "終止上市日：112/04/21。",
        "終止上市日：日期錯誤。",
        "終止上市日：112年4月21日；終止上市日：112年4月22日。",
    ],
)
def test_pending_wording_cannot_hide_invalid_or_conflicting_dates(prefix):
    case = next(c for c in NOTICE_CASES if c["params"]["fund"] == "00732")
    with pytest.raises(ValueError) as failure:
        s.parse_notice(
            case["market"],
            case["params"],
            case["subject"],
            prefix + case["description"],
            case["url"],
        )
    assert not isinstance(failure.value, s.NoticeDatePending)


def test_real_stock_lists_and_etf_indexes():
    with httpx2.Client(transport=httpx2.MockTransport(official_handler)) as client:
        assert s.fetch_twse_stocks(client)[0]["effective_date"] == "2026-09-01"
        assert s.fetch_tpex_stocks(client)[0]["code"] == "5371"
        assert s.fetch_twse_notices(client)[0][0] == TWSE_PARAMS
        assert s.fetch_tpex_notices(client)[0][0] == TPEX_PARAMS


@pytest.mark.parametrize(
    "body",
    [
        "信託契約終止；本基金暫停交易，並非終止上市。",
        "信託契約終止日：111年5月30日；清算基準日：111年6月29日。",
        "分割換發舊受益憑證；信託契約終止。終止上市日：111年5月27日",
        "信託契約終止。終止上市日：111年5月27日；終止上市日期：111年5月28日",
        "信託契約終止。終止上市日：111年5月27日；最後交易日：111年5月30日",
        "信託契約終止。終止上市日：111年2月30日",
    ],
)
def test_ambiguous_or_non_termination_text_cannot_change_status(body):
    with pytest.raises(ValueError):
        s.parse_notice("TWSE", TWSE_PARAMS, "公告", body, "https://www.twse.com.tw/")


def test_whitespace_padded_codes_are_normalized():
    """Live 2026-09-16 TPEx data had codes like '1752 ' and '\\t3126' —
    the same product, not a code-shape violation."""

    def handler(request):
        if request.url.path == "/v1/company/suspendListingCsvAndHtml":
            return httpx2.Response(
                200, json=[{"Code": " 2867 ", "DelistingDate": " 115/09/01 "}]
            )
        return httpx2.Response(
            200,
            json={
                "stat": "ok",
                "tables": [
                    {
                        "fields": ["股票代號", "終止上櫃日期", "終止上櫃原因"],
                        "totalCount": 1,
                        "data": [["\t3126", "111-01-19 ", "測試"]],
                    }
                ],
            },
        )

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        assert s.fetch_twse_stocks(client)[0]["code"] == "2867"
        assert s.fetch_tpex_stocks(client)[0]["code"] == "3126"


def test_blocking_http_error_is_retried_then_succeeds():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) < 3:
            return httpx2.Response(428)
        return httpx2.Response(
            200, json=[{"Code": "2867", "DelistingDate": "115/09/01"}]
        )

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        assert s.fetch_twse_stocks(client)[0]["code"] == "2867"
    assert len(attempts) == 3


@pytest.mark.parametrize(
    "status,is_blocking", [(428, True), (403, True), (500, True), (404, False)]
)
def test_is_blocking_error_excludes_404(status, is_blocking):
    with httpx2.Client(
        transport=httpx2.MockTransport(lambda r: httpx2.Response(status))
    ) as client:
        try:
            client.get("https://example.com/").raise_for_status()
        except httpx2.HTTPStatusError as exc:
            assert s.is_blocking_error(exc) is is_blocking


def test_renames_do_not_become_termination_candidates():
    assert not s.termination_subject("0056 名稱變更")
    assert not s.termination_subject("清算程序完成公告")


def test_tpex_detail_rejects_different_product():
    row = json.loads(fixture("tpex_00794b.json"))[0]
    row["stockNo"] = "0050"
    with httpx2.Client(
        transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json=[row]))
    ) as client:
        with pytest.raises(ValueError, match="商品代碼"):
            s.fetch_notice(client, "TPEx", TPEX_PARAMS)


def test_tpex_stock_pagination_is_complete_and_detects_missing_page():
    original = json.loads(fixture("tpex_stocks.json"))
    requests = []

    def handler(request):
        offset = int(request.url.params["paging-offset"])
        requests.append(offset)
        table = dict(original["tables"][0])
        table["data"] = table["data"][offset : offset + 1]
        return httpx2.Response(200, json={"stat": "ok", "tables": [table]})

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        assert len(s.fetch_tpex_stocks(client)) == 3
    assert requests == [0, 1, 2]
    original["tables"][0]["totalCount"] = 4
    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="缺頁"):
            s.fetch_tpex_stocks(client)


def test_tpex_stock_repeated_page_is_not_accepted():
    row = json.loads(fixture("tpex_stocks.json"))
    row["tables"][0]["totalCount"] = 6
    with httpx2.Client(
        transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json=row))
    ) as client:
        with pytest.raises(ValueError, match="重複"):
            s.fetch_tpex_stocks(client)


def test_twse_pagination_detects_duplicate_content_and_external_next_link():
    def handler(request):
        response = official_handler(request)
        next_href = "/zh/ETFortune/announcementList?type=listing&amp;offset=10"
        return httpx2.Response(
            200,
            text=response.text + f'<a class="nextLink" href="{next_href}">下一頁</a>',
        )

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="重複"):
            s.fetch_twse_notices(client)

    def external(request):
        return httpx2.Response(
            200,
            text=official_handler(request).text
            + '<a class="nextLink" href="https://example.com/">下一頁</a>',
        )

    with httpx2.Client(transport=httpx2.MockTransport(external)) as client:
        with pytest.raises(ValueError, match="網址"):
            s.fetch_twse_notices(client)


@pytest.mark.parametrize(
    "loader",
    [
        s.fetch_twse_stocks,
        s.fetch_tpex_stocks,
        s.fetch_twse_notices,
        s.fetch_tpex_notices,
    ],
)
def test_empty_or_http_failure_is_not_a_valid_source(loader):
    with httpx2.Client(
        transport=httpx2.MockTransport(lambda r: httpx2.Response(404))
    ) as client:
        with pytest.raises(httpx2.HTTPStatusError):
            loader(client)
    with httpx2.Client(
        transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json=[]))
    ) as client:
        with pytest.raises((ValueError, AttributeError)):
            loader(client)
