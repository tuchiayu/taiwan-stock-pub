"""Unit tests for design 004 §4.4.1's frequency-inference algorithm —
pure logic over a real (migrated) SQLite connection, no network.
"""

import json

import httpx2
import pytest

from taiwan_stock.catalog import (
    frequency_inference,
    operations,
    sync,
    sync_etf_distributions,
    sync_history,
)
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    yield connection
    connection.close()


def _seed_etf(conn, *, code="0050", name="元大台灣50", market="TWSE"):
    security_id = sync.upsert_security(
        conn,
        {
            "market": market,
            "code": code,
            "name": name,
            "security_type": "etf",
            "industry": None,
            "transfer_agent_name": None,
            "transfer_agent_phone": None,
            "status": "active",
        },
    )
    sync.upsert_etf_profile(
        conn,
        security_id,
        {"custodian_institution_raw": None, "fee_summary": None, "benchmark": None},
    )
    conn.commit()
    return security_id


def _add_event(conn, security_id, ex_dividend_date):
    sync.upsert_distribution(
        conn,
        security_id,
        {
            "ex_dividend_date": ex_dividend_date,
            "amount_per_share": "1.0",
            "amount_status": "final",
        },
    )
    conn.commit()


@pytest.mark.parametrize(
    "dates, expected_value, expected_month_pattern",
    [
        (["2026-07-15", "2026-08-15"], "monthly", list(range(1, 13))),
        (["2026-04-15", "2026-07-15"], "quarterly", [1, 4, 7, 10]),
    ],
)
def test_infer_classifies_single_gap_intervals_from_two_events(
    conn, dates, expected_value, expected_month_pattern
):
    # Monthly/quarterly have no shorter tracked frequency that a single
    # skipped payment could produce their interval from, so two known
    # events are enough — unlike bimonthly/semiannual/annual below.
    security_id = _seed_etf(conn)
    for d in dates:
        _add_event(conn, security_id, d)

    result = frequency_inference.infer_for_security(conn, security_id)

    assert result["value"] == expected_value
    assert result["month_pattern"] == expected_month_pattern
    assert result["basis_dates"] == dates


@pytest.mark.parametrize(
    "dates, expected_value, expected_month_pattern",
    [
        (["2026-01-15", "2026-03-15", "2026-05-15"], "bimonthly", [1, 3, 5, 7, 9, 11]),
        (["2025-02-15", "2025-08-15", "2026-02-15"], "semiannual", [2, 8]),
        (["2024-04-18", "2025-04-23", "2026-04-20"], "annual", [4]),
    ],
)
def test_infer_classifies_confirmed_gap_intervals_from_three_events(
    conn, dates, expected_value, expected_month_pattern
):
    # Real case (0052, 2026-09-13): three clean, evenly-spaced annual
    # events with every spanned year's TWSE sync confirmed `success` —
    # a textbook case the old blanket "12 is never annual" rule wrongly
    # reported as insufficient data. Bimonthly/semiannual need the same
    # 2-confirmed-gaps treatment (design 005 §4.4 follow-up) since each
    # is exactly "next shorter tracked frequency, one skip".
    security_id = _seed_etf(conn)
    for d in dates:
        _add_event(conn, security_id, d)
    years = range(int(dates[0][:4]), int(dates[-1][:4]) + 1)
    units = {
        (security_id, "twse_etf_distribution_list", str(y)): {"status": "success"}
        for y in years
    }

    result = frequency_inference.infer_for_security(
        conn, security_id, market="TWSE", units=units
    )

    assert result["value"] == expected_value
    assert result["month_pattern"] == expected_month_pattern
    assert result["basis_dates"] == dates[1:]


def test_infer_returns_none_for_annual_interval_without_coverage_info(conn):
    # Design 005 §4.4 follow-up: a 12-month interval *can* be classified
    # "annual" (see the coverage-gated tests above), but only once
    # `sync_history` confirms every spanned year was fully synced —
    # callers that don't pass `market`/`units` at all (as before this
    # was supported) still get the original, conservative "insufficient
    # data" behavior rather than a guess.
    security_id = _seed_etf(conn)
    _add_event(conn, security_id, "2024-08-15")
    _add_event(conn, security_id, "2025-08-15")
    _add_event(conn, security_id, "2026-08-15")

    assert frequency_inference.infer_for_security(conn, security_id) is None


