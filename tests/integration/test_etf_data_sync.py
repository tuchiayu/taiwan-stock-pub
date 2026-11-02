"""Design 052/053: the ETF tab's single 同步 ETF 資料 job."""

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.catalog import (
    operations,
    profile_enrichment,
    query,
    sync_etf_distributions,
    sync_history,
    sync_investment_trust,
)
from taiwan_stock.infrastructure import migrate, sync_jobs
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.shared import pacing
from taiwan_stock.web.routes_sync_jobs import result_summary

SOURCE = "twse_etf_distribution_list"


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    yield connection
    connection.close()


@pytest.fixture
def phases(monkeypatch):
    """Record the order each phase runs in, without touching any source."""
    calls = []
    real_execute = operations.execute

    def execute(conn, client, operation, options):
        if operation == "catalog":
            calls.append(operation)
            return {"phase": operation}
        return real_execute(conn, client, operation, options)

    def steps(name):
        def make(conn, client, *args, **kwargs):
            calls.append(("start", name, kwargs.get("year_list")))
            yield
            calls.append(("write", name))
            return {"synced": 2, "inserted": 1, "updated": 1, "unchanged": 0}

        return make

    def distribution_steps(conn, client, market, **kwargs):
        return steps(market.lower())(conn, client, **kwargs)

    monkeypatch.setattr(operations, "execute", execute)
    monkeypatch.setattr(profile_enrichment, "basic_info_steps", steps("basic"))
    monkeypatch.setattr(sync_investment_trust, "investment_trust_steps", steps("trust"))
    monkeypatch.setattr(
        sync_etf_distributions, "etf_distribution_steps", distribution_steps
    )
    monkeypatch.setattr(operations.frequency_inference, "recompute_all", lambda c: None)
    monkeypatch.setattr(operations, "today_taipei", lambda: "2026-10-31")
    return calls


def test_catalog_first_then_profiles_and_distributions_fetch_together(conn, phases):
    """Design 053 §4.2.3: everything after the list is queued before any of
    it is written, and writes keep the old order."""
    result = operations.execute(conn, None, "etf-data", {})

    assert phases == [
        "catalog",
        ("start", "basic", None),
        ("start", "trust", None),
        ("start", "twse", [2026]),
        ("start", "tpex", [2026]),
        ("write", "basic"),
        ("write", "trust"),
        ("write", "twse"),
        ("write", "tpex"),
    ]
    assert result["distributions"]["years"] == [2026]
    assert result["etf_profiles"]["synced"] == 2
    assert "changed_fields" in result["etf_profiles"]


def test_december_also_fetches_next_year(conn, phases, monkeypatch):
    monkeypatch.setattr(operations, "today_taipei", lambda: "2026-12-05")

    operations.execute(conn, None, "etf-data", {})

    assert ("start", "twse", [2026, 2027]) in phases


def test_a_failed_phase_does_not_stop_the_next(conn, phases, monkeypatch):
    real = operations.execute

    def failing(conn, client, operation, options):
        if operation == "catalog":
            raise ValueError("名單來源格式不符")
        return real(conn, client, operation, options)

    monkeypatch.setattr(operations, "execute", failing)

    result = real(conn, None, "etf-data", {})

    assert result["catalog"] == {"error": "名單來源格式不符"}
    assert ("write", "trust") in phases
    assert ("write", "tpex") in phases


def test_waits_out_a_running_held_etf_refresh(conn, phases, monkeypatch):
    attempts, sleeps = [], []

    def busy_then_free(conn, client, market, **kwargs):
        if market == "TWSE":
            attempts.append(1)
            if len(attempts) < 3:
                raise sync_history.SyncInProgress("此來源同步中，請稍後再試")
        yield
        return {"synced": 0}

    monkeypatch.setattr(
        sync_etf_distributions, "etf_distribution_steps", busy_then_free
    )
    monkeypatch.setattr(pacing, "pause", sleeps.append)

    result = operations.execute(conn, None, "etf-data", {})

    assert len(attempts) == 3
    assert sleeps == [operations.SOURCE_WAIT_SECONDS] * 2
    assert result["distributions"]["twse"] == {"synced": 0}


