"""Integration tests for homepage daily prices, P&L, and summary cards."""

from decimal import Decimal

from fastapi.testclient import TestClient

from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import trades


def test_homepage_shows_empty_price_state_when_no_daily_prices(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-2330', 'TWSE', '2330', '台積電', 'stock', 'active')"
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
            VALUES
                ('t-1', 'acc-1', 'sec-2330', 'buy', '2026-09-01',
                 1000, '1000', '1000000', '1425', '2026-09-01T00:00:00Z')
            """
        )
        conn.commit()
        conn.close()

        response = client.get("/")
        assert response.status_code == 200
        assert "1 檔缺行情" in response.text
        assert "同步最新股價" in response.text


def test_homepage_shows_market_value_and_unrealized_pnl_when_prices_present(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-2330', 'TWSE', '2330', '台積電', 'stock', 'active')"
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
            VALUES
                ('t-1', 'acc-1', 'sec-2330', 'buy', '2026-09-01',
                 1000, '1000', '1000000', '1425', '2026-09-01T00:00:00Z')
            """
        )
        # Day 1: close = 1020 (prev day)
        conn.execute(
            "INSERT INTO daily_prices (security_id, trade_date, close, volume) "
            "VALUES ('sec-2330', '2026-09-09', '1020.00', 20000000)"
        )
        # Day 2: close = 1050 (latest day)
        conn.execute(
            "INSERT INTO daily_prices (security_id, trade_date, close, volume) "
            "VALUES ('sec-2330', '2026-09-10', '1050.00', 25000000)"
        )
        # Add trailing distribution for yield calculation
        conn.execute(
            """
            INSERT INTO distributions
                (id, security_id, ex_dividend_date, amount_per_share, amount_status)
            VALUES ('d-1', 'sec-2330', '2026-08-01', '4.00', 'final')
            """
        )
        conn.commit()
        conn.close()

    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/")
        assert response.status_code == 200
        # Total market value = 1000 * 1050 = 1050000
        assert "1,050,000" in response.text
        # Cost = 1000000 + 1425 = 1001425
        assert "1,001,425" in response.text
        # Unrealized P&L = 1050000 - 1001425 = 48575.00
        assert "48,575" in response.text
        # Daily P&L = (1050 - 1020) * 1000 = 30000.00
        assert "30,000" in response.text
        # Latest quote date
        assert "2026-09-10" in response.text
        # Cost yield = (4.00 * 1000 / 1001425) * 100 ~= 0.40%
        assert "0.40%" in response.text


def test_holdings_amounts_are_whole_ntd_and_totals_sum_the_rows(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO broker_accounts (id, name, status) "
            "VALUES ('acc-1', '測試證券', 'active')"
        )
        # Legacy odd-lot rows stored before issue #75 still carry cents.
        for sec, code, qty, amount, commission, close in [
            ("sec-a", "2363", 37, "575.35", "20", "15.85"),
            ("sec-b", "2330", 3, "80.40", "20", "33.35"),
        ]:
            conn.execute(
                "INSERT INTO securities (id, market, code, name, security_type, "
                "status) VALUES (?, 'TWSE', ?, ?, 'stock', 'active')",
                (sec, code, code),
            )
            conn.execute(
                """
                INSERT INTO trades
                    (id, broker_account_id, security_id, event_type, trade_date,
                     quantity, executed_amount, commission, created_at)
                VALUES (?, 'acc-1', ?, 'buy', '2026-09-01', ?, ?, ?,
                        '2026-09-01T00:00:00Z')
                """,
                (f"t-{sec}", sec, qty, amount, commission),
            )
            conn.execute(
                "INSERT INTO daily_prices (security_id, trade_date, close, volume) "
                "VALUES (?, '2026-09-10', ?, 1000)",
                (sec, close),
            )
        conn.commit()
        rows = {r["security_id"]: r for r in trades.list_position_overview(conn)}
        summary = trades.compute_portfolio_summary(list(rows.values()))
        conn.close()
        html = client.get("/").text

    # 37 × 15.85 = 586.45 -> 586; cost 595.35 -> 595; P&L 586 - 595.
    assert rows["sec-a"]["market_value"] == "586"
    assert rows["sec-a"]["unrealized_pnl"] == "-9"
    # 3 × 33.35 = 100.05 -> 100; cost 100.40 -> 100; P&L 0.
    assert rows["sec-b"]["market_value"] == "100"
    assert rows["sec-b"]["unrealized_pnl"] == "0"
    # Totals add the whole rows (cost 595 + 100), not 695.75 -> 696.
    assert summary["total_market_value"] == "686"
    assert summary["total_cost"] == "695"
    assert summary["total_unrealized_pnl"] == "-9"
    assert "595.35" not in html and "586.45" not in html
    assert "595" in html and "686" in html


