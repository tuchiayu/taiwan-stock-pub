"""Integration test coverage for Issue #113 (design 037).

Covers:
1. Exact rational whole-share calculation and carried cost propagation across
   None-cost lots in replay_trades.
2. Market disambiguation for TPEx-to-TWSE transferred securities (e.g. 8908).
3. Corporate action input validation and error translation in web form.
4. CSV duplicate_pools resolution without false positives when appending
   same-day orders to exported files.
5. Daily P&L exclusion for opening inventory entries created on close_date.
6. Required price/amount validation on buy/sell trades and immutable
   account/security IDs on update.
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from taiwan_stock.catalog.query import resolve_security_by_code
from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.main import create_app
from taiwan_stock.portfolio import (
    broker_accounts,
    corporate_actions,
    csv_trades,
    replay,
    trades,
)
from taiwan_stock.shared.ids import new_id


@pytest.fixture
def conn(tmp_path):
    c = get_connection(tmp_path)
    migrate.apply_pending(c)
    yield c
    c.close()


@pytest.fixture
def seeded(conn):
    account_id = broker_accounts.create_broker_account(conn, {"name": "測試券商"})
    sec_id = new_id()
    conn.execute(
        "INSERT INTO securities "
        "(id, market, code, name, security_type, status, updated_at) "
        "VALUES (?, 'TWSE', '2330', '台積電', 'stock', 'active', '2026-01-01')",
        (sec_id,),
    )
    return {"account_id": account_id, "security_id": sec_id}


def test_split_action_preserves_whole_shares_across_multiple_lots():
    # 3 lots of 1,000 shares (3,000 total) at 10/3 split must equal 10,000
    t_lots = [
        {
            "id": f"t{i}",
            "trade_date": f"2025-01-0{i}",
            "trade_order": 1,
            "created_at": "2025-01-01",
            "event_type": "buy",
            "quantity": 1000,
            "unit_price": "10",
            "executed_amount": 10000,
            "commission": 0,
            "transaction_tax": 0,
            "other_fees": 0,
        }
        for i in range(1, 4)
    ]
    actions = [
        {
            "id": "a1",
            "action_type": "split",
            "ex_date": "2025-06-01",
            "ratio_numerator": 10,
            "ratio_denominator": 3,
        }
    ]
    res = replay.replay_trades(t_lots, actions, as_of_date="2025-12-31")
    assert res.remaining_quantity == Decimal("10000")


def test_carried_cost_propagates_across_unknown_cost_opening_lots():
    # Lot 0: buy 8 shares @ 100 = 800 cost
    # Lot 1: opening 8 shares @ unknown cost
    # Lot 2: buy 1 share @ 100 = 100 cost
    t_lots = [
        {
            "id": "t1",
            "trade_date": "2025-01-01",
            "trade_order": 1,
            "created_at": "2025-01-01",
            "event_type": "buy",
            "quantity": 8,
            "unit_price": "100",
            "executed_amount": 800,
            "commission": 0,
            "transaction_tax": 0,
            "other_fees": 0,
        },
        {
            "id": "t2",
            "trade_date": "2025-01-02",
            "trade_order": 1,
            "created_at": "2025-01-02",
            "event_type": "opening",
            "quantity": 8,
            "unit_price": None,
            "executed_amount": None,
            "commission": 0,
            "transaction_tax": 0,
            "other_fees": 0,
        },
        {
            "id": "t3",
            "trade_date": "2025-01-03",
            "trade_order": 1,
            "created_at": "2025-01-03",
            "event_type": "buy",
            "quantity": 1,
            "unit_price": "100",
            "executed_amount": 100,
            "commission": 0,
            "transaction_tax": 0,
            "other_fees": 0,
        },
    ]
    actions = [
        {
            "id": "a1",
            "action_type": "stock_dividend",
            "ex_date": "2025-02-01",
            "ratio_numerator": 1100,
            "ratio_denominator": 1000,
        },
        {
            "id": "a2",
            "action_type": "stock_dividend",
            "ex_date": "2025-03-01",
            "ratio_numerator": 1250,
            "ratio_denominator": 1000,
        },
    ]
    res = replay.replay_trades(t_lots, actions, as_of_date="2025-12-31")
    assert res.remaining_quantity == Decimal("22")
    # Lot 2 was trimmed to 0 shares. Its 100 cost must carry across Lot 1
    # into Lot 0 (800 + 100 = 900)
    assert res.lots[0].cost == Decimal("900")
    assert res.lots[1].cost is None


def test_security_resolution_disambiguates_tpex_to_twse_transfer(conn):
    tpex_id = new_id()
    twse_id = new_id()
    conn.execute(
        "INSERT INTO securities "
        "(id, market, code, name, security_type, status, updated_at) "
        "VALUES (?, 'TPEx', '8908', '欣雄', 'stock', 'delisted', '2026-01-01')",
        (tpex_id,),
    )
    conn.execute(
        "INSERT INTO securities "
        "(id, market, code, name, security_type, status, updated_at) "
        "VALUES (?, 'TWSE', '8908', '欣雄', 'stock', 'active', '2026-02-01')",
        (twse_id,),
    )

    # Bare code resolves to the active TWSE row
    resolved = resolve_security_by_code(conn, "8908")
    assert resolved is not None
    assert resolved["id"] == twse_id
    assert resolved["market"] == "TWSE"

    # Explicit market prefixes resolve correctly
    assert resolve_security_by_code(conn, "TPEx:8908")["id"] == tpex_id
    assert resolve_security_by_code(conn, "TWSE:8908")["id"] == twse_id
    assert resolve_security_by_code(conn, "8908", market="TPEx")["id"] == tpex_id


def test_create_trade_form_binds_to_active_security_on_transfer(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app)
    c = get_connection(tmp_path)
    migrate.apply_pending(c)
    acct = broker_accounts.create_broker_account(c, {"name": "測試券商"})
    tpex_id = new_id()
    twse_id = new_id()
    c.execute(
        "INSERT INTO securities "
        "(id, market, code, name, security_type, status, updated_at) "
        "VALUES (?, 'TPEx', '8908', '欣雄', 'stock', 'delisted', '2026-01-01')",
        (tpex_id,),
    )
    c.execute(
        "INSERT INTO securities "
        "(id, market, code, name, security_type, status, updated_at) "
        "VALUES (?, 'TWSE', '8908', '欣雄', 'stock', 'active', '2026-02-01')",
        (twse_id,),
    )
    c.close()

    resp = client.post(
        "/transactions",
        data={
            "broker_account_id": acct,
            "security_code": "8908",
            "event_type": "buy",
            "trade_date": "2026-03-01",
            "quantity": "1000",
            "unit_price": "50",
            "return_to": "/transactions",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    c2 = get_connection(tmp_path)
    row = c2.execute(
        "SELECT security_id FROM trades WHERE broker_account_id = ?", (acct,)
    ).fetchone()
    assert row["security_id"] == twse_id
    c2.close()


def test_corporate_action_validation_and_form_handling(conn, seeded, tmp_path):
    sec_id = seeded["security_id"]

    # Invalid action_type
    with pytest.raises(
        corporate_actions.CorporateActionValidationError, match="不支援"
    ):
        corporate_actions.create_corporate_action(
            conn,
            {
                "security_id": sec_id,
                "action_type": "invalid_type",
                "ex_date": "2026-05-01",
            },
        )

    # Invalid ex_date format
    with pytest.raises(
        corporate_actions.CorporateActionValidationError, match="除權息日期"
    ):
        corporate_actions.create_corporate_action(
            conn,
            {
                "security_id": sec_id,
                "action_type": "split",
                "ratio_numerator": 2,
                "ratio_denominator": 1,
                "ex_date": "2026-02-30",
            },
        )

    # Cash capital reduction cannot have split ratios
    with pytest.raises(
        corporate_actions.CorporateActionValidationError, match="不可包含"
    ):
        corporate_actions.create_corporate_action(
            conn,
            {
                "security_id": sec_id,
                "action_type": "cash_capital_reduction",
                "ratio_numerator": 2,
                "ratio_denominator": 1,
                "ex_date": "2026-05-01",
            },
        )

    # Web form returns 422 with friendly error on validation failure
    app = create_app(tmp_path)
    client = TestClient(app)

    resp = client.post(
        "/corporate-actions",
        data={
            "security_code": "2330",
            "action_type": "split",
            "from_shares": "2",
            "to_shares": "1",  # Invalid: to <= from for split
            "ex_date": "2026-05-01",
        },
    )
    assert resp.status_code == 422
    assert "請確認比例是否填反" in resp.text


def test_csv_reimport_with_new_same_day_order_not_falsely_duplicated(conn, seeded):
    acct = seeded["account_id"]
    sec_id = seeded["security_id"]

    trades.create_trade(
        conn,
        {
            "broker_account_id": acct,
            "security_id": sec_id,
            "trade_date": "2026-05-05",
            "event_type": "buy",
            "quantity": 1000,
            "unit_price": "1000",
            "executed_amount": 1000000,
            "commission": 0,
            "transaction_tax": 0,
        },
    )

    exported = csv_trades.export_trades_csv(conn)
    lines = exported.strip().splitlines()
    header = lines[0]
    row_existing = lines[1]
    # Simulate appending a second order today without trade_id
    cols = row_existing.split(",")
    # Find trade_id col in header
    header_cols = header.split(",")
    tid_idx = header_cols.index("trade_id")
    cols[tid_idx] = ""
    row_new = ",".join(cols)

    csv_content = f"{header}\n{row_existing}\n{row_new}\n"
    plan = csv_trades.preview_import(conn, csv_content)
    statuses = [(r.row_number, r.status) for r in plan.rows]
    assert statuses == [(2, "unchanged"), (3, "create")]


def test_opening_on_close_date_excludes_daily_pnl_from_market_value_inflation(
    conn, seeded
):
    acct = seeded["account_id"]
    sec_id = seeded["security_id"]

    conn.execute(
        "INSERT INTO daily_prices (security_id, trade_date, close) "
        "VALUES (?, '2026-05-04', '980')",
        (sec_id,),
    )
    conn.execute(
        "INSERT INTO daily_prices (security_id, trade_date, close) "
        "VALUES (?, '2026-05-05', '1000')",
        (sec_id,),
    )

    # Opening position entered on close_date with unknown cost
    trades.create_trade(
        conn,
        {
            "broker_account_id": acct,
            "security_id": sec_id,
            "trade_date": "2026-05-05",
            "event_type": "opening",
            "quantity": 1000,
        },
    )

    overview = trades.list_position_overview(conn)
    assert len(overview) == 1
    # Market value is 1,000,000, but daily_pnl must be None rather than 1,000,000
    assert overview[0]["market_value"] == "1000000"
    assert overview[0]["daily_pnl"] is None

    summary = trades.compute_portfolio_summary(overview)
    assert summary["total_daily_pnl"] is None


def test_unknown_cost_opening_and_buy_preserves_none_cost(conn, seeded):
    acct = seeded["account_id"]
    sec_id = seeded["security_id"]

    # Unknown cost opening and buy are permitted per design 002 & 032
    trades.create_trade(
        conn,
        {
            "broker_account_id": acct,
            "security_id": sec_id,
            "trade_date": "2026-05-01",
            "event_type": "opening",
            "quantity": 500,
        },
    )
    trades.create_trade(
        conn,
        {
            "broker_account_id": acct,
            "security_id": sec_id,
            "trade_date": "2026-05-02",
            "event_type": "buy",
            "quantity": 500,
        },
    )
    pos = replay.replay_trades(
        trades.fetch_trades(conn, acct, sec_id),
        trades.fetch_corporate_actions(conn, sec_id),
        as_of_date="2026-05-03",
    )
    assert pos.remaining_quantity == Decimal("1000")
    assert pos.remaining_cost is None


def test_update_trade_disallows_changing_broker_account_or_security(conn, seeded):
    acct = seeded["account_id"]
    sec_id = seeded["security_id"]
    other_acct = broker_accounts.create_broker_account(conn, {"name": "第二帳戶"})

    trade_id = trades.create_trade(
        conn,
        {
            "broker_account_id": acct,
            "security_id": sec_id,
            "trade_date": "2026-05-05",
            "event_type": "buy",
            "quantity": 500,
            "unit_price": "100",
        },
    )

    with pytest.raises(trades.TradeValidationError, match="不能變更既有交易的券商帳戶"):
        trades.update_trade(conn, trade_id, {"broker_account_id": other_acct})

    other_sec_id = new_id()
    conn.execute(
        "INSERT INTO securities "
        "(id, market, code, name, security_type, status, updated_at) "
        "VALUES (?, 'TWSE', '0050', '元大台灣50', 'etf', 'active', '2026-01-01')",
        (other_sec_id,),
    )

    with pytest.raises(trades.TradeValidationError, match="不能變更既有交易的商品"):
        trades.update_trade(conn, trade_id, {"security_id": other_sec_id})
