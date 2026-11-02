"""Text/number cleaning shared by twse_adapter.py and tpex_adapter.py.

Both government open-data feeds share the same quirks (stray/double
-escaped HTML entities, placeholder text like "尚未公告" instead of a
number, comma-thousands-separated figures, ROC-calendar dates) — kept
in one place so a fix for one market's data can't silently miss the
other's (see design 002 §5.2 item 14/15's HTML-entity fix, which
originally had to be applied to both adapters by hand).
"""

import html
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

_PLACEHOLDER_VALUES = ("--", "---", "----")

AMOUNT_QUANTUM = Decimal("0.0001")
# StockDividendRatio needs finer precision than cash amounts do — a
# genuine value like "0.221074" (TWSE) or "0.01993215" (TPEx) would be
# truncated by AMOUNT_QUANTUM's 4 places and skew the derived split
# ratio (see catalog/sync.py's normalize_ex_dividend_notice callers).
RATIO_QUANTUM = Decimal("0.00000001")


def clean_text(value: Any) -> str | None:
    """Government open-data text fields occasionally carry stray HTML
    entities — confirmed live 2026-09 on TWSE: two ETF rows' `保管機構`
    contain a *double*-escaped `&amp;nbsp;` (one `html.unescape()` pass
    only yields the still-literal `&nbsp;`, which Jinja then
    re-escapes for display right back into `&amp;nbsp;`). Unescaping
    repeatedly until the string stops changing handles that and any
    single-escaped case the same way, without assuming a fixed escape
    depth."""
    if not value:
        return None
    cleaned = str(value)
    for _ in range(5):
        unescaped = html.unescape(cleaned)
        if unescaped == cleaned:
            break
        cleaned = unescaped
    cleaned = cleaned.strip()
    return cleaned or None


class InvalidDate(ValueError):
    """A source date that isn't a real calendar day, or isn't a date at all
    in a format these parsers check strictly (design 062 §4.2.1)."""


def _iso(year: int, month: int, day: int, raw) -> str:
    try:
        return date(year, month, day).isoformat()
    except ValueError as exc:
        raise InvalidDate(f"來源日期不存在：{raw}") from exc


def optional_date(parse, value) -> str | None:
    """A non-critical date (a listing date): an invalid one reads as
    unknown, so one odd row can't fail a whole catalog sync."""
    if not value:
        return None
    try:
        return parse(value)
    except InvalidDate:
        return None


def roc_date_to_iso(value: str) -> str:
    # "1150916" -> ROC year 115 (3 digits) + month (2) + day (2).
    text = str(value).strip()
    if not text.isdigit() or len(text) not in (6, 7):
        raise InvalidDate(f"來源日期格式不符：{value}")
    roc_year, month, day = text[:-4], text[-4:-2], text[-2:]
    return _iso(int(roc_year) + 1911, int(month), int(day), value)


def gregorian_yyyymmdd_to_iso(value: Any) -> str | None:
    """Parses TPEx's `DateOfListing`/`DateOfIncorporation` ("20180808")
    — confirmed live 2026-09-13 this is plain Gregorian, unlike every
    other date field these two adapters otherwise handle (all ROC-
    calendar): `roc_date_to_iso("20180808")` would misread it as ROC
    year 2018 (→ 3929 AD). Returns `None` for a blank value."""
    if not value or not str(value).strip():
        return None
    cleaned = str(value).strip()
    if not cleaned.isdigit() or len(cleaned) != 8:
        raise InvalidDate(f"來源日期格式不符：{value}")
    return _iso(int(cleaned[:4]), int(cleaned[4:6]), int(cleaned[6:8]), value)


