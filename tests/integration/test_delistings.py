import json
from pathlib import Path
from urllib.parse import parse_qs, urlencode

import httpx2
import pytest
from fastapi.testclient import TestClient

from taiwan_stock import cli
from taiwan_stock.catalog import delisting_sources as sources
from taiwan_stock.catalog import (
    delistings,
    search,
    sync,
    sync_daily_prices,
    sync_etf_distributions,
    sync_history,
    sync_investment_trust,
    twse_adapter,
)
from taiwan_stock.infrastructure import migrate, sync_jobs
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app

# Duplicated from tests/unit/test_delisting_sources.py rather than
# imported — this project's test files aren't packages (no __init__.py),
# so a `from tests.unit... import` resolves locally under `python -m
# pytest` (which puts the cwd on sys.path) but breaks under CI's plain
# `pytest` invocation, which does not.
_FIXTURES = Path(__file__).parents[1] / "fixtures" / "delistings"


def _fixture(name):
    return (_FIXTURES / name).read_text(encoding="utf-8")


def official_handler(request):
    path = request.url.path
    if path == "/v1/company/suspendListingCsvAndHtml":
        return httpx2.Response(200, text=_fixture("twse_stocks.json"))
    if path == "/www/zh-tw/company/deListed":
        return httpx2.Response(200, text=_fixture("tpex_stocks.json"))
    if path == "/zh/ETFortune/announcementList":
        href = (
            "/zh/ETFortune/announcement?company=A00005&amp;date=20220425"
            "&amp;seq=1&amp;fund=0054&amp;type=listing"
        )
        return httpx2.Response(
            200,
            text=f'<a href="{href}">本基金終止上市日及信託契約終止事宜</a>',
        )
    if path == "/zh/ETFortune/announcement":
        return httpx2.Response(200, text=_fixture("twse_0054.html"))
    if path == "/api/etfMaInfo":
        return httpx2.Response(
            200,
            json=[
                dict(
                    subject="本基金終止上櫃日及信託契約終止事宜",
                    params="company=A00016&date=20250723&seq=1&fund=00794B&type=listing",
                )
            ],
        )
    if path == "/api/etfMaInfoDetail":
        return httpx2.Response(200, text=_fixture("tpex_00794b.json"))
    return httpx2.Response(404)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(delistings, "today_taipei", lambda: "2026-09-16")
    c = get_connection(tmp_path)
    migrate.apply_pending(c)
    for market, code, kind in [
        ("TWSE", "0054", "etf"),
        ("TPEx", "00794B", "etf"),
        ("TWSE", "2867", "stock"),
        ("TPEx", "5371", "stock"),
        ("TWSE", "0056", "etf"),
    ]:
        sid = market + code
        c.execute(
            "INSERT INTO securities"
            "(id,market,code,name,security_type,status,listed_date) "
            "VALUES(?,?,?,?,?,'active','2000-01-01')",
            (sid, market, code, code, kind),
        )
        if kind == "etf":
            c.execute("INSERT INTO etf_profiles(security_id) VALUES(?)", (sid,))
    yield c
    c.close()


def run(conn, handler=official_handler, **kwargs):
    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        return delistings.sync_delistings(conn, client, **kwargs)


def test_sync_four_sources_and_idempotency_preserve_other_market(conn):
    # The TWSE listing of a former TPEx stock is a separate market identity.
    conn.execute(
        "INSERT INTO securities(id,market,code,name,security_type,status) "
        "VALUES('new','TWSE','5371','new','stock','active')"
    )
    result = run(conn)
    assert result["errors"] == {}
    assert result["synced"] == 4
    assert sync_history.get_report(conn, result["run_id"])["status"] == "success"
    assert (
        conn.execute(
            "SELECT count(*) FROM securities WHERE status='delisted'"
        ).fetchone()[0]
        == 4
    )
    assert (
        conn.execute("SELECT status FROM securities WHERE id='new'").fetchone()[0]
        == "active"
    )
    run(conn)
    assert conn.execute("SELECT count(*) FROM security_delistings").fetchone()[0] == 4


def test_future_effective_date_and_basic_catalog_never_revives(conn, monkeypatch):
    monkeypatch.setattr(delistings, "today_taipei", lambda: "2022-05-26")
    run(conn)
    assert delistings.details(conn)["TWSE0054"]["status"] == "active"
    assert delistings.details(conn)["TWSE0054"]["delisted_date"] == "2022-05-27"
    monkeypatch.setattr(delistings, "today_taipei", lambda: "2022-05-27")
    assert search.search_securities(conn, query="0054")[0]["status"] == "delisted"
    security, _ = twse_adapter.normalize_etf(
        {"基金代號": "0054", "基金中文名稱": "元大台商50"}
    )
    sync.upsert_security(conn, security)
    assert (
        conn.execute("SELECT status FROM securities WHERE code='0054'").fetchone()[0]
        == "delisted"
    )


