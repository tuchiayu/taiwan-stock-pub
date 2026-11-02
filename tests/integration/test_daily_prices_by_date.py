"""Integration tests for the by-date daily price sync (design 013)."""

from urllib.parse import parse_qs

import httpx2
import pytest

from taiwan_stock.catalog import (
    _parsing,
    sync_daily_prices,
    tpex_adapter,
    twse_adapter,
)
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.web.pages_holdings import _price_sync_summary

TWSE_FIELDS = [
    "證券代號", "證券名稱", "成交股數", "成交筆數", "成交金額", "開盤價",
    "最高價", "最低價", "收盤價", "漲跌(+/-)", "漲跌價差",
]  # fmt: skip
# Padded exactly as seen live (2026-09-29).
TPEX_FIELDS = ["代號", "名稱", "收盤 ", "漲跌", "開盤 ", "最高 ", "最低", "成交股數  "]

TWSE_HOLIDAYS = [
    {"Name": "國曆新年開始交易日", "Date": "1150102"},
    {"Name": "中秋節", "Date": "1150925"},
    {"Name": "孔子誕辰紀念日/ 教師節", "Date": "1150928"},
]


def _twse_row(code, close, volume="1,000"):
    return [code, "名稱", volume, "1", "1", close, close, close, close, "+", "0"]


def _tpex_row(code, close, volume="2,000"):
    return [code, "名稱", close, "0", close, close, close, volume]


class FakeSources:
    """Serves both markets' report pages, the holiday schedule and the
    OpenAPI fallbacks from in-memory data, recording every request."""

    def __init__(self, twse=None, tpex=None, holidays=TWSE_HOLIDAYS):
        self.twse = twse or {}  # ISO date -> rows
        self.tpex = tpex or {}
        self.holidays = holidays
        self.requests: list[tuple[str, str]] = []
        self.twse_fields = TWSE_FIELDS
        self.tpex_echo_date = None

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path.endswith("/MI_INDEX"):
            d = request.url.params["date"]
            iso = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            self.requests.append(("twse", iso))
            if iso not in self.twse:
                return httpx2.Response(
                    200, json={"stat": "很抱歉，沒有符合條件的資料!"}
                )
            table = {
                "title": "115年 每日收盤行情(全部(不含權證、牛熊證、可展延牛熊證))",
                "fields": self.twse_fields,
                "data": self.twse[iso],
            }
            return httpx2.Response(
                200,
                json={"stat": "OK", "date": d, "tables": [{"title": "指數"}, table]},
            )
        if path.endswith("/afterTrading/otc"):
            iso = parse_qs(request.content.decode())["date"][0].replace("/", "-")
            self.requests.append(("tpex", iso))
            rows = self.tpex.get(iso, [])
            roc = self.tpex_echo_date or f"{int(iso[:4]) - 1911}/{iso[5:7]}/{iso[8:]}"
            table = {"date": roc, "totalCount": len(rows), "fields": TPEX_FIELDS}
            return httpx2.Response(
                200, json={"stat": "ok", "tables": [{**table, "data": rows}]}
            )
        if path.endswith(twse_adapter.HOLIDAY_SCHEDULE_PATH):
            if self.holidays is None:
                return httpx2.Response(500)
            return httpx2.Response(200, json=self.holidays)
        if path.endswith(twse_adapter.DAILY_PRICES_PATH):
            self.requests.append(("twse-openapi", ""))
            return httpx2.Response(
                200,
                json=[{"Date": "1150929", "Code": "2330", "ClosingPrice": "1100.00"}],
            )
        if path.endswith(tpex_adapter.DAILY_PRICES_PATH):
            self.requests.append(("tpex-openapi", ""))
            return httpx2.Response(
                200,
                json=[
                    {"Date": "1150929", "SecuritiesCompanyCode": "1240", "Close": "36"}
                ],
            )
        return httpx2.Response(404)

    def dates(self, market):
        return [d for m, d in self.requests if m == market]


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    connection.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('sec-2330', 'TWSE', '2330', '台積電', 'stock', 'active'), "
        "       ('sec-1240', 'TPEx', '1240', '茂生農經', 'stock', 'active')"
    )
    connection.commit()
    yield connection
    connection.close()


