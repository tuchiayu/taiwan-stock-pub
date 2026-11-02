"""Official frequency and ex-dividend month provenance stay independent."""

import json

from taiwan_stock.catalog.official_profile_adapters import policy
from taiwan_stock.catalog.profile_values import distribution_frequency


def test_official_ex_dividend_schedule_wins_over_evaluation_and_history():
    profile = {
        "distribution_frequency": "quarterly",
        "distribution_month_pattern": "[3, 6, 9, 12]",  # issuer evaluation months
        "distribution_ex_dividend_schedule": json.dumps(
            {"frequency": "quarterly", "months": [1, 4, 7, 10]}
        ),
        "distribution_frequency_inferred": json.dumps(
            {"value": "quarterly", "month_pattern": [2, 5, 8, 11]}
        ),
    }
    display = distribution_frequency(profile)
    assert (display["month_pattern"], display["month_tier"]) == (
        [1, 4, 7, 10],
        "official",
    )


def test_official_frequency_can_use_matching_historical_ex_dividend_months():
    profile = {
        "distribution_frequency": "quarterly",
        "distribution_month_pattern": "[3, 6, 9, 12]",
        "distribution_frequency_inferred": json.dumps(
            {"value": "quarterly", "month_pattern": [1, 4, 7, 10]}
        ),
    }
    display = distribution_frequency(profile)
    assert display["tier"] == "official"
    assert (display["month_pattern"], display["month_tier"]) == (
        [1, 4, 7, 10],
        "inferred",
    )
    profile["distribution_frequency_inferred"] = json.dumps(
        {"value": "annual", "month_pattern": [1]}
    )
    assert distribution_frequency(profile)["month_pattern"] is None


def test_override_does_not_claim_official_months_and_malformed_months_are_hidden():
    profile = {
        "distribution_frequency_user_override": "quarterly",
        "distribution_frequency": "quarterly",
        "distribution_ex_dividend_schedule": json.dumps(
            {"frequency": "quarterly", "months": [1, 4, 7, 10]}
        ),
    }
    assert distribution_frequency(profile)["month_pattern"] is None
    profile.pop("distribution_frequency_user_override")
    profile["distribution_ex_dividend_schedule"] = json.dumps(
        {"frequency": "quarterly", "months": [1, 4, 4, 10]}
    )
    assert distribution_frequency(profile)["month_pattern"] is None


def test_official_nonuniform_ex_dividend_months_keep_the_semiannual_label():
    profile = {
        "distribution_frequency": "semiannual",
        "distribution_frequency_source": json.dumps({"raw": "每年2次"}),
        "distribution_ex_dividend_schedule": json.dumps(
            {"frequency": "semiannual", "months": [3, 10]}
        ),
    }
    display = distribution_frequency(profile)
    # Issue #143: "每年2次" is still 半年配; the uneven months show the gap.
    assert display["label"] == "半年配"
    assert display["month_pattern"] == [3, 10]
    assert display["month_tier"] == "official"


def test_nonuniform_historical_months_are_not_accepted():
    profile = {
        "distribution_frequency": "semiannual",
        "distribution_frequency_inferred": json.dumps(
            {"value": "semiannual", "month_pattern": [3, 10]}
        ),
    }
    assert distribution_frequency(profile)["month_pattern"] is None


def test_explicit_accumulating_class_is_non_distributing():
    assert policy("累積型") == {"distribution_policy": "non_distributing"}


def test_official_twice_yearly_frequency_uses_confirmed_nonuniform_history():
    profile = {
        "distribution_frequency": "semiannual",
        "distribution_frequency_source": json.dumps({"raw": "一年兩次配息"}),
    }
    dates = [
        "2024-03-18",
        "2024-10-17",
        "2025-03-18",
        "2025-10-20",
        "2026-03-17",
        "2026-10-19",
    ]
    events = [{"ex_dividend_date": day} for day in dates]
    units = {
        ("e1", "twse_etf_distribution_list", str(year)): {
            "status": "success",
            "attempted_at": "2026-10-26T00:00:00+00:00",
        }
        for year in (2024, 2025)
    }
    kwargs = {
        "events": events,
        "units": units,
        "security_id": "e1",
        "market": "TWSE",
        "as_of": "2026-10-26",
    }
    display = distribution_frequency(profile, **kwargs)
    assert display["label"] == "半年配"
    assert (display["month_pattern"], display["month_tier"]) == ([3, 10], "inferred")
    assert (
        distribution_frequency(profile, **{**kwargs, "units": {}})["month_pattern"]
        is None
    )
    events.append({"ex_dividend_date": "2026-09-01"})
    assert distribution_frequency(profile, **kwargs)["month_pattern"] is None
