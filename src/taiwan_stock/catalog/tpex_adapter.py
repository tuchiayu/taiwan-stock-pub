"""TPEx (Taipei Exchange / 上櫃) OpenAPI adapter for the securities catalog.

Verified live against the real endpoints while writing this module
(2026-09): `mopsfin_t187ap03_O` ("上櫃股票基本資料") is the TPEx
counterpart of TWSE's `t187ap03_L` — same information (incl. 股票過戶
機構), but keyed in English (`SecuritiesCompanyCode`, `CompanyName`,
`StockTransferAgent`, ...) rather than TWSE's Chinese keys. Confirmed
live: 891 rows, real transfer-agent data present.

Checked the full TPEx OpenAPI catalog (227 datasets) for an ETF
counterpart to TWSE's `t187ap47_L` and found none — no dataset tagged
or named for OTC-listed ETFs exists in the *formal* catalog. Design
004 §2.1 flags that this used to be (mis)read as "ETFs only list on
TWSE" — wrong: bond ETFs in particular trade over-the-counter via
TPEx rather than listing on TWSE. The verified source (design 004
§4.2) is TPEx's own website's autocomplete service, not the OpenAPI
catalog — see `fetch_etf_list` below. It only gives code+name, so
there is still no TPEx counterpart to `t187ap47_L`'s richer basic-info
fields (custodian institution, benchmark, ...); `normalize_etf_list_entry`
below fills `etf_profiles` with those left `None`, for Phase E's
per-投信 adapters to populate later.

Also verified live: `tpex_exright_prepost` ("上櫃股票除權除息預告表")
is the TPEx counterpart of TWSE's `TWT48U_ALL` — same semantics
(`CashDividend`, `StockDividendRatio` field names are identical to
TWSE's; only the date/code field names differ:
`ExRrightsExDividendDate`/`SecuritiesCompanyCode` vs TWSE's
`Date`/`Code`). Confirmed live: 103 rows, real upcoming ex-dividend
data. `StockDividendRatio` is a fraction (e.g. "0.01993215"), same
representation TWSE uses, so it's cleaned/converted the same way. A
number of rows in this table are bond ETFs (e.g. "00966B", codes like
"統一ESG投等債15+") rather than plain stocks; they're harmless here
because `sync_ex_dividend_notices` skips any code that isn't already
in the locally synced `securities` catalog rather than guessing at
it. Confirmed live (2026-09) that several of these bond-ETF rows carry
the placeholder text "尚未公告" ("not yet announced") in `CashDividend`
instead of a number — `_clean_amount` treats that the same as an
empty/missing amount rather than crashing.
"""

from decimal import Decimal
from typing import Any

import httpx2

from taiwan_stock.catalog._parsing import AMOUNT_QUANTUM as _AMOUNT_QUANTUM
from taiwan_stock.catalog._parsing import RATIO_QUANTUM as _RATIO_QUANTUM
from taiwan_stock.catalog._parsing import (
    QuoteFormatError,
    normalize_dated_quote,
    optional_date,
)
from taiwan_stock.catalog._parsing import clean_amount as _clean_amount
from taiwan_stock.catalog._parsing import clean_price as _clean_price
from taiwan_stock.catalog._parsing import clean_text as _clean_text
from taiwan_stock.catalog._parsing import clean_volume as _clean_volume
from taiwan_stock.catalog._parsing import (
    gregorian_yyyymmdd_to_iso as _gregorian_yyyymmdd_to_iso,
)
from taiwan_stock.catalog._parsing import json_object as _json_object
from taiwan_stock.catalog._parsing import roc_date_to_iso as _roc_date_to_iso
from taiwan_stock.catalog._parsing import (
    roc_dotted_date_to_iso as _roc_dotted_date_to_iso,
)
from taiwan_stock.catalog._parsing import (
    roc_slashed_date_to_iso as _roc_slashed_date_to_iso,
)
from taiwan_stock.catalog._parsing import table_rows as _table_rows

MARKET = "TPEx"

BASE_URL = "https://www.tpex.org.tw/openapi/v1"
STOCK_BASIC_INFO_PATH = "/mopsfin_t187ap03_O"
EX_DIVIDEND_NOTICE_PATH = "/tpex_exright_prepost"
DAILY_PRICES_PATH = "/tpex_mainboard_daily_close_quotes"

# Design 004 §4.2: TPEx's own website's ETF autocomplete service, not
# part of the formal OpenAPI catalog above (different host entirely —
# `info.tpex.org.tw`, the public site, not `www.tpex.org.tw/openapi`).
# Verified live 2026-09: a bare GET (no `query`, or any prefix every
# current code shares — all start with "00") returns all 119
# currently-listed OTC ETFs in one request; an unmatched `query`
# returns an empty list rather than an error, so this is a real
# prefix filter, not a fixed canned response.
ETF_LIST_URL = "https://info.tpex.org.tw/api/etf"

