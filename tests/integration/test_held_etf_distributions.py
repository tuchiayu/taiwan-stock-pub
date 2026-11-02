"""Integration tests for refreshing held/watched ETFs' recent
distributions (design 016)."""

import threading
from contextlib import closing
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from fastapi.testclient import TestClient
from sync_helpers import load_job

from taiwan_stock import cli
from taiwan_stock.catalog import (
    frequency_inference,
    sync,
    sync_etf_distributions,
    sync_history,
)
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.shared import pacing
from taiwan_stock.web.deps import get_http_client
from taiwan_stock.web.pages_income import _held_etf_refresh_message

TODAY = "2026-09-30"

TWSE_FIELDS = [
    "證券代號", "證券簡稱", "除息交易日", "收益分配基準日", "收益分配發放日",
    "收益分配金額 (每1受益權益單位)", "收益分配標準 (102年度起啟用)", "公告年度",
]  # fmt: skip


def _roc_dotted(iso):
    return f"{int(iso[:4]) - 1911}年{iso[5:7]}月{iso[8:]}日" if iso else ""


class FakeSources:
    """TWSE `etfDiv` / TPEx `etfExDiv`, year-aware. `events` maps a code
    to `(ex_date, pay_date, amount)` tuples; a blank amount means the
    date is announced but the amount isn't yet."""

    def __init__(self):
        self.requests: list[tuple[str, str, str]] = []
        self.status: dict[str, int] = {}
        self.events = {
            "00919": [("2026-09-16", "2026-10-15", "1.10")],
            "00939": [("2026-10-05", "2026-10-28", "")],
            "00679B": [("2026-07-15", "2026-08-12", "0.40")],
        }

    def _rows(self, code, year):
        return [e for e in self.events.get(code, []) if e[0].startswith(str(year))]

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/rwd/zh/ETF/etfDiv"):
            code = request.url.params["stkNo"]
            year = request.url.params["startDate"]
            self.requests.append(("TWSE", code, year))
            if self.status.get("TWSE", 200) != 200:
                return httpx2.Response(self.status["TWSE"])
            data = [
                [code, "名", _roc_dotted(ex), "", _roc_dotted(pay), amount, "", 115]
                for ex, pay, amount in self._rows(code, year)
            ]
            return httpx2.Response(
                200, json={"status": "ok", "fields": TWSE_FIELDS, "data": data}
            )
        if request.url.path.endswith("/api/etfExDiv"):
            form = dict(httpx2.QueryParams(request.read().decode()))
            code, year = form["stkNo"], form["startDate"][:4]
            self.requests.append(("TPEx", code, year))
            if self.status.get("TPEx", 200) != 200:
                return httpx2.Response(self.status["TPEx"])
            return httpx2.Response(
                200,
                json=[
                    {
                        "amount": amount,
                        "stockName": "名",
                        "divDate": _roc_dotted(ex),
                        "year": "115",
                        "inDate": _roc_dotted(pay),
                        "description": "",
                        "inBaseDate": "",
                        "stockNo": code,
                    }
                    for ex, pay, amount in self._rows(code, year)
                ],
            )
        return httpx2.Response(404)


def _seed(conn):
    conn.execute(
        "INSERT INTO broker_accounts (id, name, status) "
        "VALUES ('acc', '測試', 'active')"
    )
    conn.executemany(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("e-00919", "TWSE", "00919", "群益台灣精選高息", "etf", "active"),
            ("e-00939", "TWSE", "00939", "統一台灣高息動能", "etf", "active"),
            ("e-0050", "TWSE", "0050", "元大台灣50", "etf", "active"),  # untracked
            ("e-00888", "TWSE", "00888", "已下市", "etf", "delisted"),
            ("e-00679B", "TPEx", "00679B", "元大美債20年", "etf", "active"),
            ("s-2330", "TWSE", "2330", "台積電", "stock", "active"),
        ],
    )
    conn.executemany(
        "INSERT INTO trades (id, broker_account_id, security_id, event_type, "
        "trade_date, quantity, created_at) VALUES (?, 'acc', ?, 'buy', "
        "'2025-01-02', 1000, '2025-01-02T00:00:00Z')",
        [
            ("t1", "e-00919"),
            ("t2", "e-00888"),
            ("t3", "e-00679B"),
            ("t4", "s-2330"),
        ],
    )
    conn.execute("INSERT INTO watchlist (security_id) VALUES ('e-00939')")
    # Synced on 9/13, before the 投信 published the amount.
    conn.execute(
        "INSERT INTO distributions (id, security_id, ex_dividend_date, pay_date, "
        "amount_status, source) VALUES ('d-00919', 'e-00919', '2026-09-16', "
        "'2026-10-15', 'unknown', 'twse_etf_distribution_list')"
    )
    conn.commit()


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    _seed(connection)
    yield connection
    connection.close()


