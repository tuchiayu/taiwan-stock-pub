"""Official ETF product sources verified in design 022.

These are public website interfaces, not guaranteed stable external APIs.
Code validation and labelled fields are required before persisting values.
"""

import re
from datetime import date
from urllib.parse import urljoin

from taiwan_stock.catalog.profile_html import Tree, compact, field
from taiwan_stock.catalog.sync_history import listed
from taiwan_stock.shared import pacing
from taiwan_stock.shared.dates import today_taipei

NOMURA_API = "https://www.nomurafunds.com.tw/API/ETFAPI/api/Fund/GetFundDetailPage"
# The /ETF landing page only shows recommended cards; /ETF/etf_list is the
# complete product table (confirmed live 2026-10: 22 rows vs 16 cards).
FUHWA_LIST = "https://www.fhtrust.com.tw/ETF/etf_list"
SITCA_LIST = "https://www.sitca.org.tw/ROC/SITCA_ETF/etf_info.aspx"

FREQUENCIES = {
    "月配": "monthly",
    "月配息": "monthly",
    "每月": "monthly",
    "雙月配": "bimonthly",
    "雙月配息": "bimonthly",
    "季配": "quarterly",
    "季配息": "quarterly",
    "每季": "quarterly",
    "半年配": "semiannual",
    "半年配息": "semiannual",
    "一年兩次": "semiannual",
    "一年兩次配息": "semiannual",
    "每年兩次": "semiannual",
    "一年二次": "semiannual",
    "每年二次": "semiannual",
    "每年2次": "semiannual",
    "一年2次": "semiannual",
    "年配": "annual",
    "年配息": "annual",
    "每年一次": "annual",
    "每年": "annual",
    "每半年": "semiannual",
    "月配型": "monthly",
    "季配型": "quarterly",
    "年配型": "annual",
    "一年一次": "annual",
    "不定期": "irregular",
    "不定期配息": "irregular",
}
NON_DISTRIBUTING = {
    "否",
    "無",
    "不分配",
    "不配息",
    "不分配收益",
    "無收益分配",
    "累積",
    "累積型",
    "不配",
    "本基金之收益全部併入本基金資產，不予分配。",
    "併入基金資產，不予分配",
}


def policy(raw):
    """Only explicit, current source labels; no event-based frequency inference."""
    if not raw:
        return {}
    # Fuhwa's labelled field combines the policy and evaluation day with '/'.
    value = compact(raw).split("/")[0].split("／")[0]
    if value in NON_DISTRIBUTING:
        return {"distribution_policy": "non_distributing"}
    if value in FREQUENCIES:
        return {"distribution_policy": "distributing", "frequency": FREQUENCIES[value]}
    if value in {"是", "有", "有收益分配", "配息"}:
        return {"distribution_policy": "distributing"}
    return {}


def sitca_distribution_class(raw):
    """Parse only the class attached to an exact ETF stock-code row.

    The association's column title asks whether a distributing class exists.
    A bare yes/no is not enough to identify a traded class; the current ETF
    report instead publishes an explicit ``class/policy`` string per code.
    Unrecognised forms remain reference-only.
    """
    if not isinstance(raw, str):
        return {}
    match = re.fullmatch(
        r"(?:不分級別|其他級別)/(不分配|分配\(([^()]*)\))", compact(raw)
    )
    if not match:
        return {}
    label = match.group(2) if match.group(2) is not None else "不分配"
    return {"distribution_frequency_raw": label, **policy(label)}


def source_values(root, *, frequency_tags=None):
    errors = {}
    try:
        bank = field(root, "保管銀行", "保管機構", tags=frequency_tags)
    except ValueError as exc:
        bank = None
        errors["bank"] = exc
    raw = None
    for label in ("配息頻率", "配息方式", "收益分配 / 評價日", "收益分配", "有無配息"):
        try:
            raw = field(root, label, tags=frequency_tags)
        except ValueError as exc:
            errors.update(frequency=exc, policy=exc)
            break
        if raw:
            break
    result = {"custodian_bank": bank, "distribution_frequency_raw": raw, **policy(raw)}
    if errors:
        result["field_errors"] = errors
    return result


