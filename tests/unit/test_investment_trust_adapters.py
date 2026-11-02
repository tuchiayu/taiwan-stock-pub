"""Unit tests for the per-投信 custodian bank/frequency adapters
(design 004 §4.3.1/§4.8 Phase E). Each fixture is a minimal snippet
reproducing the exact tag structure verified live against each site
while writing the adapter — see the module docstring for the full URL
list and confidence notes.
"""

import html
import json

import httpx2
import pytest

from taiwan_stock.catalog import investment_trust_adapters as m
from taiwan_stock.catalog import sync_history


def _client(html: str) -> httpx2.Client:
    return httpx2.Client(
        transport=httpx2.MockTransport(lambda request: httpx2.Response(200, text=html))
    )


# --- 群益 (Capital) -------------------------------------------------------


def test_capital_custodian_frequency_and_month_pattern():
    html = (
        '<div class="th cell">保管銀行</div>'
        '<div class="td cell small-6 medium-auto">中國信託商業銀行  </div>'
        '<h3 class="main-info-item-value profit-frequency">季配</h3>'
        '<div class="th cell">收益分配評價月份</div>'
        '<div class="td cell small-6 medium-auto">2、5、8、11月  </div>'
    )
    result = m.fetch_capital_custodian_and_frequency(_client(html), "195")

    assert result == {
        "source_url": "https://www.capitalfund.com.tw/etf/product/detail/195/basic",
        "custodian_bank": "中國信託商業銀行",
        "distribution_frequency_raw": "季配",
        "distribution_month_pattern": [2, 5, 8, 11],
    }


def test_capital_custodian_missing_raises():
    with pytest.raises(m.TrustAdapterError):
        m.fetch_capital_custodian_and_frequency(_client("<div>no data</div>"), "195")


def test_capital_product_ids_from_overview():
    html = (
        '<a href="/etf/product/detail/399/basic">00982A</a>'
        '<a href="/etf/product/detail/195/basic">00919</a>'
    )
    assert m.fetch_capital_product_ids(_client(html)) == {
        "00982A": "399",
        "00919": "195",
    }


def test_capital_ex_dividend_schedule_groups_are_distinct_from_evaluation():
    def card(label, months, code, internal_id):
        return (
            '<div class="schedule-main-section-card">'
            f'<div class="schedule-main-section-card-header-detail-right">{label}</div>'
            + "".join(
                '<div class="schedule-main-section-card-header-months-item">'
                f"{month}</div>"
                for month in months
            )
            + f'<a href="/etf/product/detail/{internal_id}/basic">{code} 商品</a>'
            "</div>"
        )

    html = card("季配", ["1 月", "4 月", "7 月", "10 月"], "00927", 366)
    html += card("半年配", ["2 月", "8 月"], "00923", 365)
    html += card("月配", ["每月"], "00937B", 378)
    result = m.fetch_capital_ex_dividend_schedule(_client(html))

    assert result["products"]["00927"] == {
        "frequency": "quarterly",
        "months": [1, 4, 7, 10],
    }
    assert result["products"]["00923"]["months"] == [2, 8]
    assert result["products"]["00937B"]["months"] == list(range(1, 13))
    assert result["source_url"].endswith("/cross/schedule")


@pytest.mark.parametrize(
    "months",
    ["1 月|4 月|7 月", "1 月|4 月|7 月|13 月", "1 月|4 月|4 月|10 月"],
)
def test_capital_ex_dividend_schedule_rejects_incomplete_or_bad_months(months):
    items = "".join(
        f'<div class="schedule-main-section-card-header-months-item">{month}</div>'
        for month in months.split("|")
    )
    html = (
        '<div class="schedule-main-section-card">'
        '<div class="schedule-main-section-card-header-detail-right">季配</div>'
        f'{items}<a href="/etf/product/detail/366/basic">00927 商品</a></div>'
    )
    with pytest.raises(m.TrustAdapterError):
        m.fetch_capital_ex_dividend_schedule(_client(html))


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2、5、8、11月", [2, 5, 8, 11]),
        ("3月", [3]),
        ("每月", None),
        ("2、13月", None),
        ("", None),
    ],
)
def test_parse_month_pattern(raw, expected):
    assert m._parse_month_pattern(raw) == expected


# --- 國泰 (Cathay) --------------------------------------------------------


def _json_client(payload) -> httpx2.Client:
    return httpx2.Client(
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, json=payload)
        )
    )


def test_cathay_fund_codes_from_list():
    payload = {
        "totalCount": 2,
        "result": [
            {"fundCode": "AJ", "stockCode": "00725B"},
            {"fundCode": "CN", "stockCode": "00878"},
        ],
        "returnCode": "2000",
    }
    assert m.fetch_cathay_fund_codes(_json_client(payload)) == {
        "00725B": "AJ",
        "00878": "CN",
    }


