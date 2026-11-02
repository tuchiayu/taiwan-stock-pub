"""Official issuer adapters for ETF bank and distribution policy.

Sources are public website interfaces and may change. Official issuer metadata
is preferred for routing; name prefixes are a fallback only (design 022).
Only labelled product fields establish a policy; historical counts never do.
"""

import html
import json
import re
from html.parser import HTMLParser
from typing import Any

import httpx2

from taiwan_stock.catalog import official_profile_adapters as official
from taiwan_stock.catalog._parsing import clean_text as _clean_text
from taiwan_stock.catalog.profile_html import Tree, field
from taiwan_stock.catalog.sync_history import SourceUnavailable, listed


class TrustAdapterError(Exception):
    """Raised when a page was fetched but did not have the expected
    shape — the caller must record this as a parse failure, distinct
    from "fetched fine, this fund has no custodian bank listed"
    (design 004 §5.2)."""


class ProductNotListed(KeyError):
    """The issuer's own catalog has no such code — a permanent miss recorded
    as "not listed", not a failure (issue #135). A KeyError, like a missing
    key in a prefetched issuer list."""


def _unescape_repeated(value: str, times: int = 5) -> str:
    for _ in range(times):
        unescaped = html.unescape(value)
        if unescaped == value:
            break
        value = unescaped
    return value


# --- 群益 (Capital) -----------------------------------------------------
# Server-rendered Angular app. The product-overview page also carries
# every current code→internal-id pair (needed because the detail page
# is addressed by that opaque id, not the stock code) — confirmed live
# 2026-09 to list 28 products in one request.

_CAPITAL_OVERVIEW_URL = "https://www.capitalfund.com.tw/etf/product/overview"
_CAPITAL_SCHEDULE_URL = "https://www.capitalfund.com.tw/etf/product/cross/schedule"
_CAPITAL_DETAIL_URL = "https://www.capitalfund.com.tw/etf/product/detail/{id}/basic"
_CAPITAL_LIST_ROW_RE = re.compile(
    r'href="/etf/product/detail/(\d+)/basic">([0-9A-Za-z]+)</a>'
)
_CAPITAL_CUSTODIAN_RE = re.compile(r"保管銀行</div>\s*<div[^>]*>\s*([^<]+?)\s*</div>")
_CAPITAL_FREQUENCY_RE = re.compile(r'profit-frequency">([^<]+)</h3>')
# The legacy field comes from 收益分配評價月份, which is an evaluation month,
# not an ex-dividend month. Keep it for provenance; never use it for the
# user-facing ex-dividend schedule.
_CAPITAL_MONTH_PATTERN_RE = re.compile(
    r"收益分配評價月份</div>\s*<div[^>]*>\s*([^<]+?)\s*</div>"
)


def _parse_month_pattern(raw: str) -> list[int] | None:
    """Parse legacy issuer evaluation months; fail closed on odd formats."""
    tokens = [token.strip().removesuffix("月") for token in raw.split("、")]
    months = []
    for token in tokens:
        if not token.isdigit():
            return None
        month = int(token)
        if not 1 <= month <= 12:
            return None
        months.append(month)
    return months or None


def fetch_capital_product_ids(client: httpx2.Client) -> dict[str, str]:
    response = client.get(_CAPITAL_OVERVIEW_URL)
    response.raise_for_status()
    pairs = _CAPITAL_LIST_ROW_RE.findall(response.text)
    return dict((code, id_) for id_, code in pairs)