def _run(conn, sources, today=TODAY):
    with httpx2.Client(transport=httpx2.MockTransport(sources.handler)) as client:
        return sync_etf_distributions.refresh_held_etf_distributions(
            conn, client, today=today
        )


def _amounts(conn):
    return {
        (r["security_id"], r["ex_dividend_date"]): (
            r["amount_per_share"],
            r["amount_status"],
        )
        for r in conn.execute("SELECT * FROM distributions")
    }


def test_only_held_or_watched_etfs_are_requested_for_this_year(conn):
    sources = FakeSources()

    outcome = _run(conn, sources)

    assert sorted(sources.requests) == [
        (market, code, str(year))
        for market, code in [("TPEx", "00679B"), ("TWSE", "00919"), ("TWSE", "00939")]
        for year in (2024, 2025, 2026)
    ]
    assert outcome["errors"] == {}
    assert outcome["twse"]["etfs"] == 3  # incl. the delisted one, skipped below
    assert outcome["twse"]["years"] == [2024, 2025, 2026]
    assert outcome["tpex"]["etfs"] == 1
    units = {
        r["unit_key"]: r["status"]
        for r in conn.execute(
            "SELECT unit_key, status FROM catalog_sync_units WHERE period='2026'"
        )
    }
    assert units == {
        "00919:2026": "success",
        "00939:2026": "success",
        "00888:2026": "skipped_delisted",
        "00679B:2026": "success",
    }
    sources_run = {
        r["source"] for r in conn.execute("SELECT source FROM catalog_sync_runs")
    }
    assert sources_run == {"twse_etf_distribution_list", "tpex_etf_distribution_list"}


def test_unknown_amount_is_filled_and_new_announcement_stays_pending(conn):
    outcome = _run(conn, FakeSources())

    amounts = _amounts(conn)
    assert amounts[("e-00919", "2026-09-16")] == ("1.1000", "final")
    assert amounts[("e-00939", "2026-10-05")] == (None, "unknown")
    assert amounts[("e-00679B", "2026-07-15")] == ("0.4000", "final")
    assert outcome["twse"]["synced"] == 2
    assert outcome["pending_amounts"] == 1

    # The 投信 publishes 00939's amount; refreshing again picks it up.
    sources = FakeSources()
    sources.events["00939"] = [("2026-10-05", "2026-10-28", "0.08")]
    outcome = _run(conn, sources)

    assert _amounts(conn)[("e-00939", "2026-10-05")] == ("0.0800", "final")
    assert outcome["pending_amounts"] == 0