def test_cathay_fund_codes_rejects_truncated_list():
    payload = {
        "totalCount": 150,
        "result": [{"fundCode": "AJ", "stockCode": "00725B"}],
        "returnCode": "2000",
    }
    with pytest.raises(m.TrustAdapterError):
        m.fetch_cathay_fund_codes(_json_client(payload))


def test_cathay_custodian_and_frequency():
    payload = {
        "result": {"allotType": "季配", "bank": "玉山商業銀行 \xa0"},
        "returnCode": "2000",
    }
    result = m.fetch_cathay_custodian_and_frequency(_json_client(payload), "AJ")

    assert result == {
        "source_url": "https://cwapi.cathaysite.com.tw/api/ETF/GetETFIntroduce"
        "?fundCode=AJ",
        "custodian_bank": "玉山商業銀行",
        "distribution_frequency_raw": "季配",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"result": {"allotType": None, "bank": None}, "returnCode": "4005"},
        {"result": {"allotType": "", "bank": ""}, "returnCode": "2000"},
        {"result": None, "returnCode": "2000"},
    ],
)
def test_cathay_custodian_missing_raises(payload):
    with pytest.raises(m.TrustAdapterError):
        m.fetch_cathay_custodian_and_frequency(_json_client(payload), "AJ")


# --- 中信 (CTBC) ---------------------------------------------------------


def _ctbc_client(*, detail_code="00995A", frequency="<p>季配息</p>"):
    def handler(request):
        assert request.url.host == "www.ctbcinvestments.com"
        if request.url.path.endswith("/home/AuthToken"):
            data = {"token": "fresh-token"}
        elif request.url.path.endswith("/etf/ETFList"):
            data = {"Data": [{"ETF_ID": "00995A", "CNO": "00653201"}]}
        else:
            assert request.url.path.endswith("/etf/ETFDetail")
            data = {
                "FundDetail": [
                    {
                        "ETF_ID": detail_code,
                        "CNO": "00653201",
                        "PreserveBank": "元大銀行",
                        "DistriRate": frequency,
                    }
                ]
            }
        return httpx2.Response(200, json={"ResultCode": 0, "Data": data})

    return httpx2.Client(transport=httpx2.MockTransport(handler))


def test_ctbc_catalog_and_product_match_exact_code():
    client = _ctbc_client()
    catalog = m.fetch_ctbc_catalog(client)
    assert catalog["products"] == {"00995A": "00653201"}
    assert m.fetch_ctbc_custodian_and_frequency(client, "00995A", catalog) == {
        "source_url": "https://www.ctbcinvestments.com/Etf/00653201",
        "custodian_bank": "元大銀行",
        "distribution_frequency_raw": "季配息",
    }


def test_ctbc_rejects_mismatched_detail_code():
    client = _ctbc_client(detail_code="009819")
    with pytest.raises(m.TrustAdapterError, match="代碼不符"):
        m.fetch_ctbc_custodian_and_frequency(
            client, "00995A", m.fetch_ctbc_catalog(client)
        )


# --- 元大 (Yuanta) --------------------------------------------------------


def test_yuanta_custodian():
    html = '<div>保管銀行</div><div class="fundData">中國信託商業銀行</div>'
    assert m.fetch_yuanta_custodian(_client(html), "0056") == {
        "source_url": "https://www.yuantaetfs.com/product/detail/0056/",
        "custodian_bank": "中國信託商業銀行",
    }


def test_yuanta_custodian_missing_raises():
    with pytest.raises(m.TrustAdapterError):
        m.fetch_yuanta_custodian(_client("<div>no data</div>"), "0056")


def test_yuanta_maintenance_redirect_is_a_transient_failure():
    # Design 048 §4.2.8: observed 2026-10-31, every product page answered
    # with a redirect to /maintenance and then HTTP 200.
    def handler(request):
        if request.url.path == "/maintenance":
            return httpx2.Response(200, text="<p>系統維護中</p>")
        return httpx2.Response(302, headers={"location": "/maintenance"})

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    with pytest.raises(sync_history.SourceUnavailable):
        m.fetch_yuanta_custodian(client, "0050")


# --- 富邦 (Fubon) ---------------------------------------------------------


def test_fubon_custodian():
    html = "<h5>00692 / 富邦商品</h5><p>保管銀行</p>\n<p>\n華南商業銀行\n</p>"
    assert m.fetch_fubon_custodian(_client(html), "00692") == {
        "source_url": "https://websys.fsit.com.tw/FubonETF/Fund/Profile.aspx?stkId=00692",
        "custodian_bank": "華南商業銀行",
    }