def fetch_nomura(client, code):
    today = today_taipei()
    response = client.post(
        NOMURA_API,
        json={
            "FundNo": code,
            "IsPreview": False,
            "SearchType": 1,
            "Year": int(today[:4]),
            "SDate": today + "T00:00:00.000Z",
            "EDate": today + "T23:59:59.000Z",
        },
    )
    response.raise_for_status()
    detail = response.json()["Entries"]["FundDetail"]
    if detail.get("CStockNo") != code:
        raise ValueError("野村基本資料回傳商品代碼不符")
    raw = detail.get("CCgDividendTypeName")
    return {
        "source_url": "https://www.nomurafunds.com.tw/ETFWEB/product-description?fundNo="
        + code,
        "custodian_bank": detail.get("CCustodyBankName"),
        "distribution_frequency_raw": raw,
        "benchmark": detail.get("CIndexCode"),
        "listed_date": (detail.get("CListingDt") or "").replace("/", "-") or None,
        **policy(raw),
    }


def fetch_fuhwa_catalog(client):
    response = client.get(FUHWA_LIST, follow_redirects=True)
    response.raise_for_status()
    result = {}
    for row in Tree(response.text).root.nodes():
        if row.tag != "tr" or not row.has_class("etfListTable-fundCard"):
            continue
        codes = [n.text() for n in row.nodes() if n.has_class("etfListTable-code")]
        hrefs = {
            n.attrs["href"].split("#")[0]
            for n in row.nodes()
            if n.tag == "a"
            and re.fullmatch(
                r"/ETF/etf_detail/ETF\d+(?:#nav)?", n.attrs.get("href", "")
            )
        }
        if (
            len(codes) != 1
            or not re.fullmatch(r"[0-9]{4,6}[A-Z]?", codes[0])
            or len(hrefs) != 1
        ):
            raise ValueError("復華商品清單列缺少唯一代碼或商品頁")
        code, url = codes[0], urljoin(FUHWA_LIST, hrefs.pop())
        if code in result and result[code] != url:
            raise ValueError("復華商品代碼對應到不同頁面")
        result[code] = url
    if not result:
        raise ValueError("復華商品總覽沒有可驗證的代碼對照")
    return result


def fetch_fuhwa(client, code, catalog):
    response = client.get(listed(catalog, code), follow_redirects=True)
    response.raise_for_status()
    root = Tree(response.text).root
    codes = [n.text() for n in root.nodes() if n.has_class("text-en-9-bold")]
    if code not in codes:
        raise ValueError("復華商品頁代碼不符")
    # The current information spans multiple tables (policy and bank are
    # separate); exact th labels exclude navigation and related products.
    values = source_values(root, frequency_tags={"th"})
    if not values["custodian_bank"] and not values.get("distribution_policy"):
        raise ValueError("復華商品頁未找到基本資料欄位")
    return {"source_url": str(response.url), **values}


def fetch_taishin_policies(client):
    response = client.get("https://www.tsit.com.tw/ETF/Home/ETFDIVList")
    response.raise_for_status()
    result = {}
    for card in Tree(response.text).root.nodes():
        if not card.has_class("fund_card"):
            continue
        codes = [n.attrs.get("id") for n in card.nodes() if n.has_class("SeriesName")]
        if len(codes) != 1 or not re.fullmatch(r"[0-9]{4,6}[A-Z]?", codes[0] or ""):
            continue
        rows = []
        for node in card.nodes():
            if node.tag != "tr":
                continue
            cells = [n.text() for n in node.children if getattr(n, "tag", None) == "td"]
            if len(cells) >= 2 and re.fullmatch(r"\d{4}\s*/\s*\d{2}", cells[0]):
                rows.append((compact(cells[0]), cells[1]))
        if not rows:
            continue
        latest = max(period for period, _ in rows)
        raw = {raw for period, raw in rows if period == latest}
        if len(raw) != 1:
            raise ValueError("台新最新配息頻率欄位衝突")
        raw = raw.pop()
        result[codes[0]] = {
            "distribution_frequency_raw": raw,
            **policy(raw),
            "policy_source_url": str(response.url),
            "policy_period": latest,
        }
    if not result:
        raise ValueError("台新配息表沒有可驗證的代碼及頻率")
    return result


