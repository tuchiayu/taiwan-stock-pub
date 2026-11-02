"""Regressions for real search failures and durable sync diagnostics."""

from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx2
import pytest
from fastapi.testclient import TestClient
from sync_helpers import load_job

from taiwan_stock import cli
from taiwan_stock.catalog import (
    query,
    search,
    sync,
    sync_etf_distributions,
    sync_history,
)
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.web.query_state import sort_links, url


@pytest.fixture
def conn(tmp_path):
    conn = get_connection(tmp_path)
    migrate.apply_pending(conn)
    yield conn
    conn.close()


def seed(conn, code, bank=None, override=None, frequency=None):
    sid = "s" + code
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES (?, 'TWSE', ?, ?, 'etf', 'active')",
        (sid, code, "ETF " + code),
    )
    conn.execute(
        "INSERT INTO etf_profiles "
        "(security_id, custodian_bank, custodian_bank_user_override, "
        "distribution_frequency) VALUES (?, ?, ?, ?)",
        (sid, bank, override, frequency),
    )
    return sid


@pytest.mark.parametrize(
    ("latest_day", "previous_day", "current", "previous", "expected"),
    [
        ("2026-10-16", "2026-10-15", "105", "100", Decimal("5")),
        ("2026-10-16", "2026-10-15", "95", "100", Decimal("-5")),
        ("2026-10-16", "2026-10-15", "100", "100", Decimal("0")),
        ("2026-10-19", "2026-10-16", "101", "100", Decimal("1")),
        ("2026-10-16", "2026-10-14", "105", "100", None),
        ("2026-10-12", "2026-10-08", "105", "100", None),
        ("2026-10-16", None, "105", None, None),
        ("2026-10-16", "2026-10-15", "105", "0", None),
        ("2026-10-16", "2026-10-15", "0", "100", None),
    ],
)
def test_stock_quotes_use_only_adjacent_closes(
    conn, latest_day, previous_day, current, previous, expected
):
    conn.execute(
        "INSERT INTO securities (id,market,code,name,security_type,status,industry) "
        "VALUES ('stock','TWSE','1314','中石化','stock','active','03')"
    )
    conn.execute(
        "INSERT INTO daily_prices (security_id,trade_date,close) VALUES ('stock',?,?)",
        (latest_day, current),
    )
    if previous_day:
        conn.execute(
            "INSERT INTO daily_prices (security_id,trade_date,close) "
            "VALUES ('stock',?,?)",
            (previous_day, previous),
        )
    [row] = query.browse(conn, mode="stock")["results"]
    assert row["latest_close"] == current
    assert row["close_date"] == latest_day
    assert row["change_pct"] == expected
    assert row["industry_name"] == "塑膠工業"
    assert row["industry"] == "03"
    assert row["previous_close_date"] == previous_day
    assert all(not note.startswith("收盤 ") for note in row["yield_notes"])


@pytest.mark.parametrize("sort", ["latest_close", "change_pct"])
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_stock_quote_sort_runs_before_pagination_and_keeps_unknown_last(
    conn, sort, direction
):
    for i in range(53):
        sid = f"stock-{i}"
        conn.execute(
            "INSERT INTO securities (id,market,code,name,security_type,status) "
            "VALUES (?,'TWSE',?,'測試股票','stock','active')",
            (sid, str(1000 + i)),
        )
        if i < 52:
            conn.executemany(
                "INSERT INTO daily_prices (security_id,trade_date,close) "
                "VALUES (?,?,?)",
                [(sid, "2026-10-15", "100"), (sid, "2026-10-16", str(100 + i))],
            )
    first = query.browse(conn, mode="stock", sort=sort, direction=direction)
    last = query.browse(conn, mode="stock", sort=sort, direction=direction, page=2)
    assert first["total_results"] == 53
    assert first["results"][0]["id"] == (
        "stock-0" if direction == "asc" else "stock-51"
    )
    assert last["results"][-1]["id"] == "stock-52"
    assert [row[sort] for row in first["results"]] == sorted(
        [row[sort] for row in first["results"]],
        key=Decimal,
        reverse=direction == "desc",
    )


