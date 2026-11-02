"""Stock ex-dividend notices, dividend history and pay dates (design 071).

Retry, circuit breaker and the shared upserts live in `sync.py`.
"""

import sqlite3
import time
from collections import defaultdict
from datetime import date
from decimal import Decimal
from types import ModuleType

import httpx2

from taiwan_stock.catalog import (
    delistings,
    mops_adapter,
    sync,
    tpex_adapter,
    twse_adapter,
)
from taiwan_stock.infrastructure.db import write_transaction
from taiwan_stock.shared import sync_progress
from taiwan_stock.shared.dates import now_utc_iso, today_taipei
from taiwan_stock.shared.ids import new_id


def sync_ex_dividend_notices(
    conn: sqlite3.Connection, client: httpx2.Client, adapter: ModuleType = twse_adapter
) -> dict:
    """Syncs an ex-dividend advance-notice table into `distributions`
    and `corporate_actions`. Rows for a code not present in the
    (already synced) `securities` catalog are skipped and counted
    rather than guessed at — run `sync_stock_catalog` (and, for TWSE,
    `sync_etf_catalog`) for the same market first.
    """
    from taiwan_stock.portfolio.corporate_actions import (
        CorporateActionValidationError,
    )

    rows = adapter.fetch_ex_dividend_notices(client)
    distributions_synced = 0
    actions_synced = 0
    skipped_unknown_security = 0
    skipped_invalid_ratio = 0
    validation_errors = []
    reconciliation_warnings = []
    synced_security_ids = set()
    source_url = (
        getattr(adapter, "BASE_URL", "")
        + getattr(adapter, "EX_DIVIDEND_NOTICE_PATH", "")
    ) or None
    with write_transaction(conn):
        for row in rows:
            distribution, action = adapter.normalize_ex_dividend_notice(row)
            if distribution is None and action is None:
                continue
            code = (distribution or action)["code"]

            security = conn.execute(
                "SELECT id FROM securities WHERE market = ? AND code = ?",
                (adapter.MARKET, code),
            ).fetchone()
            if security is None:
                skipped_unknown_security += 1
                continue
            security_id = security["id"]

            if distribution is not None:
                sync.upsert_distribution(conn, security_id, distribution)
                distributions_synced += 1
            if action is not None:
                try:
                    sync.upsert_corporate_action(
                        conn,
                        security_id,
                        {
                            **action,
                            "source": f"{adapter.MARKET} ex-dividend notices",
                            "source_url": source_url,
                        },
                    )
                except CorporateActionValidationError as exc:
                    skipped_invalid_ratio += 1
                    validation_errors.append(
                        {
                            "market": adapter.MARKET,
                            "code": code,
                            "ex_date": action["ex_date"],
                            "action_type": action["action_type"],
                            "reason": str(exc),
                            "source": f"{adapter.MARKET} ex-dividend notices",
                        }
                    )
                else:
                    actions_synced += 1
                    synced_security_ids.add(security_id)
        from taiwan_stock.portfolio.distribution_entitlement import history_warnings

        reconciliation_warnings = [
            w for w in history_warnings(conn) if w["security_id"] in synced_security_ids
        ]
        delistings.refresh_status(conn)

    return {
        "distributions": distributions_synced,
        "corporate_actions": actions_synced,
        "skipped_unknown_security": skipped_unknown_security,
        "skipped_invalid_ratio": skipped_invalid_ratio,
        "validation_errors": validation_errors,
        "reconciliation_warnings": reconciliation_warnings,
    }


_DIVIDEND_HISTORY_DELAY_SECONDS = 3.0
_DIVIDEND_HISTORY_SOURCES = {
    "TWSE": "twse_ex_rights_result",
    "TPEx": "tpex_ex_rights_result",
}


def _stock_dividend_scope(
    conn: sqlite3.Connection, today: date, watch_years: int
) -> dict[str, dict[str, tuple[str, str]]]:
    """`{market: {code: (security_id, backfill_start_iso)}}` for stocks
    the user traded or watches. A traded stock starts at its earliest
    acquisition (any account, buy or opening) — dividends before that
    aren't the user's; a watched-only stock has no such date and looks
    back `watch_years` instead (design 014 §4.3)."""
    try:
        fallback = today.replace(year=today.year - watch_years)
    except ValueError:  # 2/29 → a non-leap year
        fallback = today.replace(year=today.year - watch_years, day=28)
    scope: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)
    for row in conn.execute(
        """
        SELECT s.id, s.market, s.code,
               (SELECT MIN(t.trade_date) FROM live_trades t
                WHERE t.security_id = s.id
                  AND t.event_type IN ('buy', 'opening')) AS first_acquired
        FROM securities s
        WHERE s.security_type = 'stock'
          AND (s.id IN (SELECT security_id FROM live_trades)
               OR s.id IN (SELECT security_id FROM watchlist))
        """
    ):
        start = row["first_acquired"] or fallback.isoformat()
        scope[row["market"]][row["code"]] = (row["id"], start)
    return scope


