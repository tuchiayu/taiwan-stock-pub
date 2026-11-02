"""Design 023: dividend sync status kept apart from the yield figure, and
yields taken from the latest N distributions of a known frequency."""

import json
import re

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.catalog import query, sync_history
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import trades
from taiwan_stock.web import pages_security_detail

SOURCE = "twse_etf_distribution_list"


@pytest.fixture
def conn(tmp_path):
    conn = get_connection(tmp_path)
    migrate.apply_pending(conn)
    yield conn
    conn.close()


def unit(status, attempted_at):
    return {"status": status, "attempted_at": attempted_at}


def event(ex_date, amount="1", status="final"):
    return {
        "ex_dividend_date": ex_date,
        "amount_per_share": amount,
        "amount_status": status,
    }


def summary(events, units, successes=None, *, frequency=None, close="100"):
    return query.yield_summary(
        events,
        close,
        as_of="2026-10-15",
        frequency=frequency,
        sync=(
            {("e", SOURCE, str(y)): u for y, u in units.items()},
            {("e", SOURCE, str(y)): u for y, u in (successes or units).items()},
            "e",
            "TWSE",
        ),
    )


def test_older_successful_sync_shows_its_date_not_incomplete():
    ok = unit("success", "2026-09-29T02:00:00.000Z")
    info = summary(
        [event("2026-04-16"), event("2026-08-20")], {2026: ok}, frequency="semiannual"
    )

    assert info["known_yield"] == 2
    assert info["quality_label"] is None
    assert info["notes"] == []
    assert info["updated_label"] == "配息更新至 2026/09/29"


def test_missing_year_named_when_latest_events_reach_back_into_it():
    # Annual event 2025-10-09 makes 2025 a required year (§4.3).
    info = summary(
        [event("2025-10-09", "2")],
        {2026: unit("success", "2026-09-29T02:00:00.000Z")},
        frequency="annual",
    )

    assert info["known_yield"] == 2
    assert info["notes"] == ["2025 年配息資料尚未同步"]


def test_older_year_success_does_not_stand_in_for_this_year():
    # 2026 never succeeded; 2025's success date must not read as
    # "updated to" — fall back to what the rows themselves say.
    events = [
        {**event("2025-12-15"), "fetched_at": "2026-09-28T17:00:00.000Z"},
        event("2026-06-15"),
    ]
    info = summary(
        events,
        {
            2025: unit("success", "2026-09-29T02:00:00.000Z"),
            2026: unit("http_failed", "2026-09-29T02:00:00.000Z"),
        },
        {2025: unit("success", "2026-09-29T02:00:00.000Z")},
        frequency="semiannual",
    )

    assert info["updated_on"] is None
    assert info["notes"] == ["2026 年配息更新失敗"]
    assert info["updated_label"] == "紀錄取得於 2026/09/29"  # 17:00Z → Taipei 01:00


def test_failed_retry_keeps_the_previous_success_date():
    info = summary(
        [event("2026-08-20")],
        {2026: unit("http_failed", "2026-10-01T02:00:00.000Z")},
        {2026: unit("success", "2026-09-01T02:00:00.000Z")},
        frequency="annual",
    )

    assert info["known_yield"] == 1
    assert info["notes"] == ["2026 年配息更新失敗（前次成功 2026/09/01）"]
    assert info["updated_label"] == "配息更新至 2026/09/01"


def test_running_sync_keeps_previous_date_without_a_warning():
    info = summary(
        [event("2026-08-20")],
        {2026: unit("running", "2026-10-15T01:00:00.000Z")},
        {2026: unit("success", "2026-09-01T02:00:00.000Z")},
        frequency="annual",
    )

    assert info["notes"] == []
    assert info["updated_label"] == "配息更新至 2026/09/01"


def test_zero_yield_still_requires_a_sync_on_the_taipei_query_date():
    # 23:30Z on 10-14 is 07:30 on 10-15 in Taipei — the old UTC string
    # slice called this "not today".
    years = {2025: unit("empty", "2026-10-14T23:30:00.000Z")}
    today = summary([], {**years, 2026: unit("empty", "2026-10-14T23:30:00.000Z")})
    yesterday = summary([], {**years, 2026: unit("empty", "2026-10-14T15:00:00.000Z")})

    assert today["known_yield"] == 0
    assert today["quality_label"] == "該期間未查得配息"
    assert yesterday["known_yield"] is None
    assert yesterday["quality_label"] == "尚無已知配息紀錄"


