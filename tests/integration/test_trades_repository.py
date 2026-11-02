"""Integration tests for trade CRUD + the §4.8 oversell check end to end."""

from decimal import Decimal

import pytest

from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.portfolio import replay, trades
from taiwan_stock.shared.ids import new_id


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    yield connection
    connection.close()


@pytest.fixture
def seeded(conn):
    security_id = new_id()
    account_id = new_id()
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES (?, 'TWSE', '2330', 'TSMC', 'stock', 'active')",
        (security_id,),
    )
    conn.execute(
        "INSERT INTO broker_accounts (id, name, status) "
        "VALUES (?, 'Test Broker', 'active')",
        (account_id,),
    )
    return {"security_id": security_id, "account_id": account_id}


@pytest.fixture
def seeded_etf(conn, seeded):
    etf_id = new_id()
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES (?, 'TWSE', '0050', 'Taiwan 50', 'etf', 'active')",
        (etf_id,),
    )
    return {**seeded, "etf_id": etf_id}


def test_create_trade_auto_fills_commission_and_executed_amount_when_blank(
    conn, seeded
):
    trade_id = trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )

    row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    # executed_amount = gross (1000*50); commission = floor(50000*0.001425)=71
    assert row["executed_amount"] == "50000"
    assert row["commission"] == "71"


def test_create_trade_keeps_manually_provided_commission(conn, seeded):
    trade_id = trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 100,
            "unit_price": "9.60",
            "commission": "1",
        },
    )

    row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    # A promotional/manual commission (well under the formula's NT$20
    # floor) must never be silently overwritten by the formula default.
    assert row["commission"] == "1"
    assert row["executed_amount"] == "960"


def test_create_trade_auto_fills_odd_lot_amount_in_whole_ntd(conn, seeded):
    trade_id = trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 37,
            "unit_price": "15.55",
        },
    )

    row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    # 37 × 15.55 = 575.35 -> 575; commission still from the unrounded value.
    assert row["executed_amount"] == "575"
    assert row["commission"] == "20"


def test_create_trade_keeps_manually_provided_executed_amount(conn, seeded):
    trade_id = trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 37,
            "unit_price": "15.55",
            "executed_amount": "575.35",
        },
    )

    row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    assert row["executed_amount"] == "575.35"


def test_create_trade_never_auto_fills_commission_for_opening(conn, seeded):
    trade_id = trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "opening",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )

    row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    # executed_amount still gets the gross default (unit_price was
    # known), but commission is never a fee-bearing concept for an
    # opening entry — see test_opening_cost_excludes_commission_per_design.
    assert row["executed_amount"] == "50000"
    assert row["commission"] is None


def test_create_trade_auto_fills_transaction_tax_for_normal_stock_sell(conn, seeded):
    trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    sell_id = trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "sell",
            "trade_date": "2024-02-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )

    row = conn.execute("SELECT * FROM trades WHERE id = ?", (sell_id,)).fetchone()
    # floor(50000*0.003) = 150
    assert row["transaction_tax"] == "150"


def test_create_trade_day_trade_flag_halves_stock_sell_tax(conn, seeded):
    trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    sell_id = trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "sell",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
            "is_day_trade": True,
        },
    )

    row = conn.execute("SELECT * FROM trades WHERE id = ?", (sell_id,)).fetchone()
    # floor(50000*0.0015) = 75, half of the normal 150.
    assert row["transaction_tax"] == "75"
    assert row["is_day_trade"] == 1


def test_create_trade_etf_sell_ignores_day_trade_flag_for_tax(conn, seeded_etf):
    trades.create_trade(
        conn,
        {
            "broker_account_id": seeded_etf["account_id"],
            "security_id": seeded_etf["etf_id"],
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    sell_id = trades.create_trade(
        conn,
        {
            "broker_account_id": seeded_etf["account_id"],
            "security_id": seeded_etf["etf_id"],
            "event_type": "sell",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
            "is_day_trade": True,
        },
    )

    row = conn.execute("SELECT * FROM trades WHERE id = ?", (sell_id,)).fetchone()
    # ETF tax stays at its own 0.1% rate regardless of the day-trade
    # flag: floor(50000*0.001) = 50, not half of that.
    assert row["transaction_tax"] == "50"


def test_create_trade_forces_is_day_trade_to_none_outside_sell(conn, seeded):
    trade_id = trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
            "is_day_trade": True,
        },
    )

    row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    assert row["is_day_trade"] is None


def test_list_all_trades_total_amount_matches_event_type_formula(conn, seeded):
    trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "opening",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "executed_amount": "50000",
        },
    )
    trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2024-02-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "sell",
            "trade_date": "2024-03-01",
            "quantity": 500,
            "unit_price": "60",
        },
    )

    by_date = {t["trade_date"]: t for t in trades.list_all_trades(conn)}
    # opening: executed_amount alone, no fee addition.
    assert by_date["2024-01-01"]["total_amount"] == "50000"
    # buy: executed_amount (50000) + commission (71).
    assert by_date["2024-02-01"]["total_amount"] == "50071"
    # sell: executed_amount (30000) - commission (floor(30000*.001425)=42)
    # - tax (floor(30000*.003)=90).
    assert by_date["2024-03-01"]["total_amount"] == "29868"