def upsert_stock_dividend_result(
    conn: sqlite3.Connection,
    security_id: str,
    ex_dividend_date: str,
    amount: Decimal,
    *,
    source: str,
    fetched_at: str,
) -> None:
    """A confirmed ex-dividend result supersedes whatever the advance
    notice said for the same date (it reflects the final share count);
    `pay_date`/`record_date`/reconciliation columns are left untouched —
    this source has no pay date (design 014 §4.4)."""
    conn.execute(
        """
        INSERT INTO distributions
            (id, security_id, ex_dividend_date, amount_per_share, amount_status,
             source, fetched_at)
        VALUES (?, ?, ?, ?, 'final', ?, ?)
        ON CONFLICT (security_id, ex_dividend_date) DO UPDATE SET
            amount_per_share = excluded.amount_per_share,
            amount_status = 'final',
            source = excluded.source,
            fetched_at = excluded.fetched_at
        """,
        (new_id(), security_id, ex_dividend_date, str(amount), source, fetched_at),
    )


def _backfill_market_dividends(
    conn: sqlite3.Connection,
    client: httpx2.Client,
    adapter: ModuleType,
    stocks: dict[str, tuple[str, str]],
    today: date,
    fetched_at: str,
    sleep,
) -> dict:
    # One report request covers the whole market for a date range, so
    # the range is the earliest start among `stocks`; each stock is then
    # filtered to its own start.
    start = min(date.fromisoformat(first) for _, first in stocks.values())
    events: list[dict] = []
    years: list[int] = []
    requested = False
    for year in range(start.year, today.year + 1):
        sync_progress.phase(f"{adapter.MARKET} 個股歷史股利 · {year} 年")
        chunk_start = max(start, date(year, 1, 1)).isoformat()
        chunk_end = min(today, date(year, 12, 31)).isoformat()
        if requested:
            sleep(_DIVIDEND_HISTORY_DELAY_SECONDS)
        requested = True
        events.extend(
            sync._fetch_with_retry(
                adapter.fetch_ex_rights_results, client, chunk_start, chunk_end
            )
        )
        years.append(year)

    to_write = []
    detail_requests = skipped_no_cash = skipped_out_of_scope = 0
    for event in events:
        stock = stocks.get(event["code"])
        if stock is None or event["ex_dividend_date"] < stock[1]:
            skipped_out_of_scope += 1
            continue
        cash = event["cash"]
        if event["needs_detail"]:
            sleep(_DIVIDEND_HISTORY_DELAY_SECONDS)
            cash = sync._fetch_with_retry(
                adapter.fetch_ex_rights_cash_detail,
                client,
                event["code"],
                event["ex_dividend_date"],
            )
            detail_requests += 1
        if cash is None:
            skipped_no_cash += 1
            continue
        to_write.append((stock[0], event["ex_dividend_date"], cash))

    source = _DIVIDEND_HISTORY_SOURCES[adapter.MARKET]
    with write_transaction(conn):
        for security_id, ex_date, cash in to_write:
            upsert_stock_dividend_result(
                conn, security_id, ex_date, cash, source=source, fetched_at=fetched_at
            )
    return {
        "distributions": len(to_write),
        "detail_requests": detail_requests,
        "skipped_no_cash": skipped_no_cash,
        "skipped_out_of_scope": skipped_out_of_scope,
        "years": years,
    }


def sync_stock_dividend_history(
    conn: sqlite3.Connection,
    client: httpx2.Client,
    *,
    years: int = 2,
    today: str | None = None,
    sleep=time.sleep,
) -> dict:
    """Backfills cash dividends that already went ex for stocks the user
    trades or watches (design 014) — the advance-notice sync above only
    ever sees upcoming events, so anything ex-dividend before syncing
    started was missing. ETFs are out of scope: they have their own
    distribution history sync. Each market is independent: one failing
    lands in `errors` without stopping the other."""
    today_date = date.fromisoformat(today or today_taipei())
    scope = _stock_dividend_scope(conn, today_date, years)
    fetched_at = now_utc_iso()
    outcome: dict = {"errors": {}}
    for name, adapter in (("twse", twse_adapter), ("tpex", tpex_adapter)):
        stocks = scope.get(adapter.MARKET)
        if not stocks:
            outcome[name] = {
                "distributions": 0,
                "detail_requests": 0,
                "skipped_no_cash": 0,
                "skipped_out_of_scope": 0,
                "years": [],
            }
            continue
        try:
            outcome[name] = _backfill_market_dividends(
                conn, client, adapter, stocks, today_date, fetched_at, sleep
            )
        except (httpx2.HTTPError, ValueError, KeyError, TypeError) as exc:
            outcome["errors"][name] = sync._short_reason(exc)
    return outcome