def test_old_lifecycle_event_does_not_delist_new_listing(conn):
    conn.execute("UPDATE securities SET listed_date='2026-01-01' WHERE code='0054'")
    run(conn)
    assert (
        conn.execute("SELECT status FROM securities WHERE code='0054'").fetchone()[0]
        == "active"
    )


def test_failed_index_and_detail_leave_status_unchanged_and_retry(conn):
    def handler(request):
        if request.url.path.endswith("announcementList") or request.url.path.endswith(
            "etfMaInfoDetail"
        ):
            return httpx2.Response(503)
        return official_handler(request)

    result = run(conn, handler)
    assert len(result["errors"]) == 2
    delisted_etf_count = (
        "SELECT count(*) FROM securities "
        "WHERE security_type='etf' AND status='delisted'"
    )
    assert conn.execute(delisted_etf_count).fetchone()[0] == 0
    retry = run(conn, retry_run_id=result["run_id"])
    assert not retry["errors"]
    assert conn.execute(delisted_etf_count).fetchone()[0] == 2


def test_circuit_breaker_stops_after_repeated_blocking_failures(conn):
    """Live 2026-09-16: TWSE started returning HTTP 428 partway through a
    run with many candidate notices; grinding through the rest at the same
    pace just burns the run on guaranteed failures (design decision mirrors
    sync.py's ETF-distribution circuit breaker) — the remainder should stay
    `pending` for a later `sync retry`, not get recorded as failed too."""
    codes = [f"100{i}" for i in range(6)]
    for code in codes:
        sid = "TWSE" + code
        conn.execute(
            "INSERT INTO securities(id,market,code,name,security_type,status) "
            "VALUES(?,'TWSE',?,?,'etf','active')",
            (sid, code, code),
        )
        conn.execute("INSERT INTO etf_profiles(security_id) VALUES(?)", (sid,))
    links = "".join(
        f'<a href="/zh/ETFortune/announcement?company=A000{i}&date=2022010{i}'
        f'&seq=1&fund={code}&type=listing">終止上市公告</a>'
        for i, code in enumerate(codes)
    )

    def handler(request):
        if request.url.path == "/zh/ETFortune/announcementList":
            return httpx2.Response(200, text=links)
        if request.url.path == "/zh/ETFortune/announcement":
            return httpx2.Response(428)
        if request.url.path == "/api/etfMaInfo":
            return httpx2.Response(
                200,
                json=[
                    {
                        "subject": "不相關",
                        "params": (
                            "company=A0&date=20220101&seq=1&fund=NOMTCH&type=listing"
                        ),
                    }
                ],
            )
        return httpx2.Response(404)

    result = run(conn, handler)
    units = sync_history.get_report(conn, result["run_id"])["units"]
    notice_units = [u for u in units if u["stage"] == "termination_notice"]
    assert len(notice_units) == 6
    attempted = [u for u in notice_units if u["status"] != "pending"]
    pending = [u for u in notice_units if u["status"] == "pending"]
    assert len(attempted) == delistings._CIRCUIT_BREAKER_THRESHOLD
    assert len(pending) == 1
    assert all(u["status"] == "http_failed" for u in attempted)
    assert (
        conn.execute(
            "SELECT count(*) FROM securities "
            "WHERE code LIKE '100%' AND status='delisted'"
        ).fetchone()[0]
        == 0
    )


def test_conflicting_date_preserves_evidence(conn):
    run(conn)
    value = sources.event(
        "TWSE", "etf", "0054", "2022-06-01", "https://www.twse.com.tw/correction"
    )
    with pytest.raises(ValueError, match="待人工核對"):
        delistings.store_event(conn, value)
    assert delistings.details(conn)["TWSE0054"]["delisted_date"] == "2022-05-27"


def test_termination_skips_distribution_and_trust_retry_without_network(conn):
    # Record a genuine earlier failure, then discover that it is delisted.
    with sync_history.run(conn, "investment_trust") as old:
        old.plan("元大:0054", security_id="TWSE0054", issuer="元大")
        old.record("元大:0054", "http_failed", reason="HTTP 404")
    conn.execute("UPDATE securities SET name='元大台商50' WHERE code='0054'")
    run(conn)

    def forbidden(request):
        pytest.fail(f"Delisted product must not trigger request: {request.url}")

    with httpx2.Client(transport=httpx2.MockTransport(forbidden)) as client:
        result = sync_investment_trust.sync_investment_trust_data(
            conn, client, retry_run_id=old.id
        )
        report = sync_history.get_report(conn, result["run_id"])
        assert report["counts"] == {"skipped_delisted": 1}
        assert report["status"] == "success"
        # Restrict a distribution retry to 0054; the still-active 0056 is irrelevant.
        with sync_history.run(conn, "twse_etf_distribution_list") as dist:
            dist.plan("0054:2026", security_id="TWSE0054", period="2026")
            dist.record("0054:2026", "parse_failed")
        result = sync_etf_distributions.sync_etf_distributions(
            conn, client, retry_run_id=dist.id
        )
        assert sync_history.get_report(conn, result["run_id"])["counts"] == {
            "skipped_delisted": 1
        }
    assert sync_history.get_report(conn, old.id)["counts"] == {"http_failed": 1}


