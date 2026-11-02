"""Integration tests for backfilling stocks' past cash dividends
(design 014)."""

from datetime import date
from urllib.parse import parse_qs

import httpx2
import pytest
from fastapi.testclient import TestClient
from sync_helpers import load_job

from taiwan_stock import cli
from taiwan_stock.catalog import sync, sync_stock_dividends, tpex_adapter, twse_adapter
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.web.deps import get_http_client

TODAY = "2026-09-29"

TWSE_FIELDS = [
    "資料日期", "股票代號", "股票名稱", "除權息前收盤價", "除權息參考價",
    "權值+息值", "權/息", "漲停價格", "跌停價格", "開盤競價基準",
    "減除股利參考價", "詳細資料", "最近一次申報資料 季別/日期",
    "最近一次申報每股 (單位)淨值", "最近一次申報每股 (單位)盈餘",
]  # fmt: skip
TWSE_DETAIL_FIELDS = [
    "股票代號", "股票名稱", "(每股配發現金股利)除息", "(增資配股) 除權",
]  # fmt: skip
TPEX_FIELDS = [
    "除權息日期", "代號", "名稱", "除權息前收盤價", "除權息參考價", "權值",
    "息值", "權值+息值", "權/息", "漲停價", "跌停價", "開始交易基準價",
    "減除股利參考價", "現金股利",
]  # fmt: skip

# (ISO ex-date, code, 權值+息值, 權/息)
TWSE_EVENTS = [
    ("2022-01-19", "2103", "1.000000", "息"),  # before 2103's first buy
    ("2022-07-19", "2103", "2.400000", "息"),
    ("2026-06-24", "2103", "0.270000", "息"),
    ("2024-07-11", "3481", "0.500000", "息"),  # before 3481's first buy
    ("2025-06-11", "3481", "1.000000", "息"),
    ("2026-05-05", "3481", "0.500000", "權"),  # stock dividend only
    ("2025-08-20", "2363", "0.500000", "息"),  # before 2363's opening
    ("2026-08-19", "2363", "2.881818", "權息"),  # cash part via detail
    ("2026-07-17", "0056", "1.000000", "息"),  # ETF: own source
    ("2025-09-01", "9999", "1.000000", "息"),  # neither traded nor watched
]
TWSE_DETAILS = {("2363", "20260819"): "0.6 元／股"}
# (ISO ex-date, code, 現金股利)
TPEX_EVENTS = [
    ("2024-06-03", "1240", "1.00000000"),  # before 1240's first buy
    ("2025-06-02", "1240", "1.20000000"),
    ("2024-06-03", "6488", "3.00000000"),  # watched-only: outside 2 years
    ("2025-03-03", "6488", "2.00000000"),
    ("2025-09-15", "6488", "0.00000000"),  # 除權 only
]


def _roc_dotted(iso):
    return f"{int(iso[:4]) - 1911}年{iso[5:7]}月{iso[8:]}日"


class FakeSources:
    def __init__(self):
        self.requests: list[tuple[str, str]] = []
        self.tpex_echo = None
        self.twse_status = 200
        self.twse_out_of_range = False

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path.endswith("/TWT49U"):
            start, end = (request.url.params[k] for k in ("startDate", "endDate"))
            self.requests.append(("twse", f"{start}~{end}"))
            if self.twse_status != 200:
                return httpx2.Response(self.twse_status)
            lo, hi = (f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in (start, end))
            rows = [
                [_roc_dotted(d), code, "名", "1", "1", value, kind]
                + ["1", "1", "1", "1", f"{code},{d.replace('-', '')}", "", "", ""]
                for d, code, value, kind in TWSE_EVENTS
                if self.twse_out_of_range or lo <= d <= hi
            ]
            if not rows:
                return httpx2.Response(
                    200, json={"stat": "很抱歉，沒有符合條件的資料!"}
                )
            return httpx2.Response(
                200, json={"stat": "OK", "fields": TWSE_FIELDS, "data": rows}
            )
        if path.endswith("/TWT49UDetail"):
            key = (request.url.params["STK_NO"], request.url.params["T1"])
            self.requests.append(("twse-detail", ",".join(key)))
            row = [key[0], "名", TWSE_DETAILS[key], ""]
            return httpx2.Response(
                200, json={"stat": "ok", "fields": TWSE_DETAIL_FIELDS, "data": [row]}
            )
        if path.endswith("/exDailyQ"):
            form = parse_qs(request.content.decode())
            start, end = form["startDate"][0], form["endDate"][0]
            self.requests.append(("tpex", f"{start}~{end}"))
            lo, hi = start.replace("/", "-"), end.replace("/", "-")
            rows = [
                [f"{int(d[:4]) - 1911}/{d[5:7]}/{d[8:]}", f"{code}   ", "名"]
                + ["1"] * 10
                + [cash]
                for d, code, cash in TPEX_EVENTS
                if lo <= d <= hi
            ]
            echo = self.tpex_echo or f"{start.replace('/', '')}~{end.replace('/', '')}"
            return httpx2.Response(
                200,
                json={
                    "date": echo,
                    "tables": [
                        {"totalCount": len(rows), "fields": TPEX_FIELDS, "data": rows}
                    ],
                },
            )
        if path.endswith(twse_adapter.EX_DIVIDEND_NOTICE_PATH) or path.endswith(
            tpex_adapter.EX_DIVIDEND_NOTICE_PATH
        ):
            return httpx2.Response(200, json=[])
        if path.endswith("/ajax_t108sb27"):
            # Design 015's pay-date step runs after the backfill in the
            # CLI and the sync button; it has its own tests.
            return httpx2.Response(200, text="<h4>查無符合條件之資料</h4>")
        return httpx2.Response(404)

    def of(self, kind):
        return [value for k, value in self.requests if k == kind]


