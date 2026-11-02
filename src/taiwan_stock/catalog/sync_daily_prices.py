"""Daily closing prices, including the by-date backfill (design 071).

Retry and the shared helpers live in `sync.py`.
"""

import sqlite3
import time
from datetime import date, timedelta
from types import ModuleType

import httpx2

from taiwan_stock.catalog import (
    delistings,
    sync,
    tpex_adapter,
    twse_adapter,
)
from taiwan_stock.catalog._parsing import QuoteFormatError
from taiwan_stock.catalog.price_storage import (
    _market_has_prices,
    _store_daily_prices,
    price_scope,
)
from taiwan_stock.shared import sync_progress
from taiwan_stock.shared.dates import today_taipei


def sync_daily_prices(
    conn: sqlite3.Connection, client: httpx2.Client, adapter: ModuleType = twse_adapter
) -> dict:
    """Syncs daily closing prices into `daily_prices`.

    Rows for a code not present in the (already synced) `securities`
    catalog for the given market are skipped.
    """
    delistings.refresh_status(conn)
    rows = sync._fetch_with_retry(adapter.fetch_daily_prices, client)
    return _store_daily_prices(
        conn, adapter.MARKET, [adapter.normalize_daily_price(row) for row in rows]
    )


# Design 013 §4.4: look back far enough to clear the longest run of
# closed days (Lunar New Year), and space out requests to the websites'
# report pages — they aren't the OpenAPI and their limits aren't
# published; 3s is the pace measured clean for TWSE (see above).
_BY_DATE_LOOKBACK_DAYS = 14
_BY_DATE_TRADING_DAYS = 2
_BY_DATE_REQUEST_DELAY_SECONDS = 3.0


def load_market_holidays(client: httpx2.Client) -> set[str]:
    """TWSE's closure schedule, shared by both markets. Not essential:
    if it can't be fetched the by-date sync still works, it just asks
    the source about each closed weekday instead of skipping it."""
    try:
        return twse_adapter.fetch_holidays(client)
    except (httpx2.HTTPError, ValueError, KeyError, TypeError):
        return set()


def sync_daily_prices_by_date(
    conn: sqlite3.Connection,
    client: httpx2.Client,
    adapter: ModuleType,
    *,
    holidays: set[str],
    today: str | None = None,
    sleep=time.sleep,
) -> dict:
    """Makes sure the latest trading day and the one before it are both
    stored for this market (design 013 §4.4), so daily P&L compares two
    adjacent trading days. Walks back from today, never requesting a
    weekend, a scheduled holiday, or a date already stored. Requests are
    not retried: a failure here makes the caller fall back to the
    OpenAPI (which is), rather than hammering a report page that may be
    rate-limiting us. Raises `QuoteFormatError`/`httpx2.HTTPError` for
    the caller to fall back on."""
    delistings.refresh_status(conn)
    start = date.fromisoformat(today or today_taipei())
    totals = {
        "synced": 0,
        "skipped_unknown_security": 0,
        "skipped_no_price": 0,
        "skipped_delisted": 0,
    }
    trade_dates: list[str] = []
    fetched_dates: list[str] = []
    incomplete_dates: list[str] = []
    requested = False

    for offset in range(_BY_DATE_LOOKBACK_DAYS):
        day = start - timedelta(days=offset)
        iso = day.isoformat()
        if day.weekday() >= 5 or iso in holidays:
            continue
        if not _market_has_prices(conn, adapter.MARKET, iso):
            sync_progress.phase(f"{adapter.MARKET} 股價 · {iso}")
            if requested:
                sleep(_BY_DATE_REQUEST_DELAY_SECONDS)
            requested = True
            scope = price_scope(conn, adapter.MARKET, iso)
            rows = adapter.fetch_daily_prices_on(client, iso)
            if rows is None:
                # Unscheduled closure, today's close not out yet, or a
                # holiday outside the (current-year-only) schedule.
                continue
            stored = _store_daily_prices(
                conn,
                adapter.MARKET,
                rows,
                coverage_date=iso,
                coverage_scope=scope,
                source=getattr(
                    adapter, "DAILY_QUOTES_BY_DATE_URL", f"{adapter.MARKET}:date_report"
                ),
            )
            for key in totals:
                totals[key] += stored[key]
            fetched_dates.append(iso)
            if not _market_has_prices(conn, adapter.MARKET, iso):
                incomplete_dates.append(iso)
        trade_dates.append(iso)
        if len(trade_dates) == _BY_DATE_TRADING_DAYS:
            break

    if not trade_dates:
        raise QuoteFormatError(f"{_BY_DATE_LOOKBACK_DAYS} 天內找不到任何交易日資料")
    return {
        **totals,
        "latest_trade_date": trade_dates[0],
        "trade_dates": trade_dates,
        "fetched_dates": fetched_dates,
        "incomplete_dates": incomplete_dates,
        "source": "by_date",
        "fallback_reason": None,
    }


def sync_daily_prices_both(
    conn: sqlite3.Connection,
    client: httpx2.Client,
    *,
    today: str | None = None,
    sleep=time.sleep,
) -> dict:
    """Syncs TWSE then TPEx daily prices independently (issue #17): the
    two markets are unrelated hosts, so one failing (rate-limited,
    timed out, temporarily down) must not prevent the other — which
    may well be fine — from being attempted at all. Returns
    `{"results": {"twse": {...}, "tpex": {...}}, "errors": {"twse": str,
    ...}}`, populating exactly one of `results`/`errors` per market so a
    caller can tell "this market synced" from "this market failed"
    without one masking the other.

    Each market goes through the by-date sync first (design 013); if
    its report page fails or no longer parses, that market falls back
    to the OpenAPI latest-day sync and says so in `source`/
    `fallback_reason`."""
    sync_progress.phase("核對休市日")
    holidays = load_market_holidays(client)
    results, errors = {}, {}
    for name, adapter in (("twse", twse_adapter), ("tpex", tpex_adapter)):
        try:
            try:
                results[name] = sync_daily_prices_by_date(
                    conn, client, adapter, holidays=holidays, today=today, sleep=sleep
                )
            except (httpx2.HTTPError, ValueError, KeyError, TypeError) as exc:
                sync_progress.phase(f"{adapter.MARKET} 股價 · OpenAPI 備援")
                result = sync_daily_prices(conn, client, adapter=adapter)
                latest = result["latest_trade_date"]
                results[name] = {
                    **result,
                    "trade_dates": [latest] if latest else [],
                    "fetched_dates": [latest] if latest else [],
                    "source": "openapi_fallback",
                    "fallback_reason": sync._short_reason(exc),
                }
        # Design 062: a market's format error stays with that market too.
        except (httpx2.HTTPError, ValueError, KeyError, TypeError) as exc:
            errors[name] = str(exc)
    return {"results": results, "errors": errors}
