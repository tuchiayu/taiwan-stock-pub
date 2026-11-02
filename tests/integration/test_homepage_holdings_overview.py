"""Integration tests for the homepage holdings table layout (design 012):
average cost, weight, default market-value order, sort attributes and
the expandable holding detail."""

import re

from fastapi.testclient import TestClient

from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import trades
from taiwan_stock.shared import dates


def _seed(tmp_path, statements):
    conn = get_connection(tmp_path)
    conn.execute(
        "INSERT INTO broker_accounts (id, name, status) "
        "VALUES ('acc-1', '測試證券', 'active')"
    )
    for sql, params in statements:
        conn.execute(sql, params)
    conn.commit()
    conn.close()


def _security(sec_id, code, name):
    return (
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES (?, 'TWSE', ?, ?, 'stock', 'active')",
        (sec_id, code, name),
    )


def _buy(trade_id, sec_id, trade_date, quantity, amount, commission, unit_price=None):
    return (
        "INSERT INTO trades "
        "(id, broker_account_id, security_id, event_type, trade_date, quantity, "
        " unit_price, executed_amount, commission, created_at) "
        "VALUES (?, 'acc-1', ?, 'buy', ?, ?, ?, ?, ?, ?)",
        (
            trade_id,
            sec_id,
            trade_date,
            quantity,
            unit_price,
            amount,
            commission,
            f"{trade_date}T00:00:00Z",
        ),
    )


def _price(sec_id, trade_date, close):
    return (
        "INSERT INTO daily_prices (security_id, trade_date, close, volume) "
        "VALUES (?, ?, ?, 1000)",
        (sec_id, trade_date, close),
    )


def _three_holdings():
    return [
        _security("sec-0050", "0050", "元大台灣50"),
        _security("sec-2330", "2330", "台積電"),
        _security("sec-9999", "9999", "無行情"),
        _buy("t-1", "sec-2330", "2026-09-01", 1000, "1000000", "1425"),
        _buy("t-2", "sec-0050", "2026-09-01", 2000, "200000", "285"),
        _buy("t-3", "sec-9999", "2026-09-01", 100, "1000", "20"),
        _price("sec-2330", "2026-09-10", "1050.00"),
        _price("sec-0050", "2026-09-10", "150.00"),
    ]


def test_overview_adds_average_cost_weight_and_sorts_by_market_value(tmp_path):
    with TestClient(create_app(tmp_path)):
        _seed(tmp_path, _three_holdings())

    conn = get_connection(tmp_path)
    overview = trades.list_position_overview(conn)
    conn.close()

    # Largest market value first, the unpriced holding last — not code order.
    assert [row["security_code"] for row in overview] == ["2330", "0050", "9999"]
    by_code = {row["security_code"]: row for row in overview}
    # 1001425 / 1000 = 1001.425 → rounds half up.
    assert by_code["2330"]["average_cost"] == "1001.43"
    assert by_code["0050"]["average_cost"] == "100.14"
    # Weights are shares of the priced total (1050000 + 300000).
    assert by_code["2330"]["weight_pct"] == "77.78"
    assert by_code["0050"]["weight_pct"] == "22.22"
    assert by_code["9999"]["weight_pct"] is None


def test_average_cost_is_none_when_cost_unknown(tmp_path):
    with TestClient(create_app(tmp_path)):
        _seed(
            tmp_path,
            [
                _security("sec-2330", "2330", "台積電"),
                (
                    "INSERT INTO trades "
                    "(id, broker_account_id, security_id, event_type, trade_date, "
                    " quantity, created_at) "
                    "VALUES ('t-1', 'acc-1', 'sec-2330', 'opening', '2026-09-01', "
                    " 1000, '2026-09-01T00:00:00Z')",
                    (),
                ),
            ],
        )

    conn = get_connection(tmp_path)
    [row] = trades.list_position_overview(conn)
    conn.close()
    assert row["average_cost"] is None
    assert row["lot_breakevens"][0]["trade_price"] is None


