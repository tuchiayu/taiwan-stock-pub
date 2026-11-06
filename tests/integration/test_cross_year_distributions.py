"""Design 076: the sources file an early-January ex-date under the previous
year (verified live 2026-11-05: 00939 and 00950B list 2026-01-02 under 2025)."""

from contextlib import closing
from unittest.mock import patch

import httpx2
import pytest

from taiwan_stock.catalog import (
    operations,
    sync_etf_distributions,
    sync_history,
    twse_adapter,
)
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection

SOURCE = "twse_etf_distribution_list"


@pytest.fixture
def conn(tmp_path):
    with closing(get_connection(tmp_path)) as conn:
        migrate.apply_pending(conn)
        conn.execute(
            "INSERT INTO securities(id,market,code,name,security_type,status) "
            "VALUES('s','TWSE','00939','Synthetic','etf','active')"
        )
        yield conn


def _roc(iso):
    return f"{int(iso[:4]) - 1911}年{iso[5:7]}月{iso[8:]}日"


class Source:
    """TWSE `etfDiv` answering each queried year with the given ex-dates."""

    def __init__(self, years):
        self.years = years
        self.requests = []

    def handler(self, request):
        year = int(request.url.params["startDate"])
        self.requests.append(year)
        data = [
            ["00939", _roc(ex), "", _roc(ex), "0.07"] for ex in self.years.get(year, [])
        ]
        fields = list(twse_adapter._ETF_DISTRIBUTION_FIELD_NAMES.values())
        return httpx2.Response(200, json={"fields": fields, "data": data})


def _sync(conn, source, years):
    with (
        httpx2.Client(transport=httpx2.MockTransport(source.handler)) as client,
        patch.object(sync_etf_distributions, "_ETF_DISTRIBUTION_DELAY_SECONDS", 0),
    ):
        return sync_etf_distributions.sync_etf_distributions(
            conn, client, year_list=years
        )


def _rows(conn):
    return {
        r["ex_dividend_date"]: r["reconciliation_status"]
        for r in conn.execute(
            "SELECT ex_dividend_date, reconciliation_status FROM distributions"
        )
    }


def _store(conn, ex_date):
    conn.execute(
        "INSERT INTO distributions(id,security_id,ex_dividend_date,amount_per_share,"
        "amount_status,source) VALUES(?, 's', ?, '0.07', 'final', ?)",
        (ex_date, ex_date, SOURCE),
    )


@pytest.mark.parametrize(
    ("ex_date", "accepted"),
    [
        ("2026-01-02", True),
        ("2026-01-31", True),
        ("2026-02-02", False),
        ("2024-12-31", False),
        ("2027-01-02", False),
    ],
)
def test_a_year_accepts_only_next_january(conn, ex_date, accepted):
    result = _sync(conn, Source({2025: ["2025-12-02", ex_date]}), [2025])

    assert (result["failed_units"] == []) is accepted
    if accepted:
        assert set(_rows(conn)) == {"2025-12-02", ex_date}
    else:
        report = sync_history.get_report(conn, result["run_id"])
        assert report["units"][0]["reason"] == "除息日不在查詢年度"
        assert _rows(conn) == {}


def test_live_shape_stores_last_year_and_this_january(conn):
    source = Source(
        {
            2025: ["2025-11-03", "2025-12-02", "2026-01-02"],
            2026: ["2026-02-02", "2026-11-03"],
        }
    )

    result = _sync(conn, source, [2025, 2026])

    assert result["failed_units"] == []
    assert _rows(conn) == dict.fromkeys(
        ["2025-11-03", "2025-12-02", "2026-01-02", "2026-02-02", "2026-11-03"]
    )


def test_resyncing_this_year_does_not_flag_a_january_from_last_years_list(conn):
    _store(conn, "2026-01-02")
    _store(conn, "2026-03-02")

    _sync(conn, Source({2026: ["2026-02-02"]}), [2026])

    rows = _rows(conn)
    assert rows["2026-01-02"] is None
    assert rows["2026-03-02"] == "needs_review"