def test_bank_variants_override_and_unknown_match_same_display(conn):
    seed(conn, "0001", "合作金庫商業銀行")
    seed(conn, "0002", "合作金庫商業銀行")
    seed(conn, "0003", "玉山銀行", "合作金庫商業銀行")
    seed(conn, "0004")
    result = query.browse(conn, mode="etf", bank="合作金庫商業銀行")
    assert result["total_results"] == 3
    assert result["bank_counts"]["合作金庫商業銀行"] == 3
    assert result["custodian_banks"] == ["合作金庫商業銀行"]
    assert len(search.search_securities(conn, custodian_bank="合作金庫商業銀行")) == 3
    assert len(search.search_securities(conn, distribution_frequency="unknown")) == 4
    assert query.browse(conn, mode="etf", bank="unknown")["total_results"] == 1


def test_bank_sort_groups_unknown_reasons_instead_of_leaving_them_unordered(conn):
    """A user clicking the 保管銀行 header saw three different "no value"
    reasons (delisted / sync failed / source unsupported) left in
    arbitrary code order after all the real bank names — sort by a fixed,
    most-actionable-first grouping instead."""
    known = seed(conn, "0001", "合作金庫商業銀行")
    unsupported = seed(conn, "0002")
    failed = seed(conn, "0003")
    delisted = seed(conn, "0004")
    never_synced = seed(conn, "0005")
    conn.execute("UPDATE securities SET status='delisted' WHERE id=?", (delisted,))
    with sync_history.run(conn, "investment_trust") as task:
        task.plan("unsupported:0002", security_id=unsupported)
        task.record("unsupported:0002", "unsupported")
        task.plan("元大:0003", security_id=failed)
        task.record("元大:0003", "http_failed", reason="HTTP 404")
    result = query.browse(conn, mode="etf", sort="bank", include_delisted=True)
    assert [r["id"] for r in result["results"]] == [
        known,
        delisted,
        failed,
        unsupported,
        never_synced,
    ]


def test_yield_ranks_complete_set_with_missing_last_in_both_directions(
    conn, monkeypatch
):
    monkeypatch.setattr(query, "today_taipei", lambda: "2026-09-13")
    for i in range(1, 103):
        sid = seed(conn, f"{i:04d}")
        conn.execute(
            "INSERT INTO daily_prices (security_id, trade_date, close) "
            "VALUES (?, '2026-09-11', '100')",
            (sid,),
        )
        conn.execute(
            "INSERT INTO distributions "
            "(id, security_id, ex_dividend_date, amount_per_share, amount_status) "
            "VALUES (?, ?, '2026-08-01', ?, 'final')",
            (sid, sid, str(i)),
        )
    seed(conn, "9999")
    desc = query.browse(conn, mode="etf", sort="yield", direction="desc")
    assert desc["results"][0]["code"] == "0102"
    assert desc["total_results"] == 103 and desc["total_pages"] == 3
    assert (
        query.browse(conn, mode="etf", sort="yield", direction="desc", page=3)[
            "results"
        ][-1]["code"]
        == "9999"
    )
    assert (
        query.browse(conn, mode="etf", sort="yield", direction="asc", page=3)[
            "results"
        ][-1]["code"]
        == "9999"
    )
    assert (
        query.browse(conn, mode="etf", sort="yield", direction="asc", page=99)["page"]
        == 3
    )


def test_batch_queries_do_not_grow_per_product(conn):
    for i in range(70):
        seed(conn, f"{i:04d}")
    statements = []
    conn.set_trace_callback(statements.append)
    query.browse(conn, mode="etf")
    conn.set_trace_callback(None)
    # +2 vs. the pre-delisting baseline: one constant-cost refresh_status()
    # UPDATE and one join SELECT from delistings.details(); +1 for the
    # last-success sync lookup (design 023 §4.4), +1 for the stored market
    # calendar (issue #89), +2 for the latest size and holder snapshots
    # (design 044), +1 for the market-wide distribution freshness (design
    # 052), +1 for the batched corporate actions (design 059) — still O(1)
    # regardless of product count, not per-product growth.
    assert len(statements) <= 16


