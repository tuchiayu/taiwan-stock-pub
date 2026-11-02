"""SQLite persistence and date coverage for daily market prices."""

import sqlite3

from taiwan_stock.catalog import delistings
from taiwan_stock.infrastructure.db import write_transaction
from taiwan_stock.shared.dates import now_utc_iso


def price_scope(conn, market, trade_date):
    """Capture the catalog before the request, including codes without a close."""
    return {
        r["code"]: r["id"]
        for r in conn.execute(
            "SELECT id,code FROM securities WHERE market=? AND status='active' "
            "AND (listed_date IS NULL OR listed_date<=?)",
            (market, trade_date),
        )
    }


def _store_daily_prices(
    conn: sqlite3.Connection,
    market: str,
    normalized_rows: list[dict | None],
    *,
    coverage_date: str | None = None,
    coverage_scope: dict | None = None,
    source: str = "date_report",
) -> dict:
    """Persist prices and explicit date-report withdrawals atomically.

    Only a code in the pre-request scope with status=no_quote is withdrawn.
    Missing/malformed records retain prices and leave coverage unverified.
    OpenAPI writes never withdraw prices or claim date-report completeness.
    """
    synced = 0
    skipped_unknown_security = 0
    skipped_no_price = 0
    latest_trade_date = None
    skipped_delisted = 0
    delisted_codes = {
        r[0]
        for r in conn.execute(
            "SELECT code FROM securities WHERE market=? AND status='delisted'",
            (market,),
        )
    }

    securities = {
        row["code"]: row["id"]
        for row in conn.execute(
            "SELECT id, code FROM securities WHERE market = ?",
            (market,),
        ).fetchall()
    }

    quotes = {}
    for row in normalized_rows:
        if row is None:
            skipped_no_price += 1
            continue
        if coverage_date is not None and row["trade_date"] != coverage_date:
            raise ValueError("行情日期與要求日期不一致")
        code = row["code"]
        if code in quotes and quotes[code] != row:
            quotes[code] = {**row, "close": None, "status": "unverified"}
        else:
            quotes[code] = row

    with write_transaction(conn):
        for code, normalized in quotes.items():
            if code in delisted_codes:
                skipped_delisted += 1
                continue
            security_id = securities.get(code)
            if security_id is None:
                skipped_unknown_security += 1
                continue
            trade_date = normalized["trade_date"]
            status = normalized.get(
                "status",
                "quoted" if normalized.get("close") is not None else "unverified",
            )
            if status != "quoted":
                skipped_no_price += 1
                if (
                    status == "no_quote"
                    and coverage_date is not None
                    and (coverage_scope or {}).get(code) == security_id
                ):
                    conn.execute(
                        "INSERT INTO daily_price_invalidations "
                        "(security_id,market,trade_date,close,volume,source,"
                        "reason,invalidated_at) "
                        "SELECT security_id,?,trade_date,close,volume,?,?,? "
                        "FROM daily_prices "
                        "WHERE security_id=? AND trade_date=?",
                        (
                            market,
                            source,
                            "explicit_no_quote",
                            now_utc_iso(),
                            security_id,
                            trade_date,
                        ),
                    )
                    conn.execute(
                        "DELETE FROM daily_prices WHERE security_id=? AND trade_date=?",
                        (security_id, trade_date),
                    )
                continue
            if normalized.get("close") is None:
                raise ValueError("有價行情缺少收盤價")
            if latest_trade_date is None or trade_date > latest_trade_date:
                latest_trade_date = trade_date
            conn.execute(
                """
                INSERT INTO daily_prices (security_id, trade_date, close, volume)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (security_id, trade_date) DO UPDATE SET
                    close = excluded.close, volume = excluded.volume
                """,
                (security_id, trade_date, normalized["close"], normalized["volume"]),
            )
            synced += 1
        delistings.refresh_status(conn)
        if coverage_date is not None:
            stamp = now_utc_iso()
            conn.execute(
                "INSERT INTO daily_price_reports VALUES(?,?,?) "
                "ON CONFLICT(market,trade_date) DO UPDATE SET "
                "checked_at=excluded.checked_at",
                (market, coverage_date, stamp),
            )
            coverage = []
            for code, sid in (coverage_scope or {}).items():
                row = quotes.get(code) or {}
                status = row.get(
                    "status", "quoted" if row.get("close") is not None else "unverified"
                )
                coverage.append(
                    (market, coverage_date, sid, row.get("close"), stamp, status)
                )
            conn.executemany(
                "INSERT INTO daily_price_coverage "
                "(market,trade_date,security_id,close,checked_at,status) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(market,trade_date,security_id) DO UPDATE SET "
                "close=excluded.close,checked_at=excluded.checked_at,status=excluded.status",
                coverage,
            )

    return {
        "synced": synced,
        "skipped_unknown_security": skipped_unknown_security,
        "skipped_no_price": skipped_no_price,
        "latest_trade_date": latest_trade_date,
        "skipped_delisted": skipped_delisted,
    }


def _market_has_prices(conn: sqlite3.Connection, market: str, trade_date: str) -> bool:
    """Only a checked report covering every eligible catalog item is complete.

    A checked no-quote needs no retry. A newly added code or a missing stored
    quote invalidates coverage. OpenAPI writes never create report coverage.
    """
    return bool(
        conn.execute(
            """
        SELECT EXISTS(SELECT 1 FROM daily_price_reports WHERE market=? AND trade_date=?)
        AND NOT EXISTS (
            SELECT 1 FROM securities s
            LEFT JOIN daily_price_coverage c
              ON c.security_id=s.id AND c.market=? AND c.trade_date=?
            WHERE s.market=? AND s.status='active'
              AND (s.listed_date IS NULL OR s.listed_date<=?)
              AND (c.security_id IS NULL OR c.status='unverified'
              OR (c.status='no_quote' AND EXISTS (
                  SELECT 1 FROM daily_prices p WHERE p.security_id=s.id
                  AND p.trade_date=c.trade_date
              ))
              OR (c.status='quoted' AND (c.close IS NULL OR NOT EXISTS (
                  SELECT 1 FROM daily_prices p WHERE p.security_id=s.id
                  AND p.trade_date=? AND p.close=c.close
              ))))
        )
        """,
            (market, trade_date, market, trade_date, market, trade_date, trade_date),
        ).fetchone()[0]
    )