def test_never_synced_without_events_is_not_a_non_payer():
    info = summary([], {})

    assert info["known_yield"] is None
    assert info["quality_label"] == "尚未同步配息資料"
    assert info["notes"] == []
    assert info["updated_label"] is None


def test_missing_amounts_are_counted_not_turned_into_zero():
    ok = {2026: unit("success", "2026-09-29T02:00:00.000Z")}
    partial = summary(
        [event("2026-04-16"), event("2026-08-20", None, "unknown")],
        ok,
        frequency="semiannual",
    )
    none_known = summary(
        [event("2026-04-16", None, "unknown"), event("2026-08-20", None, "unknown")],
        ok,
        frequency="semiannual",
    )

    estimated = summary(
        [event("2026-04-16"), event("2026-08-20", "0.5", "estimated")],
        ok,
        frequency="semiannual",
    )

    assert partial["known_yield"] == 1
    assert partial["notes"] == ["1 筆配息金額待確認"]
    assert none_known["known_yield"] is None
    assert none_known["quality_label"] == "2 筆配息金額待確認"
    assert estimated["known_yield"] == 1.5
    assert estimated["notes"] == ["含暫估金額"]


def test_missing_close_keeps_the_update_date():
    info = summary(
        [event("2026-08-20")],
        {2026: unit("success", "2026-09-29T02:00:00.000Z")},
        frequency="annual",
        close=None,
    )

    assert info["known_yield"] is None
    assert info["quality_label"] == "缺少有效收盤價，無法計算"
    assert info["updated_label"] == "配息更新至 2026/09/29"


def test_overdue_short_history_and_extra_payment_notes():
    ok = {
        2025: unit("success", "2026-09-29T02:00:00.000Z"),
        2026: unit("success", "2026-09-29T02:00:00.000Z"),
    }
    overdue = summary([event("2025-08-15")], ok, frequency="annual")
    short = summary([event("2026-08-20")], ok, frequency="semiannual")
    # Paying for years, but only once in range: a real shortfall, not averaged.
    skipped = summary(
        [event("2024-08-20"), event("2025-02-20"), event("2026-08-20")],
        ok,
        frequency="semiannual",
    )
    extra = summary([event("2025-10-20"), event("2025-12-08")], ok, frequency="annual")

    assert overdue["known_yield"] == 1
    assert overdue["notes"] == ["已超過一個配息週期未查得新配息"]
    # Design 046 §4.2.4: started paying in range, so the average × N.
    assert short["known_yield"] == 2
    assert short["notes"] == ["推估（依 1 次）"]
    assert skipped["known_yield"] == 1
    assert skipped["notes"] == ["近一年僅配 1 次（應 2 次）"]
    assert extra["known_yield"] == 2
    assert extra["notes"] == ["近一年含額外配息"]


def seed_0050(conn):
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('e0050', 'TWSE', '0050', '元大台灣50', 'etf', 'active')"
    )
    conn.execute(
        "INSERT INTO etf_profiles (security_id, distribution_frequency_inferred) "
        "VALUES ('e0050', ?)",
        (
            json.dumps(
                {
                    "value": "semiannual",
                    "month_pattern": [1, 7],
                    "basis_dates": ["2026-01-22", "2026-07-21"],
                }
            ),
        ),
    )
    conn.execute(
        "INSERT INTO daily_prices (security_id, trade_date, close) "
        "VALUES ('e0050', '2026-10-14', '113.00')"
    )
    for ex_date, amount in (("2026-01-22", "1.00"), ("2026-07-21", "0.60")):
        conn.execute(
            "INSERT INTO distributions "
            "(id, security_id, ex_dividend_date, amount_per_share, amount_status) "
            "VALUES (?, 'e0050', ?, ?, 'final')",
            (ex_date, ex_date, amount),
        )
    with sync_history.run(conn, SOURCE) as task:
        for year in (2025, 2026):
            task.plan(f"0050:{year}", security_id="e0050", period=str(year))
            task.record(f"0050:{year}", "success")
        conn.execute(
            "UPDATE catalog_sync_units SET attempted_at='2026-09-29T02:00:00.000Z' "
            "WHERE run_id=?",
            (task.id,),
        )
    conn.commit()


