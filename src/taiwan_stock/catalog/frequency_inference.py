"""Design 004 §4.4.1 Phase F: infers a distribution frequency/month
pattern from the local `distributions` table when no 投信 adapter
publishes an official structured field (`etf_profiles.
distribution_frequency`) for that ETF.

Deliberately reads only the **most recent** events' month interval, not
a full-history event count — 2026-09-12's research found a real
counter-example (兆豐 changing a fund from semiannual to quarterly
distributions mid-history) where counting all past events would blend
the old and new policy into a frequency that never actually existed.
Looking at just the latest interval means a policy change is reflected
the next time this recomputes, with no stale history to un-learn.

Design 005 §4.4 follow-up — not every interval is equally trustworthy
from a single gap. A bimonthly/semiannual/annual gap is exactly what
"the next shorter tracked frequency (monthly/quarterly/semiannual),
with one payment skipped" would also produce — a fund pausing one
distribution then resuming is a real, plausible event, not just a
hypothetical. Monthly and quarterly gaps have no such same-length
confusion (no shorter tracked frequency divides evenly into them via a
single skip), so they're still classified from just two known events.
Bimonthly/semiannual/annual gaps require a **second, matching gap** —
three known events, both spanned years confirmed `success`/`empty` in
`sync_history` — before classifying; a lone matching gap reports
insufficient data instead of guessing. The sync-completeness check
additionally rules out a gap that's really a hole in our own sync
history swallowing a real event, not a skip. Every other interval, or
fewer than two known events, also reports insufficient data.

Issue #19 (2026-09-14): gaps are classified from the **actual elapsed
days** between two ex-dividend dates, not a naive year/month
subtraction. A real-world scan of every adjacent distribution pair in
the local catalog found 183 of 2116 (~8.6%) misclassified by the old
month-arithmetic approach — e.g. 00984D paid on 2026-07-31 then
2026-09-01 (32 real days apart, unambiguously monthly), but `(newer.year
- older.year) * 12 + (newer.month - older.month)` counted that as a
2-month gap purely because it crossed a calendar-month boundary, then
failed to confirm against the *other* recent gap (07-02 to 07-31,
0 months by the same arithmetic) and gave up as "insufficient data".
Day-count windows below are wide enough to absorb this kind of
pay-date drift near month boundaries while staying narrow enough not
to blur into a neighboring frequency — verified against the full local
scan and every case in this module's own test suite.
"""

import json
import sqlite3
from datetime import date

from taiwan_stock.catalog import sync_history
from taiwan_stock.catalog.distribution_rows import COUNTABLE_SQL
from taiwan_stock.infrastructure.db import write_transaction
from taiwan_stock.portfolio import yields
from taiwan_stock.shared.dates import now_utc_iso, today_taipei

# No shorter tracked frequency can produce these via one skipped
# payment — a single observed gap is unambiguous evidence on its own.
_SINGLE_GAP_FREQUENCIES = frozenset({"monthly", "quarterly"})
# Each of these equals "the next shorter tracked frequency × 2" — needs
# a second consecutive matching gap (see module docstring) to rule out
# a single skipped payment before classifying.
_CONFIRMED_GAP_FREQUENCIES = frozenset({"bimonthly", "semiannual", "annual"})
# The nominal month count each frequency represents — used for the
# `month_pattern` display field, not for classification (that's the
# whole point: the *actual* gap length is what classifies a frequency,
# this is only "which months does a confirmed cadence recur in").
_NOMINAL_MONTHS = {
    "monthly": 1,
    "bimonthly": 2,
    "quarterly": 3,
    "semiannual": 6,
    "annual": 12,
}
# Real elapsed-day ranges per frequency, centered on each nominal month
# count (~30.4 days/month) with enough slack either side to absorb a
# pay date landing near a calendar-month boundary, but with clear gaps
# between neighboring windows so nothing matches two frequencies at
# once (e.g. 100-165 days matches none of these — a genuinely irregular
# ~4-5 month cadence stays unclassified rather than guessed at). Shared
# with the yield calculation's cycle checks (design 023 §4.2).
_DAY_WINDOWS = yields.CYCLE_DAYS


# Design 046 §4.2.1: events in one full calendar year, allowing a payment
# that drifts across New Year to add or remove one. Ranges overlap only
# for quarterly/bimonthly at 5, which the median gap then settles.
YEAR_COUNTS = {
    "annual": (1, 1),
    "semiannual": (2, 2),
    "quarterly": (3, 5),
    "bimonthly": (5, 7),
    "monthly": (10, 14),
}
# Twice a year may be uneven (e.g. 7 and 11 月), so its gaps are wider.
_YEAR_GAP_DAYS = {**_DAY_WINDOWS, "semiannual": (120, 245)}


def _within(frequency: str, days: float) -> bool:
    low, high = _YEAR_GAP_DAYS[frequency]
    return low <= days <= high


