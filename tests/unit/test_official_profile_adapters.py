"""Small, redacted official response excerpts, verified 2026-10-14.

URLs are constants in official_profile_adapters. HTML fixtures retain only the
labelled rows and identity attributes relevant to the parsing contract.
"""

import json

import httpx2
import pytest

from taiwan_stock.catalog import investment_trust_adapters as trust
from taiwan_stock.catalog import official_profile_adapters as adapters
from taiwan_stock.catalog.profile_html import Tree, field


def client(body, *, status=200):
    return httpx2.Client(
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(status, text=body)
        )
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            "月配 / 每月底",
            {"distribution_policy": "distributing", "frequency": "monthly"},
        ),
        (
            "一年兩次",
            {"distribution_policy": "distributing", "frequency": "semiannual"},
        ),
        ("否", {"distribution_policy": "non_distributing"}),
        ("累積", {"distribution_policy": "non_distributing"}),
        ("不配", {"distribution_policy": "non_distributing"}),
        (
            "本基金之收益全部併入本基金資產，不予分配。",
            {"distribution_policy": "non_distributing"},
        ),
        ("是", {"distribution_policy": "distributing"}),
        ("視收益決定，可能每月", {}),
        (None, {}),
    ],
)
def test_policy_requires_explicit_label(raw, expected):
    assert adapters.policy(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            "不分級別/分配(一年兩次配息)",
            {
                "distribution_frequency_raw": "一年兩次配息",
                "distribution_policy": "distributing",
                "frequency": "semiannual",
            },
        ),
        (
            "其他級別/不分配",
            {
                "distribution_frequency_raw": "不分配",
                "distribution_policy": "non_distributing",
            },
        ),
        ("有發行配息級別", {}),
        ("不分級別/分配(未識別週期)", {"distribution_frequency_raw": "未識別週期"}),
    ],
)
def test_sitca_policy_requires_explicit_class_and_frequency(raw, expected):
    assert adapters.sitca_distribution_class(raw) == expected


def test_sitca_profile_reads_policy_from_exact_stock_code_row():
    index = '<select><option value="202609" selected>202609</option></select>'
    headers = [
        "序號",
        "會員代碼",
        "經理公司",
        "證券代號",
        "證券簡稱",
        "基金名稱",
        "基金成立日期",
        "分類",
        "追蹤指數",
        "是否有發行 配息級別",
    ]
    row = [
        "1",
        "A0037",
        "國泰投信",
        "00922",
        "國泰台灣領袖50",
        "國泰台灣領袖50ETF",
        "20230316",
        "上市ETF",
        "MSCI指數",
        "不分級別/分配(一年兩次配息)",
    ]
    report = "<table><tr>" + "".join(f"<th>{v}</th>" for v in headers)
    report += "</tr><tr>" + "".join(f"<td>{v}</td>" for v in row) + "</tr></table>"

    def handler(request):
        return httpx2.Response(
            200, text=report if "etf_info2" in request.url.path else index
        )

    result = adapters.fetch_sitca_profiles(
        httpx2.Client(transport=httpx2.MockTransport(handler))
    )
    assert result["00922"]["frequency"] == "semiannual"
    assert result["00922"]["distribution_frequency_raw"] == "一年兩次配息"
    assert result["00922"]["distribution_class_raw"] == row[9]
    assert result["00922"]["data_period"] == "202609"


def test_fubon_current_heading_wins_over_old_trading_information():
    html = (
        '<h5>00900 / 富邦商品</h5><h6 class="mb20">收益分配</h6>'
        '<div class="features_box"><p>月配息</p></div>'
        "<ul><li><p>收益分配</p><p>一年兩次</p></li></ul>"
    )
    result = trust.fetch_fubon_custodian(client(html), "00900")
    assert result["frequency"] == "monthly"
    assert result["custodian_bank"] is None
    conflict = trust.fetch_fubon_custodian(
        client(
            html + "<h6>收益分配</h6><div>季配息</div><p>保管銀行</p><p>永豐銀行</p>"
        ),
        "00900",
    )
    assert conflict["custodian_bank"] == "永豐銀行"
    assert isinstance(conflict["field_errors"]["frequency"], ValueError)