def _run(conn, sources, today):
    sleeps = []
    with httpx2.Client(transport=httpx2.MockTransport(sources.handler)) as client:
        outcome = sync_daily_prices.sync_daily_prices_both(
            conn, client, today=today, sleep=sleeps.append
        )
    return outcome, sleeps


def _both_markets(dates):
    return (
        {d: [_twse_row("2330", "1,050.00")] for d in dates},
        {d: [_tpex_row("1240", "35.50")] for d in dates},
    )


def test_saturday_sync_gets_the_two_latest_trading_days(conn):
    twse, tpex = _both_markets(["2026-09-24", "2026-09-23"])
    sources = FakeSources(twse, tpex)

    outcome, _ = _run(conn, sources, today="2026-09-26")

    for market in ("twse", "tpex"):
        result = outcome["results"][market]
        assert result["source"] == "by_date"
        assert result["trade_dates"] == ["2026-09-24", "2026-09-23"]
        assert result["latest_trade_date"] == "2026-09-24"
        # Saturday and the 中秋節 holiday are never requested.
        assert sources.dates(market) == ["2026-09-24", "2026-09-23"]
    rows = conn.execute(
        "SELECT security_id, trade_date, close, volume FROM daily_prices "
        "ORDER BY security_id, trade_date"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("sec-1240", "2026-09-23", "35.50", 2000),
        ("sec-1240", "2026-09-24", "35.50", 2000),
        ("sec-2330", "2026-09-23", "1050.00", 1000),
        ("sec-2330", "2026-09-24", "1050.00", 1000),
    ]


def test_long_weekend_skips_holidays_and_paces_requests(conn):
    twse, tpex = _both_markets(["2026-09-29", "2026-09-24"])
    sources = FakeSources(twse, tpex)

    outcome, sleeps = _run(conn, sources, today="2026-09-29")

    assert outcome["results"]["twse"]["trade_dates"] == ["2026-09-29", "2026-09-24"]
    assert sources.dates("twse") == ["2026-09-29", "2026-09-24"]
    assert sources.dates("tpex") == ["2026-09-29", "2026-09-24"]
    # One pause before each market's second report request.
    assert sleeps == [3.0, 3.0]


def test_dates_already_stored_are_not_requested_again(conn):
    twse, tpex = _both_markets(["2026-09-29", "2026-09-24"])
    sources = FakeSources(twse, tpex)
    _run(conn, sources, today="2026-09-29")
    sources.requests.clear()

    outcome, sleeps = _run(conn, sources, today="2026-09-29")

    assert sources.dates("twse") == [] and sources.dates("tpex") == []
    assert sleeps == []
    for market in ("twse", "tpex"):
        result = outcome["results"][market]
        assert result["trade_dates"] == ["2026-09-29", "2026-09-24"]
        assert result["fetched_dates"] == []
        assert result["synced"] == 0


def test_unverified_legacy_price_is_rechecked_once(conn):
    twse, tpex = _both_markets(["2026-09-29", "2026-09-24"])
    sources = FakeSources(twse, tpex)
    conn.execute(
        "INSERT INTO daily_prices (security_id, trade_date, close, volume) "
        "VALUES ('sec-2330', '2026-09-24', '1000', 1)"
    )
    conn.commit()

    outcome, _ = _run(conn, sources, today="2026-09-29")

    assert sources.dates("twse") == ["2026-09-29", "2026-09-24"]
    assert outcome["results"]["twse"]["fetched_dates"] == ["2026-09-29", "2026-09-24"]
    assert outcome["results"]["twse"]["trade_dates"] == ["2026-09-29", "2026-09-24"]


def test_close_not_published_yet_falls_back_to_earlier_days(conn):
    twse, tpex = _both_markets(["2026-09-24", "2026-09-23"])
    sources = FakeSources(twse, tpex)

    outcome, _ = _run(conn, sources, today="2026-09-29")

    assert sources.dates("twse") == ["2026-09-29", "2026-09-24", "2026-09-23"]
    assert outcome["results"]["twse"]["trade_dates"] == ["2026-09-24", "2026-09-23"]
    assert outcome["results"]["tpex"]["trade_dates"] == ["2026-09-24", "2026-09-23"]


