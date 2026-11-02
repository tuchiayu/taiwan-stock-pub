"""TWSE OpenAPI adapter for the securities catalog.

Design 002 §4.7: `securities` and `etf_profiles` are synced in full
from official sources (not fetched per query) because combined ETF
filtering (custodian + distribution frequency) needs the whole catalog
available locally.

Verified live against the real endpoints while writing this module
(2026-09): `t187ap03_L` ("上市公司基本資料") does NOT include ETFs —
0050/0056 are absent — so stocks and ETFs need two separate TWSE
datasets. `t187ap47_L` ("基金基本資料彙總表") covers ETFs and has
`保管機構`, but design 004 §4.3.1 found this raw value is frequently
the central depository (集保) or the issuing 投信 itself rather than
the fund's real custodian bank — cross-checking 6 known-correct
values against this field found it wrong in every case where it read
集保. It therefore only maps to `etf_profiles.custodian_institution_raw`
(reference only, never displayed as the custodian bank); the real
`custodian_bank` is left `None` here and is owned by the not-yet-built
per-投信 adapters and user overrides (design 004 Phase E). `t187ap47_L`
also has no distribution-frequency or fee field at all — `normalize_etf`
leaves those `None` rather than guessing; see design 002 §5.2 and
design 004 §4.4.1.

TPEx (OTC) has its own adapter — see `tpex_adapter.py` — kept separate
rather than merged in here because its endpoints use different field
key names for the same data (English keys vs TWSE's Chinese keys).

Also verified live (2026-09): `/exchangeReport/TWT48U_ALL` ("上市股票
除權除息預告表") gives per-security ex-dividend date, cash dividend
per share, and stock-dividend ratio directly — real, currently
populated (79 rows the day this was checked, including a genuine
upcoming TSMC cash dividend). This is an advance-notice table, not a
full history: it only covers near-term upcoming events, so it can
seed `distributions`/`corporate_actions` going forward but cannot
backfill a trailing-12-month history by itself (design 002 §5.2).
Source amounts carry floating-point noise (e.g. "7.000001" for what
should be a clean 7, "0.05000001" for 0.05) — `normalize_ex_dividend_notice`
quantizes cash amounts to 4 decimal places, but `StockDividendRatio`
to 8 (`_parsing.RATIO_QUANTUM`) so it doesn't lose genuine sub-decimal
precision seen in that field (e.g. "0.221074"; TPEx sees
"0.01993215") — a shared 4-decimal quantum would silently truncate
those to "0.2211"/"0.0199" and skew the derived split ratio.
Rows that are pure cash-subscription rights offerings
(only `SubscriptionRatio` populated, no cash/stock dividend) aren't
modeled by this app's `corporate_actions.action_type` yet and are
skipped rather than guessed at.
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
from taiwan_stock.catalog._parsing import table_rows as _table_rows

MARKET = "TWSE"

BASE_URL = "https://openapi.twse.com.tw/v1"
STOCK_BASIC_INFO_PATH = "/opendata/t187ap03_L"
ETF_BASIC_INFO_PATH = "/opendata/t187ap47_L"
EX_DIVIDEND_NOTICE_PATH = "/exchangeReport/TWT48U_ALL"
DAILY_PRICES_PATH = "/exchangeReport/STOCK_DAY_ALL"


def fetch_stock_basic_info(client: httpx2.Client) -> list[dict[str, Any]]:
    response = client.get(f"{BASE_URL}{STOCK_BASIC_INFO_PATH}")
    response.raise_for_status()
    return response.json()


def fetch_etf_basic_info(client: httpx2.Client) -> list[dict[str, Any]]:
    response = client.get(f"{BASE_URL}{ETF_BASIC_INFO_PATH}")
    response.raise_for_status()
    return response.json()


def normalize_stock(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "market": "TWSE",
        "code": row["公司代號"],
        "name": _clean_text(row.get("公司簡稱")) or _clean_text(row["公司名稱"]),
        "security_type": "stock",
        # Raw TWSE industry code, not yet decoded to a display name —
        # decoding needs a verified code table (design 002 §5.2).
        "industry": _clean_text(row.get("產業別")),
        "transfer_agent_name": _clean_text(row.get("股票過戶機構")),
        "transfer_agent_phone": _clean_text(row.get("過戶電話")),
        "status": "active",
        # Confirmed live 2026-09-13 against 2330 (listed 1994-09-05):
        # `t187ap03_L`'s `上市日期` is plain Gregorian ("19940905"), unlike
        # `t187ap47_L`'s ROC-calendar field of the same name below.
        "listed_date": optional_date(_gregorian_yyyymmdd_to_iso, row.get("上市日期")),
    }


def normalize_etf(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    security = {
        "market": "TWSE",
        "code": row["基金代號"],
        "name": _clean_text(row.get("基金簡稱")) or _clean_text(row["基金中文名稱"]),
        "security_type": "etf",
        "industry": None,
        "transfer_agent_name": None,
        "transfer_agent_phone": None,
        "status": "active",
        # ROC-calendar here — see `normalize_stock`'s `listed_date` for
        # why that one needs a different parser for the same field name.
        "listed_date": optional_date(_roc_date_to_iso, row.get("上市日期")),
    }
    profile = {
        # Reference only — design 004 §4.3.1 found this raw value is
        # often 集保 or the 投信 itself, not the real custodian bank.
        "custodian_institution_raw": _clean_text(row.get("保管機構")),
        # Owned by the per-投信 adapters / user override, not this sync
        # (design 004 §4.5.1) — never set from this raw field.
        "custodian_bank": None,
        # Not present in t187ap47_L; needs another verified source
        # before this can be filled in (design 002 §5.2).
        "distribution_frequency": None,
        "fee_summary": None,
        "benchmark": _clean_text(row.get("標的指數/追蹤指數名稱")),
        # Fund identity for pairing foreign-currency share classes with
        # their TWD class (design 043); never a custodian or policy source.
        "fund_tax_id": _clean_text(row.get("基金統一編號")),
        "fund_type": _clean_text(row.get("基金類型")),
    }
    return security, profile


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
    ex_date = _roc_date_to_iso(row["Date"])
    code = row["Code"]

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
    code = row.get("Code")
    if not raw_date or not code:
        return None
    trade_date = _roc_date_to_iso(str(raw_date))
    close = _clean_price(row.get("ClosingPrice"))
    if close is None:
        return None
    volume = _clean_volume(row.get("TradeVolume"))
    return {
        "code": str(code),
        "trade_date": trade_date,
        "close": close,
        "volume": volume,
    }


# Design 013 §4.2: the website's 每日收盤行情 report, unlike
# `STOCK_DAY_ALL` (latest day only, and verified live 2026-09-29 to lag:
# still on 9/24 while this already had 9/29), takes a date. Same
# underlying data — 9/24 matched `STOCK_DAY_ALL` row for row (1380).
DAILY_QUOTES_BY_DATE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
_DAILY_QUOTES_COLUMNS = {"code": "證券代號", "close": "收盤價", "volume": "成交股數"}


def fetch_daily_prices_on(
    client: httpx2.Client, trade_date: str
) -> list[dict[str, Any] | None] | None:
    """Whole-market closes for one ISO date, normalized like
    `normalize_daily_price` (`None` entries are rows without a close).
    Returns `None` when the date has no trading data — a holiday, an
    unscheduled closure, or today's close not published yet: the report
    answers those with a non-"OK" `stat` ("很抱歉，沒有符合條件的資料!")."""
    yyyymmdd = trade_date.replace("-", "")
    response = client.get(
        DAILY_QUOTES_BY_DATE_URL,
        params={"date": yyyymmdd, "type": "ALLBUT0999", "response": "json"},
    )
    response.raise_for_status()
    payload = _json_object(response)
    if payload.get("stat") != "OK":
        return None
    if payload.get("date") != yyyymmdd:
        raise QuoteFormatError(f"要求 {yyyymmdd}，來源回傳 {payload.get('date')}")
    table = next(
        (
            t
            for t in payload.get("tables") or []
            if "每日收盤行情" in (t.get("title") or "")
        ),
        None,
    )
    if table is None:
        raise QuoteFormatError("找不到每日收盤行情表")
    rows = _table_rows(table, _DAILY_QUOTES_COLUMNS)
    if not rows:
        return None
    return [_normalize_quote_row(row, trade_date) for row in rows]


def _normalize_quote_row(row: dict[str, Any], trade_date: str) -> dict[str, Any] | None:
    return normalize_dated_quote(row, trade_date)


# Design 014 §4.2: 除權除息計算結果表 — events that actually went
# ex-dividend, queryable by date range (verified live 2026-09-29 back to
# 2018), unlike the advance-notice table above which drops an event once
# its date passes. Includes ETFs; callers filter by their own scope.
EX_RIGHTS_RESULT_URL = "https://www.twse.com.tw/rwd/zh/exRight/TWT49U"
EX_RIGHTS_DETAIL_URL = "https://www.twse.com.tw/rwd/zh/exRight/TWT49UDetail"
_EX_RIGHTS_COLUMNS = {
    "date": "資料日期",
    "code": "股票代號",
    "value": "權值+息值",
    "kind": "權/息",
}


def fetch_ex_rights_results(
    client: httpx2.Client, start: str, end: str
) -> list[dict[str, Any]]:
    """Ex-rights/ex-dividend results between two ISO dates, as
    `{"code", "ex_dividend_date", "cash", "needs_detail"}`. For a
    cash-only ("息") row `權值+息值` is the cash dividend itself; a
    combined "權息" row mixes in the stock dividend's value, so its cash
    part has to come from `fetch_ex_rights_cash_detail` instead; a
    stock-only ("權") row has no cash. A range with no events answers a
    non-"OK" `stat`, treated as empty."""
    response = client.get(
        EX_RIGHTS_RESULT_URL,
        params={
            "startDate": start.replace("-", ""),
            "endDate": end.replace("-", ""),
            "response": "json",
        },
    )
    response.raise_for_status()
    payload = _json_object(response)
    if payload.get("stat") != "OK":
        return []
    results = []
    for row in _table_rows(payload, _EX_RIGHTS_COLUMNS):
        ex_date = _roc_dotted_date_to_iso(row["date"])
        if ex_date is None or not start <= ex_date <= end:
            raise QuoteFormatError(
                f"除權息日期 {row['date']} 不在要求區間 {start}～{end}"
            )
        kind = str(row["kind"]).strip()
        results.append(
            {
                "code": str(row["code"]).strip(),
                "ex_dividend_date": ex_date,
                "cash": (
                    _clean_amount(str(row["value"]), quantum=_AMOUNT_QUANTUM)
                    if kind == "息"
                    else None
                ),
                "needs_detail": kind == "權息",
            }
        )
    return results


def fetch_ex_rights_cash_detail(
    client: httpx2.Client, code: str, ex_date: str
) -> Decimal | None:
    """The cash part of one combined "權息" event, from its detail page
    ("0.27 元／股")."""
    response = client.get(
        EX_RIGHTS_DETAIL_URL,
        params={"STK_NO": code, "T1": ex_date.replace("-", ""), "response": "json"},
    )
    response.raise_for_status()
    rows = _table_rows(response.json(), {"cash": "(每股配發現金股利)除息"})
    if not rows:
        return None
    number = str(rows[0]["cash"]).split("元")[0].strip()
    return _clean_amount(number, quantum=_AMOUNT_QUANTUM)


HOLIDAY_SCHEDULE_PATH = "/holidaySchedule/holidaySchedule"


def fetch_holidays(client: httpx2.Client) -> set[str]:
    """ISO dates the market is closed, per TWSE's schedule (current year
    only). TPEx closes on exactly the same days, so both markets share
    this. Some entries are reminders about days that *do* trade —
    "國曆新年開始交易日", "農曆春節前最後交易日" — and are excluded;
    "市場無交易，僅辦理結算交割作業" is a genuine closure and kept."""
    response = client.get(f"{BASE_URL}{HOLIDAY_SCHEDULE_PATH}")
    response.raise_for_status()
    return {
        _roc_date_to_iso(str(row["Date"]))
        for row in response.json()
        if row.get("Date")
        and "開始交易" not in (row.get("Name") or "")
        and "最後交易" not in (row.get("Name") or "")
    }


# Design 004 §4.2: this is the TWSE *website's* own ETF distribution
# endpoint (`www.twse.com.tw`), not one of the documented OpenAPI
# datasets (`openapi.twse.com.tw`) the rest of this module uses — the
# ETF basic-info dataset has no distribution-frequency/history field at
# all, and the stock ex-dividend notice table doesn't cover ETFs, so
# this is the only verified source for ETF distribution dates/amounts.
# Verified live 2026-09: response uses `"status":"ok"` (not `"stat"`),
# an empty `data: []` for a code/year with nothing announced (not an
# error), and dates like `"115年08月18日"` — a different ROC format
# from `roc_date_to_iso`'s digit-only one, hence `roc_dotted_date_to_iso`.
ETF_DISTRIBUTION_URL = "https://www.twse.com.tw/rwd/zh/ETF/etfDiv"

_ETF_DISTRIBUTION_FIELD_NAMES = {
    "code": "證券代號",
    "ex_dividend_date": "除息交易日",
    "record_date": "收益分配基準日",
    "pay_date": "收益分配發放日",
    "amount_per_share": "收益分配金額 (每1受益權益單位)",
}


def fetch_etf_distributions(
    client: httpx2.Client, code: str, start_year: int, end_year: int
) -> dict[str, Any]:
    response = client.get(
        ETF_DISTRIBUTION_URL,
        params={
            "stkNo": code,
            "startDate": start_year,
            "endDate": end_year,
            "response": "json",
        },
    )
    response.raise_for_status()
    return response.json()


def normalize_etf_distributions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Indexes columns by the response's own `fields` names rather than
    a fixed order (design 004 §4.2) — returns `[]` if the expected
    field names aren't all present, rather than raising or guessing at
    column positions, so an unannounced format change fails safe.
    """
    if not isinstance(payload, dict):
        raise ValueError("TWSE 配息回應不是物件")
    fields = payload.get("fields") or []
    index = {name: position for position, name in enumerate(fields)}
    if not all(name in index for name in _ETF_DISTRIBUTION_FIELD_NAMES.values()):
        raise ValueError("TWSE 配息回應缺少必要欄位")
    if not isinstance(payload.get("data"), list):
        raise ValueError("TWSE 配息回應缺少事件陣列")

    def _field(row: list[Any], key: str) -> Any:
        return row[index[_ETF_DISTRIBUTION_FIELD_NAMES[key]]]

    events = []
    for row in payload.get("data", []):
        raw_ex_date = _field(row, "ex_dividend_date")
        ex_dividend_date = _roc_dotted_date_to_iso(raw_ex_date)
        if ex_dividend_date is None and _clean_text(raw_ex_date):
            # Design 062: text that isn't a date is a format change, not the
            # blank no-distribution sentinel below.
            raise ValueError("TWSE 配息除息日格式不符")
        if ex_dividend_date is None:
            # Confirmed live 2026-09-13: for a year with no
            # distribution, TWSE returns one sentinel row here (blank
            # date fields, amount literally "無收益分配", status
            # "(尚無資料)") instead of an empty `data` array — not a
            # malformed event, so it must not become one; skip it the
            # same as if `data` had been `[]`.
            continue
        # This fixed-width field is right-padded with spaces (e.g.
        # "00400A    ") — comparing it unstripped against the requested
        # code always "mismatches", even for a genuine same-code event.
        code = _clean_text(_field(row, "code"))
        amount = _clean_amount(
            _field(row, "amount_per_share") or "", quantum=_AMOUNT_QUANTUM
        )
        events.append(
            {
                "code": code,
                "ex_dividend_date": ex_dividend_date,
                "record_date": _roc_dotted_date_to_iso(_field(row, "record_date")),
                "pay_date": _roc_dotted_date_to_iso(_field(row, "pay_date")),
                "amount_per_share": str(amount) if amount is not None else None,
                # A blank/null amount means "announced date, amount not
                # yet decided" (design 004 §2.2), not zero — never
                # `"final"` without a real number.
                "amount_status": "final" if amount is not None else "unknown",
            }
        )
    return events