def test_sort_and_return_urls_preserve_encoded_filters():
    state = {
        "security_type": "etf",
        "view": "all",
        "q": "A&B 台灣",
        "custodian_bank": "玉山銀行",
        "distribution_frequency": "quarterly",
        "pay_month": "2026-09",
        "sort": "code",
        "direction": "asc",
        "page": 3,
    }
    parsed = parse_qs(urlparse(sort_links(state, {"yield"})["yield"]).query)
    assert parsed["q"] == ["A&B 台灣"]
    assert parsed["page"] == ["1"] and parsed["direction"] == ["desc"]
    assert parsed["custodian_bank"] == ["玉山銀行"]
    assert parse_qs(urlparse(url(state)).query)["page"] == ["3"]


def test_header_sort_and_tracking_form_keep_state(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        seed(conn, "0001", "玉山銀行", frequency="quarterly")
        conn.close()
        response = client.get(
            "/securities",
            params={
                "security_type": "etf",
                "view": "all",
                "custodian_bank": "玉山銀行",
                "distribution_frequency": "quarterly",
                "sort": "bank",
            },
        )
        assert response.status_code == 200
        assert 'aria-sort="ascending"' in response.text
        assert '<select name="sort">' not in response.text
        assert 'name="return_to"' in response.text
        assert "distribution_frequency=quarterly" in response.text
        assert "清除條件" in response.text


def test_no_known_amount_is_not_a_zero_yield():
    assert query.yield_summary([], "100")["known_yield"] is None
    events = [
        {
            "ex_dividend_date": "2026-08-01",
            "amount_per_share": None,
            "amount_status": "unknown",
        }
    ]
    assert (
        query.yield_summary(events, "100", as_of="2026-09-13", complete=True)[
            "known_yield"
        ]
        is None
    )
    events[0].update(amount_per_share="0", amount_status="final")
    info = query.yield_summary(events, "100", as_of="2026-09-13", complete=True)
    assert info["known_yield"] == 0 and info["complete"]


def test_newly_listed_etf_is_distinguished_from_a_confirmed_non_payer():
    # design 005 §4.5 follow-up: zero events in the 12-month query window
    # looks identical whether the security had a full opportunity to pay
    # and confirmed it didn't, or it simply listed after the window
    # started (real case: 00400A listed 2026-04-09, ~150 days before its
    # first eligible payout window closes) — `listed_date` distinguishes
    # the two instead of implying a confirmed non-payer either way.
    newly_listed = query.yield_summary(
        [], "100", as_of="2026-09-13", complete=True, listed_date="2026-04-09"
    )
    assert newly_listed["quality_label"] == "新上市，尚無配息紀錄"

    long_listed = query.yield_summary(
        [], "100", as_of="2026-09-13", complete=True, listed_date="2020-01-01"
    )
    assert long_listed["quality_label"] == "該期間未查得配息"

    unknown_listing = query.yield_summary([], "100", as_of="2026-09-13", complete=True)
    assert unknown_listing["quality_label"] == "該期間未查得配息"


def test_browse_labels_a_newly_listed_etf_using_its_listed_date(conn, monkeypatch):
    # `query.browse` reads `listed_date` off the same `securities` row it
    # already fetches for every other column (see `candidate_securities`'s
    # `SELECT securities.*`) — this exercises that wiring end-to-end,
    # complementing the direct `yield_summary` unit test above.
    monkeypatch.setattr(query, "today_taipei", lambda: "2026-09-13")
    sid = seed(conn, "00400A")
    conn.execute(
        "UPDATE securities SET listed_date = '2026-04-09' WHERE id = ?", (sid,)
    )

    results = {r["code"]: r for r in query.browse(conn, mode="etf")["results"]}

    assert results["00400A"]["yield_info"]["quality_label"] == "新上市，尚無配息紀錄"


def test_stock_yield_quality_not_downgraded_by_etf_sync_coverage(conn, monkeypatch):
    # Stocks sync via `sync_ex_dividend_notices`, which never writes
    # `catalog_sync_units` rows — `event_coverage` only has a signal for
    # the per-unit ETF distribution-list sync, so it must not be used to
    # judge stock data completeness (it would otherwise always read
    # incomplete, no matter how current the stock's data actually is).
    monkeypatch.setattr(query, "today_taipei", lambda: "2026-09-13")
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('s2330', 'TWSE', '2330', '台積電', 'stock', 'active')"
    )
    conn.execute(
        "INSERT INTO daily_prices (security_id, trade_date, close) "
        "VALUES ('s2330', '2026-09-11', '100')"
    )
    conn.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, amount_per_share, amount_status) "
        "VALUES ('d1', 's2330', '2026-08-01', '5', 'final')"
    )
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('s2454', 'TWSE', '2454', '聯發科', 'stock', 'active')"
    )
    conn.execute(
        "INSERT INTO daily_prices (security_id, trade_date, close) "
        "VALUES ('s2454', '2026-09-11', '100')"
    )

    results = {r["code"]: r for r in query.browse(conn, mode="stock")["results"]}

    assert results["2330"]["yield_info"]["complete"]
    assert results["2330"]["yield_info"]["quality_label"] == "依已知紀錄計算"
    assert results["2454"]["yield_info"]["quality_label"] == "該期間未查得配息"