def test_without_holiday_schedule_closed_days_are_detected_by_asking(conn):
    twse, tpex = _both_markets(["2026-09-29", "2026-09-24"])
    sources = FakeSources(twse, tpex, holidays=None)

    outcome, _ = _run(conn, sources, today="2026-09-29")

    # 9/28 and 9/25 now cost a request each, but weekends still don't.
    expected = ["2026-09-29", "2026-09-28", "2026-09-25", "2026-09-24"]
    assert sources.dates("twse") == expected
    assert sources.dates("tpex") == expected
    assert outcome["results"]["twse"]["trade_dates"] == ["2026-09-29", "2026-09-24"]


def test_changed_report_format_falls_back_to_openapi_for_that_market_only(conn):
    twse, tpex = _both_markets(["2026-09-29", "2026-09-24"])
    sources = FakeSources(twse, tpex)
    sources.twse_fields = [f for f in TWSE_FIELDS if f != "收盤價"]

    outcome, _ = _run(conn, sources, today="2026-09-29")

    twse_result = outcome["results"]["twse"]
    assert twse_result["source"] == "openapi_fallback"
    assert "找不到欄位" in twse_result["fallback_reason"]
    assert twse_result["trade_dates"] == ["2026-09-29"]
    assert ("twse-openapi", "") in sources.requests
    assert outcome["results"]["tpex"]["source"] == "by_date"
    assert ("tpex-openapi", "") not in sources.requests


def test_report_ignoring_the_requested_date_falls_back(conn):
    """TPEx's legacy report was seen silently answering with the latest
    day whatever date was asked for; the echoed date must be checked."""
    twse, tpex = _both_markets(["2026-09-29", "2026-09-24"])
    sources = FakeSources(twse, tpex)
    sources.tpex_echo_date = "115/09/30"

    outcome, _ = _run(conn, sources, today="2026-09-29")

    assert outcome["results"]["tpex"]["source"] == "openapi_fallback"
    assert "115/09/30" in outcome["results"]["tpex"]["fallback_reason"]


def test_no_trading_day_within_lookback_falls_back(conn):
    sources = FakeSources()

    outcome, _ = _run(conn, sources, today="2026-09-29")

    result = outcome["results"]["twse"]
    assert result["source"] == "openapi_fallback"
    assert "14 天內找不到" in result["fallback_reason"]
    # Only weekdays that aren't scheduled holidays were asked about.
    assert len(sources.dates("twse")) == 8


def test_rows_without_a_close_are_counted_not_stored(conn):
    sources = FakeSources(
        {
            "2026-09-29": [_twse_row("2330", "--"), _twse_row("9999", "10.00")],
            "2026-09-24": [_twse_row("2330", "1,000.00")],
        },
        _both_markets(["2026-09-29", "2026-09-24"])[1],
    )

    outcome, _ = _run(conn, sources, today="2026-09-29")

    result = outcome["results"]["twse"]
    assert result["skipped_no_price"] == 1
    assert result["skipped_unknown_security"] == 1
    assert result["synced"] == 1


def test_twse_report_echoing_another_date_is_a_format_error():
    def handler(request):
        return httpx2.Response(200, json={"stat": "OK", "date": "20260930"})

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        with pytest.raises(_parsing.QuoteFormatError, match="20260930"):
            twse_adapter.fetch_daily_prices_on(client, "2026-09-29")


def test_twse_report_without_a_closing_table_is_a_format_error():
    def handler(request):
        return httpx2.Response(
            200, json={"stat": "OK", "date": "20260929", "tables": [{"title": "指數"}]}
        )

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        with pytest.raises(_parsing.QuoteFormatError, match="每日收盤行情"):
            twse_adapter.fetch_daily_prices_on(client, "2026-09-29")


@pytest.mark.parametrize(
    ("response", "error"),
    [
        # Seen live on `/afterTrading/dailyQ`: a redirect to an error page.
        (httpx2.Response(302, headers={"location": "/errors"}), httpx2.HTTPError),
        (httpx2.Response(200, json={"stat": "error"}), _parsing.QuoteFormatError),
    ],
)
def test_tpex_report_unexpected_responses_raise(response, error):
    with httpx2.Client(transport=httpx2.MockTransport(lambda r: response)) as client:
        with pytest.raises(error):
            tpex_adapter.fetch_daily_prices_on(client, "2026-09-29")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("115/09/24", "2026-09-24"), ("", None), (None, None), ("115-09-24", None)],
)
def test_roc_slashed_date_to_iso(value, expected):
    assert _parsing.roc_slashed_date_to_iso(value) == expected