def test_fubon_custodian_missing_raises():
    with pytest.raises(m.TrustAdapterError):
        m.fetch_fubon_custodian(_client("<p>no data</p>"), "00692")


# --- 第一金 (First Securities) --------------------------------------------


def test_fsitc_custodian_and_frequency():
    html = "<td>保管銀行</td><td>彰化銀行</td><td>有無配息</td><td>月配息</td>"
    assert m.fetch_fsitc_custodian_and_frequency(_client(html), "00980A") == {
        "source_url": "https://www.fsitc.com.tw/FundDetail.aspx?ID=00980A",
        "custodian_bank": "彰化銀行",
        "distribution_frequency_raw": "月配息",
    }


def test_fsitc_custodian_missing_raises():
    with pytest.raises(m.TrustAdapterError):
        m.fetch_fsitc_custodian_and_frequency(_client("<td>no data</td>"), "00980A")


def _fsitc_catalog_page(rows):
    value = html.escape(json.dumps(rows), quote=True)
    return f'<input id="ContentPlaceHolder1_HiddenField_FundName" value="{value}">'


def test_fsitc_code_absent_from_official_catalog_is_not_listed():
    page = _fsitc_catalog_page([{"cNo": "D90", "cETFStockCode": "00728"}])
    with pytest.raises(m.ProductNotListed):
        m.fetch_fsitc_custodian_and_frequency(_client(page), "00729R")


def test_fsitc_ambiguous_catalog_match_stays_a_format_error():
    page = _fsitc_catalog_page(
        [
            {"cNo": "D90", "cETFStockCode": "00728"},
            {"cNo": "D91", "cETFStockCode": "00728"},
        ]
    )
    with pytest.raises(m.TrustAdapterError, match="唯一基金編號"):
        m.fetch_fsitc_custodian_and_frequency(_client(page), "00728")


# --- 兆豐 (Mega) -----------------------------------------------------------


def test_mega_product_ids_from_index():
    html = (
        '<div class="item-num">00921</div>'
        '<a href="etf_product.aspx?id=18">兆豐台灣產業龍頭</a>'
        '<div class="item-num">00932</div>'
        '<a href="etf_product.aspx?id=19">兆豐台灣ESG</a>'
    )
    assert m.fetch_mega_product_ids(_client(html)) == {"00921": "18", "00932": "19"}


def test_mega_custodian():
    html = (
        '<div class="etfin5-title">保管銀行</div>'
        '<div class="etfin5-info">\n彰化商業銀行\n</div>'
    )
    assert m.fetch_mega_custodian(_client(html), "18") == {
        "source_url": "https://www.megafunds.com.tw/MEGA/etf/etf_product.aspx?id=18",
        "custodian_bank": "彰化商業銀行",
    }


def test_mega_custodian_missing_raises():
    with pytest.raises(m.TrustAdapterError):
        m.fetch_mega_custodian(_client("<div>no data</div>"), "18")


# --- 永豐 (SinoPac) --------------------------------------------------------


def test_sinopac_overview_maps_id_and_frequency_and_skips_missing_frequency():
    html = (
        "<tr><td>006204</td>"
        '<td><a href="/newweb/sitcFund/page.do?stock_id=37&amp;tab_on=1">'
        "永豐臺灣加權</a></td>"
        '<td><a class="textlink" href="/SinopacEtfs/Funds/DividenEtf?fund=37" '
        'target="_blank">年配</a></td></tr>'
        "<tr><td>00886</td>"
        '<td><a href="/newweb/sitcFund/page.do?stock_id=81&amp;tab_on=1">'
        "永豐美國科技</a></td>"
        "<td>不分配</td></tr>"
    )
    result = m.fetch_sinopac_overview(_client(html))

    assert result["006204"] == {
        "internal_id": "37",
        "distribution_frequency_raw": "年配",
    }
    # A fund with no `DividenEtf` link (e.g. "不分配") must not pick up
    # a neighbouring row's text as if it were its own frequency.
    assert result["00886"] == {"internal_id": "81"}


def test_sinopac_custodian():
    html = '<h4>保管銀行</h4><div class="fl_txt"><p>\n國泰世華銀行\n</p></div>'
    assert m.fetch_sinopac_custodian(_client(html), "37") == {
        "source_url": "https://sitc.sinopac.com/newweb/sitcFund/page.do?stock_id=37",
        "custodian_bank": "國泰世華銀行",
    }


def test_sinopac_custodian_missing_raises():
    with pytest.raises(m.TrustAdapterError):
        m.fetch_sinopac_custodian(_client("<div>no data</div>"), "37")


# --- 台新 (Taishin) --------------------------------------------------------