_PAY_DATE_DELAY_SECONDS = 3.0


def _fill_market_pay_dates(
    conn: sqlite3.Connection,
    client: httpx2.Client,
    market: str,
    pending: list[sqlite3.Row],
    today: date,
    sleep,
) -> dict:
    # `year` on MOPS is the *announcement* year: an event announced late
    # in one year can go ex early the next, so each ex-dividend year also
    # pulls in the year before it (design 015 §4.4).
    years = sorted(
        {
            y
            for row in pending
            for y in (
                int(row["ex_dividend_date"][:4]) - 1,
                int(row["ex_dividend_date"][:4]),
            )
            if y <= today.year
        }
    )
    announcements: dict[tuple[str, str], dict] = {}
    for index, year in enumerate(years):
        sync_progress.phase(f"{market} 發放日 · {year} 年")
        if index:
            sleep(_PAY_DATE_DELAY_SECONDS)
        for item in sync._fetch_with_retry(
            mops_adapter.fetch_ex_rights_announcements, client, market, year - 1911
        ):
            key = (item["code"], item["ex_dividend_date"])
            if key not in announcements or (
                item["announced_at"] >= announcements[key]["announced_at"]
            ):
                announcements[key] = item

    updated = unmatched = 0
    mismatches = []
    with write_transaction(conn):
        for row in pending:
            item = announcements.get((row["code"], row["ex_dividend_date"]))
            if item is None:
                unmatched += 1
                continue
            conn.execute(
                """
                UPDATE distributions
                SET pay_date = ?, record_date = COALESCE(record_date, ?)
                WHERE id = ?
                """,
                (item["pay_date"], item["record_date"], row["id"]),
            )
            updated += 1
            # Design 015 §4.4: amounts stay owned by the ex-rights result
            # / notice syncs — a disagreement is reported, never "fixed".
            stored = row["amount_per_share"]
            if stored is not None and item["cash"] is not None:
                stored_amount = Decimal(stored).quantize(Decimal("0.0001"))
                if stored_amount != item["cash"]:
                    mismatches.append(
                        {
                            "code": row["code"],
                            "ex_dividend_date": row["ex_dividend_date"],
                            "stored": str(stored_amount),
                            "mops": str(item["cash"]),
                        }
                    )
    return {
        "updated": updated,
        "unmatched": unmatched,
        "amount_mismatches": mismatches,
        "years": [y - 1911 for y in years],
    }


def sync_stock_dividend_pay_dates(
    conn: sqlite3.Connection,
    client: httpx2.Client,
    *,
    today: str | None = None,
    sleep=time.sleep,
) -> dict:
    """Fills `pay_date` (and a missing `record_date`) on stock
    distributions from MOPS 除權息公告 (design 015) — neither the
    advance notices nor the ex-rights results carry a pay date, so
    without this every stock dividend sat in 日期待確認 and never
    counted toward 扣息回本價. Only markets and announcement years with
    something still missing are requested; amounts are never changed."""
    today_date = date.fromisoformat(today or today_taipei())
    pending: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT d.id, d.ex_dividend_date, d.amount_per_share, s.code, s.market
        FROM distributions d
        JOIN securities s ON s.id = d.security_id
        WHERE s.security_type = 'stock'
          AND d.pay_date IS NULL
          AND d.ex_dividend_date IS NOT NULL
        """
    ):
        pending[row["market"]].append(row)

    outcome: dict = {"errors": {}}
    for name, market in (("twse", "TWSE"), ("tpex", "TPEx")):
        if not pending.get(market):
            outcome[name] = {
                "updated": 0,
                "unmatched": 0,
                "amount_mismatches": [],
                "years": [],
            }
            continue
        try:
            outcome[name] = _fill_market_pay_dates(
                conn, client, market, pending[market], today_date, sleep
            )
        except (httpx2.HTTPError, ValueError, KeyError, TypeError) as exc:
            outcome["errors"][name] = sync._short_reason(exc)
    return outcome
