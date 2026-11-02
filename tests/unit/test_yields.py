"""Unit tests for the cash/annualized yield helpers (design 002 §4.8.1, §4.8.4)."""

from datetime import date
from decimal import Decimal

from taiwan_stock.portfolio.yields import add_months, summarize


def test_add_months_clamps_to_shorter_month():
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)  # 2024 is a leap year
    assert add_months(date(2023, 1, 31), 1) == date(2023, 2, 28)


def test_add_months_handles_negative_and_year_rollover():
    assert add_months(date(2024, 3, 31), -1) == date(2024, 2, 29)
    assert add_months(date(2024, 1, 15), -2) == date(2023, 11, 15)


def test_summarize_with_no_distributions_yields_zero_not_none():
    result = summarize([], close="100", as_of="2026-01-01")

    assert result["cash_per_share"] == Decimal(0)
    assert result["known_yield"] == Decimal(0)
    assert result["estimated_annual_cash"] is None
    assert result["estimated_yield"] is None
    assert result["provisional"] is False
    assert result["observed_count"] == 0


def test_summarize_excludes_distributions_outside_the_12_month_window():
    rows = [
        {
            "ex_dividend_date": "2024-12-31",
            "amount_per_share": "5",
            "amount_status": "final",
        },
        {
            "ex_dividend_date": "2025-06-01",
            "amount_per_share": "3",
            "amount_status": "final",
        },
        {
            "ex_dividend_date": "2026-02-01",
            "amount_per_share": "9",
            "amount_status": "final",
        },
    ]

    result = summarize(rows, close="100", as_of="2026-01-01")

    # window is (2025-01-01, 2026-01-01]: only the 2025-06-01 row qualifies
    assert result["cash_per_share"] == Decimal("3")
    assert result["observed_count"] == 1


def test_summarize_known_yield_mixes_final_and_estimated_but_flags_provisional():
    rows = [
        {
            "ex_dividend_date": "2025-06-01",
            "amount_per_share": "3",
            "amount_status": "final",
        },
        {
            "ex_dividend_date": "2025-09-01",
            "amount_per_share": "2",
            "amount_status": "estimated",
        },
    ]

    result = summarize(rows, close="100", as_of="2026-01-01")

    assert result["cash_per_share"] == Decimal("5")
    assert result["known_yield"] == Decimal("5")
    assert result["provisional"] is True
    # only the 'final' row counts as an observed sample for annualization
    assert result["observed_count"] == 2


def test_summarize_without_a_frequency_cannot_annualize():
    rows = [
        {
            "ex_dividend_date": "2025-06-01",
            "amount_per_share": "5",
            "amount_status": "final",
        }
    ]

    result = summarize(rows, close="100", as_of="2026-01-01")

    assert result["estimated_annual_cash"] is None
    assert result["estimate_basis"] is None
    assert result["estimate_unavailable"] == "unknown_frequency"


def test_summarize_annualizes_a_short_history_using_the_given_frequency():
    rows = [
        {
            "ex_dividend_date": "2025-04-01",
            "amount_per_share": "1",
            "amount_status": "final",
        },
        {
            "ex_dividend_date": "2025-08-01",
            "amount_per_share": "1",
            "amount_status": "final",
        },
        {
            "ex_dividend_date": "2025-12-01",
            "amount_per_share": "1",
            "amount_status": "final",
        },
    ]

    result = summarize(rows, close="100", as_of="2026-01-01", frequency="monthly")

    # design 028 §4.4: A_est = latest final payment * f
    assert result["estimate_basis"]["ex_dividend_date"] == "2025-12-01"
    assert result["estimate_periods"] == 12
    assert result["estimated_annual_cash"] == Decimal("12")
    assert result["estimated_yield"] == Decimal("12")


def _row(ex_date, amount="1", status="final"):
    return {
        "ex_dividend_date": ex_date,
        "amount_per_share": amount,
        "amount_status": status,
    }


def test_known_frequency_takes_latest_event_past_the_12_month_edge():
    # Design 023 §4.6: an annual payer last paid 2025-10-09; on 2026-10-15
    # the 12-month window starts 2025-10-15 and used to find nothing.
    result = summarize(
        [_row("2024-10-11", "9"), _row("2025-10-09", "2")],
        close="100",
        as_of="2026-10-15",
        frequency="annual",
    )

    assert result["basis"] == "by_count"
    assert result["cash_per_share"] == Decimal("2")
    assert result["known_yield"] == Decimal("2")
    assert result["overdue"] is False  # 371 days ≤ 385
    assert result["required_from_year"] == 2025


def test_known_frequency_fills_a_quarter_pushed_out_of_the_window():
    rows = [
        _row("2025-10-10"),
        _row("2026-01-16"),
        _row("2026-04-16"),
        _row("2026-07-16"),
    ]

    result = summarize(rows, close="100", as_of="2026-10-15", frequency="quarterly")

    assert result["cash_per_share"] == Decimal("4")
    assert [r["ex_dividend_date"] for r in result["selected"]][0] == "2025-10-10"


def test_earlier_ex_date_is_not_counted_twice():
    # 359 days apart is still one annual cycle, not an extra payment.
    result = summarize(
        [_row("2025-10-20", "3"), _row("2026-10-14", "2")],
        close="100",
        as_of="2026-10-15",
        frequency="annual",
    )

    assert result["extra_distribution"] is False
    assert result["cash_per_share"] == Decimal("2")