def test_0050_list_shows_yield_with_update_and_close_dates(tmp_path, monkeypatch):
    monkeypatch.setattr(query, "today_taipei", lambda: "2026-10-14")
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        seed_0050(conn)
        row = query.browse(conn, mode="etf")["results"][0]
        conn.close()

        assert f"{row['current_yield']:.2f}" == "1.42"
        assert row["yield_notes"] == ["配息更新至 2026/09/29"]
        assert row["close_date"] == "2026-10-14"

        page = client.get("/securities", params={"security_type": "etf"}).text
        assert "1.42%" in page
        assert '<span class="profile-value">半年配</span>' in page
        assert 'class="profile-source tier-inferred"' in page
        assert ">歷史</span>" in page
        assert "資料不完整" not in page
        assert "近一年殖利率 1.42%</p>" in page  # mobile card
        assert '<p class="inline-meta">配息更新至 2026/09/29</p>' in page
        assert "收盤 2026/10/14" in page  # mobile card's close date line


def test_0050_detail_explains_the_calculation(tmp_path, monkeypatch):
    monkeypatch.setattr(pages_security_detail, "today_taipei", lambda: "2026-10-14")
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        seed_0050(conn)
        conn.close()

        page = client.get("/securities/e0050").text

    # Design 046 §4.2.4: the semiannual range starts 3 months before a year ago.
    # Design 051 §4.2.2: two figures up front, the working in 計算明細.
    flat = " ".join(page.split())
    assert '<p class="yield-value">1.42%</p>' in page
    assert "依最近 2 次配息（半年配）" in page
    assert "近一年：2025/07/14 起最近 2 次配息" in page
    assert '>2026/01/22</td><td class="col-number">1.00</td>' in page
    assert '>2026/07/21</td><td class="col-number">0.60</td>' in page
    assert "合計每單位 1.60 元 ÷ 收盤 113 元（2026/10/14）× 100% = 1.42%" in page
    # Design 028 §4.4: the latest payment annualized, not the average.
    assert '<p class="yield-value">1.06%</p>' in page
    assert "依 2026/07/21 每單位 0.60 元 × 2 次" in flat
    assert (
        "推估：每單位 0.60 元 × 2 次 ÷ 收盤 113 元（2026/10/14）× 100% = 1.06%" in flat
    )
    assert '<p class="subtitle">配息更新至 2026/09/29</p>' in page
    assert "配息最後成功同步日期" not in page
    assert "資料不完整" not in page
    assert "最近成功同步時間" not in page


def test_holdings_use_the_same_latest_n_distributions(conn, monkeypatch):
    monkeypatch.setattr(trades, "today_taipei", lambda: "2026-10-15")
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES ('e1', 'TWSE', '00999', '年配 ETF', 'etf', 'active')"
    )
    conn.execute(
        "INSERT INTO etf_profiles (security_id, distribution_frequency) "
        "VALUES ('e1', 'annual')"
    )
    conn.execute(
        "INSERT INTO daily_prices (security_id, trade_date, close) "
        "VALUES ('e1', '2026-10-14', '100')"
    )
    conn.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, amount_per_share, amount_status) "
        "VALUES ('d1', 'e1', '2025-10-09', '5', 'final')"
    )
    conn.execute(
        "INSERT INTO broker_accounts (id, name, status) "
        "VALUES ('acc-1', '測試證券', 'active')"
    )
    conn.execute(
        """
        INSERT INTO trades
            (id, broker_account_id, security_id, event_type, trade_date,
             quantity, unit_price, executed_amount, commission, created_at)
        VALUES ('t-1', 'acc-1', 'e1', 'buy', '2025-01-02',
                1000, '100', '100000', '142', '2025-01-02T00:00:00Z')
        """
    )

    [position] = trades.list_position_overview(conn)

    assert position["dividend_yield"] == "5.00"


