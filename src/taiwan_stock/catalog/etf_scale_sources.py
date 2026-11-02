"""SITCA monthly ETF size and TDCC weekly holder counts (design 044 §4.2.2)."""

import csv
import io
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin

from taiwan_stock.catalog.delisting_sources import fetch_with_retry
from taiwan_stock.catalog.profile_html import Tree, compact

SITCA_STATEMENT = "https://www.sitca.org.tw/ROC/SITCA_ETF/etf_statement.aspx"
TDCC_DISTRIBUTION = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
TDCC_HEADER = ["資料日期", "證券代號", "持股分級", "人數", "股數", "占集保庫存數比例%"]
# Level 17 is the total row; 1–15 are holding tiers and 16 an adjustment.
TDCC_TOTAL_LEVEL = "17"
CODE = re.compile(r"[0-9]{4,6}[A-Z]?")
ETF_PREFIX = "00"


def _get(client, url, **kwargs):
    response = client.get(url, follow_redirects=True, **kwargs)
    response.raise_for_status()
    return response


def amount(text):
    """Comma-grouped non-negative integer, or None for placeholders like '-'."""
    value = text.replace(",", "").strip()
    return int(value) if value.isdigit() else None


def _decimal(text):
    try:
        value = Decimal(text.replace(",", "").strip())
    except InvalidOperation:
        return None
    return str(value) if value.is_finite() and value > 0 else None


def fetch_sitca_sizes(client):
    """Returns `(period 'YYYY-MM', {code: row}, url, skipped)` for the latest month."""
    response = fetch_with_retry(_get, client, SITCA_STATEMENT)
    months = [
        n.attrs["value"]
        for n in Tree(response.text).root.nodes()
        if n.tag == "option" and re.fullmatch(r"\d{6}", n.attrs.get("value", ""))
    ]
    if not months:
        raise ValueError("公會 ETF 月報缺少報表月份")
    month = max(months)
    response = fetch_with_retry(
        _get,
        client,
        urljoin(SITCA_STATEMENT, "etf_statement2.aspx"),
        params={"txtYM": month, "txtR1": "0"},
    )
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
    wanted = {
        "code": "證券代號",
        "size": "基金規模(台幣)",
        "nav": "單位淨值(台幣)",
        "units": "受益權單位數",
    }
    header = next(
        (
            r
            for r in rows
            if {compact(v) for v in wanted.values()} <= set(map(compact, r))
        ),
        None,
    )
    if header is None:
        raise ValueError("公會 ETF 月報表格格式變更")
    index = {
        key: list(map(compact, header)).index(compact(v)) for key, v in wanted.items()
    }
    result, skipped = {}, 0
    for row in rows:
        if len(row) != len(header) or not CODE.fullmatch(code := row[index["code"]]):
            continue
        size = amount(row[index["size"]])
        if size is None:
            # A placeholder for one fund must not discard the whole month.
            skipped += 1
            continue
        value = {
            "net_assets_twd": size,
            "nav_twd": _decimal(row[index["nav"]]),
            "units": amount(row[index["units"]]),
        }
        if code in result and result[code] != value:
            raise ValueError("公會 ETF 月報代碼重複且數值不一致")
        result[code] = value
    if not result:
        raise ValueError("公會 ETF 月報為空")
    return f"{month[:4]}-{month[4:]}", result, str(response.url), skipped


def fetch_tdcc_holders(client):
    """Returns `(data date 'YYYY-MM-DD', {code: row}, url, skipped)`."""
    response = fetch_with_retry(_get, client, TDCC_DISTRIBUTION)
    reader = csv.reader(io.StringIO(response.content.decode("utf-8-sig")))
    if next(reader, None) != TDCC_HEADER:
        raise ValueError("集保股權分散表欄位格式變更")
    dates, result, skipped = set(), {}, 0
    for row in reader:
        if len(row) != len(TDCC_HEADER) or row[2].strip() != TDCC_TOTAL_LEVEL:
            continue
        # Live 2026-10-22 data pads codes with spaces ("0050  "). The file
        # covers every listed security; ETF codes all start with 00, so stocks
        # and warrants are not reported as unknown ETFs.
        code = row[1].strip()
        if not CODE.fullmatch(code) or not code.startswith(ETF_PREFIX):
            continue
        dates.add(row[0].strip())
        holders, units = amount(row[3]), amount(row[4])
        if holders is None or units is None:
            skipped += 1
            continue
        if code in result:
            raise ValueError("集保股權分散表代碼重複")
        result[code] = {"holders": holders, "units": units}
    if len(dates) != 1:
        raise ValueError("集保股權分散表資料日期不一致或為空")
    (day,) = dates
    if not re.fullmatch(r"\d{8}", day):
        raise ValueError("集保股權分散表資料日期格式不符")
    return f"{day[:4]}-{day[4:6]}-{day[6:]}", result, str(response.url), skipped