def test_daily_pnl_excludes_a_trade_dated_after_the_latest_synced_price(tmp_path):
    """A buy recorded today, before price sync has run for today, must
    not inflate daily P&L with un-costed shares — the day-over-day
    comparison stays anchored on the latest date we actually have a
    price for (close_date), not on "today"'s live position.
    """
    from taiwan_stock.shared.dates import today_taipei

    today = today_taipei()
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-2330', 'TWSE', '2330', '台積電', 'stock', 'active')"
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
            VALUES
                ('t-1', 'acc-1', 'sec-2330', 'buy', '2026-08-01',
                 1000, '1000', '1000000', '1425', '2026-08-01T00:00:00Z')
            """
        )
        conn.execute(
            "INSERT INTO daily_prices (security_id, trade_date, close, volume) "
            "VALUES ('sec-2330', '2026-09-09', '1020.00', 20000000)"
        )
        conn.execute(
            "INSERT INTO daily_prices (security_id, trade_date, close, volume) "
            "VALUES ('sec-2330', '2026-09-10', '1050.00', 25000000)"
        )
        conn.execute(
            "INSERT INTO trades "
            "(id, broker_account_id, security_id, event_type, trade_date, "
            " quantity, unit_price, executed_amount, commission, created_at) "
            "VALUES ('t-2', 'acc-1', 'sec-2330', 'buy', ?, "
            " 500, '1060', '530000', '750', ?)",
            (today, f"{today}T00:00:00Z"),
        )
        conn.commit()
        conn.close()

    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/")
        assert response.status_code == 200
        # (1050 - 1020) * 1000 = 30000.00 — today's extra 500 shares
        # (no price for `today` yet) contribute nothing here.
        assert "30,000" in response.text


def test_homepage_degrades_gracefully_instead_of_500_on_inconsistent_stored_data(
    tmp_path,
):
    """Simulates data written before corporate_actions.py's ratio
    validation existed (or edited directly in the DB): a corporate
    action with an invalid ratio can make a previously valid sell look
    like an oversell on replay. The homepage must show an error banner,
    not crash with an unhandled 500 that leaves no way to reach any
    other page to fix it.
    """
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-2330', 'TWSE', '2330', '台積電', 'stock', 'active')"
        )
        conn.execute(
            "INSERT INTO broker_accounts (id, name, status) "
            "VALUES ('acc-1', '測試證券', 'active')"
        )
        conn.execute(
            """
            INSERT INTO trades
                (id, broker_account_id, security_id, event_type, trade_date,
                 quantity, unit_price, created_at)
            VALUES
                ('t-1', 'acc-1', 'sec-2330', 'buy', '2026-08-01',
                 1000, '50', '2026-08-01T00:00:00Z'),
                ('t-2', 'acc-1', 'sec-2330', 'sell', '2026-08-15',
                 800, '55', '2026-08-15T00:00:00Z')
            """
        )
        # Bypasses corporate_actions.create_corporate_action's
        # validation on purpose, to reproduce data that predates it.
        conn.execute(
            "INSERT INTO corporate_actions "
            "(id, security_id, action_type, ratio_numerator, "
            " ratio_denominator, ex_date) "
            "VALUES ('ca-1', 'sec-2330', 'split', 1, 4, '2026-08-10')"
        )
        conn.commit()
        conn.close()

    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/")
        assert response.status_code == 200
        # Only the inconsistent pair is set aside and named (#115 #8).
        assert "交易歷史不一致" in response.text
        assert "測試證券 · 2330 台積電" in response.text


def test_daily_pnl_falls_back_to_unit_price_when_same_day_trade_has_no_amount(
    tmp_path,
):
    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-2330', 'TWSE', '2330', '台積電', 'stock', 'active')"
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
            VALUES
                ('t-1', 'acc-1', 'sec-2330', 'buy', '2026-09-01',
                 1000, '1000', '1000000', '1425', '2026-09-01T00:00:00Z')
            """
        )
        # A same-day-as-latest-close buy with only unit_price set (no
        # executed_amount/commission) — exercises trades.py's fallback
        # `amount = unit_price * quantity` in the daily P&L cash-flow sum.
        conn.execute(
            """
            INSERT INTO trades
                (id, broker_account_id, security_id, event_type, trade_date,
                 quantity, unit_price, created_at)
            VALUES
                ('t-2', 'acc-1', 'sec-2330', 'buy', '2026-09-10',
                 100, '1000', '2026-09-10T00:00:00Z')
            """
        )
        conn.execute(
            "INSERT INTO daily_prices (security_id, trade_date, close, volume) "
            "VALUES ('sec-2330', '2026-09-09', '1020.00', 20000000)"
        )
        conn.execute(
            "INSERT INTO daily_prices (security_id, trade_date, close, volume) "
            "VALUES ('sec-2330', '2026-09-10', '1050.00', 25000000)"
        )
        conn.commit()
        conn.close()

    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/")
        assert response.status_code == 200
        # (1050*1100) - (1020*1000) + 0 - (100*1000) = 35000
        assert 'data-sort="35000"' in response.text