def test_refresh_distinguishes_new_changed_and_unchanged_events(conn, monkeypatch):
    monkeypatch.setattr(
        sync_etf_distributions, "now_utc_iso", lambda: "2026-09-30T01:00:00Z"
    )
    first = _run(conn, FakeSources())
    assert (
        first["twse"]["inserted"],
        first["twse"]["updated"],
        first["twse"]["unchanged"],
    ) == (1, 1, 0)
    assert (
        first["tpex"]["inserted"],
        first["tpex"]["updated"],
        first["tpex"]["unchanged"],
    ) == (1, 0, 0)
    assert (
        "核對 3 筆（新增 2 筆、更新 1 筆、未變更 0 筆）"
        in _held_etf_refresh_message(first)
    )
    ids = {row["id"] for row in conn.execute("SELECT id FROM distributions")}

    monkeypatch.setattr(
        sync_etf_distributions, "now_utc_iso", lambda: "2026-09-30T02:00:00Z"
    )
    second = _run(conn, FakeSources())
    assert second["twse"]["updated"] == second["twse"]["inserted"] == 0
    assert second["twse"]["unchanged"] == 2
    assert second["tpex"]["unchanged"] == 1
    assert (
        "核對 3 筆（新增 0 筆、更新 0 筆、未變更 3 筆）"
        in _held_etf_refresh_message(second)
    )
    rows = conn.execute("SELECT id, fetched_at FROM distributions").fetchall()
    assert {row["id"] for row in rows} == ids
    assert all(row["fetched_at"] == "2026-09-30T02:00:00Z" for row in rows)

    sources = FakeSources()
    sources.events["00939"] = [("2026-10-05", "2026-10-28", "0.08")]
    third = _run(conn, sources)
    assert (
        "核對 3 筆（新增 0 筆、更新 1 筆、未變更 2 筆）"
        in _held_etf_refresh_message(third)
    )
    assert _amounts(conn)[("e-00939", "2026-10-05")] == ("0.0800", "final")


@pytest.mark.parametrize(
    "field,old_value",
    [
        ("record_date", "2026-09-20"),
        ("pay_date", "2026-10-16"),
        ("amount_per_share", "1.0000"),
        ("amount_status", "estimated"),
        ("source", "manual"),
        ("reconciliation_status", "needs_review"),
    ],
)
def test_source_corrections_count_as_updates(conn, field, old_value):
    _run(conn, FakeSources())
    conn.execute(f"UPDATE distributions SET {field}=? WHERE id='d-00919'", (old_value,))
    outcome = _run(conn, FakeSources())
    assert outcome["twse"]["synced"] == 2
    assert outcome["twse"]["updated"] == 1
    assert outcome["twse"]["unchanged"] == 1
    assert outcome["twse"]["inserted"] == 0


def test_rolled_back_unit_is_excluded_from_change_counts(conn, monkeypatch):
    _run(conn, FakeSources())
    sources = FakeSources()
    sources.events["00919"] = [
        ("2026-09-16", "2026-10-15", "1.20"),
        ("2026-12-16", "2027-01-15", "0.80"),
    ]
    original = sync_etf_distributions.upsert_etf_distribution

    def fail_after_insert(connection, security_id, fields, **kwargs):
        result = original(connection, security_id, fields, **kwargs)
        if fields["ex_dividend_date"] == "2026-12-16":
            raise ValueError("simulated transaction failure")
        return result

    monkeypatch.setattr(
        sync_etf_distributions, "upsert_etf_distribution", fail_after_insert
    )
    outcome = _run(conn, sources)
    assert outcome["twse"]["failed_units"] == ["00919:2026"]
    assert outcome["twse"]["synced"] == outcome["twse"]["unchanged"] == 1
    assert outcome["twse"]["inserted"] == outcome["twse"]["updated"] == 0
    assert _amounts(conn)[("e-00919", "2026-09-16")] == ("1.1000", "final")
    assert ("e-00919", "2026-12-16") not in _amounts(conn)


def test_old_job_without_change_counts_only_reports_verified_records(conn):
    outcome = _run(conn, FakeSources())
    for market in ("twse", "tpex"):
        for key in ("inserted", "updated", "unchanged"):
            outcome[market].pop(key)
    message = _held_etf_refresh_message(outcome)
    assert "核對 3 筆；" in message
    assert "寫入" not in message and "新增" not in message


def test_vanished_ex_date_is_flagged_for_review(conn):
    sources = FakeSources()
    sources.events["00919"] = [("2026-09-17", "2026-10-15", "1.10")]

    _run(conn, sources)

    row = conn.execute(
        "SELECT reconciliation_status FROM distributions WHERE id = 'd-00919'"
    ).fetchone()
    assert row["reconciliation_status"] == "needs_review"


