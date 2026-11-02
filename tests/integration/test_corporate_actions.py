"""Integration tests for corporate-action entry (design 002 §4.11)."""

import sqlite3

import pytest

from taiwan_stock.infrastructure import migrate
from taiwan_stock.infrastructure.db import get_connection
from taiwan_stock.portfolio import corporate_actions, trades
from taiwan_stock.shared.ids import new_id


@pytest.fixture
def conn(tmp_path):
    connection = get_connection(tmp_path)
    migrate.apply_pending(connection)
    yield connection
    connection.close()


@pytest.fixture
def security_id(conn):
    sid = new_id()
    conn.execute(
        "INSERT INTO securities (id, market, code, name, security_type, status) "
        "VALUES (?, 'TWSE', '2330', 'TSMC', 'stock', 'active')",
        (sid,),
    )
    return sid


def test_split_ratio_helper():
    assert corporate_actions.compute_split_ratio(from_shares=1, to_shares=4) == (4, 1)


def test_stock_dividend_ratio_helper():
    # "每千股配 50 股" -> 1050/1000, not reduced.
    assert corporate_actions.compute_stock_dividend_ratio(50) == (1050, 1000)


def test_create_and_list_corporate_action(conn, security_id):
    numerator, denominator = corporate_actions.compute_split_ratio(1, 4)
    corporate_actions.create_corporate_action(
        conn,
        {
            "security_id": security_id,
            "action_type": "split",
            "ratio_numerator": numerator,
            "ratio_denominator": denominator,
            "ex_date": "2024-03-01",
        },
    )

    rows = corporate_actions.list_corporate_actions(conn, security_id)

    assert len(rows) == 1
    assert rows[0]["ratio_numerator"] == 4
    assert rows[0]["ratio_denominator"] == 1


def test_duplicate_action_on_same_day_is_rejected(conn, security_id):
    fields = {
        "security_id": security_id,
        "action_type": "split",
        "ratio_numerator": 4,
        "ratio_denominator": 1,
        "ex_date": "2024-03-01",
    }
    corporate_actions.create_corporate_action(conn, fields)

    with pytest.raises(corporate_actions.DuplicateActionError):
        corporate_actions.create_corporate_action(conn, fields)


def test_split_immediately_affects_computed_position(conn, security_id):
    account_id = new_id()
    conn.execute(
        "INSERT INTO broker_accounts (id, name, status) VALUES (?, 'Test', 'active')",
        (account_id,),
    )
    trades.create_trade(
        conn,
        {
            "broker_account_id": account_id,
            "security_id": security_id,
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )

    corporate_actions.create_corporate_action(
        conn,
        {
            "security_id": security_id,
            "action_type": "split",
            "ratio_numerator": 2,
            "ratio_denominator": 1,
            "ex_date": "2024-03-01",
        },
    )

    position = trades.compute_position(
        conn, account_id, security_id, as_of_date="2024-06-01"
    )
    assert str(position.remaining_quantity) == "2000"


def test_unhandled_action_lists_only_held_securities_with_non_auto_types(
    conn, security_id
):
    account_id = new_id()
    conn.execute(
        "INSERT INTO broker_accounts (id, name, status) VALUES (?, 'Test', 'active')",
        (account_id,),
    )
    trades.create_trade(
        conn,
        {
            "broker_account_id": account_id,
            "security_id": security_id,
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    # A split should never show up as "unhandled" — it's auto-applied.
    corporate_actions.create_corporate_action(
        conn,
        {
            "security_id": security_id,
            "action_type": "split",
            "ratio_numerator": 2,
            "ratio_denominator": 1,
            "ex_date": "2024-02-01",
        },
    )
    corporate_actions.create_corporate_action(
        conn,
        {
            "security_id": security_id,
            "action_type": "cash_capital_reduction",
            "ex_date": "2024-03-01",
            "note": "減資 20%",
        },
    )

    unhandled = corporate_actions.list_unhandled_actions_for_held_securities(conn)

    assert len(unhandled) == 1
    assert unhandled[0]["action_type"] == "cash_capital_reduction"
    assert unhandled[0]["security_code"] == "2330"


def test_unhandled_action_excludes_securities_with_no_position(conn, security_id):
    account_id = new_id()
    conn.execute(
        "INSERT INTO broker_accounts (id, name, status) VALUES (?, 'Test', 'active')",
        (account_id,),
    )
    # Bought then fully sold — no current position.
    trades.create_trade(
        conn,
        {
            "broker_account_id": account_id,
            "security_id": security_id,
            "event_type": "buy",
            "trade_date": "2024-01-01",
            "quantity": 1000,
            "unit_price": "50",
        },
    )
    trades.create_trade(
        conn,
        {
            "broker_account_id": account_id,
            "security_id": security_id,
            "event_type": "sell",
            "trade_date": "2024-01-15",
            "quantity": 1000,
        },
    )
    corporate_actions.create_corporate_action(
        conn,
        {
            "security_id": security_id,
            "action_type": "other",
            "ex_date": "2024-03-01",
            "note": "換股",
        },
    )

    assert corporate_actions.list_unhandled_actions_for_held_securities(conn) == []


def test_create_corporate_action_rejects_non_positive_ratio(conn, security_id):
    with pytest.raises(corporate_actions.CorporateActionValidationError):
        corporate_actions.create_corporate_action(
            conn,
            {
                "security_id": security_id,
                "action_type": "split",
                "ratio_numerator": 0,
                "ratio_denominator": 1,
                "ex_date": "2024-03-01",
            },
        )


def test_create_corporate_action_rejects_a_reversed_ratio(conn, security_id):
    """A split/stock-dividend ratio must increase share count (§4.11
    point 1) — a numerator <= denominator is very likely the from/to
    share counts entered backwards, and would otherwise only surface
    later as an unexplained OversellError when some page replays the
    position (see replay.OversellError).
    """
    with pytest.raises(corporate_actions.CorporateActionValidationError):
        corporate_actions.create_corporate_action(
            conn,
            {
                "security_id": security_id,
                "action_type": "split",
                "ratio_numerator": 1,
                "ratio_denominator": 4,
                "ex_date": "2024-03-01",
            },
        )


def test_ratio_check_constraint_is_still_enforced_at_the_database_level(
    conn, security_id
):
    """Defense in depth: even bypassing create_corporate_action's
    application-level validation entirely, the schema's own CHECK
    constraint must still reject a non-positive ratio.
    """
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO corporate_actions "
            "(id, security_id, action_type, ratio_numerator, "
            " ratio_denominator, ex_date) VALUES (?, ?, ?, ?, ?, ?)",
            ("raw-bad-ratio", security_id, "split", 0, 1, "2024-03-01"),
        )