def fetch_kgi_catalog(client):
    response = client.get("https://www.kgifund.com.tw/Home/ETF")
    response.raise_for_status()
    links = sorted(
        {
            urljoin(str(response.url), n.attrs["href"])
            for n in Tree(response.text).root.nodes()
            if n.tag == "a"
            and re.fullmatch(r"/Fund/Detail\?fundID=J\d+", n.attrs.get("href", ""))
        }
    )
    if not links:
        raise ValueError("凱基總覽未找到官方基金頁")
    result = {}

    def add(code, values):
        if code in result and result[code] != values:
            raise ValueError("凱基證券代號對應不唯一")
        result[code] = values

    for url in links:
        add(*_kgi_detail(client, url, required=True))
        pacing.pause(1)
    # The overview can lag newly listed funds (00407A at J024 was live but
    # unlinked on 2026-10-15), so also probe the IDs just past the highest
    # linked one; a missing ID returns an HTTP error page.
    highest = max(int(re.search(r"J(\d+)$", url)[1]) for url in links)
    misses = 0
    for number in range(highest + 1, highest + 1 + _KGI_PROBE_LIMIT):
        if misses >= _KGI_PROBE_MISSES:
            break
        url = urljoin(str(response.url), f"/Fund/Detail?fundID=J{number:03d}")
        found = _kgi_detail(client, url, required=False)
        misses = 0 if found else misses + 1
        if found:
            add(*found)
        pacing.pause(1)
    return result


_KGI_PROBE_LIMIT = 20
_KGI_PROBE_MISSES = 3


def _kgi_detail(client, url, *, required):
    response = client.get(url)
    if not required and response.is_error:
        return None
    response.raise_for_status()
    root = Tree(response.text).root
    code = field(root, "證券代號", tags={"span"})
    if not code or not re.fullmatch(r"[0-9]{4,6}[A-Z]?", code):
        if not required:
            return None
        raise ValueError("凱基基金頁缺少明確證券代號")
    return code, {
        "source_url": str(response.url),
        **source_values(root, frequency_tags={"span"}),
    }


def fetch_tpex_profile(client, code):
    response = client.post(
        "https://info.tpex.org.tw/api/etfProduct",
        params={"lang": "zh-tw", "query": code},
    )
    response.raise_for_status()
    data = response.json()
    if data.get("stockNo") != code:
        raise ValueError("TPEx 商品基本資料代碼不符")
    listed = data.get("listingDate")
    listed = (
        date.fromisoformat(listed.replace(".", "-")).isoformat() if listed else None
    )
    return {
        "issuer_name": data.get("issuer"),
        "benchmark": data.get("underlyingIndex"),
        "listed_date": listed,
        "source_url": str(response.url),
        "management_type": "active" if data.get("strategy") else None,
    }


def fetch_sitca_profiles(client):
    response = client.get(SITCA_LIST)
    response.raise_for_status()
    root = Tree(response.text).root
    months = [
        n.attrs.get("value")
        for n in root.nodes()
        if n.tag == "option"
        and "selected" in n.attrs
        and re.fullmatch(r"\d{6}", n.attrs.get("value", ""))
    ]
    if not months:
        raise ValueError("公會 ETF 基本資料缺少報表月份")
    month = max(months)
    response = client.get(
        urljoin(SITCA_LIST, "etf_info2.aspx"), params={"txtYM": month, "txtR1": "0"}
    )
    response.raise_for_status()
    rows = []
    for node in Tree(response.text).root.nodes():
        if node.tag == "tr":
            cells = [
                c.text()
                for c in node.children
                if getattr(c, "tag", None) in {"td", "th"}
            ]
            if cells:
                rows.append(cells)
    if not any("經理公司" in row and "證券代號" in row for row in rows):
        raise ValueError("公會 ETF 基本資料表格式變更")
    result = {}
    for row in rows:
        if len(row) != 10 or not re.fullmatch(r"[0-9]{4,6}[A-Z]?", row[3]):
            continue
        value = {
            "issuer_name": row[2],
            "benchmark": row[8] if row[8] not in {"-", "無", "不適用"} else None,
            "source_url": str(response.url),
            "data_period": month,
            "distribution_class_raw": row[9],
            **sitca_distribution_class(row[9]),
        }
        if row[3] in result and result[row[3]] != value:
            raise ValueError("公會商品代碼重複且基本資料不一致")
        result[row[3]] = value
    if not result:
        raise ValueError("公會 ETF 基本資料表為空")
    return result


FT_API = "https://www.ftft.com.tw/official/api/newetf/"


