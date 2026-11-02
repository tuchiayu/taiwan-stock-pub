"""MOPS (公開資訊觀測站) adapter — stock cash-dividend pay dates.

Design 015: the 除權息公告 report (`ajax_t108sb27`) is the one official
source carrying both an event's 除息交易日 and its 現金股利發放日, for a
whole market and a whole *announcement* year per request. Verified live
2026-09-29 on `mopsov.twse.com.tw` (sii 115: 948 rows; 111: 958; 105:
739; otc 115: 690). The new `mops.twse.com.tw` answers the same path
with a security block page, and its own 除權息公告 page is itself just a
`api/redirectToOld` hop to this same `mopsov` endpoint.

The response is an HTML report, not JSON: two tables (companies with
and without a book-closure period), a two-level header, and 19 cells
per data row. Cells are read by position, so the header text and row
width are checked first — a layout change must stop the sync rather
than write dates into the wrong columns.
"""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any

import httpx2

from taiwan_stock.catalog._parsing import QuoteFormatError
from taiwan_stock.catalog._parsing import (
    roc_slashed_date_to_iso as _roc_slashed_date_to_iso,
)

EX_RIGHTS_ANNOUNCEMENT_URL = "https://mopsov.twse.com.tw/mops/web/ajax_t108sb27"
MARKET_TYPEK = {"TWSE": "sii", "TPEx": "otc"}

_NO_DATA_MARKER = "查無符合條件之資料"
_BLOCKED_MARKERS = ("查詢過於頻繁", "因為安全性考量")
_REQUIRED_HEADERS = ("除息交易日", "現金股利發放日", "公告日期")
_ROW_WIDTH = 19
# Positions within a data row (design 015 §4.2).
_CODE, _RECORD_DATE, _CASH_EARNINGS, _CASH_RESERVES = 0, 3, 7, 8
_EX_DIVIDEND_DATE, _PAY_DATE, _ANNOUNCED_DATE, _ANNOUNCED_TIME = 10, 11, 16, 17
_QUANTUM = Decimal("0.0001")


class _ReportParser(HTMLParser):
    """Collects every `<th>` text and the `<td>` texts of each data row
    (`<tr class='odd'|'even'>`) across all tables in the report."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headers: list[str] = []
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            classes = (dict(attrs).get("class") or "").split()
            self._row = [] if {"odd", "even"} & set(classes) else None
        elif tag == "td" and self._row is not None:
            self._cell = []
        elif tag == "th":
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            text = "".join(self._cell).replace("\xa0", " ").strip()
            if tag == "th":
                self.headers.append(text)
            elif self._row is not None:
                self._row.append(text)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _decimal(value: str) -> Decimal | None:
    cleaned = value.replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def parse_ex_rights_announcements(text: str) -> list[dict[str, Any]]:
    """`[{"code", "ex_dividend_date", "pay_date", "record_date", "cash",
    "announced_at"}]`, one per (code, ex-dividend date) — the latest
    announcement wins, so a correction replaces what it corrects.
    `cash` is the ordinary-share cash per share (earnings + reserves;
    preferred-share cash excluded), quantized like stored amounts."""
    if any(marker in text for marker in _BLOCKED_MARKERS):
        raise QuoteFormatError("MOPS 拒絕查詢（查詢過於頻繁或安全性阻擋）")
    parser = _ReportParser()
    parser.feed(text)
    if not parser.rows:
        if _NO_DATA_MARKER in text:
            return []
        raise QuoteFormatError("MOPS 回應沒有資料列，也沒有「查無資料」訊息")
    missing = [h for h in _REQUIRED_HEADERS if h not in parser.headers]
    if missing:
        raise QuoteFormatError(f"MOPS 表頭缺少：{'、'.join(missing)}")
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in parser.rows:
        if len(row) != _ROW_WIDTH:
            raise QuoteFormatError(
                f"MOPS 資料列應為 {_ROW_WIDTH} 格，實際 {len(row)} 格"
            )
        ex_date = _roc_slashed_date_to_iso(row[_EX_DIVIDEND_DATE])
        pay_date = _roc_slashed_date_to_iso(row[_PAY_DATE])
        if ex_date is None or pay_date is None:
            continue  # stock-dividend-only, or pay date not decided yet
        parts = [_decimal(row[i]) for i in (_CASH_EARNINGS, _CASH_RESERVES)]
        known = [p for p in parts if p is not None]
        cash = (
            sum(known, Decimal(0)).quantize(_QUANTUM, rounding=ROUND_HALF_UP)
            if known
            else None
        )
        announcement = {
            "code": row[_CODE],
            "ex_dividend_date": ex_date,
            "pay_date": pay_date,
            "record_date": _roc_slashed_date_to_iso(row[_RECORD_DATE]),
            "cash": cash,
            "announced_at": (
                f"{_roc_slashed_date_to_iso(row[_ANNOUNCED_DATE]) or ''} "
                f"{row[_ANNOUNCED_TIME]}"
            ),
        }
        key = (announcement["code"], ex_date)
        if (
            key not in latest
            or announcement["announced_at"] >= latest[key]["announced_at"]
        ):
            latest[key] = announcement
    return list(latest.values())


def fetch_ex_rights_announcements(
    client: httpx2.Client, market: str, roc_year: int
) -> list[dict[str, Any]]:
    """All of one market's 除權息公告 announced in `roc_year` (民國年)."""
    response = client.post(
        EX_RIGHTS_ANNOUNCEMENT_URL,
        data={
            "encodeURIComponent": "1",
            "step": "1",
            "firstin": "1",
            "off": "1",
            "TYPEK": MARKET_TYPEK[market],
            "year": str(roc_year),
            "month": "all",
            "co_id_1": "",
            "co_id_2": "",
            "type": "1",
        },
    )
    response.raise_for_status()
    return parse_ex_rights_announcements(response.text)