def test_parse_failure_is_durable_and_preserves_previous_events(conn):
    sid = seed(conn, "00878")
    conn.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, amount_per_share, amount_status) "
        "VALUES ('d', ?, '2026-08-01', '1', 'final')",
        (sid,),
    )
    client = httpx2.Client(
        transport=httpx2.MockTransport(
            lambda r: httpx2.Response(200, json={"fields": ["wrong"], "data": []})
        )
    )
    result = sync_etf_distributions.sync_etf_distributions(
        conn, client, years=1, reference_year=2026
    )
    assert result["synced"] == 0 and result["failed_units"] == ["00878:2026"]
    report = sync_history.get_report(conn, result["run_id"])
    assert report["status"] == "failed"
    assert report["units"][0]["status"] == "parse_failed"
    assert (
        conn.execute("SELECT amount_per_share FROM distributions").fetchone()[0] == "1"
    )


def test_sync_rejects_mismatched_product_and_retries_failed_unit_only(conn):
    seed(conn, "00878")

    def fetch(client, code, year):
        return (
            [{"code": "0050", "ex_dividend_date": "2026-08-01"}] if year == 2026 else []
        )

    result = sync_etf_distributions._sync_etf_distributions_for_market(
        conn,
        None,
        market="TWSE",
        source="test",
        fetch_and_normalize_year=fetch,
        years=2,
        reference_year=2026,
    )
    called = []

    def good(client, code, year):
        called.append((code, year))
        return []

    retried = sync_etf_distributions._sync_etf_distributions_for_market(
        conn,
        None,
        market="TWSE",
        source="test",
        fetch_and_normalize_year=good,
        years=2,
        reference_year=2026,
        retry_run_id=result["run_id"],
    )
    assert called == [("00878", 2026)]
    assert sync_history.get_report(conn, retried["run_id"])["status"] == "success"
    assert sync_history.get_report(conn, result["run_id"])["status"] == "partial"


def _http_error(status: int = 500):
    request = httpx2.Request("GET", "https://example.com")
    return httpx2.HTTPStatusError(
        "boom", request=request, response=httpx2.Response(status, request=request)
    )


def test_transient_http_failure_recovers_within_retry_budget(conn):
    seed(conn, "00878")
    attempts = []

    def flaky(client, code, year):
        attempts.append((code, year))
        if len(attempts) < 3:
            raise _http_error()
        return []

    result = sync_etf_distributions._sync_etf_distributions_for_market(
        conn,
        None,
        market="TWSE",
        source="test",
        fetch_and_normalize_year=flaky,
        years=1,
        reference_year=2026,
    )
    assert len(attempts) == 3
    assert result.pop("run_id")
    assert result == {
        "synced": 0,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped_no_ex_date": 0,
        "failed_units": [],
    }


def test_transient_http_failure_fails_the_unit_once_retries_are_exhausted(conn):
    seed(conn, "00878")

    def always_fails(client, code, year):
        raise _http_error()

    result = sync_etf_distributions._sync_etf_distributions_for_market(
        conn,
        None,
        market="TWSE",
        source="test",
        fetch_and_normalize_year=always_fails,
        years=1,
        reference_year=2026,
    )
    assert result["failed_units"] == ["00878:2026"]