# Design 004 §4.2: also `info.tpex.org.tw`'s own endpoint (POST, not
# in the formal OpenAPI catalog). Verified live 2026-09: returns a
# bare JSON array (no `{status,fields,data}` envelope, unlike the TWSE
# ETF distribution list) with English field names; dates use the same
# "115年MM月DD日" dotted ROC format TWSE's ETF list uses — reuses
# `roc_dotted_date_to_iso` rather than a second copy of that parser.
# An undecided amount comes back as an empty string (`""`), not
# TWSE's JSON `null` — `clean_amount` already treats both as "no
# amount yet" so no extra handling is needed for the difference.
ETF_DISTRIBUTION_URL = "https://info.tpex.org.tw/api/etfExDiv"


def fetch_stock_basic_info(client: httpx2.Client) -> list[dict[str, Any]]:
    response = client.get(f"{BASE_URL}{STOCK_BASIC_INFO_PATH}")
    response.raise_for_status()
    return response.json()


def normalize_stock(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": MARKET,
        "code": row["SecuritiesCompanyCode"],
        "name": _clean_text(row.get("CompanyAbbreviation"))
        or _clean_text(row["CompanyName"]),
        "security_type": "stock",
        # Raw TPEx industry code, same "not yet decoded" caveat as the
        # TWSE adapter (design 002 §5.2).
        "industry": _clean_text(row.get("SecuritiesIndustryCode")),
        "transfer_agent_name": _clean_text(row.get("StockTransferAgent")),
        "transfer_agent_phone": _clean_text(row.get("StockTransferAgentTelephone")),
        "status": "active",
        "listed_date": optional_date(
            _gregorian_yyyymmdd_to_iso, row.get("DateOfListing")
        ),
    }


def fetch_ex_dividend_notices(client: httpx2.Client) -> list[dict[str, Any]]:
    response = client.get(f"{BASE_URL}{EX_DIVIDEND_NOTICE_PATH}")
    response.raise_for_status()
    return response.json()


