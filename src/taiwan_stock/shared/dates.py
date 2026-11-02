"""Taipei business-date helpers.

Design 002 §4.4: trade dates, ex-dates etc. are Asia/Taipei business
date labels (`YYYY-MM-DD`), not UTC-derived.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

TAIPEI = ZoneInfo("Asia/Taipei")
_business_date = ContextVar("business_date", default=None)


def today_taipei() -> str:
    return _business_date.get() or datetime.now(TAIPEI).date().isoformat()


@contextmanager
def business_date_scope():
    """A financial snapshot crossing midnight still has one valuation date."""
    token = _business_date.set(today_taipei())
    try:
        yield
    finally:
        _business_date.reset(token)


def taipei_date(utc_iso: str) -> str:
    """Taipei calendar date of a stored UTC timestamp — slicing the UTC
    string would put anything before 08:00 Taipei on the previous day."""
    moment = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(TAIPEI).date().isoformat()


def now_utc_iso() -> str:
    """UTC timestamp matching the `strftime('%Y-%m-%dT%H:%M:%fZ', 'now')`
    format SQLite uses for `created_at`/`updated_at` column defaults —
    SQLite's `%f` is millisecond-precision (`SS.SSS`), not
    microseconds, so a value computed here and one read back from the
    database sort identically.
    """
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