def _median(values: list[int]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _full_year(
    dates: list[date],
    security_id: str,
    market: str | None,
    units: dict | None,
    listed_date: str | None,
    today: str,
) -> dict | None:
    """The latest full calendar year after the first payment says the frequency.

    `dates` are oldest first. The year must be over, come after the year of
    the first payment (a fund's first payout year is usually partial),
    start after listing, and have its distribution sync confirmed.
    """
    if not dates or market is None or units is None:
        return None
    for year in range(int(today[:4]) - 1, dates[0].year, -1):
        if listed_date and listed_date >= f"{year}-01-01":
            return None
        if not sync_history.years_covered(units, security_id, market, [year]):
            continue
        count = sum(day.year == year for day in dates)
        through = [day for day in dates if day.year <= year]
        gaps = [(b - a).days for a, b in zip(through, through[1:], strict=False)]
        if not gaps:
            return None
        median = _median(gaps)
        matches = [
            frequency
            for frequency, (low, high) in YEAR_COUNTS.items()
            if low <= count <= high and _within(frequency, median)
        ]
        if len(matches) != 1:
            return None
        return {"value": matches[0], "basis_year": year, "year_count": count}
    return None


def _classify_gap(older: date, newer: date) -> str | None:
    days = (newer - older).days
    for frequency, (low, high) in _DAY_WINDOWS.items():
        if low <= days <= high:
            return frequency
    return None


def _confirm_repeating_interval(
    dates: list[date],
    frequency: str,
    security_id: str,
    market: str | None,
    units: dict | None,
) -> str | None:
    if len(dates) < 3 or market is None or units is None:
        return None
    if _classify_gap(dates[2], dates[1]) != frequency:
        return None
    years = range(dates[2].year, dates[0].year + 1)
    if not sync_history.years_covered(units, security_id, market, years):
        return None
    return frequency


def infer_for_security(
    conn: sqlite3.Connection,
    security_id: str,
    *,
    market: str | None = None,
    units: dict | None = None,
    listed_date: str | None = None,
    today: str | None = None,
) -> dict | None:
    """Returns the inferred-frequency payload for one ETF, or `None`
    when there isn't enough data to classify (§4.4.1) — the caller
    must store `None` as-is (clearing any stale prior guess), not skip
    the write, since a policy change or a now-single remaining event
    (e.g. after a data correction) must be able to revert a previous
    guess back to 尚無可信來源.

    `market`/`units` (a `sync_history.latest_units(conn)` snapshot) are
    only needed to confirm a 2/6/12-month gap (see module docstring);
    omitting them just means those specific cases report insufficient
    data instead.
    """
    rows = conn.execute(
        "SELECT ex_dividend_date FROM distributions WHERE security_id = ? "
        "AND ex_dividend_date IS NOT NULL "
        f"AND {COUNTABLE_SQL} "
        "ORDER BY ex_dividend_date",
        (security_id,),
    ).fetchall()
    history = [date.fromisoformat(r["ex_dividend_date"]) for r in rows]
    if len(history) < 2:
        return None

    dates = history[::-1][:3]
    frequency = _classify_gap(dates[1], dates[0])
    if frequency in _CONFIRMED_GAP_FREQUENCIES:
        frequency = _confirm_repeating_interval(
            dates, frequency, security_id, market, units
        )
    elif frequency not in _SINGLE_GAP_FREQUENCIES:
        frequency = None
    method = "gap"
    year = _full_year(
        history, security_id, market, units, listed_date, today or today_taipei()
    )
    # A full year wins unless the latest gap says the policy changed since
    # (design 004's 兆豐 case: semiannual became quarterly mid-history).
    if year and _within(year["value"], (dates[0] - dates[1]).days):
        frequency, method = year["value"], "year_count"
    if frequency is None:
        return None

    interval = _NOMINAL_MONTHS[frequency]
    month_pattern = sorted(
        month for month in range(1, 13) if (month - dates[0].month) % interval == 0
    )
    if method == "year_count" and frequency == "semiannual":
        # Twice a year need not be six months apart (e.g. 7 and 11 月):
        # keep the full year's own months, never ones counted back from the
        # latest payment. Uneven ones are only shown once two years agree.
        month_pattern = sorted(
            {day.month for day in history if day.year == year["basis_year"]}
        )
    result = {
        "value": frequency,
        "month_pattern": month_pattern,
        "basis_dates": [dates[1].isoformat(), dates[0].isoformat()],
        "method": method,
        "computed_at": now_utc_iso(),
    }
    if method == "year_count":
        result.update(basis_year=year["basis_year"], year_count=year["year_count"])
    return result


def recompute_all(conn: sqlite3.Connection) -> dict:
    """Recomputes `distribution_frequency_inferred` for every local ETF
    (design 004 §4.6 point 3 — full market, not just tracked/held
    securities, so the "compare before buying" use case works). Pure
    local computation over already-synced `distributions`/sync-history
    rows, no network call — safe to run after every distribution sync
    so a policy change is reflected immediately (§4.4.1's "隨新事件重新計
    算"). Always writes (including `NULL` when data becomes
    insufficient), since a stale guess must not survive its own basis
    events changing.
    """
    rows = conn.execute(
        "SELECT p.security_id, s.market, s.listed_date FROM etf_profiles p "
        "JOIN securities s ON s.id = p.security_id"
    ).fetchall()
    units = sync_history.latest_units(conn)
    updated = 0
    cleared = 0
    with write_transaction(conn):
        for row in rows:
            inferred = infer_for_security(
                conn,
                row["security_id"],
                market=row["market"],
                units=units,
                listed_date=row["listed_date"],
            )
            inferred_json = (
                json.dumps(inferred, ensure_ascii=False) if inferred else None
            )
            conn.execute(
                "UPDATE etf_profiles SET distribution_frequency_inferred = ? "
                "WHERE security_id = ?",
                (inferred_json, row["security_id"]),
            )
            if inferred:
                updated += 1
            else:
                cleared += 1
    return {"updated": updated, "insufficient_data": cleared}