def test_infer_does_not_guess_confirmed_gap_frequency_with_only_two_events(conn):
    # A single 12-month gap alone — even with `market`/`units` supplied
    # — is exactly the "could be a skipped semiannual payment" case
    # design 005 §4.4 exists to guard against; a second confirming gap
    # (a third event) is required, not just sync completeness.
    security_id = _seed_etf(conn)
    _add_event(conn, security_id, "2025-04-23")
    _add_event(conn, security_id, "2026-04-20")
    units = {
        (security_id, "twse_etf_distribution_list", "2025"): {"status": "success"},
        (security_id, "twse_etf_distribution_list", "2026"): {"status": "success"},
    }

    result = frequency_inference.infer_for_security(
        conn, security_id, market="TWSE", units=units
    )

    assert result is None


def test_full_sync_backfills_0051_once_before_annual_inference(conn, monkeypatch):
    security_id = _seed_etf(conn, code="0051", name="元大中型100")
    _add_event(conn, security_id, "2024-11-18")
    _add_event(conn, security_id, "2025-11-21")
    monkeypatch.setattr(sync_etf_distributions, "_ETF_DISTRIBUTION_DELAY_SECONDS", 0)
    fields = [
        "證券代號",
        "證券簡稱",
        "除息交易日",
        "收益分配基準日",
        "收益分配發放日",
        "收益分配金額 (每1受益權益單位)",
        "收益分配標準 (102年度起啟用)",
        "公告年度",
    ]
    dates = {2023: "112年11月16日", 2024: "113年11月18日", 2025: "114年11月21日"}
    requested = []

    def handler(request):
        assert request.url.path.endswith("/rwd/zh/ETF/etfDiv")
        year = int(request.url.params["startDate"])
        requested.append(year)
        rows = []
        if year in dates:
            rows = [
                [
                    "0051",
                    "元大中型100",
                    dates[year],
                    dates[year],
                    dates[year],
                    "1.0",
                    "…",
                    year - 1911,
                ]
            ]
        return httpx2.Response(
            200, json={"status": "ok", "fields": fields, "data": rows}
        )

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    first = operations.execute(
        conn, client, "full-etf-distributions", {"reference_year": 2026}
    )
    inferred = json.loads(
        conn.execute(
            "SELECT distribution_frequency_inferred FROM etf_profiles "
            "WHERE security_id=?",
            (security_id,),
        ).fetchone()[0]
    )
    # Design 076: the window's never-confirmed previous year (2023) is now
    # part of the main pass, so the separate annual backfill has nothing left.
    assert "annual_backfill" not in first
    assert first["twse"]["synced"] == 3
    assert inferred["value"] == "annual"
    assert requested == [2023, 2024, 2025, 2026]

    requested.clear()
    second = operations.execute(
        conn, client, "full-etf-distributions", {"reference_year": 2026}
    )
    assert "annual_backfill" not in second
    assert requested == [2024, 2025, 2026]


def test_full_sync_backfills_006201_from_tpex(conn, monkeypatch):
    security_id = _seed_etf(conn, code="006201", name="元大富櫃50", market="TPEx")
    _add_event(conn, security_id, "2024-11-18")
    _add_event(conn, security_id, "2025-11-21")
    monkeypatch.setattr(sync_etf_distributions, "_ETF_DISTRIBUTION_DELAY_SECONDS", 0)
    dates = {
        2023: "112年11月16日",
        2024: "113年11月18日",
        2025: "114年11月21日",
    }
    requested = []

    def handler(request):
        assert request.url.path.endswith("/api/etfExDiv")
        params = dict(httpx2.QueryParams(request.read().decode()))
        year = int(params["startDate"][:4])
        requested.append(year)
        rows = (
            [
                {
                    "stockNo": "006201",
                    "divDate": dates[year],
                    "inBaseDate": dates[year],
                    "inDate": dates[year],
                    "amount": "1.0",
                }
            ]
            if year in dates
            else []
        )
        return httpx2.Response(200, json=rows)

    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    result = operations.execute(
        conn, client, "full-etf-distributions", {"reference_year": 2026}
    )
    assert "annual_backfill" not in result, result  # design 076: main pass
    assert result["tpex"]["synced"] == 3, result
    inferred = json.loads(
        conn.execute(
            "SELECT distribution_frequency_inferred FROM etf_profiles "
            "WHERE security_id=?",
            (security_id,),
        ).fetchone()[0]
    )
    assert inferred["value"] == "annual"
    assert requested == [2023, 2024, 2025, 2026]