def test_holiday_schedule_excludes_days_that_still_trade():
    def handler(request):
        return httpx2.Response(200, json=TWSE_HOLIDAYS)

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        holidays = twse_adapter.fetch_holidays(client)

    assert holidays == {"2026-09-25", "2026-09-28"}


def test_table_rows_matches_padded_headers_and_rejects_missing_ones():
    table = {"fields": [" 代號", "收盤 "], "data": [["1240", "35.50"], ["short"]]}
    assert _parsing.table_rows(table, {"code": "代號", "close": "收盤"}) == [
        {"code": "1240", "close": "35.50"}
    ]
    with pytest.raises(_parsing.QuoteFormatError):
        _parsing.table_rows(table, {"volume": "成交股數"})


def test_homepage_shows_the_sync_summary(tmp_path):
    from fastapi.testclient import TestClient

    from taiwan_stock.main import create_app

    with TestClient(create_app(tmp_path)) as client:
        html = client.get(
            "/?prices_synced=3&price_sync_summary=TWSE 09/29、09/24（寫入 3 筆）"
        ).text

    assert "已同步股價：TWSE 09/29、09/24（寫入 3 筆）" in html


def test_sync_summary_names_dates_and_fallbacks():
    outcome = {
        "results": {
            "twse": {
                "trade_dates": ["2026-09-29", "2026-09-24"],
                "source": "by_date",
                "fallback_reason": None,
            },
            "tpex": {
                "trade_dates": ["2026-09-29"],
                "source": "openapi_fallback",
                "fallback_reason": "HTTP 503",
            },
        }
    }
    assert _price_sync_summary(outcome, 0) == (
        "TWSE 09/29、09/24；TPEx 09/29（依日期來源失敗，改用 OpenAPI：HTTP 503）"
        "（寫入 0 筆）"
    )


def test_absent_code_is_incomplete_retried_once_per_date_and_keeps_old_price(conn):
    days = ["2026-09-29", "2026-09-24"]
    twse, tpex = _both_markets(days)
    sources = FakeSources(twse, tpex)
    _run(conn, sources, today=days[0])
    # Force a new report after adding a code, with the old code absent.
    conn.execute(
        "INSERT INTO securities(id,market,code,name,security_type,status) "
        "VALUES('new','TWSE','9999','New','stock','active')"
    )
    sources.twse = {d: [_twse_row("9999", "20")] for d in days}
    sources.requests.clear()
    outcome, _ = _run(conn, sources, today=days[0])
    assert sources.dates("twse") == days
    assert outcome["results"]["twse"]["incomplete_dates"] == days
    assert (
        conn.execute(
            "SELECT count(*) FROM daily_prices WHERE security_id='sec-2330'"
        ).fetchone()[0]
        == 2
    )
    assert "部分商品未核對" in _price_sync_summary(outcome, 2)
    sources.requests.clear()
    _run(conn, sources, today=days[0])
    assert sources.dates("twse") == days
    # Explicit no-quote now proves coverage and withdraws the old price.
    sources.twse = {d: [_twse_row("2330", "--"), _twse_row("9999", "20")] for d in days}
    outcome, _ = _run(conn, sources, today=days[0])
    assert outcome["results"]["twse"]["incomplete_dates"] == []
    assert (
        conn.execute(
            "SELECT count(*) FROM daily_prices WHERE security_id='sec-2330'"
        ).fetchone()[0]
        == 0
    )
    sources.requests.clear()
    _run(conn, sources, today=days[0])
    assert sources.dates("twse") == []


def test_zero_price_writes_after_checked_no_quote_are_reported_as_fetched(conn):
    days = ["2026-09-29", "2026-09-24"]
    sources = FakeSources(
        {d: [_twse_row("2330", "--")] for d in days},
        {d: [_tpex_row("1240", "--")] for d in days},
    )
    outcome, _ = _run(conn, sources, today=days[0])
    assert all(r["synced"] == 0 for r in outcome["results"].values())
    assert "寫入 0 筆" in _price_sync_summary(outcome, 0)
    assert "未重新下載" not in _price_sync_summary(outcome, 0)
    outcome, _ = _run(conn, sources, today=days[0])
    assert "已是最新，未重新下載" in _price_sync_summary(outcome, 0)
