"""Design 068: a source that keeps failing to parse is flagged."""

from contextlib import closing
from itertools import count

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.catalog import sync_history
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app

TRUST = "investment_trust"
_clock = count()


@pytest.fixture
def data_dir(tmp_path):
    with closing(get_connection(tmp_path)) as conn:
        migrate.apply_pending(conn)
    return tmp_path


@pytest.fixture
def conn(data_dir):
    with closing(get_connection(data_dir)) as connection:
        yield connection


def run(conn, source, units, *, status="partial", job_id=None):
    """`units` is a list of (issuer, status) in one finished run."""
    tick = next(_clock)
    run_id = f"run-{tick}"
    stamp = f"2026-11-01T{tick // 3600:02d}:{tick // 60 % 60:02d}:{tick % 60:02d}.000Z"
    conn.execute(
        "INSERT INTO catalog_sync_runs(id,source,started_at,status,job_id) "
        "VALUES(?,?,?,?,?)",
        (run_id, source, stamp, status, job_id),
    )
    for index, (issuer, unit_status) in enumerate(units):
        conn.execute(
            "INSERT INTO catalog_sync_units(run_id,unit_key,issuer,stage,"
            "attempted_at,status,reason) VALUES(?,?,?,'detail',?,?,?)",
            (
                run_id,
                f"unit-{index}",
                issuer,
                stamp,
                unit_status,
                "來源格式不符" if unit_status == "parse_failed" else None,
            ),
        )
    return run_id


def flagged(conn, source=TRUST):
    return [
        (item["source"], item["issuer"])
        for item in sync_history.suspected_changes(conn, [source])
    ]


def test_three_runs_with_only_parse_failures_are_flagged(conn):
    for _ in range(3):
        last = run(conn, TRUST, [("元大", "parse_failed"), ("元大", "http_failed")])
    [item] = sync_history.suspected_changes(conn, [TRUST])
    assert item == {
        "source": TRUST,
        "issuer": "元大",
        "run_id": last,
        "reason": "來源格式不符",
    }


def test_a_success_in_any_of_the_last_three_runs_clears_it(conn):
    run(conn, TRUST, [("元大", "parse_failed")])
    run(conn, TRUST, [("元大", "parse_failed"), ("元大", "success")])
    run(conn, TRUST, [("元大", "parse_failed")])
    assert flagged(conn) == []


def test_two_runs_are_not_enough(conn):
    for _ in range(2):
        run(conn, TRUST, [("元大", "parse_failed")])
    assert flagged(conn) == []


def test_issuers_are_judged_separately(conn):
    for _ in range(3):
        run(conn, TRUST, [("元大", "parse_failed"), ("國泰", "success")])
    assert flagged(conn) == [(TRUST, "元大")]


def test_only_recent_runs_count_and_running_ones_are_skipped(conn):
    for _ in range(3):
        run(conn, TRUST, [("元大", "parse_failed")])
    run(conn, TRUST, [("元大", "success")])
    assert flagged(conn) == []
    for _ in range(2):
        run(conn, TRUST, [("元大", "parse_failed")])
    run(conn, TRUST, [("元大", "parse_failed")], status="running")
    assert flagged(conn) == []


def test_a_source_without_issuers_is_one_group(conn):
    source = "twse_etf_distribution_list"
    for _ in range(3):
        run(conn, source, [(None, "parse_failed"), (None, "not_listed")])
    assert flagged(conn, source) == [(source, None)]


def test_the_report_and_job_pages_show_the_flag(data_dir, conn):
    conn.execute(
        "INSERT INTO sync_jobs(id,operation,origin,request_key,parameters,status,"
        "submitted_at,finished_at,owner_token) VALUES('j','investment-trust','web',"
        "'k','{}','partial','2026-11-01T00:00:00.000Z','2026-11-01T00:01:00.000Z','t')"
    )
    for _ in range(2):
        run(conn, TRUST, [("元大", "parse_failed")])
    last = run(conn, TRUST, [("元大", "parse_failed")], job_id="j")
    healthy = run(conn, "etf_profile_basic", [(None, "success")])
    expected = "來源可能改版：投信保管銀行／配息頻率 · 元大最近 3 次同步都無法解析"
    with TestClient(create_app(data_dir)) as client:
        assert expected in client.get(f"/catalog-sync-runs/{last}").text
        assert expected in client.get("/sync-jobs/j").text
        assert "來源可能改版" not in client.get(f"/catalog-sync-runs/{healthy}").text


# Design 074: only each source's recent runs are read.


def test_a_long_history_is_still_judged_by_the_last_three_runs(conn):
    for _ in range(30):
        run(conn, TRUST, [("元大", "success")])
    for _ in range(3):
        run(conn, TRUST, [("元大", "parse_failed")])
    assert flagged(conn) == [(TRUST, "元大")]
    run(conn, TRUST, [("元大", "success")])
    assert flagged(conn) == []


def test_an_issuer_seen_only_before_the_recent_runs_is_not_flagged(conn):
    for _ in range(3):
        run(conn, TRUST, [("元大", "parse_failed")])
    for _ in range(sync_history.RECENT_RUNS):
        run(conn, TRUST, [("國泰", "success")])
    assert flagged(conn) == []


def test_each_source_keeps_its_own_recent_runs(conn):
    other = "twse_etf_distribution_list"
    for _ in range(3):
        run(conn, TRUST, [("元大", "parse_failed")])
    for _ in range(sync_history.RECENT_RUNS):
        run(conn, other, [(None, "success")])
    assert [
        (item["source"], item["issuer"])
        for item in sync_history.suspected_changes(conn, [TRUST, other])
    ] == [(TRUST, "元大")]