def test_circuit_breaker_stops_run_after_consecutive_http_failures(conn):
    # Live TWSE observation (design 005 §4.4 follow-up): once a source
    # actually starts rejecting us, a block outlasts our retry backoff
    # by minutes, not seconds — grinding through the rest of a run's
    # units at that point wastes time for zero benefit. After
    # `_CIRCUIT_BREAKER_THRESHOLD` consecutive HTTP failures, the loop
    # must stop rather than keep attempting; untouched units stay
    # `pending` so a later `sync retry` picks them back up.
    unit_count = sync._CIRCUIT_BREAKER_THRESHOLD + 2
    for i in range(unit_count):
        seed(conn, f"{i:04d}")
    attempts = []

    def always_fails(client, code, year):
        attempts.append((code, year))
        raise _http_error()

    result = sync_etf_distributions._sync_etf_distributions_for_market(
        conn,
        None,
        market="TWSE",
        source="test",
        fetch_and_normalize_year=always_fails,
        years=1,
        reference_year=2026,
    )
    # Each failed unit still gets its full `_fetch_with_retry` budget
    # (3 attempts) before counting toward the breaker — the breaker
    # trips per *unit*, not per raw HTTP attempt.
    assert len(attempts) == sync._CIRCUIT_BREAKER_THRESHOLD * 3
    assert len(result["failed_units"]) == sync._CIRCUIT_BREAKER_THRESHOLD
    report = sync_history.get_report(conn, result["run_id"])
    pending = [u for u in report["units"] if u["status"] == "pending"]
    assert len(pending) == unit_count - sync._CIRCUIT_BREAKER_THRESHOLD


def test_circuit_breaker_ignores_non_http_failures(conn):
    # A code-mismatch/parse problem says nothing about the source being
    # blocked — it must not count toward, or trip, the breaker.
    unit_count = sync._CIRCUIT_BREAKER_THRESHOLD + 2
    for i in range(unit_count):
        seed(conn, f"{i:04d}")

    def always_mismatched(client, code, year):
        return [{"code": "wrong-code", "ex_dividend_date": "2026-08-01"}]

    result = sync_etf_distributions._sync_etf_distributions_for_market(
        conn,
        None,
        market="TWSE",
        source="test",
        fetch_and_normalize_year=always_mismatched,
        years=1,
        reference_year=2026,
    )
    assert len(result["failed_units"]) == unit_count
    report = sync_history.get_report(conn, result["run_id"])
    assert all(u["status"] != "pending" for u in report["units"])


def test_circuit_breaker_ignores_404_not_found(conn):
    # Live observation (國泰/Cathay, design 005 §4.4 follow-up): a run of
    # HTTP 404s means those specific products have no page at that
    # issuer's site (often a `guess_issuer` name-prefix misfire), not
    # that the whole host is blocking us — unlike a 403/428/5xx, it
    # must not trip the breaker, or a handful of bad codes would wrongly
    # abandon every other, perfectly reachable unit for that source.
    unit_count = sync._CIRCUIT_BREAKER_THRESHOLD + 2
    for i in range(unit_count):
        seed(conn, f"{i:04d}")

    def always_404(client, code, year):
        raise _http_error(404)

    result = sync_etf_distributions._sync_etf_distributions_for_market(
        conn,
        None,
        market="TWSE",
        source="test",
        fetch_and_normalize_year=always_404,
        years=1,
        reference_year=2026,
    )
    assert len(result["failed_units"]) == unit_count
    report = sync_history.get_report(conn, result["run_id"])
    assert all(u["status"] != "pending" for u in report["units"])


def test_active_run_prevents_duplicate_and_recovery_marks_pending(conn):
    first = sync_history.Run(conn, "test")
    first.plan("pending")
    with pytest.raises(sync_history.SyncInProgress):
        sync_history.Run(conn, "test")
    sync_history.recover_interrupted(conn)
    report = sync_history.get_report(conn, first.id)
    assert report["status"] == "interrupted"
    assert report["units"][0]["status"] == "interrupted"
    another = sync_history.Run(conn, "test")
    another.finish()