def _seed(conn):
    conn.execute(
        "INSERT INTO broker_accounts (id, name, status) "
        "VALUES ('acc', '測試', 'active')"
    )
    conn.executemany(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES (?, ?, ?, ?, ?, 'active')",
        [
            ("s-2103", "TWSE", "2103", "台橡", "stock"),
            ("s-3481", "TWSE", "3481", "群創", "stock"),
            ("s-2363", "TWSE", "2363", "矽統", "stock"),
            ("s-0056", "TWSE", "0056", "元大高股息", "etf"),
            ("s-9999", "TWSE", "9999", "無關", "stock"),
            ("s-1240", "TPEx", "1240", "茂生農經", "stock"),
            ("s-6488", "TPEx", "6488", "環球晶", "stock"),
        ],
    )
    conn.executemany(
        "INSERT INTO trades (id, broker_account_id, security_id, event_type, "
        "trade_date, quantity, created_at) VALUES (?, 'acc', ?, ?, ?, 1000, ?)",
        [
            (f"t-{sid}", sid, kind, day, f"{day}T00:00:00Z")
            for sid, kind, day in [
                ("s-2103", "buy", "2022-02-24"),
                ("s-3481", "buy", "2024-08-26"),
                ("s-2363", "opening", "2026-03-30"),
                ("s-0056", "buy", "2024-03-07"),
                ("s-1240", "buy", "2025-01-10"),
            ]
        ],
    )
    conn.execute("INSERT INTO watchlist (security_id) VALUES ('s-6488')")
    conn.commit()


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    _seed(connection)
    yield connection
    connection.close()


def _run(conn, sources, **kwargs):
    sleeps = []
    with httpx2.Client(transport=httpx2.MockTransport(sources.handler)) as client:
        outcome = sync_stock_dividends.sync_stock_dividend_history(
            conn, client, today=TODAY, sleep=sleeps.append, **kwargs
        )
    return outcome, sleeps


def _stored(conn):
    return {
        (r["security_id"], r["ex_dividend_date"]): (
            r["amount_per_share"],
            r["amount_status"],
            r["source"],
            r["pay_date"],
        )
        for r in conn.execute("SELECT * FROM distributions")
    }


def test_backfills_each_stock_from_its_own_start(conn):
    sources = FakeSources()

    outcome, sleeps = _run(conn, sources)

    twse, tpex = "twse_ex_rights_result", "tpex_ex_rights_result"
    assert _stored(conn) == {
        ("s-2103", "2022-07-19"): ("2.4000", "final", twse, None),
        ("s-2103", "2026-06-24"): ("0.2700", "final", twse, None),
        ("s-3481", "2025-06-11"): ("1.0000", "final", twse, None),
        ("s-2363", "2026-08-19"): ("0.6000", "final", twse, None),
        ("s-1240", "2025-06-02"): ("1.2000", "final", tpex, None),
        ("s-6488", "2025-03-03"): ("2.0000", "final", tpex, None),
    }
    assert outcome["errors"] == {}
    assert outcome["twse"]["years"] == [2022, 2023, 2024, 2025, 2026]
    assert outcome["twse"]["distributions"] == 4
    assert outcome["twse"]["detail_requests"] == 1
    assert outcome["twse"]["skipped_no_cash"] == 1
    # 3481's and 2363's pre-acquisition events, the ETF and the untracked
    # stock. 2103's 2022-01-19 event is never even fetched: the range
    # starts at its buy date.
    assert outcome["twse"]["skipped_out_of_scope"] == 4
    # The range starts at the earliest acquisition, not 1 January…
    assert sources.of("twse")[0] == "20220224~20221231"
    assert sources.of("twse")[-1] == "20260101~20260929"
    assert sources.of("twse-detail") == ["2363,20260819"]
    # …and for TPEx at the watched-only stock's 2-year lookback.
    assert sources.of("tpex") == [
        "2024/09/29~2024/12/31",
        "2025/01/01~2025/12/31",
        "2026/01/01~2026/09/29",
    ]
    assert outcome["tpex"]["skipped_no_cash"] == 1
    # 4 between TWSE years + 1 before the detail + 2 between TPEx years.
    assert sleeps == [3.0] * 7