def test_homepage_shows_per_lot_and_dividend_inclusive_breakeven(tmp_path):
    from taiwan_stock.portfolio import pricing, trades

    with TestClient(create_app(tmp_path)) as client:
        conn = get_connection(tmp_path)
        conn.execute(
            "INSERT INTO securities (id, market, code, name, security_type, status) "
            "VALUES ('sec-2330', 'TWSE', '2330', '台積電', 'stock', 'active')"
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
            VALUES
                ('t-1', 'acc-1', 'sec-2330', 'buy', '2026-09-01',
                 1000, '1000', '1000000', '1425', '2026-09-01T00:00:00Z')
            """
        )
        # Design 009 §4.6: the dividend-inclusive breakeven basis is now
        # confirmed announced distribution income (pay_date arrived,
        # amount final), not a manually logged dividend_receipts entry.
        conn.execute(
            "INSERT INTO distributions "
            "(id, security_id, ex_dividend_date, pay_date, amount_per_share, "
            " amount_status) "
            "VALUES ('d-1', 'sec-2330', '2026-09-03', '2026-09-10', '2', 'final')"
        )
        conn.commit()
        conn.close()

        response = client.get("/")
        assert response.status_code == 200

        # §4.9 batch breakeven: one lot, same figures as compute_breakeven's
        # single overall lot for a position with a single buy.
        conn = get_connection(tmp_path)
        position = trades.compute_position(conn, "acc-1", "sec-2330")
        schedule = pricing.load_fee_schedule(conn, "stock")
        conn.close()
        breakeven = pricing.compute_breakeven(position, schedule)
        lot_price = breakeven["lots"][0]["breakeven_price"]
        assert f"{lot_price:,}" in response.text

        # Dividend-inclusive breakeven must be lower than the plain
        # breakeven price since NT$2000 (1000 shares x 2/share, confirmed
        # and paid) of the cost is already recovered.
        income_price = pricing.find_breakeven_price(
            position.remaining_quantity,
            position.remaining_cost - Decimal("2000"),
            schedule,
        )
        assert income_price < breakeven["overall_breakeven_price"]
        assert f"{income_price:,}" in response.text


def test_homepage_shows_sync_flash_messages(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        response = client.get("/?prices_synced=15")
        assert response.status_code == 200
        assert "已同步 TWSE／TPEx 最新股價（共 15 筆）" in response.text
        # Design 078: a market-wide count is shown with thousands separators.
        assert "（共 2,394 筆）" in client.get("/?prices_synced=2394").text

        err_response = client.get("/?sync_error=網路連線失敗")
        assert err_response.status_code == 200
        assert "股價同步失敗：網路連線失敗" in err_response.text
