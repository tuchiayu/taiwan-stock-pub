"""ETF size and holder snapshots, freshness and the securities table (design 044)."""

import re
from contextlib import closing

import httpx2
import pytest
from fastapi.testclient import TestClient

from taiwan_stock.catalog import etf_scale, query
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.web.deps import get_http_client

TDCC_HEAD = "資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%\n"


class Sources:
    def __init__(self):
        self.month = "202609"
        self.sizes = {"0050": "2,469,507,995,378", "00940": "66,780,183,903"}
        self.day = "20261022"
        self.holders = {"0050  ": "3720414", "00940 ": "307579", "00635U": "58330"}
        self.fail = set()
        self.calls = []

    def __call__(self, request):
        host = request.url.host
        self.calls.append(host)
        if host in self.fail:
            return httpx2.Response(503)
        if request.url.path.endswith("etf_statement.aspx"):
            return httpx2.Response(
                200, text=f'<select><option value="{self.month}">x</option></select>'
            )
        if request.url.path.endswith("etf_statement2.aspx"):
            head = "".join(
                f"<th>{c}</th>"
                for c in (
                    "證券代號",
                    "基金規模(台幣)",
                    "單位淨值(台幣)",
                    "受益權單位數",
                )
            )
            rows = "".join(
                f"<tr><td>{code}</td><td>{size}</td><td>10.5</td><td>1,000</td></tr>"
                for code, size in self.sizes.items()
            )
            return httpx2.Response(200, text=f"<table><tr>{head}</tr>{rows}</table>")
        body = TDCC_HEAD + "".join(
            f"{self.day},{code},17,{count},1000,100.00\n"
            for code, count in self.holders.items()
        )
        return httpx2.Response(200, content=("﻿" + body).encode("utf-8"))


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(etf_scale, "today_taipei", lambda: "2026-10-29")
    with closing(get_connection(tmp_path)) as connection:
        migrate.apply_pending(connection)
        for sid, market, code, name, status in (
            ("e50", "TWSE", "0050", "元大台灣50", "active"),
            ("e940", "TWSE", "00940", "元大台灣價值高息", "active"),
            ("gold", "TWSE", "00635U", "元大S&P黃金", "active"),
            ("old", "TPEx", "00940", "舊上櫃同代碼", "delisted"),
            ("gone", "TWSE", "00888", "已下市", "delisted"),
        ):
            connection.execute(
                "INSERT INTO securities(id,market,code,name,security_type,status) "
                "VALUES(?,?,?,?,'etf',?)",
                (sid, market, code, name, status),
            )
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
        "UPDATE etf_scale_coverage SET next_attempt_at='2000',"
        "attempted_at='2000-01-01T00:00:00.000Z'"
    )


def test_sync_saves_both_sources_and_maps_codes_to_trading_rows(conn, transport):
    result = etf_scale.synchronize(conn, transport)
    assert result["failed_units"] == 0
    assert result["periods"] == {"size": "2026-09", "holders": "2026-10-22"}
    sizes = dict(
        conn.execute("SELECT security_id,net_assets_twd FROM etf_size_snapshots")
    )
    assert sizes == {"e50": 2_469_507_995_378, "e940": 66_780_183_903}
    holders = dict(conn.execute("SELECT security_id,holders FROM etf_holder_snapshots"))
    # The active TWSE 00940 wins over the delisted TPEx row with the same code.
    assert holders == {"e50": 3_720_414, "e940": 307_579, "gold": 58_330}
    run = conn.execute(
        "SELECT status FROM catalog_sync_runs WHERE source='etf-scale'"
    ).fetchone()
    assert run[0] == "success"


def test_fresh_data_is_not_fetched_again_and_new_periods_keep_history(
    conn, transport, sources, monkeypatch
):
    etf_scale.synchronize(conn, transport)
    calls = len(sources.calls)
    assert etf_scale.due_sources(conn) == []
    etf_scale.synchronize(conn, transport)
    assert len(sources.calls) == calls
    # Holders are a week old; September is still the latest size month.
    sources.day, sources.holders["0050  "] = "20261029", "3800000"
    assert etf_scale.due_sources(conn, today="2026-10-31") == []  # retry interval
    expire(conn)
    assert etf_scale.due_sources(conn, today="2026-10-31") == ["holders"]
    monkeypatch.setattr(etf_scale, "today_taipei", lambda: "2026-10-31")
    etf_scale.synchronize(conn, transport)
    rows = conn.execute(
        "SELECT data_date,holders FROM etf_holder_snapshots WHERE security_id='e50' "
        "ORDER BY data_date"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("2026-10-22", 3_720_414),
        ("2026-10-29", 3_800_000),
    ]
    latest = etf_scale.latest(conn)
    assert latest["holders_date"] == "2026-10-29"
    assert latest["holders"]["e50"]["holders"] == 3_800_000