def test_december_also_requests_next_year(conn):
    sources = FakeSources()

    outcome = _run(conn, sources, today="2026-12-10")

    assert outcome["twse"]["years"] == [2024, 2025, 2026, 2027]
    assert ("TWSE", "00919", "2027") in sources.requests


def test_january_also_requests_last_year_with_unknown_amount(conn):
    conn.execute(
        "INSERT INTO distributions (id, security_id, ex_dividend_date, "
        "amount_status, source) VALUES ('d-dec', 'e-00679B', '2026-12-16', "
        "'unknown', 'tpex_etf_distribution_list')"
    )
    conn.commit()
    sources = FakeSources()

    outcome = _run(conn, sources, today="2027-01-05")

    assert outcome["tpex"]["years"] == [2025, 2026, 2027]
    assert ("TPEx", "00679B", "2026") in sources.requests
    # 00919's 2026-09-16 row is also still unknown and within 12 months.
    assert outcome["twse"]["years"] == [2025, 2026, 2027]


def test_old_unknown_amount_still_gets_refreshed(conn):
    sources = FakeSources()

    outcome = _run(conn, sources, today="2027-10-01")

    # 00919's 2026-09-16 row is now more than 12 months back.
    assert outcome["twse"]["years"] == [2025, 2026, 2027]
    assert ("TWSE", "00919", "2026") in sources.requests


def test_no_held_etfs_makes_no_requests_and_no_run(tmp_path):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    sources = FakeSources()

    outcome = _run(connection, sources)
    runs = connection.execute("SELECT COUNT(*) FROM catalog_sync_runs").fetchone()[0]
    connection.close()

    assert sources.requests == []
    assert runs == 0
    assert outcome["twse"]["run_id"] is None and outcome["tpex"]["run_id"] is None
    assert outcome["pending_amounts"] == 0


def test_running_full_sync_is_reported_without_writing(conn):
    conn.execute(
        "INSERT INTO catalog_sync_runs (id, source, started_at, status) VALUES "
        "('r-full', 'twse_etf_distribution_list', '2026-09-30T00:00:00Z', 'running')"
    )
    conn.commit()
    sources = FakeSources()

    outcome = _run(conn, sources)

    assert outcome["errors"] == {"twse": "同步進行中，請稍後再試"}
    assert "twse" not in outcome
    assert not any(market == "TWSE" for market, _, _ in sources.requests)
    assert _amounts(conn)[("e-00919", "2026-09-16")] == (None, "unknown")
    assert _amounts(conn)[("e-00679B", "2026-07-15")] == ("0.4000", "final")


def test_one_market_failing_does_not_stop_the_other(conn, monkeypatch):
    monkeypatch.setattr(sync, "_RETRY_BACKOFF_SECONDS", (0, 0))
    sources = FakeSources()
    sources.status["TWSE"] = 503

    outcome = _run(conn, sources)

    assert sorted(outcome["twse"]["failed_units"]) == [
        "00919:2024",
        "00919:2025",
        "00919:2026",
        "00939:2024",
        "00939:2025",
    ]
    assert outcome["tpex"]["synced"] == 1
    assert _amounts(conn)[("e-00679B", "2026-07-15")] == ("0.4000", "final")


def test_requests_keep_the_full_sync_pacing(conn, monkeypatch):
    sleeps = []
    monkeypatch.setattr(pacing, "pause", sleeps.append)

    _run(conn, FakeSources())

    # One pause after each of the 3 fetched units (delisted is skipped).
    assert sleeps == [sync_etf_distributions._ETF_DISTRIBUTION_DELAY_SECONDS] * 9