def test_nomura_json_identity_and_independent_fields():
    body = {
        "Entries": {
            "FundDetail": {
                "CStockNo": "00944",
                "CCgDividendTypeName": "月配",
                "CCustodyBankName": None,
                "CListingDt": "2024/05/09",
                "CIndexCode": "官方指數",
            }
        }
    }
    requests = []

    def handler(request):
        requests.append(request)
        return httpx2.Response(200, json=body)

    result = adapters.fetch_nomura(
        httpx2.Client(transport=httpx2.MockTransport(handler)), "00944"
    )
    assert result["frequency"] == "monthly"
    assert result["listed_date"] == "2024-05-09"
    assert json.loads(requests[0].content)["FundNo"] == "00944"
    with pytest.raises(ValueError, match="代碼不符"):
        adapters.fetch_nomura(client(json.dumps(body)), "00935")


def test_fuhwa_maps_codes_and_reads_information_across_tables():
    def row(code, page):
        return (
            '<tr class="fundListTable-fundCard etfListTable-fundCard">'
            f'<td class="etfListTable-code pr-md-2">{code}</td>'
            f'<td><a href="/ETF/etf_detail/{page}#nav">名稱</a>'
            f'<a href="/ETF/etf_detail/{page}">詳情</a></td></tr>'
        )

    catalog_html = f"<table>{row('00929', 'ETF21')}{row('00758B', 'ETF10')}</table>"
    catalog = adapters.fetch_fuhwa_catalog(client(catalog_html))
    assert catalog == {
        "00929": "https://www.fhtrust.com.tw/ETF/etf_detail/ETF21",
        "00758B": "https://www.fhtrust.com.tw/ETF/etf_detail/ETF10",
    }
    detail = (
        '<span class="text-en-9-bold">00929</span>'
        '<table><tr><th class="fundInformationTable-head">收益分配 / 評價日</th>'
        "<td>月配 / 每月底</td></tr></table>"
        '<table><tr><th class="fundInformationTable-head">保管機構</th>'
        "<td>台北富邦商業銀行</td></tr></table>"
    )
    result = adapters.fetch_fuhwa(client(detail), "00929", catalog)
    assert result["frequency"] == "monthly"
    assert result["custodian_bank"] == "台北富邦商業銀行"
    with pytest.raises(ValueError, match="代碼不符"):
        adapters.fetch_fuhwa(
            client(detail.replace("00929", "00650L")), "00929", catalog
        )


@pytest.mark.parametrize(
    "cells",
    [
        '<td class="etfListTable-code">00929</td>',
        '<td class="etfListTable-code">00929</td>'
        '<td><a href="/ETF/etf_detail/ETF21">A</a>'
        '<a href="/ETF/etf_detail/ETF22">B</a></td>',
        '<td><a href="/ETF/etf_detail/ETF21">A</a></td>',
    ],
)
def test_fuhwa_catalog_rejects_rows_without_unique_code_and_page(cells):
    html = f'<table><tr class="etfListTable-fundCard">{cells}</tr></table>'
    with pytest.raises(ValueError, match="唯一代碼或商品頁"):
        adapters.fetch_fuhwa_catalog(client(html))


def test_html_exact_labels_decode_entities_and_reject_conflicts():
    root = Tree(
        "<li><span>保管銀行</span><span>彰化&#x9280;&#x884C;</span></li>"
        "<script>保管銀行 = 其他銀行</script>"
    ).root
    assert field(root, "保管銀行", tags={"span"}) == "彰化銀行"
    with pytest.raises(ValueError):
        field(Tree("<th>銀行</th><td>A</td><th>銀行</th><td>B</td>").root, "銀行")


def test_taishin_uses_latest_explicit_frequency_not_history_count():
    html = (
        '<div class="fund_card"><div class="SeriesName" id="00775B">00775B</div>'
        "<table><tr><td>2025 / 01</td><td>半年配</td></tr>"
        "<tr><td>2026 / 07</td><td>季配</td></tr></table></div>"
    )
    result = adapters.fetch_taishin_policies(client(html))["00775B"]
    assert result["frequency"] == "quarterly"
    assert result["policy_period"] == "2026/07"