def test_unchanged_period_waits_for_the_retry_interval(conn, transport):
    etf_scale.synchronize(conn, transport)
    # November: SITCA's October report is due but not published yet.
    assert etf_scale.due_sources(conn, today="2026-11-20") == []
    expire(conn)
    assert "size" in etf_scale.due_sources(conn, today="2026-11-20")


def test_one_failing_source_keeps_old_data_and_does_not_block_the_other(
    conn, transport, sources, monkeypatch
):
    etf_scale.synchronize(conn, transport)
    expire(conn)
    sources.fail.add("www.sitca.org.tw")
    sources.day = "20261203"
    monkeypatch.setattr(etf_scale, "today_taipei", lambda: "2026-12-04")
    result = etf_scale.synchronize(conn, transport)
    assert result["failed_units"] == 1
    assert conn.execute("SELECT count(*) FROM etf_size_snapshots").fetchone()[0] == 2
    assert etf_scale.latest(conn)["holders_date"] == "2026-12-03"
    view = etf_scale.view(conn)
    assert "規模更新失敗，保留既有資料" in view["message"]
    assert view["canRetry"] is False  # within the 30-second force interval
    expire(conn)
    assert etf_scale.view(conn)["canRetry"] is True
    assert etf_scale.due_sources(conn, force=True) == ["size"]


def test_same_period_overwrites_corrected_values(conn, transport, sources):
    etf_scale.synchronize(conn, transport)
    sources.sizes["0050"] = "2,500,000,000,000"
    expire(conn)
    etf_scale.synchronize(conn, transport, force=True)
    # Fresh data is not due even when forced; only stale sources are asked.
    assert (
        conn.execute(
            "SELECT net_assets_twd FROM etf_size_snapshots WHERE security_id='e50'"
        ).fetchone()[0]
        == 2_469_507_995_378
    )
    conn.execute(
        "UPDATE etf_scale_coverage SET latest_period='2026-08' WHERE source='size'"
    )
    etf_scale.synchronize(conn, transport, force=True)
    assert conn.execute(
        "SELECT count(*), max(net_assets_twd) FROM etf_size_snapshots "
        "WHERE security_id='e50'"
    ).fetchone()[:] == (1, 2_500_000_000_000)


def test_list_uses_latest_period_only_and_sorts_missing_last(conn, transport):
    etf_scale.synchronize(conn, transport)
    conn.execute(
        "INSERT INTO etf_size_snapshots VALUES('gold','2026-08',999999999999,NULL,"
        "NULL,'u','2026-09-01T00:00:00.000Z')"
    )
    for direction in ("desc", "asc"):
        rows = query.browse(conn, mode="etf", sort="net_assets", direction=direction)
        codes = [(r["code"], r["market"]) for r in rows["results"]]
        known = [("0050", "TWSE"), ("00940", "TWSE")]
        assert codes[:2] == (known if direction == "desc" else known[::-1])
        # 00635U's older month never fills the current column.
        assert ("00635U", "TWSE") in codes[2:]
    holders = query.browse(conn, mode="etf", sort="holders", direction="desc")
    assert query.normalize_sort("holders", None) == ("holders", "desc")
    assert [r["holders"] for r in holders["results"][:3]] == [
        3_720_414,
        307_579,
        58_330,
    ]
    assert holders["scale_dates"] == {"size": "2026/09 月底", "holders": "2026/10/22"}


def test_delisted_rows_have_their_own_filter_category(conn):
    # Design 048 §4.2.1: 全部商品 hides delisted rows unless asked.
    hidden = query.browse(conn, mode="etf")
    assert {r["id"] for r in hidden["results"]} == {"e50", "e940", "gold"}
    assert hidden["delisted_hidden"] == 2
    assert hidden["bank_delisted_count"] == 0
    data = query.browse(conn, mode="etf", include_delisted=True)
    # Two delisted ETFs, three trading ones, none with a bank value.
    assert data["delisted_hidden"] == 0
    assert data["bank_delisted_count"] == 2
    assert data["bank_counts"][None] == 3
    assert data["frequency_counts"]["delisted"] == 2
    delisted = query.browse(conn, mode="etf", bank="delisted")
    assert {r["id"] for r in delisted["results"]} == {"old", "gone"}
    unknown = query.browse(conn, mode="etf", frequency="unknown")
    assert {r["id"] for r in unknown["results"]} == {"e50", "e940", "gold"}
    frequency = query.browse(conn, mode="etf", frequency="delisted")
    assert {r["id"] for r in frequency["results"]} == {"old", "gone"}


