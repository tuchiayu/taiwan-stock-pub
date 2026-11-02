"""Whether two stored closes are adjacent trading days (issue #89).

Shared by the catalog list, the security detail page and the holdings'
daily P&L, so a change is only ever a one-day change. Reads the stored
market calendar (#79) and never the network: a day between the two
closes counts as a trading day unless it is a weekend not marked open,
or a confirmed closure. An unconfirmed (`needs_review`) day is unknown,
and unknown is not adjacent — a missing value beats a multi-day return.
"""

import sqlite3
from datetime import date, timedelta


class TradingCalendar:
    def __init__(self, conn: sqlite3.Connection) -> None:
        rows = {}
        for row in conn.execute(
            "SELECT date, source, status FROM market_calendar_events"
        ):
            rows.setdefault(row["date"], []).append(row)
        self._status = {day: _resolve(day_rows) for day, day_rows in rows.items()}

    def adjacent(self, earlier: str | None, later: str | None) -> bool:
        """True only if no trading day (or unknown day) lies strictly between."""
        if not earlier or not later or earlier >= later:
            return False
        day = date.fromisoformat(earlier) + timedelta(days=1)
        end = date.fromisoformat(later)
        while day < end:
            status = self._status.get(day.isoformat())
            if status in ("open", "needs_review"):
                return False
            if status is None and day.weekday() < 5:
                return False
            day += timedelta(days=1)
        return True


def _resolve(rows) -> str:
    """Same precedence as `market_calendar.view`: a confirmed notice beats
    the annual table, and a notice under review disqualifies every notice."""
    notice_review = any(
        r["source"] == "notices" and r["status"] == "needs_review" for r in rows
    )
    confirmed = [
        r
        for r in rows
        if r["status"] != "needs_review"
        and not (r["source"] == "notices" and notice_review)
    ]
    chosen = next((r for r in confirmed if r["source"] == "notices"), None)
    chosen = chosen or next(iter(confirmed), None)
    return chosen["status"] if chosen else "needs_review"