def normalize_ex_dividend_notice(
    row: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Returns (distribution_fields, corporate_action_fields); either
    may be None. `code` is included in both so the caller can resolve
    the security_id — this function never touches the database.
    """
    ex_date = _roc_date_to_iso(row["ExRrightsExDividendDate"])
    code = row["SecuritiesCompanyCode"]

    distribution = None
    cash_amount = _clean_amount(row.get("CashDividend", ""), quantum=_AMOUNT_QUANTUM)
    if cash_amount is not None:
        distribution = {
            "code": code,
            "ex_dividend_date": ex_date,
            "amount_per_share": str(cash_amount),
            "amount_status": "final",
        }

    corporate_action = None
    stock_ratio = _clean_amount(
        row.get("StockDividendRatio", ""), quantum=_RATIO_QUANTUM
    )
    if stock_ratio is not None:
        denominator = 100_000_000  # matches _RATIO_QUANTUM's 8 decimal places
        numerator = int((Decimal(1) + stock_ratio) * denominator)
        corporate_action = {
            "code": code,
            "action_type": "stock_dividend",
            "ratio_numerator": numerator,
            "ratio_denominator": denominator,
            "ex_date": ex_date,
        }

    return distribution, corporate_action


def fetch_daily_prices(client: httpx2.Client) -> list[dict[str, Any]]:
    response = client.get(f"{BASE_URL}{DAILY_PRICES_PATH}")
    response.raise_for_status()
    return response.json()


def normalize_daily_price(row: dict[str, Any]) -> dict[str, Any] | None:
    raw_date = row.get("Date")
    code = row.get("SecuritiesCompanyCode")
    if not raw_date or not code:
        return None
    trade_date = _roc_date_to_iso(str(raw_date))
    close = _clean_price(row.get("Close"))
    if close is None:
        return None
    volume = _clean_volume(row.get("TradingShares"))
    return {
        "code": str(code),
        "trade_date": trade_date,
        "close": close,
        "volume": volume,
    }


# Design 013 §4.2: the website's 上櫃股票每日收盤行情 report, which takes
# a date (the OpenAPI above only ever has the latest day). Verified live
# 2026-09-29 against the alternatives: the legacy
# `stk_quote_result.php` silently ignores its date parameter and always
# answers with the latest day, and `/afterTrading/dailyQ` redirects to an
# error page — so the echoed table date is checked below rather than
# trusted, in case this one drifts the same way.
DAILY_QUOTES_BY_DATE_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/otc"
_DAILY_QUOTES_COLUMNS = {"code": "代號", "close": "收盤", "volume": "成交股數"}


def fetch_daily_prices_on(
    client: httpx2.Client, trade_date: str
) -> list[dict[str, Any] | None] | None:
    """TPEx counterpart of `twse_adapter.fetch_daily_prices_on`. A date
    without trading answers `stat: "ok"` with an empty table
    (`totalCount` 0) rather than TWSE's non-OK `stat`."""
    response = client.post(
        DAILY_QUOTES_BY_DATE_URL,
        data={"date": trade_date.replace("-", "/"), "type": "EW", "response": "json"},
    )
    response.raise_for_status()
    if response.status_code != 200:
        raise QuoteFormatError(f"非預期的回應狀態 {response.status_code}")
    payload = _json_object(response)
    tables = payload.get("tables") or []
    if str(payload.get("stat")).lower() != "ok" or not tables:
        raise QuoteFormatError(f"非預期的回應：stat={payload.get('stat')}")
    table = tables[0]
    rows = _table_rows(table, _DAILY_QUOTES_COLUMNS)
    if not rows:
        return None
    echoed = _roc_slashed_date_to_iso(table.get("date"))
    if echoed != trade_date:
        raise QuoteFormatError(f"要求 {trade_date}，來源回傳 {table.get('date')}")
    return [_normalize_quote_row(row, trade_date) for row in rows]


def _normalize_quote_row(row: dict[str, Any], trade_date: str) -> dict[str, Any] | None:
    return normalize_dated_quote(row, trade_date)


# Design 014 §4.2: TPEx's ex-rights/ex-dividend results, queryable by
# date range with its own 現金股利 column (so no per-event detail lookup,
# unlike TWSE). Verified live 2026-09-29 that it honours the range
# (June 2026 → only 115/06/01–115/06/30 rows); the echoed `date` is
# still checked since TPEx's legacy quote page silently ignored dates.
EX_RIGHTS_RESULT_URL = "https://www.tpex.org.tw/www/zh-tw/bulletin/exDailyQ"
_EX_RIGHTS_COLUMNS = {"date": "除權息日期", "code": "代號", "cash": "現金股利"}


def fetch_ex_rights_results(
    client: httpx2.Client, start: str, end: str
) -> list[dict[str, Any]]:
    """TPEx counterpart of `twse_adapter.fetch_ex_rights_results`, same
    shape; `needs_detail` is always False."""
    response = client.post(
        EX_RIGHTS_RESULT_URL,
        data={
            "startDate": start.replace("-", "/"),
            "endDate": end.replace("-", "/"),
            "response": "json",
        },
    )
    response.raise_for_status()
    payload = _json_object(response)
    expected = f"{start.replace('-', '')}~{end.replace('-', '')}"
    if payload.get("date") != expected:
        raise QuoteFormatError(f"要求 {expected}，來源回傳 {payload.get('date')}")
    tables = payload.get("tables") or []
    if not tables or not tables[0].get("data"):
        return []
    results = []
    for row in _table_rows(tables[0], _EX_RIGHTS_COLUMNS):
        ex_date = _roc_slashed_date_to_iso(row["date"])
        if ex_date is None or not start <= ex_date <= end:
            raise QuoteFormatError(
                f"除權息日期 {row['date']} 不在要求區間 {start}～{end}"
            )
        results.append(
            {
                "code": str(row["code"]).strip(),
                "ex_dividend_date": ex_date,
                "cash": _clean_amount(str(row["cash"]), quantum=_AMOUNT_QUANTUM),
                "needs_detail": False,
            }
        )
    return results


def fetch_etf_list(client: httpx2.Client) -> list[str]:
    response = client.get(ETF_LIST_URL, params={"lang": "zh-tw"})
    response.raise_for_status()
    return response.json().get("suggestions", [])


def normalize_etf_list_entry(entry: str) -> dict[str, Any] | None:
    """Each entry is one `"CODE NAME"` string (design 004 §4.2), split
    on the first space. Returns `None` for a malformed entry (no
    space, or a blank code/name) rather than guessing at it."""
    parts = entry.split(" ", 1)
    if len(parts) != 2:
        return None
    code, name = (part.strip() for part in parts)
    if not code or not name:
        return None
    return {
        "market": MARKET,
        "code": code,
        "name": name,
        "security_type": "etf",
        "industry": None,
        "transfer_agent_name": None,
        "transfer_agent_phone": None,
        "status": "active",
    }


def fetch_etf_distributions(
    client: httpx2.Client, code: str, start_date: str, end_date: str
) -> list[dict[str, Any]]:
    """`start_date`/`end_date` are `YYYYMMDD` strings — TPEx's own
    format for this endpoint (unlike the TWSE ETF distribution list,
    which takes a plain year)."""
    response = client.post(
        ETF_DISTRIBUTION_URL,
        data={
            "stkNo": code,
            "startDate": start_date,
            "endDate": end_date,
            "lang": "zh-tw",
        },
    )
    response.raise_for_status()
    return response.json()


def normalize_etf_distributions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events = []
    for row in rows:
        amount = _clean_amount(row.get("amount") or "", quantum=_AMOUNT_QUANTUM)
        events.append(
            {
                "code": row.get("stockNo"),
                "ex_dividend_date": _roc_dotted_date_to_iso(row.get("divDate")),
                "record_date": _roc_dotted_date_to_iso(row.get("inBaseDate")),
                "pay_date": _roc_dotted_date_to_iso(row.get("inDate")),
                "amount_per_share": str(amount) if amount is not None else None,
                # A blank amount means "announced date, amount not yet
                # decided" (design 004 §2.2), not zero.
                "amount_status": "final" if amount is not None else "unknown",
            }
        )
    return events