def test_homepage_renders_sortable_table_in_market_value_order(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        _seed(tmp_path, _three_holdings())
        html = client.get("/").text

    for label in ("總市值", "持有成本", "未實現損益", "當日損益（09/10）", "報酬率"):
        assert label in html
    for old in ("剩餘成本", "持股市值", "單日損益", "全部回本價", "含息回本價", "各批"):
        assert old not in html
    assert "1 檔缺行情" in html

    assert html.count('<tbody class="holding" ') == 3
    assert re.search(r'data-sort-key="market_value"[^>]*aria-sort="descending"', html)
    assert 'data-sort="1050000"' in html
    assert 'data-sort="77.78"' in html
    assert "77.78%" in html
    assert html.index("台積電") < html.index("元大台灣50") < html.index("無行情")
    assert "/static/holdings-table.js" in html
    # Cost yield is a sortable main-table column; the sale-simulation link
    # moved into each holding's detail area.
    assert re.search(r'data-sort-key="cost_yield"[^>]*title="[^"]*近期才買進', html)
    assert html.count('href="/sale-simulation?') == 6  # table detail + mobile card


def test_view_and_sort_controls_sit_in_the_holdings_heading(tmp_path):
    # Design 056: the page header keeps only its three actions.
    with TestClient(create_app(tmp_path)) as client:
        _seed(tmp_path, _three_holdings())
        html = client.get("/?sort=security&direction=ascending&display=cards").text

    start = html.index('class="workspace-controls"')
    header = html[start : html.index("</section>", start)]
    assert "同步最新股價" in header and "新增交易" in header and "計算說明" in header
    assert "holdings-controls" not in header

    heading = html[html.index("holdings-heading") : html.index("mobile-records")]
    assert "最新行情日：2026-09-10 · 金額新臺幣元／數量股" in heading
    assert '<form method="get" action="/" id="holdings-controls"' in heading
    assert '<option value="security" selected>' in heading
    assert '<option value="ascending" selected>由小到大</option>' in heading
    assert "data-holdings-direction-toggle hidden" in heading
    assert re.findall(r'name="display" value="(\w+)"', heading) == [
        "auto",
        "cards",
        "table",
    ]
    assert '<input type="radio" name="display" value="cards" checked>' in heading
    assert heading.count(" checked>") == 1
    # Without JS the form still submits through 套用.
    assert 'type="submit" class="secondary" data-holdings-apply>套用' in heading


def test_holdings_controls_are_absent_without_positions(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        html = client.get("/").text

    assert "還沒有持股紀錄" in html
    assert "holdings-controls" not in html


def test_holding_detail_lists_every_lot_only_when_there_are_several(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        _seed(
            tmp_path,
            [
                _security("sec-2330", "2330", "台積電"),
                _security("sec-0050", "0050", "元大台灣50"),
                _buy("t-1", "sec-2330", "2026-08-01", 1000, "1000000", "1425", "999.5"),
                _buy("t-2", "sec-2330", "2026-08-15", 500, "550000", "783"),
                _buy("t-3", "sec-0050", "2026-08-20", 1000, "150000", "213", "150"),
            ],
        )
        html = client.get("/").text

    assert "2 筆" in html
    assert "1 筆" in html
    assert 'aria-controls="holding-detail-1"' in html
    # One lots table (2330, two lots); 0050's single lot gets a one-liner.
    assert html.count('<table class="lots-table">') == 1
    assert "成交價" in html
    assert "該筆回本價" in html
    assert "扣息回本價" in html
    # Lot price is the recorded unit_price (fees excluded), not cost ÷ shares…
    assert re.search(r"<td\b[^>]*>999\.50</td>", html)
    # …falling back to gross amount ÷ quantity when unit_price is blank.
    assert re.search(r"<td\b[^>]*>1,100\.00</td>", html)
    assert re.search(r"取得日 2026-08-20（[\d,]+ 天），成交價 150\.00", html)


def test_mixed_quote_dates_are_flagged_instead_of_implying_all_current(tmp_path):
    """TPEx can publish today's close while TWSE is still on an older
    day; the header must not present the newest date as if every
    holding were on it."""
    with TestClient(create_app(tmp_path)) as client:
        _seed(
            tmp_path,
            [
                _security("sec-2330", "2330", "台積電"),
                _security("sec-0050", "0050", "元大台灣50"),
                _buy("t-1", "sec-2330", "2026-09-01", 1000, "1000000", "1425"),
                _buy("t-2", "sec-0050", "2026-09-01", 1000, "150000", "213"),
                _price("sec-2330", "2026-09-29", "1050.00"),
                _price("sec-0050", "2026-09-24", "150.00"),
            ],
        )
        conn = get_connection(tmp_path)
        summary = trades.compute_portfolio_summary(trades.list_position_overview(conn))
        conn.close()
        html = client.get("/").text

    assert summary["latest_quote_date"] == "2026-09-29"
    assert summary["stale_quote_count"] == 1
    assert summary["earliest_quote_date"] == "2026-09-24"
    assert "最新行情日：2026-09-29（1 檔較舊，最早 2026-09-24）" in html
    # No single date on the daily P&L card when holdings disagree.
    assert "當日損益（" not in html
    assert "各檔行情日不同" in html
    # Only the lagging holding's close is annotated with its own date.
    assert html.count('class="cell-note stale-date"') == 1
    assert ">09/24</span>" in html


def test_holding_detail_notes_original_quantity_after_a_split(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        _seed(
            tmp_path,
            [
                _security("sec-2330", "2330", "台積電"),
                _buy("t-1", "sec-2330", "2026-08-01", 1000, "1000000", "1425"),
                (
                    "INSERT INTO corporate_actions "
                    "(id, security_id, action_type, ratio_numerator, "
                    " ratio_denominator, ex_date) "
                    "VALUES ('ca-1', 'sec-2330', 'split', 2, 1, '2026-08-10')",
                    (),
                ),
            ],
        )
        html = client.get("/").text

    # The recorded price stays unadjusted; the split shows on the quantity.
    assert re.search(
        r"取得日 2026-08-01（[\d,]+ 天），成交價 1,000\.00，股數 2000\S*（原 1000\S*）",
        html,
    )


def _two_lot_holding():
    return [
        _security("sec-2330", "2330", "台積電"),
        _security("sec-9999", "9999", "無行情"),
        _buy("t-1", "sec-2330", "2026-08-01", 1000, "1000000", "1425"),
        _buy("t-2", "sec-2330", "2026-08-15", 500, "550000", "783"),
        _buy("t-3", "sec-9999", "2026-09-01", 100, "1000", "20"),
        _price("sec-2330", "2026-09-30", "1050.00"),
    ]


def test_each_lot_carries_the_holding_rows_paper_pnl(tmp_path):
    """Design 049 §4.2.2: same whole-NTD paper P&L as the holding row."""
    with TestClient(create_app(tmp_path)):
        _seed(tmp_path, _two_lot_holding())

    token = dates._business_date.set("2026-10-01")
    try:
        conn = get_connection(tmp_path)
        rows = {r["security_code"]: r for r in trades.list_position_overview(conn)}
        conn.close()
    finally:
        dates._business_date.reset(token)

    first, second = rows["2330"]["lot_breakevens"]
    assert (first["holding_days"], second["holding_days"]) == (61, 47)
    assert (first["market_value"], first["unrealized_pnl"]) == ("1050000", "48575")
    assert first["unrealized_pnl_pct"] == "4.85"
    assert (second["market_value"], second["unrealized_pnl"]) == ("525000", "-25783")
    assert second["unrealized_pnl_pct"] == "-4.68"
    assert int(first["unrealized_pnl"]) + int(second["unrealized_pnl"]) == int(
        rows["2330"]["unrealized_pnl"]
    )
    # Without a close every lot figure stays blank instead of guessing.
    [no_quote] = rows["9999"]["lot_breakevens"]
    assert no_quote["market_value"] is None
    assert no_quote["unrealized_pnl"] is None
    assert no_quote["unrealized_pnl_pct"] is None


def test_lot_pnl_is_blank_when_cost_is_unknown(tmp_path):
    with TestClient(create_app(tmp_path)):
        _seed(
            tmp_path,
            [
                _security("sec-2330", "2330", "台積電"),
                (
                    "INSERT INTO trades "
                    "(id, broker_account_id, security_id, event_type, trade_date, "
                    " quantity, created_at) "
                    "VALUES ('t-1', 'acc-1', 'sec-2330', 'opening', '2026-09-01', "
                    " 1000, '2026-09-01T00:00:00Z')",
                    (),
                ),
                _price("sec-2330", "2026-09-30", "1050.00"),
            ],
        )

    conn = get_connection(tmp_path)
    [row] = trades.list_position_overview(conn)
    conn.close()
    [lot] = row["lot_breakevens"]
    assert lot["market_value"] == "1050000"
    assert lot["unrealized_pnl"] is None
    assert lot["unrealized_pnl_pct"] is None


def test_holding_detail_panel_shows_actions_and_lot_pnl(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        _seed(tmp_path, _two_lot_holding())
        html = client.get("/").text

    # Summary and both actions share the top row; the toggle is icon-only.
    assert html.count('class="holding-detail-head"') == 2
    head = html.split('class="holding-detail-head"')[1].split("<table")[0]
    assert head.index(">新增交易</a>") < head.index(">賣出試算</a>")
    assert 'aria-label="持有明細 2 筆"' in html
    assert '<span class="cell-note lots-count">2 筆</span>' in html
    assert "持有明細 · " not in html
    # Lot table columns in order, with signed P&L.
    lots = html.split('<table class="lots-table">')[1].split("</table>")[0]
    columns = re.findall(r"<th class=\"col-[a-z]+\"[^>]*>([^<]+)</th>", lots)
    assert columns == [
        "取得日",
        "持有天數",
        "股數",
        "成交價",
        "成本",
        "市值",
        "未實現損益",
        "報酬率",
        "該筆回本價",
    ]
    assert '<span class="gain">+48,575</span>' in lots
    assert '<span class="loss">−25,783</span>' in lots
    assert '<span class="loss">−4.68%</span>' in lots
    assert re.search(r"<td class=\"col-number\">[\d,]+ 天</td>", lots)
    assert 'title="各筆加總可能與主列差幾元（四捨五入）"' in lots
    # Oldest lot first: FIFO sells consume the table from the top.
    assert 'title="依先進先出排列，賣出時由上往下扣除"' in lots
    assert lots.index("2026-08-01") < lots.index("2026-08-15")
    # Prices keep two decimals everywhere in the table.
    assert re.search(r"<td\b[^>]*>1,000\.00</td>", lots)
    assert re.search(r'data-sort="1050.00" class="col-number">1,050\.00', html)


def test_homepage_explanations_stay_one_line(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        _seed(tmp_path, _three_holdings())
        html = client.get("/").text

    assert '<dl class="calc-notes">' in html
    assert (
        "<dt>未實現損益</dt><dd>市值 − 持有成本（含買進費用，未扣賣出費用）</dd>"
        in html
    )
    assert 'title="市值 − 持有成本；未扣賣出費用"' in html
    assert 'title="回本價再扣除已入帳的配息"' in html
    for long_text in (
        "加碼的資金只計入實際投入的天數",
        "來源無待覆核問題",
        "依各自最新行情計算",
    ):
        assert long_text not in html