def test_january_is_flagged_only_when_both_years_lack_it(conn):
    _store(conn, "2026-01-02")
    _store(conn, "2026-01-22")

    _sync(conn, Source({2025: ["2026-01-02"], 2026: ["2026-02-02"]}), [2025, 2026])

    rows = _rows(conn)
    assert rows["2026-01-02"] is None
    assert rows["2026-01-22"] == "needs_review"
    note = conn.execute(
        "SELECT reconciliation_note FROM distributions WHERE id='2026-01-22'"
    ).fetchone()[0]
    assert note == "2025、2026 年度重新同步時，此除息日皆未出現於來源回應"


def test_a_failed_previous_year_never_flags_january(conn, monkeypatch):
    _store(conn, "2026-01-22")
    source = Source({2026: ["2026-02-02"]})
    real = source.handler

    def failing(request):
        if request.url.params["startDate"] == "2025":
            return httpx2.Response(200, json={"fields": ["x"], "data": [["y"]]})
        return real(request)

    source.handler = failing

    result = _sync(conn, source, [2025, 2026])

    assert result["failed_units"] == ["00939:2025"]
    assert _rows(conn)["2026-01-22"] is None


def test_plan_adds_last_year_for_a_new_database_only(conn):
    assert sync_etf_distributions.year_plan(conn, "TWSE", [2026]) == {"s": {2025, 2026}}
    _sync(conn, Source({}), [2025])

    assert sync_etf_distributions.year_plan(conn, "TWSE", [2026]) == {"s": {2026}}
    assert sync_etf_distributions.year_plan(
        conn, "TWSE", [2026], include_previous=True
    ) == {"s": {2025, 2026}}


def test_plan_rechecks_a_flagged_january_and_the_sync_clears_it(conn):
    _sync(conn, Source({}), [2025])
    _store(conn, "2026-01-02")
    conn.execute(
        "UPDATE distributions SET reconciliation_status='needs_review', "
        "reconciliation_note='2026 年度重新同步時，此除息日未再出現於來源回應'"
    )

    plan = sync_etf_distributions.year_plan(conn, "TWSE", [2026])
    assert plan == {"s": {2025, 2026}}

    source = Source({2025: ["2026-01-02"], 2026: ["2026-02-02"]})
    with (
        httpx2.Client(transport=httpx2.MockTransport(source.handler)) as client,
        patch.object(sync_etf_distributions, "_ETF_DISTRIBUTION_DELAY_SECONDS", 0),
    ):
        sync_etf_distributions.sync_etf_distributions(
            conn, client, year_list=[2025, 2026], year_map=plan
        )

    assert _rows(conn)["2026-01-02"] is None


def test_plan_keeps_delisted_etfs_to_the_base_years(conn):
    conn.execute("UPDATE securities SET status='delisted'")

    assert sync_etf_distributions.year_plan(conn, "TWSE", [2026]) == {"s": {2026}}


def test_etf_data_on_a_new_database_fetches_last_year(conn, monkeypatch):
    monkeypatch.setattr(operations, "today_taipei", lambda: "2026-11-05")
    plan = operations._etf_data_plan()

    assert plan(conn, "TWSE") == ([2025, 2026], {"s": {2025, 2026}})
    assert plan(conn, "TPEx") == ([2026], {})


def test_etf_data_in_january_always_fetches_last_year(conn, monkeypatch):
    _sync(conn, Source({}), [2026])
    monkeypatch.setattr(operations, "today_taipei", lambda: "2027-01-05")

    assert operations._etf_data_plan()(conn, "TWSE") == (
        [2026, 2027],
        {"s": {2026, 2027}},
    )


def test_cli_window_adds_the_year_before_only_when_never_confirmed(conn):
    source = Source({2023: ["2024-01-02"], 2024: ["2024-02-02"]})
    with (
        httpx2.Client(transport=httpx2.MockTransport(source.handler)) as client,
        patch.object(sync_etf_distributions, "_ETF_DISTRIBUTION_DELAY_SECONDS", 0),
        patch.object(operations.frequency_inference, "recompute_all", lambda c: None),
    ):
        options = {"years": 3, "reference_year": 2026}
        operations.execute(conn, client, "full-etf-distributions", options)
        assert source.requests == [2023, 2024, 2025, 2026]
        assert "2024-01-02" in _rows(conn)

        source.requests.clear()
        operations.execute(conn, client, "full-etf-distributions", options)
        assert source.requests == [2024, 2025, 2026]