def test_recomputes_inferred_frequency(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(frequency_inference, "recompute_all", calls.append)

    _run(conn, FakeSources())

    assert calls == [conn]


def test_complete_empty_history_is_skipped_but_new_tracked_etf_gets_its_own_years(conn):
    _run(conn, FakeSources())
    sources = FakeSources()
    _run(conn, sources)
    assert len(sources.requests) == 3
    assert all(year == "2026" for _, _, year in sources.requests)
    conn.execute("INSERT INTO watchlist(security_id) VALUES('e-0050')")
    sources = FakeSources()
    _run(conn, sources)
    assert {
        (code, year) for market, code, year in sources.requests if year != "2026"
    } == {("0050", "2024"), ("0050", "2025")}


def test_a_stored_event_does_not_count_as_complete_year_coverage(conn):
    sources = FakeSources()
    outcome = _run(conn, sources)
    assert outcome["twse"]["years"] == [2024, 2025, 2026]
    assert ("TWSE", "00919", "2024") in sources.requests


def test_traded_etf_reaches_back_to_its_first_acquisition_once(conn):
    """Design 050 §4.2.1: years before the 3-year window are fetched from
    the first acquisition on, and never again once covered."""
    conn.execute(
        "INSERT INTO trades (id, broker_account_id, security_id, event_type, "
        "trade_date, quantity, created_at) VALUES ('t-old', 'acc', 'e-00919', "
        "'opening', '2021-11-26', 1000, '2021-11-26T00:00:00Z')"
    )
    sources = FakeSources()

    outcome = _run(conn, sources)

    old = {(code, year) for _, code, year in sources.requests if year < "2024"}
    assert old == {("00919", "2021"), ("00919", "2022"), ("00919", "2023")}
    assert outcome["twse"]["years"] == [2021, 2022, 2023, 2024, 2025, 2026]
    sources = FakeSources()
    _run(conn, sources)
    assert all(year == "2026" for _, _, year in sources.requests)


def test_backfill_never_starts_before_listing(conn):
    conn.execute("UPDATE securities SET listed_date='2023-05-10' WHERE id='e-00919'")
    conn.execute(
        "INSERT INTO trades (id, broker_account_id, security_id, event_type, "
        "trade_date, quantity, created_at) VALUES ('t-old', 'acc', 'e-00919', "
        "'opening', '2020-01-02', 1000, '2020-01-02T00:00:00Z')"
    )
    sources = FakeSources()

    _run(conn, sources)

    old = {(code, year) for _, code, year in sources.requests if year < "2024"}
    assert old == {("00919", "2023")}


def test_watched_only_etf_keeps_the_three_year_window(conn):
    sources = FakeSources()

    _run(conn, sources)

    assert {y for _, code, y in sources.requests if code == "00939"} == {
        "2024",
        "2025",
        "2026",
    }


def _old_review(conn, ex_date="2025-01-02"):
    conn.execute(
        "INSERT INTO distributions "
        "(id,security_id,ex_dividend_date,pay_date,amount_per_share,amount_status,"
        "source,reconciliation_status,reconciliation_note) "
        "VALUES ('old-review','e-00919',?,'2025-10-28',1,'final',"
        "'twse_etf_distribution_list','needs_review','來源已無此除息日')",
        (ex_date,),
    )


@pytest.mark.parametrize(
    ("ex_date", "recheck"),
    [
        ("2025-01-02", False),
        ("2025-09-29", False),
        ("2025-09-30", True),
        ("2025-10-01", True),
    ],
)
def test_review_recheck_uses_one_year_window_and_preserves_warning(
    conn, ex_date, recheck
):
    _run(conn, FakeSources())  # Complete, including valid empty old years.
    _old_review(conn, ex_date)
    for _ in range(2):
        sources = FakeSources()
        _run(conn, sources)
        assert (("TWSE", "00919", "2025") in sources.requests) is recheck
        assert ("TWSE", "00919", "2026") in sources.requests
        row = conn.execute(
            "SELECT ex_dividend_date,amount_status,reconciliation_status,"
            "reconciliation_note FROM distributions WHERE id='old-review'"
        ).fetchone()
        assert tuple(row) == (
            ex_date,
            "final",
            "needs_review",
            "來源已無此除息日",
        )


@pytest.mark.parametrize("coverage_status", [None, "http_failed"])
def test_old_review_does_not_prevent_missing_or_failed_year_backfill(
    conn, coverage_status
):
    _run(conn, FakeSources())
    _old_review(conn)
    if coverage_status is None:
        conn.execute(
            "DELETE FROM catalog_sync_units "
            "WHERE security_id='e-00919' AND period='2025'"
        )
    else:
        conn.execute(
            "UPDATE catalog_sync_units SET status=? "
            "WHERE security_id='e-00919' AND period='2025'",
            (coverage_status,),
        )
    sources = FakeSources()
    _run(conn, sources)
    assert ("TWSE", "00919", "2025") in sources.requests


def test_explicit_full_sync_can_reconcile_old_review_after_daily_skips_it(conn):
    _run(conn, FakeSources())
    _old_review(conn)
    sources = FakeSources()
    _run(conn, sources)
    assert ("TWSE", "00919", "2025") not in sources.requests
    sources = FakeSources()
    sources.events["00919"].append(("2025-01-02", "2025-02-01", "0.50"))
    with httpx2.Client(transport=httpx2.MockTransport(sources.handler)) as client:
        sync_etf_distributions.sync_etf_distributions(
            conn, client, years=1, reference_year=2025
        )
    assert ("TWSE", "00919", "2025") in sources.requests
    row = conn.execute(
        "SELECT amount_per_share,reconciliation_status,reconciliation_note "
        "FROM distributions WHERE id='old-review'"
    ).fetchone()
    assert tuple(row) == ("0.5000", None, None)


def _app(tmp_path, sources):
    def override():
        with httpx2.Client(transport=httpx2.MockTransport(sources.handler)) as c:
            yield c

    app = create_app(tmp_path)
    app.dependency_overrides[get_http_client] = override
    return app


def test_calendar_button_refreshes_and_returns_to_same_view(tmp_path):
    sources = FakeSources()
    with TestClient(_app(tmp_path, sources)) as client:
        connection = get_connection(tmp_path)
        _seed(connection)
        connection.close()

        page = client.get("/income-calendar?month=2026-10")
        assert 'action="/income-calendar/refresh-etf"' in page.text
        assert "同步 ETF 配息" in page.text

        response = client.post(
            "/income-calendar/refresh-etf",
            data={"month": "2026-10", "filter": "all"},
            follow_redirects=False,
        )
        location = response.headers["location"]
        params = parse_qs(urlparse(location).query)
        assert response.status_code == 303
        assert params["month"] == ["2026-10"]
        assert "filter" not in params
        assert "etf_refresh_failed" not in params
        job = load_job(client, response)
        assert job["status"] == "success"
        assert job["result"]["pending_amounts"] == 1

        page = client.get(location)
        assert "已更新持有 ETF 配息" in page.text
        assert "2026 年 10 月" in page.text


def test_calendar_button_reports_running_sync(tmp_path):
    sources = FakeSources()
    with TestClient(_app(tmp_path, sources)) as client:
        connection = get_connection(tmp_path)
        _seed(connection)
        connection.execute(
            "INSERT INTO catalog_sync_runs (id, source, started_at, status) VALUES "
            "('r', 'tpex_etf_distribution_list', '2026-09-30T00:00:00Z', 'running')"
        )
        connection.commit()
        connection.close()

        response = client.post(
            "/income-calendar/refresh-etf",
            data={"month": "2026-09", "filter": "all"},
            follow_redirects=False,
        )

    params = parse_qs(urlparse(response.headers["location"]).query)
    assert "job_id" not in params
    assert "此來源同步中" in params["sync_error"][0]
    assert sources.requests == []


def test_api_returns_per_market_results(tmp_path):
    sources = FakeSources()
    with TestClient(_app(tmp_path, sources)) as client:
        connection = get_connection(tmp_path)
        _seed(connection)
        connection.close()
        body = client.post("/api/v1/held-etf-distributions-sync").json()

    assert body["errors"] == {}
    assert body["twse"]["synced"] == 2 and body["twse"]["years"] == [2024, 2025, 2026]
    assert body["tpex"]["etfs"] == 1 and body["tpex"]["run_id"]
    assert body["pending_amounts"] == 1


def test_cli_held_only(tmp_path, monkeypatch, capsys):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    _seed(connection)
    connection.close()
    sources = FakeSources()
    monkeypatch.setattr(
        cli,
        "new_client",
        lambda: httpx2.Client(transport=httpx2.MockTransport(sources.handler)),
    )

    code = cli.main(
        ["sync", "etf-distributions", "--held-only", "--data-dir", str(tmp_path)]
    )

    assert code == 0
    out = capsys.readouterr().out
    assert "TWSE ETF 配息" in out and "TPEx ETF 配息" in out
    assert "仍待公布金額：1 筆" in out
    assert len(sources.requests) == 9


def test_retry_job_does_not_add_newly_tracked_market_to_original_scope(conn, tmp_path):
    from taiwan_stock.infrastructure import sync_jobs

    conn.execute("DELETE FROM trades WHERE security_id='e-00679B'")
    sources = FakeSources()
    sources.status["TWSE"] = 503

    def launcher(directory, job_id):
        with httpx2.Client(transport=httpx2.MockTransport(sources.handler)) as client:
            sync_jobs.run_worker(directory, job_id, client=client)

    first = sync_jobs.submit(
        conn, tmp_path, "held-etf-distributions", launcher=launcher
    )
    assert first["status"] == "partial"
    assert first["result"]["tpex"]["etfs"] == 0
    conn.execute("INSERT INTO watchlist(security_id) VALUES('e-00679B')")
    sources.status["TWSE"] = 200
    sources.requests.clear()
    second = sync_jobs.retry(conn, tmp_path, first["id"], launcher=launcher)
    assert second["status"] == "success"
    assert sources.requests
    assert all(market == "TWSE" for market, _, _ in sources.requests)


def test_v2_completed_submission_is_idempotent_and_lookup_survives_completion(tmp_path):
    sources = FakeSources()
    with TestClient(_app(tmp_path, sources)) as web:
        with closing(get_connection(tmp_path)) as connection:
            _seed(connection)
        payload = {"operation": "held-etf-distributions", "request_key": "once"}
        first = web.post("/api/v2/sync-jobs", json=payload)
        assert first.status_code == 202
        before = list(sources.requests)
        repeated = web.post("/api/v2/sync-jobs", json=payload)
        assert repeated.status_code == 202
        assert repeated.json()["id"] == first.json()["id"]
        assert sources.requests == before
        lookup = web.get("/api/v2/sync-jobs?request_key=once").json()
        assert lookup["jobs"] == []
        assert lookup["requested_job"]["id"] == first.json()["id"]
        assert lookup["requested_job"]["status"] == "success"
        history = web.get("/api/v2/sync-jobs?active=false").json()
        assert [job["id"] for job in history["jobs"]] == [first.json()["id"]]


def test_markets_fetch_together_and_only_this_thread_writes(conn, monkeypatch):
    """Design 053: TWSE and TPEx requests overlap; every write and unit record
    still happens on the calling thread, one at a time."""
    sources = FakeSources()
    both_markets = threading.Barrier(2, timeout=5)
    first_seen = set()
    handler = sources.handler

    def overlapping(request):
        market = "TWSE" if request.url.path.endswith("/etfDiv") else "TPEx"
        if market not in first_seen:
            first_seen.add(market)
            both_markets.wait()  # only returns once the other market is fetching
        return handler(request)

    sources.handler = overlapping
    writers = set()
    record = sync_history.Run.record

    def tracked(self, *args, **kwargs):
        writers.add(threading.get_ident())
        return record(self, *args, **kwargs)

    monkeypatch.setattr(sync_history.Run, "record", tracked)
    monkeypatch.setattr(pacing, "pause", lambda s: None)

    outcome = _run(conn, sources)

    assert outcome["errors"] == {}
    assert outcome["twse"]["failed_units"] == []
    assert outcome["tpex"]["failed_units"] == []
    assert writers == {threading.get_ident()}
