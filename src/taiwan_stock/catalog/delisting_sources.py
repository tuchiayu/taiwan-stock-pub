"""Official termination lists and ETF notices (verified 2026-09-16).

Listing-category notices also contain renames and unit replacements. Only
explicit trading termination dates with verified approval qualify as evidence.
Never infer termination from missing data or an HTTP response status.
"""

import re
from datetime import date
from html import unescape
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import httpx2

from taiwan_stock.shared import pacing

TWSE_STOCKS = "https://openapi.twse.com.tw/v1/company/suspendListingCsvAndHtml"
TPEX_STOCKS = "https://www.tpex.org.tw/www/zh-tw/company/deListed"
TWSE_ETFS = "https://www.twse.com.tw/zh/ETFortune/announcementList"
TPEX_ETFS = "https://info.tpex.org.tw/api/etfMaInfo"
TPEX_DETAIL = "https://info.tpex.org.tw/api/etfMaInfoDetail"

# Same TWSE host, same empirically-tuned baseline as the ETF distribution
# sync (sync.py's `_ETF_DISTRIBUTION_DELAY_SECONDS`): 0.3s pacing measured
# live (2026-09-16) got HTTP 428 within a handful of requests once a run
# made dozens of per-notice detail fetches in a row.
PAGE_DELAY_SECONDS = 2.0
_RETRY_BACKOFF_SECONDS = (0.5, 2.0)


def is_blocking_error(exc):
    """403/428/429/5xx and connection failures suggest the host itself is
    pushing back; a 404 means only this one notice/detail is missing, not
    that every other request to the same host will fail too."""
    if not isinstance(exc, httpx2.HTTPError):
        return False
    if isinstance(exc, httpx2.HTTPStatusError) and exc.response.status_code == 404:
        return False
    return True


def fetch_with_retry(fetch, *args, **kwargs):
    for delay in (*_RETRY_BACKOFF_SECONDS, None):
        try:
            return fetch(*args, **kwargs)
        except httpx2.HTTPError:
            if delay is None:
                raise
            pacing.pause(delay)


def _get(client, url, **kwargs):
    response = client.get(url, **kwargs)
    response.raise_for_status()
    return response


def _post(client, url, **kwargs):
    response = client.post(url, **kwargs)
    response.raise_for_status()
    return response


def text_only(value):
    return re.sub(r"\s+", "", unescape(re.sub(r"<[^>]*>", "", value)))


def official_date(value):
    match = re.fullmatch(r"(\d{2,4})[年/.-](\d{1,2})[月/.-](\d{1,2})日?", value)
    if not match:
        raise ValueError("終止交易日期格式不符")
    year, month, day = map(int, match.groups())
    return date(year + 1911 if year < 1911 else year, month, day).isoformat()


def event(market, kind, code, effective, url, *, last=None, reason=None):
    if not isinstance(code, str) or not re.fullmatch(r"[0-9A-Z]{4,6}", code):
        raise ValueError("終止交易商品代碼不符")
    return dict(
        market=market,
        security_type=kind,
        code=code,
        effective_date=effective,
        last_trade_date=last,
        reason=reason,
        source_url=url,
    )


def fetch_twse_stocks(client):
    response = fetch_with_retry(_get, client, TWSE_STOCKS)
    rows = response.json()
    if not isinstance(rows, list) or not rows:
        raise ValueError("TWSE 終止上市公司清單為空或格式不符")
    return [
        event(
            "TWSE",
            "stock",
            r["Code"].strip(),
            official_date(r["DelistingDate"].strip()),
            TWSE_STOCKS,
        )
        for r in rows
    ]