def test_tpex_rejects_wrong_product_and_normalizes_listing_date():
    body = {
        "stockNo": "00719B",
        "issuer": "元大投信",
        "listingDate": "2018.02.01",
        "underlyingIndex": "ICE 美國政府債券指數",
        "strategy": None,
    }
    result = adapters.fetch_tpex_profile(client(json.dumps(body)), "00719B")
    assert result["listed_date"] == "2018-02-01"
    assert result["issuer_name"] == "元大投信"
    with pytest.raises(ValueError):
        adapters.fetch_tpex_profile(client(json.dumps(body)), "00980D")


def kgi_client(details, *, overview='<a href="/Fund/Detail?fundID=J015">名稱可改</a>'):
    requested = []

    def handler(request):
        if request.url.path == "/Home/ETF":
            return httpx2.Response(200, text=overview)
        fund_id = request.url.params["fundID"]
        requested.append(fund_id)
        if fund_id not in details:
            return httpx2.Response(500, text="<p>error</p>")
        return httpx2.Response(200, text=details[fund_id])

    return httpx2.Client(transport=httpx2.MockTransport(handler)), requested


def kgi_detail(code, distribution, bank="彰化銀行"):
    return (
        f"<li><span>證券代號</span><span>{code}</span></li>"
        f"<li><span>收益分配</span><span>{distribution}</span></li>"
        f"<li><span>保管銀行</span><span>{bank}</span></li>"
    )


def test_kgi_catalog_resolves_official_identity_not_display_name():
    c, _ = kgi_client({"J015": kgi_detail("00915", "季配")})
    assert adapters.fetch_kgi_catalog(c)["00915"]["frequency"] == "quarterly"


def test_kgi_catalog_finds_funds_newer_than_the_overview_links():
    c, requested = kgi_client(
        {
            "J015": kgi_detail("00915", "季配"),
            "J016": kgi_detail("00407A", "不分配"),
            "J018": kgi_detail("00999A", "月配"),
            "J022": kgi_detail("00998A", "月配"),
        }
    )
    catalog = adapters.fetch_kgi_catalog(c)
    assert catalog["00407A"]["distribution_policy"] == "non_distributing"
    assert catalog["00407A"]["custodian_bank"] == "彰化銀行"
    assert catalog["00407A"]["source_url"].endswith("fundID=J016")
    # A gap shorter than the miss limit is crossed; three misses in a row end it.
    assert catalog["00999A"]["frequency"] == "monthly"
    assert "00998A" not in catalog
    assert requested == ["J015", "J016", "J017", "J018", "J019", "J020", "J021"]


def test_kgi_catalog_probe_skips_codeless_pages_but_linked_pages_need_one():
    c, _ = kgi_client({"J015": kgi_detail("00915", "季配"), "J016": "<p>維護中</p>"})
    assert set(adapters.fetch_kgi_catalog(c)) == {"00915"}
    c, _ = kgi_client({"J015": "<p>維護中</p>"})
    with pytest.raises(ValueError, match="證券代號"):
        adapters.fetch_kgi_catalog(c)


def test_kgi_catalog_rejects_one_code_on_two_different_pages():
    c, _ = kgi_client(
        {"J015": kgi_detail("00915", "季配"), "J016": kgi_detail("00915", "月配")}
    )
    with pytest.raises(ValueError, match="不唯一"):
        adapters.fetch_kgi_catalog(c)


def test_kgi_catalog_probe_is_bounded():
    details = {f"J{n:03d}": kgi_detail(f"{n:05d}A", "月配") for n in range(15, 60)}
    c, requested = kgi_client(details)
    assert len(adapters.fetch_kgi_catalog(c)) == 1 + adapters._KGI_PROBE_LIMIT
    assert requested[-1] == f"J{15 + adapters._KGI_PROBE_LIMIT:03d}"