@pytest.mark.parametrize(
    "exc, status",
    [
        (_http_error(503), "http_failed"),
        (_http_error(403), "http_failed"),
        (httpx2.ConnectError("down"), "http_failed"),
        (_http_error(404), "parse_failed"),
        (ValueError("格式不符"), "parse_failed"),
        (sync_history.ProductNotListed("00636K"), "not_listed"),
        (KeyError("Entries"), "parse_failed"),
    ],
)
def test_error_info_separates_transient_source_change_and_not_listed(exc, status):
    assert sync_history.error_info(exc)[0] == status


def test_not_listed_unit_is_settled_while_failures_are_retried(conn):
    run = sync_history.Run(conn, "test")
    for key, exc in (
        ("missing", sync_history.ProductNotListed("missing")),
        ("changed", ValueError("格式不符")),
        ("offline", _http_error(503)),
    ):
        run.plan(key)
        run.record(key, "parse_failed", exc=exc)
    run.finish()
    assert sync_history.get_report(conn, run.id)["status"] == "failed"

    retry = sync_history.Run(conn, "test", retry_of=run.id)
    assert retry.retry_keys == {"changed", "offline"}
    retry.finish()

    only_missing = sync_history.Run(conn, "test")
    only_missing.plan("missing")
    only_missing.record(
        "missing", "parse_failed", exc=sync_history.ProductNotListed("missing")
    )
    only_missing.finish()
    assert sync_history.get_report(conn, only_missing.id)["status"] == "success"


def test_bank_migration_keeps_raw_source_and_override(tmp_path, monkeypatch):
    conn = get_connection(tmp_path)
    migrations = migrate.discover_migrations()
    monkeypatch.setattr(migrate, "discover_migrations", lambda: migrations[:4])
    migrate.apply_pending(conn)
    sid = seed(conn, "0001", "合作金庫商業銀行", "合作金庫商業銀行")
    conn.execute(
        "UPDATE etf_profiles SET custodian_institution_raw='原始機構' "
        "WHERE security_id=?",
        (sid,),
    )
    monkeypatch.undo()
    migrate.apply_pending(conn)
    row = conn.execute("SELECT * FROM etf_profiles").fetchone()
    assert (
        row["custodian_bank"]
        == row["custodian_bank_user_override"]
        == "合作金庫商業銀行"
    )
    assert row["custodian_institution_raw"] == "原始機構"
    conn.close()


def test_cli_sync_already_running_exits_with_message_not_a_traceback(
    tmp_path, monkeypatch, capsys
):
    # A second `taiwan-stock sync` invocation for a source that's still
    # running (impatient re-run, or a lingering process) must exit
    # cleanly with the existing `SyncInProgress` message, not an
    # unhandled traceback — this used to be a route's 409 JSON handler;
    # the same guarantee now belongs to the CLI, which is the only way
    # to trigger this sync at all (see cli.py's module docstring).
    real_client_cls = httpx2.Client
    monkeypatch.setattr(
        httpx2,
        "Client",
        lambda *a, **k: real_client_cls(
            transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json=[]))
        ),
    )

    conn = get_connection(tmp_path)
    migrate.apply_pending(conn)
    sync_history.Run(conn, "twse_etf_distribution_list")
    conn.close()

    exit_code = cli.main(["sync", "etf-distributions", "--data-dir", str(tmp_path)])

    assert exit_code == 1
    assert "此來源同步中" in capsys.readouterr().err


