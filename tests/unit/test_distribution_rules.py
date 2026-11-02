"""Design 046: counting ranges, estimates and quarterly month groups."""

import json
from decimal import Decimal

import pytest

from taiwan_stock.catalog import profile_values, query, sync_history
from taiwan_stock.portfolio import yields

AS_OF = "2026-10-29"
SOURCE = "twse_etf_distribution_list"


def event(ex_date, amount="1"):
    return {
        "ex_dividend_date": ex_date,
        "amount_per_share": amount,
        "amount_status": "final",
    }


def summarize(dates, frequency):
    return yields.summarize(
        [event(d) for d in dates], close="100", as_of=AS_OF, frequency=frequency
    )


def monthly(start_year, start_month, count, day=15):
    out = []
    for i in range(count):
        year, month = divmod(start_year * 12 + start_month - 1 + i, 12)
        out.append(f"{year}-{month + 1:02d}-{day:02d}")
    return out


@pytest.mark.parametrize(
    ("frequency", "expected"),
    [
        ("monthly", "2025-10-14"),
        ("bimonthly", "2025-09-29"),
        ("quarterly", "2025-09-14"),
        ("semiannual", "2025-07-29"),
        ("annual", "2025-01-01"),
    ],
)
def test_counting_range_is_a_year_plus_half_a_cycle(frequency, expected):
    assert yields.window_start(AS_OF, frequency) == expected


# Design 046 §4.2.4 誤判檢查表, one row each.
def test_regular_quarterly_takes_the_latest_four():
    info = summarize(
        ["2025-10-15", "2026-01-15", "2026-04-15", "2026-07-15"], "quarterly"
    )
    assert info["known_yield"] == 4
    assert (info["annualized_from"], info["short_count"]) == (None, None)


def test_a_quarterly_payment_ten_days_late_still_counts():
    info = summarize(
        ["2025-09-24", "2025-12-15", "2026-03-15", "2026-06-25"], "quarterly"
    )
    assert info["known_yield"] == 4


def test_monthly_paying_twice_in_july_and_not_in_august_is_still_twelve():
    dates = monthly(2025, 10, 9) + [
        "2026-07-01",
        "2026-07-30",
        "2026-09-02",
        "2026-10-01",
    ]
    info = summarize(dates, "monthly")
    assert len(info["selected"]) == 12
    assert info["selected"][0]["ex_dividend_date"] == "2025-11-15"
    assert not info["frequency_changed"]


def test_uneven_semiannual_months_count_both_payments():
    info = summarize(["2025-07-10", "2025-11-10", "2026-07-10"], "semiannual")
    assert [r["ex_dividend_date"] for r in info["selected"]] == [
        "2025-11-10",
        "2026-07-10",
    ]
    assert info["known_yield"] == 2


@pytest.mark.parametrize("paid", ["2025-03-20", "2025-01-10"])
def test_annual_paid_last_year_counts_until_this_year_pays(paid):
    info = summarize(["2024-03-20", paid], "annual")
    assert info["known_yield"] == 1
    assert not info["no_payment_in_range"]


def test_a_new_quarterly_fund_is_annualized_from_its_first_payment():
    info = summarize(["2026-09-16"], "quarterly")
    assert info["annualized_from"] == 1
    assert info["annual_cash_per_share"] == Decimal(4)
    assert info["cash_per_share"] == Decimal(1)
    assert info["known_yield"] == 4


def test_a_fund_paying_for_years_with_fewer_in_range_is_a_real_shortfall():
    info = summarize(
        ["2024-12-15", "2025-03-15", "2025-06-15", "2025-12-15", "2026-03-15"],
        "quarterly",
    )
    assert info["short_count"] == 2
    assert info["annualized_from"] is None
    assert info["known_yield"] == 2


def test_a_quarterly_payment_far_too_late_reads_as_three_this_year():
    info = summarize(
        ["2025-05-01", "2025-08-01", "2025-11-01", "2026-02-01", "2026-05-01"],
        "quarterly",
    )
    assert info["short_count"] == 3
    assert info["known_yield"] == 3


def test_quarterly_turned_monthly_sums_the_last_twelve_months():
    dates = ["2025-07-15", "2025-10-15", "2026-01-15", "2026-04-15"]
    dates += monthly(2026, 6, 5)
    info = summarize(dates, "monthly")
    assert info["frequency_changed"]
    assert info["basis"] == "by_window"
    # 2026-01, 04, then June to October: what a holder was actually paid.
    assert info["known_yield"] == 7


def test_monthly_turned_quarterly_sums_the_last_twelve_months():
    dates = monthly(2025, 10, 8) + ["2026-08-15"]
    info = summarize(dates, "quarterly")
    assert info["frequency_changed"]
    # November to May monthly plus August: 2025-10-15 is over 12 months ago.
    assert info["known_yield"] == 8