def test_prices_skip_delisted_and_keep_history(conn):
    run(conn)
    conn.execute(
        "INSERT INTO daily_prices(security_id,trade_date,close) "
        "VALUES('TWSE0054','2022-05-26','10')"
    )
    rows = [
        {"Date": "1150916", "Code": code, "ClosingPrice": "20", "TradeVolume": "100"}
        for code in ("0054", "0056")
    ]
    with httpx2.Client(
        transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json=rows))
    ) as client:
        result = sync_daily_prices.sync_daily_prices(conn, client)
    assert result["synced"] == 1
    assert result["skipped_delisted"] == 1
    assert conn.execute(
        "SELECT trade_date,close FROM daily_prices WHERE security_id='TWSE0054'"
    ).fetchall()[0][:] == ("2022-05-26", "10")


def test_ui_shows_delisted_and_scheduled_without_deleting_watchlist(
    conn, tmp_path, monkeypatch
):
    conn.execute("INSERT INTO watchlist(security_id) VALUES('TWSE0054')")
    run(conn)
    with TestClient(create_app(tmp_path)) as client:
        page = client.get("/securities?security_type=etf&view=watchlist")
        assert "已下市 · 2022-05-27" in page.text
        # Design 048 §4.2.2: bank and frequency read 已下市, not 已終止交易.
        assert '<span class="profile-value tier-unknown">已下市</span>' in page.text
        assert "已終止交易" not in page.text
        detail = client.get("/securities/TWSE0054")
        assert "保留歷史資料，略過日常同步" in detail.text
        assert "已下市（2022-05-27）" in detail.text
    assert conn.execute("SELECT count(*) FROM watchlist").fetchone()[0] == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_cli_delistings_and_failure_exit(conn, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "new_client",
        lambda: httpx2.Client(transport=httpx2.MockTransport(official_handler)),
    )
    assert cli.main(["sync", "delistings", "--data-dir", str(tmp_path)]) == 0
    assert "上市／上櫃狀態" in capsys.readouterr().out
    monkeypatch.setattr(
        cli,
        "new_client",
        lambda: httpx2.Client(
            transport=httpx2.MockTransport(lambda r: httpx2.Response(503))
        ),
    )
    assert cli.main(["sync", "delistings", "--data-dir", str(tmp_path)]) == 1


def test_upgrade_adds_evidence_table_without_changing_legacy_data(
    tmp_path, monkeypatch
):
    c = get_connection(tmp_path)
    migrations = migrate.discover_migrations()
    with monkeypatch.context() as m:
        m.setattr(
            migrate,
            "discover_migrations",
            lambda: [item for item in migrations if item.version < 12],
        )
        migrate.apply_pending(c)
    c.execute(
        "INSERT INTO securities(id,market,code,name,security_type,status) "
        "VALUES('s','TWSE','0054','legacy','etf','active')"
    )
    c.execute("INSERT INTO watchlist(security_id) VALUES('s')")
    assert migrate.apply_pending(c) == [12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23]
    assert migrate.apply_pending(c) == []
    assert (
        c.execute("SELECT status FROM securities WHERE id='s'").fetchone()[0]
        == "active"
    )
    assert c.execute("SELECT security_id FROM watchlist").fetchone()[0] == "s"
    c.close()