def test_gives_up_on_a_source_still_busy_after_two_minutes(conn, phases, monkeypatch):
    real_steps = sync_etf_distributions.etf_distribution_steps

    def always_busy(conn, client, market, **kwargs):
        if market == "TWSE":
            raise sync_history.SyncInProgress("此來源同步中，請稍後再試")
        return real_steps(conn, client, market, **kwargs)

    monkeypatch.setattr(sync_etf_distributions, "etf_distribution_steps", always_busy)
    monkeypatch.setattr(pacing, "pause", lambda s: None)

    result = operations.execute(conn, None, "etf-data", {})

    assert result["distributions"]["twse"] == {"error": "此來源同步中，請稍後再試"}
    assert ("write", "tpex") in phases


def test_does_not_block_the_income_calendar_refresh():
    """The same reservations as the two buttons it replaces."""
    etf_data = set(sync_jobs.RESOURCES["etf-data"])
    assert etf_data & set(sync_jobs.RESOURCES["catalog"])
    assert etf_data & set(sync_jobs.RESOURCES["investment-trust"])
    assert not etf_data & set(sync_jobs.RESOURCES["held-etf-distributions"])


def _etf(conn, code, market="TWSE"):
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES (?, ?, ?, ?, 'etf', 'active')",
        ("e" + code, market, code, "ETF " + code),
    )


def _synced(conn, codes, attempted_at):
    with sync_history.run(conn, SOURCE) as task:
        for code in codes:
            task.plan(f"{code}:2026", security_id="e" + code, period="2026")
            task.record(f"{code}:2026", "success")
        conn.execute(
            "UPDATE catalog_sync_units SET attempted_at=? WHERE run_id=?",
            (attempted_at, task.id),
        )
    conn.commit()


def test_freshness_is_the_oldest_current_year_sync(conn):
    for code in ("0050", "0056"):
        _etf(conn, code)
    _synced(conn, ["0050", "0056"], "2026-09-29T15:04:12.437Z")
    _synced(conn, ["0056"], "2026-10-31T01:46:45.919Z")  # a held refresh

    label = query.market_distribution_freshness(
        conn, "2026-10-31", sync_history.latest_successes(conn)
    )

    assert label == "全市場配息更新至 2026/09/29"


def test_freshness_counts_etfs_not_yet_synced_this_year(conn):
    for code in ("0050", "0056", "00878"):
        _etf(conn, code)
    _synced(conn, ["0056"], "2026-10-31T01:46:45.919Z")

    successes = sync_history.latest_successes(conn)

    assert query.market_distribution_freshness(conn, "2026-10-31", successes) == (
        "全市場配息更新至 2026/10/31（2 檔尚未同步本年）"
    )
    assert (
        query.market_distribution_freshness(conn, "2027-01-02", successes)
        == "全市場配息尚未同步"
    )


def test_etf_tab_offers_one_button_and_the_freshness(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        _etf(conn, "0050")
        conn.commit()
        conn.close()
        etf = client.get("/securities", params={"security_type": "etf"}).text
        stock = client.get("/securities", params={"security_type": "stock"}).text

    assert 'data-sync-operation="etf-data"' in etf
    assert 'data-sync-operation="catalog"' not in etf
    assert "更新 ETF 資料" not in etf
    assert "全市場配息尚未同步" in etf
    assert 'data-sync-operation="catalog"' in stock
    assert 'data-sync-operation="etf-data"' not in stock


def test_job_summary_names_each_phase():
    job = {
        "operation": "etf-data",
        "result": {
            "catalog": {"error": "HTTP 503"},
            "etf_profiles": {"coverage": {"total": 363, "bank_known": 356}},
            "distributions": {
                "years": [2026],
                "twse": {"synced": 30, "inserted": 4, "updated": 2},
                "tpex": {"error": "此來源同步中，請稍後再試"},
            },
        },
    }

    summary = result_summary(job)

    assert summary.startswith("商品名單失敗：HTTP 503；ETF 363 檔；銀行有值 356")
    assert summary.endswith(
        "全市場配息 30 筆（新增 4、更新 2）、TPEx失敗：此來源同步中，請稍後再試"
    )


def test_parallel_phase_uses_one_short_label(conn, phases, monkeypatch):
    """Design 054 §4.2.4."""

    class Progress:
        phase, detail = "執行中", None

        def update(self, **_):
            pass

    progress = Progress()
    monkeypatch.setattr(operations, "_begin_step", lambda *a: (False, 0))
    monkeypatch.setattr(operations, "_end_step", lambda *a: None)
    token = operations.sync_progress.current.set(progress)
    try:
        operations.execute(conn, None, "etf-data", {})
    finally:
        operations.sync_progress.current.reset(token)

    assert progress.phase == "ETF 資料與配息"