def test_infer_does_not_guess_annual_when_a_spanned_year_is_incomplete(conn):
    # Same doubly-confirmed 12-month gaps, but one of the three spanned
    # years' sync never confirmed success (still pending/failed) — the
    # second gap rules out a skipped payment, but not a sync gap that
    # swallowed a real intermediate event, so this must still report
    # insufficient data. Design 046: the unsynced year is the middle one,
    # the only full year after the first payment, so the full-year rule
    # has no trusted year either (an unsynced 2026 alone would no longer
    # matter: 2025 is a full, synced year with one payment).
    security_id = _seed_etf(conn)
    _add_event(conn, security_id, "2024-04-18")
    _add_event(conn, security_id, "2025-04-23")
    _add_event(conn, security_id, "2026-04-20")
    units = {
        (security_id, "twse_etf_distribution_list", "2024"): {"status": "success"},
        (security_id, "twse_etf_distribution_list", "2025"): {"status": "pending"},
        (security_id, "twse_etf_distribution_list", "2026"): {"status": "success"},
    }

    result = frequency_inference.infer_for_security(
        conn, security_id, market="TWSE", units=units
    )

    assert result is None


def test_infer_does_not_guess_when_the_second_gap_does_not_match(conn):
    # Three events, but the two gaps differ (12 then 6 months) — a real
    # skipped-payment or policy-change case, not a confirmed repeating
    # annual cadence; must not classify off the more recent gap alone.
    security_id = _seed_etf(conn)
    _add_event(conn, security_id, "2024-04-18")  # 12 months before the next
    _add_event(conn, security_id, "2025-04-23")  # 12 months before the next
    _add_event(conn, security_id, "2026-04-20")
    units = {
        (security_id, "twse_etf_distribution_list", str(y)): {"status": "success"}
        for y in (2024, 2025, 2026)
    }
    # Confirm the doubly-matching case works first (sanity check), then
    # break just the older gap and confirm it stops classifying.
    assert (
        frequency_inference.infer_for_security(
            conn, security_id, market="TWSE", units=units
        )["value"]
        == "annual"
    )

    conn.execute(
        "UPDATE distributions SET ex_dividend_date = '2024-10-18' "
        "WHERE security_id = ? AND ex_dividend_date = '2024-04-18'",
        (security_id,),
    )
    conn.commit()

    assert (
        frequency_inference.infer_for_security(
            conn, security_id, market="TWSE", units=units
        )
        is None
    )


def test_infer_classifies_monthly_across_a_calendar_month_boundary(conn):
    # Issue #19 / real case (00984D, 2026-09-14): the pay date drifts
    # near month boundaries, so two truly-monthly events can land in
    # non-adjacent calendar months (07-31 -> 09-01 is 32 real days, but
    # naive year/month subtraction used to count that as a 2-month
    # gap). Classification must follow the actual day count, not the
    # calendar-month arithmetic.
    security_id = _seed_etf(conn)
    _add_event(conn, security_id, "2026-07-31")
    _add_event(conn, security_id, "2026-09-01")

    result = frequency_inference.infer_for_security(conn, security_id)

    assert result["value"] == "monthly"
    assert result["month_pattern"] == list(range(1, 13))
    assert result["basis_dates"] == ["2026-07-31", "2026-09-01"]