def test_extra_distribution_falls_back_to_the_12_month_window():
    # An official annual payer adding a payment 48 days later must not
    # have its regular payment crowded out by "latest 1".
    result = summarize(
        [_row("2025-10-14", "3"), _row("2025-12-01", "1")],
        close="100",
        as_of="2026-10-13",
        frequency="annual",
    )

    assert result["extra_distribution"] is True
    assert result["basis"] == "by_window"
    assert result["cash_per_share"] == Decimal("4")


def test_announced_but_not_yet_ex_dividend_event_is_not_counted():
    result = summarize(
        [_row("2026-04-16", "1"), _row("2026-10-20", "5")],
        close="100",
        as_of="2026-10-15",
        frequency="semiannual",
    )

    assert result["cash_per_share"] == Decimal("1")
    assert len(result["selected"]) == 1


def test_confirmed_nonuniform_semiannual_schedule_keeps_regular_estimate():
    rows = [_row("2025-10-15", "2"), _row("2026-03-16", "3")]
    result = summarize(
        rows,
        close="100",
        as_of="2026-10-26",
        frequency="semiannual",
        month_pattern=[3, 10],
    )
    assert result["estimated_annual_cash"] == Decimal("6")
    assert result["overdue"] is False

    missing_october = summarize(
        rows,
        close="100",
        as_of="2026-11-10",
        frequency="semiannual",
        month_pattern=[3, 10],
    )
    assert missing_october["overdue"] is True

    without_confirmed_months = summarize(
        rows, close="100", as_of="2026-10-26", frequency="semiannual"
    )
    assert without_confirmed_months["estimated_annual_cash"] is None


def test_overdue_boundary_uses_the_cycle_upper_bound():
    on_bound = summarize(
        [_row("2025-09-25")], close="100", as_of="2026-10-15", frequency="annual"
    )
    past_bound = summarize(
        [_row("2025-09-24")], close="100", as_of="2026-10-15", frequency="annual"
    )

    assert on_bound["overdue"] is False  # exactly 385 days
    assert past_bound["overdue"] is True
    assert past_bound["known_yield"] == Decimal("1")  # still counted


def test_unknown_frequency_keeps_the_12_month_window():
    result = summarize(
        [_row("2025-10-09"), _row("2026-03-01")], close="100", as_of="2026-10-15"
    )

    assert result["basis"] == "by_window"
    assert result["cash_per_share"] == Decimal("1")
    assert result["required_from_year"] == 2025


def test_summarize_prefers_the_latest_observed_frequency_label_over_the_argument():
    rows = [
        {
            "ex_dividend_date": "2025-06-01",
            "amount_per_share": "5",
            "amount_status": "final",
            "frequency_label": "annual",
        }
    ]

    result = summarize(rows, close="100", as_of="2026-01-01", frequency="monthly")

    assert result["frequency"] == "annual"
    assert result["estimated_annual_cash"] == Decimal("5")


def test_estimate_annualizes_the_latest_payment_not_the_average():
    # Design 028 §4.4: a rising monthly payout must not read the same as
    # the known yield; the latest payment shows the current level.
    rows = [_row(f"2025-{m:02d}-17", "0.1") for m in range(11, 13)] + [
        _row(f"2026-{m:02d}-17", "0.1") for m in range(1, 9)
    ]
    rows += [_row("2026-09-17", "0.38")]

    result = summarize(rows, close="29.84", as_of="2026-10-17", frequency="monthly")

    assert result["estimate_basis"]["ex_dividend_date"] == "2026-09-17"
    assert result["estimated_annual_cash"] == Decimal("4.56")
    assert result["estimated_yield"].quantize(Decimal("0.01")) == Decimal("15.28")
    assert result["known_yield"] != result["estimated_yield"]


def test_estimate_skips_estimated_amounts_for_the_latest_final():
    result = summarize(
        [_row("2026-07-16", "1"), _row("2026-10-15", "3", status="estimated")],
        close="100",
        as_of="2026-10-17",
        frequency="quarterly",
    )

    assert result["estimate_basis"]["ex_dividend_date"] == "2026-07-16"
    assert result["estimated_annual_cash"] == Decimal("4")


def test_estimate_refuses_a_latest_payment_closer_than_one_cycle():
    result = summarize(
        [_row("2026-07-16", "1"), _row("2026-08-10", "5")],
        close="100",
        as_of="2026-10-17",
        frequency="quarterly",
    )

    assert result["estimate_basis"] is None
    assert result["estimated_yield"] is None
    assert result["estimate_unavailable"] == "extra_distribution"


def test_estimate_needs_a_final_amount():
    result = summarize(
        [_row("2026-07-16", None)],
        close="100",
        as_of="2026-10-17",
        frequency="quarterly",
    )

    assert result["estimate_unavailable"] == "no_final"
    assert result["estimated_annual_cash"] is None


def test_estimate_without_a_close_keeps_the_annual_cash():
    result = summarize(
        [_row("2026-07-16", "1")], close=None, as_of="2026-10-17", frequency="quarterly"
    )

    assert result["estimated_annual_cash"] == Decimal("4")
    assert result["estimated_yield"] is None