def test_cli_sync_prints_a_retry_hint_when_the_breaker_leaves_units_pending(
    tmp_path, monkeypatch, capsys
):
    # When the circuit breaker (sync.py) stops a run early, the CLI must
    # say so plainly and point at the exact retry command — not bury it
    # in the raw JSON summary, and not silently sleep-and-retry on a
    # guessed wait time (see cli.py's `_print_result`).
    real_client_cls = httpx2.Client
    monkeypatch.setattr(
        httpx2,
        "Client",
        lambda *a, **k: real_client_cls(
            transport=httpx2.MockTransport(
                lambda r: httpx2.Response(500, json={"error": "boom"})
            )
        ),
    )

    conn = get_connection(tmp_path)
    migrate.apply_pending(conn)
    for i in range(sync._CIRCUIT_BREAKER_THRESHOLD + 2):
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES (?, 'TWSE', ?, ?, 'etf', 'active')",
            (f"s{i}", f"{i:04d}", f"ETF {i}"),
        )
    conn.commit()
    conn.close()

    exit_code = cli.main(
        [
            "sync",
            "etf-distributions",
            "--data-dir",
            str(tmp_path),
            "--years",
            "1",
            "--reference-year",
            "2026",
        ]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "疑似遇到來源限流而提前停止" in out
    assert "taiwan-stock sync retry " in out


_ETF_DISTRIBUTION_FIELDS = [
    "證券代號",
    "證券簡稱",
    "除息交易日",
    "收益分配基準日",
    "收益分配發放日",
    "收益分配金額 (每1受益權益單位)",
    "收益分配標準 (102年度起啟用)",
    "公告年度",
]


def test_cli_sync_retry_only_reattempts_failed_units(tmp_path, monkeypatch):
    # End-to-end for `taiwan-stock sync retry` — the retry route itself
    # was removed (see cli.py's module docstring), so this is now the
    # only way `sync_history.Run`'s retry-of/`--retry` wiring gets
    # exercised at the CLI's own dispatch layer (source → loader,
    # years/reference_year from the failed units' periods).
    conn = get_connection(tmp_path)
    migrate.apply_pending(conn)
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('e1', 'TWSE', '00878', '國泰永續高股息', 'etf', 'active')"
    )
    conn.commit()
    conn.close()

    real_client_cls = httpx2.Client

    def _patch_client(handler):
        monkeypatch.setattr(
            httpx2,
            "Client",
            lambda *a, **k: real_client_cls(transport=httpx2.MockTransport(handler)),
        )

    _patch_client(lambda r: httpx2.Response(500, json={"error": "boom"}))
    assert (
        cli.main(
            [
                "sync",
                "etf-distributions",
                "--data-dir",
                str(tmp_path),
                "--years",
                "1",
                "--reference-year",
                "2026",
            ]
        )
        == 0
    )

    conn = get_connection(tmp_path)
    run_id = conn.execute(
        "SELECT id FROM catalog_sync_runs WHERE source='twse_etf_distribution_list'"
    ).fetchone()[0]
    assert (
        conn.execute(
            "SELECT status FROM catalog_sync_runs WHERE id=?", (run_id,)
        ).fetchone()[0]
        == "failed"
    )
    conn.close()

    def good_handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith("/rwd/zh/ETF/etfDiv"):
            return httpx2.Response(
                200,
                json={"status": "ok", "fields": _ETF_DISTRIBUTION_FIELDS, "data": []},
            )
        return httpx2.Response(200, json=[])

    _patch_client(good_handler)
    assert cli.main(["sync", "retry", run_id, "--data-dir", str(tmp_path)]) == 0

    conn = get_connection(tmp_path)
    retried_status = conn.execute(
        "SELECT status FROM catalog_sync_runs WHERE retry_of=?", (run_id,)
    ).fetchone()[0]
    conn.close()
    assert retried_status == "success"


def _failing_http_client_override():
    with httpx2.Client(
        transport=httpx2.MockTransport(
            lambda r: httpx2.Response(500, json={"error": "boom"})
        )
    ) as client:
        yield client


def test_catalog_sync_error_returns_to_the_same_filtered_view(tmp_path):
    # A user syncing from a filtered ETF view must land back on that
    # same view (`return_to`, carried by a hidden form field) even when
    # the sync errors — not bounce to the default stock view, which
    # silently drops whatever they were looking at.
    from taiwan_stock.web.deps import get_http_client

    app = create_app(tmp_path)
    app.dependency_overrides[get_http_client] = _failing_http_client_override
    with TestClient(app) as client:
        return_to = "/securities?security_type=etf&view=all&sort=bank&page=2"
        response = client.post(
            "/securities/sync",
            data={"return_to": return_to},
            follow_redirects=False,
        )
        assert response.status_code == 303
        location = response.headers["location"]
        assert location.startswith(return_to)
        assert load_job(client, response)["status"] == "failed"
        assert "security_type=stock" not in location