def fetch_capital_ex_dividend_schedule(client: httpx2.Client) -> dict[str, Any]:
    """Read Capital's grouped ETF ex-dividend schedule, never evaluation months."""
    response = client.get(_CAPITAL_SCHEDULE_URL)
    response.raise_for_status()
    cards = [
        node
        for node in Tree(response.text).root.nodes()
        if node.has_class("schedule-main-section-card")
    ]
    if not cards:
        raise TrustAdapterError("群益除息時程頁未找到時程群組")
    frequencies = {"季配": "quarterly", "半年配": "semiannual", "月配": "monthly"}
    expected = {"quarterly": 4, "semiannual": 2, "monthly": 12}
    products = {}
    for card in cards:
        labels = [
            node.text()
            for node in card.nodes()
            if node.has_class("schedule-main-section-card-header-detail-right")
        ]
        if len(labels) != 1 or labels[0] not in frequencies:
            raise TrustAdapterError("群益除息時程頻率無法辨識")
        frequency = frequencies[labels[0]]
        tokens = [
            node.text()
            for node in card.nodes()
            if node.has_class("schedule-main-section-card-header-months-item")
        ]
        if frequency == "monthly" and tokens == ["每月"]:
            months = list(range(1, 13))
        else:
            matches = [re.fullmatch(r"(\d{1,2})\s*月", token) for token in tokens]
            if not matches:
                raise TrustAdapterError("群益除息時程缺少月份")
            months = [int(match.group(1)) for match in matches if match]
            if len(months) != len(tokens):
                raise TrustAdapterError("群益除息時程月份格式不符")
        if (
            len(months) != expected[frequency]
            or len(set(months)) != len(months)
            or any(not 1 <= month <= 12 for month in months)
            or (
                frequency == "quarterly"
                and any((months[(i + 1) % 4] - months[i]) % 12 != 3 for i in range(4))
            )
            or (frequency == "semiannual" and (months[1] - months[0]) % 12 != 6)
        ):
            raise TrustAdapterError("群益除息時程月份與頻率不符")
        candidates = [
            node
            for node in card.nodes()
            if node.tag == "a"
            and node.attrs.get("href", "").startswith("/etf/product/detail/")
        ]
        if not candidates or any(
            not re.fullmatch(
                r"/etf/product/detail/\d+/basic", node.attrs.get("href", "")
            )
            for node in candidates
        ):
            raise TrustAdapterError("群益除息時程群組沒有商品")
        for link in candidates:
            match = re.match(r"([0-9]{4,6}[A-Z]?)(?:\s|$)", link.text())
            if not match or match.group(1) in products:
                raise TrustAdapterError("群益除息時程商品代碼缺失或重複")
            products[match.group(1)] = {"frequency": frequency, "months": months}
    return {"source_url": str(response.url), "products": products}


def fetch_capital_custodian_and_frequency(
    client: httpx2.Client, internal_id: str
) -> dict[str, Any]:
    response = client.get(_CAPITAL_DETAIL_URL.format(id=internal_id))
    response.raise_for_status()
    text = response.text
    custodian_match = _CAPITAL_CUSTODIAN_RE.search(text)
    frequency_match = _CAPITAL_FREQUENCY_RE.search(text)
    raw = (
        _clean_text(frequency_match.group(1))
        if frequency_match
        else field(Tree(text).root, "配息頻率")
    )
    if custodian_match is None and raw is None:
        raise TrustAdapterError("群益商品頁未找到保管銀行／頻率欄位")
    month_pattern_match = _CAPITAL_MONTH_PATTERN_RE.search(text)
    return {
        "source_url": str(response.url),
        "custodian_bank": _clean_text(custodian_match.group(1))
        if custodian_match
        else None,
        "distribution_frequency_raw": (raw),
        "distribution_month_pattern": (
            _parse_month_pattern(_clean_text(month_pattern_match.group(1)))
            if month_pattern_match
            else None
        ),
    }


# --- 國泰 (Cathay) -------------------------------------------------------
# The redesigned site (/ETF/detail/{code}) is an Angular SPA whose data
# comes from a public JSON API addressed by an internal fund code; the old
# server-rendered /proj/SPOCathayETF/{code}/ pages now 404 for most codes
# (confirmed live 2026-10). The list maps every current stock code to that
# fund code; the introduce payload carries the labelled bank and policy.

_CATHAY_API = "https://cwapi.cathaysite.com.tw/api/ETF/"
_CATHAY_PAGE_SIZE = 100


def _cathay_get(client: httpx2.Client, path: str, **params) -> tuple[Any, str]:
    response = client.get(_CATHAY_API + path, params=params)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("returnCode") != "2000":
        raise TrustAdapterError(f"國泰 API {path} 未回傳有效資料")
    return payload, str(response.url)


def fetch_cathay_fund_codes(client: httpx2.Client) -> dict[str, str]:
    payload, _ = _cathay_get(
        client,
        "GetETFList",
        FundType="",
        Keyword="",
        CurrentPage=1,
        PerPageCount=_CATHAY_PAGE_SIZE,
    )
    rows = payload.get("result")
    if not isinstance(rows, list):
        raise TrustAdapterError("國泰 ETF 清單格式不符")
    if payload.get("totalCount", 0) > len(rows):
        raise TrustAdapterError("國泰 ETF 清單超過單頁筆數，名單不完整")
    return {
        row["stockCode"]: row["fundCode"]
        for row in rows
        if isinstance(row, dict) and row.get("stockCode") and row.get("fundCode")
    }


