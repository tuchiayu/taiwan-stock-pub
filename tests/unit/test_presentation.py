"""Display rounding and navigation must not alter persisted values."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from taiwan_stock.web.presentation import local_return, number, taipei_time, with_query


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "—"),
        ("", "—"),
        (0, "0"),
        ("0.00", "0"),
        (Decimal("1234567.125"), "1,234,567.13"),
        ("-0.001", "0"),
        ("NaN", "—"),
        ("Infinity", "—"),
        ("bad", "—"),
    ],
)
def test_number_formats_zero_missing_and_half_up(value, expected):
    assert number(value) == expected


def test_fixed_percent_and_integer_shares():
    assert number("1.2", 2, True) == "1.20"
    assert number(1000, 0) == "1,000"


@pytest.mark.parametrize(
    "value",
    [
        "//evil.test",
        "https://evil.test",
        "/\\evil.test",
        "/\n/evil",
        "https://[",
        "relative",
    ],
)
def test_invalid_return_uses_fallback(value):
    assert local_return(value) == "/transactions"


def test_return_keeps_filter_fragment_and_replaces_query():
    assert (
        with_query("/transactions?q=0050&page=2#history", page=3, saved="trade")
        == "/transactions?q=0050&page=3&saved=trade#history"
    )


def test_per_share_distribution_preserves_source_precision():
    assert number("0.035", None) == "0.035"
    assert number("7.0000", None) == "7.0000"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-10-31T02:01:25.267Z", "2026-10-31 10:01"),
        ("2026-10-31T16:05:00+00:00", "2026-11-01 00:05"),  # crosses midnight
        ("2026-10-31T02:01:25", "2026-10-31 10:01"),  # naive means UTC
        (datetime(2026, 10, 31, 2, 1, tzinfo=UTC), "2026-10-31 10:01"),
        (None, "—"),
        ("", "—"),
        ("尚未取得", "尚未取得"),
    ],
)
def test_taipei_time_shows_stored_utc_as_taipei_minutes(value, expected):
    """Design 051 §4.2.3."""
    assert taipei_time(value) == expected