def test_ft_ids_and_code_are_verified_before_basic_values_are_used():
    pages = {
        "Fund/List": {"data": [{"code": "00961", "id": 3}]},
        "Fund/Detail/3": {"code": "00961", "fund0Id": 3, "ftftId": "162"},
        "Fund0/Detail/3": {
            "id": 3,
            "distribution": "每月",
            "custodianBank": "彰化商業銀行股份有限公司\r\n",
        },
    }
    c = httpx2.Client(
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(
                200, json=pages[request.url.path.split("/newetf/")[1]]
            )
        )
    )
    catalog = adapters.fetch_ft_catalog(c)
    result = adapters.fetch_ft(c, "00961", catalog)
    assert result["frequency"] == "monthly"
    assert result["custodian_bank"] == "彰化商業銀行股份有限公司"
    pages["Fund0/Detail/3"]["id"] = 5
    with pytest.raises(ValueError, match="編號不符"):
        adapters.fetch_ft(c, "00961", catalog)


def test_uob_excludes_mutual_funds_and_preserves_exact_etf_code():
    body = {
        "resultP": [
            {
                "result": [
                    {
                        "fundID": "88329556",
                        "etf002": "00918",
                        "ec010": "台灣中小企業銀行",
                        "ec022": "每季",
                    },
                    {"fundID": "MUTUAL", "etf002": None, "ec022": "每月"},
                ]
            }
        ]
    }
    result = adapters.fetch_uob_catalog(client(json.dumps(body)))
    assert list(result) == ["00918"]
    assert result["00918"]["frequency"] == "quarterly"


def test_esun_six_digit_security_code_is_not_truncated():
    def handler(request):
        if request.url.path.endswith("GetETFOverview"):
            return httpx2.Response(
                200,
                json={
                    "TotalPages": 1,
                    "Entries": [{"StcokNo": "009803", "FundNo": "50"}],
                },
            )
        assert json.loads(request.content)["FundNo"] == "50"
        return httpx2.Response(
            200,
            json={
                "Entries": {
                    "CSecuritiesCode": "009803",
                    "CCgDividendTypeStr": "季配型",
                    "CCustodyBankStr": "元大商業銀行",
                }
            },
        )

    c = httpx2.Client(transport=httpx2.MockTransport(handler))
    catalog = adapters.fetch_esun_catalog(c)
    assert "00980" not in catalog
    assert adapters.fetch_esun(c, "009803", catalog)["frequency"] == "quarterly"
    with pytest.raises(ValueError, match="證券代碼不符"):
        adapters.fetch_esun(c, "009810", {"009810": "50"})


def test_jko_explicit_non_distribution_and_bank_absence():
    body = {
        "TotalPages": 1,
        "Entries": [
            {
                "StockNo": "00715L",
                "FundNo": "39",
                "IsETF": True,
                "FundDividendStr": "不配息",
            },
            {
                "StockNo": "00992B",
                "FundNo": "62",
                "IsETF": True,
                "FundDividendStr": "月配",
            },
        ],
    }
    result = adapters.fetch_jko_catalog(client(json.dumps(body)))
    assert result["00715L"]["distribution_policy"] == "non_distributing"
    assert result["00992B"]["frequency"] == "monthly"
    assert not result["00992B"].get("custodian_bank")


def test_blackrock_catalog_uses_canonical_url_and_verifies_product_code():
    html = (
        '<a href="https://www.blackrock.com/tw/products/345655/">009813</a>'
        '<a href="https://www.blackrock.com/tw/products/345655/fund">基金 009813</a>'
    )
    catalog = adapters.fetch_blackrock_catalog(client(html))
    assert catalog["009813"].endswith("/fund")
    data = "<div>ETF 代號</div><div>009813</div><div>配息方式</div><div>半年配息</div>"
    assert (
        adapters.fetch_blackrock(client(data), "009813", catalog)["frequency"]
        == "semiannual"
    )
    with pytest.raises(ValueError):
        adapters.fetch_blackrock(
            client(data.replace("009813", "009826")), "009813", catalog
        )