def fetch_ft_catalog(client):
    response = client.post(
        FT_API + "Fund/List",
        json={
            "pageRequestParameter": {
                "isReturnAllDataAndNoPage": True,
                "showCount": 0,
                "targetPage": 1,
            }
        },
    )
    response.raise_for_status()
    result = {}
    for row in response.json()["data"]:
        code = row.get("code", "")
        if re.fullmatch(r"[0-9]{4,6}[A-Z]?", code) and row.get("id"):
            if code in result:
                raise ValueError("富蘭克林華美代碼重複")
            result[code] = row["id"]
    if not result:
        raise ValueError("富蘭克林華美名單為空")
    return result


def fetch_ft(client, code, catalog):
    response = client.get(FT_API + "Fund/Detail/" + str(listed(catalog, code)))
    response.raise_for_status()
    fund = response.json()
    if fund.get("code") != code or not fund.get("fund0Id"):
        raise ValueError("富蘭克林華美商品代碼或基本資料編號不符")
    response = client.get(FT_API + "Fund0/Detail/" + str(fund["fund0Id"]))
    response.raise_for_status()
    data = response.json()
    if data.get("id") != fund["fund0Id"]:
        raise ValueError("富蘭克林華美基本資料編號不符")
    raw = data.get("distribution")
    return {
        "source_url": "https://www.ftft.com.tw/etf/product/details?id="
        + str(fund["ftftId"]),
        "custodian_bank": (data.get("custodianBank") or "").strip() or None,
        "distribution_frequency_raw": raw,
        **policy(raw),
        "listed_date": (data.get("listedDate") or "")[:10] or None,
    }


def fetch_uob_catalog(client):
    response = client.get("https://www.uobam.com.tw/json/reply/WebSiteStockListRequest")
    response.raise_for_status()
    result = {}
    for group in response.json()["resultP"]:
        for row in group["result"]:
            code = row.get("etf002") or ""
            if not re.fullmatch(r"[0-9]{4,6}[A-Z]?", code):
                continue
            raw = row.get("ec022")
            if code in result:
                raise ValueError("大華銀 ETF 代碼重複")
            result[code] = {
                "source_url": "https://www.uobam.com.tw/fund/etf/" + row["fundID"],
                "custodian_bank": row.get("ec010"),
                "distribution_frequency_raw": raw,
                **policy(raw),
                "benchmark": row.get("etf005"),
                "listed_date": (row.get("etf004") or "").replace("/", "-") or None,
            }
    if not result:
        raise ValueError("大華銀 ETF 商品名單為空")
    return result


def fetch_jko_catalog(client):
    response = client.post(
        "https://www.jkoam.com/webapi/api/Fund/GetETFOverviewList",
        json={
            "PageSize": 100,
            "PageIndex": 1,
            "Keyword": "",
            "TypeIDs": [],
            "AreaIDs": [],
            "RiskLvIDs": [],
            "DividendIDs": [],
            "CurrencyTypeIDs": [],
        },
    )
    response.raise_for_status()
    body = response.json()
    if body.get("TotalPages") != 1:
        raise ValueError("街口 ETF 名單未完整取得")
    result = {}
    for row in body["Entries"]:
        code = row.get("StockNo", "")
        if not re.fullmatch(r"[0-9]{4,6}[A-Z]?", code) or not row.get("IsETF"):
            continue
        raw = row.get("FundDividendStr")
        if code in result:
            raise ValueError("街口 ETF 代碼重複")
        result[code] = {
            "source_url": "https://www.jkoam.com/etf/etf-info/"
            + row["FundNo"]
            + "?tab=1",
            "distribution_frequency_raw": raw,
            **policy(raw),
        }
    if not result:
        raise ValueError("街口 ETF 商品名單為空")
    return result


ESUN_API = "https://www.esunam.com/ETFAPI/"


def fetch_esun_catalog(client):
    response = client.post(
        ESUN_API + "GetETFOverview",
        json={
            "PageSize": 999,
            "PageIndex": 1,
            "SortColName": "",
            "ETFInvestmentIDs": [],
            "ETFFundTypeIDs": [],
            "ETFFundDividendIDs": [],
            "ETFInvestAreaIDs": [],
            "IsDesc": False,
        },
    )
    response.raise_for_status()
    body = response.json()
    if body.get("TotalPages") != 1:
        raise ValueError("玉山 ETF 總覽未完整取得")
    result = {}
    for row in body["Entries"]:
        code = row.get("StcokNo", "")  # Official API's spelling, not StockNo.
        if re.fullmatch(r"[0-9]{4,6}[A-Z]?", code) and row.get("FundNo"):
            if code in result:
                raise ValueError("玉山 ETF 代碼重複")
            result[code] = row["FundNo"]
    if not result:
        raise ValueError("玉山 ETF 商品名單為空")
    return result