def fetch_tpex_stocks(client):
    result, seen, total = [], set(), None
    for _ in range(100):
        response = fetch_with_retry(
            _get,
            client,
            TPEX_STOCKS,
            params={
                "date": "ALL",
                "code": "",
                "reason": "-1",
                "response": "json",
                "paging-size": 100,
                "paging-offset": len(result),
            },
        )
        payload = response.json()
        if payload.get("stat") != "ok" or len(payload.get("tables", [])) != 1:
            raise ValueError("TPEx 終止上櫃公司清單格式不符")
        table = payload["tables"][0]
        fields = table["fields"]
        indices = [
            fields.index(f) for f in ("股票代號", "終止上櫃日期", "終止上櫃原因")
        ]
        count = table["totalCount"]
        if not isinstance(count, int) or count <= 0 or total not in (None, count):
            raise ValueError("TPEx 終止上櫃清單筆數不完整或查詢中異動")
        total = count
        if not table["data"]:
            raise ValueError("TPEx 終止上櫃清單缺頁")
        for row in table["data"]:
            code, when, reason = (row[i] for i in indices)
            # Live 2026-09-16 data has stray whitespace in some codes
            # (e.g. "1752 ", "\t3126") — the same product, not a format
            # error; stripping it here keeps `event()`'s code-shape
            # check from rejecting an otherwise-valid whole page.
            code, when = code.strip(), when.strip()
            key = (code, when)
            if key in seen:
                raise ValueError("TPEx 終止上櫃清單分頁重複")
            seen.add(key)
            result.append(
                event(
                    "TPEx",
                    "stock",
                    code,
                    official_date(when),
                    TPEX_STOCKS + "?date=ALL",
                    reason=reason,
                )
            )
        if len(result) == total:
            return result
        if len(result) > total:
            break
        pacing.pause(PAGE_DELAY_SECONDS)
    raise ValueError("TPEx 終止上櫃清單超出分頁限制")