@pytest.mark.parametrize("retry_legacy", [False, True])
def test_six_notice_regression_job_counts_reports_retries_and_lifecycles(
    conn, tmp_path, retry_legacy
):
    cases = json.loads(_fixture("notice_cases.json"))
    existing_dates = {
        "00732": "2023-04-21",
        "00743": "2022-09-29",
        "00793B": "2026-09-18",
    }
    for case in cases:
        code, market = case["params"]["fund"], case["market"]
        sid = market + code
        conn.execute(
            "INSERT INTO securities"
            "(id,market,code,name,security_type,status,listed_date) "
            "VALUES(?,?,?,?,'etf','active','2000-01-01')",
            (sid, market, code, code),
        )
        conn.execute("INSERT INTO etf_profiles(security_id) VALUES(?)", (sid,))
        if code in existing_dates:
            delistings.store_event(
                conn,
                sources.event(
                    market,
                    "etf",
                    code,
                    existing_dates[code],
                    "https://example.com/later-final-notice/" + code,
                ),
            )
    conn.execute(
        "INSERT INTO securities(id,market,code,name,security_type,status) "
        "VALUES('other-market','TPEx','0080','other','etf','active')"
    )
    conn.execute("INSERT INTO watchlist(security_id) VALUES('TWSE0080')")
    attempted = []

    def handler(request):
        if request.url.path.endswith("announcementList"):
            return httpx2.Response(
                200,
                text="".join(
                    f'<a href="/zh/ETFortune/announcement?{urlencode(c["params"])}">'
                    f"{c['subject']}</a>"
                    for c in cases
                    if c["market"] == "TWSE"
                ),
            )
        if request.url.path == "/api/etfMaInfo":
            return httpx2.Response(
                200,
                json=[
                    {"params": urlencode(c["params"]), "subject": c["subject"]}
                    for c in cases
                    if c["market"] == "TPEx"
                ],
            )
        if request.url.path.endswith(("announcement", "etfMaInfoDetail")):
            params = (
                dict(request.url.params)
                if request.method == "GET"
                else {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            )
            case = next(c for c in cases if c["params"] == params)
            attempted.append(params["fund"])
            if case["market"] == "TPEx":
                return httpx2.Response(200, json=[{**case, "stockNo": params["fund"]}])
            return httpx2.Response(
                200,
                text=(
                    f"<table><tr><th>主旨</th><td>{case['subject']}</td></tr>"
                    f"<tr><th>說明</th><td>{case['description']}</td></tr></table>"
                ),
            )
        return official_handler(request)

    previous = None
    if retry_legacy:
        # Model the six historical failures without modifying the old run later.
        with sync_history.run(conn, delistings.SOURCE) as previous:
            for case in cases:
                p = case["params"]
                key = f"{case['market']}:etf:{p['fund']}:{p['date']}:{p['seq']}"
                previous.plan(key, security_id=case["market"] + p["fund"])
                previous.record(key, "parse_failed", reason="legacy parse failure")

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as http:

        def launch(data_dir, job_id):
            sync_jobs.run_worker(data_dir, job_id, client=http)

        job = sync_jobs.submit(
            conn,
            tmp_path,
            "retry" if previous else "delistings",
            {"run_id": previous.id} if previous else {},
            launcher=launch,
        )
        assert job["status"] == "success", job
        assert job["failed_units"] == 0
        assert job["processed"] == job["total"] == (8 if previous else 10)
        report = sync_history.get_report(conn, job["result"]["run_id"])
        assert report["status"] == "success"
        assert report["counts"]["skipped_notice"] == 3
        skipped = [u for u in report["units"] if u["status"] == "skipped_notice"]
        assert all(
            u["source_url"]
            and u["reason"]
            and not u["error_type"]
            and u["row_count"] == 0
            for u in skipped
        )
        assert len(attempted) == 6

        # Successful/ignored child units must never be fetched by a source retry.
        attempted.clear()
        repeated = run(conn, handler, retry_run_id=report["id"])
        assert not repeated["errors"]
        assert not attempted

        # A full rerun remains idempotent and does not duplicate event evidence.
        again = run(conn, handler)
        assert not again["errors"]
    for case in cases:
        code = case["params"]["fund"]
        row = conn.execute(
            "SELECT status FROM securities WHERE id=?", (case["market"] + code,)
        ).fetchone()
        expected = case["effective_date"] or existing_dates[code]
        assert row[0] == ("active" if expected > "2026-09-16" else "delisted")
        assert (
            conn.execute(
                "SELECT effective_date FROM security_delistings "
                "WHERE market=? AND code=?",
                (case["market"], code),
            ).fetchone()[0]
            == expected
        )
    assert conn.execute("SELECT count(*) FROM security_delistings").fetchone()[0] == 8
    assert (
        conn.execute(
            "SELECT status FROM securities WHERE id='other-market'"
        ).fetchone()[0]
        == "active"
    )
    assert conn.execute("SELECT count(*) FROM watchlist").fetchone()[0] == 1
    assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    if previous:
        old = sync_history.get_report(conn, previous.id)
        assert old["status"] == "failed" and old["counts"] == {"parse_failed": 6}
    with TestClient(create_app(tmp_path)) as web:
        page = web.get(f"/catalog-sync-runs/{report['id']}")
        assert page.status_code == 200
        assert "前置公告，略過" in page.text
        assert "不計失敗" in page.text
        assert skipped[0]["source_url"].replace("&", "&amp;") in page.text

    # An old delisting cannot apply to a new listing using the same code.
    conn.execute(
        "UPDATE securities SET status='active', listed_date='2016-01-01' "
        "WHERE id='TWSE0080'"
    )
    run(conn, handler)
    assert (
        conn.execute("SELECT status FROM securities WHERE id='TWSE0080'").fetchone()[0]
        == "active"
    )