def fetch_esun(client, code, catalog):
    fund_no = listed(catalog, code)
    response = client.post(
        ESUN_API + "GetETFFundDetail",
        json={"FundNo": fund_no, "IsPreview": False},
    )
    response.raise_for_status()
    data = response.json()["Entries"]
    if data.get("CSecuritiesCode") != code:
        raise ValueError("玉山 ETF 基本資料證券代碼不符")
    raw = data.get("CCgDividendTypeStr")
    return {
        "source_url": "https://www.esunam.com/ETF/etf-info/"
        + str(catalog[code])
        + "?tab=1",
        "custodian_bank": data.get("CCustodyBankStr"),
        "distribution_frequency_raw": raw,
        **policy(raw),
        "listed_date": (data.get("CListingDt") or "").replace("/", "-") or None,
    }


def fetch_blackrock_catalog(client):
    response = client.get("https://www.blackrock.com/tw/ishares")
    response.raise_for_status()
    result = {}
    for node in Tree(response.text).root.nodes():
        url = node.attrs.get("href", "")
        if node.tag != "a" or not re.fullmatch(
            r"https://www[.]blackrock[.]com/tw/products/[0-9]+/[^?]*", url
        ):
            continue
        codes = re.findall(r"\b[0-9]{4,6}[A-Z]?\b", node.text())
        if len(codes) != 1:
            continue
        code = codes[0]
        if code in result:
            # Prefer the product's published canonical slug over its short URL.
            if result[code].split("/")[5] != url.split("/")[5]:
                raise ValueError("貝萊德 ETF 代碼對應不同商品")
            if len(url) <= len(result[code]):
                continue
        result[code] = url
    if not result:
        raise ValueError("貝萊德 ETF 總覽缺少商品對照")
    return result


def fetch_blackrock(client, code, catalog):
    response = client.get(listed(catalog, code))
    response.raise_for_status()
    root = Tree(response.text).root
    if field(root, "ETF 代號") != code:
        raise ValueError("貝萊德商品頁代碼不符")
    data = {
        "source_url": str(response.url),
        **source_values(root),
        "benchmark": field(root, "追蹤指數"),
    }
    if data.get("distribution_policy") or data.get("field_errors"):
        return data
    links = {
        urljoin(str(response.url), n.attrs["href"])
        for n in root.nodes()
        if n.tag == "a"
        and n.text() == code
        and re.fullmatch(r"/tw/ishares/[a-z0-9-]+", n.attrs.get("href", ""))
    }
    if len(links) == 1:
        try:
            campaign = client.get(links.pop())
            campaign.raise_for_status()
            details = Tree(campaign.text).root
            if field(details, "證券代號") != code:
                raise ValueError("貝萊德專頁證券代碼不符")
            values = source_values(details)
            for key in (
                "distribution_frequency_raw",
                "distribution_policy",
                "frequency",
            ):
                if values.get(key) is not None:
                    data[key] = values[key]
            data["policy_source_url"] = str(campaign.url)
            if values.get("field_errors"):
                data["field_errors"] = values["field_errors"]
        except Exception as exc:
            data["field_errors"] = {"frequency": exc, "policy": exc}
    return data


def fetch_ab_catalog(client):
    url = "https://webapi.alliancebernstein.com/v2/funds/tw/zh-tw/investor/etf"
    response = client.get(url)
    response.raise_for_status()
    result = {}
    for row in response.json()["etfs"]:
        data = row["fundInfo"]
        code = data.get("fundNumber", "")
        isin = data.get("isin", "")
        if not re.fullmatch(r"[0-9]{4,6}[A-Z]?", code) or isin[5:-1] != code:
            raise ValueError("聯博 ETF 證券代碼與 ISIN 不符")
        raw = data.get("dividendPayoutFrequency")
        value = (raw or "").removeprefix("fundinfo-dividendpayoutfrequency-")
        policy_value = (
            {"frequency": value, "distribution_policy": "distributing"}
            if value in {"monthly", "quarterly", "semiannual", "annual"}
            else {}
        )
        if code in result:
            raise ValueError("聯博 ETF 證券代碼重複")
        result[code] = {
            "source_url": url,
            "distribution_frequency_raw": raw,
            **policy_value,
        }
    if not result:
        raise ValueError("聯博 ETF 名單為空")
    return result