def test_infer_returns_none_for_unrecognized_interval(conn):
    security_id = _seed_etf(conn)
    _add_event(conn, security_id, "2026-03-15")
    _add_event(conn, security_id, "2026-08-15")  # 5-month gap, not classified

    assert frequency_inference.infer_for_security(conn, security_id) is None


def test_infer_returns_none_with_fewer_than_two_events(conn):
    security_id = _seed_etf(conn)
    _add_event(conn, security_id, "2026-08-15")

    assert frequency_inference.infer_for_security(conn, security_id) is None


def test_infer_uses_only_the_two_most_recent_events_not_full_history(conn):
    # Design 004 §4.4.1's 兆豐 counter-example: a policy change from
    # semiannual to quarterly must be picked up from the latest two
    # events, not blended with older history into a bogus frequency.
    security_id = _seed_etf(conn)
    _add_event(conn, security_id, "2024-01-15")  # old semiannual policy
    _add_event(conn, security_id, "2024-07-15")
    _add_event(conn, security_id, "2025-04-15")  # new quarterly policy
    _add_event(conn, security_id, "2025-07-15")

    result = frequency_inference.infer_for_security(conn, security_id)

    assert result["value"] == "quarterly"
    assert result["basis_dates"] == ["2025-04-15", "2025-07-15"]


def test_recompute_all_writes_json_and_clears_stale_guess_when_insufficient(conn):
    quarterly_id = _seed_etf(conn, code="0050", name="元大台灣50")
    _add_event(conn, quarterly_id, "2026-04-15")
    _add_event(conn, quarterly_id, "2026-07-15")

    unknown_id = _seed_etf(conn, code="00878", name="國泰永續高股息")
    _add_event(conn, unknown_id, "2026-08-15")  # only one event

    result = frequency_inference.recompute_all(conn)

    assert result == {"updated": 1, "insufficient_data": 1}
    quarterly_row = conn.execute(
        "SELECT distribution_frequency_inferred FROM etf_profiles "
        "WHERE security_id = ?",
        (quarterly_id,),
    ).fetchone()
    assert json.loads(quarterly_row["distribution_frequency_inferred"])["value"] == (
        "quarterly"
    )
    unknown_row = conn.execute(
        "SELECT distribution_frequency_inferred FROM etf_profiles "
        "WHERE security_id = ?",
        (unknown_id,),
    ).fetchone()
    assert unknown_row["distribution_frequency_inferred"] is None


def test_recompute_all_reverts_a_stale_guess_when_data_no_longer_supports_it(conn):
    # A previous run may have written a guess from events that are no
    # longer the most recent two (e.g. a data correction) — the next
    # recompute must overwrite it, not leave it frozen (§4.4.1).
    security_id = _seed_etf(conn)
    conn.execute(
        "UPDATE etf_profiles SET distribution_frequency_inferred = ? "
        "WHERE security_id = ?",
        (json.dumps({"value": "monthly"}), security_id),
    )
    conn.commit()
    _add_event(conn, security_id, "2026-08-15")  # now only one usable event

    frequency_inference.recompute_all(conn)

    row = conn.execute(
        "SELECT distribution_frequency_inferred FROM etf_profiles "
        "WHERE security_id = ?",
        (security_id,),
    ).fetchone()
    assert row["distribution_frequency_inferred"] is None


def test_recompute_all_classifies_annual_using_real_sync_history(conn):
    # End-to-end version of the 0052 case, through `recompute_all`'s own
    # `sync_history.latest_units` lookup rather than a hand-built dict.
    security_id = _seed_etf(conn, code="0052", name="富邦科技")
    _add_event(conn, security_id, "2024-04-18")
    _add_event(conn, security_id, "2025-04-23")
    _add_event(conn, security_id, "2026-04-20")
    with sync_history.run(conn, "twse_etf_distribution_list") as task:
        for period in ("2024", "2025", "2026"):
            key = f"0052:{period}"
            task.plan(key, security_id=security_id, period=period)
            task.record(key, "success", count=1)

    frequency_inference.recompute_all(conn)

    row = conn.execute(
        "SELECT distribution_frequency_inferred FROM etf_profiles "
        "WHERE security_id = ?",
        (security_id,),
    ).fetchone()
    assert json.loads(row["distribution_frequency_inferred"])["value"] == "annual"