def _row(html, code):
    return re.search(
        rf'<tr>\s*<th scope="row" class="col-code"><a[^>]*>{code}</a>.*?</tr>',
        html,
        re.S,
    )[0]


def test_page_shows_units_dates_market_labels_and_short_buttons(
    tmp_path, conn, transport
):
    etf_scale.synchronize(conn, transport)
    app = create_app(tmp_path)
    app.dependency_overrides[get_http_client] = lambda: transport
    with TestClient(app) as client:
        html = client.get("/securities?security_type=etf&view=all&sort=net_assets").text
        with_delisted = client.get(
            "/securities?security_type=etf&view=all&include_delisted=1"
        ).text
        stock = client.get("/securities?security_type=stock&view=all").text
        detail = client.get("/securities/e50").text
    row = _row(html, "0050")
    title = "2,469,507,995,378 元 · 2026/09 月底 · 投信投顧公會"
    assert f'<span title="{title}">24,695.1</span>' in row
    assert ">3,720,414</span>" in row
    assert '<td class="col-status">上市</td>' in row
    assert 'aria-label="加入追蹤 0050 元大台灣50">加入</button>' in row
    gold = _row(html, "00635U")
    assert '<td class="col-number col-size">—</td>' in gold
    assert "規模 2026/09 月底 · 受益人數 2026/10/22" in html
    assert "規模 24,695.1 億" in html  # mobile card
    assert ">資料不足（3）</option>" in html
    # Design 048 §4.2.1: delisted rows and their options wait for the toggle.
    assert 'value="delisted"' not in html
    assert ">包含已下市（2）</a>" in html
    assert 'value="delisted">已下市（2）</option>' in with_delisted
    assert '<option value="delisted">已下市（2）</option>' in with_delisted
    assert ">隱藏已下市</a>" in with_delisted
    assert "data-etf-scale-status" not in stock
    assert "etf-scale.js" not in stock
    assert "ETF · 上市 ·" in detail
    assert '<td class="col-text">上市</td>' in detail


def test_api_refresh_admits_only_stale_sources_and_is_idempotent(
    tmp_path, conn, transport
):
    app = create_app(tmp_path)
    app.dependency_overrides[get_http_client] = lambda: transport
    with TestClient(app) as client:
        before = client.get("/api/v2/etf-scale")
        assert before.headers["cache-control"] == "no-store"
        assert before.json()["needsRefresh"] is True
        assert before.json()["message"] == "規模／受益人數尚未取得"
        payload = {"requestKey": "one"}
        first = client.post("/api/v2/etf-scale/refresh", json=payload)
        assert first.status_code == 202
        job_id = first.json()["jobId"]
        assert client.get(f"/api/v2/sync-jobs/{job_id}").json()["status"] == "success"
        again = client.post("/api/v2/etf-scale/refresh", json=payload)
        assert again.json()["jobId"] == job_id
        second = client.post("/api/v2/etf-scale/refresh", json={"requestKey": "two"})
        assert second.status_code == 200 and second.json()["jobId"] is None
        after = client.get("/api/v2/etf-scale?requestKey=one").json()
        assert after["needsRefresh"] is False and after["requestedJobId"] == job_id
        assert after["message"] == "規模 2026/09 月底 · 受益人數 2026/10/22"
        assert (
            client.post(
                "/api/v2/etf-scale/refresh", json={"requestKey": "x", "force": "yes"}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v2/etf-scale/refresh",
                json={"requestKey": "three"},
                headers={"origin": "https://elsewhere.test"},
            ).status_code
            == 403
        )
    assert conn.execute("SELECT count(*) FROM sync_jobs").fetchone()[0] == 1


def test_schema_can_be_removed_and_reapplied(conn, transport, rollback_sql):
    etf_scale.synchronize(conn, transport)
    conn.executescript(rollback_sql("rollback-etf-scale.sql"))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert not tables & {
        "etf_size_snapshots",
        "etf_holder_snapshots",
        "etf_scale_coverage",
    }
    assert migrate.apply_pending(conn) == [23]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
