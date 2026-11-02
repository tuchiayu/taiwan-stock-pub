"""Official closure parsing, durable cache, job admission and payment invariants."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy

import httpx2
import pytest
from fastapi.testclient import TestClient

from taiwan_stock.catalog import market_calendar as calendar
from taiwan_stock.catalog import market_calendar_adapter as adapter
from taiwan_stock.infrastructure import migrate, sync_jobs
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.web.deps import get_http_client


class Sources:
    def __init__(self):
        self.year = 2026
        self.rows = [
            ["2026-01-02", "國曆新年開始交易日", "開始交易。"],
            ["2026-02-12", "市場無交易，僅辦理結算交割作業", ""],
            ["2026-10-09", "國慶日", "補假。"],
        ]
        self.news = [
            [
                1,
                "臺灣證券交易所集中交易市場115年10月15日休市一天",
                "115年10月14日",
                "a123",
                "",
            ]
        ]
        self.changed = False
        self.incomplete = False
        self.failure = None
        self.detail_failure = None
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        if self.failure:
            return httpx2.Response(self.failure)
        if request.url.path.endswith("/holidaySchedule/holidaySchedule"):
            return httpx2.Response(
                200,
                json={
                    "stat": "ok",
                    "date": f"{self.year}0101",
                    "queryYear": self.year,
                    "fields": ["日期", "名稱", "說明"],
                    "data": self.rows,
                },
            )
        if request.url.path.endswith("/news/newsList"):
            rows = self.news if request.url.params["startDate"] == "20260101" else []
            return httpx2.Response(
                200,
                json={
                    "stat": "ok",
                    "fields": ["項次", "標題", "日期", "zhId", "enId"],
                    "data": rows,
                    "totalCount": len(rows) + int(self.incomplete),
                },
            )
        if "/news/newsDetail/" in request.url.path:
            if self.detail_failure:
                return httpx2.Response(self.detail_failure)
            row = next(row for row in self.news if request.url.path.endswith(row[3]))
            return httpx2.Response(
                200,
                json={
                    "stat": "ok",
                    "date": "202610141900",
                    "tables": [
                        {
                            "title": row[1],
                            "fields": ["text", "html", "pdf", "lang"],
                            "data": [
                                "",
                                "",
                                "/changed.pdf" if self.changed else "/original.pdf",
                                "",
                            ],
                        }
                    ],
                },
            )
        raise AssertionError(str(request.url))


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(calendar, "today_taipei", lambda: "2026-10-17")
    with closing(get_connection(tmp_path)) as connection:
        migrate.apply_pending(connection)
        yield connection


@pytest.fixture
def sources():
    return Sources()


@pytest.fixture
def transport(sources):
    with httpx2.Client(transport=httpx2.MockTransport(sources)) as client:
        yield client


def expire(conn):
    conn.execute(
        "UPDATE market_calendar_coverage SET next_attempt_at='2000',"
        "attempted_at='2000-01-01T00:00:00.000Z'"
    )


def test_years_and_open_reminders_are_validated(transport, sources):
    events, _, reasons = adapter.annual(transport, 2026)
    assert not reasons
    assert [e["status"] for e in events] == ["open", "closed", "closed"]
    assert sources.requests[0].url.params["date"] == "20260101"
    sources.year = 2025
    with pytest.raises(ValueError, match="年份不符"):
        adapter.annual(transport, 2026)


def test_announcements_use_event_date_and_validated_details(transport, sources):
    sources.news.append([2, "個股暫停交易及休市注意事項", "115年10月14日", "b456", ""])
    events, _, reasons = adapter.notices(transport, 2026)
    assert len(events) == 1
    assert events[0]["date"] == "2026-10-15"
    assert events[0]["published"] == "2026-10-14"
    assert events[0]["url"].endswith("?a123")
    assert not reasons
    sources.incomplete = True
    with pytest.raises(ValueError, match="未完整取得"):
        adapter.notices(transport, 2026)


@pytest.mark.parametrize(
    "title",
    [
        "臺灣證券交易所集中交易市場115年10月15日下午停止交易",
        "更正：臺灣證券交易所集中交易市場115年10月15日休市公告",
        "臺灣證券交易所集中交易市場115年10月15日至16日休市",
    ],
)
def test_unclear_announcements_are_not_confirmed(transport, sources, title):
    sources.news[0][1] = title
    events, _, _ = adapter.notices(transport, 2026)
    assert not any(e["status"] == "closed" for e in events)


def test_success_cache_avoids_repeat_network_and_sources_stay_separate(
    conn, transport, sources
):
    outcome = calendar.synchronize(conn, transport, 2026)
    assert outcome == {"updated": 4, "failed_units": 0}
    requests = len(sources.requests)
    calendar.synchronize(conn, transport, 2026)
    assert len(sources.requests) == requests
    data = calendar.view(conn, 2026)
    assert data["events"]["2026-10-15"]["name"] == "臨時休市"
    assert not data["needsRefresh"]
    assert calendar.day_info("2026-10-17")["closed"]
    assert not calendar.day_info("2026-10-17", {"status": "open"})["closed"]


def test_failed_refresh_keeps_last_data_and_uses_backoff(conn, transport, sources):
    calendar.synchronize(conn, transport, 2026)
    before = calendar.view(conn, 2026)
    expire(conn)
    sources.failure = 503
    assert calendar.synchronize(conn, transport, 2026)["failed_units"] == 2
    result = calendar.view(conn, 2026)
    assert result["events"] == before["events"]
    assert result["annualUpdatedAt"] == before["annualUpdatedAt"]
    assert result["annualStatus"] == "failed"
    assert not result["needsRefresh"]


def test_future_empty_calendar_and_out_of_range_do_not_use_current_year(
    conn, transport, sources
):
    sources.year, sources.rows, sources.news = 2027, [], []
    calendar.synchronize(conn, transport, 2027)
    data = calendar.view(conn, 2027)
    assert data["annualStatus"] == "empty"
    assert not data["events"]
    assert not data["needsRefresh"]
    assert not calendar.view(conn, 2028)["supported"]
    assert not calendar.view(conn, 2020)["needsRefresh"]


def test_changed_and_missing_notice_is_reviewed_not_deleted(conn, transport, sources):
    calendar.synchronize(conn, transport, 2026)
    expire(conn)
    sources.changed = True
    calendar.synchronize(conn, transport, 2026)
    data = calendar.view(conn, 2026)
    assert data["events"]["2026-10-15"]["status"] == "needs_review"
    assert data["noticeStatus"] == "partial"
    assert conn.execute(
        "SELECT previous_snapshot FROM market_calendar_events WHERE source='notices'"
    ).fetchone()[0]
    expire(conn)
    sources.news.clear()
    calendar.synchronize(conn, transport, 2026)
    assert calendar.view(conn, 2026)["events"]["2026-10-15"]["needsReview"]
    assert calendar.view(conn, 2026)["events"]["2026-10-09"]["status"] == "closed"


def test_empty_or_incomplete_refresh_never_erases_cached_events(
    conn, transport, sources
):
    calendar.synchronize(conn, transport, 2026)
    old = deepcopy(calendar.view(conn, 2026)["events"])
    expire(conn)
    sources.incomplete = True
    sources.rows = []
    calendar.synchronize(conn, transport, 2026)
    assert calendar.view(conn, 2026)["events"] == old
    assert calendar.view(conn, 2026)["annualStatus"] == "partial"


def test_cross_year_announcement_is_found_in_previous_year(transport, sources):
    sources.news[0][1] = "臺灣證券交易所集中交易市場116年1月4日休市一天"
    events, _, _ = adapter.notices(transport, 2027)
    assert events[0]["date"] == "2027-01-04"


def test_api_refresh_is_idempotent_and_fresh_reads_launch_nothing(
    tmp_path, conn, transport
):
    app = create_app(tmp_path)
    app.dependency_overrides[get_http_client] = lambda: transport
    with TestClient(app) as client:
        before = client.get("/api/v2/market-calendar?year=2026")
        assert before.status_code == 200
        assert before.json()["needsRefresh"]
        assert before.headers["cache-control"] == "no-store"
        payload = {"year": 2026, "requestKey": "one"}
        first = client.post("/api/v2/market-calendar/refresh", json=payload)
        assert first.status_code == 202
        job_id = first.json()["jobId"]
        assert client.get(f"/api/v2/sync-jobs/{job_id}").json()["status"] == "success"
        assert (
            client.post("/api/v2/market-calendar/refresh", json=payload).json()["jobId"]
            == job_id
        )
        second = client.post(
            "/api/v2/market-calendar/refresh", json={**payload, "requestKey": "two"}
        )
        assert second.status_code == 200
        assert second.json()["jobId"] is None
        assert conn.execute("SELECT count(*) FROM sync_jobs").fetchone()[0] == 1
        assert (
            client.post(
                "/api/v2/market-calendar/refresh", json={**payload, "year": 2028}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v2/market-calendar/refresh",
                json=payload,
                headers={"origin": "https://elsewhere.test"},
            ).status_code
            == 403
        )


def test_new_tables_can_be_removed_and_reapplied_without_touching_income(conn):
    assert migrate.apply_pending(conn) == []
    conn.execute("DROP TABLE market_calendar_events")
    conn.execute("DROP TABLE market_calendar_coverage")
    conn.execute("DELETE FROM schema_migrations WHERE version=16")
    assert migrate.apply_pending(conn) == [16]
    assert conn.execute("SELECT count(*) FROM distributions").fetchone()[0] == 0


def test_two_tabs_recheck_freshness_under_admission_lease(tmp_path, conn, transport):
    def submit(index):
        with closing(get_connection(tmp_path)) as own:
            return sync_jobs.submit(
                own,
                tmp_path,
                "market-calendar",
                {"year": 2026},
                request_key=f"tab-{index}",
                admit=lambda: bool(calendar.due_sources(own, 2026)),
                launcher=lambda data_dir, job_id: sync_jobs.run_worker(
                    data_dir, job_id, client=transport
                ),
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert sum(result is not None for result in results) == 1
    assert conn.execute("SELECT count(*) FROM sync_jobs").fetchone()[0] == 1
    assert not calendar.view(conn, 2026)["needsRefresh"]


def seed_payment(conn):
    conn.execute(
        "INSERT INTO securities(id,market,code,name,security_type,status) "
        "VALUES('etf','TWSE','00878','測試 ETF','etf','active')"
    )
    conn.execute(
        "INSERT INTO broker_accounts(id,name,status) VALUES('a','帳戶','active')"
    )
    # Seed using the legacy schema too: current repositories require current migrations.
    conn.execute(
        "INSERT INTO trades(id,broker_account_id,security_id,event_type,trade_date,"
        "quantity,unit_price,executed_amount,commission) "
        "VALUES('trade','a','etf','buy','2024-01-01',1000,'50','50000','71')"
    )
    conn.execute(
        "INSERT INTO distributions(id,security_id,ex_dividend_date,pay_date,"
        "amount_per_share,amount_status) "
        "VALUES('d','etf','2026-10-01','2026-10-15','5','final')"
    )


def test_closure_rendering_never_moves_or_recalculates_announced_payments(
    conn, tmp_path, transport
):
    seed_payment(conn)
    with TestClient(create_app(tmp_path)) as client:
        path = "/income-calendar?month=2026-10&filter=account:a"
        before = client.get(path)
        api_before = client.get("/api/v2/income-calendar").json()
        calendar.synchronize(conn, transport, 2026)
        after = client.get(path)
        assert after.status_code == 200
        assert (
            after.context["overview"]["gross"]
            == before.context["overview"]["gross"]
            == 5000
        )
        assert (
            after.context["overview"]["count"]
            == before.context["overview"]["count"]
            == 1
        )
        assert after.context["entries"] == before.context["entries"]
        assert client.get("/api/v2/income-calendar").json() == api_before
        assert 'class="income-day income-day-payment income-day-closed' in after.text
        assert 'data-income-dialog="payments-2026-10-15"' in after.text
        assert '<small class="income-day-code">00878</small>' in after.text
        assert "臨時休市" in after.text
        assert "data-market-retry hidden" in after.text
        assert after.context["market_calendar"]["noticeStatus"] == "success"


def test_migration_from_015_preserves_existing_distribution(tmp_path, monkeypatch):
    with closing(get_connection(tmp_path)) as connection:
        all_migrations = migrate.discover_migrations()
        with monkeypatch.context() as patch:
            patch.setattr(
                migrate,
                "discover_migrations",
                lambda: [m for m in all_migrations if m.version <= 15],
            )
            migrate.apply_pending(connection)
        seed_payment(connection)
        old = dict(connection.execute("SELECT * FROM distributions").fetchone())
        assert migrate.apply_pending(connection) == [16, 17, 18, 19, 20, 21, 22, 23]
        assert dict(connection.execute("SELECT * FROM distributions").fetchone()) == old


def test_invalid_news_ids_and_wrong_detail_titles_are_rejected(transport, sources):
    sources.news[0][3] = "../not-an-id"
    with pytest.raises(ValueError, match="識別異常"):
        adapter.notices(transport, 2026)


def test_force_refresh_still_respects_minimum_request_interval(conn, transport):
    calendar.synchronize(conn, transport, 2026)
    assert not calendar.due_sources(conn, 2026, force=True)
    conn.execute(
        "UPDATE market_calendar_coverage SET attempted_at='2000-01-01T00:00:00.000Z'"
    )
    assert calendar.due_sources(conn, 2026, force=True) == ["annual", "notices"]


def test_failed_worker_launch_is_throttled_and_reported(tmp_path, conn):
    def fail(*args):
        raise OSError("test launch failure")

    job = sync_jobs.submit(
        conn,
        tmp_path,
        "market-calendar",
        {"year": 2026},
        launcher=fail,
        admit=lambda: bool(calendar.due_sources(conn, 2026)),
    )
    assert job["status"] == "failed"
    data = calendar.view(conn, 2026)
    assert data["annualStatus"] == "failed"
    assert not data["needsRefresh"]
    assert (
        sync_jobs.submit(
            conn,
            tmp_path,
            "market-calendar",
            {"year": 2026},
            launcher=fail,
            admit=lambda: bool(calendar.due_sources(conn, 2026)),
        )
        is None
    )
    assert conn.execute("SELECT count(*) FROM sync_jobs").fetchone()[0] == 1


def test_correction_does_not_leave_an_older_notice_confirmed(conn, transport, sources):
    calendar.synchronize(conn, transport, 2026)
    expire(conn)
    sources.news.append(
        [
            2,
            "更正：臺灣證券交易所集中交易市場115年10月15日休市公告",
            "115年10月14日",
            "b456",
            "",
        ]
    )
    calendar.synchronize(conn, transport, 2026)
    event = calendar.view(conn, 2026)["events"]["2026-10-15"]
    assert event["status"] == "needs_review"
    assert event["needsReview"]


def test_missing_notice_details_are_reviewed(conn, transport, sources):
    calendar.synchronize(conn, transport, 2026)
    expire(conn)
    sources.detail_failure = 404
    calendar.synchronize(conn, transport, 2026)
    assert calendar.view(conn, 2026)["events"]["2026-10-15"]["status"] == "needs_review"