def _units(security_id, *years):
    return {
        (security_id, "twse_etf_distribution_list", str(y)): {"status": "success"}
        for y in years
    }


def test_full_year_confirms_annual_from_two_payments(conn):
    # Design 046 §4.2.1: listed in 2024, the full year 2025 had one payment.
    security_id = _seed_etf(conn)
    _add_event(conn, security_id, "2024-11-20")
    _add_event(conn, security_id, "2025-11-21")
    result = frequency_inference.infer_for_security(
        conn,
        security_id,
        market="TWSE",
        units=_units(security_id, 2025),
        listed_date="2024-01-10",
        today="2026-10-29",
    )
    assert (result["value"], result["method"]) == ("annual", "year_count")
    assert (result["basis_year"], result["year_count"]) == (2025, 1)


def test_full_year_needs_listing_before_the_year(conn):
    security_id = _seed_etf(conn)
    _add_event(conn, security_id, "2024-11-20")
    _add_event(conn, security_id, "2025-11-21")
    result = frequency_inference.infer_for_security(
        conn,
        security_id,
        market="TWSE",
        units=_units(security_id, 2025),
        listed_date="2025-02-01",
        today="2026-10-29",
    )
    assert result is None


def test_first_payout_year_is_never_the_full_year(conn):
    # Semiannual, first paid in late 2025: 2025 must not read as annual.
    security_id = _seed_etf(conn)
    _add_event(conn, security_id, "2025-10-15")
    _add_event(conn, security_id, "2026-04-15")
    result = frequency_inference.infer_for_security(
        conn,
        security_id,
        market="TWSE",
        units=_units(security_id, 2025, 2026),
        listed_date="2024-06-01",
        today="2026-10-29",
    )
    assert result is None


def test_quarterly_with_one_extra_payment_is_not_bimonthly(conn):
    security_id = _seed_etf(conn)
    for day in (
        "2024-10-15",
        "2025-01-15",
        "2025-04-15",
        "2025-07-15",
        "2025-08-20",
        "2025-10-15",
        "2026-01-15",
    ):
        _add_event(conn, security_id, day)
    result = frequency_inference.infer_for_security(
        conn,
        security_id,
        market="TWSE",
        units=_units(security_id, 2025),
        today="2026-10-29",
    )
    assert (result["value"], result["year_count"]) == ("quarterly", 5)


def test_monthly_drifting_across_month_ends_counts_twelve(conn):
    security_id = _seed_etf(conn)
    days = ["2024-12-16"] + [f"2025-{m:02d}-16" for m in range(1, 7)]
    days += ["2025-07-01", "2025-07-30", "2025-09-02", "2025-10-01"]
    days += ["2025-11-03", "2025-12-01", "2026-01-02"]
    for day in days:
        _add_event(conn, security_id, day)
    result = frequency_inference.infer_for_security(
        conn,
        security_id,
        market="TWSE",
        units=_units(security_id, 2025),
        today="2026-10-29",
    )
    assert (result["value"], result["year_count"]) == ("monthly", 12)


@pytest.mark.parametrize(
    ("days", "months"),
    [
        (["2024-07-10", "2025-01-10", "2025-07-10", "2026-01-10"], [1, 7]),
        (["2024-11-10", "2025-07-10", "2025-11-10"], [7, 11]),
    ],
)
def test_full_year_semiannual_keeps_its_own_months(conn, days, months):
    # Design 046: twice a year need not be six months apart.
    security_id = _seed_etf(conn)
    for day in days:
        _add_event(conn, security_id, day)
    result = frequency_inference.infer_for_security(
        conn,
        security_id,
        market="TWSE",
        units=_units(security_id, 2025),
        today="2026-10-29",
    )
    assert (result["value"], result["method"]) == ("semiannual", "year_count")
    assert result["month_pattern"] == months