class NoticeLinks(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.links, self.next_links = {}, []
        self.href, self.parts = None, []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.href = attrs.get("href")
            self.parts = []
            if "nextLink" in attrs.get("class", "").split():
                self.next_links.append(self.href)

    def handle_data(self, data):
        if self.href:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            if urlsplit(self.href).path == "/zh/ETFortune/announcement":
                self.links[self.href] = "".join(self.parts)
            self.href = None


def notice_params(query):
    params = parse_qs(query)
    required = ("company", "date", "seq", "fund", "type")
    if any(len(params.get(k, [])) != 1 for k in required):
        raise ValueError("ETF 公告識別欄位不完整")
    result = {k: params[k][0] for k in required}
    if (
        result["type"] != "listing"
        or not re.fullmatch(r"\d{8}", result["date"])
        or not re.fullmatch(r"[0-9A-Z]{4,6}", result["fund"])
    ):
        raise ValueError("ETF 公告識別欄位格式不符")
    return result


def fetch_twse_notices(client):
    url = TWSE_ETFS + "?type=listing"
    result, pages, identities = [], set(), set()
    for _ in range(200):
        if url in pages:
            raise ValueError("TWSE ETF 公告分頁重複")
        pages.add(url)
        response = fetch_with_retry(_get, client, url)
        page = NoticeLinks(response.text)
        if not page.links or len(page.next_links) > 1:
            raise ValueError("TWSE ETF 公告清單為空或格式不符")
        for href, subject in page.links.items():
            params = notice_params(urlsplit(href).query)
            identity = tuple(params.items())
            if identity in identities:
                raise ValueError("TWSE ETF 公告分頁內容重複")
            identities.add(identity)
            result.append((params, subject))
        if not page.next_links:
            return result
        next_url = urljoin(TWSE_ETFS, page.next_links[0])
        parsed = urlsplit(next_url)
        if (
            parsed.netloc != "www.twse.com.tw"
            or parsed.path != urlsplit(TWSE_ETFS).path
        ):
            raise ValueError("TWSE ETF 公告分頁網址不符")
        url = next_url
        pacing.pause(PAGE_DELAY_SECONDS)
    raise ValueError("TWSE ETF 公告超出分頁限制")


def fetch_tpex_notices(client):
    response = fetch_with_retry(_post, client, TPEX_ETFS, data={"type": "listing"})
    rows = response.json()
    if not isinstance(rows, list) or not rows:
        raise ValueError("TPEx ETF 公告清單為空或格式不符")
    result, seen = [], set()
    for row in rows:
        params = notice_params(row["params"])
        key = tuple(params.items())
        if key in seen:
            raise ValueError("TPEx ETF 公告重複")
        seen.add(key)
        result.append((params, row["subject"]))
    return result


def termination_subject(subject):
    subject = text_only(subject)
    return bool(re.search(r"終止(?:上市|上櫃|櫃檯買賣)", subject))


class NoticeDatePending(ValueError):
    """An approval notice explicitly defers the trading date to another notice."""

    def __init__(self, url):
        super().__init__("前置公告，終止交易日期將另行公告，略過狀態更新")
        self.url = url


def parse_notice(market, params, subject, description, url):
    subject, body = text_only(subject), text_only(description)
    text = subject + "。" + body
    # Replacement of old units is not termination of the fund itself.
    if any(
        word in subject + body
        for word in ("分割", "反分割", "換發", "撤銷終止", "取消終止")
    ):
        raise ValueError("公告涉及換發或撤銷，終止狀態待人工確認")
    if re.search(
        r"(?:並非|並未|不會|不予|尚未|未曾|未取得|未獲准|未經)"
        r"[^。；;]{0,30}終止(?:在[臺台]灣上市|上市|上櫃|櫃檯買賣)",
        text,
    ):
        raise ValueError("公告未確認終止交易，狀態待人工確認")
    contract_termination = "信託契約終止" in body or "終止信託契約" in body
    # A cross-listed fund can leave Taiwan while remaining listed overseas.
    # Require both the requested product code and explicit TWSE approval.
    local_delisting = (
        market == "TWSE"
        and re.search(rf"(?<![0-9A-Z]){re.escape(params['fund'])}(?![0-9A-Z])", text)
        and re.search(r"(?:經|取得)臺灣證券交易所(?:核准|同意)", text)
    )
    if not contract_termination and not local_delisting:
        raise ValueError("ETF 公告缺少基金終止或在台終止掛牌依據")
    termination = (
        "終止(?:在[臺台]灣)?上市" if market == "TWSE" else "終止(?:上櫃|櫃檯買賣)"
    )
    date_re = r"\d{2,4}年\d{1,2}月\d{1,2}日"
    date_label = rf"{termination}(?:買賣)?(?:日期|日)(?:[（(]即下市日[）)])?[：:]"
    patterns = [
        rf"{date_label}({date_re})",
        rf"(?:自|訂於|將於|已於)({date_re})起?{termination}(?:買賣)?",
    ]
    dates = {
        official_date(d) for pattern in patterns for d in re.findall(pattern, body)
    }
    if (
        not dates
        and not re.search(date_label, body)
        and contract_termination
        and re.search(
            rf"有關本基金{termination}日[^。；;]*將於本基金獲准{termination}後[，,]?"
            r"再另行公告",
            body,
        )
    ):
        raise NoticeDatePending(url)
    if len(dates) != 1:
        raise ValueError("ETF 終止交易日期缺漏或互相矛盾，待確認")
    effective = dates.pop()
    last_dates = {
        official_date(d) for d in re.findall(rf"最後交易日[：:]({date_re})", body)
    }
    if len(last_dates) > 1 or any(d >= effective for d in last_dates):
        raise ValueError("ETF 最後交易日不符")
    return event(
        market,
        "etf",
        params["fund"],
        effective,
        url,
        last=next(iter(last_dates), None),
        reason=subject,
    )


def fetch_notice(client, market, params):
    if market == "TWSE":
        url = "https://www.twse.com.tw/zh/ETFortune/announcement?" + urlencode(params)
        response = fetch_with_retry(_get, client, url)
        cells = {}
        for label in ("主旨", "說明"):
            match = re.search(
                rf"<th[^>]*>\s*{label}\s*</th>\s*<td[^>]*>(.*?)</td>",
                response.text,
                re.S,
            )
            if not match:
                raise ValueError("TWSE ETF 公告內文格式不符")
            cells[label] = match.group(1)
        subject, body = cells["主旨"], cells["說明"]
    else:
        url = "https://info.tpex.org.tw/ETF/zh/announcement-detail.html?" + urlencode(
            params
        )
        response = fetch_with_retry(_post, client, TPEX_DETAIL, data=params)
        rows = response.json()
        if (
            not isinstance(rows, list)
            or len(rows) != 1
            or rows[0].get("stockNo") != params["fund"]
        ):
            raise ValueError("TPEx ETF 公告商品代碼不符")
        subject, body = rows[0]["subject"], rows[0]["description"]
    return parse_notice(market, params, subject, body, url)