def test_one_skipped_month_is_not_a_frequency_change():
    dates = [d for d in monthly(2025, 10, 13) if d != "2026-03-15"]
    info = summarize(dates, "monthly")
    assert not info["frequency_changed"]
    assert info["known_yield"] == 12


def test_nothing_in_range_is_flagged_for_the_zero_rule():
    info = summarize(["2023-05-01"], "annual")
    assert info["no_payment_in_range"] and info["paid_before_range"]


def _sync(*years):
    units = {
        ("e", SOURCE, str(y)): {
            "status": "success",
            "attempted_at": f"{AS_OF}T01:00:00.000Z",
        }
        for y in years
    }
    return (units, units, "e", "TWSE")


def test_zero_needs_a_fund_that_could_have_paid_and_synced_years():
    old = query.yield_summary(
        [],
        "100",
        as_of=AS_OF,
        frequency="annual",
        listed_date="2024-07-01",
        sync=_sync(2025, 2026),
    )
    assert old["known_yield"] == 0
    assert old["notes"] == ["近一年未配息"]
    assert old["quality_label"] is None
    new = query.yield_summary(
        [],
        "100",
        as_of=AS_OF,
        frequency="annual",
        listed_date="2025-06-01",
        sync=_sync(2025, 2026),
    )
    assert new["known_yield"] is None
    assert new["quality_label"] == "新上市，尚無配息紀錄"
    unsynced = query.yield_summary(
        [],
        "100",
        as_of=AS_OF,
        frequency="annual",
        listed_date="2024-07-01",
        sync=_sync(2026),
    )
    assert unsynced["known_yield"] is None
    assert "2025 年配息資料尚未同步" in unsynced["notes"]


def test_non_distributing_says_so():
    info = query.yield_summary(
        [],
        "100",
        as_of=AS_OF,
        frequency="non_distributing",
        listed_date="2015-01-01",
        sync=_sync(2025, 2026),
    )
    assert info["known_yield"] is None
    assert info["quality_label"] == "不配息"
    assert info["cash_label"] == "不配息"
    assert info["notes"] == []


def rows(*days, review=()):
    return [
        {
            "ex_dividend_date": d,
            **({"reconciliation_status": "needs_review"} if d in review else {}),
        }
        for d in days
    ]


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (["2026-04-15", "2026-07-15"], (1, "confirmed")),
        # A June payment moved from July agrees with nothing and is ignored.
        (["2026-01-15", "2026-04-15", "2026-06-30", "2026-10-15"], (1, "confirmed")),
        (["2026-09-16"], (3, "provisional")),
        (["2026-03-16", "2026-05-15"], (2, "provisional")),
        # 2–2: the latest payment's group is the current one.
        (["2025-11-15", "2026-02-15", "2026-03-31", "2026-06-30"], (3, "confirmed")),
        # Fixed first-of-month payers keep their own group.
        (["2025-11-01", "2026-02-01", "2026-05-01", "2026-08-01"], (2, "confirmed")),
        ([], (None, None)),
    ],
)
def test_quarterly_group_follows_payments_that_agree(days, expected):
    assert profile_values.quarter_group(rows(*days)) == expected


def test_quarterly_group_ignores_events_under_review():
    events = rows("2026-04-15", "2026-07-15", "2026-08-20", review=("2026-08-20",))
    assert profile_values.quarter_group(events) == (1, "confirmed")


def test_official_months_set_the_group_and_events_fill_in_otherwise():
    profile = {"distribution_frequency": "quarterly"}
    shown = profile_values.distribution_frequency(profile, events=rows("2026-09-16"))
    assert (shown["quarter_group"], shown["quarter_group_status"]) == (3, "provisional")
    assert (shown["month_pattern"], shown["month_tier"]) == (
        [3, 6, 9, 12],
        "provisional",
    )
    profile["distribution_ex_dividend_schedule"] = (
        '{"frequency":"quarterly","months":[2,5,8,11]}'
    )
    official = profile_values.distribution_frequency(profile, events=rows("2026-09-16"))
    assert (official["quarter_group"], official["month_tier"]) == (2, "official")


def test_official_frequency_disagreeing_with_a_full_year_is_flagged():
    profile = {
        "distribution_frequency": "quarterly",
        "distribution_frequency_inferred": json.dumps(
            {"value": "monthly", "method": "year_count"}
            | {"basis_year": 2025, "year_count": 12}
        ),
    }
    assert profile_values.distribution_frequency(profile)["record_mismatch"] == {
        "year": 2025,
        "count": 12,
    }
    profile["distribution_frequency_inferred"] = '{"value":"monthly","method":"gap"}'
    assert profile_values.distribution_frequency(profile)["record_mismatch"] is None


def test_one_extra_quarterly_payment_stays_an_extra_payment():
    info = summarize(
        ["2025-11-15", "2026-02-15", "2026-05-15", "2026-06-20", "2026-08-15"],
        "quarterly",
    )
    # The extra June payment splits one cycle into a monthly-length and a
    # bimonthly-length gap: no other cadence repeats, so no policy change.
    assert info["extra_distribution"]
    assert not info["frequency_changed"]