def test_taishin_custodian():
    html = "<th>保管銀行</th>\n<td>\n兆豐國際商業銀行\n</td>"
    assert m.fetch_taishin_custodian(_client(html), "00936") == {
        "source_url": "https://www.tsit.com.tw/ETF/Home/ETFSeriesDetail/00936",
        "custodian_bank": "兆豐國際商業銀行",
    }


def test_taishin_custodian_missing_raises():
    with pytest.raises(m.TrustAdapterError):
        m.fetch_taishin_custodian(_client("<th>no data</th>"), "00936")


# --- 統一 (Uni-President / ezmoney) ---------------------------------------


def test_ezmoney_fund_codes_from_bank_index():
    html = '<a href="/ETF/Fund/Info?fundCode=49YTW">00981A</a>'
    assert m.fetch_ezmoney_fund_codes(_client(html)) == {"00981A": "49YTW"}


def test_ezmoney_custodian_unescapes_and_strips_literal_escape_sequences():
    # Reproduces the real double-escaped shape (HTML text embedded in a
    # JSON string embedded in an HTML attribute): `\n`/`\t` here are
    # literal two-character escape sequences copied verbatim from a
    # real fetched page (design 004 §4.3.1), not actual whitespace
    # bytes, and html.unescape must run before the `\s*` in the regex
    # can see normal tag boundaries.
    fragment = (
        "保管銀行\\n\\t\\t\\t</th>\\n\\t\\t\\t<td>\\n\\t\\t\\t\\t"
        "華南銀行\\n\\t\\t\\t</td>"
    )
    html = f"<div>{fragment}</div>"

    assert m.fetch_ezmoney_custodian(_client(html), "49YTW") == {
        "source_url": "https://www.ezmoney.com.tw/ETF/Fund/Info?fundCode=49YTW",
        "custodian_bank": "華南銀行",
    }


def test_ezmoney_custodian_missing_raises():
    with pytest.raises(m.TrustAdapterError):
        m.fetch_ezmoney_custodian(_client("<div>no data</div>"), "49YTW")


# --- Issuer routing --------------------------------------------------------


@pytest.mark.parametrize(
    "name, issuer",
    [
        ("群益台灣精選高息", "群益"),
        ("國泰永續高股息", "國泰"),
        ("元大台灣50", "元大"),
        ("中信中國高股息", "中信"),
        ("", None),
    ],
)
def test_guess_issuer(name, issuer):
    assert m.guess_issuer(name) == issuer


def test_sinopac_alphanumeric_ids_and_explicit_non_distribution():
    body = (
        "<table><tr><th>證券代碼</th><th>基金名稱</th><th>配息頻率</th></tr>"
        "<tr><td>00930</td><td>"
        '<a href="/newweb/sitcFund/page.do?stock_id=B5">永豐</a></td>'
        '<td><a href="DividenEtf?fund=B5">雙月配</a></td></tr>'
        "<tr><td>00886</td><td>"
        '<a href="/newweb/sitcFund/page.do?stock_id=81">永豐</a></td>'
        "<td>不分配</td></tr></table>"
    )
    c = httpx2.Client(
        transport=httpx2.MockTransport(lambda request: httpx2.Response(200, text=body))
    )
    result = m.fetch_sinopac_overview(c)
    assert result["00930"]["internal_id"] == "B5"
    assert result["00930"]["distribution_frequency_raw"] == "雙月配"
    assert result["00886"]["distribution_frequency_raw"] == "不分配"


def test_ezmoney_decodes_current_object_and_excludes_related_funds():
    import html
    import json

    current = {
        "sFundCode": "49YTW",
        "sStockNo": "00981A ",
        "mFundIntro": (
            "<table><tr><th>股票代號</th><td>00981A</td></tr>"
            "<tr><th>保管銀行</th><td><span>彰化商業銀行</span></td></tr>"
            "<tr><th>收益分配</th><td>季配息，每年2、5、8、11月底評價。</td></tr></table>"
        ),
    }
    unrelated = [
        {"sFundCode": "other", "mFundIntro": "<th>保管銀行</th><td>元大銀行</td>"}
    ]
    body = (
        '<div data-content="'
        + html.escape(json.dumps(current), quote=True)
        + '"></div>'
    )
    body += (
        '<div data-content="'
        + html.escape(json.dumps(unrelated), quote=True)
        + '"></div>'
    )
    c = httpx2.Client(
        transport=httpx2.MockTransport(lambda request: httpx2.Response(200, text=body))
    )
    result = m.fetch_ezmoney_custodian(c, "49YTW")
    assert result["custodian_bank"] == "彰化商業銀行"
    assert result["frequency"] == "quarterly"
    with pytest.raises(m.TrustAdapterError, match="唯一基金代碼"):
        m.fetch_ezmoney_custodian(c, "wrong")
