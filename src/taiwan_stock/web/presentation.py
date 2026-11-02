"""Shared display formats and safe navigation for server-rendered pages."""

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jinja2 import Undefined

from taiwan_stock.shared.dates import TAIPEI

STATUS_LABELS = {
    "queued": "準備中",
    "running": "執行中",
    "success": "完成",
    "partial": "部分成功",
    "failed": "失敗",
    "interrupted": "中斷",
    "pending": "尚未完成",
    "empty": "來源無資料",
    "unsupported": "來源尚未支援",
    "http_failed": "連線失敗",
    "db_failed": "資料庫忙碌",
    "base_pending": "等待主級別",
    "parse_failed": "來源待更新",
    "not_listed": "來源未收錄",
    "mapping_failed": "來源未收錄",
    "skipped_delisted": "已下市，略過",
    "skipped_notice": "前置公告，略過",
    "source_maintenance": "來源維護中",
    "final": "已公告",
    "estimated": "預估",
    "unknown": "待確認",
}


# Shared by the holdings and corporate actions pages (design 070).
ACTION_TYPE_LABELS = {
    "split": "分割",
    "stock_dividend": "股票股利",
    "cash_capital_reduction": "現金減資",
    "other": "其他",
}
# Markets as the price and distribution sync summaries name them.
SYNC_MARKET_LABELS = {"twse": "TWSE", "tpex": "TPEx"}


def number(value, places=2, fixed=False):
    if value is None or isinstance(value, Undefined) or value == "":
        return "—"
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            return "—"
        if places is None:
            places = max(0, -amount.as_tuple().exponent)
            fixed = True
        amount = amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return "—"
    result = f"{abs(amount) if amount == 0 else amount:,.{places}f}"
    return result if fixed or not places else result.rstrip("0").rstrip(".")


def taipei_time(value):
    """A stored UTC timestamp as Taipei `YYYY-MM-DD HH:MM` (design 051 §4.2.3).

    Naive values are UTC, like everything the app stores; text that isn't a
    timestamp stays as-is rather than disappearing.
    """
    if value is None or isinstance(value, Undefined) or value == "":
        return "—"
    moment = value
    if not isinstance(moment, datetime):
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return value
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M")


def hundred_million(value):
    """Yuan as 億元 with one fixed decimal, so a column lines up (design 044)."""
    if value is None or isinstance(value, Undefined):
        return "—"
    return number(Decimal(value) / Decimal(100_000_000), 1, True)


MARKET_LABELS = {"TWSE": "上市", "TPEx": "上櫃"}


def market_label(value):
    """Exchange code as investors say it; unknown codes stay visible as-is."""
    return MARKET_LABELS.get(value, value)


def local_return(value, default="/transactions"):
    """Only accept application paths, including their query and fragment."""
    if not value or any(ord(c) < 32 for c in value) or "\\" in value:
        return default
    try:
        parsed = urlsplit(value)
    except ValueError:
        return default
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return default
    if value.startswith("//"):
        return default
    return value


def with_query(url, **changes):
    parsed = urlsplit(local_return(url))
    params = dict(parse_qsl(parsed.query))
    params.update(changes)
    return urlunsplit(
        (
            "",
            "",
            parsed.path,
            urlencode({k: v for k, v in params.items() if v is not None}),
            parsed.fragment,
        )
    )