def test_uneven_semiannual_months_wait_for_two_agreeing_years():
    # One full year of 7 and 11 月 is not shown as evenly spaced months,
    # nor as uneven ones until two full years agree (design 040).
    profile = {
        "distribution_frequency": "semiannual",
        "distribution_frequency_inferred": json.dumps(
            {"value": "semiannual", "month_pattern": [7, 11], "method": "year_count"}
        ),
    }
    shown = profile_values.distribution_frequency(profile, events=rows("2025-07-10"))
    assert (shown["value"], shown["month_pattern"]) == ("semiannual", None)
    profile["distribution_frequency_inferred"] = json.dumps(
        {"value": "semiannual", "month_pattern": [1, 7], "method": "year_count"}
    )
    shown = profile_values.distribution_frequency(profile, events=rows("2025-07-10"))
    assert shown["month_pattern"] == [1, 7]


def annual(dates, **profile):
    shown = profile_values.distribution_frequency(
        {"distribution_frequency": "annual", **profile},
        events=[event(d) for d in dates],
        as_of="2026-10-31",
    )
    return shown["month_pattern"], shown["month_tier"]


@pytest.mark.parametrize(
    ("dates", "expected"),
    [
        # Design 048 §4.2.7 with the 2026-10-31 cases.
        ([], (None, None)),  # 0057: never paid
        (["2025-10-17", "2026-10-16"], ([10], "inferred")),  # 00851
        (["2024-04-18", "2026-04-20"], ([4], "inferred")),  # 00909
        (["2025-12-16"], ([12], "provisional")),  # 00971
        (["2025-01-17", "2026-02-03"], ([2], "provisional")),  # month moved
        # 00770: twice this year contradicts 年配, so no month is guessed.
        (["2024-01-17", "2025-01-17", "2026-01-20", "2026-09-16"], (None, None)),
    ],
)
def test_official_annual_month_comes_from_its_payments(dates, expected):
    assert annual(dates) == expected


def test_official_annual_months_and_review_rows():
    schedule = json.dumps({"frequency": "annual", "months": [7]})
    assert annual(["2026-10-16"], distribution_ex_dividend_schedule=schedule) == (
        [7],
        "official",
    )
    rows = [
        event("2025-10-17"),
        event("2026-03-02") | {"reconciliation_status": "needs_review"},
    ]
    shown = profile_values.distribution_frequency(
        {"distribution_frequency": "annual"}, events=rows, as_of="2026-10-31"
    )
    assert (shown["month_pattern"], shown["month_tier"]) == ([10], "provisional")
    # A stale inferred month never stands in for a contradicted year.
    inferred = json.dumps({"value": "annual", "month_pattern": [1]})
    assert annual(
        ["2025-01-17", "2026-01-20", "2026-09-16"],
        distribution_frequency_inferred=inferred,
    ) == (None, None)


def test_this_years_count_beyond_the_official_frequency_is_flagged():
    def mismatch(frequency, dates, extra=()):
        rows = [event(d) for d in dates] + list(extra)
        return profile_values.distribution_frequency(
            {"distribution_frequency": frequency}, events=rows, as_of="2026-10-31"
        )["record_mismatch"]

    assert mismatch("annual", ["2026-01-20", "2026-09-16"]) == {
        "year": 2026,
        "count": 2,
        "to_date": True,
    }
    assert mismatch("annual", ["2025-01-17", "2026-01-20"]) is None
    assert mismatch("quarterly", ["2026-01-15", "2026-04-15", "2026-07-15"]) is None
    review = event("2026-09-16") | {"reconciliation_status": "needs_review"}
    assert mismatch("annual", ["2026-01-20"], [review]) is None
    # Not yet ex-dividend: only past dates count.
    assert mismatch("annual", ["2026-01-20", "2026-12-01"]) is None
    # A full year's own count still takes precedence.
    profile = {
        "distribution_frequency": "annual",
        "distribution_frequency_inferred": json.dumps(
            {"value": "semiannual", "method": "year_count"}
            | {"basis_year": 2025, "year_count": 2}
        ),
    }
    assert profile_values.distribution_frequency(
        profile, events=[event("2026-01-20"), event("2026-09-16")], as_of="2026-10-31"
    )["record_mismatch"] == {"year": 2025, "count": 2}


def test_a_maintenance_page_is_its_own_retried_non_failure():
    """Design 055 (replacing 048's transient failure)."""
    status, _, reason = sync_history.error_info(sync_history.SourceUnavailable("x"))
    assert (status, reason) == ("source_maintenance", "來源網站維護中")
    assert not sync_history.is_failure(status)
    assert not sync_history.is_transient_failure(status)
    assert status not in sync_history.SETTLED  # so a retry still covers it