def test_rerunning_does_not_duplicate(conn):
    sources = FakeSources()
    _run(conn, sources)
    first = _stored(conn)

    _run(conn, sources)

    assert _stored(conn) == first


def test_result_supersedes_notice_amount_but_keeps_other_fields(conn):
    conn.execute(
        "INSERT INTO distributions (id, security_id, ex_dividend_date, "
        "amount_per_share, amount_status, pay_date) "
        "VALUES ('d-notice', 's-2103', '2026-06-24', '0.3000', 'estimated', "
        "'2026-07-20')"
    )
    conn.commit()

    _run(conn, FakeSources())

    row = conn.execute("SELECT * FROM distributions WHERE id = 'd-notice'").fetchone()
    assert row["amount_per_share"] == "0.2700"
    assert row["amount_status"] == "final"
    assert row["source"] == "twse_ex_rights_result"
    assert row["pay_date"] == "2026-07-20"


def test_watch_years_only_moves_watched_only_stocks(conn):
    sources = FakeSources()

    _run(conn, sources, years=3)

    stored = _stored(conn)
    assert ("s-6488", "2024-06-03") in stored  # now inside 3 years
    assert ("s-1240", "2024-06-03") not in stored  # still before its buy


def test_no_traded_or_watched_stocks_means_no_requests(tmp_path):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    sources = FakeSources()

    outcome, sleeps = _run(connection, sources)
    connection.close()

    assert sources.requests == []
    assert sleeps == []
    assert outcome["twse"]["years"] == [] and outcome["tpex"]["years"] == []


def test_tpex_range_mismatch_fails_only_tpex(conn):
    sources = FakeSources()
    sources.tpex_echo = "20260929~20260929"

    outcome, _ = _run(conn, sources)

    assert "20260929~20260929" in outcome["errors"]["tpex"]
    assert "tpex" not in outcome
    assert ("s-2103", "2026-06-24") in _stored(conn)
    assert not any(sid in ("s-1240", "s-6488") for sid, _ in _stored(conn))


def test_twse_event_outside_requested_range_fails_twse(conn):
    sources = FakeSources()
    sources.twse_out_of_range = True

    outcome, _ = _run(conn, sources)

    assert "不在要求區間" in outcome["errors"]["twse"]
    assert not any(sid.startswith("s-2") or sid == "s-3481" for sid, _ in _stored(conn))
    assert ("s-1240", "2025-06-02") in _stored(conn)


def test_http_failure_is_retried_then_reported(conn, monkeypatch):
    monkeypatch.setattr(sync, "_RETRY_BACKOFF_SECONDS", (0, 0))
    sources = FakeSources()
    sources.twse_status = 503

    outcome, _ = _run(conn, sources)

    assert outcome["errors"]["twse"] == "HTTP 503"
    assert len(sources.of("twse")) == 3  # first try + 2 retries
    assert outcome["tpex"]["distributions"] == 2


def test_watch_lookback_on_leap_day(tmp_path):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    connection.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('s', 'TWSE', '2330', '台積電', 'stock', 'active')"
    )
    connection.execute("INSERT INTO watchlist (security_id) VALUES ('s')")
    connection.commit()

    scope = sync_stock_dividends._stock_dividend_scope(connection, date(2028, 2, 29), 2)
    connection.close()

    assert scope["TWSE"]["2330"] == ("s", "2026-02-28")


def test_cli_stock_dividends(tmp_path, monkeypatch, capsys):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    _seed(connection)
    connection.close()
    sources = FakeSources()
    monkeypatch.setattr(sync_stock_dividends, "_DIVIDEND_HISTORY_DELAY_SECONDS", 0)
    monkeypatch.setattr(sync_stock_dividends, "_PAY_DATE_DELAY_SECONDS", 0)
    monkeypatch.setattr(
        cli,
        "new_client",
        lambda: httpx2.Client(transport=httpx2.MockTransport(sources.handler)),
    )

    code = cli.main(["sync", "stock-dividends", "--data-dir", str(tmp_path)])

    assert code == 0
    assert "個股歷史現金股利" in capsys.readouterr().out
    assert sources.of("twse")


def test_corporate_actions_sync_button_reports_backfill(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_stock_dividends, "_DIVIDEND_HISTORY_DELAY_SECONDS", 0)
    monkeypatch.setattr(sync_stock_dividends, "_PAY_DATE_DELAY_SECONDS", 0)
    sources = FakeSources()

    def override():
        with httpx2.Client(transport=httpx2.MockTransport(sources.handler)) as c:
            yield c

    app = create_app(tmp_path)
    app.dependency_overrides[get_http_client] = override
    with TestClient(app) as client:
        connection = get_connection(tmp_path)
        _seed(connection)
        connection.close()
        response = client.post("/corporate-actions/sync", follow_redirects=False)

    message = load_job(client, response)["summary"]
    assert "歷史現金股利回補" in message
    assert "回補失敗" not in message
