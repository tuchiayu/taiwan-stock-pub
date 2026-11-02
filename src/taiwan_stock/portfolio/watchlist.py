"""Watchlist management for Taiwan Stock.

Design 002 §4.6.2 & §4.7:
    Tracked securities are persisted in the `watchlist` table.
"""

import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any

from taiwan_stock.infrastructure.db import write_transaction
from taiwan_stock.portfolio import limits
from taiwan_stock.shared.dates import now_utc_iso

TARGET_PRICE_ERROR = (
    f"目標價須為大於 0、不超過 {limits.MAX_UNIT_PRICE:,}、最多兩位小數的數字。"
)


def parse_target_price(raw: str | None) -> str | None:
    """Blank clears; otherwise a positive Decimal with at most 2 places and
    no more than a unit price may be (design 060), stored as text like the
    existing `watchlist.target_price` (design 042)."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(TARGET_PRICE_ERROR) from None
    if (
        not value.is_finite()
        or value <= 0
        or value > limits.MAX_UNIT_PRICE
        or value.as_tuple().exponent < -2
    ):
        raise ValueError(TARGET_PRICE_ERROR)
    return str(value)


def add_to_watchlist(
    conn: sqlite3.Connection,
    security_id: str,
    note: str | None = None,
    target_price: str | None = None,
) -> None:
    """Raises LookupError for an unknown security, which callers turn into
    404 instead of a foreign-key 500 (design 065)."""
    with write_transaction(conn):
        if not conn.execute(
            "SELECT 1 FROM securities WHERE id = ?", (security_id,)
        ).fetchone():
            raise LookupError(f"找不到商品 {security_id}")
        conn.execute(
            """
            INSERT INTO watchlist (security_id, note, target_price, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (security_id) DO UPDATE SET
                note = coalesce(excluded.note, watchlist.note),
                target_price = coalesce(excluded.target_price, watchlist.target_price),
                updated_at = excluded.updated_at
            """,
            (security_id, note, target_price, now_utc_iso()),
        )


def update_watchlist_details(
    conn: sqlite3.Connection,
    security_id: str,
    *,
    note: str | None,
    target_price: str | None,
) -> bool:
    """Set both fields exactly, so None clears them — unlike
    `add_to_watchlist`'s coalesce. Only an already-tracked security is
    updated; saving details must never start tracking (design 042 §4.4)."""
    cursor = conn.execute(
        "UPDATE watchlist SET note = ?, target_price = ?, updated_at = ? "
        "WHERE security_id = ?",
        (note, target_price, now_utc_iso(), security_id),
    )
    return cursor.rowcount > 0


def get_watchlist_item(
    conn: sqlite3.Connection, security_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM watchlist WHERE security_id = ?", (security_id,)
    ).fetchone()
    return dict(row) if row else None


def remove_from_watchlist(conn: sqlite3.Connection, security_id: str) -> bool:
    cursor = conn.execute("DELETE FROM watchlist WHERE security_id = ?", (security_id,))
    return cursor.rowcount > 0


def get_watchlist_security_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT security_id FROM watchlist").fetchall()
    return {row["security_id"] for row in rows}


def list_watchlist(
    conn: sqlite3.Connection, *, security_type: str | None = None
) -> list[dict[str, Any]]:
    """List watchlist securities joined with catalog info and latest
    daily price, optionally narrowed to one `security_type` — design
    004 §4.7.1: the 商品查詢 page's 股票／ETF mode shows tracked-count
    and the tracked-items table scoped to the current mode only, so
    switching mode must not look like items were added/removed.
    Callers that want every tracked security regardless of type (e.g.
    the JSON API) simply omit the filter.

    `transfer_agent_name` (stock-only) and `custodian_bank` (ETF-only)
    are kept as separate columns rather than one shared "過戶機構／保管
    銀行" value — design 004 §2.1/§4.7.3 flags merging them as one of
    the sources of the custodian-bank confusion this design fixes, so
    the caller must not recombine them into a single label either.
    """
    where = "WHERE s.security_type = ?" if security_type else ""
    params = [security_type] if security_type else []
    rows = conn.execute(
        f"""
        SELECT w.security_id, w.note, w.target_price, w.updated_at,
               s.code, s.name, s.market, s.security_type, s.transfer_agent_name,
               p.custodian_bank, p.custodian_bank_user_override,
               (
                   SELECT close FROM daily_prices dp
                   WHERE dp.security_id = w.security_id
                   ORDER BY dp.trade_date DESC LIMIT 1
               ) AS latest_close,
               (
                   SELECT trade_date FROM daily_prices dp
                   WHERE dp.security_id = w.security_id
                   ORDER BY dp.trade_date DESC LIMIT 1
               ) AS close_date
        FROM watchlist w
        JOIN securities s ON s.id = w.security_id
        LEFT JOIN etf_profiles p ON p.security_id = w.security_id
        {where}
        ORDER BY s.code
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]