def fetch_cathay_custodian_and_frequency(
    client: httpx2.Client, fund_code: str
) -> dict[str, Any]:
    payload, url = _cathay_get(client, "GetETFIntroduce", fundCode=fund_code)
    result = payload.get("result")
    if not isinstance(result, dict):
        raise TrustAdapterError("國泰商品介紹格式不符")
    bank = _clean_text(result.get("bank") or "") or None
    raw = _clean_text(result.get("allotType") or "") or None
    if bank is None and raw is None:
        raise TrustAdapterError("國泰商品介紹未找到保管銀行／頻率欄位")
    return {
        "source_url": url,
        "custodian_bank": bank,
        "distribution_frequency_raw": raw,
    }


# --- 中信 (CTBC) ----------------------------------------------------------
# The official ETF page uses an authenticated public JSON interface. Its
# short-lived token is issued to the website's own public client identifier;
# the list supplies CNO, while the detail supplies the labelled policy.
# The .com.tw host stopped accepting connections (2026-10); the same API and
# product pages are served from .com, the domain of the official ETF page.
_CTBC_SITE = "https://www.ctbcinvestments.com/"
_CTBC_API = _CTBC_SITE + "API/"
_CTBC_CLIENT = "www.ctbcinvestments.com"


def _ctbc_post(client: httpx2.Client, path: str, token: str, **body):
    response = client.post(
        _CTBC_API + path, params={"token": token}, json={"token": token, **body}
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("ResultCode") != 0 or not isinstance(payload.get("Data"), dict):
        raise TrustAdapterError(f"中信 API {path} 未回傳有效資料")
    return payload["Data"]


def fetch_ctbc_catalog(client: httpx2.Client) -> dict:
    token = _ctbc_post(client, "home/AuthToken", _CTBC_CLIENT).get("token")
    if not isinstance(token, str) or not token:
        raise TrustAdapterError("中信 API 未提供 token")
    entries = _ctbc_post(client, "etf/ETFList", token).get("Data")
    if not isinstance(entries, list) or not entries:
        raise TrustAdapterError("中信 ETF 清單為空")
    products = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise TrustAdapterError("中信 ETF 清單格式不符")
        code, cno = entry.get("ETF_ID"), entry.get("CNO")
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{4,6}[A-Z]?", code):
            raise TrustAdapterError("中信 ETF 清單代碼格式不符")
        if not isinstance(cno, str) or not cno:
            raise TrustAdapterError("中信 ETF 清單缺少商品編號")
        if code in products:
            raise TrustAdapterError("中信 ETF 清單代碼重複")
        products[code] = cno
    return {"token": token, "products": products}


def fetch_ctbc_custodian_and_frequency(
    client: httpx2.Client, code: str, catalog: dict
) -> dict[str, Any]:
    cno = listed(catalog["products"], code)
    detail = _ctbc_post(client, "etf/ETFDetail", catalog["token"], CNO=cno).get(
        "FundDetail"
    )
    if (
        not isinstance(detail, list)
        or len(detail) != 1
        or not isinstance(detail[0], dict)
    ):
        raise TrustAdapterError("中信 ETF 商品資料格式不符")
    entry = detail[0]
    if entry.get("ETF_ID") != code or entry.get("CNO") != catalog["products"][code]:
        raise TrustAdapterError("中信 ETF 商品代碼不符")
    raw_html = entry.get("DistriRate") or ""
    raw = Tree(raw_html).root.text() if isinstance(raw_html, str) else None
    bank = entry.get("PreserveBank")
    if not raw and not bank:
        raise TrustAdapterError("中信 ETF 商品頁未提供保管銀行／配息頻率")
    return {
        "source_url": f"{_CTBC_SITE}Etf/{catalog['products'][code]}",
        "custodian_bank": bank,
        "distribution_frequency_raw": raw,
    }


# --- 元大 (Yuanta) -------------------------------------------------------
# Nuxt/Vue SSR; addressed directly by stock code. No frequency field
# found on this page (design 004 §4.3.1) — only "收益分配：是／否".

_YUANTA_DETAIL_URL = "https://www.yuantaetfs.com/product/detail/{code}/"
_YUANTA_CUSTODIAN_RE = re.compile(
    r"保管銀行</div>\s*<div[^>]*fundData[^>]*>\s*([^<]+?)\s*</div>"
)


def fetch_yuanta_custodian(client: httpx2.Client, code: str) -> dict[str, Any]:
    response = client.get(_YUANTA_DETAIL_URL.format(code=code), follow_redirects=True)
    response.raise_for_status()
    # Observed 2026-10-31: every product page redirected here with HTTP 200.
    if response.url.path.startswith("/maintenance"):
        raise SourceUnavailable("元大網站維護中")
    match = _YUANTA_CUSTODIAN_RE.search(response.text)
    raw = field(Tree(response.text).root, "收益分配")
    extra = {"distribution_frequency_raw": raw, **official.policy(raw)} if raw else {}
    if match is None and not extra:
        raise TrustAdapterError("元大商品頁未找到保管銀行欄位")
    return {
        "source_url": str(response.url),
        "custodian_bank": _clean_text(match.group(1)) if match else None,
        **extra,
    }


# --- 富邦 (Fubon) --------------------------------------------------------
# Current product-policy heading wins over older trading information.

_FUBON_DETAIL_URL = "https://websys.fsit.com.tw/FubonETF/Fund/Profile.aspx?stkId={code}"
_FUBON_CUSTODIAN_RE = re.compile(r"<p>保管銀行</p>\s*<p>\s*([^<]+?)\s*</p>")


def fetch_fubon_custodian(client: httpx2.Client, code: str) -> dict[str, Any]:
    response = client.get(_FUBON_DETAIL_URL.format(code=code))
    response.raise_for_status()
    match = _FUBON_CUSTODIAN_RE.search(response.text)
    root = Tree(response.text).root
    codes = [
        header_match.group(1)
        for node in root.nodes()
        if node.tag == "h5"
        and (header_match := re.match(r"([0-9]{4,6}[A-Z]?)\s*/", node.text()))
    ]
    if len(codes) == 1 and codes[0] != code:
        # Unknown codes fall back to another product's page (seen live:
        # 00625K shows 00717); never take that product's values.
        raise ProductNotListed(code)
    if codes != [code]:
        raise TrustAdapterError("富邦商品頁代碼不符或缺少商品標題")
    headings = [n for n in root.nodes() if n.tag == "h6" and n.text() == "收益分配"]
    try:
        raw = field(root, "收益分配", tags={"h6"}) if headings else None
        extra = (
            {"distribution_frequency_raw": raw, **official.policy(raw)} if raw else {}
        )
    except ValueError as exc:
        extra = {"field_errors": {"frequency": exc, "policy": exc}}
    if match is None and not extra:
        raise TrustAdapterError("富邦商品頁未找到保管銀行欄位")
    return {
        "source_url": str(response.url),
        "custodian_bank": _clean_text(match.group(1)) if match else None,
        **extra,
    }


# --- 第一金 (First Securities) -------------------------------------------
# Server-rendered ASP.NET; `ID=` accepts the stock code directly
# (confirmed live 2026-09), no separate internal-id lookup needed.

_FSITC_DETAIL_URL = "https://www.fsitc.com.tw/FundDetail.aspx?ID={code}"
_FSITC_CUSTODIAN_RE = re.compile(r"保管銀行</td><td[^>]*>([^<]+)</td>")
_FSITC_FREQUENCY_RE = re.compile(r"有無配息</td><td[^>]*>([^<]+)</td>")


class _FundCatalogParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if (
            tag == "input"
            and attributes.get("id") == "ContentPlaceHolder1_HiddenField_FundName"
        ):
            value = attributes.get("value", "")
            for _ in range(5):
                try:
                    self.rows = json.loads(value)
                    return
                except ValueError:
                    value = html.unescape(value)


def fetch_fsitc_custodian_and_frequency(
    client: httpx2.Client, code: str
) -> dict[str, Any]:
    response = client.get(_FSITC_DETAIL_URL.format(code=code), follow_redirects=True)
    response.raise_for_status()
    text = response.text
    custodian_match = _FSITC_CUSTODIAN_RE.search(text)
    if custodian_match is None:
        # The official page's selector navigates by cNo, NOT stock code/cID.
        # Decode its embedded catalog and require an exact stock-code match.
        parser = _FundCatalogParser()
        parser.feed(text)
        matches = [row for row in parser.rows if row.get("cETFStockCode") == code]
        if parser.rows and not matches:
            raise ProductNotListed(code)
        if len(matches) != 1 or not matches[0].get("cNo"):
            raise TrustAdapterError("第一金商品代碼未對應到唯一基金編號")
        response = client.get(
            _FSITC_DETAIL_URL.format(code=matches[0]["cNo"]), follow_redirects=True
        )
        response.raise_for_status()
        text = response.text
        custodian_match = _FSITC_CUSTODIAN_RE.search(text)
    if custodian_match is None:
        raise TrustAdapterError("第一金商品頁未找到保管銀行欄位")
    frequency_match = _FSITC_FREQUENCY_RE.search(text)
    return {
        "source_url": str(response.url),
        "custodian_bank": _clean_text(custodian_match.group(1))
        if custodian_match
        else None,
        "distribution_frequency_raw": _clean_text(frequency_match.group(1))
        if frequency_match
        else None,
    }


# --- 兆豐 (Mega) ----------------------------------------------------------
# Server-rendered ASP.NET; addressed by an opaque internal id — the
# product-index page carries every current code→id pair. Frequency is
# only in legal-disclaimer prose, deliberately not parsed here.

_MEGA_INDEX_URL = "https://www.megafunds.com.tw/MEGA/etf/index.aspx"
_MEGA_DETAIL_URL = "https://www.megafunds.com.tw/MEGA/etf/etf_product.aspx?id={id}"
# `id=` is an opaque internal id, not the stock code (confirmed live
# 2026-09-12: `id=00918` loads without error but every field is blank) —
# each carousel entry pairs the code with its id via `item-num` /
# `etf_product.aspx?id=`, in that order, not vice versa.
_MEGA_LIST_ROW_RE = re.compile(
    r'"item-num">([0-9A-Za-z]+)</div>\s*<a href="etf_product\.aspx\?id=(\d+)"'
)
_MEGA_CUSTODIAN_RE = re.compile(
    r'保管銀行</div>\s*<div class="etfin5-info">\s*([^<]+?)\s*</div>'
)


def fetch_mega_product_ids(client: httpx2.Client) -> dict[str, str]:
    response = client.get(_MEGA_INDEX_URL)
    response.raise_for_status()
    return dict(_MEGA_LIST_ROW_RE.findall(response.text))


def fetch_mega_custodian(client: httpx2.Client, internal_id: str) -> dict[str, Any]:
    response = client.get(_MEGA_DETAIL_URL.format(id=internal_id))
    response.raise_for_status()
    match = _MEGA_CUSTODIAN_RE.search(response.text)
    if match is None:
        raise TrustAdapterError("兆豐商品頁未找到保管銀行欄位")
    return {
        "source_url": str(response.url),
        "custodian_bank": _clean_text(match.group(1)),
    }


# --- 永豐 (SinoPac) -------------------------------------------------------
# Server-rendered ASP.NET. The one overview table doubles as the
# code→internal-id map *and* a clean structured frequency column (a
# bonus find, design 004 §4.3.1 originally reported no frequency field
# for this trust — that was true of the per-product detail page only).
# The detail page (addressed by the internal id from that table) is
# still needed for the custodian bank, which the overview table lacks.

_SINOPAC_OVERVIEW_URL = "https://sitc.sinopac.com/SinopacEtfs/Funds/FundInfo"
_SINOPAC_DETAIL_URL = "https://sitc.sinopac.com/newweb/sitcFund/page.do?stock_id={id}"
# Code and internal id come from the name-column link. The frequency
# column is its own `DividenEtf?fund={id}` link matched separately (not
# "whatever `<a>` follows the code row") because a fund that currently
# distributes nothing (confirmed live 2026-09-12: 00886 = "不分配") has
# no `<a>` in that cell at all — a single row-spanning regex silently
# pulled in the *next* row's name-link text as if it were this row's
# frequency when tried that way.
_SINOPAC_CODE_ID_RE = re.compile(
    r'<td>([0-9A-Za-z]+)</td>\s*<td>\s*<a href="[^"]*stock_id=([0-9A-Za-z]+)'
)
_SINOPAC_FREQUENCY_RE = re.compile(r'DividenEtf\?fund=([0-9A-Za-z]+)"[^>]*>([^<]+)</a>')
_SINOPAC_CUSTODIAN_RE = re.compile(
    r"<h4>保管銀行</h4>\s*<div class=\"fl_txt\">\s*<p>\s*([^<]+?)\s*</p>"
)


def fetch_sinopac_overview(client: httpx2.Client) -> dict[str, dict[str, str]]:
    """Returns `{code: {"internal_id": ..., "distribution_frequency_raw": ...}}`
    — `distribution_frequency_raw` is only present for a code whose
    internal id also appears in the frequency column (design 004
    §4.4.1: a genuine "does not distribute" is data, not a parse gap,
    but this module only extracts frequency *labels* like 年配/季配 —
    turning "不分配" into a `distribution_frequency` value is 4.4's
    classification work, not this adapter's)."""
    response = client.get(_SINOPAC_OVERVIEW_URL)
    response.raise_for_status()
    text = response.text
    frequency_by_id = dict(_SINOPAC_FREQUENCY_RE.findall(text))
    result = {}
    for code, internal_id in _SINOPAC_CODE_ID_RE.findall(text):
        entry = {"internal_id": internal_id}
        frequency = frequency_by_id.get(internal_id)
        if frequency is not None:
            entry["distribution_frequency_raw"] = _clean_text(frequency)
        result[code] = entry
    # Current desktop rows also include explicit "不分配" without a link.
    for table in Tree(text).root.nodes():
        if table.tag != "table":
            continue
        rows = [node for node in table.nodes() if node.tag == "tr"]
        header = next(
            (
                [n.text() for n in row.children if getattr(n, "tag", None) == "th"]
                for row in rows
                if any(getattr(n, "tag", None) == "th" for n in row.children)
            ),
            [],
        )
        if "證券代碼" not in header or "配息頻率" not in header:
            continue
        index = header.index("配息頻率")
        for row in rows:
            cells = [n.text() for n in row.children if getattr(n, "tag", None) == "td"]
            if len(cells) == len(header) and cells[0] in result:
                result[cells[0]]["distribution_frequency_raw"] = cells[index]
    return result


def fetch_sinopac_custodian(client: httpx2.Client, internal_id: str) -> dict[str, Any]:
    response = client.get(_SINOPAC_DETAIL_URL.format(id=internal_id))
    response.raise_for_status()
    match = _SINOPAC_CUSTODIAN_RE.search(response.text)
    if match is None:
        raise TrustAdapterError("永豐商品頁未找到保管銀行欄位")
    return {
        "source_url": str(response.url),
        "custodian_bank": _clean_text(match.group(1)),
    }


# --- 台新 (Taishin) -------------------------------------------------------
# Current labelled profile fields, with the explicit dividend table as fallback.

_TAISHIN_DETAIL_URL = "https://www.tsit.com.tw/ETF/Home/ETFSeriesDetail/{code}"


def fetch_taishin_custodian(client: httpx2.Client, code: str) -> dict[str, Any]:
    response = client.get(_TAISHIN_DETAIL_URL.format(code=code))
    response.raise_for_status()
    values = official.source_values(Tree(response.text).root, frequency_tags={"th"})
    if not values.get("custodian_bank") and not values.get("distribution_policy"):
        raise TrustAdapterError("台新商品頁未找到保管銀行／政策欄位")
    # Preserve the legacy bank-only contract when the current page lacks policy.
    if not values.get("distribution_frequency_raw") and not values.get("field_errors"):
        return {
            "source_url": str(response.url),
            "custodian_bank": values["custodian_bank"],
        }
    return {"source_url": str(response.url), **values}


# --- 統一 (Uni-President / ezmoney) ---------------------------------------
# Server-rendered ASP.NET; requires a session cookie from a prior GET
# on the same domain (design 004 §4.3.1 — not a CAPTCHA/anti-bot wall,
# a plain cookie handshake, so a `httpx2.Client` with cookie
# persistence across calls satisfies it automatically). The bank-index
# page carries every current stock-code→internal fundCode pair. The
# custodian value is embedded as HTML text inside a JSON string inside
# an HTML attribute (double/triple-escaped) — `_unescape_repeated`
# collapses that back to plain HTML; the literal two-character escape
# sequences `\n`/`\t` *within* that JSON string (not real whitespace
# bytes, since the browser's own JS — not this fetch — is what would
# normally turn them into real newlines) are then blanked out so the
# regex's `\s*` can match normally. Frequency is only in marketing
# copy, deliberately not parsed here.

_EZMONEY_WARMUP_URL = "https://www.ezmoney.com.tw/ETF/Ebill/ProfitDistributionBankIndex"
_EZMONEY_DETAIL_URL = "https://www.ezmoney.com.tw/ETF/Fund/Info"
_EZMONEY_LIST_ROW_RE = re.compile(
    r'href="/ETF/Fund/Info\?fundCode=([^"]+)">([0-9A-Za-z]+)'
)


def fetch_ezmoney_fund_codes(client: httpx2.Client) -> dict[str, str]:
    """Also performs the session-cookie warm-up GET (design 004
    §4.3.1) — a `httpx2.Client` instance persists the resulting cookie
    for the caller's subsequent `fetch_ezmoney_custodian` call on the
    same client."""
    client.get(_EZMONEY_WARMUP_URL)
    response = client.get(_EZMONEY_WARMUP_URL)
    response.raise_for_status()
    pairs = _EZMONEY_LIST_ROW_RE.findall(response.text)
    return dict((code, fund_code) for fund_code, code in pairs)


def fetch_ezmoney_custodian(client: httpx2.Client, fund_code: str) -> dict[str, Any]:
    response = client.get(_EZMONEY_DETAIL_URL, params={"fundCode": fund_code})
    response.raise_for_status()
    entries = []
    has_embedded = False
    for node in Tree(response.text).root.nodes():
        value = node.attrs.get("data-content")
        if not value:
            continue
        has_embedded = True
        for _ in range(5):
            try:
                data = json.loads(value)
                break
            except ValueError:
                value = html.unescape(value)
        else:
            continue
        if isinstance(data, dict) and data.get("sFundCode") == fund_code:
            entries.append(data)
    if has_embedded:
        if len(entries) != 1:
            raise TrustAdapterError("統一商品基本資料未對應到唯一基金代碼")
        data = entries[0]
        root = Tree(_unescape_repeated(data.get("mFundIntro", ""))).root
        stock_code = (data.get("sStockNo") or "").strip()
        table_code = field(root, "股票代號")
        if not stock_code or table_code != stock_code:
            raise TrustAdapterError("統一商品基本資料證券代碼不符")
        values = official.source_values(root)
        raw = values.get("distribution_frequency_raw")
        # This exact current product field begins with its policy label;
        # the rest describes evaluation/ex-dividend dates, not another policy.
        label = re.split(r"[，,；;。]", raw or "")[0].strip()
        values.update(official.policy(label))
        if not values.get("custodian_bank") and not values.get("distribution_policy"):
            raise TrustAdapterError("統一商品頁未找到銀行／配息政策欄位")
        return {"source_url": str(response.url), **values}
    # Compatibility with older server-rendered pages, restricted to one bank row.
    decoded = _unescape_repeated(response.text).replace("\\n", " ").replace("\\t", " ")
    idx = decoded.find("保管銀行")
    end = decoded.find("</td>", idx)
    if idx == -1 or end == -1:
        raise TrustAdapterError("統一商品頁未找到保管銀行欄位")
    bank = field(Tree("<th>" + decoded[idx : end + 5]).root, "保管銀行", tags={"th"})
    if not bank:
        raise TrustAdapterError("統一商品頁保管銀行欄位格式不符預期")
    return {"source_url": str(response.url), "custodian_bank": bank}


# --- Issuer routing -------------------------------------------------------
# See the module docstring — this is a name-prefix heuristic, not a
# verified issuer classification.

ISSUER_NAME_PREFIXES = (
    "群益",
    "國泰",
    "元大",
    "富邦",
    "第一金",
    "兆豐",
    "永豐",
    "台新",
    "統一",
    "野村",
    "復華",
    "中信",
    "凱基",
    "大華",
    "FT",
    "玉山",
    "街口",
    "華南永昌",
    "貝萊德",
    "聯邦",
    "摩根",
    "安聯",
    "聯博",
    "恒生",
    "柏瑞",
)


def guess_issuer(security_name: str) -> str | None:
    security_name = re.sub(r"^(主動|平衡|期)", "", security_name.strip(), count=1)
    for name, alias in (
        ("中國信託", "中信"),
        ("富蘭克林華美", "FT"),
        ("大華銀", "大華"),
        ("新光", "台新"),
        ("FB", "富邦"),
    ):
        if security_name.startswith(name):
            return alias
    for prefix in ISSUER_NAME_PREFIXES:
        if security_name.startswith(prefix):
            return prefix
    return None