def roc_dotted_date_to_iso(value: Any) -> str | None:
    """Parses the "115年08月18日" style ROC date TWSE's ETF distribution
    JSON uses (design 004 §4.2) — a different format from
    `roc_date_to_iso`'s digit-only "1150916" (the ex-dividend notice
    table's format), so it needs its own parser rather than reusing
    that one. Returns `None` for a blank/unannounced value rather than
    raising, matching "未公告的日期留空即可" (design 004 §2.2)."""
    if not value or not str(value).strip():
        return None
    cleaned = str(value).strip()
    try:
        roc_year_str, rest = cleaned.split("年", 1)
        month_str, rest = rest.split("月", 1)
        day_str = rest.split("日", 1)[0]
        parts = int(roc_year_str) + 1911, int(month_str), int(day_str)
    except ValueError:
        return None
    return _iso(*parts, value)


def roc_slashed_date_to_iso(value: Any) -> str | None:
    """Parses the "115/09/24" ROC date TPEx's website quote tables use.
    Returns `None` for a blank or malformed value."""
    if not value:
        return None
    try:
        roc_year, month, day = str(value).strip().split("/")
        parts = int(roc_year) + 1911, int(month), int(day)
    except ValueError:
        return None
    return _iso(*parts, value)


class QuoteFormatError(ValueError):
    """A website quote report no longer looks the way it was verified
    to (missing table/column, wrong date echoed back). These reports
    aren't formal APIs, so the caller treats this as "fall back to the
    OpenAPI source", not as a hard failure."""


def json_object(response) -> dict[str, Any]:
    """A report's JSON body, which must be an object (design 062): any
    other shape is a format change, handled like the other quote errors."""
    payload = response.json()
    if not isinstance(payload, dict):
        raise QuoteFormatError("回應不是預期的物件格式")
    return payload


def table_rows(table: dict[str, Any], columns: dict[str, str]) -> list[dict[str, Any]]:
    """Maps a `{"fields": [...], "data": [[...], ...]}` report table to
    dicts keyed by `columns`' keys, locating each column by its header
    name rather than position. Header names are stripped first — TPEx's
    carry stray padding (e.g. "收盤 ", "成交股數  ", verified live
    2026-09-29)."""
    fields = [str(field).strip() for field in table.get("fields") or []]
    try:
        index = {key: fields.index(name) for key, name in columns.items()}
    except ValueError as exc:
        raise QuoteFormatError(f"找不到欄位（現有欄位：{fields}）") from exc
    width = max(index.values()) + 1
    return [
        {key: row[i] for key, i in index.items()}
        for row in table.get("data") or []
        if len(row) >= width
    ]


def clean_amount(raw: str, quantum: Decimal) -> Decimal | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        amount = Decimal(raw).quantize(quantum, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        # Confirmed live on both markets: bond-fund/placeholder rows
        # use text like "尚未公告" ("not yet announced") instead of a
        # number here — treat the same as "no amount yet", not a
        # parse error.
        return None
    return amount if amount > 0 else None


def clean_price(raw: Any) -> str | None:
    if raw is None:
        return None
    cleaned = str(raw).strip().replace(",", "")
    if not cleaned or cleaned in _PLACEHOLDER_VALUES:
        return None
    try:
        dec = Decimal(cleaned)
    except InvalidOperation:
        return None
    return str(dec) if dec.is_finite() and dec > 0 else None


def clean_volume(raw: Any) -> int | None:
    if raw is None:
        return None
    cleaned = str(raw).strip().replace(",", "")
    if not cleaned or cleaned in _PLACEHOLDER_VALUES:
        return None
    try:
        vol = int(Decimal(cleaned))
    except (InvalidOperation, ValueError):
        return None
    return vol if vol >= 0 else None


def normalize_dated_quote(row: dict, trade_date: str) -> dict | None:
    """Only recognized exchange placeholders prove a code has no quote."""
    code = str(row.get("code") or "").strip()
    if not code:
        return None
    raw = row.get("close")
    close = clean_price(raw)
    status = (
        "quoted"
        if close is not None
        else ("no_quote" if str(raw).strip() in _PLACEHOLDER_VALUES else "unverified")
    )
    return {
        "code": code,
        "trade_date": trade_date,
        "close": close,
        "volume": clean_volume(row.get("volume")),
        "status": status,
    }