def test_create_trade_and_compute_position(conn, seeded):
    trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
            "commission": "50",
        },
    )

    position = trades.compute_position(
        conn, seeded["account_id"], seeded["security_id"], as_of_date="2024-06-01"
    )

    assert position.remaining_quantity == Decimal(1000)
    assert position.remaining_cost == Decimal(50050)


def test_create_trade_rejects_oversell_and_writes_nothing(conn, seeded):
    trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )

    with pytest.raises(replay.OversellError):
        trades.create_trade(
            conn,
            {
                "broker_account_id": seeded["account_id"],
                "security_id": seeded["security_id"],
                "event_type": "sell",
                "trade_date": "2024-02-01",
                "quantity": 2000,
            },
        )

    rows = conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()
    assert rows["n"] == 1


def test_create_trade_rejects_zero_or_negative_quantity(conn, seeded):
    for bad_quantity in (0, -1):
        with pytest.raises(trades.TradeValidationError):
            trades.create_trade(
                conn,
                {
                    "broker_account_id": seeded["account_id"],
                    "security_id": seeded["security_id"],
                    "event_type": "buy",
                    "trade_date": "2024-01-01",
                    "quantity": bad_quantity,
                    "unit_price": "50",
                },
            )
    rows = conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()
    assert rows["n"] == 0


def test_create_trade_rejects_an_unknown_broker_account(conn, seeded):
    with pytest.raises(trades.TradeValidationError):
        trades.create_trade(
            conn,
            {
                "broker_account_id": "does-not-exist",
                "security_id": seeded["security_id"],
                "event_type": "buy",
                "trade_date": "2024-01-01",
                "quantity": 1000,
                "unit_price": "50",
            },
        )


def test_create_trade_rejects_an_unknown_security(conn, seeded):
    with pytest.raises(trades.TradeValidationError):
        trades.create_trade(
            conn,
            {
                "broker_account_id": seeded["account_id"],
                "security_id": "does-not-exist",
                "event_type": "buy",
                "trade_date": "2024-01-01",
                "quantity": 1000,
                "unit_price": "50",
            },
        )


def test_editing_an_earlier_buy_into_an_oversell_is_rejected(conn, seeded):
    buy_id = trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "sell",
            "trade_date": "2024-02-01",
            "quantity": 800,
        },
    )

    with pytest.raises(replay.OversellError):
        trades.update_trade(conn, buy_id, {"quantity": 500})

    unchanged = conn.execute(
        "SELECT quantity FROM trades WHERE id = ?", (buy_id,)
    ).fetchone()
    assert unchanged["quantity"] == 1000


def test_update_trade_preserves_created_at(conn, seeded):
    buy_id = trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    before = conn.execute(
        "SELECT created_at FROM trades WHERE id = ?", (buy_id,)
    ).fetchone()["created_at"]

    trades.update_trade(conn, buy_id, {"quantity": 1200})

    after = conn.execute(
        "SELECT created_at, quantity FROM trades WHERE id = ?", (buy_id,)
    ).fetchone()
    assert after["created_at"] == before
    assert after["quantity"] == 1200


def test_compute_position_applies_corporate_action_from_db(conn, seeded):
    trades.create_trade(
        conn,
        {
            "broker_account_id": seeded["account_id"],
            "security_id": seeded["security_id"],
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    conn.execute(
        "INSERT INTO corporate_actions "
        "(id, security_id, action_type, ratio_numerator, ratio_denominator, ex_date) "
        "VALUES (?, ?, 'split', 2, 1, '2024-03-01')",
        (new_id(), seeded["security_id"]),
    )

    position = trades.compute_position(
        conn, seeded["account_id"], seeded["security_id"], as_of_date="2024-06-01"
    )

    assert position.remaining_quantity == Decimal(2000)
    # cost = executed_amount (50000, auto-filled from unit_price*quantity)
    # + commission (71, auto-filled: floor(50000*0.001425)=71, above the
    # NT$20 floor) — a split adjusts quantity, not total cost.
    assert position.remaining_cost == Decimal(50071)


def test_future_history_validated_on_insert_and_edit(conn, seeded, monkeypatch):
    monkeypatch.setattr(trades, "today_taipei", lambda: "2026-09-11")
    common = {
        "broker_account_id": seeded["account_id"],
        "security_id": seeded["security_id"],
    }
    buy_id = trades.create_trade(
        conn,
        {
            **common,
            "event_type": "buy",
            "trade_date": "2026-09-12",
            "quantity": 100,
            "unit_price": "50",
        },
    )
    trades.create_trade(
        conn,
        {
            **common,
            "event_type": "sell",
            "trade_date": "2026-09-13",
            "quantity": 100,
        },
    )
    # Future trades are allowed, but excluded from today's position.
    assert (
        trades.compute_position(
            conn, seeded["account_id"], seeded["security_id"]
        ).remaining_quantity
        == 0
    )
    with pytest.raises(replay.OversellError):
        trades.update_trade(conn, buy_id, {"quantity": 50})
    assert (
        conn.execute("SELECT quantity FROM trades WHERE id = ?", (buy_id,)).fetchone()[
            0
        ]
        == 100
    )
    # A sell cannot use shares acquired after its own date.
    with pytest.raises(replay.OversellError):
        trades.create_trade(
            conn,
            {
                **common,
                "event_type": "sell",
                "trade_date": "2026-09-11",
                "quantity": 1,
            },
        )