def seed_holding(conn, security_id="e2", close="50"):
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES (?, 'TWSE', '00998', '季配 ETF', 'etf', 'active')",
        (security_id,),
    )
    conn.execute(
        "INSERT INTO etf_profiles (security_id, distribution_frequency) "
        "VALUES (?, 'quarterly')",
        (security_id,),
    )
    if close:
        conn.execute(
            "INSERT INTO daily_prices (security_id, trade_date, close) "
            "VALUES (?, '2026-10-14', ?)",
            (security_id, close),
        )
    conn.execute(
        "INSERT INTO broker_accounts (id, name, status) "
        "VALUES ('acc-2', '測試證券', 'active')"
    )
    conn.execute(
        """
        INSERT INTO trades
            (id, broker_account_id, security_id, event_type, trade_date,
             quantity, unit_price, executed_amount, commission, created_at)
        VALUES ('t-2', 'acc-2', ?, 'buy', '2025-01-02',
                1000, '40', '40000', '0', '2025-01-02T00:00:00Z')
        """,
        (security_id,),
    )


def test_holdings_cost_yield_waits_for_a_first_sync(conn, monkeypatch):
    # Design 028 §4.2: never synced is not a confirmed 0.00%.
    monkeypatch.setattr(trades, "today_taipei", lambda: "2026-10-15")
    seed_holding(conn)

    [position] = trades.list_position_overview(conn)

    assert position["cost_yield"] is None
    assert position["cost_yield_label"] == "尚未同步配息資料"


def test_holdings_cost_yield_is_zero_once_today_sync_confirms_none(conn, monkeypatch):
    monkeypatch.setattr(trades, "today_taipei", lambda: "2026-10-15")
    seed_holding(conn)
    with sync_history.run(conn, SOURCE) as task:
        for year in (2025, 2026):
            task.plan(f"00998:{year}", security_id="e2", period=str(year))
            task.record(f"00998:{year}", "empty")
        conn.execute(
            "UPDATE catalog_sync_units SET attempted_at='2026-10-15T02:00:00.000Z' "
            "WHERE run_id=?",
            (task.id,),
        )

    [position] = trades.list_position_overview(conn)

    assert position["cost_yield"] == "0.00"
    assert position["cost_yield_label"] is None


def test_holdings_cost_yield_does_not_need_a_close(conn, monkeypatch):
    monkeypatch.setattr(trades, "today_taipei", lambda: "2026-10-15")
    seed_holding(conn, close=None)
    conn.execute(
        "INSERT INTO distributions "
        "(id, security_id, ex_dividend_date, amount_per_share, amount_status) "
        "VALUES ('d2', 'e2', '2026-07-16', '1', 'final')"
    )

    [position] = trades.list_position_overview(conn)

    assert position["dividend_yield"] is None
    # Design 046: one quarterly payment so far, annualized like the list.
    assert position["cost_yield"] == "10.00"  # 1 × 4 × 1000 ÷ 40000
    assert position["cost_yield_label"] == "推估（依 1 次）"


def test_holdings_pages_show_why_cost_yield_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(trades, "today_taipei", lambda: "2026-10-15")
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        seed_holding(conn)
        conn.commit()
        conn.close()

        home = client.get("/").text
        detail = client.get("/securities/e2").text

    note = '—<small class="cell-note">尚未同步配息資料</small>'
    # Cost and received yield (design 029), each on desktop and mobile.
    assert home.count(note) == 4
    assert note in detail


def test_detail_sync_status_reads_in_chinese_and_taipei_time(tmp_path):
    """Design 051 §4.2.3–4.2.4: no source codes or raw UTC on the pages."""
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        seed_0050(conn)
        conn.close()

        detail = client.get("/securities/e0050").text
        listing = client.get("/securities", params={"security_type": "etf"}).text

    assert "TWSE ETF 配息 2026 年：完成 · 2026-09-29 10:00" in detail
    assert "twse_etf_distribution_list" not in detail
    for page in (detail, listing):
        # JSON handed to scripts stays UTC, like the API; only visible text counts.
        visible = re.sub(
            r'<script type="application/json".*?</script>', "", page, flags=re.S
        )
        m = re.search(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", visible)
        assert not m, visible[m.start() - 300 : m.end() + 40]