JPM_API = "https://am.jpmorgan.com/FundsMarketingHandler/"
JPM_PARAMS = {
    "country": "tw",
    "role": "twetf",
    "language": "zh",
    "userLoggedIn": "false",
}


def fetch_jpm_catalog(client):
    response = client.get(JPM_API + "autocomplete", params=JPM_PARAMS)
    response.raise_for_status()
    result = {}
    for row in response.json():
        code = row.get("ticker", "")
        if row.get("fundType") != "etf" or not re.fullmatch(r"[0-9]{4,6}[A-Z]?", code):
            continue
        isin = row.get("cusip", "")
        if not re.fullmatch(r"TW000[0-9A-Z]{7}", isin) or isin[5:-1] != code:
            raise ValueError("摩根 ETF 證券代碼與 ISIN 不符")
        if code in result:
            raise ValueError("摩根 ETF 證券代碼重複")
        result[code] = isin
    if not result:
        raise ValueError("摩根 ETF 名單為空")
    return result


def fetch_jpm(client, code, catalog):
    cusip = listed(catalog, code)
    response = client.get(
        JPM_API + "product-data", params={**JPM_PARAMS, "cusip": cusip}
    )
    response.raise_for_status()
    share = response.json()["fundData"]["shareClass"]
    if share.get("cusip") != catalog[code] or share.get("exchangeTicker") != code:
        raise ValueError("摩根 ETF 商品回傳代碼不符")
    raw = share.get("distributionFrequency")
    value = (raw or "").lower()
    policy_value = (
        {"frequency": value, "distribution_policy": "distributing"}
        if value in {"monthly", "quarterly", "semiannual", "annual"}
        else {}
    )
    return {
        "source_url": str(response.url),
        "distribution_frequency_raw": raw,
        "listed_date": share.get("listingDate"),
        **policy_value,
    }


ALLIANZ_API = "https://etf.allianzgi.com.tw/webapi/api/"


def fetch_allianz_catalog(client):
    # Normal public-site antiforgery handshake; keep the cookie on this client
    # and send its opaque token only to this issuer, never in global headers.
    response = client.get(ALLIANZ_API + "AntiForgery/GetAntiForgeryToken")
    response.raise_for_status()
    token = response.json()["token"]
    response = client.post(
        ALLIANZ_API + "Fund/GetFundOverview",
        json={
            "Keyword": "",
            "FundNo": "",
            "FundType": -1,
            "PageSize": 999,
            "PageIndex": 1,
        },
        headers={"X-XSRF-TOKEN": token},
    )
    response.raise_for_status()
    body = response.json()
    if len(body["Entries"]) != body["TotalItems"]:
        raise ValueError("安聯 ETF 商品名單未完整取得")
    result = {}
    for row in body["Entries"]:
        code = row.get("CSecuritiesCode", "")
        if not re.fullmatch(r"[0-9]{4,6}[A-Z]?", code):
            continue
        if code in result:
            raise ValueError("安聯 ETF 代碼重複")
        result[code] = {"fund_no": row["CFundNo"], "verification": token}
    if not result:
        raise ValueError("安聯 ETF 商品名單為空")
    return result


def fetch_allianz(client, code, catalog):
    entry = listed(catalog, code)
    response = client.post(
        ALLIANZ_API + "Fund/GetFundDetail",
        json={"FundNo": entry["fund_no"], "IsPreview": False},
        headers={"X-XSRF-TOKEN": entry["verification"]},
    )
    response.raise_for_status()
    data = response.json()["Entries"]
    if data.get("CSecuritiesCode") != code or data.get("CFundNo") != entry["fund_no"]:
        raise ValueError("安聯 ETF 證券代碼或基金編號不符")
    raw = data.get("CCgDividendFrequencyStr") or data.get("CCgDividendTypeStr")
    return {
        "source_url": "https://etf.allianzgi.com.tw/etf-info/"
        + entry["fund_no"]
        + "?tab=1",
        "custodian_bank": data.get("CCgCustodyBankStr"),
        "distribution_frequency_raw": raw,
        **policy(raw),
    }
