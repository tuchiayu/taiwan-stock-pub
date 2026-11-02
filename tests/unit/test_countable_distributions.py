"""Design 058: flagged rows never count; scheduled uneven pairs aren't extras."""

import sqlite3
from decimal import Decimal
from types import SimpleNamespace

import pytest

from taiwan_stock.catalog.distribution_rows import COUNTABLE_SQL, countable
from taiwan_stock.portfolio import yields


def row(ex_date, amount="1.5", status=None):
    return {
        "ex_dividend_date": ex_date,
        "amount_per_share": amount,
        "amount_status": "final",
        "reconciliation_status": status,
    }


def test_countable_reads_dicts_rows_and_entries():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE d(reconciliation_status TEXT)")
    conn.executemany("INSERT INTO d VALUES (?)", [(None,), ("needs_review",)])
    stored = conn.execute("SELECT * FROM d ORDER BY rowid").fetchall()
    assert [countable(r) for r in stored] == [True, False]
    assert countable({"ex_dividend_date": "2026-01-01"})
    assert not countable(SimpleNamespace(reconciliation_status="needs_review"))
    kept = conn.execute(f"SELECT count(*) FROM d WHERE {COUNTABLE_SQL}").fetchone()
    assert kept[0] == 1


def test_a_moved_ex_date_is_counted_once_and_reported():
    result = yields.summarize(
        [row("2026-10-05", "1", "needs_review"), row("2026-10-08", "1")],
        close="20",
        as_of="2026-10-31",
    )
    assert result["cash_per_share"] == Decimal("1")
    assert result["needs_review_count"] == 1


@pytest.mark.parametrize(
    "older,newer,months,expected",
    [
        ("2026-06-18", "2026-10-20", [6, 10], True),
        ("2025-10-20", "2026-06-18", [6, 10], True),
        ("2025-11-18", "2026-01-19", [1, 11], True),
        ("2026-01-19", "2026-11-18", [1, 11], True),
        ("2025-12-15", "2026-03-16", [3, 12], True),
        ("2025-10-15", "2026-03-16", [3, 10], True),
        ("2025-11-01", "2026-06-18", [6, 10], False),
        ("2026-06-18", "2026-06-30", [6, 10], False),
        ("2025-06-01", "2026-10-20", [6, 10], False),
    ],
)
def test_scheduled_pair_follows_the_months(older, newer, months, expected):
    assert yields._scheduled_pair(older, newer, months) is expected


def test_uneven_june_october_pair_is_not_an_extra_payment():
    result = yields.summarize(
        [row("2025-11-01"), row("2026-06-18"), row("2026-10-20")],
        close="50",
        as_of="2026-10-31",
        frequency="semiannual",
        month_pattern=[6, 10],
    )
    assert result["extra_distribution"] is False
    assert result["cash_per_share"] == Decimal("3.0")


def test_close_november_january_pair_can_be_estimated():
    result = yields.summarize(
        [row("2025-11-18"), row("2026-01-19")],
        close="50",
        as_of="2026-10-31",
        frequency="semiannual",
        month_pattern=[1, 11],
    )
    assert result["estimate_unavailable"] is None
    assert result["estimated_annual_cash"] == Decimal("3.0")


def test_without_months_a_short_gap_is_still_an_extra_payment():
    result = yields.summarize(
        [row("2025-11-01"), row("2026-06-18"), row("2026-10-20")],
        close="50",
        as_of="2026-10-31",
        frequency="semiannual",
    )
    assert result["extra_distribution"] is True