def test_allianz_normal_antiforgery_handshake_stays_on_issuer_requests():
    def handler(request):
        if request.url.host != "etf.allianzgi.com.tw":
            assert "x-xsrf-token" not in request.headers
            return httpx2.Response(200)
        if request.url.path.endswith("GetAntiForgeryToken"):
            return httpx2.Response(
                200,
                json={"token": "example-session"},
                headers={"set-cookie": "session=example; Path=/; Secure"},
            )
        assert request.headers["x-xsrf-token"] == "example-session"
        assert "session=example" in request.headers["cookie"]
        if request.url.path.endswith("GetFundOverview"):
            return httpx2.Response(
                200,
                json={
                    "TotalItems": 1,
                    "Entries": [{"CFundNo": "E0001", "CSecuritiesCode": "00984A"}],
                },
            )
        return httpx2.Response(
            200,
            json={
                "Entries": {
                    "CFundNo": "E0001",
                    "CSecuritiesCode": "00984A",
                    "CCgDividendFrequencyStr": "季配",
                    "CCgCustodyBankStr": "國泰世華銀行",
                }
            },
        )

    c = httpx2.Client(transport=httpx2.MockTransport(handler))
    result = adapters.fetch_allianz(c, "00984A", adapters.fetch_allianz_catalog(c))
    assert result["frequency"] == "quarterly"
    assert "verification" not in result
    c.get("https://example.org/")


def test_jpm_catalog_and_detail_verify_both_identities():
    def handler(request):
        if request.url.path.endswith("autocomplete"):
            return httpx2.Response(
                200,
                json=[{"ticker": "00401A", "cusip": "TW00000401A1", "fundType": "etf"}],
            )
        return httpx2.Response(
            200,
            json={
                "fundData": {
                    "shareClass": {
                        "cusip": "TW00000401A1",
                        "exchangeTicker": "00401A",
                        "distributionFrequency": "MONTHLY",
                    }
                }
            },
        )

    c = httpx2.Client(transport=httpx2.MockTransport(handler))
    catalog = adapters.fetch_jpm_catalog(c)
    assert adapters.fetch_jpm(c, "00401A", catalog)["frequency"] == "monthly"
    with pytest.raises(ValueError):
        adapters.fetch_jpm(c, "00989A", {"00989A": "TW00000401A1"})


def test_ab_official_frequency_does_not_require_a_bank():
    body = {
        "etfs": [
            {
                "fundInfo": {
                    "fundNumber": "00980D",
                    "isin": "TW00000980D8",
                    "dividendPayoutFrequency": (
                        "fundinfo-dividendpayoutfrequency-monthly"
                    ),
                }
            }
        ]
    }
    result = adapters.fetch_ab_catalog(client(json.dumps(body)))
    assert result["00980D"]["frequency"] == "monthly"
    assert result["00980D"].get("custodian_bank") is None
    body["etfs"][0]["fundInfo"]["isin"] = "TW00000984D0"
    with pytest.raises(ValueError):
        adapters.fetch_ab_catalog(client(json.dumps(body)))


def test_blackrock_policy_campaign_requires_exact_security_identity():
    def handler(request):
        if "/products/" in request.url.path:
            return httpx2.Response(
                200,
                text=(
                    "<div>ETF 代號</div><div>009826</div>"
                    "<div>保管銀行</div><div>永豐銀行</div>"
                    '<a href="/tw/ishares/global-allcap-equity-etf">009826</a>'
                ),
            )
        return httpx2.Response(
            200,
            text="<div>證券代號</div><div>009826</div><div>收益分配</div><div>不配息</div>",
        )

    c = httpx2.Client(transport=httpx2.MockTransport(handler))
    data = adapters.fetch_blackrock(
        c, "009826", {"009826": "https://www.blackrock.com/tw/products/351824/fund"}
    )
    assert data["distribution_policy"] == "non_distributing"
    assert data["custodian_bank"] == "永豐銀行"
    assert data["policy_source_url"].endswith("/global-allcap-equity-etf")


def test_fubon_rejects_a_different_product_even_with_valid_policy_fields():
    # Fubon falls back to another product's page for unknown codes.
    with pytest.raises(trust.ProductNotListed):
        trust.fetch_fubon_custodian(
            client(
                "<h5>0052 / 富邦科技</h5><p>保管銀行</p><p>永豐銀行</p>"
                "<h6>收益分配</h6><div>年配息</div>"
            ),
            "00900",
        )
